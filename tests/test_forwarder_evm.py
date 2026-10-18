"""ERC-20 forwarder: decoded and signer-recovered transactions (fake node)."""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402

from chainwatch_payments.forwarder.base import ForwardDeferred, ForwardError  # noqa: E402
from chainwatch_payments.forwarder.evm import ERC20Forwarder, transfer_calldata  # noqa: E402

from .conftest import ETH_XPUB, TRX_XPUB  # noqa: E402

M = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
TREASURY = "0xb6716976A3ebe8D39aCEB04372f22Ff8e6802D7A"   # index 2 of the test seed (any valid address)
GWEI = 10**9


def decode(raw_hex):
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes

    tx = TypedTransaction.from_bytes(HexBytes(raw_hex)).as_dict()
    return tx, Account.recover_transaction(raw_hex)


class FakeNode:
    def __init__(self, base_gwei=10, tip_gwei=1):
        self.base, self.tip = base_gwei * GWEI, tip_gwei * GWEI
        self.eth: dict[str, int] = {}
        self.tokens: dict[str, int] = {}
        self.nonces: dict[str, int] = {}
        self.sent: list[tuple] = []
        self.receipts: dict[str, dict] = {}
        self.mine = True

    def chain_id(self):
        return 1

    def eth_balance(self, a):
        return self.eth.get(a.lower(), 0)

    def token_balance(self, c, a):
        return self.tokens.get(a.lower(), 0)

    def nonce(self, a):
        return self.nonces.get(a.lower(), 0)

    def fees(self):
        return self.base, self.tip

    def estimate_gas(self, tx):
        return 60_000

    def send_raw(self, raw):
        tx, sender = decode(raw)
        txid = f"0x{len(self.sent):064x}"
        self.sent.append((tx, sender))
        if self.mine:
            self.receipts[txid] = {"status": "0x1"}
            s = sender.lower()
            self.nonces[s] = self.nonces.get(s, 0) + 1
            if tx["data"] in (b"", "0x"):
                to = tx["to"] if isinstance(tx["to"], str) else "0x" + tx["to"].hex()
                self.eth[to.lower()] = self.eth.get(to.lower(), 0) + tx["value"]
            else:
                self.tokens[s] = 0
        return txid

    def receipt(self, txid):
        return self.receipts.get(txid)


def make(node, **kw):
    args = dict(mnemonic=M, expected_xpub=ETH_XPUB, contract=USDC, destination=TREASURY,
                allowed_destinations=[TREASURY], client=node, sleep=lambda s: None, poll_seconds=1, wait_seconds=3)
    args.update(kw)
    return ERC20Forwarder(**args)


def _hex(v):
    return v if isinstance(v, str) else "0x" + bytes(v).hex()


def test_tops_up_gas_then_moves_tokens():
    node = FakeNode()
    f = make(node)
    dep = f.deposit_address(1)
    assert dep == "0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0"      # matches the watch-only side
    node.tokens[dep.lower()] = 25_000_000
    r = f.forward(1, dep.lower())                                      # case-insensitive match
    assert r.status == "done" and r.amount == 25_000_000 and r.gas_tx and r.transfer_tx

    (gas_tx, gas_from), (tok_tx, tok_from) = node.sent
    assert gas_from == f.gas_address and _hex(gas_tx["to"]).lower() == dep.lower()
    gas = int(60_000 * 1.3)
    max_fee = 2 * 10 * GWEI + 1 * GWEI
    assert gas_tx["value"] == int(Decimal(gas * max_fee) * Decimal("1.2"))
    assert tok_from == dep and _hex(tok_tx["to"]).lower() == USDC.lower()
    assert _hex(tok_tx["data"]) == transfer_calldata(TREASURY, 25_000_000)
    assert tok_tx["chainId"] == 1 and tok_tx["maxFeePerGas"] == max_fee and tok_tx["gas"] == gas
    assert tok_tx["value"] == 0


def test_no_top_up_when_deposit_has_eth_and_idempotent():
    node = FakeNode()
    f = make(node)
    dep = f.deposit_address(3)
    node.tokens[dep.lower()] = 5
    node.eth[dep.lower()] = 10**18
    assert f.forward(3, dep).status == "done"
    assert len(node.sent) == 1
    assert f.forward(3, dep).status == "nothing"                       # balance now 0


def test_gas_price_cap_defers():
    node = FakeNode(base_gwei=200)
    f = make(node, max_fee_gwei="50")
    dep = f.deposit_address(4)
    node.tokens[dep.lower()] = 5
    with pytest.raises(ForwardDeferred, match="above max_fee_gwei"):
        f.forward(4, dep)
    assert node.sent == []


def test_unmined_transfer_reports_sent_then_state():
    node = FakeNode()
    f = make(node)
    dep = f.deposit_address(5)
    node.tokens[dep.lower()] = 5
    node.eth[dep.lower()] = 10**18
    node.mine = False
    r = f.forward(5, dep)
    assert r.status == "sent"
    assert f.tx_state(r.transfer_tx) == "pending"
    node.receipts[r.transfer_tx] = {"status": "0x1"}
    assert f.tx_state(r.transfer_tx) == "confirmed"
    node.receipts[r.transfer_tx] = {"status": "0x0"}
    assert f.tx_state(r.transfer_tx) == "dropped"


def test_unmined_gas_top_up_defers():
    node = FakeNode()
    node.mine = False
    f = make(node)
    dep = f.deposit_address(6)
    node.tokens[dep.lower()] = 5
    with pytest.raises(ForwardDeferred, match="not mined yet"):
        f.forward(6, dep)


def test_evm_safety_rails():
    with pytest.raises(ForwardError, match="does not match the xpub"):
        make(FakeNode(), expected_xpub=TRX_XPUB)
    with pytest.raises(ForwardError, match="not in the allowed"):
        make(FakeNode(), destination="0x" + "11" * 20)
    f = make(FakeNode())
    with pytest.raises(ForwardError, match="gas wallet"):
        f.forward(0, f.gas_address)
    with pytest.raises(ForwardError, match="does not match invoice address"):
        f.forward(7, TREASURY)
    with pytest.raises(ForwardError, match="must not be the gas wallet"):
        make(FakeNode(), destination=f.gas_address, allowed_destinations=[f.gas_address])


def test_evm_rpc_client(http):
    from chainwatch_payments.forwarder.evm import EvmRpcClient

    replies = {"eth_chainId": "0xaa36a7", "eth_getBalance": "0x10", "eth_call": "0x" + "0" * 63 + "5",
               "eth_getTransactionCount": "0x3", "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
               "eth_maxPriorityFeePerGas": "0x2", "eth_estimateGas": "0x5208",
               "eth_sendRawTransaction": "0xabc", "eth_getTransactionReceipt": None}
    http.on_post("https://rpc.test", lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": replies[p["method"]]})
    c = EvmRpcClient("https://rpc.test", http=http)
    assert c.chain_id() == 11155111 and c.eth_balance("0x" + "1" * 40) == 16
    assert c.token_balance(USDC, "0x" + "1" * 40) == 5 and c.nonce("0x" + "1" * 40) == 3
    assert c.fees() == (10**9, 2) and c.estimate_gas({}) == 21000
    assert c.send_raw("0x00") == "0xabc" and c.receipt("0xabc") is None
