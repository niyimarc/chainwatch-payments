"""Forward ERC-20 tokens (USDT/USDC on Ethereum or any EVM chain) from
one-time deposit addresses.

Deposit addresses hold tokens but no ETH, so like TRON a gas wallet (index 0
of the same account) pays for fees. For each paid invoice:

1. derive the deposit key and REFUSE unless its address matches the invoice;
2. read the token balance (0: nothing to do);
3. price gas (EIP-1559: 2 x base fee + priority tip); if above
   ``max_fee_gwei``, DEFER (try later, does not count as a failed attempt);
4. if the deposit address can't pay ``gas_limit x max fee``, top it up from
   the gas wallet and wait for that to confirm (or defer);
5. send the whole token balance to ``destination`` and wait for receipt
   status 1 ("done"); if it isn't mined in time, report "sent" and check later.

Signing uses ``eth-account`` (``pip install chainwatch-payments[forwarder-evm]``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from ..crypto.addresses import evm_address, is_valid_evm_address
from ..errors import ChainSourceError, MissingDependency
from ..http import HttpClient, HttpxClient
from .base import BaseForwarder, ForwardDeferred, ForwardError, ForwardResult
from .keys import PrivateNode

__all__ = ["EvmRpcClient", "ERC20Forwarder", "transfer_calldata"]

log = logging.getLogger("chainwatch_payments.forwarder")
GWEI = 10**9
TRANSFER_SELECTOR = "a9059cbb"
BALANCE_OF_SELECTOR = "70a08231"


def transfer_calldata(to: str, amount: int) -> str:
    return "0x" + TRANSFER_SELECTOR + to.lower()[2:].rjust(64, "0") + hex(amount)[2:].rjust(64, "0")


class EvmRpcClient:
    """Minimal JSON-RPC client for what the forwarder needs."""

    def __init__(self, rpc_url: str, http: HttpClient | None = None) -> None:
        self.rpc_url = rpc_url
        self.http = http or HttpxClient()
        self._id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        r = self.http.post_json(self.rpc_url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if not isinstance(r, dict):
            raise ChainSourceError(f"{method}: invalid JSON-RPC response")
        if r.get("error"):
            raise ChainSourceError(f"{method}: {r['error']}")
        return r.get("result")

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def eth_balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def token_balance(self, contract: str, address: str) -> int:
        data = "0x" + BALANCE_OF_SELECTOR + address.lower()[2:].rjust(64, "0")
        out = self.call("eth_call", [{"to": contract, "data": data}, "latest"])
        return int(out or "0x0", 16)

    def nonce(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def fees(self) -> tuple[int, int]:
        """(base fee per gas, priority tip per gas) in wei."""
        block = self.call("eth_getBlockByNumber", ["latest", False]) or {}
        base = int(block.get("baseFeePerGas") or "0x0", 16)
        try:
            tip = int(self.call("eth_maxPriorityFeePerGas", []), 16)
        except ChainSourceError:      # not every node supports it
            tip = int(1.5 * GWEI)
        return base, tip

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        return int(self.call("eth_estimateGas", [tx]), 16)

    def send_raw(self, raw_hex: str) -> str:
        return str(self.call("eth_sendRawTransaction", [raw_hex]))

    def receipt(self, txid: str) -> dict[str, Any] | None:
        r = self.call("eth_getTransactionReceipt", [txid])
        return r if isinstance(r, dict) else None


class ERC20Forwarder(BaseForwarder):
    ACCOUNT_PATH = "m/44'/60'/{account}'"

    def __init__(
        self,
        *,
        mnemonic: str,
        expected_xpub: str,
        contract: str,
        destination: str,
        allowed_destinations: Iterable[str],
        client: EvmRpcClient,
        chain_id: int | None = None,
        passphrase: str = "",
        account: int = 0,
        account_path: str | None = None,
        gas_index: int = 0,
        gas_limit: int = 100_000,
        max_fee_gwei: Decimal | str = "50",
        gas_buffer: Decimal | str = "1.2",
        min_amount: int = 1,
        wait_seconds: float = 180.0,
        poll_seconds: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.GAS_INDEX = gas_index
        super().__init__(mnemonic=mnemonic, expected_xpub=expected_xpub, destination=destination,
                         allowed_destinations=allowed_destinations, validator=is_valid_evm_address,
                         passphrase=passphrase, account=account, account_path=account_path)
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MissingDependency("the ERC-20 forwarder needs eth-account: "
                                    "pip install 'chainwatch-payments[forwarder-evm]'") from exc
        self._Account = Account
        if not is_valid_evm_address(contract):
            raise ForwardError(f"invalid token contract {contract!r}")
        self.contract = contract
        self.client = client
        self._chain_id = chain_id
        self.gas_limit = gas_limit
        self.max_fee_wei = int(Decimal(str(max_fee_gwei)) * GWEI)
        self.gas_buffer = Decimal(str(gas_buffer))
        self.min_amount = min_amount
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self._sleep = sleep
        self._gas = self._node(gas_index)
        if self.destination.lower() == self.gas_address.lower():
            raise ForwardError("destination must not be the gas wallet")

    def address_of(self, node: PrivateNode) -> str:
        return evm_address(node.public_key)

    @property
    def gas_address(self) -> str:
        return self.address_of(self._gas)

    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = self.client.chain_id()
        return self._chain_id

    # -- helpers -------------------------------------------------------------------
    def _sign(self, node: PrivateNode, tx: dict[str, Any]) -> str:
        signed = self._Account.sign_transaction(tx, node.private_key_bytes)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        return "0x" + bytes(raw).hex()

    def _wait(self, txid: str) -> dict[str, Any] | None:
        deadline = self.wait_seconds
        waited = 0.0
        while waited <= deadline:
            rec = self.client.receipt(txid)
            if rec is not None:
                return rec
            self._sleep(self.poll_seconds)
            waited += self.poll_seconds
        return None

    def _price(self) -> tuple[int, int]:
        base, tip = self.client.fees()
        max_fee = 2 * base + tip
        if max_fee > self.max_fee_wei:
            raise ForwardDeferred(f"gas {max_fee / GWEI:.2f} gwei is above max_fee_gwei "
                                  f"{self.max_fee_wei / GWEI:.2f}; waiting")
        return max_fee, min(tip, max_fee)

    # -- forwarding ------------------------------------------------------------------
    def forward(self, index: int, expected_address: str, *, gas_tx_already: str = "") -> ForwardResult:
        node = self._deposit_node(index, expected_address, case_insensitive=True)
        address = self.address_of(node)
        amount = self.client.token_balance(self.contract, address)
        if amount < self.min_amount:
            return ForwardResult("nothing")

        max_fee, tip = self._price()
        data = transfer_calldata(self.destination, amount)
        try:
            gas = int(self.client.estimate_gas({"from": address, "to": self.contract, "data": data}) * 1.3)
            gas = min(max(gas, 50_000), self.gas_limit)
        except ChainSourceError:
            gas = self.gas_limit
        need = gas * max_fee

        gas_tx = gas_tx_already
        if self.client.eth_balance(address) < need:
            top_up = int(Decimal(need) * self.gas_buffer)
            gas_tx = self._sign(self._gas, {
                "type": 2, "chainId": self.chain_id, "nonce": self.client.nonce(self.gas_address),
                "to": address, "value": top_up, "gas": 21_000,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip, "data": "0x",
            })
            gas_tx = self.client.send_raw(gas_tx)
            log.info("forwarder: sent %s wei gas to %s (%s)", top_up, address, gas_tx)
            rec = self._wait(gas_tx)
            if rec is None:
                raise ForwardDeferred(f"gas top-up {gas_tx} not mined yet; will retry")
            if int(rec.get("status", "0x0"), 16) != 1:
                raise ForwardError(f"gas top-up {gas_tx} failed")

        raw = self._sign(node, {
            "type": 2, "chainId": self.chain_id, "nonce": self.client.nonce(address),
            "to": self.contract, "value": 0, "gas": gas,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip, "data": data,
        })
        txid = self.client.send_raw(raw)
        log.info("forwarder: sent %s token units from %s to %s (%s)", amount, address, self.destination, txid)
        rec = self._wait(txid)
        if rec is None:
            return ForwardResult("sent", amount=amount, gas_tx=gas_tx, transfer_tx=txid)
        if int(rec.get("status", "0x0"), 16) != 1:
            raise ForwardError(f"token transfer {txid} reverted")
        return ForwardResult("done", amount=amount, gas_tx=gas_tx, transfer_tx=txid)

    def tx_state(self, txid: str) -> str:
        rec = self.client.receipt(txid)
        if rec is None:
            return "pending"
        return "confirmed" if int(rec.get("status", "0x0"), 16) == 1 else "dropped"
