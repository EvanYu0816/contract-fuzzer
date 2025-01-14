from pyfuzz.evm.evm import EvmHandler
from pyfuzz.fuzzer.interface import ContractAbi, Transaction
from pyfuzz.trainer.model import Action, ActionProcessor, State, StateProcessor
from pyfuzz.fuzzer.trace import TraceAnalyzer, branch_op
from pyfuzz.analyzer.static_analyzer import StaticAnalyzer, AnalysisReport
from pyfuzz.fuzzer.detector.exploit import Exploit
from pyfuzz.fuzzer.mythril_concolic import MythrilConcolic
from pyfuzz.config import TRAIN_CONFIG, DIR_CONFIG, FUZZ_CONFIG, CONCOLIC_CONFIG

import logging
from random import shuffle, randint, choice
import json
import os

logger = logging.getLogger("Fuzzer")
logger.setLevel(logging.INFO)


class Fuzzer():
    def __init__(self, evmEndPoint=None, opts={}):
        # load config
        self.opts = opts
        if "exploit" not in opts:
            self.opts["exploit"] = False
        if "vulnerability" not in opts:
            self.opts["vulnerability"] = False
        if "concolic" not in opts:
            self.opts["concolic"] = False
        if "path-coverage" not in opts:
            self.opts["path-coverage"] = False
        # init evm handler
        if evmEndPoint:
            self.evm = EvmHandler(evmEndPoint)
        else:
            self.evm = EvmHandler()
        # contract properties
        self.filename = None
        self.contract = None
        self.contractAbi = None
        self.contractAnalysisReport = None
        # contract cache. Each element is a dict {"name", "contract", "abi", "report", "visited"}
        self.contractMap = {}
        self.contractAddress = None
        # current state
        self.state = None
        self.seqLen = None
        # configurations
        self.maxFuncNum = TRAIN_CONFIG["max_func_num"]
        self.maxCallNum = TRAIN_CONFIG["max_call_num"]
        self.actionNum = TRAIN_CONFIG["action_num"]
        # state and action processor
        self.stateProcessor = StateProcessor()
        self.actionProcessor = ActionProcessor()
        # execution results
        self.traces = []
        self.report = []
        # eth accounts
        self.accounts = self.evm.getAccounts()
        self.defaultAccount = list(self.accounts.keys())[0]
        # analyzers
        self.traceAnalyzer = TraceAnalyzer(opts)
        self.staticAnalyzer = StaticAnalyzer()

        self.mythrilConcolic = None
        self.concolicCnt = 0
        self.concolicWait = CONCOLIC_CONFIG["initial_wait"]
        # for test
        self.counter = 0
        # eth accounts as seeds of type address
        with open(os.path.join(DIR_CONFIG["seed_dir"], 'address.json'), 'w') as f:
            json.dump(list(self.accounts.keys()), f, indent="\t")

    def refreshEvm(self):
        self.evm.reset()

    def loadContract(self, filename, contract_name):
        self.state = None
        self.seqLen = None
        self.traces = []
        self.report = []
        self.counter = 0
        self.filename = filename
        if filename in self.contractMap:
            # the contract is in cache
            self.contract = self.contractMap[filename]["contract"]
            self.contractMap[filename]["abi"] = ContractAbi(self.contract)
            self.contractAbi = self.contractMap[filename]["abi"]
            self.contractAnalysisReport = self.contractMap[filename]["report"]
            self.mythrilConcolic = MythrilConcolic(self.contract["runtime_bytecode"], self.contractAbi)
            return True
        else:
            try:
                with open(filename, "r",encoding="utf-8") as f:
                    source = f.read()
                self.contract = self.evm.compile(source, contract_name)
                contractAddress = self.evm.deploy(self.contract)
                if not contractAddress:
                    return False
                self.contractAbi = ContractAbi(self.contract)
                # run static analysis
                self.staticAnalyzer.load_contract(filename, contract_name)
                self.contractAnalysisReport = self.staticAnalyzer.run()
                # set cache
                self.contractMap[filename] = {
                    "name": contract_name,
                    "contract": self.contract,
                    "abi": self.contractAbi,
                    "report": self.contractAnalysisReport,
                    "visited": set([])
                }
                self.mythrilConcolic = MythrilConcolic(self.contract["runtimeBytecode"], self.contractAbi)
                return True
            except Exception as e:
                logger.exception("fuzz.loadContract: {}".format(str(e)))
                return False

    def runOneTx(self, tx, opts={}):
        if self.contract == None:
            logger.exception("Contract have not been loaded.")
            return None
        trace = self.evm.sendTx(tx.sender, self.contractAddress,
                                str(tx.value), tx.payload, opts)
        if not trace:
            trace = []
        return trace

    def runTxs(self, txList):
        self.contractAddress = self.evm.deploy(self.contract)
        traces = []
        opts = {}
        calls = []

        for tx in txList:
            if not tx:
                calls.append(0)
                traces.append([])
                continue
            if tx.hash in self.contractAnalysisReport.encoded_report:
                calls.append(self.contractAnalysisReport.encoded_report[tx.hash]["features"][0])
            else:
                calls.append(0)
            tx.updateVisited()
            self.contractMap[self.filename]["abi"].updateVisited(tx.hash)
            trace = self.runOneTx(tx, opts)
            if not trace:
                trace = []
            traces.append(trace)
        
        # if there is any call
        if sum(calls) > 0:
            # revert all calls when executing transactions
            opts["revert"] = True
            for tx in txList:
                if not tx:
                    continue
                trace = self.runOneTx(tx, opts)
                if trace:
                    traces.append(trace)
        return traces

    def loadSeed(self, txList, visited, more_seeds=[]):
        """
        add arguments of a tx list to seeds
        """
        new_path_flag = False

        visitedList = self.contractMap[self.filename]["visited"]
        j = 0
        for i in range(len(txList)):
            if not txList[i]:
                continue
            if not self.opts["path-coverage"] and visited[i].issubset(visitedList):
                continue
            if self.opts["path-coverage"] and visited[i] in visitedList:
                continue

            logger.debug("find new path")
            
            # get Seeds
            seeds = self.contractAbi.typeHandlers[txList[i].hash].seeds
            
            new_path_flag = True
            
            if self.opts["path-coverage"]:
                self.contractMap[self.filename]["visited"] = visitedList.union({visited[i]})
            else:
                self.contractMap[self.filename]["visited"] = visitedList.union(visited[i])

            for arg in txList[i].typedArgs:
                if arg[0] not in seeds:
                    seeds[arg[0]] = [arg[1]]
                else:
                    seeds[arg[0]].append(arg[1])

            for seed in more_seeds[i]:
                if seed[0] in seeds:
                    seeds[seed[0]].append(seed[1])
                    logger.debug("load seed", seed)
        return new_path_flag

    def mutate(self, state, action):
        txList = state.txList + []
        actionId = action.actionId
        actionArg = action.actionArg
        txHash = None
        if actionArg < 0 or actionArg >= self.maxCallNum:
            logger.error("wrong action")
            return None
        if txList[actionArg]:
            txHash = txList[actionArg].hash

        # get Seeds
        hashList = []
        for tx in txList:
            if not tx:
                continue
            hashList.append(tx.hash)
        seeds = self.contractAbi.getSeeds(hashList)

        # manual checks
        # NOTE: only use manual checks without training
        logger.debug("action", actionId, actionArg)
        if not txList[-1]:
            actionArg = len(txList) -1
            actionId = 0
        elif txHash == None:
            actionId = 0
        logger.debug("checked action", actionId, actionArg)

        # modify
        if actionId == 0:
            funcHash = txHash
            if len(self.contractAbi.funcHashList) <= 0 or (len(self.contractAbi.funcHashList) == 1 and txHash in self.contractAbi.funcHashList):
                return None
            candidateFunc = []
            for funcHash in self.contractAbi.funcHashList:
                if funcHash == txHash or not funcHash in self.contractAnalysisReport.func_map:
                    continue
                if self.contractAnalysisReport.func_map[funcHash].features["call"] > 0:
                    candidateFunc.append(funcHash)
                    continue
                read_set = set()
                write_set = set(self.contractAnalysisReport.func_map[funcHash]._vars_written)
                for i in range(actionArg + 1, TRAIN_CONFIG["max_call_num"]):
                    if not isinstance(state.txList[i], Transaction):
                        continue
                    tmp_hash = state.txList[i].hash
                    if tmp_hash in self.contractAnalysisReport.func_map:
                        read_set = read_set.union(self.contractAnalysisReport.func_map[tmp_hash]._vars_read)
                if len(read_set.intersection(write_set)) > 0:
                    candidateFunc.append(funcHash)
                    continue

            if len(candidateFunc) == 0:
                return None
            selectedFuncHash = choice(candidateFunc)
            tx = self.contractAbi.generateTx(selectedFuncHash, self.defaultAccount, seeds)
            txList[actionArg] = tx
        elif actionId == 1:
            # modify args
            if txHash == None:
                return None
            txList[actionArg].args = self.contractAbi.generateTxArgs(txHash, seeds)
        elif actionId == 2:
            # modify sender
            if txHash == None:
                return None
            sender = state.txList[actionArg].sender
            attempt = 100
            while sender == state.txList[actionArg].sender and attempt > 0:
                randIndex = randint(0, len(self.accounts.keys())-1)
                sender = list(self.accounts.keys())[randIndex]
                attempt -= 1
            txList[actionArg].sender = sender
        elif actionId == 3:
            # modify value
            if txHash == None:
                return None
            if not self.contractAbi.interface[txHash]['payable']:
                # not payable function
                return None
            value = state.txList[actionArg].value
            attempt = 100
            while value == state.txList[actionArg].value and attempt > 0:
                value = self.contractAbi.generateTxValue(txHash, seeds)
                attempt -= 1
            txList[actionArg].value = value
        else:
            return None
        return State(state.staticAnalysis, txList)

    def reset(self):
        """
        reset the fuzzer with current contract
        """
        self.counter = 0
        if not self.contract:
            logger.exception("Contract not inintialized in fuzzer.")
            return None, None
        self.contractAddress = self.evm.deploy(self.contract)

        self.state = State(self.contractAnalysisReport, [
                           None for i in range(self.maxCallNum)])
        self.traces = []
        self.report = []
        state, seqLen = self.stateProcessor.encodeState(self.state)
        return state, seqLen

    def random_reset(self, datadir):
        """
        randomly select a contract from datadir and reset
        """
        contract_files = os.listdir(datadir)
        filename = choice(contract_files)
        state, seq_len = self.contract_reset(datadir, filename)
        while seq_len == None:
            try:
                filename = choice(contract_files)
                state, seq_len = self.contract_reset(datadir, filename)
            except:
                state, seq_len = None, None
        return state, seq_len, filename

    def contract_reset(self, datadir, filename):
        """
        reset fuzzer with the given contract file
        """
        assert(filename != None)
        full_filename = os.path.join(datadir, filename)
        # todo: this is only proper for our dataset
        contract_name = filename.split('.')[0].split("#")[-1]
        if self.loadContract(full_filename, contract_name):
            return self.reset()
        else:
            return None, None

    def step(self, action):
        done = 0
        self.counter += 1
        reward = 0
        timeout = 0
        p_coverage = self.coverage()
        # print(self.state.txList)
        try:
            # testing
            if self.counter >= FUZZ_CONFIG["max_attempt"]:
                timeout = 1
            # concolic
            nextState = None
            if self.opts["concolic"] and self.concolicCnt >= self.concolicWait:
                _, _, jumpi, _ = self.traceAnalyzer.path_variaty(self.traces, self.traces)
                # print(jumpi)
                print("start concolic")
                result = self.mythrilConcolic.run(self.state.txList, jumpi)
                if len(result) > 0:
                    nextState = State(self.state.staticAnalysis, result)
                self.concolicCnt = 0
                if nextState:
                    self.concolicWait = max(1, int(self.concolicWait/CONCOLIC_CONFIG["concolic_reward"]/CONCOLIC_CONFIG["concolic_penalty"]))
            else:
                action = self.actionProcessor.decodeAction(action)
                nextState = self.mutate(self.state, action)

            if not nextState:
                state, seqLen = self.stateProcessor.encodeState(self.state)
                return state, seqLen, reward, done, timeout
            # execute transactions
            traces = self.runTxs(nextState.txList)
            # get reward of executions
            reward, report, pcs, seeds, paths = self.traceAnalyzer.run(self.traces, traces)
            self.traces = traces
            # bonus for valid mutation
            reward += FUZZ_CONFIG["valid_mutation_reward"]
            # update seeds
            tmp_visited = paths if self.opts["path-coverage"] else pcs
            if self.loadSeed(nextState.txList, tmp_visited, seeds):
                reward += FUZZ_CONFIG["path_discovery_reward"]
            # check whether exploitation happens
            if self.opts["exploit"]:
                self.accounts = self.evm.getAccounts()
                # balance increase
                bal_p = 0
                bal = 0
                for acc in self.accounts.keys():
                    bal_p += int(str(FUZZ_CONFIG["account_balance"]), 16)
                    bal += int(str(self.accounts[acc]), 16)

                if bal > bal_p:
                    reward += FUZZ_CONFIG["exploit_reward"]
                    report.append(Exploit("BalanceIncrement", nextState.txList))
            # fill in transasactions for exploitation
            for rep in report:
                if isinstance(rep, Exploit):
                    rep.txList = nextState.txList
            # testing
            if len(report) > 0:
                done = 1

            if p_coverage == self.coverage():
                self.concolicCnt += 1
            else:
                self.concolicCnt = 0
                self.concolicWait = int(self.concolicWait * CONCOLIC_CONFIG["concolic_penalty"])
            # update
            self.state = nextState
            self.traces = traces
            # should exclude repeated reports
            self.report = list(set(self.report + report))
            state, seqLen = self.stateProcessor.encodeState(self.state)
            _, _, jumpi, _= self.traceAnalyzer.path_variaty(self.traces, self.traces)
            print(self.coverage(), jumpi)
            return state, seqLen, reward, done, timeout
        except Exception as e:
            logger.error("fuzzer.step: {}".format(str(e)))
            state, seqLen = self.stateProcessor.encodeState(self.state)
            return state, seqLen, 0, 0, 1

    def coverage(self):
        if self.opts["path-coverage"]:
            return len(self.contractMap[self.filename]["visited"])
        jump_cnt = 0
        try:
            jump_cnt = self.contract["opcodes"].count("JUMP")
        except:
            pass

        if jump_cnt == 0:
            return 1
        else:
            return len(self.contractMap[self.filename]["visited"]) / jump_cnt

    def printTxList(self):
        print("TX LIST:")
        txList = self.state.txList
        for i in range(len(txList)):
            if not txList[i]:
                continue
            function = ""
            abi = self.contractAbi.interface[txList[i].hash]
            if "name" in abi:
                function = abi["name"]
            print("[{}] function: {}, args: {}, sender: {}, value: {}".format(
                str(i), function, txList[i].args, txList[i].sender[:4], txList[i].value))