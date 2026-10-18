"""Opt-in: move paid TRON payments from their one-time addresses to your wallet.

Run ``python manage.py chainwatch_forward --loop`` in a SEPARATE process or
container that holds the forwarder mnemonic; your web app never needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from ...types import from_base_units
from . import conf
from .models import PAID_STATUSES, AddressMode, ForwardRecord, ForwardStatus, InvoiceStatus
from .services import _models
from .signals import forward_failed, invoice_forwarded

__all__ = ["ForwardRun", "forward_paid_invoices"]

log = logging.getLogger("chainwatch_payments.forwarder")
_FINAL = (ForwardStatus.DONE, ForwardStatus.FAILED)


@dataclass
class ForwardRun:
    forwarded: int = 0
    nothing: int = 0
    errors: int = 0
    failures: list[tuple[object, str]] = field(default_factory=list)


def _candidates(model, asset: str, network: str, include_underpaid: bool, now: datetime):
    statuses = set(PAID_STATUSES)
    qs = model._default_manager.filter(asset=asset, network=network, address_mode=AddressMode.HD)
    if include_underpaid:
        # underpaid invoices whose late-payment window has closed: the money is
        # still yours, move it too
        hours = conf.backend_config(asset, network).late_payment_hours
        statuses.add(InvoiceStatus.UNDERPAID)
        qs = qs.exclude(status=InvoiceStatus.UNDERPAID, expires_at__gte=now - timedelta(hours=hours))
    return qs.filter(status__in=statuses).order_by("paid_at", "pk")


def forward_paid_invoices(model=None, *, limit: int | None = None, include_underpaid: bool = False,
                          now: datetime | None = None) -> ForwardRun:
    """Forward every paid invoice on each chain in ``CHAINWATCH['FORWARDERS']``.

    Never forwards twice: one ``ForwardRecord`` per invoice (unique, row
    locked while working), and the on-chain balance is checked first; a zero
    balance just marks the record done.
    """
    run = ForwardRun()
    now = now or timezone.now()
    max_attempts = int(conf.get("FORWARD_MAX_ATTEMPTS"))
    for asset, network in conf.forwarder_keys():
        forwarder = conf.get_forwarder(asset, network)
        decimals = conf.backend_config(asset, network).backend.capabilities().decimals
        for m in _models(model):
            label = m._meta.label_lower
            final = set(ForwardRecord.objects.filter(invoice_model=label, status__in=_FINAL)
                        .values_list("invoice_pk", flat=True))
            done_here = 0
            for inv in _candidates(m, asset, network, include_underpaid, now):
                if str(inv.pk) in final:
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
                    if rec.status in _FINAL:
                        continue
                    rec.attempts += 1
                    try:
                        res = forwarder.forward(inv.derivation_index, inv.address, gas_tx_already=rec.gas_tx)
                    except Exception as exc:  # keep going with the others
                        rec.error = str(exc)[:2000]
                        rec.status = ForwardStatus.FAILED if rec.attempts >= max_attempts else ForwardStatus.PENDING
                        rec.save()
                        run.errors += 1
                        run.failures.append((inv.pk, str(exc)))
                        log.warning("forwarder: invoice %s #%s attempt %d failed: %s", label, inv.pk, rec.attempts, exc)
                        if rec.status == ForwardStatus.FAILED:
                            transaction.on_commit(lambda r=rec, i=inv, e=str(exc), mm=m: forward_failed.send(
                                sender=mm, invoice=i, record=r, error=e))
                        continue
                    rec.error = ""
                    rec.status = ForwardStatus.DONE
                    if res.status == "nothing":
                        run.nothing += 1
                        rec.error = "balance already zero (forwarded earlier or moved manually)"
                    else:
                        run.forwarded += 1
                        rec.amount = from_base_units(res.amount, decimals)
                        rec.gas_tx, rec.transfer_tx = res.gas_tx, res.transfer_tx
                        transaction.on_commit(lambda r=rec, i=inv, mm=m: invoice_forwarded.send(
                            sender=mm, invoice=i, record=r))
                    rec.save()
    return run
