# Django integration

This guide covers the optional Django integration of
[chainwatch-payments](README.md): installing the app, every setting,
the invoice model, creating and polling invoices, signals, late-payment
pricing, single-address mode, the admin and the opt-in TRON forwarder.

For the framework-neutral core (backends, `PaymentRegistry.confirm`, pricing
and fee helpers), see the [README](README.md).

## Contents

- [1. Install the app](#1-install-the-app)
- [2. Configure CHAINWATCH](#2-configure-chainwatch)
- [3. Inherit the invoice model](#3-inherit-the-invoice-model)
- [4. Create an invoice at checkout](#4-create-an-invoice-at-checkout)
- [5. Poll in the background](#5-poll-in-the-background)
- [6. React to payments with signals](#6-react-to-payments-with-signals)
- [Statuses](#statuses)
- [Late payments and price risk](#late-payments-and-price-risk)
- [Single-address mode](#single-address-mode)
- [Gap limit](#gap-limit)
- [Audit trail](#audit-trail)
- [Admin](#admin)
- [Forwarding (opt-in, TRON)](#forwarding-opt-in-tron)
- [Security notes](#security-notes)

```
pip install "chainwatch-payments[django,bitcoin,tron]"
```

The `[django]` extra adds a reusable Django app. It:

* creates crypto invoices (price locked, one fresh address each);
* watches them on chain and keeps their status up to date;
* records the price at the moment each payment arrives;
* keeps an audit trail of every change;
* tells your billing app what happened through signals.

Your app keeps everything about users, plans and subscriptions. It supports
Django 4.2+.

## 1. Install the app

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "chainwatch_payments.contrib.django",
    "billing",
]
```

Then run `python manage.py migrate`. This creates three small tables:

* `chainwatch_derivationcounter`: hands out address indexes;
* `chainwatch_invoiceevent`: the audit trail;
* `chainwatch_forwardrecord`: used only by the optional forwarder.

## 2. Configure `CHAINWATCH`

Everything is configured here; nothing is stored in the database. Read secrets
(xpubs, API keys, the forwarder mnemonic) from environment variables.

```python
import os

CHAINWATCH = {
    "BACKENDS": {
        "BTC:mainnet": {
            "type": "bitcoin",
            "options": {"xpub": os.environ["BTC_ZPUB"], "network": "mainnet"},
            "volatility_buffer": "0.01",      # +1% during the quote window
            "network_fee": "0.50",            # minimum fee, in the invoice's fiat currency
            "fee_estimator": {"type": "mempool"},
            "late_payment_hours": 24,         # BTC: shorter late window than the default 72 h
        },
        "USDT:tron": {
            "type": "tron_trc20",
            "options": {"xpub": os.environ["TRON_XPUB"], "network": "mainnet",
                        "api_key": os.environ.get("TRONGRID_API_KEY")},
            "network_fee": "1.50",
            "fee_estimator": {"type": "tron_energy",
                              "options": {"api_key": os.environ.get("TRONGRID_API_KEY")}},
            "underpay_tolerance": "0.01",     # accept up to 0.01 USDT short
        },
    },
    "PRICE_PROVIDER": "coingecko",
    "PRICE_API_KEY": os.environ.get("COINGECKO_API_KEY"),
    "PRICE_CROSS_CHECK": "kraken",
    "INVOICE_MODELS": ["billing.Invoice"],
}
```

### Settings reference

A misspelled setting name raises `ImproperlyConfigured`. Typos are never
silently ignored. Amounts and percentages must be strings or `Decimal`
(`"0.01"`), never floats.

| Setting | Default | Meaning |
|---|---|---|
| `BACKENDS` | `{}` | Enabled chains, keyed `"ASSET:network"` (or a tuple). One entry per chain; see the next table. |
| `PRICE_PROVIDER` | `"coingecko"` | `"coingecko"`, `"fixed"` (`FIXED_RATES` only), a dotted path to a provider class or factory, or a provider instance. `FIXED_RATES` is always tried first. |
| `PRICE_API_KEY` | `None` | CoinGecko key. Without a key the public, rate-limited API is used. |
| `PRICE_API_PLAN` | `None` | `"public"`, `"demo"` or `"pro"`. `None` means `"demo"` when a key is set, otherwise `"public"`. |
| `PRICE_CACHE_SECONDS` | `60` | How long a fetched rate is reused. |
| `FIXED_RATES` | `{"USDT:USD": "1", "USDC:USD": "1"}` | Rates that never need an API call (and skip the cross-check), keyed `"ASSET:FIAT"`. |
| `PRICE_CROSS_CHECK` | `None` | A second source to compare against: `"kraken"`, a dotted path, or an instance. |
| `PRICE_MAX_DEVIATION` | `"0.02"` | Refuse to quote when the two sources differ by more than this fraction. |
| `PRICE_REQUIRE_CROSS_CHECK` | `False` | If `True`, refuse to quote when the cross-check source is down. If `False`, log a warning and use the primary. |
| `QUOTE_MINUTES` | `20` | How long a quote is valid. |
| `START_INDEX` | `1` | First address index. Index 0 is left for your wallet's own first address (and is the forwarder's gas wallet). |
| `LATE_PAYMENT_HOURS` | `72` | Default late-payment window (each chain can override it). |
| `LATE_PAYMENT_PRICING` | `"at_payment"` | Default late-payment rule: `"at_payment"` or `"locked"` (see below; each chain can override it). |
| `REORG_WATCH_HOURS` | `6` | How long paid invoices keep being re-checked in case a payment is reversed. |
| `GAP_LIMIT_WARNING` | `20` | Send `gap_limit_warning` when this many invoices in a row never received anything. `0` turns it off. |
| `SINGLE_ADDRESS_LOOKBACK_MINUTES` | `10` | Single-address mode: also count payments up to this long before the invoice was created (clock skew). |
| `INVOICE_MODELS` | `[]` | `"app_label.ModelName"` of your invoice model(s), used by the commands. |
| `POLL_INTERVAL_SECONDS` | `30` | Seconds between runs with `--loop`. |
| `LATE_RECHECK_SECONDS` | `300` | Expired/underpaid invoices are re-checked at most this often. |
| `PAID_RECHECK_SECONDS` | `600` | Paid invoices (reorg watch) are re-checked at most this often. |
| `FORWARDERS` | `{}` | Opt-in forwarder configuration; see "Forwarding". |
| `FORWARD_MAX_ATTEMPTS` | `5` | After this many failed attempts a forward is marked `failed` and left for you. |

Each `BACKENDS` entry takes these keys:

| Key | Default | Meaning |
|---|---|---|
| `type` | required, or `backend` | `"bitcoin"`, `"tron_trc20"`, `"erc20"`, `"mock"`, or a dotted path to your own backend class. |
| `options` | `{}` | Keyword arguments for that backend class, e.g. `xpub`, `network`, `address`, `api_key`, `rpc_url`, `contract`, `decimals`. |
| `backend` | | A ready-made backend instance, instead of `type`. |
| `volatility_buffer` | `0` | Fraction added to the price, e.g. `"0.01"` = +1%. Use for volatile coins. |
| `network_fee` | `0` | Fee in the invoice's fiat currency, added to the price. With an estimator, this is the minimum. |
| `fee_estimator` | `None` | `{"type": "mempool" \| "tron_energy" \| dotted path, "options": {...}}` or an instance. |
| `fee_margin` | `"0.2"` | Safety margin on the estimate (+20%). |
| `underpay_tolerance` | `0` | How much short (in coin units) still counts as paid, e.g. for exchange withdrawal fees. |
| `confirmations` | backend default | Confirmations required (BTC 2, TRON 19, ERC-20 12). |
| `late_payment_hours` | `LATE_PAYMENT_HOURS` | Late-payment window for this chain. |
| `late_payment_pricing` | `LATE_PAYMENT_PRICING` | `"at_payment"` or `"locked"` for this chain. |
| `address_mode` | `"hd"` | `"hd"` (one address per invoice, needs `xpub`) or `"single"` (one shared address, needs `address`). |
| `amount_step` | smallest unit | Single mode: amount added per slot, e.g. `"0.0001"`. Must be larger than `underpay_tolerance`. |
| `max_slots` | `1000` | Single mode: how many invoices can be open on the address at once. |

## 3. Inherit the invoice model

```python
# billing/models.py
from chainwatch_payments.contrib.django.models import AbstractCryptoInvoice

class Invoice(AbstractCryptoInvoice):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    plan = models.ForeignKey("billing.Plan", on_delete=models.PROTECT)

    class Meta(AbstractCryptoInvoice.Meta):
        pass
```

Then run `python manage.py makemigrations billing` and `migrate`. If you
declare your own `Meta`, inherit `AbstractCryptoInvoice.Meta` as above so the
uniqueness constraints are kept. Those constraints are conditional, so use
PostgreSQL or SQLite; MySQL ignores conditional constraints (the index
allocation still prevents duplicates there).

| Field | Meaning |
|---|---|
| `asset`, `network` | e.g. `BTC` / `mainnet` |
| `fiat_currency`, `fiat_price` | what the customer is paying for, e.g. `USD` / `20.00` |
| `volatility_buffer`, `network_fee_fiat`, `rate` | the quote inputs, locked at creation |
| `total_price` | **the exact crypto amount the customer must send** |
| `address_mode`, `derivation_index`, `address`, `amount_slot` | where to pay (`amount_slot` only in single mode) |
| `start_height` | chain height at creation (ERC-20 scans start here) |
| `status` | see the status table below |
| `received_amount`, `confirmed_amount`, `confirmations`, `tx_ids` | what the chain shows |
| `payment_detected_at`, `fully_seen_at` | when funds, and the full amount, were first seen |
| `payment_rate`, `payment_rate_at`, `payment_value_fiat` | **the price when the payment arrived, and what it was worth** |
| `paid_late` | `True` if it was paid after expiry and accepted |
| `created_at`, `expires_at`, `paid_at`, `last_checked_at` | timestamps |

The model also has:
* `is_paid`, `is_open` and `seconds_left()`;
* `amount_due_fiat` (price + fee) and `quoted_total_fiat` (with buffer);
* `refresh_payment()`, which checks the chain now;
* `payment_events()`, the audit trail.

## 4. Create an invoice at checkout

```python
from chainwatch_payments.contrib.django.services import create_invoice
from chainwatch_payments import PriceUnavailable

try:
    invoice = create_invoice(Invoice, asset="BTC", network="mainnet",
                             fiat_price=plan.price, fiat_currency="USD",
                             user=request.user, plan=plan)
except PriceUnavailable:
    ...  # show "crypto payments are temporarily unavailable"

# show invoice.address, invoice.total_price and invoice.seconds_left()
```

In one transaction this:
* fetches (and cross-checks) the rate and estimates the fee;
* calculates `total_price`;
* takes the next free index, row-locked so two checkouts at the same moment
  never share an address (tested with 60 simultaneous checkouts on
  PostgreSQL);
* derives the address and records the chain height.

If the price can't be fetched, nothing is created. Always create invoices this
way, not with `Invoice.objects.create`.

## 5. Poll in the background

```
python manage.py chainwatch_poll                 # one pass (cron)
python manage.py chainwatch_poll --loop          # keep running (Docker/systemd)
python manage.py chainwatch_poll --model billing.Invoice --limit 500
```

With Celery or another scheduler, call
`chainwatch_payments.contrib.django.services.poll_open_invoices()` from a
periodic task instead. Each pass checks:

* open invoices (`pending`, `confirming`, `partially_paid`, `reverted`):
  every pass;
* `expired` and `underpaid` invoices inside their chain's late-payment window:
  at most every `LATE_RECHECK_SECONDS`;
* `paid` and `overpaid` invoices inside `REORG_WATCH_HOURS`: at most every
  `PAID_RECHECK_SECONDS`.

One failing invoice (explorer down, rate limit) is logged and skipped. It
never stops the run and is never marked as failed. Rate limits and transient
errors are also retried automatically with backoff before giving up.

## 6. React to payments with signals

```python
# billing/receivers.py  (import it from your AppConfig.ready())
from django.dispatch import receiver
from chainwatch_payments.contrib.django.signals import invoice_paid, invoice_reverted

@receiver(invoice_paid, sender=Invoice)
def activate(sender, invoice, status, previous, **kwargs):
    invoice.user.subscription.activate(invoice.plan)

@receiver(invoice_reverted, sender=Invoice)
def suspend(sender, invoice, **kwargs):
    invoice.user.subscription.suspend()
```

Each signal fires **once per status change**, after the database transaction
commits, never on every poll. Status signals receive `invoice`, `status` (the
chain-level `PaymentStatus`) and `previous` (the old status).

| Signal | Sent when |
|---|---|
| `invoice_status_changed` | any new status |
| `invoice_confirming` | status becomes `confirming` |
| `invoice_partially_paid` | status becomes `partially_paid` |
| `invoice_paid` | status becomes `paid` or `overpaid` (check `invoice.status`, `invoice.paid_late`) |
| `invoice_underpaid` | status becomes `underpaid` |
| `invoice_expired` | status becomes `expired` |
| `invoice_reverted` | status becomes `reverted` |
| `gap_limit_warning` | `GAP_LIMIT_WARNING` HD invoices in a row received nothing (args: `asset`, `network`, `unused_run`, `limit`) |
| `invoice_forwarded` / `forward_failed` | the forwarder moved a payment / gave up (args: `invoice`, `record`, and `error` for failures) |

## Statuses

| Status | Meaning |
|---|---|
| `pending` | Waiting for payment, not yet expired. |
| `confirming` | The full amount has arrived and is waiting for confirmations. |
| `partially_paid` | Less than `total_price` so far, not yet expired. More can still be sent. |
| `paid` | Confirmed and enough (see the rules below). |
| `overpaid` | Confirmed, more than required. Counts as paid; refunds are up to you. |
| `underpaid` | Expired with not enough. Still watched for a top-up. |
| `expired` | Expired with nothing received. Still watched for a late payment. |
| `reverted` | Was paid, then the payment was reorged out. Goes back to `paid` if it re-confirms. |

## Late payments and price risk

**On time:** the full amount was on chain before `expires_at`, even if
confirmations finish later. Such a payment is judged against the **locked
crypto amount** (`total_price`), exactly as quoted.

**Late:** the full amount arrived after `expires_at`, but within the chain's
`late_payment_hours`. How it's judged depends on `late_payment_pricing`:

* **`"at_payment"` (default):** the package records the price when it first
  detects the payment (`payment_rate`). The payment is accepted if, at that
  price, it is worth at least `fiat_price + network_fee_fiat`. So:
  * if BTC fell after the quote, the locked amount is no longer enough and the
    invoice stays `underpaid` until the customer tops up;
  * if BTC rose, a smaller amount is enough.

  Accepted late payments are `paid` with `paid_late=True`. If the price can't
  be fetched, nothing changes and the next poll retries: a late payment is
  never judged without a price.
* **`"locked"`:** late payments are judged against the locked crypto amount,
  as if on time. This is fine for stablecoins, where both rules agree anyway.

Every invoice that receives funds stores `payment_rate`, `payment_rate_at` and
`payment_value_fiat`, so you always know what a payment was worth when it
arrived. "When it arrived" means when the poller first saw it, so poll
regularly. Single-address invoices always use `"locked"`, because there the
amount identifies the invoice.

## Single-address mode

For chains where you want every payment in one wallet address (for example
USDT on TRON, to avoid moving funds out of many addresses):

```python
"USDT:tron": {
    "type": "tron_trc20",
    "options": {"address": "T...your-wallet...", "network": "mainnet"},
    "address_mode": "single",
    "amount_step": "0.0001",      # invoice amounts become 25.0001, 25.0002, ...
    "max_slots": 1000,
},
```

Each open invoice gets a unique amount slot on the shared address, and only a
payment of exactly that amount (within `underpay_tolerance`, which must be
smaller than `amount_step`) counts. Over- and underpayments can't be told
apart from other customers' payments in this mode, so they stay unmatched.
Tell customers to send the exact amount shown. Slots are freed when an
invoice is paid or its late window closes. If every slot is taken,
`SlotsExhausted` is raised.

## Gap limit

Wallets stop looking for payments after a run of unused addresses, often 20.
`unused_address_run(Invoice, "BTC", "mainnet")` tells you the current run,
and `gap_limit_warning` fires when it reaches `GAP_LIMIT_WARNING`. When that
happens, raise your wallet's gap limit (Sparrow, Electrum and Nunchuk allow
it). The funds are never lost; the wallet just isn't looking.

## Audit trail

Every status change writes an `InvoiceEvent` row with:
* the old and new status;
* amounts, confirmations and transaction IDs;
* the payment-time rate and value;
* `paid_late`.

Read it with `invoice.payment_events()`, or in the admin (read-only), for
disputes and debugging.

## Admin

```python
from chainwatch_payments.contrib.django.admin import CryptoInvoiceAdminMixin

@admin.register(Invoice)
class InvoiceAdmin(CryptoInvoiceAdminMixin, admin.ModelAdmin):
    pass
```

This gives:
* read-only payment fields, including the payment history;
* filters by status, asset, mode and `paid_late`;
* a "Check selected invoices on chain now" action.

The index counter, the audit trail and the forward records also appear in the
admin, read-only.

## Forwarding (opt-in, TRON)

With one address per invoice on TRON, the money ends up spread across many
addresses. The forwarder moves each paid invoice's USDT to your own wallet
automatically.

**It holds keys, unlike everything else in this package:**
* It runs **only** in its own process, `python manage.py chainwatch_forward --loop`.
  Put that process in a separate container that isn't reachable from the
  internet.
* The mnemonic is read from an environment variable that **only that
  process** has. Your web app never sees it.
* Use a **dedicated** wallet's seed, never your savings wallet.

```
pip install "chainwatch-payments[django,tron,forwarder]"
```

```python
CHAINWATCH = {
    ...,
    "FORWARDERS": {
        "USDT:tron": {
            "mnemonic_env": "CHAINWATCH_TRON_MNEMONIC",   # the NAME of the env var, not the words
            "destination": "T...your-treasury...",
            "allowed_destinations": ["T...your-treasury..."],
            "gas_trx": "20",          # TRX sent to a deposit address that lacks fees
            "fee_limit_trx": "30",    # max TRX a single transfer may burn
            "min_amount": "1",        # don't bother forwarding dust below 1 USDT
            "api_key": os.environ.get("TRONGRID_API_KEY"),
        },
    },
}
```

For each paid invoice (19 confirmations, so final), it:
1. derives that address's key and **refuses unless it matches the invoice
   address**;
2. reads the USDT balance; zero means it was already forwarded, so there's
   nothing to do;
3. sends `gas_trx` from the **gas wallet** (index 0 of the same account) if
   the address can't pay its own fee;
4. sends the whole balance to `destination` and waits for a SUCCESS receipt.

**Safety rails:**
* **Wallet check:** the mnemonic must produce exactly the xpub your backend
  watches, or the forwarder refuses to start.
* **Destination check:** the destination must be on `allowed_destinations`,
  and is checked again before every send.
* **Fee cap:** fees are capped by `fee_limit_trx`.
* **Never twice:** a unique `ForwardRecord` per invoice, row-locked while
  working, plus the balance check, means a payment is never forwarded twice,
  even after a crash.
* **Bounded retries:** failures are retried up to `FORWARD_MAX_ATTEMPTS` and
  then left as `failed` for you, with `forward_failed` sent.

Keep a small TRX float on the gas wallet: index 0's address, shown by
`conf.get_forwarder("USDT", "tron").gas_address`. Each forward currently burns
around 13 TRX at mainnet prices. `--include-underpaid` also moves funds from
underpaid invoices whose late window has closed.

## Security notes

* Only extended **public** keys go in `BACKENDS`. The watch-only part never
  needs or accepts a private key.
* The forwarder's mnemonic belongs only to the forwarder process. Never put it
  in the web app's environment or in `settings.py`.
* Keep `.env` out of git and out of Docker images (`.gitignore` and
  `.dockerignore`), and never run production with `DEBUG=True`.
* The xpub can't spend, but it reveals all your addresses and their history,
  so treat it as confidential. Anyone who has the xpub **and** any single
  derived private key can calculate every private key in that account. That's
  why the forwarder's seed must be used only for this purpose.
* Use PostgreSQL in production. Index allocation relies on row locks
  (`SELECT ... FOR UPDATE`).

---

Back to the [README](README.md).
