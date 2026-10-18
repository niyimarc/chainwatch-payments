"""Models: the abstract invoice your billing app inherits, plus small tables
this app owns (index counter, payment event log, forward records)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

__all__ = ["InvoiceStatus", "AddressMode", "AbstractCryptoInvoice", "DerivationCounter",
           "InvoiceEvent", "ForwardRecord", "ForwardStatus", "ForwardEvent", "ForwardEventKind",
           "PAID_STATUSES", "ACTIVE_STATUSES", "LATE_WATCH_STATUSES"]


class InvoiceStatus(models.TextChoices):
    PENDING = "pending", "Pending"                       # waiting for payment
    CONFIRMING = "confirming", "Confirming"              # enough sent, waiting for confirmations
    PARTIALLY_PAID = "partially_paid", "Partially paid"  # less than required so far, still open
    PAID = "paid", "Paid"
    OVERPAID = "overpaid", "Overpaid"                    # paid, with more than required
    UNDERPAID = "underpaid", "Underpaid"                 # expired with less than required
    EXPIRED = "expired", "Expired"                       # expired with nothing received
    REVERTED = "reverted", "Reverted"                    # was paid, then the tx was reorged out


class AddressMode(models.TextChoices):
    HD = "hd", "One-time address"
    SINGLE = "single", "Shared address + unique amount"


PAID_STATUSES = frozenset({InvoiceStatus.PAID, InvoiceStatus.OVERPAID})
ACTIVE_STATUSES = frozenset({InvoiceStatus.PENDING, InvoiceStatus.CONFIRMING,
                             InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.REVERTED})
LATE_WATCH_STATUSES = frozenset({InvoiceStatus.EXPIRED, InvoiceStatus.UNDERPAID})

# Crypto amounts: up to 18 decimal places (ERC-20 tokens can use 18).
_AMOUNT = dict(max_digits=38, decimal_places=18)
_FIAT = dict(max_digits=20, decimal_places=4)
_RATE = dict(max_digits=30, decimal_places=12)


class DerivationCounter(models.Model):
    """Next free HD index per (asset, network). Owned by this app; locked on use."""

    asset = models.CharField(max_length=16)
    network = models.CharField(max_length=40)
    next_index = models.PositiveBigIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "chainwatch"
        constraints = [models.UniqueConstraint(fields=["asset", "network"], name="chainwatch_counter_unique")]

    def __str__(self) -> str:
        return f"{self.asset}:{self.network} next={self.next_index}"


class AbstractCryptoInvoice(models.Model):
    """Inherit this in your billing app and add your own fields (user, plan, ...).

    Create rows with ``chainwatch_payments.contrib.django.services.create_invoice``
    rather than ``objects.create`` so index, address and price are consistent.
    """

    # what is being paid
    asset = models.CharField(max_length=16)
    network = models.CharField(max_length=32)
    fiat_currency = models.CharField(max_length=8, default="USD")
    fiat_price = models.DecimalField(**_FIAT)
    volatility_buffer = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    network_fee_fiat = models.DecimalField(**_FIAT, default=0)
    rate = models.DecimalField(**_RATE, help_text="Fiat value of 1 unit of the asset, locked at creation.")
    total_price = models.DecimalField(**_AMOUNT, help_text="Exact crypto amount the customer must send.")

    # where
    address_mode = models.CharField(max_length=8, choices=AddressMode.choices, default=AddressMode.HD,
                                    editable=False)
    derivation_index = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    address = models.CharField(max_length=128, editable=False, db_index=True)
    amount_slot = models.PositiveIntegerField(null=True, blank=True, editable=False,
                                              help_text="Single-address mode: the amount discriminator slot.")
    start_height = models.PositiveBigIntegerField(null=True, blank=True, editable=False,
                                                  help_text="Chain height at creation (scan start for ERC-20).")

    # lifecycle
    status = models.CharField(max_length=20, choices=InvoiceStatus.choices,
                              default=InvoiceStatus.PENDING, db_index=True)
    received_amount = models.DecimalField(**_AMOUNT, default=0)
    confirmed_amount = models.DecimalField(**_AMOUNT, default=0)
    confirmations = models.PositiveIntegerField(default=0)
    tx_ids = models.JSONField(default=list, blank=True)
    paid_late = models.BooleanField(default=False,
                                    help_text="Paid after expiry and accepted under the late-payment rule.")

    # price at the time the payment arrived
    payment_detected_at = models.DateTimeField(null=True, blank=True,
                                               help_text="When funds were first seen on chain.")
    fully_seen_at = models.DateTimeField(null=True, blank=True,
                                         help_text="When the full amount was first seen (confirmed or not).")
    payment_rate = models.DecimalField(**_RATE, null=True, blank=True,
                                       help_text="Fiat value of 1 unit when the payment was detected.")
    payment_rate_at = models.DateTimeField(null=True, blank=True)
    payment_value_fiat = models.DecimalField(**_FIAT, null=True, blank=True,
                                             help_text="received_amount x payment_rate.")

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(fields=["asset", "network", "derivation_index"],
                                    condition=Q(address_mode="hd"),
                                    name="%(app_label)s_%(class)s_unique_index"),
            models.UniqueConstraint(fields=["address"], condition=Q(address_mode="hd"),
                                    name="%(app_label)s_%(class)s_unique_address"),
        ]

    def __str__(self) -> str:
        return f"{self.total_price} {self.asset} to {self.address} ({self.status})"

    @property
    def is_paid(self) -> bool:
        return self.status in PAID_STATUSES

    @property
    def is_open(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def amount_due_fiat(self) -> Decimal:
        """What a late payment must be worth (price + network fee, no buffer)."""
        return Decimal(self.fiat_price) + Decimal(self.network_fee_fiat)

    @property
    def quoted_total_fiat(self) -> Decimal:
        return Decimal(self.fiat_price) * (1 + Decimal(self.volatility_buffer)) + Decimal(self.network_fee_fiat)

    def seconds_left(self, now: datetime | None = None) -> int:
        now = now or timezone.now()
        return max(int((self.expires_at - now).total_seconds()), 0)

    def refresh_payment(self, now: datetime | None = None):
        """Check the chain now and update this invoice (fires signals on change)."""
        from .services import refresh_invoice

        return refresh_invoice(self, now=now)

    def payment_events(self):
        """The audit trail of status changes for this invoice, oldest first."""
        return InvoiceEvent.objects.filter(invoice_model=self._meta.label_lower,
                                           invoice_pk=str(self.pk)).order_by("created_at", "pk")


class InvoiceEvent(models.Model):
    """One row per invoice status change: the audit trail for disputes."""

    invoice_model = models.CharField(max_length=100)   # "app_label.modelname"
    invoice_pk = models.CharField(max_length=64)
    asset = models.CharField(max_length=16)
    network = models.CharField(max_length=32)
    address = models.CharField(max_length=128)
    previous_status = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    received_amount = models.DecimalField(**_AMOUNT, default=0)
    confirmed_amount = models.DecimalField(**_AMOUNT, default=0)
    confirmations = models.PositiveIntegerField(default=0)
    tx_ids = models.JSONField(default=list, blank=True)
    payment_rate = models.DecimalField(**_RATE, null=True, blank=True)
    payment_value_fiat = models.DecimalField(**_FIAT, null=True, blank=True)
    paid_late = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "chainwatch"
        indexes = [models.Index(fields=["invoice_model", "invoice_pk"], name="chainwatch_event_invoice")]
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        return f"{self.invoice_model}#{self.invoice_pk}: {self.previous_status} -> {self.status}"


class ForwardStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    GAS_SENT = "gas_sent", "Gas sent"
    SENT = "sent", "Transfer sent"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class ForwardRecord(models.Model):
    """One row per invoice forwarded by the opt-in forwarder. Unique per
    invoice, so the same payment is never forwarded twice."""

    invoice_model = models.CharField(max_length=100)
    invoice_pk = models.CharField(max_length=64)
    asset = models.CharField(max_length=16)
    network = models.CharField(max_length=32)
    from_address = models.CharField(max_length=128)
    to_address = models.CharField(max_length=128)
    amount = models.DecimalField(**_AMOUNT, default=0, help_text="Amount that arrives at the destination.")
    fee = models.DecimalField(**_AMOUNT, default=0,
                              help_text="Network fee paid from the forwarded coins (BTC); "
                                        "gas paid separately is not included.")
    status = models.CharField(max_length=12, choices=ForwardStatus.choices, default=ForwardStatus.PENDING)
    gas_tx = models.CharField(max_length=128, blank=True, default="")
    transfer_tx = models.CharField(max_length=128, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "chainwatch"
        constraints = [models.UniqueConstraint(fields=["invoice_model", "invoice_pk"],
                                               name="chainwatch_forward_once")]

    def __str__(self) -> str:
        return f"{self.amount} {self.asset} {self.from_address} -> {self.to_address} ({self.status})"


class ForwardEventKind(models.TextChoices):
    SENT = "sent", "Broadcast, awaiting confirmation"
    DONE = "done", "Arrived"
    CONFIRMED = "confirmed", "Earlier broadcast confirmed"
    NOTHING = "nothing", "Balance already zero"
    DEFERRED = "deferred", "Waiting (fees above cap, funding not mined)"
    ERROR = "error", "Attempt failed, will retry"
    FAILED = "failed", "Gave up after max attempts"
    DROPPED = "dropped", "Broadcast never confirmed, will retry"


class ForwardEvent(models.Model):
    """One row per forwarding attempt or status change: the forwarding log.

    Self-contained on purpose (addresses, amounts and tx ids are copied in),
    so each row reads on its own in exports and audits.
    """

    record = models.ForeignKey(ForwardRecord, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=12, choices=ForwardEventKind.choices)
    status = models.CharField(max_length=12, choices=ForwardStatus.choices,
                              help_text="The record's status after this event.")
    attempt = models.PositiveIntegerField(default=0)
    asset = models.CharField(max_length=16)
    network = models.CharField(max_length=32)
    from_address = models.CharField(max_length=128)
    to_address = models.CharField(max_length=128)
    amount = models.DecimalField(**_AMOUNT, default=0)
    fee = models.DecimalField(**_AMOUNT, default=0)
    gas_tx = models.CharField(max_length=128, blank=True, default="")
    transfer_tx = models.CharField(max_length=128, blank=True, default="")
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "chainwatch"
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["record", "created_at"], name="chainwatch_fwdevent_record")]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.kind}: {self.amount} {self.asset} -> {self.to_address}"
