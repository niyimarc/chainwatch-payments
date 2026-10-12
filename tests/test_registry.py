"""Registry routing, multiple backends, custom backends."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

import chainwatch_payments as cw
from chainwatch_payments import (
    BackendAlreadyRegistered,
    BackendNotFound,
    Capabilities,
    PaymentRegistry,
    State,
    Transfer,
    UnsupportedCapability,
)
from chainwatch_payments.backends import BitcoinBackend, ERC20Backend, MockBackend, TronTRC20Backend

from .conftest import ETH_XPUB, TRX_XPUB, ZPUB_84


def test_routes_to_the_right_backend():
    reg = PaymentRegistry()
    btc = MockBackend("BTC", "testnet", decimals=8)
    usdt = MockBackend("USDT", "tron", decimals=6)
    reg.register("BTC", "testnet", btc)
    reg.register("USDT", "tron", usdt)

    a_btc = reg.derive_address("BTC", "testnet", 0)
    a_usdt = reg.derive_address("USDT", "tron", 0)
    assert a_btc == btc.derive_address(0) and a_usdt == usdt.derive_address(0)
    assert a_btc != a_usdt

    reg.confirm("USDT", "tron", a_usdt, "1")
    assert (btc.reads, usdt.reads) == (0, 1)
    reg.confirm("BTC", "testnet", a_btc, "1")
    assert (btc.reads, usdt.reads) == (1, 1)


def test_keys_are_case_insensitive():
    reg = PaymentRegistry()
    b = MockBackend()
    reg.register("usdt", "TRON", b)
    assert reg.get("USDT", "tron") is b
    assert ("Usdt", "Tron") in reg
    assert reg.enabled() == [("USDT", "tron")]


def test_unknown_backend_lists_enabled():
    reg = PaymentRegistry()
    reg.register("BTC", "mainnet", MockBackend())
    with pytest.raises(BackendNotFound, match="BTC/mainnet"):
        reg.derive_address("ETH", "mainnet", 0)


def test_duplicate_registration_and_replace():
    reg = PaymentRegistry()
    a, b = MockBackend(seed="a"), MockBackend(seed="b")
    reg.register("BTC", "mainnet", a)
    with pytest.raises(BackendAlreadyRegistered):
        reg.register("BTC", "mainnet", b)
    reg.register("BTC", "mainnet", b, replace=True)
    assert reg.get("BTC", "mainnet") is b
    reg.unregister("BTC", "mainnet")
    assert reg.enabled() == []


def test_rejects_non_backend():
    with pytest.raises(TypeError):
        PaymentRegistry().register("BTC", "mainnet", object())  # type: ignore[arg-type]


def test_negative_index_rejected():
    reg = PaymentRegistry()
    reg.register("BTC", "mainnet", MockBackend())
    with pytest.raises(ValueError):
        reg.derive_address("BTC", "mainnet", -1)


def test_hd_unsupported_backend():
    reg = PaymentRegistry()
    reg.register("X", "n", MockBackend(hd=False, single_address="fixed"))
    with pytest.raises(UnsupportedCapability):
        reg.derive_address("X", "n", 0)
    assert reg.receive_address("X", "n") == "fixed"
    with pytest.raises(UnsupportedCapability):
        reg.address_with_memo("X", "n", "ref")


def test_custom_backend_without_subclassing():
    """A host can plug in its own chain with a plain class (duck typing)."""

    class DogeBackend:
        def __init__(self):
            self.ledger: dict[str, list[Transfer]] = {}

        def capabilities(self):
            return Capabilities(hd_derivation=True, memo=False, decimals=8, default_confirmations=6)

        def derive_address(self, index):
            return f"D{index:033d}"

        def get_incoming(self, address, not_before: datetime | None = None):
            return self.ledger.get(address, [])

    doge = DogeBackend()
    reg = PaymentRegistry()
    reg.register("DOGE", "mainnet", doge)
    addr = reg.derive_address("DOGE", "mainnet", 7)
    doge.ledger[addr] = [Transfer("t1", 42_00000000, 6)]
    s = reg.confirm("DOGE", "mainnet", addr, Decimal("42"))
    assert s.state is State.CONFIRMED and s.required_confirmations == 6


def test_multiple_real_backends_enabled_at_once(http):
    """BTC, USDT-TRON, USDT-ERC20 and USDC-ERC20 side by side, each independent."""
    reg = PaymentRegistry()
    reg.register("BTC", "mainnet", BitcoinBackend(ZPUB_84, http=http))
    reg.register("USDT", "tron", TronTRC20Backend(TRX_XPUB, http=http))
    reg.register("USDT", "ethereum", ERC20Backend(ETH_XPUB, asset="USDT", http=http))
    reg.register("USDC", "ethereum", ERC20Backend(ETH_XPUB, asset="USDC", http=http))

    assert reg.derive_address("BTC", "mainnet", 0).startswith("bc1q")
    assert reg.derive_address("USDT", "tron", 0).startswith("T")
    assert reg.derive_address("USDT", "ethereum", 0) == reg.derive_address("USDC", "ethereum", 0)
    caps = {k: reg.capabilities(*k) for k in reg.enabled()}
    assert caps[("BTC", "mainnet")].decimals == 8
    assert caps[("USDT", "tron")].default_confirmations == 19
    assert caps[("USDC", "ethereum")].decimals == 6
    assert reg.get("USDT", "ethereum").contract != reg.get("USDC", "ethereum").contract


def test_concurrent_invoices_on_different_chains():
    reg = PaymentRegistry()
    btc = MockBackend("BTC", "testnet", decimals=8, default_confirmations=2)
    usdt = MockBackend("USDT", "tron", decimals=6, default_confirmations=19)
    reg.register("BTC", "testnet", btc)
    reg.register("USDT", "tron", usdt)
    inv_btc = reg.derive_address("BTC", "testnet", 0)
    inv_usdt = reg.derive_address("USDT", "tron", 0)
    btc.credit(inv_btc, "0.001", mined=True)
    usdt.credit(inv_usdt, "25", mined=True)
    btc.mine(1)
    usdt.mine(5)
    assert reg.confirm("BTC", "testnet", inv_btc, "0.001").state is State.CONFIRMED
    assert reg.confirm("USDT", "tron", inv_usdt, "25").state is State.SEEN   # 6/19
    usdt.mine(13)
    assert reg.confirm("USDT", "tron", inv_usdt, "25").state is State.CONFIRMED


def test_module_level_facade(monkeypatch):
    fresh = PaymentRegistry()
    monkeypatch.setattr(cw, "default_registry", fresh)
    b = MockBackend("BTC", "regtest", default_confirmations=1)
    cw.register("BTC", "regtest", b)
    addr = cw.derive_address("BTC", "regtest", 3)
    b.credit(addr, "1", mined=True)
    assert cw.confirm("BTC", "regtest", addr, "1").state is State.CONFIRMED
