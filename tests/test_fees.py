"""Dynamic fee estimators (mocked HTTP)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from chainwatch_payments import FixedRateProvider
from chainwatch_payments.fees import (
    FeeUnavailable,
    MempoolSpaceFeeEstimator,
    TronEnergyFeeEstimator,
    estimate_fee_fiat,
)

PRICES = FixedRateProvider({("BTC", "USD"): "100000", ("TRX", "USD"): "0.25"})


def test_mempool_fee(http):
    http.on_get("https://mempool.space/api/v1/fees/recommended",
                {"fastestFee": 20, "halfHourFee": 10, "hourFee": 5, "economyFee": 2, "minimumFee": 1})
    est = MempoolSpaceFeeEstimator(http=http)
    # 10 sat/vB * 110 vB = 1100 sat = 0.000011 BTC = $1.10
    assert est.fee_fiat("USD", PRICES) == Decimal("1.10")
    assert MempoolSpaceFeeEstimator(network="testnet4", http=http).base_url.endswith("/testnet4/api")


def test_tron_energy_fee(http):
    http.on_post("https://api.trongrid.io/wallet/getchainparameters", {"chainParameter": [
        {"key": "getEnergyFee", "value": 100}, {"key": "getTransactionFee", "value": 1000},
        {"key": "getSomethingElse", "value": 1},
    ]})
    est = TronEnergyFeeEstimator(http=http, energy=65_000)
    # 65000*100 + 350*1000 sun = 6,850,000 sun = 6.85 TRX = $1.7125 (energy set explicitly)
    assert est.fee_trx() == Decimal("6.85")
    assert est.fee_fiat("USD", PRICES) == Decimal("1.7125")


def test_estimate_applies_margin_minimum_and_fallback(http):
    http.on_get("https://mempool.space/api/v1/fees/recommended", {"halfHourFee": 10})
    est = MempoolSpaceFeeEstimator(http=http)
    assert estimate_fee_fiat(est, fiat="USD", prices=PRICES) == Decimal("1.32")         # 1.10 * 1.2
    assert estimate_fee_fiat(est, fiat="USD", prices=PRICES, minimum=Decimal("2")) == Decimal("2")
    assert estimate_fee_fiat(None, fiat="USD", prices=PRICES, minimum=Decimal("0.5")) == Decimal("0.5")

    http.on_get("https://mempool.space/api/v1/fees/recommended", {})
    broken = MempoolSpaceFeeEstimator(http=http)
    assert estimate_fee_fiat(broken, fiat="USD", prices=PRICES, minimum=Decimal("3")) == Decimal("3")
    with pytest.raises(FeeUnavailable):
        estimate_fee_fiat(broken, fiat="USD", prices=PRICES)


def test_fee_cache(http):
    t = [0.0]
    calls = []
    http.on_get("https://mempool.space/api/v1/fees/recommended",
                lambda p: calls.append(1) or {"halfHourFee": 1})
    est = MempoolSpaceFeeEstimator(http=http, clock=lambda: t[0], cache_seconds=60)
    est.sat_per_vbyte()
    est.sat_per_vbyte()
    t[0] = 100
    est.sat_per_vbyte()
    assert len(calls) == 2
