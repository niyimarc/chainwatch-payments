"""What every forwarder shares: results, errors, and the key/destination checks."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..errors import ChainwatchError
from .keys import PrivateNode, mnemonic_to_seed, same_account

__all__ = ["ForwardError", "ForwardDeferred", "ForwardResult", "Forwarder", "BaseForwarder"]


class ForwardError(ChainwatchError):
    """The forward failed; it counts as an attempt."""


class ForwardDeferred(ForwardError):
    """Not now, try again later (fees above your cap, funding tx still
    confirming). Does NOT count towards FORWARD_MAX_ATTEMPTS."""


@dataclass(frozen=True)
class ForwardResult:
    status: str             # "done" (final), "sent" (broadcast, awaiting confirmation), "nothing"
    amount: int = 0         # base units that arrive at the destination
    fee: int = 0            # base units of the forwarded asset spent on fees (BTC); 0 when gas is paid separately
    gas_tx: str = ""
    transfer_tx: str = ""


@runtime_checkable
class Forwarder(Protocol):
    destination: str

    def forward(self, index: int, expected_address: str, *, gas_tx_already: str = "") -> ForwardResult: ...

    def tx_state(self, txid: str) -> str:
        """"confirmed", "pending" or "dropped" for a transfer previously returned as "sent"."""
        ...


class BaseForwarder:
    """Seed handling and safety checks common to all chains.

    Subclasses set ``ACCOUNT_PATH`` (e.g. ``"m/84'/0'/{account}'"``) or pass
    ``account_path``, and implement ``address_of(node)`` and ``forward``.
    """

    ACCOUNT_PATH = ""
    GAS_INDEX: int | None = 0   # chains that pay fees from a separate wallet reserve index 0

    def __init__(
        self,
        *,
        mnemonic: str,
        expected_xpub: str,
        destination: str,
        allowed_destinations: Iterable[str],
        validator: Callable[[str], bool],
        passphrase: str = "",
        account: int = 0,
        account_path: str | None = None,
    ) -> None:
        if not mnemonic or len(mnemonic.split()) not in (12, 15, 18, 21, 24):
            raise ForwardError("the forwarder mnemonic must be 12-24 words")
        path = (account_path or self.ACCOUNT_PATH).format(account=account)
        self._account: PrivateNode = PrivateNode.from_seed(mnemonic_to_seed(mnemonic, passphrase)).derive(path)
        if not same_account(self._account, expected_xpub):
            raise ForwardError(
                "the forwarder mnemonic does not match the xpub your backend watches; "
                f"refusing to run (wrong seed, passphrase or account? derived {path})"
            )
        allowed = {a.strip() for a in allowed_destinations}
        for a in allowed | {destination}:
            if not validator(a):
                raise ForwardError(f"invalid destination address in forwarder config: {a!r}")
        if destination not in allowed:
            raise ForwardError("destination is not in the allowed destinations list")
        self.destination = destination
        self._allowed = frozenset(allowed)
        self.account_path = path

    def __repr__(self) -> str:  # never print key material
        return f"{type(self).__name__}(destination={self.destination})"

    def _node(self, index: int) -> PrivateNode:
        return self._account.derive(f"0/{index}")

    def address_of(self, node: PrivateNode) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def deposit_address(self, index: int) -> str:
        return self.address_of(self._node(index))

    def _deposit_node(self, index: int, expected_address: str, *, case_insensitive: bool = False) -> PrivateNode:
        """Derive the deposit key and refuse unless it matches the invoice address."""
        if self.GAS_INDEX is not None and index == self.GAS_INDEX:
            raise ForwardError("refusing to forward from the gas wallet index")
        node = self._node(index)
        derived = self.address_of(node)
        same = derived.lower() == expected_address.lower() if case_insensitive else derived == expected_address
        if not same:
            raise ForwardError(f"derived address {derived} does not match invoice address {expected_address}")
        if self.destination not in self._allowed:  # pragma: no cover - guarded at init too
            raise ForwardError("destination not allowed")
        if self.destination.lower() == derived.lower():
            raise ForwardError("destination is this deposit address itself; check your forwarder config")
        return node

    def tx_state(self, txid: str) -> str:
        return "confirmed"
