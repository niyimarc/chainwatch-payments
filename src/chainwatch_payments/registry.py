"""Registry / facade that routes calls to the right (asset, network) backend."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from threading import RLock

from .backend import Backend
from .confirm import evaluate
from .errors import BackendAlreadyRegistered, BackendNotFound, UnsupportedCapability
from .types import Capabilities, MatchMode, PaymentStatus, Tolerance, to_base_units

__all__ = ["PaymentRegistry"]

Key = tuple[str, str]


def _key(asset: str, network: str) -> Key:
    if not asset or not network:
        raise ValueError("asset and network are required")
    return asset.strip().upper(), network.strip().lower()


class PaymentRegistry:
    """Holds the enabled backends and exposes the host-facing API.

    Asset symbols are case-insensitive (stored upper-case) and network names
    are case-insensitive (stored lower-case): ``("usdt", "TRON")`` and
    ``("USDT", "tron")`` are the same backend.
    """

    def __init__(self, default_tolerance: Tolerance | None = None) -> None:
        self._backends: dict[Key, Backend] = {}
        self._lock = RLock()
        self.default_tolerance = default_tolerance or Tolerance.exact()

    # -- registration ----------------------------------------------------------
    def register(self, asset: str, network: str, backend: Backend, *, replace: bool = False) -> None:
        if not isinstance(backend, Backend):
            raise TypeError(
                f"{type(backend).__name__} does not implement the Backend protocol "
                "(capabilities, derive_address, get_incoming)"
            )
        k = _key(asset, network)
        with self._lock:
            if k in self._backends and not replace:
                raise BackendAlreadyRegistered(f"{k[0]}/{k[1]} is already registered")
            self._backends[k] = backend

    def unregister(self, asset: str, network: str) -> None:
        with self._lock:
            self._backends.pop(_key(asset, network), None)

    def get(self, asset: str, network: str) -> Backend:
        k = _key(asset, network)
        try:
            return self._backends[k]
        except KeyError:
            enabled = ", ".join(f"{a}/{n}" for a, n in sorted(self._backends)) or "none"
            raise BackendNotFound(f"no backend registered for {k[0]}/{k[1]} (enabled: {enabled})") from None

    def __contains__(self, item: object) -> bool:
        return isinstance(item, tuple) and len(item) == 2 and _key(*item) in self._backends

    def enabled(self) -> list[Key]:
        return sorted(self._backends)

    def capabilities(self, asset: str, network: str) -> Capabilities:
        return self.get(asset, network).capabilities()

    # -- addresses -------------------------------------------------------------
    def derive_address(self, asset: str, network: str, index: int) -> str:
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("index must be a non-negative int")
        backend = self.get(asset, network)
        if not backend.capabilities().hd_derivation:
            raise UnsupportedCapability(f"{_key(asset, network)} does not support HD derivation")
        return backend.derive_address(index)

    def receive_address(self, asset: str, network: str) -> str:
        backend = self.get(asset, network)
        fn = getattr(backend, "receive_address", None)
        if fn is None:
            raise UnsupportedCapability("backend has no single-address mode")
        return fn()

    def address_with_memo(self, asset: str, network: str, reference: str) -> tuple[str, str]:
        backend = self.get(asset, network)
        if not backend.capabilities().memo or not hasattr(backend, "address_with_memo"):
            raise UnsupportedCapability(f"{_key(asset, network)} does not support memos/tags")
        return backend.address_with_memo(reference)

    # -- confirmation ------------------------------------------------------------
    def confirm(
        self,
        asset: str,
        network: str,
        address: str,
        expected_amount: Decimal | int | str,
        required_confirmations: int | None = None,
        not_before: datetime | None = None,
        *,
        tolerance: Tolerance | None = None,
        memo: str | None = None,
        match_mode: MatchMode = MatchMode.SUM,
        previous: PaymentStatus | None = None,
    ) -> PaymentStatus:
        """Read the chain and report the payment status for ``address``.

        ``expected_amount`` is in HUMAN units of the asset (``Decimal("25")``
        USDT, ``Decimal("0.0015")`` BTC). Never pass floats.

        Raises ``ChainSourceError`` if the chain can't be read: a failed read
        is never reported as "no payment".
        """
        k = _key(asset, network)
        backend = self.get(*k)
        caps = backend.capabilities()
        required = caps.default_confirmations if required_confirmations is None else required_confirmations
        expected = to_base_units(expected_amount, caps.decimals)
        transfers = backend.get_incoming(address, not_before=not_before)
        return evaluate(
            transfers,
            asset=k[0],
            network=k[1],
            address=address,
            expected_amount=expected,
            decimals=caps.decimals,
            required_confirmations=required,
            tolerance=tolerance or self.default_tolerance,
            not_before=not_before,
            memo=memo,
            match_mode=match_mode,
            previous=previous,
        )
