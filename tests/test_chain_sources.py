"""Shipped backends against mocked chain access (no network, ever)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chainwatch_payments import ChainSourceError, Match, PaymentRegistry, State
from chainwatch_payments.backends import BitcoinBackend, ERC20Backend, TronTRC20Backend
from chainwatch_payments.backends.evm import TRANSFER_TOPIC

from .conftest import ETH_XPUB, TRX_XPUB, VPUB_84_TEST, ZPUB_84

ESPLORA = "https://blockstream.info/testnet/api"


# -- Bitcoin / Esplora --------------------------------------------------------------

def esplora_tx(txid, outputs, height=None, block_time=None):
    status = {"confirmed": height is not None}
    if height is not None:
        status.update(block_height=height, block_time=block_time or 1_700_000_000 + height)
    return {"txid": txid, "status": status,
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in outputs]}


@pytest.fixture
def btc(http):
    b = BitcoinBackend(VPUB_84_TEST, network="testnet", http=http)
    reg = PaymentRegistry()
    reg.register("BTC", "testnet", b)
    return b, reg


def test_esplora_lifecycle_and_reorg(btc, http):
    b, reg = btc
    addr = b.derive_address(0)
    other = b.derive_address(1)
    http.on_get(f"{ESPLORA}/blocks/tip/height", 100)
    http.on_get(f"{ESPLORA}/address/{addr}/txs", [])
    assert reg.confirm("BTC", "testnet", addr, "0.0005").state is State.NONE

    # mempool: pays our address (vout 1) plus change elsewhere (vout 0)
    tx = esplora_tx("t1", [(other, 999), (addr, 50_000)])
    http.on_get(f"{ESPLORA}/address/{addr}/txs", [tx])
    s = reg.confirm("BTC", "testnet", addr, "0.0005")
    assert (s.state, s.match, s.received_amount) == (State.SEEN, Match.EXACT, 50_000)

    http.on_get(f"{ESPLORA}/address/{addr}/txs", [esplora_tx("t1", [(other, 999), (addr, 50_000)], height=99)])
    confirmed = reg.confirm("BTC", "testnet", addr, "0.0005")
    assert confirmed.state is State.CONFIRMED and confirmed.confirmations == 2
    assert confirmed.transfers[0].index == 1

    # reorg: tx disappears entirely
    http.on_get(f"{ESPLORA}/address/{addr}/txs", [])
    after = reg.confirm("BTC", "testnet", addr, "0.0005", previous=confirmed)
    assert after.state is State.NONE and after.reverted


def test_esplora_pagination(btc, http):
    b, reg = btc
    addr = b.derive_address(0)
    http.on_get(f"{ESPLORA}/blocks/tip/height", "1000\n")       # text body
    first = [esplora_tx(f"a{i:02d}", [(addr, 1)], height=900 - i) for i in range(25)]
    second = [esplora_tx("b00", [(addr, 1000)], height=800)]
    http.on_get(f"{ESPLORA}/address/{addr}/txs", first)
    http.on_get(f"{ESPLORA}/address/{addr}/txs/chain/a24", second)
    s = reg.confirm("BTC", "testnet", addr, Decimal("0.00001025"))
    assert s.state is State.CONFIRMED and len(s.tx_ids) == 26


def test_esplora_pagination_stops_at_not_before(btc, http):
    b, reg = btc
    addr = b.derive_address(0)
    http.on_get(f"{ESPLORA}/blocks/tip/height", 1000)
    page = [esplora_tx(f"a{i:02d}", [(addr, 1)], height=900 - i, block_time=1_000 - i) for i in range(25)]
    http.on_get(f"{ESPLORA}/address/{addr}/txs", page)
    nb = datetime.fromtimestamp(990, tz=timezone.utc)
    s = reg.confirm("BTC", "testnet", addr, Decimal("0.00000011"), not_before=nb)
    assert len(s.tx_ids) == 11          # block_time 990..1000
    assert not any("/txs/chain/" in c[1] for c in http.calls)


def test_source_failure_is_an_error_not_none(btc):
    b, reg = btc

    class Down:
        def incoming(self, address, not_before=None):
            raise ChainSourceError("explorer down")

    b.source = Down()
    with pytest.raises(ChainSourceError):
        reg.confirm("BTC", "testnet", b.derive_address(0), "0.001")


def test_invalid_address_rejected(btc):
    b, reg = btc
    with pytest.raises(ValueError):
        reg.confirm("BTC", "testnet", "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "0.001")  # mainnet addr


def test_single_address_mode_bitcoin(http):
    addr = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    b = BitcoinBackend(address=addr, http=http)
    assert not b.capabilities().hd_derivation
    reg = PaymentRegistry()
    reg.register("BTC", "mainnet", b)
    assert reg.receive_address("BTC", "mainnet") == addr


# -- TRON / TronGrid ----------------------------------------------------------------

TRONGRID = "https://api.trongrid.io"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def trc20_row(txid, to, value, contract=USDT_TRON):
    return {"transaction_id": txid, "to": to, "from": "TXsender", "type": "Transfer", "value": str(value),
            "block_timestamp": 1_760_000_000_000, "token_info": {"address": contract, "decimals": 6}}


@pytest.fixture
def tron(http):
    b = TronTRC20Backend(TRX_XPUB, http=http, api_key="k")
    reg = PaymentRegistry()
    reg.register("USDT", "tron", b)
    http.on_post(f"{TRONGRID}/wallet/getnowblock", {"block_header": {"raw_data": {"number": 5000}}})
    return b, reg


def test_trongrid_confirmations_and_failures(tron, http):
    b, reg = tron
    addr = b.derive_address(0)
    rows = [
        trc20_row("ok", addr, 10_000_000),
        trc20_row("failed", addr, 10_000_000),
        trc20_row("fake-token", addr, 10_000_000, contract="TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"),
        trc20_row("pending", addr, 5_000_000),
    ]
    http.on_get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20", {"data": rows, "meta": {}})
    infos = {
        "ok": {"blockNumber": 4990, "blockTimeStamp": 1_760_000_000_000, "receipt": {"result": "SUCCESS"}},
        "failed": {"blockNumber": 4990, "receipt": {"result": "REVERT"}},
        "pending": {},
    }
    http.on_post(f"{TRONGRID}/wallet/gettransactioninfobyid", lambda p: infos[p["value"]])

    s = reg.confirm("USDT", "tron", addr, "10")
    assert s.state is State.SEEN                   # 11 confs < 19
    assert s.received == Decimal("15") and s.match is Match.OVER
    assert set(s.tx_ids) == {"ok", "pending"}
    s = reg.confirm("USDT", "tron", addr, "10", required_confirmations=11)
    assert s.state is State.CONFIRMED and s.tx_ids == ("ok",) and s.match is Match.EXACT

    params = [c[2] for c in http.calls if c[0] == "GET"][0]
    assert params["only_to"] == "true" and params["contract_address"] == USDT_TRON


def test_trongrid_pagination_and_not_before(tron, http):
    b, reg = tron
    addr = b.derive_address(1)

    def listing(params):
        assert params["min_timestamp"] == 1_759_999_000_000
        if "fingerprint" not in params:
            return {"data": [trc20_row("p1", addr, 1_000_000)], "meta": {"fingerprint": "next"}}
        return {"data": [trc20_row("p2", addr, 1_000_000)], "meta": {}}

    http.on_get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20", listing)
    http.on_post(f"{TRONGRID}/wallet/gettransactioninfobyid",
                 {"blockNumber": 4000, "blockTimeStamp": 1_760_000_000_000, "receipt": {"result": "SUCCESS"}})
    nb = datetime.fromtimestamp(1_759_999_000, tz=timezone.utc)
    s = reg.confirm("USDT", "tron", addr, "2", not_before=nb)
    assert s.state is State.CONFIRMED and set(s.tx_ids) == {"p1", "p2"}


def test_trongrid_error_raises(tron, http):
    b, reg = tron
    addr = b.derive_address(0)
    http.on_get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20", {"success": False, "error": "rate limited"})
    with pytest.raises(ChainSourceError):
        reg.confirm("USDT", "tron", addr, "1")


def test_tron_requires_contract_for_unknown_token(http):
    with pytest.raises(ValueError, match="pass contract"):
        TronTRC20Backend(TRX_XPUB, asset="USDD", http=http)


# -- EVM / JSON-RPC ---------------------------------------------------------------------

RPC = "https://rpc.example"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


class FakeRpc:
    def __init__(self, head, logs, block_ts=None):
        self.head, self.logs, self.block_ts = head, logs, block_ts or {}
        self.get_logs_ranges: list[tuple[int, int]] = []

    def __call__(self, payload):
        m, p = payload["method"], payload["params"]
        if m == "eth_blockNumber":
            return {"result": hex(self.head)}
        if m == "eth_getLogs":
            f = p[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            self.get_logs_ranges.append((lo, hi))
            return {"result": [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi
                               and lg["topics"][2] == f["topics"][2]]}
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            return {"result": {"timestamp": hex(self.block_ts.get(n, 1_700_000_000 + n * 12))}}
        return {"error": {"code": -32601, "message": m}}


def erc20_log(to, value, block, tx="0xabc", log_index=0, removed=False, contract=USDC):
    return {"address": contract.lower(), "blockNumber": hex(block), "transactionHash": tx,
            "logIndex": hex(log_index), "removed": removed, "data": hex(value),
            "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + to.lower()[2:]]}


def make_evm(http, rpc, **kw):
    http.on_post(RPC, rpc)
    b = ERC20Backend(ETH_XPUB, asset="USDC", rpc_url=RPC, http=http, **kw)
    reg = PaymentRegistry()
    reg.register("USDC", "ethereum", b)
    return b, reg


def test_evm_logs_confirmations_and_removed(http):
    addr = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    rpc = FakeRpc(head=10_000, logs=[
        erc20_log(addr, 30_000_000, 9_995, tx="0x1"),
        erc20_log(addr, 30_000_000, 9_996, tx="0x2", removed=True),          # reorged out
        erc20_log(addr, 30_000_000, 9_996, tx="0x3", contract="0x" + "22" * 20),  # other token
    ])
    b, reg = make_evm(http, rpc)
    assert b.derive_address(0) == addr
    s = reg.confirm("USDC", "ethereum", addr, "30")
    assert s.state is State.SEEN and s.confirmations == 6 and s.tx_ids == ("0x1",)
    rpc.head = 10_006
    assert reg.confirm("USDC", "ethereum", addr, "30").state is State.CONFIRMED


def test_evm_chunking_and_lookback(http):
    addr = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    rpc = FakeRpc(head=10_000, logs=[])
    _, reg = make_evm(http, rpc, lookback_blocks=5_000, chunk_size=2_000)
    reg.confirm("USDC", "ethereum", addr, "1")
    assert rpc.get_logs_ranges == [(5_001, 7_000), (7_001, 9_000), (9_001, 10_000)]


def test_evm_not_before_narrows_range_and_filters(http):
    addr = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    rpc = FakeRpc(head=10_000, logs=[
        erc20_log(addr, 5_000_000, 9_900, tx="0xold"),
        erc20_log(addr, 5_000_000, 9_980, tx="0xnew"),
    ])
    _, reg = make_evm(http, rpc)
    head_ts = 1_700_000_000 + 10_000 * 12
    nb = datetime.fromtimestamp(head_ts - 60 * 12, tz=timezone.utc)   # ~60 blocks ago
    s = reg.confirm("USDC", "ethereum", addr, "5", not_before=nb)
    assert s.tx_ids == ("0xnew",) and s.state is State.CONFIRMED
    lo = rpc.get_logs_ranges[0][0]
    assert 9_000 < lo <= 9_941            # estimated window, not the full lookback


def test_evm_rpc_error_raises(http):
    http.on_post(RPC, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit"}})
    b = ERC20Backend(ETH_XPUB, asset="USDC", rpc_url=RPC, http=http)
    with pytest.raises(ChainSourceError):
        b.get_incoming(b.derive_address(0))


def test_evm_presets_and_custom_token(http):
    assert ERC20Backend(ETH_XPUB, asset="USDT", http=http).contract == "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    sep = ERC20Backend(ETH_XPUB, asset="USDC", network="sepolia", http=http)
    assert sep.contract == "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    assert sep.capabilities().decimals == 6
    with pytest.raises(ValueError, match="pass contract"):
        ERC20Backend(ETH_XPUB, asset="DAI", http=http)
    custom = ERC20Backend(ETH_XPUB, asset="DAI", network="polygon", rpc_url=RPC,
                          contract="0x" + "33" * 20, decimals=18, http=http)
    assert custom.capabilities().decimals == 18
    with pytest.raises(ValueError, match="invalid ERC-20 contract"):
        ERC20Backend(ETH_XPUB, asset="X", contract="0x123", decimals=6, rpc_url=RPC, http=http)


def test_btc_and_erc20_share_nothing(http):
    """Same registry, BTC and ERC-20 reads go to their own sources."""
    addr_eth = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    rpc = FakeRpc(head=100, logs=[erc20_log(addr_eth, 1_000_000, 50)])
    http.on_post(RPC, rpc)
    reg = PaymentRegistry()
    btc = BitcoinBackend(ZPUB_84, http=http)
    reg.register("BTC", "mainnet", btc)
    reg.register("USDC", "ethereum", ERC20Backend(ETH_XPUB, asset="USDC", rpc_url=RPC, http=http))
    btc_addr = reg.derive_address("BTC", "mainnet", 0)
    http.on_get("https://blockstream.info/api/blocks/tip/height", 10)
    http.on_get(f"https://blockstream.info/api/address/{btc_addr}/txs", [])
    assert reg.confirm("USDC", "ethereum", addr_eth, "1").state is State.CONFIRMED
    assert reg.confirm("BTC", "mainnet", btc_addr, "1").state is State.NONE


def test_evm_since_height_scans_only_new_blocks(http):
    addr = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    rpc = FakeRpc(head=10_000, logs=[erc20_log(addr, 1_000_000, 9_990)])
    b, reg = make_evm(http, rpc, chunk_size=2_000)
    assert reg.current_height("USDC", "ethereum") == 10_000
    s = reg.confirm("USDC", "ethereum", addr, "1", since_height=9_950)
    assert rpc.get_logs_ranges == [(9_950, 10_000)]          # one call, not ten
    assert s.state is State.SEEN and s.tx_ids == ("0xabc",)


def test_since_height_is_ignored_by_non_scanning_backends(btc, http):
    b, reg = btc
    addr = b.derive_address(0)
    http.on_get(f"{ESPLORA}/blocks/tip/height", 100)
    http.on_get(f"{ESPLORA}/address/{addr}/txs", [])
    assert reg.current_height("BTC", "testnet") is None
    assert reg.confirm("BTC", "testnet", addr, "0.001", since_height=50).state is State.NONE


def test_trongrid_deep_transfers_skip_per_tx_lookups(http):
    b = TronTRC20Backend(TRX_XPUB, http=http)
    addr = b.derive_address(5)
    head_ts = 1_792_352_913_000
    http.on_post(f"{TRONGRID}/wallet/getnowblock",
                 {"block_header": {"raw_data": {"number": 87_226_633, "timestamp": head_ts}}})
    old = trc20_row("old", addr, 7_000_000)
    old["block_timestamp"] = head_ts - 3_600_000            # an hour ago: ~1200 blocks deep
    new = trc20_row("new", addr, 3_000_000)
    new["block_timestamp"] = head_ts - 30_000               # 30 s ago: needs the exact lookup
    http.on_get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20", {"data": [new, old], "meta": {}})
    looked_up = []
    http.on_post(f"{TRONGRID}/wallet/gettransactioninfobyid",
                 lambda p: looked_up.append(p["value"]) or {"blockNumber": 87_226_624, "receipt": {"result": "SUCCESS"}})
    transfers = {t.tx_id: t for t in b.get_incoming(addr)}
    assert looked_up == ["new"]
    assert transfers["old"].confirmations == 1201 and transfers["old"].block_height == 87_226_633 - 1200
    assert transfers["new"].confirmations == 10
