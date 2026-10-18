"""Forward TRC-20 payments (USDT) from one-time deposit addresses to your wallet.

For each paid invoice:

1. derive the deposit address' key and REFUSE unless it matches the invoice address;
2. read its token balance (0 means already forwarded: nothing to do);
3. if it lacks TRX for fees, send ``gas_trx`` from the gas wallet (index 0 of
   the same account, which is why invoices start at index 1);
4. send the whole token balance to ``destination``, which must be on the
   allowlist, and wait for a SUCCESS receipt.

Safety rails: the account must match your watch-only xpub, the destination is
checked against an allowlist on every send, the fee burn is capped by
``fee_limit_trx``, and a zero balance short-circuits so retries never double-send.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol

from ..crypto.addresses import is_valid_tron_address
from ..errors import MissingDependency
from .base import BaseForwarder, ForwardError, ForwardResult
from .keys import PrivateNode

__all__ = ["ForwardError", "TronClient", "TronpyClient", "ForwardResult", "TronTokenForwarder"]

log = logging.getLogger("chainwatch_payments.forwarder")
SUN = Decimal(10**6)


class TronClient(Protocol):
    """What the forwarder needs from the network. ``TronpyClient`` implements
    it with tronpy; tests use a fake."""

    def trx_balance(self, address: str) -> Decimal: ...
    def token_balance(self, contract: str, address: str) -> int: ...
    def send_trx(self, key: bytes, from_address: str, to: str, amount_sun: int) -> str: ...
    def send_token(self, key: bytes, contract: str, from_address: str, to: str, amount: int,
                   fee_limit_sun: int) -> str: ...


class TronpyClient:
    """tronpy-backed client. Waits for each transaction and checks its result."""

    def __init__(self, network: str = "mainnet", api_key: str | None = None, timeout: float = 15.0,
                 wait_seconds: float = 60.0) -> None:
        try:
            from tronpy import Tron
            from tronpy.exceptions import AddressNotFound
            from tronpy.keys import PrivateKey
            from tronpy.providers import HTTPProvider
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MissingDependency("the forwarder needs tronpy: pip install 'chainwatch-payments[forwarder]'") from exc
        self._PrivateKey = PrivateKey
        self._AddressNotFound = AddressNotFound
        if network == "mainnet":
            provider = HTTPProvider(api_key=api_key, timeout=timeout) if api_key else HTTPProvider(timeout=timeout)
            self.client = Tron(provider)
        else:
            self.client = Tron(network=network)
        self.wait_seconds = wait_seconds
        self._contracts: dict = {}

    def _contract(self, address: str):
        if address not in self._contracts:
            self._contracts[address] = self.client.get_contract(address)
        return self._contracts[address]

    def trx_balance(self, address: str) -> Decimal:
        try:
            return Decimal(self.client.get_account_balance(address))
        except self._AddressNotFound:   # never received TRX: not activated yet
            return Decimal(0)

    def token_balance(self, contract: str, address: str) -> int:
        return int(self._contract(contract).functions.balanceOf(address))

    def send_trx(self, key: bytes, from_address: str, to: str, amount_sun: int) -> str:
        txn = self.client.trx.transfer(from_address, to, amount_sun).build().sign(self._PrivateKey(key))
        ret = txn.broadcast()
        ret.wait(timeout=self.wait_seconds)
        return ret.txid

    def send_token(self, key: bytes, contract: str, from_address: str, to: str, amount: int,
                   fee_limit_sun: int) -> str:
        txn = (self._contract(contract).functions.transfer(to, amount)
               .with_owner(from_address).fee_limit(fee_limit_sun).build().sign(self._PrivateKey(key)))
        ret = txn.broadcast()
        info = ret.wait(timeout=self.wait_seconds)
        result = (info.get("receipt") or {}).get("result")
        if result != "SUCCESS":
            raise ForwardError(f"token transfer {ret.txid} did not succeed: {result or info}")
        return ret.txid


class TronTokenForwarder(BaseForwarder):
    ACCOUNT_PATH = "m/44'/195'/{account}'"

    def __init__(
        self,
        *,
        mnemonic: str,
        expected_xpub: str,
        contract: str,
        destination: str,
        allowed_destinations: Iterable[str],
        client: TronClient,
        passphrase: str = "",
        account: int = 0,
        account_path: str | None = None,
        gas_index: int = 0,
        gas_trx: Decimal | str = "20",
        fee_limit_trx: Decimal | str = "30",
        min_amount: int = 1,
    ) -> None:
        self.GAS_INDEX = gas_index
        super().__init__(mnemonic=mnemonic, expected_xpub=expected_xpub, destination=destination,
                         allowed_destinations=allowed_destinations, validator=is_valid_tron_address,
                         passphrase=passphrase, account=account, account_path=account_path)
        self.contract = contract
        self.client = client
        self.gas_index = gas_index
        self.gas_sun = int(Decimal(str(gas_trx)) * SUN)
        self.fee_limit_sun = int(Decimal(str(fee_limit_trx)) * SUN)
        self.min_amount = min_amount
        self._gas = self._node(gas_index)
        if self.destination == self._gas.tron_address():
            raise ForwardError("destination must not be the gas wallet")

    def address_of(self, node: PrivateNode) -> str:
        return node.tron_address()

    @property
    def gas_address(self) -> str:
        return self._gas.tron_address()

    def forward(self, index: int, expected_address: str, *, gas_tx_already: str = "") -> ForwardResult:
        node = self._deposit_node(index, expected_address)
        address = expected_address

        amount = self.client.token_balance(self.contract, address)
        if amount < self.min_amount:
            return ForwardResult("nothing")

        gas_tx = gas_tx_already
        if self.client.trx_balance(address) * SUN < self.gas_sun:
            gas_tx = self.client.send_trx(self._gas.private_key_bytes, self.gas_address, address, self.gas_sun)
            log.info("forwarder: sent %s TRX gas to %s (%s)", Decimal(self.gas_sun) / SUN, address, gas_tx)

        transfer_tx = self.client.send_token(node.private_key_bytes, self.contract, address,
                                             self.destination, amount, self.fee_limit_sun)
        log.info("forwarder: moved %s base units from %s to %s (%s)", amount, address, self.destination, transfer_tx)
        return ForwardResult("done", amount=amount, gas_tx=gas_tx, transfer_tx=transfer_tx)
