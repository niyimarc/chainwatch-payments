"""BIP32 extended PUBLIC keys and non-hardened public child derivation.

Watch-only by construction: extended private keys (xprv/yprv/zprv/tprv/...)
are rejected on parse, and only non-hardened (public) derivation exists.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from . import secp256k1
from .encoding import b58check_decode, b58check_encode
from .hashes import hash160

__all__ = ["ExtendedPublicKey", "PUBLIC_VERSIONS", "PrivateKeyRejected"]

HARDENED = 0x80000000

# version bytes -> (prefix, is_testnet, script hint)
PUBLIC_VERSIONS: dict[bytes, tuple[str, bool, str]] = {
    bytes.fromhex("0488B21E"): ("xpub", False, "p2pkh"),
    bytes.fromhex("049D7CB2"): ("ypub", False, "p2sh-p2wpkh"),
    bytes.fromhex("04B24746"): ("zpub", False, "p2wpkh"),
    bytes.fromhex("043587CF"): ("tpub", True, "p2pkh"),
    bytes.fromhex("044A5262"): ("upub", True, "p2sh-p2wpkh"),
    bytes.fromhex("045F1CF6"): ("vpub", True, "p2wpkh"),
}
PRIVATE_VERSIONS = {
    bytes.fromhex(v) for v in (
        "0488ADE4", "049D7878", "04B2430C", "04358394", "044A4E28", "045F18BC",
    )
}


class PrivateKeyRejected(ValueError):
    """Raised when an extended PRIVATE key is supplied. This package is watch-only."""


@dataclass(frozen=True)
class ExtendedPublicKey:
    version: bytes
    depth: int
    parent_fingerprint: bytes
    child_number: int
    chain_code: bytes
    public_key: bytes  # 33-byte compressed

    # -- parsing / serialisation -------------------------------------------
    @classmethod
    def parse(cls, s: str) -> "ExtendedPublicKey":
        s = s.strip()
        raw = b58check_decode(s)
        if len(raw) != 78:
            raise ValueError("extended key must decode to 78 bytes")
        version = raw[:4]
        if version in PRIVATE_VERSIONS or raw[45] == 0:
            raise PrivateKeyRejected(
                "an extended PRIVATE key was supplied; chainwatch_payments is "
                "watch-only and accepts extended PUBLIC keys only"
            )
        if version not in PUBLIC_VERSIONS:
            raise ValueError(f"unknown extended public key version {version.hex()}")
        key = raw[45:78]
        secp256k1.decompress(key)  # validates the point
        return cls(
            version=version,
            depth=raw[4],
            parent_fingerprint=raw[5:9],
            child_number=int.from_bytes(raw[9:13], "big"),
            chain_code=raw[13:45],
            public_key=key,
        )

    def serialize(self) -> str:
        return b58check_encode(
            self.version
            + bytes([self.depth])
            + self.parent_fingerprint
            + self.child_number.to_bytes(4, "big")
            + self.chain_code
            + self.public_key
        )

    @property
    def prefix(self) -> str:
        return PUBLIC_VERSIONS[self.version][0]

    @property
    def is_testnet(self) -> bool:
        return PUBLIC_VERSIONS[self.version][1]

    @property
    def script_hint(self) -> str:
        return PUBLIC_VERSIONS[self.version][2]

    @property
    def fingerprint(self) -> bytes:
        return hash160(self.public_key)[:4]

    # -- derivation ----------------------------------------------------------
    def child(self, index: int) -> "ExtendedPublicKey":
        if not 0 <= index < HARDENED:
            raise ValueError(
                "only non-hardened indexes (0 .. 2^31-1) can be derived from a public key"
            )
        if self.depth >= 255:
            raise ValueError("maximum derivation depth reached")
        digest = hmac.new(
            self.chain_code, self.public_key + index.to_bytes(4, "big"), hashlib.sha512
        ).digest()
        tweak = int.from_bytes(digest[:32], "big")
        if tweak >= secp256k1.N:  # pragma: no cover - probability ~2^-127
            return self.child(index + 1)
        try:
            child_key = secp256k1.tweak_add(self.public_key, tweak)
        except ValueError:  # pragma: no cover - point at infinity, ~2^-127
            return self.child(index + 1)
        return ExtendedPublicKey(
            version=self.version,
            depth=self.depth + 1,
            parent_fingerprint=self.fingerprint,
            child_number=index,
            chain_code=digest[32:],
            public_key=child_key,
        )

    def derive(self, path: str | list[int]) -> "ExtendedPublicKey":
        """Derive a relative non-hardened path such as ``"0/5"`` or ``[0, 5]``."""
        if isinstance(path, str):
            p = path.strip()
            if p in ("", "m", "M"):
                return self
            if p.startswith(("m/", "M/")):
                p = p[2:]
            parts = []
            for seg in p.split("/"):
                if seg.endswith(("'", "h", "H")):
                    raise ValueError("hardened derivation needs a private key; not supported")
                parts.append(int(seg))
        else:
            parts = list(path)
        key = self
        for i in parts:
            key = key.child(i)
        return key
