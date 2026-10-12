"""EVM ERC-20 backend (USDT/USDC on Ethereum, Sepolia, or any EVM chain).

Detection uses only standard JSON-RPC (``eth_blockNumber``, ``eth_getLogs``,
``eth_getBlockByNumber``), so it works against a hosted RPC provider or your
own node unchanged. Logs flagged ``removed`` (reorged out) are ignored, and a
transfer whose block is reorged away simply stops being returned.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from itertools import count
from typing import Any

from ..crypto.addresses import evm_address, is_valid_evm_address
from ..errors import ChainSourceError
from ..http import HttpClient, default_http_client
from ..types import Transfer
from ._common import ChainSource, HDBackend

__all__ = ["ERC20Backend", "JsonRpcSource", "ERC20_TOKENS", "DEFAULT_RPC_URLS", "TRANSFER_TOPIC"]

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Public, keyless endpoints, fine for development; use your own provider/node in production.
DEFAULT_RPC_URLS = {
    "ethereum": "https://ethereum-rpc.publicnode.com",
    "sepolia": "https://ethereum-sepolia-rpc.publicnode.com",
}

# (asset, network) -> (contract, decimals). Verify before production use.
ERC20_TOKENS: dict[tuple[str, str], tuple[str, int]] = {
    ("USDT", "ethereum"): ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    ("USDC", "ethereum"): ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    ("USDC", "sepolia"): ("0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238", 6),
}


def _hex(n: int) -> str:
    return hex(n)


class JsonRpcSource:
    def __init__(
        self,
        rpc_url: str,
        contract: str,
        *,
        http: HttpClient | None = None,
        start_block: int = 0,
        lookback_blocks: int = 20_000,
        chunk_size: int = 2_000,
        block_time: float = 12.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.contract = contract.lower()
        self._http = http
        self.start_block = start_block
        self.lookback_blocks = lookback_blocks
        self.chunk_size = chunk_size
        self.block_time = block_time
        self._ids = count(1)

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = default_http_client()
        return self._http

    def rpc(self, method: str, params: list[Any]) -> Any:
        r = self.http.post_json(
            self.rpc_url, {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        )
        if not isinstance(r, dict):
            raise ChainSourceError(f"{method}: invalid JSON-RPC response")
        if r.get("error"):
            raise ChainSourceError(f"{method}: {r['error']}")
        return r.get("result")

    def _block(self, number: int | str) -> dict[str, Any]:
        tag = number if isinstance(number, str) else _hex(number)
        b = self.rpc("eth_getBlockByNumber", [tag, False])
        if not isinstance(b, dict):
            raise ChainSourceError(f"block {number} not available")
        return b

    def _start(self, head: int, not_before: datetime | None) -> int:
        if not_before is None:
            back = self.lookback_blocks
        else:
            nb = not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)
            head_ts = int(self._block(head)["timestamp"], 16)
            secs = max(head_ts - nb.timestamp(), 0)
            # estimate generously; exact timestamp filtering happens afterwards
            back = math.ceil(secs / self.block_time * 1.25) + 100
        return max(self.start_block, head - back + 1, 0)

    def incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]:
        head = int(self.rpc("eth_blockNumber", []), 16)
        start = self._start(head, not_before)
        to_topic = "0x" + "0" * 24 + address.lower()[2:]
        logs: list[dict[str, Any]] = []
        frm = start
        while frm <= head:
            to = min(frm + self.chunk_size - 1, head)
            res = self.rpc("eth_getLogs", [{
                "address": self.contract,
                "topics": [TRANSFER_TOPIC, None, to_topic],
                "fromBlock": _hex(frm),
                "toBlock": _hex(to),
            }])
            logs.extend(res or [])
            frm = to + 1

        ts_cache: dict[int, datetime] = {}
        out: list[Transfer] = []
        for lg in logs:
            if lg.get("removed"):
                continue
            if (lg.get("address") or "").lower() != self.contract:
                continue
            topics = lg.get("topics") or []
            if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC or topics[2].lower() != to_topic:
                continue
            if lg.get("blockNumber") is None:
                height, confs, ts = None, 0, None
            else:
                height = int(lg["blockNumber"], 16)
                confs = max(head - height + 1, 0)
                ts = None
                if not_before is not None:
                    if height not in ts_cache:
                        ts_cache[height] = datetime.fromtimestamp(
                            int(self._block(height)["timestamp"], 16), tz=timezone.utc
                        )
                    ts = ts_cache[height]
            out.append(Transfer(
                tx_id=lg["transactionHash"], amount=int(lg.get("data") or "0x0", 16),
                confirmations=confs, timestamp=ts, index=int(lg.get("logIndex") or "0x0", 16),
                block_height=height,
            ))
        return out


class ERC20Backend(HDBackend):
    """ERC-20 token on an EVM network.

    ``xpub`` is the account key at ``m/44'/60'/0'``; addresses are
    ``<xpub>/0/<index>`` (same as MetaMask/Ledger Live account 0 receive
    chain). Known tokens (USDT/USDC on Ethereum, USDC on Sepolia) are filled
    in; anything else needs ``contract=`` and ``decimals=``.

    Note: funds sent to a one-time ERC-20 address need gas (ETH) on that
    address to be moved later. That is the host's sweeping concern, outside
    this watch-only package.
    """

    def __init__(
        self,
        xpub: str | None = None,
        *,
        asset: str,
        network: str = "ethereum",
        contract: str | None = None,
        decimals: int | None = None,
        address: str | None = None,
        confirmations: int = 12,
        rpc_url: str | None = None,
        source: ChainSource | None = None,
        http: HttpClient | None = None,
        start_block: int = 0,
        lookback_blocks: int = 20_000,
        chunk_size: int = 2_000,
        block_time: float = 12.0,
        path_template: str | None = None,
    ) -> None:
        self.asset = asset.upper()
        self.network = network
        preset = ERC20_TOKENS.get((self.asset, network))
        if contract is None or decimals is None:
            if preset is None:
                raise ValueError(f"no preset for {self.asset} on {network}; pass contract= and decimals=")
            contract = contract or preset[0]
            decimals = preset[1] if decimals is None else decimals
        if not is_valid_evm_address(contract):
            raise ValueError(f"invalid ERC-20 contract address {contract!r}")
        self.contract = contract
        if source is None:
            url = rpc_url or DEFAULT_RPC_URLS.get(network)
            if url is None:
                raise ValueError(f"no default RPC for {network}; pass rpc_url= or source=")
            source = JsonRpcSource(
                url, contract, http=http, start_block=start_block,
                lookback_blocks=lookback_blocks, chunk_size=chunk_size, block_time=block_time,
            )
        super().__init__(
            xpub=xpub,
            address=address,
            source=source,
            decimals=decimals,
            confirmations=confirmations,
            encoder=evm_address,
            validator=is_valid_evm_address,
            path_template=path_template,
        )
