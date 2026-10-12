"""Turn a compressed secp256k1 public key into chain addresses."""

from __future__ import annotations

from . import secp256k1
from .encoding import (
    b58check_decode,
    b58check_encode,
    bech32_decode_segwit,
    bech32_encode_segwit,
    eip55_checksum,
)
from .hashes import hash160, keccak256

__all__ = [
    "BTC_PARAMS",
    "btc_address",
    "is_valid_btc_address",
    "evm_address",
    "is_valid_evm_address",
    "tron_address",
    "tron_address_to_hex",
    "is_valid_tron_address",
]

# network -> (p2pkh version, p2sh version, bech32 hrp)
BTC_PARAMS: dict[str, tuple[int, int, str]] = {
    "mainnet": (0x00, 0x05, "bc"),
    "testnet": (0x6F, 0xC4, "tb"),
    "testnet4": (0x6F, 0xC4, "tb"),
    "signet": (0x6F, 0xC4, "tb"),
    "regtest": (0x6F, 0xC4, "bcrt"),
}


def btc_address(pubkey: bytes, network: str = "mainnet", script: str = "p2wpkh") -> str:
    p2pkh_v, p2sh_v, hrp = BTC_PARAMS[network]
    h = hash160(pubkey)
    if script == "p2wpkh":
        return bech32_encode_segwit(hrp, 0, h)
    if script == "p2pkh":
        return b58check_encode(bytes([p2pkh_v]) + h)
    if script == "p2sh-p2wpkh":
        redeem = b"\x00\x14" + h
        return b58check_encode(bytes([p2sh_v]) + hash160(redeem))
    raise ValueError(f"unsupported script type {script!r}")


def is_valid_btc_address(addr: str, network: str = "mainnet") -> bool:
    p2pkh_v, p2sh_v, hrp = BTC_PARAMS[network]
    try:
        if addr.lower().startswith(hrp + "1"):
            witver, prog = bech32_decode_segwit(hrp, addr)
            return 0 <= witver <= 16 and 2 <= len(prog) <= 40
        payload = b58check_decode(addr)
        return len(payload) == 21 and payload[0] in (p2pkh_v, p2sh_v)
    except ValueError:
        return False


def _eth_hash20(pubkey: bytes) -> bytes:
    raw = secp256k1.uncompressed(secp256k1.decompress(pubkey))[1:]
    return keccak256(raw)[-20:]


def evm_address(pubkey: bytes) -> str:
    return eip55_checksum(_eth_hash20(pubkey))


def is_valid_evm_address(addr: str) -> bool:
    if not (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42):
        return False
    try:
        raw = bytes.fromhex(addr[2:])
    except ValueError:
        return False
    body = addr[2:]
    if body.lower() == body or body.upper() == body:
        return True
    return eip55_checksum(raw) == addr


def tron_address(pubkey: bytes) -> str:
    return b58check_encode(b"\x41" + _eth_hash20(pubkey))


def tron_address_to_hex(addr: str) -> str:
    """Base58 TRON address -> 20-byte hex (no 0x, no 41 prefix), lowercase."""
    payload = b58check_decode(addr)
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError("not a TRON address")
    return payload[1:].hex()


def is_valid_tron_address(addr: str) -> bool:
    try:
        tron_address_to_hex(addr)
        return True
    except ValueError:
        return False
