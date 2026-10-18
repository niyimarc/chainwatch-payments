"""Django forwarding: config checks, forward once, failures, signals."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from chainwatch_payments.backends import TronTRC20Backend
from chainwatch_payments.contrib.django import conf, signals
from chainwatch_payments.contrib.django.forwarding import forward_paid_invoices
from chainwatch_payments.contrib.django.models import ForwardRecord, ForwardStatus
from chainwatch_payments.contrib.django.models import InvoiceStatus as S
from chainwatch_payments.contrib.django.services import create_invoice

from ..conftest import ETH_XPUB, TRX_XPUB
from ..test_forwarder import TREASURY, FakeTron, M
from .testapp.models import Invoice

pytestmark = pytest.mark.django_db(transaction=True)


class NoHttp:
    def get_json(self, *a, **k):
        raise AssertionError("no chain reads in these tests")

    post_json = get_json


@pytest.fixture
def net(settings, monkeypatch):
    fake = FakeTron()
    monkeypatch.setenv("TEST_TRON_MNEMONIC", M)
    backend = TronTRC20Backend(TRX_XPUB, network="nile", http=NoHttp())
    settings.CHAINWATCH = {
        **settings.CHAINWATCH,
        "BACKENDS": {**settings.CHAINWATCH["BACKENDS"], "USDT:nile": {"backend": backend}},
        "FORWARDERS": {"USDT:nile": {"mnemonic_env": "TEST_TRON_MNEMONIC", "destination": TREASURY,
                                     "client": fake, "min_amount": "0.5"}},
        "FORWARD_MAX_ATTEMPTS": 2,
    }
    conf.reset()
    yield fake
    conf.reset()


def paid_invoice(fake, amount=25_000_000):
    inv = create_invoice(Invoice, asset="USDT", network="nile", fiat_price="25")
    Invoice.objects.filter(pk=inv.pk).update(status=S.PAID)
    fake.tokens[inv.address] = amount
    return inv


def test_forwards_each_paid_invoice_once(net):
    got = []
    h = lambda sender, invoice, record, **kw: got.append(record.transfer_tx)  # noqa: E731
    signals.invoice_forwarded.connect(h, weak=False)
    try:
        inv = paid_invoice(net)
        assert inv.derivation_index == 1                       # index 0 = gas wallet
        r = forward_paid_invoices()
        assert (r.forwarded, r.errors) == (1, 0)
        rec = ForwardRecord.objects.get()
        assert rec.status == ForwardStatus.DONE and rec.amount == Decimal("25")
        assert rec.from_address == inv.address and rec.to_address == TREASURY
        assert net.tokens[TREASURY] == 25_000_000 and got == [rec.transfer_tx]
        r2 = forward_paid_invoices()
        assert (r2.forwarded, r2.nothing) == (0, 0)            # already done: not even attempted
        assert len(net.sent) == 2                              # one gas tx + one transfer, ever
    finally:
        signals.invoice_forwarded.disconnect(h)


def test_unpaid_invoices_are_not_forwarded(net):
    inv = create_invoice(Invoice, asset="USDT", network="nile", fiat_price="25")
    net.tokens[inv.address] = 1_000_000
    assert forward_paid_invoices().forwarded == 0 and net.sent == []


def test_zero_balance_marks_done_without_sending(net):
    inv = paid_invoice(net, amount=0)
    r = forward_paid_invoices()
    assert r.nothing == 1 and net.sent == []
    assert ForwardRecord.objects.get(invoice_pk=str(inv.pk)).status == ForwardStatus.DONE


def test_failures_retry_then_give_up(net):
    got = []
    h = lambda sender, invoice, record, error, **kw: got.append(error)  # noqa: E731
    signals.forward_failed.connect(h, weak=False)
    try:
        paid_invoice(net)

        def broken(*a, **k):
            raise RuntimeError("node unavailable")

        net.send_token = broken
        assert forward_paid_invoices().errors == 1
        assert ForwardRecord.objects.get().status == ForwardStatus.PENDING
        assert forward_paid_invoices().errors == 1
        rec = ForwardRecord.objects.get()
        assert rec.status == ForwardStatus.FAILED and rec.attempts == 2 and "unavailable" in rec.error
        assert got == ["node unavailable"]
        assert forward_paid_invoices().errors == 0           # failed records are left for a human
    finally:
        signals.forward_failed.disconnect(h)


def test_forwarder_config_errors(net, settings, monkeypatch):
    monkeypatch.delenv("TEST_TRON_MNEMONIC")
    conf.reset()
    with pytest.raises(ImproperlyConfigured, match="is not set"):
        conf.get_forwarder("USDT", "nile")
    monkeypatch.setenv("TEST_TRON_MNEMONIC", M)
    fw = dict(settings.CHAINWATCH["FORWARDERS"]["USDT:nile"], xpub=ETH_XPUB)
    settings.CHAINWATCH = {**settings.CHAINWATCH, "FORWARDERS": {"USDT:nile": fw}}
    conf.reset()
    with pytest.raises(ImproperlyConfigured, match="does not match the xpub"):
        conf.get_forwarder("USDT", "nile")


def test_forward_command(net, capsys):
    paid_invoice(net)
    call_command("chainwatch_forward")
    assert "forwarded=1 sent=0 nothing=0 deferred=0 errors=0" in capsys.readouterr().out
