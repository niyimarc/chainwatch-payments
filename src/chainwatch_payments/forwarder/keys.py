"""BIP39 seed + BIP32 private derivation, for the forwarder only.

Kept apart from ``chainwatch_payments.crypto`` (which is public-key only) so
that the watch-only core never contains private-key code paths.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass

from ..crypto import secp256k1
from ..crypto.addresses import tron_address
from ..crypto.bip32 import ExtendedPublicKey
from ..crypto.encoding import b58check_encode
from ..crypto.hashes import hash160

__all__ = ["mnemonic_to_seed", "PrivateNode", "account_node"]

HARDENED = 0x80000000
N = secp256k1.N


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """BIP39 seed (PBKDF2-HMAC-SHA512, 2048 rounds). The checksum is not
    verified here; the forwarder instead verifies the derived account xpub
    matches the one your watch-only backend uses, which catches any typo."""
    words = " ".join(unicodedata.normalize("NFKD", mnemonic).split())
    salt = "mnemonic" + unicodedata.normalize("NFKD", passphrase)
    return hashlib.pbkdf2_hmac("sha512", words.encode(), salt.encode(), 2048)


def _point(k: int) -> bytes:
    """Compressed public key for private scalar k."""
    if not 0 < k < N:
        raise ValueError("invalid private key")
    if secp256k1.using_coincurve():  # pragma: no cover - optional dependency
        return secp256k1._coincurve.PrivateKey(k.to_bytes(32, "big")).public_key.format(compressed=True)
    jac = secp256k1._jmul((secp256k1.G[0], secp256k1.G[1], 1), k)
    return secp256k1.compress(secp256k1._to_affine(jac))


@dataclass(frozen=True)
class PrivateNode:
    key: int
    chain_code: bytes
    depth: int = 0
    parent_fingerprint: bytes = b"\x00" * 4
    child_number: int = 0

    def __repr__(self) -> str:  # never print key material
        return f"PrivateNode(depth={self.depth}, child={self.child_number})"

    @classmethod
    def from_seed(cls, seed: bytes) -> PrivateNode:
        i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        k = int.from_bytes(i[:32], "big")
        if not 0 < k < N:  # pragma: no cover - probability ~2^-127
            raise ValueError("invalid master key")
        return cls(k, i[32:])

    @property
    def public_key(self) -> bytes:
        return _point(self.key)

    @property
    def private_key_bytes(self) -> bytes:
        return self.key.to_bytes(32, "big")

    def child(self, index: int) -> PrivateNode:
        if index >= HARDENED:
            data = b"\x00" + self.private_key_bytes + index.to_bytes(4, "big")
        else:
            data = self.public_key + index.to_bytes(4, "big")
        i = hmac.new(self.chain_code, data, hashlib.sha512).digest()
        il = int.from_bytes(i[:32], "big")
        k = (il + self.key) % N
        if il >= N or k == 0:  # pragma: no cover - probability ~2^-127
            return self.child(index + 1)
        return PrivateNode(k, i[32:], self.depth + 1, hash160(self.public_key)[:4], index)

    def derive(self, path: str) -> PrivateNode:
        node = self
        p = path.strip()
        if p.startswith(("m/", "M/")):
            p = p[2:]
        for seg in filter(None, p.split("/")):
            hard = seg.endswith(("'", "h", "H"))
            idx = int(seg.rstrip("'hH")) + (HARDENED if hard else 0)
            node = node.child(idx)
        return node

    def xpub(self, version: bytes = bytes.fromhex("0488B21E")) -> str:
        """The matching extended PUBLIC key (what the watch-only side uses)."""
        return b58check_encode(version + bytes([self.depth]) + self.parent_fingerprint
                               + self.child_number.to_bytes(4, "big") + self.chain_code + self.public_key)

    def tron_address(self) -> str:
        return tron_address(self.public_key)


def account_node(seed: bytes, coin_type: int = 195, account: int = 0) -> PrivateNode:
    """``m/44'/<coin>'/<account>'`` (TRON: coin 195)."""
    return PrivateNode.from_seed(seed).derive(f"m/44'/{coin_type}'/{account}'")


def same_account(node: PrivateNode, xpub: str) -> bool:
    """True if ``xpub`` is the public side of ``node`` (version bytes ignored)."""
    other = ExtendedPublicKey.parse(xpub)
    return other.public_key == node.public_key and other.chain_code == node.chain_code
