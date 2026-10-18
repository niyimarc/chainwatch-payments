"""Signals sent once per status change, after the database transaction commits.

Every signal is sent with ``sender=<your invoice model class>`` and keyword
arguments ``invoice``, ``status`` (the chain-level ``PaymentStatus``) and
``previous`` (the old status string).
"""

from django.dispatch import Signal

from .models import InvoiceStatus

invoice_status_changed = Signal()   # any change
invoice_confirming = Signal()       # -> confirming
invoice_partially_paid = Signal()   # -> partially_paid
invoice_paid = Signal()             # -> paid or overpaid (check invoice.status / invoice.paid_late)
invoice_underpaid = Signal()        # -> underpaid
invoice_expired = Signal()          # -> expired
invoice_reverted = Signal()         # -> reverted (a confirmed payment was reorged out)

# Not an invoice status change. Sent with asset, network, unused_run, limit when
# that many HD invoices in a row never received anything (wallet gap limit).
gap_limit_warning = Signal()

# Sent by the opt-in forwarder with record (ForwardRecord) after a payment was
# moved to your wallet, and forward_failed with record + error when it gave up.
invoice_forwarded = Signal()
forward_failed = Signal()

BY_STATUS = {
    InvoiceStatus.CONFIRMING: invoice_confirming,
    InvoiceStatus.PARTIALLY_PAID: invoice_partially_paid,
    InvoiceStatus.PAID: invoice_paid,
    InvoiceStatus.OVERPAID: invoice_paid,
    InvoiceStatus.UNDERPAID: invoice_underpaid,
    InvoiceStatus.EXPIRED: invoice_expired,
    InvoiceStatus.REVERTED: invoice_reverted,
}


def send_for(model, invoice, status, previous) -> None:
    kwargs = dict(invoice=invoice, status=status, previous=previous)
    invoice_status_changed.send(sender=model, **kwargs)
    specific = BY_STATUS.get(invoice.status)
    if specific is not None:
        specific.send(sender=model, **kwargs)
