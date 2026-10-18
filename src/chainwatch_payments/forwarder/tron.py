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
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..crypto.addresses import is_valid_tron_address
from ..errors import ChainwatchError, MissingDependency
from .keys import PrivateNode, account_node, mnemonic_to_seed, same_account

__all__ = ["ForwardError", "TronClient", "TronpyClient", "ForwardResult", "TronTokenForwarder"]

log = logging.getLogger("chainwatch_payments.forwarder")
SUN = Decimal(10**6)


class ForwardError(ChainwatchError):
    pass


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


@dataclass(frozen=True)
class ForwardResult:
    status: str             # "done" | "nothing"
    amount: int = 0         # base units moved
    gas_tx: str = ""
    transfer_tx: str = ""


class TronTokenForwarder:
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
        gas_index: int = 0,
        gas_trx: Decimal | str = "20",
        fee_limit_trx: Decimal | str = "30",
        min_amount: int = 1,
    ) -> None:
        if not mnemonic or len(mnemonic.split()) not in (12, 15, 18, 21, 24):
            raise ForwardError("the forwarder mnemonic must be 12-24 words")
        self._account: PrivateNode = account_node(mnemonic_to_seed(mnemonic, passphrase), 195, account)
        if not same_account(self._account, expected_xpub):
            raise ForwardError(
                "the forwarder mnemonic does not match the xpub your backend watches; "
                "refusing to run (wrong seed, passphrase or account?)"
            )
        allowed = {a.strip() for a in allowed_destinations}
        for a in allowed | {destination}:
            if not is_valid_tron_address(a):
                raise ForwardError(f"invalid TRON address in forwarder config: {a!r}")
        if destination not in allowed:
            raise ForwardError("destination is not in the allowed destinations list")
        self.contract = contract
        self.destination = destination
        self._allowed = frozenset(allowed)
        self.client = client
        self.gas_index = gas_index
        self.gas_sun = int(Decimal(str(gas_trx)) * SUN)
        self.fee_limit_sun = int(Decimal(str(fee_limit_trx)) * SUN)
        self.min_amount = min_amount
        self._gas = self._node(gas_index)
        if self.destination == self._gas.tron_address():
            raise ForwardError("destination must not be the gas wallet")

    def __repr__(self) -> str:
        return f"TronTokenForwarder(contract={self.contract}, destination={self.destination})"

    def _node(self, index: int) -> PrivateNode:
        return self._account.derive(f"0/{index}")

    @property
    def gas_address(self) -> str:
        return self._gas.tron_address()

    def deposit_address(self, index: int) -> str:
        return self._node(index).tron_address()

    def forward(self, index: int, expected_address: str, *, gas_tx_already: str = "") -> ForwardResult:
        if index == self.gas_index:
            raise ForwardError("refusing to forward from the gas wallet index")
        node = self._node(index)
        address = node.tron_address()
        if address != expected_address:
            raise ForwardError(f"derived address {address} does not match invoice address {expected_address}")
        if self.destination not in self._allowed:  # pragma: no cover - guarded at init too
            raise ForwardError("destination not allowed")

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
        return ForwardResult("done", amount, gas_tx, transfer_tx)
