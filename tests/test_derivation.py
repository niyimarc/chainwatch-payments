"""Address derivation vectors.

Vectors come from BIP32 test vector 1, BIP44/49/84 and the widely published
addresses for the "abandon ... about" mnemonic, and were cross-checked against
an independent private-key derivation when this suite was written.
"""

from __future__ import annotations

import pytest

from chainwatch_payments.backends import BitcoinBackend, ERC20Backend, TronTRC20Backend
from chainwatch_payments.crypto import ExtendedPublicKey, PrivateKeyRejected, btc_address
from chainwatch_payments.crypto.encoding import b58check_encode
from chainwatch_payments.crypto.hashes import (
    _keccak256_pure,
    _ripemd160_pure,
    keccak256,
    ripemd160,
)

from .conftest import ETH_XPUB, TRX_XPUB, VPUB_84_TEST, XPUB_44, YPUB_49, ZPUB_84

BTC_VECTORS = [
    (ZPUB_84, "mainnet", None, ["bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
                                "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g",
                                "bc1qp59yckz4ae5c4efgw2s5wfyvrz0ala7rgvuz8z"]),
    (VPUB_84_TEST, "testnet", None, ["tb1q6rz28mcfaxtmd6v789l9rrlrusdprr9pqcpvkl",
                                     "tb1qd7spv5q28348xl4myc8zmh983w5jx32cjhkn97",
                                     "tb1qxdyjf6h5d6qxap4n2dap97q4j5ps6ua8sll0ct"]),
    (XPUB_44, "mainnet", "p2pkh", ["1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA",
                                   "1Ak8PffB2meyfYnbXZR9EGfLfFZVpzJvQP",
                                   "1MNF5RSaabFwcbtJirJwKnDytsXXEsVsNb"]),
    (YPUB_49, "mainnet", None, ["37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf",
                                "3LtMnn87fqUeHBUG414p9CWwnoV6E2pNKS",
                                "3B4cvWGR8X6Xs8nvTxVUoMJV77E4f7oaia"]),
]


@pytest.mark.parametrize("xpub,network,script,expected", BTC_VECTORS)
def test_bitcoin_vectors(crypto_impl, http, xpub, network, script, expected):
    b = BitcoinBackend(xpub, network=network, script=script, http=http)
    assert [b.derive_address(i) for i in range(3)] == expected


def test_ethereum_vectors(crypto_impl, http):
    b = ERC20Backend(ETH_XPUB, asset="USDT", network="ethereum", http=http)
    assert [b.derive_address(i) for i in range(3)] == [
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
        "0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0",
        "0xb6716976A3ebe8D39aCEB04372f22Ff8e6802D7A",
    ]


def test_tron_vectors(crypto_impl, http):
    b = TronTRC20Backend(TRX_XPUB, network="mainnet", http=http)
    assert [b.derive_address(i) for i in range(3)] == [
        "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
        "TSeJkUh4Qv67VNFwY8LaAxERygNdy6NQZK",
        "TYJPRrdB5APNeRs4R7fYZSwW3TcrTKw2gx",
    ]


def test_bip32_vector1_public_child(crypto_impl):
    # BIP32 test vector 1: m/0H -> m/0H/1 is a non-hardened (public) step.
    parent = ExtendedPublicKey.parse(
        "xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK1VTsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw"
    )
    assert parent.child(1).serialize() == (
        "xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWMiGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ"
    )


def test_derivation_is_deterministic(http):
    b = BitcoinBackend(ZPUB_84, http=http)
    assert b.derive_address(1234) == b.derive_address(1234)
    assert len({b.derive_address(i) for i in range(20)}) == 20


def test_xpub_roundtrip():
    assert ExtendedPublicKey.parse(ZPUB_84).serialize() == ZPUB_84


def test_rejects_extended_private_key():
    raw = (bytes.fromhex("04B2430C") + b"\x03" + b"\x00" * 4 + b"\x80\x00\x00\x00"
           + b"\x11" * 32 + b"\x00" + b"\x22" * 32)
    zprv_like = b58check_encode(raw)
    with pytest.raises(PrivateKeyRejected):
        ExtendedPublicKey.parse(zprv_like)


def test_rejects_hardened_derivation():
    key = ExtendedPublicKey.parse(ZPUB_84)
    with pytest.raises(ValueError):
        key.derive("0'/1")
    with pytest.raises(ValueError):
        key.child(0x80000000)


def test_network_mismatch_rejected(http):
    with pytest.raises(ValueError, match="does not belong"):
        BitcoinBackend(ZPUB_84, network="testnet", http=http)
    with pytest.raises(ValueError, match="does not belong"):
        BitcoinBackend(VPUB_84_TEST, network="mainnet", http=http)


def test_non_account_level_xpub_rejected(http):
    chain_level = ExtendedPublicKey.parse(ZPUB_84).child(0).serialize()
    with pytest.raises(ValueError, match="ACCOUNT-level"):
        BitcoinBackend(chain_level, http=http)
    # explicit override is allowed
    b = BitcoinBackend(chain_level, http=http, path_template="{index}")
    assert b.derive_address(0) == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


def test_bad_checksum_rejected():
    with pytest.raises(ValueError):
        ExtendedPublicKey.parse(ZPUB_84[:-1] + ("t" if ZPUB_84[-1] != "t" else "u"))


@pytest.mark.parametrize("data", [b"", b"abc", b"a" * 135, b"a" * 136, b"x" * 1000])
def test_pure_hashes_match(data):
    assert _keccak256_pure(data) == keccak256(data)
    assert _ripemd160_pure(data) == ripemd160(data)


def test_hash_known_answers():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert _ripemd160_pure(b"abc").hex() == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"


def test_regtest_hrp():
    pub = ExtendedPublicKey.parse(VPUB_84_TEST).derive("0/0").public_key
    assert btc_address(pub, "regtest").startswith("bcrt1q")
