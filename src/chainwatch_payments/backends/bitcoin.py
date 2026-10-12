"""Bitcoin backend: watch-only HD derivation + Esplora-compatible detection.

Esplora is the REST API served by blockstream.info, mempool.space and by
self-hosted ``electrs``/``mempool`` instances, so "use my own node" is just
``EsploraSource(base_url="http://my-electrs:3000")``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

from ..crypto.addresses import btc_address, is_valid_btc_address
from ..crypto.bip32 import ExtendedPublicKey
from ..errors import ChainSourceError
from ..http import HttpClient, default_http_client
from ..types import Transfer
from ._common import ChainSource, HDBackend

__all__ = ["BitcoinBackend", "EsploraSource", "ESPLORA_URLS", "BTC_NETWORKS"]

BTC_NETWORKS = ("mainnet", "testnet", "testnet4", "signet", "regtest")

ESPLORA_URLS = {
    "mainnet": "https://blockstream.info/api",
    "testnet": "https://blockstream.info/testnet/api",
    "testnet4": "https://mempool.space/testnet4/api",
    "signet": "https://mempool.space/signet/api",
}


class EsploraSource:
    """Read-only Esplora REST client."""

    PAGE = 25  # confirmed txs per page served by Esplora

    def __init__(self, base_url: str, http: HttpClient | None = None, max_pages: int = 40) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http
        self.max_pages = max_pages

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = default_http_client()
        return self._http

    def tip_height(self) -> int:
        raw = self.http.get_json(f"{self.base_url}/blocks/tip/height")
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise ChainSourceError(f"bad tip height {raw!r}") from exc

    def incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]:
        tip = self.tip_height()
        nb = not_before.replace(tzinfo=not_before.tzinfo or timezone.utc).timestamp() if not_before else None
        page = self.http.get_json(f"{self.base_url}/address/{address}/txs")
        if not isinstance(page, list):
            raise ChainSourceError("unexpected Esplora response for address txs")
        txs = list(page)
        pages = 1
        while True:
            confirmed = [t for t in page if t.get("status", {}).get("confirmed")]
            if len(confirmed) < self.PAGE:
                break
            oldest = confirmed[-1]["status"].get("block_time")
            if nb is not None and oldest is not None and oldest < nb:
                break
            if pages >= self.max_pages:
                raise ChainSourceError(
                    f"address {address} has more than {self.max_pages * self.PAGE} txs; "
                    "pass not_before or raise max_pages"
                )
            page = self.http.get_json(f"{self.base_url}/address/{address}/txs/chain/{confirmed[-1]['txid']}")
            if not isinstance(page, list) or not page:
                break
            txs.extend(page)
            pages += 1

        out: list[Transfer] = []
        for tx in txs:
            st = tx.get("status") or {}
            if st.get("confirmed"):
                height = int(st["block_height"])
                confs = max(tip - height + 1, 0)
                ts = datetime.fromtimestamp(int(st["block_time"]), tz=timezone.utc) if st.get("block_time") else None
            else:
                height, confs, ts = None, 0, None
            for i, vout in enumerate(tx.get("vout") or []):
                if vout.get("scriptpubkey_address") == address and int(vout.get("value", 0)) > 0:
                    out.append(Transfer(
                        tx_id=tx["txid"], amount=int(vout["value"]), confirmations=confs,
                        timestamp=ts, index=i, block_height=height,
                    ))
        return out


class BitcoinBackend(HDBackend):
    """BTC on mainnet/testnet/testnet4/signet/regtest.

    ``xpub`` is the ACCOUNT-level extended public key (``m/84'/0'/0'`` for
    native SegWit). Receive addresses are ``<xpub>/0/<index>``. The script type
    is taken from the key prefix (xpub/tpub -> p2wpkh unless you say
    otherwise, ypub/upub -> p2sh-p2wpkh, zpub/vpub -> p2wpkh) or set it with
    ``script=``.
    """

    asset = "BTC"

    def __init__(
        self,
        xpub: str | None = None,
        *,
        network: str = "mainnet",
        address: str | None = None,
        script: str | None = None,
        confirmations: int = 2,
        source: ChainSource | None = None,
        base_url: str | None = None,
        http: HttpClient | None = None,
        path_template: str | None = None,
    ) -> None:
        if network not in BTC_NETWORKS:
            raise ValueError(f"network must be one of {BTC_NETWORKS}")
        self.network = network
        if xpub:
            key = ExtendedPublicKey.parse(xpub)
            if key.is_testnet != (network != "mainnet"):
                raise ValueError(f"{key.prefix} key does not belong to BTC {network}")
            if script is None:
                hint = key.script_hint
                script = "p2wpkh" if hint == "p2pkh" else hint  # plain xpub/tpub: default to SegWit
        self.script = script or "p2wpkh"
        if source is None:
            url = base_url or ESPLORA_URLS.get(network)
            if url is None:
                raise ValueError(f"no default explorer for {network}; pass base_url= or source=")
            source = EsploraSource(url, http=http)
        super().__init__(
            xpub=xpub,
            address=address,
            source=source,
            decimals=8,
            confirmations=confirmations,
            encoder=partial(btc_address, network=network, script=self.script),
            validator=partial(is_valid_btc_address, network=network),
            path_template=path_template,
        )
