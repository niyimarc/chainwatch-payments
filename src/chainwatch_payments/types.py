"""Normalised, chain-agnostic data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

__all__ = [
    "AddressMode",
    "Capabilities",
    "Transfer",
    "State",
    "Match",
    "MatchMode",
    "Tolerance",
    "PaymentStatus",
    "to_base_units",
    "from_base_units",
]


class AddressMode(str, Enum):
    HD = "hd"                  # one-time address per index (default)
    SINGLE = "single"          # one fixed address, payments told apart by memo/amount


@dataclass(frozen=True)
class Capabilities:
    hd_derivation: bool
    memo: bool
    decimals: int
    default_confirmations: int
    address_modes: tuple[AddressMode, ...] = (AddressMode.HD,)


@dataclass(frozen=True)
class Transfer:
    """One incoming credit to a watched address, in integer base units.

    ``index`` disambiguates several credits inside one transaction (a BTC
    output index, an EVM log index). ``(tx_id, index)`` is the identity used
    for de-duplication.
    """

    tx_id: str
    amount: int
    confirmations: int
    timestamp: datetime | None = None   # block time; None while unconfirmed
    index: int = 0
    memo: str | None = None
    block_height: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_id, self.index)


class State(str, Enum):
    NONE = "none"            # nothing seen
    SEEN = "seen"            # something arrived, not (yet) enough confirmed value
    CONFIRMED = "confirmed"  # confirmed value covers the expected amount


class Match(str, Enum):
    NONE = "none"    # nothing to compare
    EXACT = "exact"  # within the tolerance band
    UNDER = "under"
    OVER = "over"


class MatchMode(str, Enum):
    SUM = "sum"        # all credits to the address add up (HD default)
    SINGLE = "single"  # one credit must match on its own (amount discriminator)


@dataclass(frozen=True)
class Tolerance:
    """Acceptance band around the expected amount.

    Absolute values are in the asset's human units (e.g. ``Decimal("0.01")``
    USDT); relative values are fractions (``Decimal("0.005")`` = 0.5%). The
    effective band on each side is the larger of the absolute and relative
    allowance.
    """

    under_abs: Decimal = Decimal(0)
    over_abs: Decimal = Decimal(0)
    under_rel: Decimal = Decimal(0)
    over_rel: Decimal = Decimal(0)

    @classmethod
    def exact(cls) -> "Tolerance":
        return cls()

    @classmethod
    def absolute(cls, under: Decimal | str | int, over: Decimal | str | int | None = None) -> "Tolerance":
        u = Decimal(str(under))
        return cls(under_abs=u, over_abs=Decimal(str(over)) if over is not None else u)

    @classmethod
    def relative(cls, under: Decimal | str | float, over: Decimal | str | float | None = None) -> "Tolerance":
        u = Decimal(str(under))
        return cls(under_rel=u, over_rel=Decimal(str(over)) if over is not None else u)

    def band(self, expected: int, decimals: int) -> tuple[int, int]:
        """Return the inclusive (low, high) acceptance band in base units."""
        scale = Decimal(10) ** decimals
        under = max(self.under_abs * scale, self.under_rel * expected)
        over = max(self.over_abs * scale, self.over_rel * expected)
        low = expected - int(under.to_integral_value(rounding="ROUND_FLOOR"))
        high = expected + int(over.to_integral_value(rounding="ROUND_FLOOR"))
        return max(low, 0), high


@dataclass(frozen=True)
class PaymentStatus:
    asset: str
    network: str
    address: str
    state: State
    match: Match
    expected_amount: int        # base units
    received_amount: int        # all matching credits, any confirmations
    confirmed_amount: int       # credits with >= required confirmations
    required_confirmations: int
    confirmations: int          # min confirmations over the counted credits (0 if none)
    tx_ids: tuple[str, ...] = ()
    decimals: int = 0
    reverted: bool = False      # True if a previously CONFIRMED status no longer holds
    memo: str | None = None
    transfers: tuple[Transfer, ...] = field(default=(), repr=False)

    @property
    def is_paid(self) -> bool:
        """Convenience for hosts: confirmed AND not underpaid."""
        return self.state is State.CONFIRMED

    def _human(self, v: int) -> Decimal:
        return from_base_units(v, self.decimals)

    @property
    def expected(self) -> Decimal:
        return self._human(self.expected_amount)

    @property
    def received(self) -> Decimal:
        return self._human(self.received_amount)

    @property
    def confirmed(self) -> Decimal:
        return self._human(self.confirmed_amount)


def to_base_units(amount: Decimal | int | str, decimals: int) -> int:
    """Convert a human amount (``Decimal("12.5")``) to integer base units.

    Floats are refused on purpose (binary rounding). Raises if the amount has
    more precision than the asset supports.
    """
    if isinstance(amount, float):
        raise TypeError("use Decimal or str for amounts, not float")
    d = Decimal(str(amount)) * (Decimal(10) ** decimals)
    if d != d.to_integral_value():
        raise ValueError(f"amount {amount} has more than {decimals} decimal places")
    if d < 0:
        raise ValueError("amount must be non-negative")
    return int(d)


def from_base_units(amount: int, decimals: int) -> Decimal:
    return Decimal(amount).scaleb(-decimals)
