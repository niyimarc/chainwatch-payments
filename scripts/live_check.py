"""Read-only check of chainwatch_payments against the REAL public services.

Sends nothing, needs no keys or funds. Run it from the project folder:

    pip install -e ".[all]"
    python scripts/live_check.py

TronGrid rate-limits requests without an API key. If the TRON mainnet check
fails with 429, get a free key at trongrid.io and run it again with:

    set TRONGRID_API_KEY=your-key        (Windows)
    export TRONGRID_API_KEY=your-key     (macOS/Linux)

Each check prints PASS or FAIL. A FAIL usually means an API changed, a
service is down or rate-limiting, or your network blocks it; re-run once
before worrying.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chainwatch_payments import (  # noqa: E402
    CoinGeckoProvider,
    CrossCheckedPriceProvider,
    KrakenProvider,
    PaymentRegistry,
    State,
)
from chainwatch_payments.backends import BitcoinBackend, ERC20Backend, TronTRC20Backend  # noqa: E402
from chainwatch_payments.backends.bitcoin import EsploraSource  # noqa: E402
from chainwatch_payments.backends.evm import TRANSFER_TOPIC, JsonRpcSource  # noqa: E402
from chainwatch_payments.backends.tron import TRC20_TOKENS, TronGridSource  # noqa: E402
from chainwatch_payments.crypto.encoding import b58check_encode  # noqa: E402
from chainwatch_payments.fees import MempoolSpaceFeeEstimator, TronEnergyFeeEstimator  # noqa: E402
from chainwatch_payments.http import HttpxClient  # noqa: E402

# Public test-vector account (the "abandon ... about" mnemonic). Strangers send
# coins to its first address, so it has real history to read. Never use it.
ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
TRX_XPUB = "xpub6D1AabNHCupeiLM65ZR9UStMhJ1vCpyV4XbZdyhMZBiJXALQtmn9p42VTQckoHVn8WNqS7dqnJokZHAHcHGoaQgmv8D45oNUKx6DZMNZBCd"
ETH_XPUB = "xpub6DCoCpSuQZB2jawqnGMEPS63ePKWkwWPH4TU45Q7LPXWuNd8TMtVxRrgjtEshuqpK3mdhaWHPFsBngh5GFZaM6si3yZdUsT8ddYM3PwnATt"

http = HttpxClient(timeout=20)
results: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        try:
            detail = fn() or ""
            results.append((name, True, str(detail)))
        except Exception as exc:  # report and keep going
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
            if "-v" in sys.argv:
                traceback.print_exc()
        return fn
    return wrap


@check("BTC mainnet: Esplora history of a known address")
def _():
    b = BitcoinBackend(ZPUB, network="mainnet", http=http)
    addr = b.derive_address(0)
    assert addr == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    transfers = b.get_incoming(addr)
    assert transfers, "expected some history on this public test address"
    confirmed = [t for t in transfers if t.confirmations > 0]
    assert confirmed and all(t.block_height and t.timestamp for t in confirmed)
    return f"{len(transfers)} credits, newest {max(t.timestamp for t in confirmed):%Y-%m-%d}"


@check("BTC testnet4: Esplora tip height")
def _():
    h = EsploraSource("https://mempool.space/testnet4/api", http=http).tip_height()
    assert h > 0
    return f"height {h}"


@check("BTC fees: mempool.space recommended")
def _():
    sat = MempoolSpaceFeeEstimator(http=http).sat_per_vbyte()
    assert sat > 0
    return f"{sat} sat/vB"


TRON_KEY = os.environ.get("TRONGRID_API_KEY")
TRON_HEADERS = {"TRON-PRO-API-KEY": TRON_KEY} if TRON_KEY else {}


def _tron_recent_recipient(base: str, contract: str) -> str:
    r = http.get_json(f"{base}/v1/contracts/{contract}/events", params={"event_name": "Transfer", "limit": 1},
                      headers=TRON_HEADERS)
    to_hex = r["data"][0]["result"]["to"]
    return b58check_encode(bytes.fromhex("41" + to_hex[2:]))


def _tron_check(network: str, base: str):
    contract = TRC20_TOKENS[("USDT", network)][0]
    to = _tron_recent_recipient(base, contract)
    # busy recipients (exchanges) have huge histories: read only the last hour
    src = TronGridSource(base, contract, http=http, api_key=TRON_KEY if "nile" not in base else None)
    transfers = src.incoming(to, not_before=datetime.now(timezone.utc) - timedelta(hours=1))
    assert transfers, f"no USDT transfers found for {to}"
    t = transfers[0]
    assert t.amount > 0 and (t.confirmations > 0 or t.block_height is None)
    return f"{to}: {len(transfers)} transfers, latest {Decimal(t.amount) / 10**6} USDT, {t.confirmations} confs"


@check("TRON mainnet: TronGrid USDT detection")
def _():
    return _tron_check("mainnet", "https://api.trongrid.io")


@check("TRON Nile: TronGrid USDT detection")
def _():
    return _tron_check("nile", "https://nile.trongrid.io")


@check("TRON fees: chain energy/bandwidth prices")
def _():
    est = TronEnergyFeeEstimator(http=http, api_key=TRON_KEY)
    energy, bw = est.prices_sun()
    return f"{energy} sun/energy, {bw} sun/byte -> {est.fee_trx()} TRX per USDT transfer"


@check("TRON derivation matches published vector")
def _():
    assert TronTRC20Backend(TRX_XPUB, http=http).derive_address(0) == "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"


def _eth_call(rpc: JsonRpcSource, contract: str, selector: str) -> bytes:
    out = rpc.rpc("eth_call", [{"to": contract, "data": selector}, "latest"])
    return bytes.fromhex(out[2:])


def _abi_string(raw: bytes) -> str:
    length = int.from_bytes(raw[32:64], "big")
    return raw[64:64 + length].decode()


@check("Ethereum mainnet: USDT/USDC presets are the real tokens")
def _():
    rpc = JsonRpcSource("https://ethereum-rpc.publicnode.com", "0x" + "0" * 40, http=http)
    out = []
    for asset in ("USDT", "USDC"):
        contract = ERC20Backend(ETH_XPUB, asset=asset, http=http).contract
        symbol = _abi_string(_eth_call(rpc, contract, "0x95d89b41"))
        decimals = int.from_bytes(_eth_call(rpc, contract, "0x313ce567"), "big")
        assert symbol == asset and decimals == 6, (symbol, decimals)
        out.append(f"{asset} ok")
    return ", ".join(out)


@check("Sepolia: ERC-20 log scanning finds a recent USDC transfer")
def _():
    b = ERC20Backend(ETH_XPUB, asset="USDC", network="sepolia", http=http)
    src = b.source
    head = src.head()
    logs = src.rpc("eth_getLogs", [{"address": b.contract, "topics": [TRANSFER_TOPIC],
                                    "fromBlock": hex(head - 300), "toBlock": hex(head)}])
    assert logs, "no USDC transfers in the last 300 Sepolia blocks"
    to = "0x" + logs[-1]["topics"][2][-40:]
    found = src.incoming(to, since_height=head - 300)
    assert found, f"package found no transfers to {to}"
    return f"{len(found)} transfer(s) to {to}"


@check("Prices: CoinGecko and Kraken agree on BTC/USD")
def _():
    cg = CoinGeckoProvider(http=http)
    kr = KrakenProvider(http=http)
    a, b = cg.rate("BTC", "USD"), kr.rate("BTC", "USD")
    CrossCheckedPriceProvider(cg, kr, max_deviation="0.02").rate("BTC", "USD")
    return f"CoinGecko {a}, Kraken {b}"


@check("Registry: confirm() end to end on a live address")
def _():
    reg = PaymentRegistry()
    reg.register("BTC", "mainnet", BitcoinBackend(ZPUB, http=http))
    s = reg.confirm("BTC", "mainnet", "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "0.00000001")
    assert s.state is State.CONFIRMED
    return f"received {s.received} BTC over {len(s.tx_ids)} txs"


if __name__ == "__main__":
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
