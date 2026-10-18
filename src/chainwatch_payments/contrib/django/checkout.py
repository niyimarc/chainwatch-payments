"""Helpers for letting the customer choose which coin to pay with.

``payment_options()`` lists the coins offered at checkout, ``quote_invoice()``
and ``quote_options()`` price them WITHOUT creating an invoice or using an
address, and ``parse_option()`` turns the customer's pick back into
``(asset, network)`` safely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured

from ...backends import BitcoinBackend, ERC20Backend, TronTRC20Backend
from ...errors import ChainwatchError
from ...fees import estimate_fee_fiat
from ...pricing import PriceUnavailable, Quote, quote
from . import conf

__all__ = ["PaymentOption", "InvoiceQuote", "payment_options", "payment_choices", "parse_option",
           "quote_invoice", "quote_options", "price_invoice"]

log = logging.getLogger("chainwatch_payments")

_MAINNETS = {"mainnet", "bitcoin", "tron", "ethereum", "eth"}


@dataclass(frozen=True)
class PaymentOption:
    key: str            # "USDT:tron", the value to put in your form
    asset: str
    network: str
    label: str          # "USDT (TRC-20)", for display
    decimals: int
    confirmations: int  # confirmations needed before the invoice counts as paid


@dataclass(frozen=True)
class InvoiceQuote:
    """What an invoice for this coin would ask for right now. A preview only:
    the invoice created later fetches the rate again and is the amount to pay."""

    option: PaymentOption
    fiat_price: Decimal
    fiat_currency: str
    rate: Decimal              # fiat value of 1 unit of the coin
    volatility_buffer: Decimal
    network_fee_fiat: Decimal
    total_fiat: Decimal        # price * (1 + buffer) + fee
    total_amount: Decimal      # what the customer would send
    exact: bool                # False in single-address mode (a tiny slot amount is added at creation)
    error: str = ""            # quote_options only: why this coin could not be priced


def _default_label(asset: str, network: str, backend) -> str:
    if isinstance(backend, BitcoinBackend):
        standard = ""
    elif isinstance(backend, TronTRC20Backend):
        standard = "TRC-20"
    elif isinstance(backend, ERC20Backend):
        standard = "ERC-20"
    else:
        return f"{asset} ({network})"
    extra = [s for s in (standard, "" if network in _MAINNETS else network) if s]
    return f"{asset} ({', '.join(extra)})" if extra else asset


def _option(cfg: conf.BackendConfig) -> PaymentOption:
    caps = cfg.backend.capabilities()
    confirmations = cfg.confirmations if cfg.confirmations is not None else caps.default_confirmations
    return PaymentOption(
        key=f"{cfg.asset}:{cfg.network}", asset=cfg.asset, network=cfg.network,
        label=cfg.label or _default_label(cfg.asset, cfg.network, cfg.backend),
        decimals=caps.decimals, confirmations=int(confirmations),
    )


def payment_options() -> list[PaymentOption]:
    """Coins offered at checkout, in the order of ``CHAINWATCH['BACKENDS']``.

    Entries with ``"checkout": False`` are left out (they are still watched,
    so invoices already created for them keep updating).
    """
    return [_option(cfg) for cfg in conf.backend_configs().values() if cfg.checkout]


def payment_choices() -> list[tuple[str, str]]:
    """``[(key, label), ...]`` ready for a Django ``ChoiceField``."""
    return [(o.key, o.label) for o in payment_options()]


def parse_option(key: str) -> tuple[str, str]:
    """Turn a submitted choice (``"USDT:tron"``) into ``(asset, network)``.

    Raises ``ValueError`` unless it is one of ``payment_options()``, so a
    tampered form can't pick a coin you don't offer.
    """
    if not isinstance(key, str) or ":" not in key:
        raise ValueError(f"not a payment option: {key!r}")
    asset, network = key.split(":", 1)
    asset, network = asset.strip().upper(), network.strip().lower()
    if f"{asset}:{network}" not in {o.key for o in payment_options()}:
        raise ValueError(f"not a payment option: {key!r}")
    return asset, network


def price_invoice(cfg: conf.BackendConfig, fiat_price, fiat_currency: str) -> Quote:
    """Rate + fee estimate + amount, exactly as ``create_invoice`` computes them."""
    prices = conf.get_price_provider()
    rate = prices.rate(cfg.asset, fiat_currency)
    fee = estimate_fee_fiat(cfg.fee_estimator, fiat=fiat_currency, prices=prices,
                            minimum=cfg.network_fee, margin=cfg.fee_margin)
    return quote(asset=cfg.asset, fiat=fiat_currency, fiat_price=fiat_price, rate=rate,
                 decimals=cfg.backend.capabilities().decimals,
                 volatility_buffer=cfg.volatility_buffer, network_fee_fiat=fee)


def _invoice_quote(cfg: conf.BackendConfig, q: Quote) -> InvoiceQuote:
    return InvoiceQuote(
        option=_option(cfg), fiat_price=q.fiat_price, fiat_currency=q.fiat, rate=q.rate,
        volatility_buffer=q.volatility_buffer, network_fee_fiat=q.network_fee_fiat,
        total_fiat=q.total_fiat, total_amount=q.total_amount, exact=cfg.address_mode == "hd",
    )


def quote_invoice(*, asset: str, network: str, fiat_price, fiat_currency: str = "USD") -> InvoiceQuote:
    """Price one coin without creating anything (raises ``PriceUnavailable``)."""
    cfg = conf.backend_config(asset, network)
    return _invoice_quote(cfg, price_invoice(cfg, fiat_price, fiat_currency.upper()))


def quote_options(fiat_price, fiat_currency: str = "USD") -> list[InvoiceQuote]:
    """Price every coin in ``payment_options()`` for a checkout page.

    A coin whose price can't be fetched is still returned, with
    ``total_amount`` 0 and ``error`` set, so you can grey it out instead of
    failing the whole page.
    """
    fiat_currency = fiat_currency.upper()
    out: list[InvoiceQuote] = []
    for cfg in conf.backend_configs().values():
        if not cfg.checkout:
            continue
        try:
            out.append(_invoice_quote(cfg, price_invoice(cfg, fiat_price, fiat_currency)))
        except (PriceUnavailable, ChainwatchError, ImproperlyConfigured) as exc:
            log.info("chainwatch: cannot quote %s:%s: %s", cfg.asset, cfg.network, exc)
            zero = Decimal(0)
            out.append(InvoiceQuote(
                option=_option(cfg), fiat_price=Decimal(str(fiat_price)), fiat_currency=fiat_currency,
                rate=zero, volatility_buffer=cfg.volatility_buffer, network_fee_fiat=zero,
                total_fiat=zero, total_amount=zero, exact=False, error=str(exc) or type(exc).__name__,
            ))
    return out
