"""Django: letting the customer choose a coin (options, previews, safe parsing)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured

from chainwatch_payments import PriceUnavailable
from chainwatch_payments.backends import BitcoinBackend, ERC20Backend, MockBackend, TronTRC20Backend
from chainwatch_payments.contrib.django import conf
from chainwatch_payments.contrib.django.checkout import (
    _default_label,
    parse_option,
    payment_choices,
    payment_options,
    quote_invoice,
    quote_options,
)
from chainwatch_payments.contrib.django.models import DerivationCounter
from chainwatch_payments.contrib.django.services import create_invoice

from .testapp.models import Invoice

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def fresh_conf():
    conf.reset()
    yield
    conf.reset()


def _backends(settings, **changes):
    backends = {k: dict(v) for k, v in settings.CHAINWATCH["BACKENDS"].items()}
    for key, extra in changes.items():
        backends[key.replace("__", ":")].update(extra)
    settings.CHAINWATCH = {**settings.CHAINWATCH, "BACKENDS": backends}
    conf.reset()


def test_options_follow_settings_order():
    opts = payment_options()
    assert [o.key for o in opts] == ["BTC:regtest", "USDT:tron", "USDT:shared"]
    btc = opts[0]
    assert (btc.asset, btc.network, btc.decimals, btc.confirmations) == ("BTC", "regtest", 8, 2)
    assert btc.label == "BTC (regtest)"   # mock backend: asset + network
    assert payment_choices()[1] == ("USDT:tron", "USDT (tron)")


def test_label_and_checkout_flag(settings):
    _backends(settings, USDT__tron={"label": "Tether on TRON"}, USDT__shared={"checkout": False})
    assert payment_choices() == [("BTC:regtest", "BTC (regtest)"), ("USDT:tron", "Tether on TRON")]
    # still configured, so existing invoices keep being watched and the developer can still create one
    assert conf.backend_config("USDT", "shared").checkout is False


@pytest.mark.parametrize("bad", [{"label": 5}, {"checkout": "no"}])
def test_bad_option_settings_rejected(settings, bad):
    _backends(settings, USDT__tron=bad)
    with pytest.raises(ImproperlyConfigured):
        payment_options()


def test_default_labels_for_real_backends():
    def bare(cls):
        return object.__new__(cls)

    assert _default_label("BTC", "mainnet", bare(BitcoinBackend)) == "BTC"
    assert _default_label("BTC", "testnet", bare(BitcoinBackend)) == "BTC (testnet)"
    assert _default_label("USDT", "tron", bare(TronTRC20Backend)) == "USDT (TRC-20)"
    assert _default_label("USDT", "nile", bare(TronTRC20Backend)) == "USDT (TRC-20, nile)"
    assert _default_label("USDC", "ethereum", bare(ERC20Backend)) == "USDC (ERC-20)"
    assert _default_label("USDC", "sepolia", bare(ERC20Backend)) == "USDC (ERC-20, sepolia)"
    assert _default_label("X", "foo", bare(MockBackend)) == "X (foo)"


def test_parse_option_only_accepts_offered_coins(settings):
    assert parse_option("usdt:TRON") == ("USDT", "tron")
    for bad in ["DOGE:mainnet", "USDT", "", None, "BTC:regtest:x"]:
        with pytest.raises(ValueError):
            parse_option(bad)
    _backends(settings, USDT__shared={"checkout": False})
    with pytest.raises(ValueError):
        parse_option("USDT:shared")


def test_quote_matches_invoice_and_creates_nothing():
    q = quote_invoice(asset="btc", network="REGTEST", fiat_price="49.99")
    # 49.99 * 1.01 + 0.90 = 51.3899 USD at 100000 USD/BTC, rounded up to 8 decimals
    assert q.total_fiat == Decimal("51.3899")
    assert q.total_amount == Decimal("0.00051390")
    assert q.rate == Decimal("100000") and q.exact is True
    assert q.option.key == "BTC:regtest"
    assert Invoice.objects.count() == 0 and DerivationCounter.objects.count() == 0

    inv = create_invoice(Invoice, asset="BTC", network="regtest", fiat_price="49.99")
    assert inv.total_price == q.total_amount


def test_quote_options_prices_every_coin():
    quotes = {q.option.key: q for q in quote_options("49.99")}
    assert list(quotes) == ["BTC:regtest", "USDT:tron", "USDT:shared"]
    assert quotes["USDT:tron"].total_amount == Decimal("51.490000")
    assert quotes["USDT:shared"].exact is False      # a slot amount is added at creation
    assert all(not q.error for q in quotes.values())
    assert Invoice.objects.count() == 0


def test_quote_options_keeps_going_when_one_price_is_down(settings):
    class OnlyStable:
        def rate(self, asset, fiat):
            raise PriceUnavailable(f"no {asset} price")

    settings.CHAINWATCH = {**settings.CHAINWATCH, "PRICE_PROVIDER": OnlyStable(),
                           "FIXED_RATES": {"USDT:USD": "1"}}
    conf.reset()
    quotes = {q.option.key: q for q in quote_options("10")}
    assert quotes["BTC:regtest"].error and quotes["BTC:regtest"].total_amount == 0
    assert not quotes["USDT:tron"].error and quotes["USDT:tron"].total_amount == Decimal("11.500000")
    with pytest.raises(PriceUnavailable):
        quote_invoice(asset="BTC", network="regtest", fiat_price="10")
