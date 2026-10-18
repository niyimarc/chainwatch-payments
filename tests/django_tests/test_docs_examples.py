"""The testing example from DJANGO_DOC.md, "Testing your integration", runs as written."""

from __future__ import annotations

import pytest

from chainwatch_payments.contrib.django import conf
from chainwatch_payments.contrib.django.services import create_invoice

from .testapp.models import Invoice

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def doc_settings(settings):
    settings.CHAINWATCH = {
        "BACKENDS": {
            "USDT:test": {"type": "mock", "options": {"asset": "USDT", "network": "test",
                                                       "decimals": 6, "default_confirmations": 2}},
            "BTC:test": {"type": "mock", "options": {"asset": "BTC", "network": "test", "decimals": 8}},
        },
        "PRICE_PROVIDER": "fixed",
        "FIXED_RATES": {"USDT:USD": "1", "BTC:USD": "100000"},
        "INVOICE_MODELS": ["testapp.Invoice"],
    }
    conf.reset()
    yield
    conf.reset()


def test_invoice_gets_paid(doc_settings):
    invoice = create_invoice(Invoice, asset="USDT", network="test", fiat_price="20")
    chain = conf.backend_config("USDT", "test").backend
    chain.credit(invoice.address, invoice.total_price)
    chain.mine(2)
    invoice.refresh_payment()
    assert invoice.status == "paid"

    chain.reorg(2)
    invoice.refresh_payment()
    assert invoice.status == "reverted"
