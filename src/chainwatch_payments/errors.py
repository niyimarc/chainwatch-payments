"""Exception hierarchy."""

from __future__ import annotations

from .crypto.bip32 import PrivateKeyRejected

__all__ = [
    "ChainwatchError",
    "BackendNotFound",
    "BackendAlreadyRegistered",
    "UnsupportedCapability",
    "ChainSourceError",
    "MissingDependency",
    "PrivateKeyRejected",
]


class ChainwatchError(Exception):
    """Base class for all package errors."""


class BackendNotFound(ChainwatchError, KeyError):
    def __str__(self) -> str:  # KeyError quotes its message otherwise
        return str(self.args[0]) if self.args else "backend not found"


class BackendAlreadyRegistered(ChainwatchError, ValueError):
    pass


class UnsupportedCapability(ChainwatchError, NotImplementedError):
    pass


class ChainSourceError(ChainwatchError, RuntimeError):
    """The node/explorer could not be read. Never means 'no payment'."""


class MissingDependency(ChainwatchError, ImportError):
    pass
