"""TRON TRC-20 backend (USDT by default): watch-only HD + TronGrid detection.

Detection lists TRC-20 transfers to the address via TronGrid's
``/v1/accounts/{addr}/transactions/trc20`` and then reads each transaction's
block via the standard full-node ``/wallet/gettransactioninfobyid`` and
``/wallet/getnowblock`` endpoints to compute confirmations and check the
execution result. TRON blocks are irreversible ("solidified") after 19
confirmations, hence the default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..crypto.addresses import is_valid_tron_address, tron_address
from ..errors import ChainSourceError
from ..http import HttpClient, default_http_client
from ..types import Transfer
from ._common import ChainSource, HDBackend

__all__ = ["TronTRC20Backend", "TronGridSource", "TRONGRID_URLS", "TRC20_TOKENS"]

TRONGRID_URLS = {
    "mainnet": "https://api.trongrid.io",
    "nile": "https://nile.trongrid.io",
    "shasta": "https://api.shasta.trongrid.io",
}

# (asset, tron network) -> (contract, decimals). Verify before production use.
TRC20_TOKENS: dict[tuple[str, str], tuple[str, int]] = {
    ("USDT", "mainnet"): ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6),
    ("USDT", "nile"): ("TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf", 6),
}


class TronGridSource:
    def __init__(
        self,
        base_url: str,
        contract: str,
        *,
        http: HttpClient | None = None,
        api_key: str | None = None,
        page_limit: int = 200,
        max_pages: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.contract = contract
        self._http = http
        self.api_key = api_key
        self.page_limit = page_limit
        self.max_pages = max_pages

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = default_http_client()
        return self._http

    @property
    def _headers(self) -> dict[str, str]:
        return {"TRON-PRO-API-KEY": self.api_key} if self.api_key else {}

    def head_block(self) -> int:
        r = self.http.post_json(f"{self.base_url}/wallet/getnowblock", {}, headers=self._headers)
        try:
            return int(r["block_header"]["raw_data"]["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainSourceError("unexpected getnowblock response") from exc

    def tx_info(self, tx_id: str) -> dict[str, Any]:
        r = self.http.post_json(
            f"{self.base_url}/wallet/gettransactioninfobyid", {"value": tx_id}, headers=self._headers
        )
        return r if isinstance(r, dict) else {}

    def _list(self, address: str, not_before: datetime | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "only_to": "true",
            "contract_address": self.contract,
            "limit": self.page_limit,
            "order_by": "block_timestamp,desc",
        }
        if not_before is not None:
            nb = not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)
            params["min_timestamp"] = int(nb.timestamp() * 1000)
        rows: list[dict[str, Any]] = []
        url = f"{self.base_url}/v1/accounts/{address}/transactions/trc20"
        for _ in range(self.max_pages):
            r = self.http.get_json(url, params=params, headers=self._headers)
            if not isinstance(r, dict) or r.get("success") is False:
                raise ChainSourceError(f"TronGrid error: {r!r}")
            rows.extend(r.get("data") or [])
            fp = (r.get("meta") or {}).get("fingerprint")
            if not fp:
                return rows
            params = {**params, "fingerprint": fp}
        raise ChainSourceError(f"too many TRC-20 transfers for {address}; pass not_before")

    def incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]:
        rows = [
            r for r in self._list(address, not_before)
            if r.get("to") == address
            and r.get("type", "Transfer") == "Transfer"
            and (r.get("token_info") or {}).get("address", self.contract) == self.contract
        ]
        if not rows:
            return []
        head = self.head_block()
        seen: dict[str, int] = {}
        out: list[Transfer] = []
        infos: dict[str, dict[str, Any]] = {}
        for r in rows:
            tx_id = r["transaction_id"]
            idx = seen.get(tx_id, 0)
            seen[tx_id] = idx + 1
            info = infos.get(tx_id)
            if info is None:
                info = infos[tx_id] = self.tx_info(tx_id)
            result = (info.get("receipt") or {}).get("result")
            if result is not None and result != "SUCCESS":
                continue  # reverted/failed execution: no funds moved
            block = info.get("blockNumber")
            if block is None:
                confs, height, ts = 0, None, None
            else:
                height = int(block)
                confs = max(head - height + 1, 0)
                ms = info.get("blockTimeStamp") or r.get("block_timestamp")
                ts = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None
            out.append(Transfer(
                tx_id=tx_id, amount=int(r["value"]), confirmations=confs,
                timestamp=ts, index=idx, block_height=height,
            ))
        return out


class TronTRC20Backend(HDBackend):
    """TRC-20 token on TRON mainnet / Nile / Shasta.

    ``xpub`` is the account key at ``m/44'/195'/0'``; addresses are
    ``<xpub>/0/<index>``. For USDT on mainnet/Nile the contract and decimals
    are filled in; for any other TRC-20 pass ``contract=`` and ``decimals=``.
    """

    def __init__(
        self,
        xpub: str | None = None,
        *,
        network: str = "mainnet",
        asset: str = "USDT",
        contract: str | None = None,
        decimals: int | None = None,
        address: str | None = None,
        confirmations: int = 19,
        source: ChainSource | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        http: HttpClient | None = None,
        path_template: str | None = None,
    ) -> None:
        self.network = network
        self.asset = asset.upper()
        preset = TRC20_TOKENS.get((self.asset, network))
        if contract is None or decimals is None:
            if preset is None:
                raise ValueError(f"no preset for {self.asset} on TRON {network}; pass contract= and decimals=")
            contract = contract or preset[0]
            decimals = preset[1] if decimals is None else decimals
        if not is_valid_tron_address(contract):
            raise ValueError(f"invalid TRC-20 contract address {contract!r}")
        self.contract = contract
        if source is None:
            url = base_url or TRONGRID_URLS.get(network)
            if url is None:
                raise ValueError(f"no default TronGrid URL for {network}; pass base_url= or source=")
            source = TronGridSource(url, contract, http=http, api_key=api_key)
        super().__init__(
            xpub=xpub,
            address=address,
            source=source,
            decimals=decimals,
            confirmations=confirmations,
            encoder=tron_address,
            validator=is_valid_tron_address,
            path_template=path_template,
        )
