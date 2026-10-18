import time

from django.apps import apps
from django.core.management.base import BaseCommand

from chainwatch_payments.contrib.django import conf
from chainwatch_payments.contrib.django.forwarding import forward_paid_invoices


class Command(BaseCommand):
    help = ("Forward paid TRON payments from their one-time addresses to your wallet "
            "(opt-in; needs CHAINWATCH['FORWARDERS'] and the mnemonic env var).")

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", dest="models", help="app_label.ModelName (repeatable).")
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=float, default=None)
        parser.add_argument("--limit", type=int, default=None, help="Max invoices per chain per run.")
        parser.add_argument("--include-underpaid", action="store_true",
                            help="Also move funds of underpaid invoices whose late-payment window closed.")

    def handle(self, *args, models=None, loop=False, interval=None, limit=None, include_underpaid=False, **o):
        targets = [apps.get_model(m) for m in models] if models else [None]
        interval = interval if interval is not None else float(conf.get("POLL_INTERVAL_SECONDS"))
        while True:
            for model in targets:
                r = forward_paid_invoices(model, limit=limit, include_underpaid=include_underpaid)
                self.stdout.write(f"forwarded={r.forwarded} nothing={r.nothing} errors={r.errors}")
            if not loop:
                return
            time.sleep(interval)
