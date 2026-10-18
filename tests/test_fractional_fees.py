"""Fractional fees through a local Bitcoin Core node on an isolated regtest chain."""

from decimal import Decimal
import math
import os

from fixtures import *
from test_framework.serializations import PSBT
from test_framework.utils import (
    COIN,
    RpcError,
    sign_and_broadcast,
    sign_and_broadcast_psbt,
    wait_for,
)


@pytest.fixture
def bitcoind(directory, request):
    # These policy settings apply only to this test's disposable regtest node.
    node = Bitcoind(bitcoin_dir=os.path.join(directory, "bitcoind"))
    min_relay = Decimal(str(getattr(request, "param", 0.1))) * 1000 / COIN
    node.cmd_line.extend(
        [
            f"-minrelaytxfee={min_relay:.8f}",
            "-blockmintxfee=0.00000100",
            "-incrementalrelayfee=0.00001000",
        ]
    )
    node.startup()
    try:
        node.rpc.createwallet(node.rpc.wallet_name, False, False, "", False, True, True)
        node.generate_block(101)
        yield node
    finally:
        node.cleanup()


def fund_wallet(lianad, bitcoind):
    deposit = bitcoind.rpc.sendtoaddress(lianad.rpc.getnewaddress()["address"], 0.001)
    bitcoind.generate_block(1, wait_for_mempool=deposit)
    wait_for(lambda: len(lianad.rpc.listcoins(["confirmed"])["coins"]) == 1)
    return lianad.rpc.listcoins(["confirmed"])["coins"][0]


def spend_is_confirmed(lianad, outpoint):
    spend_info = lianad.rpc.listcoins([], [outpoint])["coins"][0]["spend_info"]
    return spend_info is not None and spend_info["height"] is not None


def outputs_by_address(bitcoind, psbt):
    decoded = bitcoind.rpc.decoderawtransaction(psbt.tx.serialize().hex())
    return {
        out["scriptPubKey"]["address"]: int(out["value"] * COIN)
        for out in decoded["vout"]
    }


def assert_mempool_fee(bitcoind, txid, input_amount, psbt, feerate, extra_fee=0):
    # Check the actual input/output balance against Core's independently computed
    # fee, then tightly bound the charge using the signed transaction's vsize.
    fee = input_amount - sum(out.nValue for out in psbt.tx.vout)
    entry = bitcoind.rpc.getmempoolentry(txid)
    assert fee == int(entry["fees"]["base"] * COIN)
    rate = Decimal(str(feerate))
    assert math.ceil(rate * entry["vsize"]) <= fee - extra_fee
    # Descriptor satisfaction estimates allow for maximum signature sizes.
    assert fee - extra_fee <= math.ceil(rate * (entry["vsize"] + 2)) + 1
    return fee


@pytest.mark.parametrize("feerate", [0.1, 0.333, 0.999, 1])
@pytest.mark.parametrize("mode", ["send", "automatic", "refresh", "sweep", "recovery"])
def test_fractional_fee_roundtrip(lianad, bitcoind, feerate, mode):
    coin = fund_wallet(lianad, bitcoind)
    recipient = bitcoind.rpc.getnewaddress()
    if mode == "recovery":
        bitcoind.generate_block(9)
        wait_for(
            lambda: lianad.rpc.getinfo()["block_height"] == bitcoind.rpc.getblockcount()
        )
        result = lianad.rpc.createrecovery(recipient, feerate, 10, [coin["outpoint"]])
    else:
        destinations = {recipient: 40_000} if mode in ("send", "automatic") else {}
        outpoints = [] if mode == "automatic" else [coin["outpoint"]]
        result = (
            lianad.rpc.createspend(destinations, outpoints, feerate, recipient)
            if mode == "sweep"
            else lianad.rpc.createspend(destinations, outpoints, feerate)
        )
    if mode == "recovery":
        assert set(result) == {"psbt"}
    else:
        assert result["warnings"] == []
    psbt = PSBT.from_base64(result["psbt"])
    outputs = outputs_by_address(bitcoind, psbt)
    wallet_change = {item["change"] for item in lianad.rpc.listaddresses()["addresses"]}
    if mode in ("send", "automatic"):
        assert outputs[recipient] == 40_000
        assert len(outputs) == 2
        assert set(outputs) - {recipient} <= wallet_change
    elif mode == "refresh":
        assert len(outputs) == 1
        assert set(outputs) <= wallet_change
    else:
        assert set(outputs) == {recipient}
    assert len(psbt.tx.vin) == 1
    assert (
        f"{psbt.tx.vin[0].prevout.hash:064x}:{psbt.tx.vin[0].prevout.n}"
        == coin["outpoint"]
    )
    txid = sign_and_broadcast(lianad, bitcoind, psbt, recovery=mode == "recovery")
    fee = assert_mempool_fee(bitcoind, txid, coin["amount"], psbt, feerate)
    if feerate <= 0.333:
        assert fee < bitcoind.rpc.getmempoolentry(txid)["vsize"]
    if mode in ("sweep", "recovery"):
        assert outputs[recipient] == coin["amount"] - fee
    bitcoind.generate_block(1, wait_for_mempool=txid)
    wait_for(lambda: spend_is_confirmed(lianad, coin["outpoint"]))
    if mode in ("send", "automatic", "refresh"):
        wait_for(lambda: len(lianad.rpc.listcoins(["confirmed"])["coins"]) == 1)
        change = lianad.rpc.listcoins(["confirmed"])["coins"][0]
        assert change["is_change"]
        assert change["amount"] == coin["amount"] - fee - (
            40_000 if mode in ("send", "automatic") else 0
        )


@pytest.mark.parametrize("is_cancel,rate", [(False, 1.5), (True, None)])
def test_fractional_fee_rbf(lianad, bitcoind, rate, is_cancel):
    coin = fund_wallet(lianad, bitcoind)
    recipient = bitcoind.rpc.getnewaddress()
    first = PSBT.from_base64(
        lianad.rpc.createspend({recipient: 40_000}, [coin["outpoint"]], 0.1)["psbt"]
    )
    first_txid = sign_and_broadcast_psbt(lianad, first)
    first_fee = assert_mempool_fee(bitcoind, first_txid, coin["amount"], first, 0.1)
    # A fractional increment must still satisfy the conservative 1 sat/vB RBF
    # relay increment, including on Core releases predating its lower default.
    if not is_cancel:
        with pytest.raises(RpcError, match="too low"):
            lianad.rpc.rbfpsbt(first_txid, False, 0.2)
    result = (
        lianad.rpc.rbfpsbt(first_txid, True)
        if is_cancel
        else lianad.rpc.rbfpsbt(first_txid, False, rate)
    )
    replacement = PSBT.from_base64(result["psbt"])
    first_outputs = outputs_by_address(bitcoind, first)
    new_outputs = outputs_by_address(bitcoind, replacement)
    change_address = (set(first_outputs) - {recipient}).pop()
    if is_cancel:
        assert set(new_outputs) == {change_address}
    else:
        assert new_outputs[recipient] == first_outputs[recipient] == 40_000
        assert set(new_outputs) == set(first_outputs)
    assert [i.prevout.serialize() for i in replacement.tx.vin] == [
        i.prevout.serialize() for i in first.tx.vin
    ]
    replacement_txid = sign_and_broadcast_psbt(lianad, replacement)
    entry = bitcoind.rpc.getmempoolentry(replacement_txid)
    fee = coin["amount"] - sum(out.nValue for out in replacement.tx.vout)
    assert fee == int(entry["fees"]["base"] * COIN)
    assert fee - first_fee >= entry["vsize"]
    if rate is None:
        # The automatic minimum may overestimate signed sizes by a few bytes.
        assert fee <= first_fee + entry["vsize"] + 10
    else:
        assert_mempool_fee(
            bitcoind, replacement_txid, coin["amount"], replacement, rate
        )
    assert first_txid not in bitcoind.rpc.getrawmempool()
    bitcoind.generate_block(1, wait_for_mempool=replacement_txid)
    wait_for(lambda: spend_is_confirmed(lianad, coin["outpoint"]))
    wait_for(lambda: len(lianad.rpc.listcoins(["confirmed"])["coins"]) == 1)
    assert (
        lianad.rpc.listcoins(["confirmed"])["coins"][0]["amount"]
        == new_outputs[change_address]
    )


def test_fractional_fee_unconfirmed_ancestor(lianad, bitcoind):
    coin = fund_wallet(lianad, bitcoind)
    parent = PSBT.from_base64(
        lianad.rpc.createspend(
            {bitcoind.rpc.getnewaddress(): 40_000}, [coin["outpoint"]], 0.1
        )["psbt"]
    )
    parent_txid = sign_and_broadcast_psbt(lianad, parent)
    ancestor = bitcoind.rpc.getmempoolentry(parent_txid)
    wait_for(lambda: len(lianad.rpc.listcoins(["unconfirmed"])["coins"]) == 1)
    change = lianad.rpc.listcoins(["unconfirmed"])["coins"][0]
    recipient = bitcoind.rpc.getnewaddress()
    rate = Decimal("0.333")
    extra_fee = max(
        0,
        math.ceil(rate * ancestor["ancestorsize"])
        - int(ancestor["fees"]["ancestor"] * COIN),
    )
    result = lianad.rpc.createspend({recipient: 10_000}, [], float(rate))
    assert len(result["warnings"]) == 1
    assert f"increased by {extra_fee} sats" in result["warnings"][0]
    child = PSBT.from_base64(result["psbt"])
    assert outputs_by_address(bitcoind, child)[recipient] == 10_000
    assert len(child.tx.vin) == 1
    child_txid = sign_and_broadcast_psbt(lianad, child)
    assert_mempool_fee(bitcoind, child_txid, change["amount"], child, rate, extra_fee)
    package = bitcoind.rpc.getmempoolentry(child_txid)
    assert int(package["fees"]["ancestor"] * COIN) >= math.ceil(
        rate * package["ancestorsize"]
    )
    bitcoind.generate_block(1, wait_for_mempool=[parent_txid, child_txid])
    wait_for(lambda: len(lianad.rpc.listcoins(["confirmed"])["coins"]) == 1)


def test_fractional_fee_invalid_rpc(lianad, bitcoind):
    coin = fund_wallet(lianad, bitcoind)
    recipient = bitcoind.rpc.getnewaddress()
    first = lianad.rpc.createspend({recipient: 40_000}, [coin["outpoint"]], 0.1)
    lianad.rpc.updatespend(first["psbt"])
    txid = PSBT.from_base64(first["psbt"]).tx.txid().hex()
    bitcoind.generate_block(9)
    wait_for(
        lambda: lianad.rpc.getinfo()["block_height"] == bitcoind.rpc.getblockcount()
    )
    for invalid in (0, -1, 0.099, 0.1001, 1.0001, 1000.001, 1001, 2**64, "0.1", True):
        with pytest.raises(RpcError, match="feerate"):
            lianad.rpc.createspend({recipient: 40_000}, [coin["outpoint"]], invalid)
        with pytest.raises(RpcError, match="feerate"):
            lianad.rpc.createrecovery(recipient, invalid, 10, [coin["outpoint"]])
        with pytest.raises(RpcError, match="feerate"):
            lianad.rpc.rbfpsbt(txid, False, invalid)


@pytest.mark.parametrize("bitcoind", [1], indirect=True)
def test_fractional_fee_node_policy_rejection(lianad, bitcoind):
    """A node's higher relay floor rejects a spend without consuming its inputs."""
    coin = fund_wallet(lianad, bitcoind)
    recipient = bitcoind.rpc.getnewaddress()
    rejected = PSBT.from_base64(
        lianad.rpc.createspend({recipient: 40_000}, [coin["outpoint"]], 0.1)["psbt"]
    )
    rejected_txid = rejected.tx.txid().hex()
    with pytest.raises(RpcError, match="min relay fee"):
        sign_and_broadcast_psbt(lianad, rejected)
    assert rejected_txid not in bitcoind.rpc.getrawmempool()
    assert bitcoind.rpc.getreceivedbyaddress(recipient, 0) == 0
    remaining = lianad.rpc.listcoins(["confirmed"])["coins"]
    assert len(remaining) == 1
    assert remaining[0]["outpoint"] == coin["outpoint"]
    assert remaining[0]["amount"] == coin["amount"]
    assert remaining[0]["spend_info"] is None

    accepted = PSBT.from_base64(
        lianad.rpc.createspend({recipient: 40_000}, [coin["outpoint"]], 1)["psbt"]
    )
    outputs = outputs_by_address(bitcoind, accepted)
    assert outputs[recipient] == 40_000
    assert len(outputs) == 2
    assert set(outputs) - {recipient} <= {
        item["change"] for item in lianad.rpc.listaddresses()["addresses"]
    }
    accepted_txid = sign_and_broadcast_psbt(lianad, accepted)
    fee = assert_mempool_fee(bitcoind, accepted_txid, coin["amount"], accepted, 1)
    bitcoind.generate_block(1, wait_for_mempool=accepted_txid)
    wait_for(lambda: spend_is_confirmed(lianad, coin["outpoint"]))
    assert int(bitcoind.rpc.getreceivedbyaddress(recipient, 1) * COIN) == 40_000
    wait_for(lambda: len(lianad.rpc.listcoins(["confirmed"])["coins"]) == 1)
    change = lianad.rpc.listcoins(["confirmed"])["coins"][0]
    assert change["is_change"]
    assert change["amount"] == coin["amount"] - 40_000 - fee


@pytest.mark.parametrize("bitcoind", [1], indirect=True)
def test_fractional_fee_unbroadcast_cancel_fallback(lianad, bitcoind):
    """An absent original keeps the conservative 1 sat/vB cancellation floor."""
    coin = fund_wallet(lianad, bitcoind)
    recipient = bitcoind.rpc.getnewaddress()
    first = PSBT.from_base64(
        lianad.rpc.createspend({recipient: 40_000}, [coin["outpoint"]], 0.1)["psbt"]
    )
    first = lianad.signer.sign_psbt(first)
    lianad.rpc.updatespend(first.to_base64())
    first_txid = first.tx.txid().hex()
    assert first_txid not in bitcoind.rpc.getrawmempool()
    with pytest.raises(RpcError, match="too low"):
        lianad.rpc.rbfpsbt(first_txid, False, 0.5)
    cancel = PSBT.from_base64(lianad.rpc.rbfpsbt(first_txid, True)["psbt"])
    outputs = outputs_by_address(bitcoind, cancel)
    assert len(outputs) == 1
    assert recipient not in outputs
    assert set(outputs) <= {
        item["change"] for item in lianad.rpc.listaddresses()["addresses"]
    }
    cancel_txid = sign_and_broadcast_psbt(lianad, cancel)
    fee = assert_mempool_fee(bitcoind, cancel_txid, coin["amount"], cancel, 1)
    bitcoind.generate_block(1, wait_for_mempool=cancel_txid)
    wait_for(lambda: spend_is_confirmed(lianad, coin["outpoint"]))
    assert bitcoind.rpc.getreceivedbyaddress(recipient, 0) == 0
    wait_for(lambda: len(lianad.rpc.listcoins(["confirmed"])["coins"]) == 1)
    assert (
        lianad.rpc.listcoins(["confirmed"])["coins"][0]["amount"]
        == coin["amount"] - fee
    )
