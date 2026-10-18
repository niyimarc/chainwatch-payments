"""chainwatch_payments: watch-only, multi-chain crypto payment detection.

Derive receive addresses from extended PUBLIC keys and confirm incoming
payments across BTC, TRON TRC-20 and EVM ERC-20 (or your own backends). No
private keys, no sending, no payment gateway.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .backend import Backend, BaseBackend
from .confirm import evaluate, unique_amount
from .errors import (
    BackendAlreadyRegistered,
    BackendNotFound,
    ChainSourceError,
    ChainwatchError,
    MissingDependency,
    PrivateKeyRejected,
    UnsupportedCapability,
)
from .pricing import (
    ChainedPriceProvider,
    CoinGeckoProvider,
    CrossCheckedPriceProvider,
    FixedRateProvider,
    KrakenProvider,
    PriceProvider,
    PriceUnavailable,
    Quote,
    quote,
)
from .registry import PaymentRegistry
from .types import (
    AddressMode,
    Capabilities,
    Match,
    MatchMode,
    PaymentStatus,
    State,
    Tolerance,
    Transfer,
    from_base_units,
    to_base_units,
)

__version__ = "0.0.2"

__all__ = [
    "__version__",
    # facade
    "PaymentRegistry", "default_registry", "register", "derive_address", "confirm",
    # protocol
    "Backend", "BaseBackend",
    # types
    "AddressMode", "Capabilities", "Transfer", "PaymentStatus", "State", "Match",
    "MatchMode", "Tolerance", "to_base_units", "from_base_units",
    # logic
    "evaluate", "unique_amount",
    # pricing
    "PriceProvider", "FixedRateProvider", "CoinGeckoProvider", "ChainedPriceProvider",
    "KrakenProvider", "CrossCheckedPriceProvider",
    "PriceUnavailable", "Quote", "quote",
    # errors
    "ChainwatchError", "BackendNotFound", "BackendAlreadyRegistered",
    "UnsupportedCapability", "ChainSourceError", "MissingDependency", "PrivateKeyRejected",
]

#: Process-wide registry used by the module-level helpers below. Create your
#: own ``PaymentRegistry()`` instead if you prefer explicit wiring.
default_registry = PaymentRegistry()


def register(asset: str, network: str, backend: Backend, *, replace: bool = False) -> None:
    default_registry.register(asset, network, backend, replace=replace)


def derive_address(asset: str, network: str, index: int) -> str:
    return default_registry.derive_address(asset, network, index)


def confirm(
    asset: str,
    network: str,
    address: str,
    expected_amount: Decimal | int | str,
    required_confirmations: int | None = None,
    not_before: datetime | None = None,
    **kwargs,
) -> PaymentStatus:
    return default_registry.confirm(
        asset, network, address, expected_amount, required_confirmations, not_before, **kwargs
    )
