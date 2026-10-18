"""HttpxClient retry behaviour, using httpx.MockTransport (no network)."""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")

from chainwatch_payments import ChainSourceError  # noqa: E402
from chainwatch_payments.http import HttpxClient  # noqa: E402


def client_with(responses, **kw):
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    c = HttpxClient(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, **kw)
    return c, calls, sleeps


def test_retries_rate_limit_then_succeeds():
    c, calls, sleeps = client_with([
        httpx.Response(429, headers={"retry-after": "3"}),
        httpx.Response(503),
        httpx.Response(200, json={"ok": 1}),
    ])
    assert c.get_json("https://x.test/a") == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [3.0, 1.0]          # Retry-After honoured, then backoff 0.5*2^1


def test_gives_up_after_max_retries():
    c, calls, sleeps = client_with([httpx.Response(429)], max_retries=2)
    with pytest.raises(ChainSourceError, match="429"):
        c.get_json("https://x.test/a")
    assert len(calls) == 3 and len(sleeps) == 2


def test_client_errors_are_not_retried():
    c, calls, _ = client_with([httpx.Response(404)])
    with pytest.raises(ChainSourceError):
        c.get_json("https://x.test/a")
    assert len(calls) == 1


def test_network_errors_are_retried():
    c, calls, _ = client_with([httpx.ConnectError("boom"), httpx.Response(200, text="123")])
    assert c.get_json("https://x.test/height") == "123"
    assert len(calls) == 2


def test_retry_after_is_capped():
    c, _, sleeps = client_with([httpx.Response(429, headers={"retry-after": "999"}),
                                httpx.Response(200, json=[])], max_delay=5)
    c.get_json("https://x.test/a")
    assert sleeps == [5]
