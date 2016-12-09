#!/usr/bin/env python3
# Copyright (c) 2016 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

from test_framework.test_framework import BitcoinTestFramework
from test_framework import blocktools
from test_framework.mininode import CTransaction
from test_framework.util import *
from test_framework.util import *

import io
import time

class BumpFeeTest (BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 2
        self.setup_clean_chain = True

    def setup_network(self, split=False):
        self.nodes = []
        self.nodes.append(start_node(0, self.options.tmpdir, ["-debug", "-walletrbf"]))
        self.nodes.append(start_node(1, self.options.tmpdir, ["-debug"]))
        connect_nodes_bi(self.nodes,0,1)
        self.is_network_split=False
        self.sync_all()

    def create_fund_sign_send(self, node, outputs, feerate=0):
        if feerate != 0:
            node.settxfee(feerate)
        rawtx = node.createrawtransaction([], outputs)
        fundtx = node.fundrawtransaction(rawtx)
        signedtx = node.signrawtransaction(fundtx['hex'])
        txid = node.sendrawtransaction(signedtx['hex'])
        return txid


    def run_test (self):

        print("Mining blocks...")

        # fund node0 with 10 coins of 0.001 btc (100,000 satoshis)
        self.nodes[1].generate(110)
        self.sync_all()
        a0 = self.nodes[0].getnewaddress()
        a1 = self.nodes[1].getnewaddress()
        for i in range(10):
            self.nodes[1].sendtoaddress(a0, 0.001)
        self.sync_all()
        self.nodes[1].generate(1)
        self.sync_all()
        assert_equal(self.nodes[0].getbalance(), Decimal('0.01'))

        # create and bump an RBF transaction
        outputs = {a1:0.00090000}
        rbfid = self.create_fund_sign_send(self.nodes[0], outputs)
        rbftx = self.nodes[0].gettransaction(rbfid)
        sync_mempools(self.nodes)
        assert(rbfid in self.nodes[0].getrawmempool() and rbfid in self.nodes[1].getrawmempool())
        bumped_tx = self.nodes[0].bumpfee(rbfid)
        assert(bumped_tx['fee'] - abs(rbftx['fee']) > 0)

        # check that bumped_tx propogates, original tx was evicted and has a wallet conflict
        sync_mempools(self.nodes)
        assert(bumped_tx['txid'] in self.nodes[0].getrawmempool())
        assert(bumped_tx['txid'] in self.nodes[1].getrawmempool())
        assert(rbfid not in self.nodes[0].getrawmempool())
        assert(rbfid not in self.nodes[1].getrawmempool())
        oldwtx = self.nodes[0].gettransaction(rbfid)
        assert(len(oldwtx['walletconflicts']) > 0)

        # check wallet transaction replaces and replaced_by values
        bumpedwtx = self.nodes[0].gettransaction(bumped_tx['txid'])
        assert_equal(oldwtx['replaced_by_txid'], bumped_tx['txid'])
        assert_equal(bumpedwtx['replaces_txid'], rbfid)

        # cannot replace a non RBF transaction (use node1, which did not enable RBF)
        outputs = {a1:0.00090000}
        not_rbfid = self.create_fund_sign_send(self.nodes[1], outputs)
        assert_raises_message(JSONRPCException, "not BIP 125 replaceable", self.nodes[1].bumpfee, not_rbfid)

        # cannot bump fee unless the tx has only inputs that we own.
        # here, the rbftx has a node1 coin and then adds a node0 input
        # Note that this test depends upon the RPC code checking input ownership prior to change outputs
        # (since it can't use fundrawtransaction, it lacks a proper change output)
        #
        utxo = self.nodes[0].listunspent().pop()
        utxo1 = self.nodes[1].listunspent().pop()
        inputs = []
        inputs.append({"txid":utxo["txid"],"vout":utxo["vout"],"address":utxo["address"],"sequence":1000})
        inputs.append({"txid":utxo1["txid"],"vout":utxo1["vout"],"address":utxo1["address"],"sequence":1000})
        output_val = utxo['amount'] + utxo1['amount'] - Decimal('0.001')
        outputs = {a0:output_val}
        rawtx = self.nodes[0].createrawtransaction(inputs, outputs)
        signedtx = self.nodes[0].signrawtransaction(rawtx)
        signedtx = self.nodes[1].signrawtransaction(signedtx['hex'])
        rbfid = self.nodes[0].sendrawtransaction(signedtx['hex'])
        assert_raises_message(JSONRPCException, "Transaction contains inputs that don't belong to this wallet", self.nodes[0].bumpfee, rbfid)

        # cannot bump fee if the transaction has a descendant
        # parent is send-to-self, so we don't have to check which output is change when creating the child tx
        parent_id = self.create_fund_sign_send(self.nodes[0], {a0:0.00050000})
        inputs = [ {'txid':parent_id, 'vout':0} ]
        outputs = {a1:0.00020000}
        tx = self.nodes[0].createrawtransaction(inputs, outputs)
        tx = self.nodes[0].signrawtransaction(tx)
        txid = self.nodes[0].sendrawtransaction(tx['hex'])
        assert_raises_message(JSONRPCException, "Transaction has descendants in the wallet", self.nodes[0].bumpfee, parent_id)

        # cannot bump fee with a too-small output
        outputs = {a1:0.00090000}
        rbfid = self.create_fund_sign_send(self.nodes[0], outputs)
        self.nodes[0].bumpfee(rbfid, {"totalFee":10000})
        rbfid = self.create_fund_sign_send(self.nodes[0], outputs)
        assert_raises_message(JSONRPCException, "Change output is too small", self.nodes[0].bumpfee, rbfid, {"totalFee":10001})

        # check that if output is reduced to dust, it will be converted to fee
        # the bumped tx sets fee=9900, but it converts to 10,000
        outputs = {a1:0.00090000}
        rbfid = self.create_fund_sign_send(self.nodes[0], outputs)
        fulltx = self.nodes[0].getrawtransaction(rbfid, 1)
        bumped_tx = self.nodes[0].bumpfee(rbfid, {"totalFee":9900})
        full_bumped_tx = self.nodes[0].getrawtransaction(bumped_tx['txid'], 1)
        assert_equal(bumped_tx['fee'], Decimal('0.00010000'))
        assert_equal(len(fulltx['vout']), 2)
        assert_equal(len(full_bumped_tx['vout']), 1) #change output is eliminated

        # check that bumpfee reacts correctly to the use of settxfee (paytxfee)
        # increase feerate by 2.5x, test that fee increased at least 2x
        self.nodes[0].settxfee(Decimal('0.00001000'))
        rbfid = self.create_fund_sign_send(self.nodes[0], outputs)
        rbftx = self.nodes[0].gettransaction(rbfid)
        self.nodes[0].settxfee(Decimal('0.00002500'))
        bumped_tx = self.nodes[0].bumpfee(rbfid)
        assert(bumped_tx['fee'] > 2*abs(rbftx['fee']))
        self.nodes[0].settxfee(Decimal('0.00000000')) # unset paytxfee

        # check that re-bumping the original tx fails, but bumping the bumper succeeds
        outputs = {a1:0.00090000}
        rbfid = self.create_fund_sign_send(self.nodes[0], outputs, Decimal('0.00001000'))
        sync_mempools(self.nodes)
        bumped = self.nodes[0].bumpfee(rbfid, {"totalFee":1000})
        sync_mempools(self.nodes)
        assert_raises_message(JSONRPCException, "already bumped", self.nodes[0].bumpfee, rbfid, {"totalFee":2000})
        self.nodes[0].bumpfee(bumped['txid'], {"totalFee":2000})

        # check that unconfirmed outputs from bumped transactions are not spendable
        node = self.nodes[0]
        a0 = node.getnewaddress()
        rbfid = self.create_fund_sign_send(node, {a0: 0.00090000})
        rbftx = node.gettransaction(rbfid)['hex']
        assert rbfid in node.getrawmempool()
        bumpid = node.bumpfee(rbfid)['txid']
        assert bumpid in node.getrawmempool()
        assert rbfid not in node.getrawmempool()
        assert_equal([t for t in node.listunspent(0) if t['txid'] == bumpid], [])

        # submit a block with the rbf transaction to clear the bump transaction out
        # of the mempool, then invalidate the block so the rbf transaction will be
        # put back in the mempool. this makes it possible to check whether the rbf
        # transaction outputs are spendable before the rbf tx is confirmed.
        block = submit_block_with_tx(node, rbftx)
        node.invalidateblock(block.hash)
        assert bumpid not in node.getrawmempool()
        assert rbfid in node.getrawmempool()

        # check that outputs from the rbf tx are not spendable until confirmed
        assert_equal([t for t in node.listunspent(0) if t['txid'] == rbfid], [])

        # check that the main output from the rbf tx is spendable after confirmed
        self.nodes[0].generate(1)
        assert_equal(
            sum(1 for t in node.listunspent(0)
                if t['txid'] == rbfid and t['address'] == a0 and t['spendable']), 1)

        print("Success")

def submit_block_with_tx(node, tx):
    ctx = CTransaction()
    ctx.deserialize(io.BytesIO(hex_str_to_bytes(tx)))

    tip = node.getbestblockhash()
    height = node.getblockcount() + 1
    block_time = node.getblockheader(tip)["mediantime"] + 1
    block = blocktools.create_block(int(tip, 16), blocktools.create_coinbase(height), block_time)
    block.vtx.append(ctx)
    block.rehash()
    block.hashMerkleRoot = block.calc_merkle_root()
    block.solve()
    error = node.submitblock(bytes_to_hex_str(block.serialize(True)))
    if error is not None:
        raise Exception(error)
    return block

if __name__ == '__main__':
    BumpFeeTest ().main ()
