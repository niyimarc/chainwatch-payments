"""Create invoices, refresh them from the chain, poll the open ones."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, models, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from ...errors import ChainwatchError, UnsupportedCapability
from ...pricing import PriceUnavailable
from ...types import Match, MatchMode, PaymentStatus, State, Tolerance, from_base_units, to_base_units
from . import conf
from .checkout import price_invoice
from .models import (
    ACTIVE_STATUSES,
    LATE_WATCH_STATUSES,
    PAID_STATUSES,
    AbstractCryptoInvoice,
    AddressMode,
    DerivationCounter,
    InvoiceEvent,
    InvoiceStatus,
)
from .signals import gap_limit_warning, send_for

__all__ = ["allocate_index", "create_invoice", "refresh_invoice", "poll_open_invoices", "PollResult",
           "invoices_to_poll", "unused_address_run", "SlotsExhausted", "InvoiceDecision", "decide"]

log = logging.getLogger("chainwatch_payments")
S = InvoiceStatus
_CENT = Decimal("0.0001")


class SlotsExhausted(ChainwatchError):
    """Single-address mode: every amount slot is taken by an open invoice."""


# -- index / slot allocation -------------------------------------------------------

def _locked_counter(asset: str, network: str, start: int) -> DerivationCounter:
    qs = DerivationCounter.objects.select_for_update()
    counter = qs.filter(asset=asset, network=network).first()
    if counter is None:
        try:
            with transaction.atomic():
                counter = DerivationCounter.objects.create(asset=asset, network=network, next_index=start)
        except IntegrityError:  # another process created it first
            counter = qs.get(asset=asset, network=network)
    return counter


def allocate_index(asset: str, network: str) -> int:
    """Return a never-before-used HD index for (asset, network).

    Uses a row lock (SELECT ... FOR UPDATE), so concurrent checkouts on
    PostgreSQL/MySQL never receive the same index. SQLite serialises writes,
    which gives the same guarantee there.
    """
    asset, network = asset.upper(), network.lower()
    start = int(conf.get("START_INDEX"))
    with transaction.atomic():
        counter = _locked_counter(asset, network, start)
        index = max(counter.next_index, start)
        counter.next_index = index + 1
        counter.save(update_fields=["next_index", "updated_at"])
        return index


def _allocate_slot(model, cfg: conf.BackendConfig, address: str, now: datetime) -> int:
    """Smallest amount slot not used by any invoice still being watched on this
    shared address. Must be called inside a transaction (it takes a lock)."""
    _locked_counter(cfg.asset, f"single:{cfg.network}"[:40], 0)   # serialises slot allocation
    late_cutoff = now - timedelta(hours=cfg.late_payment_hours)
    used = set(
        model._default_manager.filter(
            address_mode=AddressMode.SINGLE, asset=cfg.asset, network=cfg.network, address=address,
        ).filter(
            Q(status__in=ACTIVE_STATUSES)
            | Q(status__in=LATE_WATCH_STATUSES, expires_at__gte=late_cutoff)
        ).values_list("amount_slot", flat=True)
    )
    for slot in range(1, cfg.max_slots + 1):
        if slot not in used:
            return slot
    raise SlotsExhausted(f"all {cfg.max_slots} amount slots on {address} are in use")


# -- gap limit ---------------------------------------------------------------------

def unused_address_run(model, asset: str, network: str) -> int:
    """How many HD invoices in a row (highest indexes) never received anything.

    Wallets stop scanning after a run of unused addresses (the "gap limit",
    often 20); past that, later payments won't show in your wallet until you
    raise its gap limit.
    """
    asset, network = asset.upper(), network.lower()
    qs = model._default_manager.filter(asset=asset, network=network, address_mode=AddressMode.HD)
    last_used = qs.filter(received_amount__gt=0).aggregate(m=Max("derivation_index"))["m"]
    if last_used is not None:
        qs = qs.filter(derivation_index__gt=last_used)
    return qs.count()


def _check_gap(model, asset: str, network: str) -> None:
    limit = int(conf.get("GAP_LIMIT_WARNING") or 0)
    if limit <= 0:
        return
    run = unused_address_run(model, asset, network)
    if run >= limit and run % limit == 0:
        log.warning("chainwatch: %s %s:%s has %d unused addresses in a row; raise your wallet's gap limit",
                    model.__name__, asset, network, run)
        transaction.on_commit(lambda: gap_limit_warning.send(sender=model, asset=asset, network=network,
                                                             unused_run=run, limit=limit))


# -- creation ----------------------------------------------------------------------

def create_invoice(
    model: type[AbstractCryptoInvoice],
    *,
    asset: str,
    network: str,
    fiat_price: Decimal | str | int,
    fiat_currency: str = "USD",
    now: datetime | None = None,
    **fields,
) -> AbstractCryptoInvoice:
    """Price, address and save a new invoice in one go.

    1. fetches the rate (raises ``PriceUnavailable``; nothing is created),
    2. estimates the network fee (dynamic estimator if configured, never below
       the static ``network_fee``),
    3. computes ``total_price`` = (price * (1 + buffer) + fee) / rate, rounded up,
    4. HD mode: allocates the next index and derives its address;
       single mode: picks a free amount slot on the shared address,
    5. records the chain height (scan start hint) and saves the row.

    Extra keyword arguments are your own model fields (``user=...``, ``plan=...``).
    """
    asset, network, fiat_currency = asset.upper(), network.lower(), fiat_currency.upper()
    cfg = conf.backend_config(asset, network)
    caps = cfg.backend.capabilities()
    registry = conf.get_registry()
    q = price_invoice(cfg, fiat_price, fiat_currency)
    try:
        start_height = registry.current_height(asset, network)
    except ChainwatchError as exc:  # optional hint only; never block a checkout
        log.info("chainwatch: no start height for %s:%s (%s)", asset, network, exc)
        start_height = None
    now = now or timezone.now()
    common = dict(
        asset=asset, network=network, fiat_currency=fiat_currency, fiat_price=q.fiat_price,
        volatility_buffer=q.volatility_buffer, network_fee_fiat=q.network_fee_fiat, rate=q.rate,
        start_height=start_height, created_at=now,
        expires_at=now + timedelta(minutes=int(conf.get("QUOTE_MINUTES"))),
    )
    with transaction.atomic():
        if cfg.address_mode == "single":
            step = cfg.amount_step
            if step is None:  # pragma: no cover - conf validates this
                raise UnsupportedCapability("single-address mode needs amount_step")
            address = registry.receive_address(asset, network)
            slot = _allocate_slot(model, cfg, address, now)
            total = (q.total_amount + step * slot).quantize(Decimal(1).scaleb(-caps.decimals))
            invoice = model(address_mode=AddressMode.SINGLE, address=address, amount_slot=slot,
                            total_price=total, **common, **fields)
        else:
            index = allocate_index(asset, network)
            address = registry.derive_address(asset, network, index)
            invoice = model(address_mode=AddressMode.HD, derivation_index=index, address=address,
                            total_price=q.total_amount, **common, **fields)
        invoice.save(force_insert=True)
        if cfg.address_mode == "hd":
            _check_gap(model, asset, network)
    return invoice


# -- status logic ------------------------------------------------------------------

@dataclass
class InvoiceDecision:
    status: str
    late: bool


def _covered_on_time(row, ps: PaymentStatus, low_base: int, decimals: int) -> bool:
    """Was the full amount on chain by the deadline?"""
    if row.fully_seen_at is not None and row.fully_seen_at <= row.expires_at:
        return True
    # the poller may have missed the moment: use block timestamps
    on_time = sum(t.amount for t in ps.transfers
                  if t.timestamp is not None and t.timestamp <= row.expires_at)
    return on_time >= low_base


def decide(row, ps: PaymentStatus, cfg: conf.BackendConfig, now: datetime) -> InvoiceDecision:
    """Map a chain result onto an invoice status.

    * Covered before expiry (even if confirmations finish later): judged on the
      locked crypto amount, as quoted.
    * Arrived after expiry, ``late_payment_pricing="at_payment"`` (default):
      judged on what the payment was WORTH when it was detected
      (``payment_rate``). It must cover ``fiat_price + network_fee``.
    * Arrived after expiry, ``"locked"``: judged on the locked crypto amount.
    Single-address invoices always use the locked amount (the amount is the
    invoice's identity there).
    """
    previous = row.status
    expired = now >= row.expires_at
    decimals = ps.decimals
    under_base = to_base_units(cfg.underpay_tolerance, decimals)
    low_base = max(ps.expected_amount - under_base, 0)
    has_funds = ps.received_amount > 0
    was_paid = previous in PAID_STATUSES or previous == S.REVERTED

    late = bool(expired and has_funds and not _covered_on_time(row, ps, low_base, decimals))
    reprice = (late and cfg.late_payment_pricing == "at_payment"
               and row.address_mode == AddressMode.HD and row.payment_rate is not None)

    if reprice:
        rate = Decimal(row.payment_rate)
        due = row.amount_due_fiat - cfg.underpay_tolerance * rate
        confirmed_value = from_base_units(ps.confirmed_amount, decimals) * rate
        received_value = from_base_units(ps.received_amount, decimals) * rate
        if confirmed_value >= due:
            new = S.OVERPAID if confirmed_value > row.quoted_total_fiat else S.PAID
        elif was_paid:
            new = S.REVERTED
        elif received_value >= due:
            new = S.CONFIRMING
        else:
            new = S.UNDERPAID
    elif ps.state is State.CONFIRMED:
        new = S.OVERPAID if ps.match is Match.OVER else S.PAID
    elif was_paid:
        new = S.REVERTED
    elif ps.state is State.SEEN:
        if ps.match is Match.UNDER:
            new = S.UNDERPAID if expired else S.PARTIALLY_PAID
        else:
            new = S.CONFIRMING
    else:
        new = S.EXPIRED if expired else S.PENDING
    return InvoiceDecision(new, late)


_UPDATE_FIELDS = ["status", "received_amount", "confirmed_amount", "confirmations", "tx_ids", "paid_late",
                  "paid_at", "last_checked_at", "payment_detected_at", "fully_seen_at", "payment_rate",
                  "payment_rate_at", "payment_value_fiat"]


def _read_chain(invoice, cfg: conf.BackendConfig) -> PaymentStatus:
    registry = conf.get_registry()
    tolerance = Tolerance.absolute(cfg.underpay_tolerance, 0)
    if invoice.address_mode == AddressMode.SINGLE:
        lookback = timedelta(minutes=float(conf.get("SINGLE_ADDRESS_LOOKBACK_MINUTES")))
        return registry.confirm(
            invoice.asset, invoice.network, invoice.address, invoice.total_price, cfg.confirmations,
            invoice.created_at - lookback, tolerance=tolerance, match_mode=MatchMode.SINGLE,
            since_height=invoice.start_height,
        )
    # One-time HD address: every credit belongs to this invoice, so no
    # not_before filter (block timestamps can legitimately precede created_at).
    return registry.confirm(
        invoice.asset, invoice.network, invoice.address, invoice.total_price, cfg.confirmations,
        tolerance=tolerance, since_height=invoice.start_height,
    )


def refresh_invoice(invoice: AbstractCryptoInvoice, *, now: datetime | None = None) -> AbstractCryptoInvoice:
    """Read the chain for this invoice and update it.

    Raises ``ChainSourceError`` if the chain can't be read, or
    ``PriceUnavailable`` if a late payment needs a price that can't be fetched;
    in both cases nothing is changed and the next poll retries.
    Signals fire once per status change, after commit. Safe to call
    concurrently: the row is locked while the new status is decided.
    """
    cfg = conf.backend_config(invoice.asset, invoice.network)
    ps = _read_chain(invoice, cfg)
    now = now or timezone.now()
    has_funds = ps.received_amount > 0

    # price at payment time: fetched outside the row lock (it's a network call)
    rate_now = None
    if has_funds and invoice.payment_rate is None:
        try:
            rate_now = conf.get_price_provider().rate(invoice.asset, invoice.fiat_currency)
        except PriceUnavailable as exc:
            expired = now >= invoice.expires_at
            needed = expired and cfg.late_payment_pricing == "at_payment" and invoice.address_mode == AddressMode.HD
            if needed:
                raise
            log.info("chainwatch: payment-time rate for invoice #%s not available yet (%s)", invoice.pk, exc)

    model = type(invoice)
    with transaction.atomic():
        row = model._default_manager.select_for_update().get(pk=invoice.pk)
        previous = row.status
        if has_funds and row.payment_detected_at is None:
            row.payment_detected_at = now
        if rate_now is not None and row.payment_rate is None:
            row.payment_rate, row.payment_rate_at = rate_now, now
        if has_funds and row.fully_seen_at is None and ps.state is not State.NONE and ps.match is not Match.UNDER:
            row.fully_seen_at = now

        decision = decide(row, ps, cfg, now)
        row.status = decision.status
        row.received_amount = ps.received
        row.confirmed_amount = ps.confirmed
        row.confirmations = ps.confirmations
        row.tx_ids = list(ps.tx_ids)
        row.last_checked_at = now
        if row.payment_rate is not None:
            row.payment_value_fiat = (ps.received * Decimal(row.payment_rate)).quantize(_CENT)
        if decision.status in PAID_STATUSES and previous not in PAID_STATUSES:
            row.paid_at = now
            if previous != S.REVERTED:
                row.paid_late = decision.late
        row.save(update_fields=_UPDATE_FIELDS)
        if decision.status != previous:
            InvoiceEvent.objects.create(
                invoice_model=model._meta.label_lower, invoice_pk=str(row.pk), asset=row.asset,
                network=row.network, address=row.address, previous_status=previous, status=row.status,
                received_amount=row.received_amount, confirmed_amount=row.confirmed_amount,
                confirmations=row.confirmations, tx_ids=row.tx_ids, payment_rate=row.payment_rate,
                payment_value_fiat=row.payment_value_fiat, paid_late=row.paid_late, created_at=now,
            )
            transaction.on_commit(lambda: send_for(model, row, ps, previous))
    for name in _UPDATE_FIELDS:
        setattr(invoice, name, getattr(row, name))
    return invoice


# -- polling -----------------------------------------------------------------------

@dataclass
class PollResult:
    checked: int = 0
    changed: int = 0
    errors: int = 0
    failures: list[tuple[object, str]] = field(default_factory=list)


def invoices_to_poll(model: type[AbstractCryptoInvoice], now: datetime | None = None) -> models.QuerySet:
    """Which invoices need checking now.

    * open ones (pending, confirming, partially_paid, reverted): every pass;
    * expired/underpaid ones inside their chain's late-payment window: every
      ``LATE_RECHECK_SECONDS``;
    * paid ones inside ``REORG_WATCH_HOURS``: every ``PAID_RECHECK_SECONDS``.
    """
    now = now or timezone.now()
    late_due = Q(last_checked_at__isnull=True) | Q(last_checked_at__lte=now - timedelta(
        seconds=float(conf.get("LATE_RECHECK_SECONDS"))))
    paid_due = Q(last_checked_at__isnull=True) | Q(last_checked_at__lte=now - timedelta(
        seconds=float(conf.get("PAID_RECHECK_SECONDS"))))

    late_window = Q(pk__in=[])  # matches nothing
    for (asset, network), cfg in conf.backend_configs().items():
        late_window |= Q(asset=asset, network=network,
                         expires_at__gte=now - timedelta(hours=cfg.late_payment_hours))
    reorg_cutoff = now - timedelta(hours=float(conf.get("REORG_WATCH_HOURS")))

    return model._default_manager.filter(
        Q(status__in=ACTIVE_STATUSES)
        | (Q(status__in=LATE_WATCH_STATUSES) & late_window & late_due)
        | (Q(status__in=PAID_STATUSES, paid_at__gte=reorg_cutoff) & paid_due)
    ).order_by(F("last_checked_at").asc(nulls_first=True), "pk")


def _models(model) -> Iterable[type[AbstractCryptoInvoice]]:
    if model is not None:
        return [model]
    names = conf.get("INVOICE_MODELS") or []
    if not names:
        raise ImproperlyConfigured("pass a model or set CHAINWATCH['INVOICE_MODELS'] = ['app_label.ModelName']")
    return [apps.get_model(n) for n in names]


def poll_open_invoices(model: type[AbstractCryptoInvoice] | None = None, *,
                       now: datetime | None = None, limit: int | None = None) -> PollResult:
    """Refresh every invoice that needs checking now.

    One failing invoice (explorer down, rate limit, bad data) is logged and
    skipped; it never stops the run and is never marked as failed.
    """
    result = PollResult()
    for m in _models(model):
        qs = invoices_to_poll(m, now)
        if limit:
            qs = qs[:limit]
        for inv in qs:
            before = inv.status
            result.checked += 1
            try:
                refresh_invoice(inv, now=now)
            except Exception as exc:  # isolate per invoice
                result.errors += 1
                result.failures.append((inv.pk, str(exc)))
                log.warning("chainwatch: could not refresh %s #%s: %s", m.__name__, inv.pk, exc)
                continue
            if inv.status != before:
                result.changed += 1
    return result
