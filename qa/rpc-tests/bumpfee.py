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

# Sequence number that is BIP 125 opt-in and BIP 68-compliant
BIP125_SEQUENCE_NUMBER = 0xfffffffd


class BumpFeeTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 2
        self.setup_clean_chain = True

    def setup_network(self, split=False):
        self.nodes = []
        self.nodes.append(start_node(0, self.options.tmpdir, ["-debug", "-walletrbf"]))
        self.nodes.append(start_node(1, self.options.tmpdir, ["-debug"]))
        connect_nodes_bi(self.nodes, 0, 1)
        self.is_network_split = False
        self.sync_all()

    def run_test(self):
        rbf_node, peer_node = self.nodes
        rbf_node_address = rbf_node.getnewaddress()

        # fund rbf node with 10 coins of 0.001 btc (100,000 satoshis)
        print("Mining blocks...")
        self.nodes[1].generate(110)
        self.sync_all()
        for i in range(10):
            self.nodes[1].sendtoaddress(rbf_node_address, 0.001)
        self.sync_all()
        self.nodes[1].generate(1)
        self.sync_all()
        assert_equal(self.nodes[0].getbalance(), Decimal("0.01"))

        print("Running tests")
        dest_address = peer_node.getnewaddress()
        test_simple_bumpfee_succeeds(rbf_node, peer_node, dest_address)
        test_nonrbf_bumpfee_fails(peer_node, dest_address)
        test_notmine_bumpfee_fails(rbf_node, peer_node, dest_address)
        test_bumpfee_with_descendant_fails(rbf_node, rbf_node_address, dest_address)
        test_small_output_fails(rbf_node, dest_address)
        test_dust_to_fee(rbf_node, dest_address)
        test_settxfee(rbf_node, dest_address)
        test_rebumping(rbf_node, dest_address)
        test_unconfirmed_not_spendable(rbf_node, rbf_node_address)
        print("Success")


def test_simple_bumpfee_succeeds(rbf_node, peer_node, dest_address):
    rbfid = create_fund_sign_send(rbf_node, {dest_address: 0.00090000})
    rbftx = rbf_node.gettransaction(rbfid)
    sync_mempools((rbf_node, peer_node))
    assert rbfid in rbf_node.getrawmempool() and rbfid in peer_node.getrawmempool()
    bumped_tx = rbf_node.bumpfee(rbfid)
    assert bumped_tx["fee"] - abs(rbftx["fee"]) > 0
    # check that bumped_tx propogates, original tx was evicted and has a wallet conflict
    sync_mempools((rbf_node, peer_node))
    assert bumped_tx["txid"] in rbf_node.getrawmempool()
    assert bumped_tx["txid"] in peer_node.getrawmempool()
    assert rbfid not in rbf_node.getrawmempool()
    assert rbfid not in peer_node.getrawmempool()
    oldwtx = rbf_node.gettransaction(rbfid)
    assert len(oldwtx["walletconflicts"]) > 0
    # check wallet transaction replaces and replaced_by values
    bumpedwtx = rbf_node.gettransaction(bumped_tx["txid"])
    assert_equal(oldwtx["replaced_by_txid"], bumped_tx["txid"])
    assert_equal(bumpedwtx["replaces_txid"], rbfid)


def test_nonrbf_bumpfee_fails(peer_node, dest_address):
    # cannot replace a non RBF transaction (from node which did not enable RBF)
    not_rbfid = create_fund_sign_send(peer_node, {dest_address: 0.00090000})
    assert_raises_message(JSONRPCException, "not BIP 125 replaceable", peer_node.bumpfee, not_rbfid)


def test_notmine_bumpfee_fails(rbf_node, peer_node, dest_address):
    # cannot bump fee unless the tx has only inputs that we own.
    # here, the rbftx has a peer_node coin and then adds a rbf_node input
    # Note that this test depends upon the RPC code checking input ownership prior to change outputs
    # (since it can't use fundrawtransaction, it lacks a proper change output)
    utxos = [node.listunspent()[-1] for node in (rbf_node, peer_node)]
    inputs = [{
        "txid": utxo["txid"],
        "vout": utxo["vout"],
        "address": utxo["address"],
        "sequence": BIP125_SEQUENCE_NUMBER
    } for utxo in utxos]
    output_val = sum(utxo["amount"] for utxo in utxos) - Decimal("0.001")
    rawtx = rbf_node.createrawtransaction(inputs, {dest_address: output_val})
    signedtx = rbf_node.signrawtransaction(rawtx)
    signedtx = peer_node.signrawtransaction(signedtx["hex"])
    rbfid = rbf_node.sendrawtransaction(signedtx["hex"])
    assert_raises_message(JSONRPCException, "Transaction contains inputs that don't belong to this wallet",
                          rbf_node.bumpfee, rbfid)


def test_bumpfee_with_descendant_fails(rbf_node, rbf_node_address, dest_address):
    # cannot bump fee if the transaction has a descendant
    # parent is send-to-self, so we don't have to check which output is change when creating the child tx
    parent_id = create_fund_sign_send(rbf_node, {rbf_node_address: 0.00050000})
    tx = rbf_node.createrawtransaction([{"txid": parent_id, "vout": 0}], {dest_address: 0.00020000})
    tx = rbf_node.signrawtransaction(tx)
    txid = rbf_node.sendrawtransaction(tx["hex"])
    assert_raises_message(JSONRPCException, "Transaction has descendants in the wallet", rbf_node.bumpfee, parent_id)


def test_small_output_fails(rbf_node, dest_address):
    # cannot bump fee with a too-small output
    rbfid = create_fund_sign_send(rbf_node, {dest_address: 0.00090000})
    rbf_node.bumpfee(rbfid, {"totalFee": 10000})
    rbfid = create_fund_sign_send(rbf_node, {dest_address: 0.00090000})
    assert_raises_message(JSONRPCException, "Change output is too small", rbf_node.bumpfee, rbfid, {"totalFee": 10001})


def test_dust_to_fee(rbf_node, dest_address):
    # check that if output is reduced to dust, it will be converted to fee
    # the bumped tx sets fee=9900, but it converts to 10,000
    rbfid = create_fund_sign_send(rbf_node, {dest_address: 0.00090000})
    fulltx = rbf_node.getrawtransaction(rbfid, 1)
    bumped_tx = rbf_node.bumpfee(rbfid, {"totalFee": 9900})
    full_bumped_tx = rbf_node.getrawtransaction(bumped_tx["txid"], 1)
    assert_equal(bumped_tx["fee"], Decimal("0.00010000"))
    assert_equal(len(fulltx["vout"]), 2)
    assert_equal(len(full_bumped_tx["vout"]), 1)  #change output is eliminated


def test_settxfee(rbf_node, dest_address):
    # check that bumpfee reacts correctly to the use of settxfee (paytxfee)
    # increase feerate by 2.5x, test that fee increased at least 2x
    rbf_node.settxfee(Decimal("0.00001000"))
    rbfid = create_fund_sign_send(rbf_node, {dest_address: 0.00090000})
    rbftx = rbf_node.gettransaction(rbfid)
    rbf_node.settxfee(Decimal("0.00002500"))
    bumped_tx = rbf_node.bumpfee(rbfid)
    assert bumped_tx["fee"] > 2 * abs(rbftx["fee"])
    rbf_node.settxfee(Decimal("0.00000000"))  # unset paytxfee


def test_rebumping(rbf_node, dest_address):
    # check that re-bumping the original tx fails, but bumping the bumper succeeds
    rbfid = create_fund_sign_send(rbf_node, {dest_address: 0.00090000}, Decimal("0.00001000"))
    bumped = rbf_node.bumpfee(rbfid, {"totalFee": 1000})
    assert_raises_message(JSONRPCException, "already bumped", rbf_node.bumpfee, rbfid, {"totalFee": 2000})
    rbf_node.bumpfee(bumped["txid"], {"totalFee": 2000})


def test_unconfirmed_not_spendable(rbf_node, rbf_node_address):
    # check that unconfirmed outputs from bumped transactions are not spendable
    rbfid = create_fund_sign_send(rbf_node, {rbf_node_address: 0.00090000})
    rbftx = rbf_node.gettransaction(rbfid)["hex"]
    assert rbfid in rbf_node.getrawmempool()
    bumpid = rbf_node.bumpfee(rbfid)["txid"]
    assert bumpid in rbf_node.getrawmempool()
    assert rbfid not in rbf_node.getrawmempool()

    # check that outputs from the bump transaction are not spendable
    # due to the replaces_txid check in CWallet::AvailableCoins
    assert_equal([t for t in rbf_node.listunspent(0) if t["txid"] == bumpid], [])

    # submit a block with the rbf tx to clear the bump transaction out
    # of the mempool, then invalidate the block so the rbf transaction will be
    # put back in the mempool. this makes it possible to check whether the rbf
    # transaction outputs are spendable before the rbf tx is confirmed.
    block = submit_block_with_tx(rbf_node, rbftx)
    rbf_node.invalidateblock(block.hash)
    assert bumpid not in rbf_node.getrawmempool()
    assert rbfid in rbf_node.getrawmempool()

    # check that outputs from the rbf tx are not spendable before the
    # transaction is confirmed, due to the replaced_by_txid check in
    # CWallet::AvailableCoins
    assert_equal([t for t in rbf_node.listunspent(0) if t["txid"] == rbfid], [])

    # check that the main output from the rbf tx is spendable after confirmed
    rbf_node.generate(1)
    assert_equal(
        sum(1 for t in rbf_node.listunspent(0)
            if t["txid"] == rbfid and t["address"] == rbf_node_address and t["spendable"]), 1)


def create_fund_sign_send(node, outputs, feerate=0):
    if feerate != 0:
        node.settxfee(feerate)
    rawtx = node.createrawtransaction([], outputs)
    fundtx = node.fundrawtransaction(rawtx)
    signedtx = node.signrawtransaction(fundtx["hex"])
    txid = node.sendrawtransaction(signedtx["hex"])
    return txid


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


if __name__ == "__main__":
    BumpFeeTest().main()
