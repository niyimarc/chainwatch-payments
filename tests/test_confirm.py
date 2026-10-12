"""Confirmation logic: states, tolerance, reorgs, idempotency, discriminators."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chainwatch_payments import (
    Match,
    MatchMode,
    PaymentRegistry,
    State,
    Tolerance,
    Transfer,
    evaluate,
    unique_amount,
)
from chainwatch_payments.backends import MockBackend


@pytest.fixture
def chain():
    return MockBackend("BTC", "regtest", decimals=8, default_confirmations=3)


@pytest.fixture
def reg(chain):
    r = PaymentRegistry()
    r.register("BTC", "regtest", chain)
    return r


def status(reg, addr, amount="0.01", **kw):
    return reg.confirm("BTC", "regtest", addr, Decimal(amount), **kw)


# -- NONE -> SEEN -> CONFIRMED -------------------------------------------------

def test_state_transitions(reg, chain):
    addr = chain.derive_address(0)
    s = status(reg, addr)
    assert (s.state, s.match, s.tx_ids) == (State.NONE, Match.NONE, ())

    tx = chain.credit(addr, "0.01")
    s = status(reg, addr)
    assert s.state is State.SEEN and s.match is Match.EXACT
    assert s.confirmations == 0 and s.tx_ids == (tx,)
    assert s.received == Decimal("0.01") and s.confirmed == 0

    chain.mine(2)
    s = status(reg, addr)
    assert s.state is State.SEEN and s.confirmations == 2

    chain.mine(1)
    s = status(reg, addr)
    assert s.state is State.CONFIRMED and s.is_paid
    assert s.confirmations == 3 and s.confirmed_amount == 1_000_000


def test_required_confirmations_override(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.01", mined=True)
    assert status(reg, addr).state is State.SEEN                       # default 3
    assert status(reg, addr, required_confirmations=1).state is State.CONFIRMED
    chain.credit(chain.derive_address(1), "0.01")
    assert status(reg, chain.derive_address(1), required_confirmations=0).state is State.CONFIRMED


# -- exact / under / over with tolerance ----------------------------------------

def test_underpayment_never_confirms(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.009", mined=True)
    chain.mine(10)
    s = status(reg, addr)
    assert s.state is State.SEEN and s.match is Match.UNDER and not s.is_paid


def test_overpayment_confirms_flagged_over(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.02", mined=True)
    chain.mine(5)
    s = status(reg, addr)
    assert s.state is State.CONFIRMED and s.match is Match.OVER


def test_absolute_tolerance_band(reg, chain):
    tol = Tolerance.absolute("0.0001")
    a1, a2, a3 = (chain.derive_address(i) for i in range(3))
    chain.credit(a1, "0.0099")     # exactly at lower edge
    chain.credit(a2, "0.01009")    # inside upper band
    chain.credit(a3, "0.00989")    # just below band
    chain.mine(3)
    assert status(reg, a1, tolerance=tol).match is Match.EXACT
    assert status(reg, a1, tolerance=tol).state is State.CONFIRMED
    assert status(reg, a2, tolerance=tol).match is Match.EXACT
    s3 = status(reg, a3, tolerance=tol)
    assert (s3.state, s3.match) == (State.SEEN, Match.UNDER)


def test_relative_tolerance_band(reg, chain):
    tol = Tolerance.relative("0.01", over="0")   # 1% under, nothing over
    a1, a2 = chain.derive_address(0), chain.derive_address(1)
    chain.credit(a1, "0.0099")
    chain.credit(a2, "0.01000001")
    chain.mine(3)
    assert status(reg, a1, tolerance=tol).state is State.CONFIRMED
    assert status(reg, a2, tolerance=tol).match is Match.OVER


def test_registry_default_tolerance(chain):
    reg = PaymentRegistry(default_tolerance=Tolerance.absolute("0.001"))
    reg.register("BTC", "regtest", chain)
    addr = chain.derive_address(0)
    chain.credit(addr, "0.0095", mined=True)
    chain.mine(3)
    assert status(reg, addr).state is State.CONFIRMED


def test_partial_payments_sum(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.004", mined=True)
    chain.mine(3)
    assert status(reg, addr).match is Match.UNDER
    chain.credit(addr, "0.006")                       # second part in mempool
    s = status(reg, addr)
    assert s.state is State.SEEN and s.match is Match.EXACT and len(s.tx_ids) == 2
    chain.mine(3)
    s = status(reg, addr)
    assert s.state is State.CONFIRMED and s.match is Match.EXACT


# -- reorgs ------------------------------------------------------------------------

def test_reorg_drop_reverts_confirmed(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.01", mined=True)
    chain.mine(2)
    first = status(reg, addr)
    assert first.state is State.CONFIRMED

    chain.reorg(3)                     # the block holding the tx is orphaned, tx gone
    chain.mine(3)
    after = status(reg, addr, previous=first)
    assert after.state is State.NONE and after.reverted


def test_reorg_back_to_mempool_then_reconfirms(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.01", mined=True)
    chain.mine(2)
    first = status(reg, addr)
    chain.reorg(3, drop=False)
    s = status(reg, addr, previous=first)
    assert s.state is State.SEEN and s.reverted and s.confirmations == 0
    chain.mine(3)
    s2 = status(reg, addr, previous=s)
    assert s2.state is State.CONFIRMED and not s2.reverted


def test_partial_reorg_drops_below_required(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.01", mined=True)
    chain.mine(4)                       # 5 confirmations
    first = status(reg, addr)
    chain.reorg(4, drop=False)          # tx block survives; confs drop to 1
    s = status(reg, addr, previous=first)
    assert s.state is State.SEEN and s.reverted and s.confirmations == 1


# -- idempotency / determinism -------------------------------------------------------

def test_repeated_confirmation_is_idempotent(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.006", mined=True)
    chain.credit(addr, "0.004", mined=True)
    chain.mine(3)
    results = [status(reg, addr) for _ in range(5)]
    assert all(r == results[0] for r in results)
    assert results[0].state is State.CONFIRMED


def test_duplicate_transfers_are_counted_once():
    t = Transfer("aa", 500, 3, index=0)
    dup = Transfer("aa", 500, 5, index=0)
    other_output = Transfer("aa", 500, 5, index=1)
    s = evaluate([t, dup, t], asset="X", network="n", address="a", expected_amount=1000,
                 decimals=0, required_confirmations=3)
    assert s.received_amount == 500 and s.match is Match.UNDER
    s = evaluate([t, dup, other_output], asset="X", network="n", address="a",
                 expected_amount=1000, decimals=0, required_confirmations=3)
    assert s.state is State.CONFIRMED and s.received_amount == 1000 and s.tx_ids == ("aa",)


def test_order_of_transfers_does_not_matter():
    ts = [Transfer("b", 300, 4, block_height=10), Transfer("a", 700, 0)]
    kw = dict(asset="X", network="n", address="a", expected_amount=1000, decimals=0,
              required_confirmations=1)
    assert evaluate(ts, **kw) == evaluate(list(reversed(ts)), **kw)


# -- not_before -----------------------------------------------------------------------

def test_not_before_ignores_older_payments(reg, chain):
    addr = chain.derive_address(0)
    chain.credit(addr, "0.01", mined=True)         # old payment (e.g. previous invoice)
    chain.mine(5)
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(hour=2)
    assert status(reg, addr).state is State.CONFIRMED
    assert status(reg, addr, not_before=cutoff).state is State.NONE
    chain.credit(addr, "0.01")                     # unconfirmed has no timestamp: counted
    assert status(reg, addr, not_before=cutoff).state is State.SEEN


def test_not_before_naive_datetime_treated_as_utc():
    t = Transfer("a", 10, 5, timestamp=datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
    kw = dict(asset="X", network="n", address="a", expected_amount=10, decimals=0, required_confirmations=1)
    assert evaluate([t], not_before=datetime(2026, 5, 1, 11), **kw).state is State.CONFIRMED
    assert evaluate([t], not_before=datetime(2026, 5, 1, 13), **kw).state is State.NONE


# -- single-address mode: amount discriminator and memo -------------------------------

def test_amount_discriminator_mode():
    chain = MockBackend("USDT", "mock", decimals=6, default_confirmations=1, hd=False,
                        single_address="shared")
    reg = PaymentRegistry()
    reg.register("USDT", "mock", chain)
    addr = reg.receive_address("USDT", "mock")
    a_amt = unique_amount("10", 1, "0.000001", 6)    # 10.000001
    b_amt = unique_amount("10", 2, "0.000001", 6)    # 10.000002
    chain.credit(addr, "10.000002", mined=True)      # invoice B pays
    chain.credit(addr, "3")                          # unrelated payment
    a = reg.confirm("USDT", "mock", addr, a_amt, match_mode=MatchMode.SINGLE)
    b = reg.confirm("USDT", "mock", addr, b_amt, match_mode=MatchMode.SINGLE)
    assert a.state is State.NONE                      # others' funds are not attributed
    assert b.state is State.CONFIRMED and b.received == Decimal("10.000002")


def test_memo_mode():
    chain = MockBackend("XRP", "mock", decimals=6, supports_memo=True, single_address="rShared")
    reg = PaymentRegistry()
    reg.register("XRP", "mock", chain)
    addr, memo = reg.address_with_memo("XRP", "mock", "invoice-42")
    assert addr == "rShared"
    assert reg.address_with_memo("XRP", "mock", "invoice-42") == (addr, memo)  # deterministic
    chain.credit(addr, "5", memo="someone-else", mined=True)
    assert reg.confirm("XRP", "mock", addr, "5", memo=memo).state is State.NONE
    chain.credit(addr, "5", memo=memo, mined=True)
    s = reg.confirm("XRP", "mock", addr, "5", memo=memo)
    assert s.state is State.CONFIRMED and s.memo == memo


def test_unique_amount_precision():
    assert unique_amount("10", 37, "0.000001", 6) == Decimal("10.000037")
    with pytest.raises(ValueError):
        unique_amount("10", 1, "0.0000001", 6)


# -- input validation ------------------------------------------------------------------

def test_float_amounts_refused(reg, chain):
    with pytest.raises(TypeError):
        reg.confirm("BTC", "regtest", chain.derive_address(0), 0.01)
    with pytest.raises(ValueError):  # Decimal(float) drags binary noise along: also refused
        reg.confirm("BTC", "regtest", chain.derive_address(0), Decimal(0.01))


def test_too_precise_amount_refused(reg, chain):
    with pytest.raises(ValueError):
        status(reg, chain.derive_address(0), amount="0.000000001")


def test_zero_expected_refused(reg, chain):
    with pytest.raises(ValueError):
        status(reg, chain.derive_address(0), amount="0")
