import time

from django.apps import apps
from django.core.management.base import BaseCommand

from chainwatch_payments.contrib.django import conf
from chainwatch_payments.contrib.django.services import poll_open_invoices


class Command(BaseCommand):
    help = "Check open crypto invoices on chain and update their status."

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", dest="models",
                            help="app_label.ModelName (repeatable). Default: CHAINWATCH['INVOICE_MODELS'].")
        parser.add_argument("--loop", action="store_true", help="Keep running, polling every --interval seconds.")
        parser.add_argument("--interval", type=float, default=None,
                            help="Seconds between runs with --loop. Default: CHAINWATCH['POLL_INTERVAL_SECONDS'].")
        parser.add_argument("--limit", type=int, default=None, help="Max invoices per model per run.")

    def handle(self, *args, models=None, loop=False, interval=None, limit=None, **opts):
        targets = [apps.get_model(m) for m in models] if models else [None]
        interval = interval if interval is not None else float(conf.get("POLL_INTERVAL_SECONDS"))
        while True:
            for model in targets:
                r = poll_open_invoices(model, limit=limit)
                self.stdout.write(f"checked={r.checked} changed={r.changed} errors={r.errors}")
            if not loop:
                return
            time.sleep(interval)
