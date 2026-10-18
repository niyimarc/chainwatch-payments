"""The Backend protocol every (asset, network) implementation follows."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Protocol, runtime_checkable

from .errors import UnsupportedCapability
from .types import Capabilities, Transfer

__all__ = ["Backend", "BaseBackend"]


@runtime_checkable
class Backend(Protocol):
    """What the registry needs from a chain backend.

    Implementations must be watch-only: they derive or hold PUBLIC
    information only and never sign or send anything.

    Optional extras a backend MAY offer (the registry uses them only when present):

    * ``current_height() -> int``: chain height, recorded when an address is
      handed out;
    * ``get_incoming(..., since_height=...)``: accept that height back as a hint
      to scan only newer blocks (useful for log-scanning chains like ERC-20).
      The registry passes ``since_height`` only when the caller supplies one.
    """

    def capabilities(self) -> Capabilities: ...

    def derive_address(self, index: int) -> str: ...

    def get_incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]: ...


class BaseBackend(ABC):
    """Optional convenience base class.

    Subclasses implement ``capabilities``, ``derive_address`` and
    ``get_incoming``. Override ``address_with_memo`` for memo-capable chains
    and set ``single_address`` for single-address mode.
    """

    single_address: str | None = None

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def derive_address(self, index: int) -> str: ...

    @abstractmethod
    def get_incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]: ...

    def receive_address(self) -> str:
        """The fixed address in single-address mode."""
        if not self.single_address:
            raise UnsupportedCapability(f"{type(self).__name__} has no single address configured")
        return self.single_address

    def address_with_memo(self, reference: str) -> tuple[str, str]:
        raise UnsupportedCapability(f"{type(self).__name__} does not support memos/tags")

    def validate_address(self, address: str) -> bool:  # pragma: no cover - trivial default
        return bool(address)
