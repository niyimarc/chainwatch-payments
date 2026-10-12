"""Address encodings: Base58Check, Bech32/Bech32m (BIP173/BIP350), EIP-55."""

from __future__ import annotations

from .hashes import keccak256, sha256

__all__ = [
    "b58encode", "b58decode", "b58check_encode", "b58check_decode",
    "bech32_encode_segwit", "bech32_decode_segwit", "eip55_checksum",
]

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58[r])
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(out))


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_INDEX:
            raise ValueError(f"invalid base58 character {c!r}")
        n = n * 58 + _B58_INDEX[c]
    pad = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def b58check_encode(payload: bytes) -> str:
    return b58encode(payload + sha256(sha256(payload))[:4])


def b58check_decode(s: str) -> bytes:
    raw = b58decode(s)
    if len(raw) < 5:
        raise ValueError("base58check string too short")
    payload, check = raw[:-4], raw[-4:]
    if sha256(sha256(payload))[:4] != check:
        raise ValueError("base58check checksum mismatch")
    return payload


# ---- Bech32 / Bech32m -----------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data: bytes | list[int], frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise ValueError("invalid value for convertbits")
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("invalid padding")
    return ret


def bech32_encode_segwit(hrp: str, witver: int, program: bytes) -> str:
    const = _BECH32_CONST if witver == 0 else _BECH32M_CONST
    data = [witver] + _convertbits(program, 8, 5)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def bech32_decode_segwit(hrp: str, addr: str) -> tuple[int, bytes]:
    if addr.lower() != addr and addr.upper() != addr:
        raise ValueError("mixed case bech32")
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or addr[:pos] != hrp or pos + 7 > len(addr):
        raise ValueError("invalid bech32 hrp/separator")
    try:
        data = [_CHARSET.index(c) for c in addr[pos + 1:]]
    except ValueError as exc:
        raise ValueError("invalid bech32 character") from exc
    const = _polymod(_hrp_expand(hrp) + data)
    witver = data[0]
    expected = _BECH32_CONST if witver == 0 else _BECH32M_CONST
    if const != expected:
        raise ValueError("bech32 checksum mismatch")
    program = bytes(_convertbits(data[1:-6], 5, 8, pad=False))
    return witver, program


def eip55_checksum(addr20: bytes) -> str:
    hexaddr = addr20.hex()
    digest = keccak256(hexaddr.encode()).hex()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c
        for i, c in enumerate(hexaddr)
    )
