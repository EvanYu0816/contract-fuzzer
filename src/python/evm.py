"""python interface for evm functions"""

import json
import grpc
import os
import logging

import evm_pb2
import evm_pb2_grpc

class EvmHandler():
    def __init__(self, endpoint='localhost:50051'):
        self.channel = grpc.insecure_channel(endpoint)
        self.stub = evm_pb2_grpc.EVMStub(self.channel)

    def reset(self, option=0):
        status = evm_pb2.Status(option=option)
        ret = self.stub.Reset(status)
        return ret.option

    def getAccounts(self, option=0):
        status = evm_pb2.Status(option=option)
        ret = None
        for i in self.stub.GetAccounts(status):
            ret = i.data
            break
        if not ret or len(ret) > 0:
            return json.loads(ret)
        else:
            logging.error("Correct response not received from rpc server (getAccounts)")
            return None

    def compile(self, text, name):
        source = evm_pb2.Source(text=text, name=name)
        ret = self.stub.Compile(source)
        if not ret or len(ret.data) > 0:
            return json.loads(ret.data)
        else:
            logging.error("Correct response not received from rpc server (compile)")
            return None

    def deploy(self, contract):
        contractRpc = evm_pb2.Json(data=json.dumps(contract))
        ret = None
        for i in self.stub.Deploy(contractRpc):
            ret = i.address
            break
        return ret

    def sendTx(self, fromAddr, toAddr, value, data):
        sendTxData = evm_pb2.SendTxData(fromAddr=fromAddr, toAddr=toAddr, value=value, data=data)
        ret = None
        for i in self.stub.SendTx(sendTxData):
            ret = i.data
            break
        if not ret or len(ret) > 0:
            return json.loads(ret)
        else:
            logging.error("Correct response not received from rpc server (sendTx)")
            return None