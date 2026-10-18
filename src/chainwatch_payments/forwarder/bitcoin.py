"""Forward BTC from one-time native SegWit (P2WPKH) deposit addresses.

No gas wallet is needed: the network fee is paid from the coins themselves.
For each paid invoice:

1. derive the deposit key and REFUSE unless its address matches the invoice;
2. list the address' CONFIRMED unspent outputs (none: nothing to do);
3. fee = current fee rate (mempool.space, capped by ``max_fee_rate``) x size;
4. send everything minus the fee to ``destination`` in one transaction
   (signals RBF), broadcast it, and report it as "sent";
5. later checks report it "confirmed", or "dropped" so it can be retried.

Transactions are built and signed with ``embit`` (``pip install
chainwatch-payments[forwarder-btc]``).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Protocol

from ..crypto.addresses import btc_address, is_valid_btc_address
from ..errors import ChainSourceError, MissingDependency
from ..http import HttpClient, HttpxClient
from .base import BaseForwarder, ForwardDeferred, ForwardError, ForwardResult
from .keys import PrivateNode

__all__ = ["BitcoinSpendClient", "EsploraSpendClient", "BitcoinForwarder", "p2wpkh_sweep_vsize"]

log = logging.getLogger("chainwatch_payments.forwarder")

DUST_SATS = 546
SEQUENCE_RBF = 0xFFFFFFFD
_ESPLORA = {
    "mainnet": "https://mempool.space/api",
    "testnet": "https://mempool.space/testnet/api",
    "testnet4": "https://mempool.space/testnet4/api",
    "signet": "https://mempool.space/signet/api",
}


def p2wpkh_sweep_vsize(n_inputs: int, output_script_len: int) -> int:
    """Virtual size of a transaction spending ``n_inputs`` P2WPKH inputs to one output."""
    weight = 42 + n_inputs * 272 + 4 * (9 + output_script_len)
    return math.ceil(weight / 4)


class BitcoinSpendClient(Protocol):
    def utxos(self, address: str) -> list[dict[str, Any]]:
        """[{"txid", "vout", "value", "status": {"confirmed": bool}}] (Esplora format)."""
        ...
    def fee_rate(self, target: str) -> Decimal: ...
    def broadcast(self, raw_hex: str) -> str: ...
    def tx_state(self, txid: str) -> str: ...


class EsploraSpendClient:
    """Esplora (mempool.space / blockstream.info / your own electrs) for spending."""

    def __init__(self, network: str = "mainnet", base_url: str | None = None,
                 http: HttpClient | None = None) -> None:
        url = base_url or _ESPLORA.get(network)
        if url is None:
            raise ValueError(f"no default Esplora URL for {network}; pass base_url=")
        self.base_url = url.rstrip("/")
        self.http = http or HttpxClient()

    def utxos(self, address: str) -> list[dict[str, Any]]:
        r = self.http.get_json(f"{self.base_url}/address/{address}/utxo")
        if not isinstance(r, list):
            raise ChainSourceError("unexpected Esplora utxo response")
        return r

    def fee_rate(self, target: str) -> Decimal:
        r = self.http.get_json(f"{self.base_url}/v1/fees/recommended")
        try:
            return Decimal(str(r[target]))
        except (KeyError, TypeError) as exc:
            raise ChainSourceError(f"no {target} in fee response") from exc

    def broadcast(self, raw_hex: str) -> str:
        post_text = getattr(self.http, "post_text", None)
        if post_text is None:
            raise ChainSourceError("HTTP client cannot POST raw text")
        txid = post_text(f"{self.base_url}/tx", raw_hex)
        return str(txid).strip()

    def tx_state(self, txid: str) -> str:
        try:
            st = self.http.get_json(f"{self.base_url}/tx/{txid}/status")
        except ChainSourceError as exc:
            if "404" in str(exc):
                return "dropped"
            raise
        return "confirmed" if isinstance(st, dict) and st.get("confirmed") else "pending"


class BitcoinForwarder(BaseForwarder):
    GAS_INDEX = None   # fees come from the coins; index 0 is not special here

    def __init__(
        self,
        *,
        mnemonic: str,
        expected_xpub: str,
        destination: str,
        allowed_destinations: Iterable[str],
        client: BitcoinSpendClient,
        network: str = "mainnet",
        passphrase: str = "",
        account: int = 0,
        account_path: str | None = None,
        fee_target: str = "halfHourFee",
        fee_rate: Decimal | str | None = None,
        max_fee_rate: Decimal | str = "100",
        min_amount: int = 10_000,
    ) -> None:
        self.network = network
        coin = 0 if network == "mainnet" else 1
        super().__init__(mnemonic=mnemonic, expected_xpub=expected_xpub, destination=destination,
                         allowed_destinations=allowed_destinations,
                         validator=lambda a: is_valid_btc_address(a, network),
                         passphrase=passphrase, account=account,
                         account_path=account_path or f"m/84'/{coin}'/{{account}}'")
        try:
            from embit import script as _script  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MissingDependency(
                "the BTC forwarder needs embit: pip install 'chainwatch-payments[forwarder-btc]'") from exc
        self.client = client
        self.fee_target = fee_target
        self.fixed_fee_rate = Decimal(str(fee_rate)) if fee_rate is not None else None
        self.max_fee_rate = Decimal(str(max_fee_rate))
        self.min_amount = min_amount

    def address_of(self, node: PrivateNode) -> str:
        return btc_address(node.public_key, self.network, "p2wpkh")

    def _rate(self) -> Decimal:
        rate = self.fixed_fee_rate if self.fixed_fee_rate is not None else self.client.fee_rate(self.fee_target)
        if rate > self.max_fee_rate:
            raise ForwardDeferred(f"fee rate {rate} sat/vB is above max_fee_rate {self.max_fee_rate}; waiting")
        return max(rate, Decimal(1))

    def build(self, node: PrivateNode, utxos: list[dict[str, Any]], rate: Decimal) -> tuple[str, int, int]:
        """Sign a sweep of ``utxos`` to the destination. Returns (raw hex, amount, fee)."""
        from embit import ec
        from embit.script import address_to_scriptpubkey, p2pkh, p2wpkh, witness_p2wpkh
        from embit.transaction import Transaction, TransactionInput, TransactionOutput

        key = ec.PrivateKey(node.private_key_bytes)
        pub = key.get_public_key()
        own_spk = p2wpkh(pub)
        dest_spk = address_to_scriptpubkey(self.destination)
        total = sum(int(u["value"]) for u in utxos)
        vsize = p2wpkh_sweep_vsize(len(utxos), len(dest_spk.data))
        fee = int((rate * vsize).to_integral_value(rounding="ROUND_CEILING"))
        amount = total - fee
        if amount < max(DUST_SATS, self.min_amount):
            raise ForwardError(f"{total} sats is too small to forward (fee {fee} sats at {rate} sat/vB)")

        vin = [TransactionInput(bytes.fromhex(u["txid"]), int(u["vout"]), sequence=SEQUENCE_RBF) for u in utxos]
        tx = Transaction(version=2, vin=vin, vout=[TransactionOutput(amount, dest_spk)], locktime=0)
        script_code = p2pkh(pub)               # BIP143: scriptCode for P2WPKH
        for i, u in enumerate(utxos):
            sighash = tx.sighash_segwit(i, script_code, int(u["value"]))
            tx.vin[i].witness = witness_p2wpkh(key.sign(sighash), pub)
        if own_spk.data[2:] != script_code.data[3:23]:  # pragma: no cover - sanity check
            raise ForwardError("internal error: script code does not match the deposit key")
        return tx.serialize().hex(), amount, fee

    def forward(self, index: int, expected_address: str, *, gas_tx_already: str = "") -> ForwardResult:
        node = self._deposit_node(index, expected_address)
        utxos = [u for u in self.client.utxos(expected_address) if (u.get("status") or {}).get("confirmed")]
        if not utxos:
            return ForwardResult("nothing")
        raw, amount, fee = self.build(node, utxos, self._rate())
        txid = self.client.broadcast(raw)
        log.info("forwarder: sent %s sats (fee %s) from %s to %s (%s)",
                 amount, fee, expected_address, self.destination, txid)
        return ForwardResult("sent", amount=amount, fee=fee, transfer_tx=txid)

    def tx_state(self, txid: str) -> str:
        return self.client.tx_state(txid)
