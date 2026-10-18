"""Django integration: invoice creation, status lifecycle, signals, polling."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from chainwatch_payments import ChainSourceError, FixedRateProvider, PriceUnavailable
from chainwatch_payments.backends import MockBackend
from chainwatch_payments.contrib.django import conf, signals
from chainwatch_payments.contrib.django.models import DerivationCounter
from chainwatch_payments.contrib.django.models import InvoiceStatus as S
from chainwatch_payments.contrib.django.services import (
    allocate_index,
    create_invoice,
    invoices_to_poll,
    poll_open_invoices,
    refresh_invoice,
)

from .testapp.models import Invoice

pytestmark = pytest.mark.django_db(transaction=True)  # so on_commit signals actually fire


@pytest.fixture(autouse=True)
def fresh_conf():
    conf.reset()
    yield
    conf.reset()


@pytest.fixture
def chain():
    def get(asset="BTC", network="regtest") -> MockBackend:
        return conf.get_registry().get(asset, network)
    return get


@pytest.fixture
def events():
    got = []

    def handler(signal_name):
        def _h(sender, invoice, status, previous, **kw):
            got.append((signal_name, previous, invoice.status))
        return _h

    hs = {}
    for name in ("invoice_status_changed", "invoice_confirming", "invoice_partially_paid", "invoice_paid",
                 "invoice_underpaid", "invoice_expired", "invoice_reverted"):
        hs[name] = handler(name)
        getattr(signals, name).connect(hs[name], weak=False)
    yield got
    for name, h in hs.items():
        getattr(signals, name).disconnect(h)


def specific(events):
    return [e for e in events if e[0] != "invoice_status_changed"]


def new_btc(**kw):
    return create_invoice(Invoice, asset="btc", network="regtest", fiat_price="20", customer="alice", **kw)


# -- creation -----------------------------------------------------------------------------

def test_create_invoice_prices_and_addresses(chain):
    now = timezone.now()
    inv = new_btc(now=now)
    inv.refresh_from_db()
    assert (inv.asset, inv.network, inv.customer) == ("BTC", "regtest", "alice")
    assert inv.derivation_index == 1                     # START_INDEX default 1 (0 reserved)
    assert inv.address == chain().derive_address(1)
    # (20 * 1.01 + 0.90) / 100000 = 0.000211 BTC
    assert inv.total_price == Decimal("0.00021100")
    assert inv.rate == Decimal("100000")
    assert inv.status == S.PENDING
    assert inv.expires_at == now + timedelta(minutes=20)
    assert new_btc().derivation_index == 2


def test_indexes_are_per_chain_and_unique():
    assert [allocate_index("BTC", "regtest") for _ in range(3)] == [1, 2, 3]
    assert allocate_index("USDT", "tron") == 1
    assert DerivationCounter.objects.get(asset="BTC").next_index == 4


def test_db_rejects_duplicate_index():
    inv = new_btc()
    dup = Invoice(asset="BTC", network="regtest", fiat_price=1, rate=1, total_price=1,
                  derivation_index=inv.derivation_index, address="other", expires_at=timezone.now())
    with pytest.raises(IntegrityError), transaction.atomic():
        dup.save()


def test_price_failure_creates_nothing(settings):
    settings.CHAINWATCH = {**settings.CHAINWATCH, "FIXED_RATES": {}}
    with pytest.raises(PriceUnavailable):
        new_btc()
    assert Invoice.objects.count() == 0
    assert not DerivationCounter.objects.exists()


def test_unknown_chain_is_a_config_error():
    with pytest.raises(ImproperlyConfigured, match="not in CHAINWATCH"):
        create_invoice(Invoice, asset="ETH", network="mainnet", fiat_price="5")


def test_stablecoin_quote():
    inv = create_invoice(Invoice, asset="USDT", network="tron", fiat_price="20")
    assert inv.total_price == Decimal("21.500000")


# -- lifecycle ------------------------------------------------------------------------------

def test_happy_path_signals_fire_once(chain, events):
    inv = new_btc()
    refresh_invoice(inv)
    assert inv.status == S.PENDING and events == []

    chain().credit(inv.address, inv.total_price)
    refresh_invoice(inv)
    refresh_invoice(inv)                                  # no change: no repeat signal
    assert inv.status == S.CONFIRMING

    chain().mine(2)
    refresh_invoice(inv)
    refresh_invoice(inv)
    inv.refresh_from_db()
    assert inv.status == S.PAID and inv.is_paid and inv.paid_at and not inv.paid_late
    assert inv.confirmations == 2 and len(inv.tx_ids) == 1
    assert specific(events) == [("invoice_confirming", S.PENDING, S.CONFIRMING),
                                ("invoice_paid", S.CONFIRMING, S.PAID)]
    assert len([e for e in events if e[0] == "invoice_status_changed"]) == 2


def test_overpaid_counts_as_paid(chain, events):
    inv = new_btc()
    chain().credit(inv.address, inv.total_price * 2, mined=True)
    chain().mine(1)
    refresh_invoice(inv)
    assert inv.status == S.OVERPAID and inv.is_paid
    assert specific(events) == [("invoice_paid", S.PENDING, S.OVERPAID)]


def test_partial_then_topped_up(chain):
    inv = new_btc()
    chain().credit(inv.address, "0.0001", mined=True)
    chain().mine(2)
    refresh_invoice(inv)
    assert inv.status == S.PARTIALLY_PAID
    chain().credit(inv.address, "0.000111", mined=True)
    chain().mine(1)
    refresh_invoice(inv)
    assert inv.status == S.PAID


def test_underpaid_after_expiry(chain, events):
    inv = new_btc()
    chain().credit(inv.address, "0.0001", mined=True)
    chain().mine(2)
    refresh_invoice(inv, now=inv.expires_at + timedelta(seconds=1))
    assert inv.status == S.UNDERPAID
    assert specific(events)[-1][0] == "invoice_underpaid"


def test_underpay_tolerance_from_settings(chain):
    inv = create_invoice(Invoice, asset="USDT", network="tron", fiat_price="20")   # 21.50, tol 0.01
    chain("USDT", "tron").credit(inv.address, "21.49", mined=True)
    chain("USDT", "tron").mine(18)
    refresh_invoice(inv)
    assert inv.status == S.PAID


def test_expired_with_nothing(events):
    inv = new_btc()
    refresh_invoice(inv, now=inv.expires_at)
    assert inv.status == S.EXPIRED
    assert specific(events) == [("invoice_expired", S.PENDING, S.EXPIRED)]


def test_late_payment_covering_price_is_accepted(chain, events):
    inv = new_btc()
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=1))
    assert inv.status == S.EXPIRED
    chain().credit(inv.address, inv.total_price, mined=True, at=inv.expires_at + timedelta(minutes=20))
    chain().mine(1)
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=30))
    inv.refresh_from_db()
    assert inv.status == S.PAID and inv.paid_late
    assert specific(events)[-1] == ("invoice_paid", S.EXPIRED, S.PAID)


def test_late_payment_short_of_price_stays_underpaid(chain):
    inv = new_btc()
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=1))
    chain().credit(inv.address, "0.0002", mined=True, at=inv.expires_at + timedelta(minutes=20))
    chain().mine(3)
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=30))
    assert inv.status == S.UNDERPAID and not inv.is_paid


def test_paid_on_time_but_confirmed_after_expiry_is_not_late(chain):
    inv = new_btc()
    chain().credit(inv.address, inv.total_price)
    refresh_invoice(inv)
    assert inv.status == S.CONFIRMING
    chain().mine(2)
    refresh_invoice(inv, now=inv.expires_at + timedelta(minutes=5))
    assert inv.status == S.PAID and not inv.paid_late


def test_reorg_reverts_then_reconfirms(chain, events):
    inv = new_btc()
    chain().credit(inv.address, inv.total_price, mined=True)
    chain().mine(1)
    refresh_invoice(inv)
    assert inv.status == S.PAID
    chain().reorg(2, drop=False)                    # tx back to mempool
    refresh_invoice(inv)
    assert inv.status == S.REVERTED
    chain().mine(2)
    refresh_invoice(inv)
    assert inv.status == S.PAID
    assert [e[0] for e in specific(events)] == ["invoice_paid", "invoice_reverted", "invoice_paid"]


def test_chain_error_changes_nothing(chain):
    inv = new_btc()

    def down(*a, **k):
        raise ChainSourceError("explorer down")

    chain().get_incoming = down
    with pytest.raises(ChainSourceError):
        refresh_invoice(inv)
    inv.refresh_from_db()
    assert inv.status == S.PENDING and inv.last_checked_at is None


# -- polling ------------------------------------------------------------------------------

def test_poll_selects_the_right_invoices():
    now = timezone.now()
    active = new_btc()
    old_expired = new_btc()
    Invoice.objects.filter(pk=old_expired.pk).update(status=S.EXPIRED, expires_at=now - timedelta(hours=100))
    recent_expired = new_btc()
    Invoice.objects.filter(pk=recent_expired.pk).update(status=S.EXPIRED, expires_at=now - timedelta(hours=1))
    old_paid = new_btc()
    Invoice.objects.filter(pk=old_paid.pk).update(status=S.PAID, paid_at=now - timedelta(hours=7))
    recent_paid = new_btc()
    Invoice.objects.filter(pk=recent_paid.pk).update(status=S.PAID, paid_at=now - timedelta(hours=1))
    picked = set(invoices_to_poll(Invoice, now).values_list("pk", flat=True))
    assert picked == {active.pk, recent_expired.pk, recent_paid.pk}


def test_poll_isolates_failures(chain):
    good = new_btc()
    bad = create_invoice(Invoice, asset="USDT", network="tron", fiat_price="5")
    chain().credit(good.address, good.total_price, mined=True)
    chain().mine(1)

    def down(*a, **k):
        raise ChainSourceError("TronGrid 429")

    chain("USDT", "tron").get_incoming = down
    r = poll_open_invoices()
    assert (r.checked, r.changed, r.errors) == (2, 1, 1)
    assert r.failures[0][0] == bad.pk
    good.refresh_from_db()
    assert good.status == S.PAID


def test_management_command(chain, capsys):
    inv = new_btc()
    chain().credit(inv.address, inv.total_price, mined=True)
    chain().mine(1)
    call_command("chainwatch_poll", "--model", "testapp.Invoice")
    assert "checked=1 changed=1 errors=0" in capsys.readouterr().out
    inv.refresh_from_db()
    assert inv.status == S.PAID


def test_admin_mixin_action(chain, rf):
    from django.contrib import admin

    from chainwatch_payments.contrib.django.admin import CryptoInvoiceAdminMixin

    class InvoiceAdmin(CryptoInvoiceAdminMixin, admin.ModelAdmin):
        pass

    inv = new_btc()
    chain().credit(inv.address, inv.total_price, mined=True)
    chain().mine(1)
    ma = InvoiceAdmin(Invoice, admin.site)
    msgs = []
    ma.message_user = lambda request, msg, level=None: msgs.append(msg)
    ma.chainwatch_check_now(rf.get("/"), Invoice.objects.all())
    inv.refresh_from_db()
    assert inv.status == S.PAID and msgs == ["Checked 1 invoice(s); 0 failed."]


# -- configuration ------------------------------------------------------------------------

def test_config_validation(settings):
    settings.CHAINWATCH = {"BACKENDS": {"BTC": {"type": "mock"}}}
    with pytest.raises(ImproperlyConfigured, match="ASSET:network"):
        conf.get_registry()
    settings.CHAINWATCH = {"BACKENDS": {"BTC:x": {"type": "mock", "volatility_buffer": 0.01}}}
    with pytest.raises(ImproperlyConfigured, match="not float"):
        conf.get_registry()
    settings.CHAINWATCH = {"BACKENDS": {"BTC:x": {"type": "mock", "typo": 1}}}
    with pytest.raises(ImproperlyConfigured, match="unknown keys"):
        conf.get_registry()
    settings.CHAINWATCH = {"BAKCENDS": {}}
    with pytest.raises(ImproperlyConfigured, match="Unknown CHAINWATCH settings"):
        conf.get_registry()


def test_backend_instance_and_dotted_price_provider(settings):
    b = MockBackend("LTC", "mainnet", decimals=8)
    settings.CHAINWATCH = {"BACKENDS": {("LTC", "mainnet"): {"backend": b}},
                           "PRICE_PROVIDER": FixedRateProvider({("LTC", "USD"): "80"})}
    inv = create_invoice(Invoice, asset="LTC", network="mainnet", fiat_price="8")
    assert inv.total_price == Decimal("0.1") and inv.address == b.derive_address(1)


def test_coingecko_provider_is_chained_with_fixed(settings):
    settings.CHAINWATCH = {**settings.CHAINWATCH, "PRICE_PROVIDER": "coingecko", "PRICE_API_KEY": "k"}
    p = conf.get_price_provider()
    assert p.rate("USDT", "USD") == 1   # stablecoin served by FIXED_RATES, no HTTP call
