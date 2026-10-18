"""Forwarder: key derivation vectors and forwarding logic (fake network)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from chainwatch_payments.forwarder.keys import account_node, mnemonic_to_seed, same_account
from chainwatch_payments.forwarder.tron import ForwardError, TronTokenForwarder

from .conftest import ETH_XPUB, TRX_XPUB

M = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TREASURY = "TYJPRrdB5APNeRs4R7fYZSwW3TcrTKw2gx"     # some valid address (index 2 of the test seed)
OTHER = "TSeJkUh4Qv67VNFwY8LaAxERygNdy6NQZK"


def test_private_derivation_matches_watch_only_side():
    acct = account_node(mnemonic_to_seed(M))
    assert acct.xpub() == TRX_XPUB and same_account(acct, TRX_XPUB)
    assert not same_account(acct, ETH_XPUB)
    assert [acct.derive(f"0/{i}").tron_address() for i in range(2)] == [
        "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH", "TSeJkUh4Qv67VNFwY8LaAxERygNdy6NQZK"]
    assert "key" not in repr(acct.derive("0/1"))


class FakeTron:
    def __init__(self):
        self.trx: dict[str, Decimal] = {}
        self.tokens: dict[str, int] = {}
        self.sent: list[tuple] = []

    def trx_balance(self, address):
        return self.trx.get(address, Decimal(0))

    def token_balance(self, contract, address):
        return self.tokens.get(address, 0)

    def send_trx(self, key, from_address, to, amount_sun):
        self.sent.append(("trx", from_address, to, amount_sun))
        self.trx[to] = self.trx.get(to, Decimal(0)) + Decimal(amount_sun) / 10**6
        return f"gas{len(self.sent)}"

    def send_token(self, key, contract, from_address, to, amount, fee_limit_sun):
        self.sent.append(("token", from_address, to, amount, fee_limit_sun))
        self.tokens[from_address] -= amount
        self.tokens[to] = self.tokens.get(to, 0) + amount
        return f"tx{len(self.sent)}"


def make(client=None, **kw):
    args = dict(mnemonic=M, expected_xpub=TRX_XPUB, contract=USDT, destination=TREASURY,
                allowed_destinations=[TREASURY], client=client or FakeTron())
    args.update(kw)
    return TronTokenForwarder(**args)


def test_forward_funds_gas_then_moves_tokens():
    net = FakeTron()
    f = make(net)
    dep = f.deposit_address(1)
    net.tokens[dep] = 25_000_000
    r = f.forward(1, dep)
    assert r.status == "done" and r.amount == 25_000_000 and r.gas_tx and r.transfer_tx
    assert net.sent[0] == ("trx", f.gas_address, dep, 20_000_000)
    assert net.sent[1] == ("token", dep, TREASURY, 25_000_000, 30_000_000)
    assert net.tokens[TREASURY] == 25_000_000


def test_forward_is_idempotent():
    net = FakeTron()
    f = make(net)
    dep = f.deposit_address(1)
    net.tokens[dep] = 5_000_000
    f.forward(1, dep)
    assert f.forward(1, dep).status == "nothing"          # balance now 0: never sends twice
    assert len(net.sent) == 2


def test_no_gas_sent_when_address_already_has_trx():
    net = FakeTron()
    f = make(net)
    dep = f.deposit_address(3)
    net.tokens[dep] = 1_000_000
    net.trx[dep] = Decimal(20)
    f.forward(3, dep)
    assert [s[0] for s in net.sent] == ["token"]


def test_safety_rails():
    with pytest.raises(ForwardError, match="does not match the xpub"):
        make(expected_xpub=ETH_XPUB)
    with pytest.raises(ForwardError, match="not in the allowed"):
        make(destination=OTHER)
    with pytest.raises(ForwardError, match="invalid destination address"):
        make(allowed_destinations=[TREASURY, "Tnotanaddress"])
    with pytest.raises(ForwardError, match="12-24 words"):
        make(mnemonic="abandon about")
    f = make()
    with pytest.raises(ForwardError, match="does not match invoice address"):
        f.forward(4, OTHER)
    with pytest.raises(ForwardError, match="gas wallet"):
        f.forward(0, f.gas_address)
    with pytest.raises(ForwardError, match="must not be the gas wallet"):
        make(destination="TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
             allowed_destinations=["TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"])
    assert "abandon" not in repr(f)


def test_tronpy_adapter_uses_real_tronpy_keys():
    tronpy = pytest.importorskip("tronpy")
    from tronpy.keys import PrivateKey

    acct = account_node(mnemonic_to_seed(M))
    node = acct.derive("0/1")
    assert PrivateKey(node.private_key_bytes).public_key.to_base58check_address() == node.tron_address()
    assert tronpy


def test_tronpy_adapter_checks_receipts(monkeypatch):
    pytest.importorskip("tronpy")
    from chainwatch_payments.forwarder.tron import TronpyClient

    c = TronpyClient(network="nile")

    class Ret:
        def __init__(self, result):
            self.txid, self.result = "abc", result

        def wait(self, timeout=30):
            return {"receipt": {"result": self.result}} if self.result else {}

    class Chain:
        def __init__(self, result):
            self.result = result

        def __getattr__(self, name):        # with_owner / fee_limit / build / sign
            return lambda *a, **k: self

        def broadcast(self):
            return Ret(self.result)

    class Functions:
        def __init__(self, result):
            self.result = result

        def balanceOf(self, addr):
            return 42

        def transfer(self, to, amount):
            return Chain(self.result)

    class Contract:
        def __init__(self, result):
            self.functions = Functions(result)

    key = account_node(mnemonic_to_seed(M)).derive("0/1").private_key_bytes
    c._contracts[USDT] = Contract("SUCCESS")
    assert c.token_balance(USDT, OTHER) == 42
    assert c.send_token(key, USDT, OTHER, TREASURY, 1, 1) == "abc"
    c._contracts[USDT] = Contract("REVERT")
    with pytest.raises(ForwardError, match="did not succeed"):
        c.send_token(key, USDT, OTHER, TREASURY, 1, 1)

    from tronpy.exceptions import AddressNotFound

    def missing(addr):
        raise AddressNotFound("account not found")

    monkeypatch.setattr(c.client, "get_account_balance", missing)
    assert c.trx_balance(OTHER) == 0
