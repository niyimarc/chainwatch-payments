# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/); while the version is `0.0.x` the
API may still change between releases.

## 0.0.2

### Added
- **Pricing (core):** `quote()` turns a fiat price into an exact crypto amount
  (rounded up, with volatility buffer and network fee). Providers:
  `CoinGeckoProvider` (public/demo/pro), `KrakenProvider`, `FixedRateProvider`
  (stablecoins), `ChainedPriceProvider`, and `CrossCheckedPriceProvider`, which
  refuses to quote when two sources disagree.
- **Dynamic network fees (core):** `MempoolSpaceFeeEstimator` (BTC) and
  `TronEnergyFeeEstimator` (TRON), plus `estimate_fee_fiat()` with minimum,
  margin and fallback.
- **HTTP retries:** the default client retries rate limits (429) and transient
  5xx/network errors with exponential backoff, honouring `Retry-After`.
- **ERC-20 scan start:** `registry.current_height()` and a `since_height` hint
  on `confirm()`, so ERC-20 checks scan only blocks after the invoice was created.
- **Async facade:** `chainwatch_payments.aio.AsyncPaymentRegistry`.
- **Django integration (`[django]` extra):** `AbstractCryptoInvoice`,
  `create_invoice()`, `chainwatch_poll` command, signals, admin mixin.
  - Late payments are judged at the **price when the payment was detected**
    (`LATE_PAYMENT_PRICING="at_payment"`, default) or at the locked amount
    (`"locked"`), configurable per chain, as is the late-payment window.
  - Payment-time rate and fiat value stored on every invoice.
  - Audit trail: `InvoiceEvent` row for every status change.
  - Polling backoff for expired and paid invoices.
  - Gap-limit warning signal for HD wallets.
  - Single-address mode (shared address + unique amount slots).
- **Forwarder (`[forwarder]` extra, opt-in):** moves paid USDT from one-time
  TRON addresses to an allow-listed destination, funding gas from index 0;
  `chainwatch_forward` command, `ForwardRecord` table, never forwards twice.
- `LICENSE` file, CI workflow (lint, types, Python 3.10-3.14, Django 4.2+),
  ruff and mypy configuration.

### Changed
- Version is now defined once, in `chainwatch_payments/__init__.py`.
- TronGrid: transfers older than `deep_seconds` (default 10 minutes, far past
  TRON finality) get their confirmations from block time instead of one
  `gettransactioninfobyid` call each, so busy or shared addresses no longer
  hit rate limits. Found by the live check against mainnet.
- `MockBackend` block timestamps default to the current time (pass
  `fixed_times=True` for the old deterministic timeline); `mine()`/`credit()`
  accept `at=` to set a block time.

## 0.0.1

- First release: watch-only address derivation and payment confirmation for
  BTC, TRON TRC-20 and EVM ERC-20, mock backend, pluggable chain sources.
