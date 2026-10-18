"""In-memory backend with a tiny simulated chain, for tests and examples."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..backend import BaseBackend
from ..errors import UnsupportedCapability
from ..types import AddressMode, Capabilities, Transfer, to_base_units

__all__ = ["MockBackend"]

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class _Tx:
    address: str
    amount: int
    tx_id: str
    index: int
    memo: str | None
    block: int | None       # None = in mempool
    timestamp: datetime | None


class MockBackend(BaseBackend):
    """Deterministic fake chain.

    >>> b = MockBackend("BTC", "regtest", decimals=8)
    >>> addr = b.derive_address(0)
    >>> tx = b.credit(addr, "0.5")        # lands in the mempool
    >>> b.mine(3)                          # now 3 confirmations
    """

    def __init__(
        self,
        asset: str = "MOCK",
        network: str = "mocknet",
        *,
        decimals: int = 8,
        default_confirmations: int = 1,
        supports_memo: bool = False,
        hd: bool = True,
        single_address: str | None = None,
        seed: str = "chainwatch",
        block_seconds: int = 600,
        fixed_times: bool = False,
    ) -> None:
        self.asset = asset
        self.network = network
        self._decimals = decimals
        self._confs = default_confirmations
        self._memo = supports_memo
        self._hd = hd
        self._seed = seed
        self._block_seconds = block_seconds
        self._fixed_times = fixed_times
        self.single_address = single_address
        self.height = 0
        self._txs: list[_Tx] = []
        self._counter = 0
        self.reads = 0  # number of get_incoming calls, handy in tests
        self.last_since_height: int | None = None

    # -- Backend protocol -----------------------------------------------------
    def capabilities(self) -> Capabilities:
        modes = ((AddressMode.HD,) if self._hd else ()) + ((AddressMode.SINGLE,) if self.single_address else ())
        return Capabilities(
            hd_derivation=self._hd,
            memo=self._memo,
            decimals=self._decimals,
            default_confirmations=self._confs,
            address_modes=modes,
        )

    def derive_address(self, index: int) -> str:
        if not self._hd:
            raise UnsupportedCapability("HD derivation disabled on this mock")
        h = hashlib.sha256(f"{self._seed}:{self.asset}:{self.network}:{index}".encode()).hexdigest()
        return f"mock{self.asset.lower()}1{h[:32]}"

    def address_with_memo(self, reference: str) -> tuple[str, str]:
        if not self._memo:
            raise UnsupportedCapability("memos disabled on this mock")
        addr = self.single_address or self.derive_address(0)
        memo = hashlib.sha256(f"memo:{reference}".encode()).hexdigest()[:10]
        return addr, memo

    def current_height(self) -> int:
        return self.height

    def get_incoming(self, address: str, not_before: datetime | None = None,
                     since_height: int | None = None) -> list[Transfer]:
        self.reads += 1
        self.last_since_height = since_height
        out = []
        for t in self._txs:
            if t.address != address:
                continue
            if since_height is not None and t.block is not None and t.block < since_height:
                continue
            confs = self.height - t.block + 1 if t.block is not None else 0
            out.append(Transfer(
                tx_id=t.tx_id, amount=t.amount, confirmations=confs, timestamp=t.timestamp,
                index=t.index, memo=t.memo, block_height=t.block,
            ))
        return out

    # -- chain simulation -----------------------------------------------------
    def _block_time(self, height: int) -> datetime:
        """Simulated block time: wall-clock time when the block is mined, or a
        fixed timeline if ``fixed_times=True`` was passed (fully deterministic)."""
        if self._fixed_times:
            return _EPOCH + timedelta(seconds=height * self._block_seconds)
        return datetime.now(timezone.utc)

    def credit(
        self,
        address: str,
        amount: Decimal | str | int,
        *,
        tx_id: str | None = None,
        index: int = 0,
        memo: str | None = None,
        mined: bool = False,
        at: datetime | None = None,
    ) -> str:
        """Send ``amount`` (human units) to ``address``. Mempool unless ``mined``
        (then ``at`` sets the block time)."""
        self._counter += 1
        tx_id = tx_id or hashlib.sha256(f"tx{self._counter}:{self._seed}".encode()).hexdigest()
        tx = _Tx(address, to_base_units(amount, self._decimals), tx_id, index, memo, None, None)
        self._txs.append(tx)
        if mined:
            self.mine(1, only=tx_id, at=at)
        return tx_id

    def mine(self, blocks: int = 1, *, only: str | None = None, at: datetime | None = None) -> None:
        """Mine ``blocks`` blocks; mempool txs go into the first one, whose
        timestamp is ``at`` if given (simulated block time otherwise)."""
        for i in range(blocks):
            self.height += 1
            if i == 0:
                for t in self._txs:
                    if t.block is None and (only is None or t.tx_id == only):
                        t.block = self.height
                        t.timestamp = at or self._block_time(self.height)

    def reorg(self, depth: int, *, drop: bool = True) -> None:
        """Orphan the last ``depth`` blocks. Their txs vanish (``drop``) or
        return to the mempool. Height is NOT replaced; call ``mine`` after."""
        cutoff = self.height - depth
        for t in list(self._txs):
            if t.block is not None and t.block > cutoff:
                if drop:
                    self._txs.remove(t)
                else:
                    t.block, t.timestamp = None, None
        self.height = cutoff

    def drop(self, tx_id: str) -> None:
        self._txs = [t for t in self._txs if t.tx_id != tx_id]
