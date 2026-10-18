from django.contrib import admin, messages
from django.utils.html import format_html_join

from .models import DerivationCounter, ForwardEvent, ForwardRecord, InvoiceEvent

__all__ = ["CryptoInvoiceAdminMixin"]


class CryptoInvoiceAdminMixin:
    """Mix into your invoice's ModelAdmin:

        @admin.register(Invoice)
        class InvoiceAdmin(CryptoInvoiceAdminMixin, admin.ModelAdmin):
            pass
    """

    list_display = ("id", "asset", "network", "total_price", "fiat_price", "fiat_currency",
                    "status", "confirmations", "paid_late", "created_at", "expires_at")
    list_filter = ("status", "asset", "network", "paid_late", "address_mode")
    search_fields = ("address",)
    readonly_fields = ("asset", "network", "fiat_currency", "fiat_price", "volatility_buffer",
                       "network_fee_fiat", "rate", "total_price", "address_mode", "derivation_index",
                       "address", "amount_slot", "start_height", "status", "received_amount",
                       "confirmed_amount", "confirmations", "tx_ids", "paid_late", "payment_detected_at",
                       "fully_seen_at", "payment_rate", "payment_rate_at", "payment_value_fiat",
                       "created_at", "expires_at", "paid_at", "last_checked_at", "payment_history")
    actions = ["chainwatch_check_now"]

    @admin.display(description="Payment history")
    def payment_history(self, obj):
        rows = [(e.created_at.strftime("%Y-%m-%d %H:%M:%S"), e.previous_status, e.status,
                 e.received_amount, e.payment_value_fiat if e.payment_value_fiat is not None else "-")
                for e in obj.payment_events()] if obj.pk else []
        if not rows:
            return "-"
        return format_html_join("\n", "<div>{}: {} &rarr; {} (received {}, value {})</div>", rows)

    @admin.action(description="Check selected invoices on chain now")
    def chainwatch_check_now(self, request, queryset):
        ok = failed = 0
        for inv in queryset:
            try:
                inv.refresh_payment()
                ok += 1
            except Exception as exc:  # show, don't crash the admin
                failed += 1
                self.message_user(request, f"#{inv.pk}: {exc}", messages.WARNING)
        self.message_user(request, f"Checked {ok} invoice(s); {failed} failed.")


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DerivationCounter)
class DerivationCounterAdmin(_ReadOnly):
    list_display = ("asset", "network", "next_index", "updated_at")


@admin.register(InvoiceEvent)
class InvoiceEventAdmin(_ReadOnly):
    list_display = ("created_at", "invoice_model", "invoice_pk", "asset", "previous_status", "status",
                    "received_amount", "payment_value_fiat", "paid_late")
    list_filter = ("status", "asset", "network", "invoice_model")
    search_fields = ("invoice_pk", "address")


class ForwardEventInline(admin.TabularInline):
    model = ForwardEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "status", "attempt", "amount", "fee", "gas_tx", "transfer_tx", "message")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ForwardEvent)
class ForwardEventAdmin(_ReadOnly):
    list_display = ("created_at", "kind", "asset", "network", "amount", "fee", "from_address", "to_address",
                    "transfer_tx", "attempt")
    list_filter = ("kind", "asset", "network")
    search_fields = ("from_address", "to_address", "transfer_tx", "gas_tx", "record__invoice_pk")


@admin.register(ForwardRecord)
class ForwardRecordAdmin(_ReadOnly):
    inlines = [ForwardEventInline]
    list_display = ("created_at", "invoice_model", "invoice_pk", "asset", "amount", "to_address",
                    "status", "attempts", "transfer_tx")
    list_filter = ("status", "asset", "network")
    search_fields = ("invoice_pk", "from_address", "transfer_tx")
