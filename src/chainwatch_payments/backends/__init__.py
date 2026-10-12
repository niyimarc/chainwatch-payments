"""Reference backends.

Shipped backends import only the standard library at import time; their
network transport (httpx) is loaded lazily, so importing this module never
fails because an extra is missing.
"""

from ._common import ChainSource, HDBackend
from .bitcoin import BitcoinBackend, EsploraSource
from .evm import ERC20Backend, JsonRpcSource
from .mock import MockBackend
from .tron import TronGridSource, TronTRC20Backend

__all__ = [
    "ChainSource",
    "HDBackend",
    "MockBackend",
    "BitcoinBackend",
    "EsploraSource",
    "TronTRC20Backend",
    "TronGridSource",
    "ERC20Backend",
    "JsonRpcSource",
]
