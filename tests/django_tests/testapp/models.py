from django.db import models

from chainwatch_payments.contrib.django.models import AbstractCryptoInvoice


class Invoice(AbstractCryptoInvoice):
    """What a host billing app would write: inherit and add its own fields."""

    customer = models.CharField(max_length=64, default="")

    class Meta(AbstractCryptoInvoice.Meta):
        app_label = "testapp"
