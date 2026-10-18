"""Django: payment-time pricing, late rules, backoff, audit log, gap warning,
single-address mode, dynamic fees, cross-checked prices, scan start height."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from chainwatch_payments import PriceUnavailable
from chainwatch_payments.contrib.django import conf, signals
from chainwatch_payments.contrib.django.models import AddressMode, InvoiceEvent
from chainwatch_payments.contrib.django.models import InvoiceStatus as S
from chainwatch_payments.contrib.django.services import (
    SlotsExhausted,
    create_invoice,
    invoices_to_poll,
    refresh_invoice,
    unused_address_run,
)

from .testapp.models import Invoice

pytestmark = pytest.mark.django_db(transaction=True)


class Rates:
    """A price feed the test can move."""

    def __init__(self, **rates):
        self.rates = {k: Decimal(v) for k, v in rates.items()}
        self.down = False

    def rate(self, asset, fiat):
        if self.down:
            raise PriceUnavailable("feed down")
        try:
            return self.rates[asset]
        except KeyError:
            raise PriceUnavailable(asset) from None


@pytest.fixture
def rates(settings):
    r = Rates(BTC="100000")
    settings.CHAINWATCH = {**settings.CHAINWATCH, "PRICE_PROVIDER": r, "FIXED_RATES": {"USDT:USD": "1"}}
    conf.reset()
    yield r
    conf.reset()


@pytest.fixture(autouse=True)
def fresh_conf():
    conf.reset()
    yield
    conf.reset()


def chain(asset="BTC", network="regtest"):
    return conf.get_registry().get(asset, network)


def btc_invoice(now=None):
    # (20 * 1.01 + 0.90) / 100000 = 0.000211 BTC;  amount due late = $20.90
    return create_invoice(Invoice, asset="BTC", network="regtest", fiat_price="20", now=now)


def late_pay(inv, amount, minutes_after=20):
    when = inv.expires_at + timedelta(minutes=minutes_after)
    chain().credit(inv.address, amount, mined=True, at=when)
    chain().mine(1)
    return when + timedelta(minutes=5)


# -- price at payment time ----------------------------------------------------------

def test_on_time_payment_records_rate_and_value(rates):
    inv = btc_invoice()
    rates.rates["BTC"] = Decimal("101000")
    chain().credit(inv.address, inv.total_price, mined=True)
    chain().mine(1)
    refresh_invoice(inv)
    inv.refresh_from_db()
    assert inv.status == S.PAID and not inv.paid_late
    assert inv.payment_rate == Decimal("101000") and inv.payment_rate_at and inv.payment_detected_at
    assert inv.payment_value_fiat == Decimal("21.3110")   # 0.000211 * 101000


def test_late_payment_after_price_drop_must_cover_value(rates):
    inv = btc_invoice()
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=1))
    rates.rates["BTC"] = Decimal("90000")                 # BTC fell 10%
    t = late_pay(inv, inv.total_price)                    # 0.000211 * 90000 = $18.99 < $20.90
    refresh_invoice(inv, now=t)
    assert inv.status == S.UNDERPAID and inv.payment_rate == Decimal("90000")

    t = late_pay(inv, "0.000022", minutes_after=40)       # +$1.98 -> $20.97 >= $20.90
    refresh_invoice(inv, now=t)
    inv.refresh_from_db()
    assert inv.status == S.PAID and inv.paid_late
    assert inv.payment_value_fiat == Decimal("20.9700")


def test_late_payment_after_price_rise_can_send_less(rates):
    inv = btc_invoice()
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=1))
    rates.rates["BTC"] = Decimal("110000")
    t = late_pay(inv, "0.00019")                          # 0.00019 * 110000 = $20.90
    refresh_invoice(inv, now=t)
    assert inv.status == S.PAID and inv.paid_late


def test_late_payment_needs_a_price(rates):
    inv = btc_invoice()
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=1))
    rates.down = True
    t = late_pay(inv, inv.total_price)
    with pytest.raises(PriceUnavailable):
        refresh_invoice(inv, now=t)
    inv.refresh_from_db()
    assert inv.status == S.EXPIRED and inv.payment_rate is None      # nothing changed, retried later
    rates.down = False
    refresh_invoice(inv, now=t)
    assert inv.status == S.PAID


def test_on_time_payment_does_not_wait_for_price(rates):
    inv = btc_invoice()
    rates.down = True
    chain().credit(inv.address, inv.total_price, mined=True)
    chain().mine(1)
    refresh_invoice(inv)
    assert inv.status == S.PAID and inv.payment_rate is None
    rates.down = False
    refresh_invoice(inv)                                   # rate filled in on a later check
    assert inv.payment_rate == Decimal("100000")


def test_locked_policy_per_chain(rates, settings):
    backends = dict(settings.CHAINWATCH["BACKENDS"])
    backends["BTC:regtest"] = {**backends["BTC:regtest"], "late_payment_pricing": "locked"}
    settings.CHAINWATCH = {**settings.CHAINWATCH, "BACKENDS": backends}
    conf.reset()
    inv = btc_invoice()
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=1))
    rates.rates["BTC"] = Decimal("50000")                 # ignored under "locked"
    t = late_pay(inv, inv.total_price)
    refresh_invoice(inv, now=t)
    assert inv.status == S.PAID and inv.paid_late


def test_bad_late_pricing_value(settings):
    settings.CHAINWATCH = {**settings.CHAINWATCH, "LATE_PAYMENT_PRICING": "whatever"}
    with pytest.raises(ImproperlyConfigured):
        conf.get_registry()


# -- polling windows and backoff -----------------------------------------------------

def test_per_chain_late_window_and_recheck_backoff(settings):
    backends = dict(settings.CHAINWATCH["BACKENDS"])
    backends["BTC:regtest"] = {**backends["BTC:regtest"], "late_payment_hours": 2}
    settings.CHAINWATCH = {**settings.CHAINWATCH, "BACKENDS": backends}
    conf.reset()
    now = timezone.now()
    btc_old = btc_invoice()
    Invoice.objects.filter(pk=btc_old.pk).update(status=S.EXPIRED, expires_at=now - timedelta(hours=3))
    usdt = create_invoice(Invoice, asset="USDT", network="tron", fiat_price="5")
    Invoice.objects.filter(pk=usdt.pk).update(status=S.EXPIRED, expires_at=now - timedelta(hours=3))
    picked = set(invoices_to_poll(Invoice, now).values_list("pk", flat=True))
    assert picked == {usdt.pk}                               # BTC window is 2h, USDT keeps 72h

    Invoice.objects.filter(pk=usdt.pk).update(last_checked_at=now - timedelta(seconds=60))
    assert not invoices_to_poll(Invoice, now).exists()       # late re-check every 300 s
    Invoice.objects.filter(pk=usdt.pk).update(last_checked_at=now - timedelta(seconds=301))
    assert invoices_to_poll(Invoice, now).exists()

    paid = btc_invoice()
    Invoice.objects.filter(pk=paid.pk).update(status=S.PAID, paid_at=now - timedelta(hours=1),
                                              last_checked_at=now - timedelta(seconds=100))
    assert paid.pk not in set(invoices_to_poll(Invoice, now).values_list("pk", flat=True))


# -- audit trail -----------------------------------------------------------------------

def test_every_status_change_is_logged():
    inv = btc_invoice()
    chain().credit(inv.address, inv.total_price)
    refresh_invoice(inv)
    refresh_invoice(inv)
    chain().mine(2)
    refresh_invoice(inv)
    events = list(inv.payment_events())
    assert [(e.previous_status, e.status) for e in events] == [(S.PENDING, S.CONFIRMING), (S.CONFIRMING, S.PAID)]
    assert events[-1].tx_ids == inv.tx_ids and events[-1].confirmations == 2
    assert InvoiceEvent.objects.count() == 2


# -- gap limit ------------------------------------------------------------------------

def test_gap_limit_warning(settings):
    settings.CHAINWATCH = {**settings.CHAINWATCH, "GAP_LIMIT_WARNING": 3}
    conf.reset()
    got = []

    def h(sender, asset, network, unused_run, limit, **kw):
        got.append((asset, network, unused_run))

    signals.gap_limit_warning.connect(h, weak=False)
    try:
        first = btc_invoice()
        btc_invoice()
        assert got == []
        btc_invoice()
        assert got == [("BTC", "regtest", 3)]
        Invoice.objects.filter(pk=first.pk).update(received_amount=1)
        assert unused_address_run(Invoice, "BTC", "regtest") == 2
    finally:
        signals.gap_limit_warning.disconnect(h)


# -- single-address mode ----------------------------------------------------------------

def shared(price="10"):
    return create_invoice(Invoice, asset="USDT", network="shared", fiat_price=price)


def test_single_address_gives_unique_amounts():
    a, b = shared(), shared()
    assert a.address == b.address == "TSHAREDADDRESS"
    assert a.address_mode == AddressMode.SINGLE and a.derivation_index is None
    assert (a.amount_slot, b.amount_slot) == (1, 2)
    assert a.total_price == Decimal("10.000100") and b.total_price == Decimal("10.000200")


def test_single_address_matches_exact_amount_only():
    a, b = shared(), shared()
    ch = chain("USDT", "shared")
    ch.credit("TSHAREDADDRESS", "7", mined=True)               # unrelated customer
    ch.credit("TSHAREDADDRESS", b.total_price, mined=True)
    refresh_invoice(a)
    refresh_invoice(b)
    assert a.status == S.PENDING and b.status == S.PAID


def test_single_address_slots_exhaust_and_free_up():
    invs = [shared() for _ in range(3)]
    with pytest.raises(SlotsExhausted):
        shared()
    Invoice.objects.filter(pk=invs[0].pk).update(status=S.PAID)
    assert shared().amount_slot == 1


def test_single_mode_tolerance_must_be_below_step(settings):
    backends = dict(settings.CHAINWATCH["BACKENDS"])
    backends["USDT:shared"] = {**backends["USDT:shared"], "underpay_tolerance": "0.0001"}
    settings.CHAINWATCH = {**settings.CHAINWATCH, "BACKENDS": backends}
    with pytest.raises(ImproperlyConfigured, match="smaller than amount_step"):
        conf.get_registry()


def test_hd_db_constraint_does_not_block_shared_address():
    shared()
    shared()
    assert Invoice.objects.filter(address="TSHAREDADDRESS").count() == 2


# -- dynamic fees, cross-checked prices, scan start ----------------------------------------

class FlatFee:
    def __init__(self, usd):
        self.usd = Decimal(usd)

    def fee_fiat(self, fiat, prices):
        return self.usd


def test_dynamic_fee_with_minimum(settings):
    backends = dict(settings.CHAINWATCH["BACKENDS"])
    backends["BTC:regtest"] = {**backends["BTC:regtest"], "fee_estimator": FlatFee("2.00"), "fee_margin": "0.1"}
    settings.CHAINWATCH = {**settings.CHAINWATCH, "BACKENDS": backends}
    conf.reset()
    assert btc_invoice().network_fee_fiat == Decimal("2.20")          # 2.00 * 1.1 > minimum 0.90
    backends["BTC:regtest"]["fee_estimator"] = FlatFee("0.10")
    conf.reset()
    assert btc_invoice().network_fee_fiat == Decimal("0.90")          # minimum wins


def test_cross_checked_prices_block_bad_quotes(settings):
    settings.CHAINWATCH = {**settings.CHAINWATCH, "PRICE_PROVIDER": Rates(BTC="100000"),
                           "PRICE_CROSS_CHECK": Rates(BTC="80000"), "FIXED_RATES": {"USDT:USD": "1"}}
    conf.reset()
    with pytest.raises(PriceUnavailable, match="disagree"):
        btc_invoice()
    assert not Invoice.objects.exists()
    # stablecoins skip the cross-check entirely (fixed rate)
    assert create_invoice(Invoice, asset="USDT", network="tron", fiat_price="5").rate == 1


def test_start_height_is_recorded_and_used():
    chain().mine(7)
    inv = btc_invoice()
    assert inv.start_height == 7
    refresh_invoice(inv)
    assert chain().last_since_height == 7


def test_admin_shows_payment_history():
    from django.contrib import admin as dj_admin

    from chainwatch_payments.contrib.django.admin import CryptoInvoiceAdminMixin

    class InvoiceAdmin(CryptoInvoiceAdminMixin, dj_admin.ModelAdmin):
        pass

    inv = btc_invoice()
    chain().credit(inv.address, inv.total_price, mined=True)
    chain().mine(1)
    refresh_invoice(inv)
    html = InvoiceAdmin(Invoice, dj_admin.site).payment_history(inv)
    assert "pending &rarr; paid" in html
