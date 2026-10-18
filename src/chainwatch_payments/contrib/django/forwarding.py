"""Opt-in: move paid payments from their one-time addresses to your wallet.

Works for every chain in ``CHAINWATCH['FORWARDERS']`` (TRON TRC-20, BTC,
EVM ERC-20). Run ``python manage.py chainwatch_forward --loop`` in a SEPARATE
process or container that holds the forwarder mnemonic; your web app never
needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from ...forwarder.base import ForwardDeferred
from ...types import from_base_units
from . import conf
from .models import (
    PAID_STATUSES,
    AddressMode,
    ForwardEvent,
    ForwardEventKind,
    ForwardRecord,
    ForwardStatus,
    InvoiceStatus,
)
from .services import _models
from .signals import forward_failed, invoice_forwarded

__all__ = ["ForwardRun", "forward_paid_invoices"]

log = logging.getLogger("chainwatch_payments.forwarder")
_FINAL = (ForwardStatus.DONE, ForwardStatus.FAILED)


@dataclass
class ForwardRun:
    forwarded: int = 0      # completed this run (including "sent" ones that confirmed)
    sent: int = 0           # broadcast, waiting for confirmation (BTC, slow EVM)
    nothing: int = 0        # balance already zero
    deferred: int = 0       # fees above your cap etc.; retried later, not counted as failures
    errors: int = 0
    failures: list[tuple[object, str]] = field(default_factory=list)


def _candidates(model, asset: str, network: str, include_underpaid: bool, now: datetime):
    statuses = set(PAID_STATUSES)
    qs = model._default_manager.filter(asset=asset, network=network, address_mode=AddressMode.HD)
    if include_underpaid:
        hours = conf.backend_config(asset, network).late_payment_hours
        statuses.add(InvoiceStatus.UNDERPAID)
        qs = qs.exclude(status=InvoiceStatus.UNDERPAID, expires_at__gte=now - timedelta(hours=hours))
    return qs.filter(status__in=statuses).order_by("paid_at", "pk")


def _log(rec, kind: str, message: str = "") -> None:
    """Append one row to the forwarding log (same transaction as the record update)."""
    ForwardEvent.objects.create(
        record=rec, kind=kind, status=rec.status, attempt=rec.attempts, asset=rec.asset, network=rec.network,
        from_address=rec.from_address, to_address=rec.to_address, amount=rec.amount, fee=rec.fee,
        gas_tx=rec.gas_tx, transfer_tx=rec.transfer_tx, message=message[:2000],
    )


def _done(rec, model, invoice) -> None:
    rec.status = ForwardStatus.DONE
    transaction.on_commit(lambda r=rec, i=invoice, mm=model: invoice_forwarded.send(sender=mm, invoice=i, record=r))


def _check_sent(forwarder, model, label: str, asset: str, network: str, run: ForwardRun) -> None:
    """Resolve transfers that were broadcast but not yet confirmed."""
    for rec in ForwardRecord.objects.filter(invoice_model=label, asset=asset, network=network,
                                            status=ForwardStatus.SENT):
        try:
            state = forwarder.tx_state(rec.transfer_tx)
        except Exception as exc:  # network trouble: just look again next run
            log.info("forwarder: could not check %s: %s", rec.transfer_tx, exc)
            continue
        with transaction.atomic():
            locked = ForwardRecord.objects.select_for_update().get(pk=rec.pk)
            if locked.status != ForwardStatus.SENT:
                continue
            if state == "confirmed":
                invoice = model._default_manager.filter(pk=locked.invoice_pk).first()
                _done(locked, model, invoice)
                run.forwarded += 1
                locked.save()
                _log(locked, ForwardEventKind.CONFIRMED)
            elif state == "dropped":
                # never confirmed: retry; the on-chain balance check prevents double sends
                dropped_tx = locked.transfer_tx
                locked.status = ForwardStatus.PENDING
                locked.error = f"transfer {dropped_tx} was dropped; retrying"
                locked.save()
                _log(locked, ForwardEventKind.DROPPED, locked.error)
                locked.transfer_tx = ""
                locked.save()
            else:
                run.sent += 1


def forward_paid_invoices(model=None, *, limit: int | None = None, include_underpaid: bool = False,
                          now: datetime | None = None) -> ForwardRun:
    """Forward every paid invoice on each chain in ``CHAINWATCH['FORWARDERS']``.

    Never forwards twice: one ``ForwardRecord`` per invoice (unique, row
    locked while working), and the on-chain balance is checked before any
    send; a zero balance just marks the record done.
    """
    run = ForwardRun()
    now = now or timezone.now()
    max_attempts = int(conf.get("FORWARD_MAX_ATTEMPTS"))
    for asset, network in conf.forwarder_keys():
        forwarder = conf.get_forwarder(asset, network)
        decimals = conf.backend_config(asset, network).backend.capabilities().decimals
        for m in _models(model):
            label = m._meta.label_lower
            _check_sent(forwarder, m, label, asset, network, run)
            skip = set(ForwardRecord.objects.filter(invoice_model=label)
                       .filter(status__in=(*_FINAL, ForwardStatus.SENT)).values_list("invoice_pk", flat=True))
            done_here = 0
            for inv in _candidates(m, asset, network, include_underpaid, now):
                if str(inv.pk) in skip:
                    continue
                if limit and done_here >= limit:
                    break
                done_here += 1
                with transaction.atomic():
                    rec, _ = ForwardRecord.objects.select_for_update().get_or_create(
                        invoice_model=label, invoice_pk=str(inv.pk),
                        defaults=dict(asset=asset, network=network, from_address=inv.address,
                                      to_address=forwarder.destination),
                    )
                    if rec.status in (*_FINAL, ForwardStatus.SENT):
                        continue
                    try:
                        res = forwarder.forward(inv.derivation_index, inv.address, gas_tx_already=rec.gas_tx)
                    except ForwardDeferred as exc:
                        rec.error = f"deferred: {exc}"[:2000]
                        rec.save()
                        _log(rec, ForwardEventKind.DEFERRED, str(exc))
                        run.deferred += 1
                        log.info("forwarder: invoice %s #%s deferred: %s", label, inv.pk, exc)
                        continue
                    except Exception as exc:  # keep going with the others
                        rec.attempts += 1
                        rec.error = str(exc)[:2000]
                        rec.status = ForwardStatus.FAILED if rec.attempts >= max_attempts else ForwardStatus.PENDING
                        rec.save()
                        _log(rec, ForwardEventKind.FAILED if rec.status == ForwardStatus.FAILED
                             else ForwardEventKind.ERROR, str(exc))
                        run.errors += 1
                        run.failures.append((inv.pk, str(exc)))
                        log.warning("forwarder: invoice %s #%s attempt %d failed: %s", label, inv.pk, rec.attempts, exc)
                        if rec.status == ForwardStatus.FAILED:
                            transaction.on_commit(lambda r=rec, i=inv, e=str(exc), mm=m: forward_failed.send(
                                sender=mm, invoice=i, record=r, error=e))
                        continue
                    rec.attempts += 1
                    rec.error = ""
                    if res.status == "nothing":
                        run.nothing += 1
                        rec.status = ForwardStatus.DONE
                        rec.error = "balance already zero (forwarded earlier or moved manually)"
                        kind = ForwardEventKind.NOTHING
                    else:
                        rec.amount = from_base_units(res.amount, decimals)
                        rec.fee = from_base_units(res.fee, decimals)
                        rec.gas_tx, rec.transfer_tx = res.gas_tx, res.transfer_tx
                        if res.status == "sent":
                            rec.status = ForwardStatus.SENT
                            run.sent += 1
                            kind = ForwardEventKind.SENT
                        else:
                            _done(rec, m, inv)
                            run.forwarded += 1
                            kind = ForwardEventKind.DONE
                    rec.save()
                    _log(rec, kind, rec.error)
    return run
