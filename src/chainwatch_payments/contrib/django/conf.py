"""Reads and validates ``settings.CHAINWATCH``.

Everything is configured in ``settings.py``; nothing is stored in the database.
Secrets (xpubs, API keys, the forwarder mnemonic) should come from environment
variables there. See the README's settings reference for every option.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from ...backend import Backend
from ...backends import BitcoinBackend, ERC20Backend, MockBackend, TronTRC20Backend
from ...fees import FeeEstimator, MempoolSpaceFeeEstimator, TronEnergyFeeEstimator
from ...pricing import (
    ChainedPriceProvider,
    CoinGeckoProvider,
    CrossCheckedPriceProvider,
    FixedRateProvider,
    KrakenProvider,
    PriceProvider,
)
from ...registry import PaymentRegistry

__all__ = ["DEFAULTS", "BackendConfig", "get", "backend_config", "backend_configs",
           "get_registry", "get_price_provider", "get_forwarder", "forwarder_keys", "reset"]

DEFAULTS: dict[str, Any] = {
    "BACKENDS": {},
    # pricing
    "PRICE_PROVIDER": "coingecko",
    "PRICE_API_KEY": None,
    "PRICE_API_PLAN": None,
    "PRICE_CACHE_SECONDS": 60,
    "FIXED_RATES": {"USDT:USD": "1", "USDC:USD": "1"},
    "PRICE_CROSS_CHECK": None,            # None | "kraken" | dotted path | provider instance
    "PRICE_MAX_DEVIATION": "0.02",
    "PRICE_REQUIRE_CROSS_CHECK": False,
    # invoice lifecycle
    "QUOTE_MINUTES": 20,
    "START_INDEX": 1,
    "LATE_PAYMENT_HOURS": 72,
    "LATE_PAYMENT_PRICING": "at_payment",   # "at_payment" | "locked"
    "REORG_WATCH_HOURS": 6,
    "GAP_LIMIT_WARNING": 20,
    "SINGLE_ADDRESS_LOOKBACK_MINUTES": 10,
    # polling
    "INVOICE_MODELS": [],
    "POLL_INTERVAL_SECONDS": 30,
    "LATE_RECHECK_SECONDS": 300,
    "PAID_RECHECK_SECONDS": 600,
    # opt-in forwarder (see README "Forwarding")
    "FORWARDERS": {},
    "FORWARD_MAX_ATTEMPTS": 5,
}

BACKEND_TYPES = {
    "bitcoin": BitcoinBackend,
    "tron_trc20": TronTRC20Backend,
    "erc20": ERC20Backend,
    "mock": MockBackend,
}

FEE_ESTIMATOR_TYPES = {
    "mempool": MempoolSpaceFeeEstimator,
    "tron_energy": TronEnergyFeeEstimator,
}

LATE_PRICING = ("at_payment", "locked")


@dataclass(frozen=True)
class BackendConfig:
    asset: str
    network: str
    backend: Backend
    volatility_buffer: Decimal
    network_fee: Decimal              # static fee, also the minimum when an estimator is set
    underpay_tolerance: Decimal
    confirmations: int | None
    late_payment_hours: float
    late_payment_pricing: str
    fee_estimator: FeeEstimator | None
    fee_margin: Decimal
    address_mode: str                 # "hd" | "single"
    amount_step: Decimal | None       # single mode
    max_slots: int
    label: str = ""                   # display name at checkout ("" = automatic)
    checkout: bool = True             # offered by payment_options()


_lock = threading.RLock()
_cache: dict[str, Any] = {}


def reset() -> None:
    with _lock:
        _cache.clear()


def _settings() -> dict[str, Any]:
    raw = getattr(settings, "CHAINWATCH", None) or {}
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ImproperlyConfigured(f"Unknown CHAINWATCH settings: {sorted(unknown)}")
    return {**DEFAULTS, **raw}


def get(name: str) -> Any:
    return _settings()[name]


def _decimal(value: Any, label: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, float):
        raise ImproperlyConfigured(f"{label}: use a string or Decimal, not float ({value!r})")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ImproperlyConfigured(f"{label}: invalid number {value!r}") from exc
    if d < 0 or (not allow_zero and d == 0):
        raise ImproperlyConfigured(f"{label}: must be {'>' if not allow_zero else '>='} 0")
    return d


def _split_key(key: Any) -> tuple[str, str]:
    if isinstance(key, tuple) and len(key) == 2:
        a, n = key
    elif isinstance(key, str) and ":" in key:
        a, n = key.split(":", 1)
    else:
        raise ImproperlyConfigured(f"CHAINWATCH key {key!r} must be 'ASSET:network' or (asset, network)")
    return a.strip().upper(), n.strip().lower()


def _build_backend(label: str, entry: dict[str, Any]) -> Backend:
    if "backend" in entry:
        backend = entry["backend"]
    else:
        kind = entry.get("type")
        if not kind:
            raise ImproperlyConfigured(f"CHAINWATCH BACKENDS[{label}] needs 'type' (or 'backend')")
        cls = BACKEND_TYPES.get(kind) or import_string(kind)
        try:
            backend = cls(**dict(entry.get("options") or {}))
        except (TypeError, ValueError) as exc:
            raise ImproperlyConfigured(f"CHAINWATCH BACKENDS[{label}]: {exc}") from exc
    if not isinstance(backend, Backend):
        raise ImproperlyConfigured(f"CHAINWATCH BACKENDS[{label}] is not a Backend")
    return backend


def _build_fee_estimator(label: str, spec: Any) -> FeeEstimator | None:
    if spec is None:
        return None
    if isinstance(spec, FeeEstimator):
        return spec
    if not isinstance(spec, dict) or "type" not in spec:
        raise ImproperlyConfigured(f"{label}.fee_estimator must be a dict with 'type' or an estimator instance")
    cls = FEE_ESTIMATOR_TYPES.get(spec["type"]) or import_string(spec["type"])
    try:
        est = cls(**dict(spec.get("options") or {}))
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{label}.fee_estimator: {exc}") from exc
    if not isinstance(est, FeeEstimator):
        raise ImproperlyConfigured(f"{label}.fee_estimator is not a FeeEstimator")
    return est


def _label(label: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ImproperlyConfigured(f"{label}.label must be a string")
    return value.strip()


def _bool(label: str, key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ImproperlyConfigured(f"{label}.{key} must be True or False")
    return value


_ENTRY_KEYS = {
    "type", "options", "backend", "volatility_buffer", "network_fee", "underpay_tolerance",
    "confirmations", "late_payment_hours", "late_payment_pricing", "fee_estimator", "fee_margin",
    "address_mode", "amount_step", "max_slots", "label", "checkout",
}


def _load() -> tuple[PaymentRegistry, dict[tuple[str, str], BackendConfig]]:
    registry = PaymentRegistry()
    configs: dict[tuple[str, str], BackendConfig] = {}
    default_late_hours = float(get("LATE_PAYMENT_HOURS"))
    default_pricing = get("LATE_PAYMENT_PRICING")
    if default_pricing not in LATE_PRICING:
        raise ImproperlyConfigured(f"LATE_PAYMENT_PRICING must be one of {LATE_PRICING}")
    for key, entry in (get("BACKENDS") or {}).items():
        asset, network = _split_key(key)
        label = f"{asset}:{network}"
        if not isinstance(entry, dict):
            raise ImproperlyConfigured(f"CHAINWATCH BACKENDS[{label}] must be a dict")
        extra = set(entry) - _ENTRY_KEYS
        if extra:
            raise ImproperlyConfigured(f"CHAINWATCH BACKENDS[{label}]: unknown keys {sorted(extra)}")
        backend = _build_backend(label, entry)
        registry.register(asset, network, backend)

        caps = backend.capabilities()
        mode = entry.get("address_mode", "hd")
        if mode not in ("hd", "single"):
            raise ImproperlyConfigured(f"{label}.address_mode must be 'hd' or 'single'")
        tolerance = _decimal(entry.get("underpay_tolerance", 0), f"{label}.underpay_tolerance")
        step = None
        if mode == "hd" and not caps.hd_derivation:
            raise ImproperlyConfigured(f"{label}: address_mode 'hd' needs an xpub in the backend options")
        if mode == "single":
            if not getattr(backend, "single_address", None):
                raise ImproperlyConfigured(f"{label}: address_mode 'single' needs 'address' in the backend options")
            step = _decimal(entry.get("amount_step", Decimal(1).scaleb(-caps.decimals)), f"{label}.amount_step",
                            allow_zero=False)
            if tolerance >= step:
                raise ImproperlyConfigured(
                    f"{label}: underpay_tolerance ({tolerance}) must be smaller than amount_step ({step}) "
                    "in single-address mode, or payments could match the wrong invoice"
                )
        pricing = entry.get("late_payment_pricing", default_pricing)
        if pricing not in LATE_PRICING:
            raise ImproperlyConfigured(f"{label}.late_payment_pricing must be one of {LATE_PRICING}")
        conf = entry.get("confirmations")
        configs[(asset, network)] = BackendConfig(
            asset=asset,
            network=network,
            backend=backend,
            volatility_buffer=_decimal(entry.get("volatility_buffer", 0), f"{label}.volatility_buffer"),
            network_fee=_decimal(entry.get("network_fee", 0), f"{label}.network_fee"),
            underpay_tolerance=tolerance,
            confirmations=None if conf is None else int(conf),
            late_payment_hours=float(entry.get("late_payment_hours", default_late_hours)),
            late_payment_pricing=pricing,
            fee_estimator=_build_fee_estimator(label, entry.get("fee_estimator")),
            fee_margin=_decimal(entry.get("fee_margin", "0.2"), f"{label}.fee_margin"),
            address_mode=mode,
            amount_step=step,
            max_slots=int(entry.get("max_slots", 1000)),
            label=_label(label, entry.get("label", "")),
            checkout=_bool(label, "checkout", entry.get("checkout", True)),
        )
    return registry, configs


def _loaded():
    with _lock:
        if "registry" not in _cache:
            _cache["registry"], _cache["configs"] = _load()
        return _cache["registry"], _cache["configs"]


def get_registry() -> PaymentRegistry:
    return _loaded()[0]


def backend_configs() -> dict[tuple[str, str], BackendConfig]:
    return dict(_loaded()[1])


def backend_config(asset: str, network: str) -> BackendConfig:
    key = (asset.strip().upper(), network.strip().lower())
    configs = _loaded()[1]
    try:
        return configs[key]
    except KeyError:
        enabled = ", ".join(f"{a}:{n}" for a, n in sorted(configs)) or "none"
        raise ImproperlyConfigured(
            f"{key[0]}:{key[1]} is not in CHAINWATCH['BACKENDS'] (enabled: {enabled})"
        ) from None


def _fixed_rates() -> FixedRateProvider:
    rates = {}
    for k, v in (get("FIXED_RATES") or {}).items():
        a, f = _split_key(k) if isinstance(k, str) else k
        rates[(a.upper(), f.upper())] = _decimal(v, f"FIXED_RATES[{k}]")
    return FixedRateProvider(rates)


def _instantiate_provider(spec: Any, label: str) -> PriceProvider:
    if isinstance(spec, PriceProvider):
        return spec
    if spec == "kraken":
        return KrakenProvider(cache_seconds=float(get("PRICE_CACHE_SECONDS")))
    if isinstance(spec, str):
        obj = import_string(spec)
        provider = obj() if callable(obj) and not isinstance(obj, PriceProvider) else obj
        if isinstance(provider, PriceProvider):
            return provider
    raise ImproperlyConfigured(f"{label} {spec!r} is not a PriceProvider")


def _build_price_provider() -> PriceProvider:
    spec = get("PRICE_PROVIDER")
    if spec == "fixed":
        return _fixed_rates()
    if spec == "coingecko":
        try:
            market: PriceProvider = CoinGeckoProvider(
                get("PRICE_API_KEY"),
                plan=get("PRICE_API_PLAN"),
                cache_seconds=float(get("PRICE_CACHE_SECONDS")),
            )
        except ValueError as exc:
            raise ImproperlyConfigured(f"CHAINWATCH price provider: {exc}") from exc
    else:
        market = _instantiate_provider(spec, "PRICE_PROVIDER")
    cross = get("PRICE_CROSS_CHECK")
    if cross:
        market = CrossCheckedPriceProvider(
            market,
            _instantiate_provider(cross, "PRICE_CROSS_CHECK"),
            max_deviation=_decimal(get("PRICE_MAX_DEVIATION"), "PRICE_MAX_DEVIATION"),
            require_reference=bool(get("PRICE_REQUIRE_CROSS_CHECK")),
        )
    # fixed (stablecoin) rates first: no API call, no cross-check needed
    return ChainedPriceProvider([_fixed_rates(), market])


def get_price_provider() -> PriceProvider:
    with _lock:
        if "price" not in _cache:
            _cache["price"] = _build_price_provider()
        return _cache["price"]


# -- opt-in forwarder ------------------------------------------------------------------

_COMMON_FORWARDER_KEYS = {"type", "mnemonic_env", "passphrase_env", "destination", "allowed_destinations",
                          "min_amount", "client", "xpub", "account", "account_path"}
_FORWARDER_KEYS = {
    "tron_trc20": _COMMON_FORWARDER_KEYS | {"network", "api_key", "gas_trx", "fee_limit_trx", "contract"},
    "bitcoin": _COMMON_FORWARDER_KEYS | {"network", "base_url", "fee_target", "fee_rate", "max_fee_rate"},
    "erc20": _COMMON_FORWARDER_KEYS | {"rpc_url", "chain_id", "contract", "gas_limit", "max_fee_gwei",
                                       "gas_buffer", "wait_seconds"},
}


def forwarder_keys() -> list[tuple[str, str]]:
    return [_split_key(k) for k in (get("FORWARDERS") or {})]


def _forwarder_type(entry: dict[str, Any], backend: Any, label: str) -> str:
    kind = entry.get("type")
    if kind is None:
        if isinstance(backend, TronTRC20Backend):
            kind = "tron_trc20"
        elif isinstance(backend, BitcoinBackend):
            kind = "bitcoin"
        elif isinstance(backend, ERC20Backend):
            kind = "erc20"
        else:
            raise ImproperlyConfigured(f"FORWARDERS[{label}]: set 'type' (tron_trc20, bitcoin or erc20)")
    if kind not in _FORWARDER_KEYS:
        raise ImproperlyConfigured(f"FORWARDERS[{label}]: type must be one of {sorted(_FORWARDER_KEYS)}")
    return kind


def _build_forwarder(asset: str, network: str):
    import os

    from ...forwarder.base import ForwardError

    entries = {_split_key(k): v for k, v in (get("FORWARDERS") or {}).items()}
    label = f"{asset}:{network}"
    try:
        entry = entries[(asset, network)]
    except KeyError:
        raise ImproperlyConfigured(f"no CHAINWATCH['FORWARDERS'] entry for {label}") from None
    cfg = backend_config(asset, network)
    backend = cfg.backend
    kind = _forwarder_type(entry, backend, label)
    extra = set(entry) - _FORWARDER_KEYS[kind]
    if extra:
        raise ImproperlyConfigured(f"FORWARDERS[{label}] ({kind}): unknown keys {sorted(extra)}")
    if cfg.address_mode != "hd":
        raise ImproperlyConfigured(f"FORWARDERS[{label}]: forwarding needs address_mode 'hd' "
                                   "(single-address payments already land in your wallet)")

    env = entry.get("mnemonic_env")
    if not env:
        raise ImproperlyConfigured(f"FORWARDERS[{label}] needs 'mnemonic_env' (the NAME of an environment variable)")
    mnemonic = os.environ.get(env, "")
    if not mnemonic:
        raise ImproperlyConfigured(f"environment variable {env} is not set (forwarder seed for {label})")
    passphrase = os.environ.get(entry["passphrase_env"], "") if entry.get("passphrase_env") else ""
    watch_key = getattr(backend, "_xpub", None)
    xpub = entry.get("xpub") or (watch_key.serialize() if watch_key is not None else None)
    if not xpub:
        raise ImproperlyConfigured(f"FORWARDERS[{label}]: the backend has no xpub to check the seed against")
    destination = entry.get("destination")
    if not destination:
        raise ImproperlyConfigured(f"FORWARDERS[{label}] needs 'destination'")
    decimals = backend.capabilities().decimals
    common = dict(
        mnemonic=mnemonic, passphrase=passphrase, expected_xpub=xpub, destination=destination,
        allowed_destinations=entry.get("allowed_destinations") or [destination],
        account=int(entry.get("account", 0)), account_path=entry.get("account_path"),
    )
    min_default = {"bitcoin": "0.0001", "tron_trc20": "1", "erc20": "1"}[kind]
    min_amount = int(_decimal(entry.get("min_amount", min_default), f"{label}.min_amount") * 10**decimals)
    try:
        if kind == "tron_trc20":
            from ...forwarder.tron import TronpyClient, TronTokenForwarder

            contract = entry.get("contract") or getattr(backend, "contract", None)
            client = entry.get("client") or TronpyClient(
                network=entry.get("network") or getattr(backend, "network", "mainnet"), api_key=entry.get("api_key"))
            return TronTokenForwarder(
                **common, contract=contract, client=client, min_amount=min_amount,
                gas_trx=_decimal(entry.get("gas_trx", "20"), f"{label}.gas_trx"),
                fee_limit_trx=_decimal(entry.get("fee_limit_trx", "30"), f"{label}.fee_limit_trx"),
            )
        if kind == "bitcoin":
            from ...forwarder.bitcoin import BitcoinForwarder, EsploraSpendClient

            btc_network = entry.get("network") or getattr(backend, "network", "mainnet")
            if getattr(backend, "script", "p2wpkh") != "p2wpkh":
                raise ImproperlyConfigured(
                    f"FORWARDERS[{label}]: only native SegWit (p2wpkh) addresses can be forwarded")
            client = entry.get("client") or EsploraSpendClient(btc_network, base_url=entry.get("base_url"))
            return BitcoinForwarder(
                **common, client=client, network=btc_network, min_amount=min_amount,
                fee_target=entry.get("fee_target", "halfHourFee"),
                fee_rate=_decimal(entry["fee_rate"], f"{label}.fee_rate") if entry.get("fee_rate") else None,
                max_fee_rate=_decimal(entry.get("max_fee_rate", "100"), f"{label}.max_fee_rate"),
            )
        from ...forwarder.evm import ERC20Forwarder, EvmRpcClient

        contract = entry.get("contract") or getattr(backend, "contract", None)
        rpc_url = entry.get("rpc_url") or getattr(getattr(backend, "source", None), "rpc_url", None)
        if not entry.get("client") and not rpc_url:
            raise ImproperlyConfigured(f"FORWARDERS[{label}] needs 'rpc_url'")
        client = entry.get("client") or EvmRpcClient(rpc_url)
        return ERC20Forwarder(
            **common, contract=contract, client=client, min_amount=min_amount,
            chain_id=int(entry["chain_id"]) if entry.get("chain_id") else None,
            gas_limit=int(entry.get("gas_limit", 100_000)),
            max_fee_gwei=_decimal(entry.get("max_fee_gwei", "50"), f"{label}.max_fee_gwei"),
            gas_buffer=_decimal(entry.get("gas_buffer", "1.2"), f"{label}.gas_buffer"),
            wait_seconds=float(entry.get("wait_seconds", 180)),
        )
    except ForwardError as exc:
        raise ImproperlyConfigured(f"FORWARDERS[{label}]: {exc}") from exc


def get_forwarder(asset: str, network: str):
    """Build (once per process) the forwarder for a chain. Reads the mnemonic
    from the environment at this moment; it is never stored in settings."""
    key = (asset.strip().upper(), network.strip().lower())
    with _lock:
        cache = _cache.setdefault("forwarders", {})
        if key not in cache:
            cache[key] = _build_forwarder(*key)
        return cache[key]
