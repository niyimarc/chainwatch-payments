"""Shared pieces for the shipped reference backends."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..backend import BaseBackend
from ..crypto.bip32 import ExtendedPublicKey
from ..errors import UnsupportedCapability
from ..types import AddressMode, Capabilities, Transfer

__all__ = ["ChainSource", "HDBackend"]

DEFAULT_PATH = "0/{index}"


@runtime_checkable
class ChainSource(Protocol):
    """Read-only access to a chain: explorer API, RPC node, or a mock."""

    def incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]: ...


class HDBackend(BaseBackend):
    """Backend that derives one-time addresses from an account-level xpub,
    or watches one fixed address (single-address mode). Never both keys and
    signing: only public material is accepted.
    """

    def __init__(
        self,
        *,
        xpub: str | None,
        address: str | None,
        source: ChainSource,
        decimals: int,
        confirmations: int,
        encoder: Callable[[bytes], str],
        validator: Callable[[str], bool],
        path_template: str | None = None,
    ) -> None:
        if not xpub and not address:
            raise ValueError("configure an account xpub (HD mode) and/or a single receive address")
        if confirmations < 0:
            raise ValueError("confirmations must be >= 0")
        self._xpub = ExtendedPublicKey.parse(xpub) if xpub else None
        if self._xpub is not None and path_template is None and self._xpub.depth != 3:
            raise ValueError(
                f"expected an ACCOUNT-level extended public key (depth 3, e.g. m/84'/0'/0'); "
                f"got depth {self._xpub.depth}. Pass path_template='...' if you really "
                "mean to derive from a different level."
            )
        self._path = path_template or DEFAULT_PATH
        self._encoder = encoder
        self._validator = validator
        if address is not None and not validator(address):
            raise ValueError(f"invalid address for this backend: {address!r}")
        self.single_address = address
        self.source = source
        self._decimals = decimals
        self._confirmations = confirmations

    def capabilities(self) -> Capabilities:
        modes = []
        if self._xpub is not None:
            modes.append(AddressMode.HD)
        if self.single_address:
            modes.append(AddressMode.SINGLE)
        return Capabilities(
            hd_derivation=self._xpub is not None,
            memo=False,
            decimals=self._decimals,
            default_confirmations=self._confirmations,
            address_modes=tuple(modes),
        )

    def derive_address(self, index: int) -> str:
        if self._xpub is None:
            raise UnsupportedCapability("no xpub configured; this backend is in single-address mode")
        if index < 0:
            raise ValueError("index must be >= 0")
        return self._encoder(self._xpub.derive(self._path.format(index=index)).public_key)

    def validate_address(self, address: str) -> bool:
        return self._validator(address)

    def get_incoming(self, address: str, not_before: datetime | None = None,
                     since_height: int | None = None) -> list[Transfer]:
        """``since_height`` is a scanning hint (only backends that scan block
        ranges, like ERC-20, use it); others ignore it."""
        if not self._validator(address):
            raise ValueError(f"invalid address for this backend: {address!r}")
        return self.source.incoming(address, not_before=not_before)
