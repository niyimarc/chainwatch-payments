"""Pricing providers and quote maths (no network)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from chainwatch_payments import (
    ChainedPriceProvider,
    ChainSourceError,
    CoinGeckoProvider,
    FixedRateProvider,
    PriceUnavailable,
    quote,
)

CG = "https://api.coingecko.com/api/v3/simple/price"


def test_quote_rounds_up_and_includes_buffer_and_fee():
    q = quote(asset="btc", fiat="usd", fiat_price="20", rate="96500", decimals=8,
              volatility_buffer="0.01", network_fee_fiat="0.90")
    assert q.total_fiat == Decimal("21.1000")
    # 21.10 / 96500 = 0.000218652849... -> rounded UP to 8 dp
    assert q.total_amount == Decimal("0.00021866")
    assert q.asset == "BTC" and q.fiat == "USD"


def test_quote_stablecoin_exact():
    q = quote(asset="USDT", fiat="USD", fiat_price="20", rate="1", decimals=6, network_fee_fiat="1.5")
    assert q.total_amount == Decimal("21.500000")


@pytest.mark.parametrize("kw", [
    dict(fiat_price="0"), dict(rate="0"), dict(volatility_buffer="-0.1"), dict(network_fee_fiat="-1"),
])
def test_quote_rejects_bad_inputs(kw):
    args = dict(asset="BTC", fiat="USD", fiat_price="10", rate="100", decimals=8)
    args.update(kw)
    with pytest.raises((ValueError, PriceUnavailable)):
        quote(**args)


def test_quote_refuses_floats():
    with pytest.raises(TypeError):
        quote(asset="BTC", fiat="USD", fiat_price=10.0, rate="1", decimals=8)


def test_fixed_rates():
    p = FixedRateProvider()
    assert p.rate("usdt", "usd") == 1
    with pytest.raises(PriceUnavailable):
        p.rate("BTC", "USD")
    assert FixedRateProvider({("USDT", "EUR"): "0.92"}).rate("USDT", "EUR") == Decimal("0.92")


def test_coingecko_demo_key_header_and_parsing(http):
    seen = {}

    def route(params):
        seen.update(params)
        return {"bitcoin": {"usd": 96500.12}}

    http.on_get(CG, route)
    p = CoinGeckoProvider("demo-key", http=http)
    assert p.rate("BTC", "USD") == Decimal("96500.12")
    assert seen == {"ids": "bitcoin", "vs_currencies": "usd"}


def test_coingecko_pro_url():
    p = CoinGeckoProvider("k", plan="pro")
    assert p.base_url.startswith("https://pro-api.coingecko.com")
    with pytest.raises(ValueError):
        CoinGeckoProvider(plan="pro")


def test_coingecko_cache(http):
    t = [0.0]
    calls = []
    http.on_get(CG, lambda params: calls.append(1) or {"bitcoin": {"usd": 100}})
    p = CoinGeckoProvider(http=http, cache_seconds=60, clock=lambda: t[0])
    p.rate("BTC", "USD")
    p.rate("BTC", "USD")
    assert len(calls) == 1
    t[0] = 61
    p.rate("BTC", "USD")
    assert len(calls) == 2


def test_coingecko_failures_become_price_unavailable(http):
    p = CoinGeckoProvider(http=http)
    http.on_get(CG, {})
    with pytest.raises(PriceUnavailable):
        p.rate("BTC", "USD")
    http.on_get(CG, {"bitcoin": {"usd": 0}})
    with pytest.raises(PriceUnavailable):
        p.rate("BTC", "USD")

    def down(params):
        raise ChainSourceError("429 Too Many Requests")

    http.on_get(CG, down)
    with pytest.raises(PriceUnavailable, match="429"):
        p.rate("BTC", "USD")
    with pytest.raises(PriceUnavailable, match="unknown CoinGecko id"):
        p.rate("XYZ", "USD")


def test_chained_provider_prefers_fixed(http):
    calls = []
    http.on_get(CG, lambda params: calls.append(params) or {"bitcoin": {"usd": 50000}})
    p = ChainedPriceProvider([FixedRateProvider(), CoinGeckoProvider(http=http)])
    assert p.rate("USDT", "USD") == 1 and calls == []
    assert p.rate("BTC", "USD") == 50000 and len(calls) == 1


# -- Kraken and cross-checking -----------------------------------------------------------

KR = "https://api.kraken.com/0/public/Ticker"


def test_kraken_parsing_and_asset_mapping(http):
    seen = {}

    def route(params):
        seen.update(params)
        return {"error": [], "result": {"XXBTZUSD": {"c": ["96510.5", "0.01"]}}}

    http.on_get(KR, route)
    from chainwatch_payments import KrakenProvider
    assert KrakenProvider(http=http).rate("BTC", "USD") == Decimal("96510.5")
    assert seen == {"pair": "XBTUSD"}


def test_kraken_errors(http):
    from chainwatch_payments import KrakenProvider
    http.on_get(KR, {"error": ["EQuery:Unknown asset pair"]})
    with pytest.raises(PriceUnavailable, match="Unknown asset pair"):
        KrakenProvider(http=http).rate("ABC", "USD")


class Static:
    def __init__(self, value=None, fail=False):
        self.value, self.fail = value, fail

    def rate(self, asset, fiat):
        if self.fail:
            raise PriceUnavailable("down")
        return Decimal(self.value)


def test_cross_check_accepts_close_prices():
    from chainwatch_payments import CrossCheckedPriceProvider
    p = CrossCheckedPriceProvider(Static("100000"), Static("101000"), max_deviation="0.02")
    assert p.rate("BTC", "USD") == Decimal("100000")


def test_cross_check_refuses_disagreeing_prices():
    from chainwatch_payments import CrossCheckedPriceProvider
    p = CrossCheckedPriceProvider(Static("100000"), Static("90000"), max_deviation="0.02")
    with pytest.raises(PriceUnavailable, match="disagree"):
        p.rate("BTC", "USD")


def test_cross_check_reference_down(caplog):
    from chainwatch_payments import CrossCheckedPriceProvider
    lenient = CrossCheckedPriceProvider(Static("100"), Static(fail=True))
    assert lenient.rate("BTC", "USD") == 100
    assert "unavailable" in caplog.text
    strict = CrossCheckedPriceProvider(Static("100"), Static(fail=True), require_reference=True)
    with pytest.raises(PriceUnavailable):
        strict.rate("BTC", "USD")
