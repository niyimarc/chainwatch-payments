"""Dynamic network-fee estimates, converted to fiat so they can be added to a quote.

The estimate is what it will cost YOU to move one received payment later
(spending one input on BTC, one USDT transfer on TRON). Use it with a static
minimum and a safety margin; see ``estimate_fee_fiat``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from .errors import ChainSourceError
from .http import HttpClient, default_http_client
from .pricing import PriceProvider, PriceUnavailable

__all__ = [
    "FeeUnavailable",
    "FeeEstimator",
    "MempoolSpaceFeeEstimator",
    "TronEnergyFeeEstimator",
    "estimate_fee_fiat",
    "MEMPOOL_URLS",
]


class FeeUnavailable(PriceUnavailable):
    """A fee estimate could not be obtained."""


@runtime_checkable
class FeeEstimator(Protocol):
    def fee_fiat(self, fiat: str, prices: PriceProvider) -> Decimal:
        """Estimated cost, in ``fiat``, of moving one received payment."""
        ...


class _Cached:
    def __init__(self, cache_seconds: float, http: HttpClient | None, clock) -> None:
        self.cache_seconds = cache_seconds
        self._http = http
        self._clock = clock
        self._value: tuple[float, Any] | None = None
        self._lock = threading.Lock()

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = default_http_client()
        return self._http

    def _get(self, fetch):
        with self._lock:
            if self._value and self._clock() - self._value[0] < self.cache_seconds:
                return self._value[1]
        value = fetch()
        with self._lock:
            self._value = (self._clock(), value)
        return value


MEMPOOL_URLS = {
    "mainnet": "https://mempool.space/api",
    "testnet": "https://mempool.space/testnet/api",
    "testnet4": "https://mempool.space/testnet4/api",
    "signet": "https://mempool.space/signet/api",
}


class MempoolSpaceFeeEstimator(_Cached):
    """BTC fee from mempool.space's recommended fee rates.

    ``vbytes``: size you will pay for per received payment. 110 vB is a
    1-input/1-output native SegWit transaction (forwarding); if you instead
    spend many payments together from your wallet, ~68 vB per input is fair.
    ``target``: ``fastestFee``, ``halfHourFee`` (default), ``hourFee`` or ``economyFee``.
    """

    TARGETS = ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")

    def __init__(self, *, network: str = "mainnet", target: str = "halfHourFee", vbytes: int = 110,
                 base_url: str | None = None, http: HttpClient | None = None,
                 cache_seconds: float = 60.0, clock=time.monotonic) -> None:
        if target not in self.TARGETS:
            raise ValueError(f"target must be one of {self.TARGETS}")
        url = base_url or MEMPOOL_URLS.get(network)
        if url is None:
            raise ValueError(f"no default mempool.space URL for {network}; pass base_url=")
        super().__init__(cache_seconds, http, clock)
        self.base_url = url.rstrip("/")
        self.target = target
        self.vbytes = vbytes

    def sat_per_vbyte(self) -> Decimal:
        def fetch():
            try:
                data = self.http.get_json(f"{self.base_url}/v1/fees/recommended")
                return Decimal(str(data[self.target]))
            except ChainSourceError as exc:
                raise FeeUnavailable(f"mempool.space request failed: {exc}") from exc
            except (KeyError, TypeError, ArithmeticError) as exc:
                raise FeeUnavailable(f"unexpected mempool.space response: {exc}") from exc
        return self._get(fetch)

    def fee_fiat(self, fiat: str, prices: PriceProvider) -> Decimal:
        btc = self.sat_per_vbyte() * self.vbytes / Decimal(10**8)
        return btc * prices.rate("BTC", fiat)


TRON_URLS = {
    "mainnet": "https://api.trongrid.io",
    "nile": "https://nile.trongrid.io",
    "shasta": "https://api.shasta.trongrid.io",
}


class TronEnergyFeeEstimator(_Cached):
    """TRX cost of one TRC-20 transfer, from the chain's current energy and
    bandwidth prices (``/wallet/getchainparameters``).

    ``energy``: energy one USDT transfer uses. Measured on mainnet (Oct 2026):
    ~130,000 including TRON's dynamic-energy penalty on the busy USDT
    contract (the base cost is ~65,000). Default 130,000; tune from your own
    forward receipts (``energy_usage_total``).
    ``bandwidth``: bytes of bandwidth burned (~347 for a TRC-20 transfer).
    This is the BURN cost; if you stake/delegate energy the real cost is lower.
    """

    def __init__(self, *, network: str = "mainnet", energy: int = 130_000, bandwidth: int = 350,
                 base_url: str | None = None, api_key: str | None = None,
                 http: HttpClient | None = None, cache_seconds: float = 300.0, clock=time.monotonic) -> None:
        url = base_url or TRON_URLS.get(network)
        if url is None:
            raise ValueError(f"no default TronGrid URL for {network}; pass base_url=")
        super().__init__(cache_seconds, http, clock)
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.energy = energy
        self.bandwidth = bandwidth

    def prices_sun(self) -> tuple[Decimal, Decimal]:
        """(sun per energy unit, sun per bandwidth byte)."""
        def fetch():
            headers = {"TRON-PRO-API-KEY": self.api_key} if self.api_key else {}
            try:
                data = self.http.post_json(f"{self.base_url}/wallet/getchainparameters", {}, headers=headers)
            except ChainSourceError as exc:
                raise FeeUnavailable(f"TronGrid request failed: {exc}") from exc
            params: Mapping[str, Any] = {
                p.get("key"): p.get("value") for p in (data or {}).get("chainParameter", []) if isinstance(p, dict)
            }
            try:
                return Decimal(str(params["getEnergyFee"])), Decimal(str(params["getTransactionFee"]))
            except KeyError as exc:
                raise FeeUnavailable(f"TRON chain parameter missing: {exc}") from exc
        return self._get(fetch)

    def fee_trx(self) -> Decimal:
        energy_sun, bw_sun = self.prices_sun()
        return (self.energy * energy_sun + self.bandwidth * bw_sun) / Decimal(10**6)

    def fee_fiat(self, fiat: str, prices: PriceProvider) -> Decimal:
        return self.fee_trx() * prices.rate("TRX", fiat)


def estimate_fee_fiat(
    estimator: FeeEstimator | None,
    *,
    fiat: str,
    prices: PriceProvider,
    minimum: Decimal = Decimal(0),
    margin: Decimal = Decimal("0.2"),
    fallback_to_minimum: bool = True,
) -> Decimal:
    """``max(minimum, estimate * (1 + margin))``, rounded up to cents.

    If the estimator fails and ``fallback_to_minimum`` is true (and a minimum
    is set), the minimum is used; otherwise ``FeeUnavailable`` is raised.
    """
    from decimal import ROUND_UP

    if estimator is None:
        return minimum
    try:
        est = estimator.fee_fiat(fiat, prices) * (1 + margin)
    except PriceUnavailable:
        if fallback_to_minimum and minimum > 0:
            return minimum
        raise
    return max(minimum, est).quantize(Decimal("0.01"), rounding=ROUND_UP)
