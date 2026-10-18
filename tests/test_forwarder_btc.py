"""BTC forwarder. Signatures are checked against an INDEPENDENT BIP143
implementation (below) and an independent ECDSA library (coincurve)."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

pytest.importorskip("embit")

from chainwatch_payments.forwarder.base import ForwardDeferred, ForwardError  # noqa: E402
from chainwatch_payments.forwarder.bitcoin import BitcoinForwarder, p2wpkh_sweep_vsize  # noqa: E402

from .conftest import ZPUB_84  # noqa: E402

M = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
DEST = "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"       # index 1 of the test seed (any valid address)


class FakeEsplora:
    def __init__(self, utxos=None, rate="5"):
        self._utxos = utxos or {}
        self.rate = Decimal(rate)
        self.broadcasts: list[str] = []
        self.states: dict[str, str] = {}

    def utxos(self, address):
        return self._utxos.get(address, [])

    def fee_rate(self, target):
        return self.rate

    def broadcast(self, raw_hex):
        self.broadcasts.append(raw_hex)
        return "f" * 64

    def tx_state(self, txid):
        return self.states.get(txid, "pending")


def make(client, **kw):
    args = dict(mnemonic=M, expected_xpub=ZPUB_84, destination=DEST, allowed_destinations=[DEST], client=client)
    args.update(kw)
    return BitcoinForwarder(**args)


# -- independent BIP143 sighash (P2WPKH, SIGHASH_ALL) ------------------------------------------

def _dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _varint(n):
    return bytes([n]) if n < 0xFD else b"\xfd" + n.to_bytes(2, "little")


def parse_segwit_tx(raw: bytes):
    i = 4
    version = raw[:4]
    assert raw[4:6] == b"\x00\x01"                      # segwit marker + flag
    i = 6
    n_in = raw[i]; i += 1                               # noqa: E702
    ins = []
    for _ in range(n_in):
        txid, vout = raw[i:i + 32], raw[i + 32:i + 36]; i += 36  # noqa: E702
        slen = raw[i]; i += 1 + slen                    # noqa: E702
        seq = raw[i:i + 4]; i += 4                      # noqa: E702
        ins.append((txid, vout, seq))
    n_out = raw[i]; i += 1                              # noqa: E702
    outs_start = i
    outs = []
    for _ in range(n_out):
        value = int.from_bytes(raw[i:i + 8], "little"); i += 8  # noqa: E702
        slen = raw[i]; spk = raw[i + 1:i + 1 + slen]; i += 1 + slen  # noqa: E702
        outs.append((value, spk))
    outs_raw = raw[outs_start:i]
    wits = []
    for _ in range(n_in):
        items = []
        n = raw[i]; i += 1                              # noqa: E702
        for _ in range(n):
            ln = raw[i]; items.append(raw[i + 1:i + 1 + ln]); i += 1 + ln  # noqa: E702
        wits.append(items)
    locktime = raw[i:i + 4]
    assert i + 4 == len(raw)
    return version, ins, outs, outs_raw, wits, locktime


def bip143_sighash(version, ins, outs_raw, locktime, idx, pubkey_hash, value):
    prevouts = b"".join(t + v for t, v, _ in ins)
    seqs = b"".join(s for _, _, s in ins)
    txid, vout, seq = ins[idx]
    script_code = b"\x19\x76\xa9\x14" + pubkey_hash + b"\x88\xac"
    pre = (version + _dsha(prevouts) + _dsha(seqs) + txid + vout + script_code
           + value.to_bytes(8, "little") + seq + _dsha(outs_raw) + locktime + (1).to_bytes(4, "little"))
    return _dsha(pre)


def hash160(b):
    from chainwatch_payments.crypto.hashes import hash160 as h
    return h(b)


def test_sweep_is_correctly_signed():
    coincurve = pytest.importorskip("coincurve")
    f0 = make(FakeEsplora())
    dep = f0.deposit_address(0)
    assert dep == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"   # matches the watch-only side
    utxos = [
        {"txid": "aa" * 32, "vout": 1, "value": 150_000, "status": {"confirmed": True}},
        {"txid": "bb" * 32, "vout": 0, "value": 50_000, "status": {"confirmed": True}},
        {"txid": "cc" * 32, "vout": 3, "value": 999_999, "status": {"confirmed": False}},  # ignored
    ]
    net = FakeEsplora({dep: utxos}, rate="5")
    r = make(net).forward(0, dep)
    assert r.status == "sent" and r.transfer_tx == "f" * 64

    raw = bytes.fromhex(net.broadcasts[0])
    version, ins, outs, outs_raw, wits, locktime = parse_segwit_tx(raw)
    fee = 5 * p2wpkh_sweep_vsize(2, 22)
    assert len(ins) == 2 and [t[::-1].hex() for t, _, _ in ins] == ["aa" * 32, "bb" * 32]
    assert all(s == bytes.fromhex("fdffffff") for _, _, s in ins)            # RBF signalled
    assert outs == [(200_000 - fee, bytes.fromhex("0014") + _decode_bech32(DEST))]
    assert (r.amount, r.fee) == (200_000 - fee, fee)

    values = [150_000, 50_000]
    for i, (sig, pub) in enumerate(wits):
        assert sig[-1] == 1                                                   # SIGHASH_ALL
        h = bip143_sighash(version, ins, outs_raw, locktime, i, hash160(pub), values[i])
        assert coincurve.PublicKey(pub).verify(sig[:-1], h, hasher=None)
        assert hash160(pub) == _decode_bech32(dep)                            # signed by the deposit key


def _decode_bech32(addr):
    from chainwatch_payments.crypto.encoding import bech32_decode_segwit
    return bech32_decode_segwit("bc", addr)[1]


def test_nothing_to_forward_and_too_small():
    f = make(FakeEsplora())
    dep = f.deposit_address(2)
    assert f.forward(2, dep).status == "nothing"
    tiny = FakeEsplora({dep: [{"txid": "aa" * 32, "vout": 0, "value": 9_000, "status": {"confirmed": True}}]})
    with pytest.raises(ForwardError, match="too small"):
        make(tiny).forward(2, dep)


def test_fee_cap_defers():
    f = make(FakeEsplora())
    dep = f.deposit_address(3)
    hot = FakeEsplora({dep: [{"txid": "aa" * 32, "vout": 0, "value": 500_000, "status": {"confirmed": True}}]},
                      rate="250")
    with pytest.raises(ForwardDeferred, match="above max_fee_rate"):
        make(hot, max_fee_rate="100").forward(3, dep)
    assert hot.broadcasts == []


def test_btc_safety_rails():
    with pytest.raises(ForwardError, match="does not match the xpub"):
        make(FakeEsplora(), account=1)
    with pytest.raises(ForwardError, match="invalid destination"):
        make(FakeEsplora(), destination="tb1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
             allowed_destinations=["tb1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"])
    f = make(FakeEsplora())
    with pytest.raises(ForwardError, match="does not match invoice address"):
        f.forward(5, DEST)


def test_tx_state_passthrough():
    net = FakeEsplora()
    net.states["abc"] = "confirmed"
    assert make(net).tx_state("abc") == "confirmed"


def test_esplora_spend_client(http):
    from chainwatch_payments import ChainSourceError
    from chainwatch_payments.forwarder.bitcoin import EsploraSpendClient

    posted = []
    http.post_text = lambda url, body, headers=None: posted.append((url, body)) or "ab" * 32 + "\n"
    base = "https://mempool.space/testnet4/api"
    http.on_get(f"{base}/address/tb1qxyz/utxo", [{"txid": "aa" * 32, "vout": 0, "value": 1}])
    http.on_get(f"{base}/v1/fees/recommended", {"halfHourFee": 3})
    http.on_get(f"{base}/tx/{'ab' * 32}/status", {"confirmed": True})

    def missing(params):
        raise ChainSourceError("GET .../status failed: 404 Not Found")

    http.on_get(f"{base}/tx/{'cd' * 32}/status", missing)
    c = EsploraSpendClient("testnet4", http=http)
    assert c.utxos("tb1qxyz")[0]["value"] == 1
    assert c.fee_rate("halfHourFee") == 3
    assert c.broadcast("0200") == "ab" * 32 and posted == [(f"{base}/tx", "0200")]
    assert c.tx_state("ab" * 32) == "confirmed" and c.tx_state("cd" * 32) == "dropped"


def test_refuses_to_forward_to_itself():
    f = make(FakeEsplora())
    own = f.deposit_address(1)
    g = make(FakeEsplora(), destination=own, allowed_destinations=[own])
    with pytest.raises(ForwardError, match="deposit address itself"):
        g.forward(1, own)
