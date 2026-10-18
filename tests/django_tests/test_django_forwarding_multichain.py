"""Django forwarding for BTC and ERC-20: type inference, sent->confirmed,
dropped->retry, deferral not counting as failure."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured

pytest.importorskip("embit")
pytest.importorskip("eth_account")

from chainwatch_payments.backends import BitcoinBackend, ERC20Backend  # noqa: E402
from chainwatch_payments.contrib.django import conf  # noqa: E402
from chainwatch_payments.contrib.django.forwarding import forward_paid_invoices  # noqa: E402
from chainwatch_payments.contrib.django.models import ForwardRecord, ForwardStatus  # noqa: E402
from chainwatch_payments.contrib.django.models import InvoiceStatus as S  # noqa: E402
from chainwatch_payments.contrib.django.services import create_invoice  # noqa: E402
from chainwatch_payments.forwarder.bitcoin import BitcoinForwarder  # noqa: E402
from chainwatch_payments.forwarder.evm import ERC20Forwarder  # noqa: E402

from ..conftest import ETH_XPUB, ZPUB_84  # noqa: E402
from ..test_forwarder_btc import FakeEsplora  # noqa: E402
from ..test_forwarder_evm import (
    FakeNode,  # noqa: E402
    M,  # noqa: E402
)
from .test_django_forwarding import NoHttp  # noqa: E402
from .testapp.models import Invoice  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

# Destinations outside the test wallet, so they can never equal a deposit address.
BTC_DEST = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"      # BIP173 example address
EVM_DEST = "0x" + "11" * 20


class EvmHttp:
    """Answers the one call invoice creation makes (block height)."""

    def post_json(self, url, payload, headers=None):
        assert payload["method"] == "eth_blockNumber"
        return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x10"}

    def get_json(self, *a, **k):
        raise AssertionError("unexpected GET")


@pytest.fixture
def chains(settings, monkeypatch):
    monkeypatch.setenv("TEST_SEED", M)
    esplora, node = FakeEsplora(rate="5"), FakeNode()
    settings.CHAINWATCH = {
        **settings.CHAINWATCH,
        "BACKENDS": {
            **settings.CHAINWATCH["BACKENDS"],
            "BTC:mainnet": {"backend": BitcoinBackend(ZPUB_84, http=NoHttp())},
            "USDC:ethereum": {"backend": ERC20Backend(ETH_XPUB, asset="USDC", rpc_url="https://rpc.test",
                                                      http=EvmHttp())},
        },
        "FIXED_RATES": {"USDT:USD": "1", "USDC:USD": "1", "BTC:USD": "100000"},
        "FORWARDERS": {
            "BTC:mainnet": {"mnemonic_env": "TEST_SEED", "destination": BTC_DEST, "client": esplora},
            "USDC:ethereum": {"mnemonic_env": "TEST_SEED", "destination": EVM_DEST, "client": node,
                              "chain_id": 1, "wait_seconds": 0},
        },
        "FORWARD_MAX_ATTEMPTS": 2,
    }
    conf.reset()
    yield esplora, node
    conf.reset()


def paid(asset, network, price="50"):
    inv = create_invoice(Invoice, asset=asset, network=network, fiat_price=price)
    Invoice.objects.filter(pk=inv.pk).update(status=S.PAID)
    return inv


def test_types_are_inferred_from_backends(chains):
    assert isinstance(conf.get_forwarder("BTC", "mainnet"), BitcoinForwarder)
    assert isinstance(conf.get_forwarder("USDC", "ethereum"), ERC20Forwarder)


def test_btc_sent_then_confirmed(chains):
    esplora, _ = chains
    inv = paid("BTC", "mainnet")
    esplora._utxos[inv.address] = [{"txid": "aa" * 32, "vout": 0, "value": 60_000, "status": {"confirmed": True}}]
    r = forward_paid_invoices()
    assert (r.sent, r.forwarded) == (1, 0)
    rec = ForwardRecord.objects.get(asset="BTC")
    assert rec.status == ForwardStatus.SENT and rec.fee > 0 and rec.amount + rec.fee == Decimal("0.0006")
    assert len(esplora.broadcasts) == 1
    assert forward_paid_invoices().sent == 1 and len(esplora.broadcasts) == 1   # pending: no re-send
    esplora.states[rec.transfer_tx] = "confirmed"
    assert forward_paid_invoices().forwarded == 1
    assert ForwardRecord.objects.get(asset="BTC").status == ForwardStatus.DONE


def test_btc_dropped_is_retried(chains):
    esplora, _ = chains
    inv = paid("BTC", "mainnet")
    esplora._utxos[inv.address] = [{"txid": "aa" * 32, "vout": 0, "value": 60_000, "status": {"confirmed": True}}]
    forward_paid_invoices()
    rec = ForwardRecord.objects.get(asset="BTC")
    esplora.states[rec.transfer_tx] = "dropped"
    r = forward_paid_invoices()                    # marks dropped -> pending and re-sends in the same run
    assert r.sent == 1 and len(esplora.broadcasts) == 2
    assert ForwardRecord.objects.get(asset="BTC").status == ForwardStatus.SENT


def test_evm_forward_done(chains):
    _, node = chains
    inv = paid("USDC", "ethereum")
    node.tokens[inv.address.lower()] = 50_000_000
    r = forward_paid_invoices()
    assert r.forwarded == 1
    rec = ForwardRecord.objects.get(asset="USDC")
    assert rec.status == ForwardStatus.DONE and rec.gas_tx and rec.transfer_tx and rec.fee == 0


def test_deferral_does_not_use_up_attempts(chains):
    esplora, _ = chains
    inv = paid("BTC", "mainnet")
    esplora._utxos[inv.address] = [{"txid": "aa" * 32, "vout": 0, "value": 60_000, "status": {"confirmed": True}}]
    esplora.rate = esplora.rate * 100                 # 500 sat/vB, above the default cap of 100
    for _ in range(4):
        assert forward_paid_invoices().deferred == 1
    rec = ForwardRecord.objects.get(asset="BTC")
    assert rec.status == ForwardStatus.PENDING and rec.attempts == 0 and rec.error.startswith("deferred")
    esplora.rate = esplora.rate / 100
    assert forward_paid_invoices().sent == 1


def test_forwarder_needs_hd_mode_and_known_keys(chains, settings):
    fw = {**settings.CHAINWATCH["FORWARDERS"], "USDT:shared": {"mnemonic_env": "TEST_SEED", "destination": "T",
                                                               "type": "tron_trc20"}}
    settings.CHAINWATCH = {**settings.CHAINWATCH, "FORWARDERS": fw}
    conf.reset()
    with pytest.raises(ImproperlyConfigured, match="address_mode 'hd'"):
        conf.get_forwarder("USDT", "shared")
    fw = {**settings.CHAINWATCH["FORWARDERS"],
          "BTC:mainnet": {**settings.CHAINWATCH["FORWARDERS"]["BTC:mainnet"], "gas_trx": "5"}}
    settings.CHAINWATCH = {**settings.CHAINWATCH, "FORWARDERS": fw}
    conf.reset()
    with pytest.raises(ImproperlyConfigured, match="unknown keys"):
        conf.get_forwarder("BTC", "mainnet")


# -- forwarding log (ForwardEvent) ---------------------------------------------------------

def kinds(asset):
    from chainwatch_payments.contrib.django.models import ForwardEvent
    return [e.kind for e in ForwardEvent.objects.filter(record__asset=asset).order_by("created_at", "pk")]


def test_log_btc_sent_dropped_resent_confirmed(chains):
    from chainwatch_payments.contrib.django.models import ForwardEvent

    esplora, _ = chains
    inv = paid("BTC", "mainnet")
    esplora._utxos[inv.address] = [{"txid": "aa" * 32, "vout": 0, "value": 60_000, "status": {"confirmed": True}}]
    forward_paid_invoices()
    first = ForwardRecord.objects.get(asset="BTC").transfer_tx
    esplora.states[first] = "dropped"
    forward_paid_invoices()                                   # dropped -> re-sent
    rec = ForwardRecord.objects.get(asset="BTC")
    esplora.states[rec.transfer_tx] = "confirmed"
    forward_paid_invoices()
    assert kinds("BTC") == ["sent", "dropped", "sent", "confirmed"]

    sent = ForwardEvent.objects.filter(kind="sent").first()
    assert (sent.from_address, sent.to_address) == (inv.address, BTC_DEST)
    assert sent.amount + sent.fee == Decimal("0.0006") and sent.transfer_tx == first and sent.attempt == 1
    dropped = ForwardEvent.objects.get(kind="dropped")
    assert dropped.transfer_tx == first and "dropped" in dropped.message
    assert ForwardEvent.objects.get(kind="confirmed").status == ForwardStatus.DONE
    assert list(rec.events.values_list("kind", flat=True)) == ["sent", "dropped", "sent", "confirmed"]


def test_log_deferred_error_failed_and_done(chains):
    esplora, node = chains
    inv = paid("BTC", "mainnet")
    esplora._utxos[inv.address] = [{"txid": "aa" * 32, "vout": 0, "value": 60_000, "status": {"confirmed": True}}]
    esplora.rate = esplora.rate * 100
    forward_paid_invoices()                                   # deferred
    esplora.rate = esplora.rate / 100

    def boom(raw):
        raise RuntimeError("node rejected")

    esplora.broadcast = boom
    forward_paid_invoices()                                   # error (attempt 1)
    forward_paid_invoices()                                   # failed (attempt 2 = max)
    assert kinds("BTC") == ["deferred", "error", "failed"]

    evm = paid("USDC", "ethereum")
    node.tokens[evm.address.lower()] = 1_000_000
    forward_paid_invoices()
    assert kinds("USDC") == ["done"]


def test_log_nothing(chains):
    paid("BTC", "mainnet")                                    # no UTXOs on the address
    forward_paid_invoices()
    assert kinds("BTC") == ["nothing"]


def test_admin_shows_forward_log(chains):
    from django.contrib import admin as dj_admin

    from chainwatch_payments.contrib.django.admin import ForwardEventInline, ForwardRecordAdmin

    assert ForwardEventInline in ForwardRecordAdmin.inlines
    esplora, _ = chains
    inv = paid("BTC", "mainnet")
    esplora._utxos[inv.address] = [{"txid": "aa" * 32, "vout": 0, "value": 60_000, "status": {"confirmed": True}}]
    forward_paid_invoices()
    inline = ForwardEventInline(ForwardRecord, dj_admin.site)
    assert not inline.has_add_permission(None) and not inline.has_change_permission(None)
