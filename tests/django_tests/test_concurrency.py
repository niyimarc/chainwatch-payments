"""Concurrent checkouts must never share an address. Runs only on PostgreSQL
(set CHAINWATCH_TEST_POSTGRES=host:port); SQLite serialises writes anyway."""

from __future__ import annotations

import threading

import pytest
from django.db import connection, connections

from chainwatch_payments.contrib.django import conf
from chainwatch_payments.contrib.django.services import allocate_index, create_invoice

from .testapp.models import Invoice

pytestmark = pytest.mark.django_db(transaction=True)


def _need_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("concurrency test needs PostgreSQL (CHAINWATCH_TEST_POSTGRES)")


def _hammer(fn, threads=8, per_thread=15):
    barrier = threading.Barrier(threads)
    out, errors = [], []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            for _ in range(per_thread):
                v = fn()
                with lock:
                    out.append(v)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            connections.close_all()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    return out


def test_concurrent_index_allocation_is_unique():
    _need_postgres()
    got = _hammer(lambda: allocate_index("BTC", "regtest"))
    assert sorted(got) == list(range(1, 8 * 15 + 1))       # no duplicates, no gaps


def test_concurrent_checkouts_get_distinct_addresses():
    _need_postgres()
    conf.reset()
    conf.get_registry()                                      # build once before threads start
    got = _hammer(lambda: create_invoice(Invoice, asset="BTC", network="regtest", fiat_price="20").pk,
                  threads=6, per_thread=10)
    rows = list(Invoice.objects.filter(pk__in=got).values_list("derivation_index", "address"))
    assert len(rows) == 60
    assert len({i for i, _ in rows}) == 60 and len({a for _, a in rows}) == 60
