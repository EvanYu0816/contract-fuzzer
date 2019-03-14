from pyfuzz.evm.evm import *
from pyfuzz.fuzzer.interface import ContractAbi, Transaction
from pyfuzz.trainer.model import *
from pyfuzz.fuzzer.trace import *
from pyfuzz.analyzer.static_analyzer import *
from pyfuzz.config import TRAIN_CONFIG, DIR_CONFIG

import logging
from random import randint
import json

class Fuzzer():
    def __init__(self, evmEndPoint=None):
        if evmEndPoint:
            self.evm = EvmHandler(evmEndPoint)
        else:
            self.evm = EvmHandler()
        self.contract = None
        self.contractAddress = None
        self.contractAbi = None
        self.state = None
        self.seqLen = None
        self.maxFuncNum = TRAIN_CONFIG["max_func_num"]
        self.maxCallNum = TRAIN_CONFIG["max_call_num"]
        self.actionNum = TRAIN_CONFIG["action_num"]
        self.stateProcessor = StateProcessor()
        self.actionProcessor = ActionProcessor()
        self.traces = []
        self.reports = []
        self.accounts = self.evm.getAccounts()
        self.defaultAccount = list(self.accounts.keys())[1]
        with open(os.path.join(DIR_CONFIG["seed_dir"], 'address.json'), 'w') as f:
            json.dump(list(self.accounts.keys()), f, indent="\t")
        self.traceAnalyzer = TraceAnalyzer()
        self.staticAnalyzer = StaticAnalyzer()
        self.counter = 0

    def loadContract(self, filename, contract_name):
        with open(filename, "r") as f:
            source = f.read()
        self.contract = self.evm.compile(source, contract_name)
        # self.contractAddress = self.evm.deploy(self.contract)
        self.contractAbi = ContractAbi(self.contract)
        # run static analysis
        self.staticAnalyzer.load_contract(filename, contract_name)
        self.staticAnalyzer.run()

    def runOneTx(self, tx):
        if self.contract == None:
            logging.error("Contract have not been loaded.")
            return None
        trace = self.evm.sendTx(tx.sender, self.contractAddress,
                                tx.value, tx.payload)
        return trace

    def runTxs(self, txList):
        traces = []
        for tx in txList:
            traces.append(self.runOneTx(tx))
        return traces

    def reward(self, traces):
        self.counter += 1
        report, reward = self.traceAnalyzer.run(self.traces, traces)

        self.accounts_p = self.accounts
        self.accounts = self.evm.getAccounts()
        # balance increase
        bal_p = 0
        bal = 0
        for acc in self.accounts.keys():
            bal_p += int(self.accounts_p[acc], 16)
            bal += int(self.accounts[acc], 16)

        if bal > bal_p:
            reward += 1
            report.append("balanceIncrease")
        self.traces = traces
        return reward, report

    def mutate(self, state, action):
        txList = state.txList + []
        actionId = action.actionId
        actionArg = action.actionArg
        if actionId >= 0 and actionId < 2:
            # insert
            if actionArg < 0 or actionArg >= len(self.contractAbi.funcHashList):
                return None
            if len(txList) >= self.maxCallNum:
                return None
            tx = self.contractAbi.generateTx(
                self.contractAbi.funcHashList[actionArg], self.defaultAccount)
            if actionId == 0:
                # insert at first
                txList.insert(0, tx)
            else:
                # insert at last
                txList.append(tx)
        elif actionId < 4:
            # remove
            if len(txList) <= 0:
                # cannot remove
                return None
            if actionId == 2:
                # remove the first
                del txList[0]
            else:
                # remove the last
                txList.pop()
        elif actionId < 7:
            # modify
            if actionArg < 0 or actionArg >= len(txList):
                return None
            funcHash = state.txList[actionArg].hash
            if actionId == 4:
                # modify args
                txList[actionArg].args = self.contractAbi.generateTxArgs(funcHash)
            elif actionId == 5:
                # modify sender
                sender = state.txList[actionArg].sender
                attempt = 100
                while sender == state.txList[actionArg].sender and attempt > 0:
                    randIndex = randint(1, len(self.accounts.keys())-1)
                    sender = list(self.accounts.keys())[randIndex]
                    sender = self.defaultAccount
                    attempt -= 1
                txList[actionArg].sender = sender
            else:
                # modify value
                if not self.contractAbi.interface[funcHash]['payable']:
                    # not payable function
                    return None
                value = state.txList[actionArg].value
                attempt = 100
                while value == state.txList[actionArg].value and attempt > 0:
                    value = self.contractAbi.generateTxValue(funcHash)
                    attempt -= 1
                txList[actionArg].value = value
        else:
            return None
        return State(state.staticAnalysis, txList)

    def reset(self):
        self.counter = 0
        if not self.contract:
            logging.error("Contract not inintialized in fuzzer.")
            return
        self.contractAddress = self.evm.deploy(self.contract)
        # todo
        self.state = State(self.staticAnalyzer.report, [])
        self.traces = []
        self.reports = []
        # randomFuncIndex = randint(0, len(self.contractAbi.funcHashList)-1)
        # self.state = self.mutate(self.state, actionProcessor.encode(0, randomFuncIndex))
        state, seqLen = self.stateProcessor.encodeState(self.state)
        return state, seqLen

    def step(self, action):
        done = 0
        action = self.actionProcessor.decodeAction(action)
        nextState = self.mutate(self.state, action)
        if not nextState:
            state, seqLen = self.stateProcessor.encodeState(self.state)
            return state, seqLen, 0, done
        # testing
        # self.printTxList(nextState.txList)
        traces = self.runTxs(nextState.txList)
        reward, report = self.reward(traces)
        # testing
        # print(reward, report)
        # testing
        if self.counter >= 100 or len(report) > 0:
            done = 1
        # update
        self.state = nextState
        self.traces = traces
        # should exclude repeated reports
        self.reports = list(set(self.reports + report))
        state, seqLen = self.stateProcessor.encodeState(self.state)
        return state, seqLen, reward, done

    @staticmethod
    def printTxList(txList):
        print("txList:")
        for i in range(len(txList)):
            print("[{}] payload: {}, sender: {}, value: {}".format(
                str(i), txList[i].payload, txList[i].sender, txList[i].value))