"""Chain-agnostic confirmation logic.

``evaluate`` is a pure function: the same transfers and parameters always give
the same ``PaymentStatus``. The package keeps no state, so a reorg that drops a
transaction simply makes the next evaluation come back lower; pass the
previous status as ``previous`` to have that flagged as ``reverted``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal

from .types import Match, MatchMode, PaymentStatus, State, Tolerance, Transfer

__all__ = ["evaluate", "unique_amount"]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalise(
    transfers: Iterable[Transfer], not_before: datetime | None, memo: str | None
) -> list[Transfer]:
    best: dict[tuple[str, int], Transfer] = {}
    for t in transfers:
        if t.amount <= 0:
            continue
        if memo is not None and t.memo != memo:
            continue
        if not_before is not None and t.timestamp is not None:
            if _aware(t.timestamp) < _aware(not_before):
                continue
        cur = best.get(t.key)
        # duplicates (explorer paging overlap): keep the most-confirmed copy
        if cur is None or t.confirmations > cur.confirmations:
            best[t.key] = t
    return sorted(
        best.values(),
        key=lambda t: (t.block_height is None, t.block_height or 0, t.tx_id, t.index),
    )


def evaluate(
    transfers: Iterable[Transfer],
    *,
    asset: str,
    network: str,
    address: str,
    expected_amount: int,
    decimals: int,
    required_confirmations: int,
    tolerance: Tolerance | None = None,
    not_before: datetime | None = None,
    memo: str | None = None,
    match_mode: MatchMode = MatchMode.SUM,
    previous: PaymentStatus | None = None,
) -> PaymentStatus:
    if expected_amount <= 0:
        raise ValueError("expected_amount must be positive")
    if required_confirmations < 0:
        raise ValueError("required_confirmations must be >= 0")
    tol = tolerance or Tolerance.exact()
    low, high = tol.band(expected_amount, decimals)
    txs = _normalise(transfers, not_before, memo)
    confirmed_txs = [t for t in txs if t.confirmations >= required_confirmations]

    if match_mode is MatchMode.SINGLE:
        # Amount discriminator: only a single credit inside the band counts.
        # Anything else on the shared address may belong to another payment,
        # so it is ignored rather than reported as under/over.
        cands = [t for t in txs if low <= t.amount <= high]
        conf_cands = [t for t in cands if t.confirmations >= required_confirmations]
        if conf_cands:
            pick, state = conf_cands[0], State.CONFIRMED
        elif cands:
            pick, state = cands[0], State.SEEN
        else:
            pick, state = None, State.NONE
        counted = [pick] if pick else []
        received = pick.amount if pick else 0
        confirmed = pick.amount if state is State.CONFIRMED and pick else 0
        match = Match.EXACT if pick else Match.NONE
    else:
        received = sum(t.amount for t in txs)
        confirmed = sum(t.amount for t in confirmed_txs)
        if not txs:
            state, match, counted = State.NONE, Match.NONE, []
        elif confirmed >= low:
            state = State.CONFIRMED
            match = Match.OVER if confirmed > high else Match.EXACT
            counted = confirmed_txs
        else:
            state = State.SEEN
            counted = txs
            if received < low:
                match = Match.UNDER
            elif received > high:
                match = Match.OVER
            else:
                match = Match.EXACT

    reverted = bool(
        previous is not None
        and previous.state is State.CONFIRMED
        and state is not State.CONFIRMED
    )
    return PaymentStatus(
        asset=asset,
        network=network,
        address=address,
        state=state,
        match=match,
        expected_amount=expected_amount,
        received_amount=received,
        confirmed_amount=confirmed,
        required_confirmations=required_confirmations,
        confirmations=min((t.confirmations for t in counted), default=0),
        tx_ids=tuple(dict.fromkeys(t.tx_id for t in counted)),
        decimals=decimals,
        reverted=reverted,
        memo=memo,
        transfers=tuple(counted),
    )


def unique_amount(base: Decimal | str, slot: int, step: Decimal | str, decimals: int) -> Decimal:
    """Amount discriminator for single-address mode: ``base + slot*step``.

    E.g. ``unique_amount("10", 37, "0.000001", 6) == Decimal("10.000037")``.
    The host owns slot allocation (and must not reuse a slot while a payment
    using it is still open).
    """
    if slot < 0:
        raise ValueError("slot must be >= 0")
    q = Decimal(1).scaleb(-decimals)
    value = Decimal(str(base)) + Decimal(str(step)) * slot
    if value != value.quantize(q):
        raise ValueError("discriminated amount exceeds the asset's precision")
    return value.quantize(q)
