"""Tiny HTTP abstraction so chain sources can be mocked and transports swapped.

The default client uses ``httpx`` (installed by the ``[bitcoin]``, ``[tron]``
and ``[evm]`` extras). Anything with ``get_json`` / ``post_json`` works, so
you can plug in requests, aiohttp-in-a-thread, a caching layer, etc.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .errors import ChainSourceError, MissingDependency

__all__ = ["HttpClient", "HttpxClient", "default_http_client"]


@runtime_checkable
class HttpClient(Protocol):
    def get_json(self, url: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> Any: ...

    def post_json(self, url: str, payload: Any,
                  headers: Mapping[str, str] | None = None) -> Any: ...


RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpxClient:
    """httpx-based client with bounded retries.

    Rate limits (429) and transient server/network errors are retried up to
    ``max_retries`` times with exponential backoff (``backoff * 2**attempt``
    seconds), honouring a ``Retry-After`` header capped at ``max_delay``.
    Anything else fails immediately with ``ChainSourceError``.
    """

    def __init__(
        self,
        timeout: float = 15.0,
        client: Any = None,
        *,
        max_retries: int = 2,
        backoff: float = 0.5,
        max_delay: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MissingDependency(
                "httpx is required for the default chain sources: "
                "pip install 'chainwatch-payments[bitcoin]' (or [tron], [evm], [all])"
            ) from exc
        self._httpx = httpx
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_delay = max_delay
        self._sleep = sleep

    def _delay(self, attempt: int, response: Any = None) -> float:
        if response is not None:
            ra = response.headers.get("retry-after")
            if ra:
                try:
                    return min(max(float(ra), 0.0), self.max_delay)
                except ValueError:
                    pass
        return min(self.backoff * (2 ** attempt), self.max_delay)

    def _call(self, method: str, url: str, **kw: Any) -> Any:
        httpx = self._httpx
        attempt = 0
        while True:
            try:
                r = self._client.request(method, url, **kw)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    self._sleep(self._delay(attempt))
                    attempt += 1
                    continue
                raise ChainSourceError(f"{method} {url} failed: {exc}") from exc
            if r.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(self._delay(attempt, r))
                attempt += 1
                continue
            try:
                r.raise_for_status()
                if not r.content:
                    return None
                ctype = r.headers.get("content-type", "")
                return r.json() if "json" in ctype or r.content[:1] in (b"{", b"[") else r.text
            except httpx.HTTPError as exc:
                raise ChainSourceError(f"{method} {url} failed: {exc}") from exc
            except ValueError as exc:
                raise ChainSourceError(f"{method} {url} returned invalid JSON") from exc

    def get_json(self, url, params=None, headers=None):
        return self._call("GET", url, params=dict(params or {}), headers=dict(headers or {}))

    def post_json(self, url, payload, headers=None):
        return self._call("POST", url, json=payload, headers=dict(headers or {}))

    def post_text(self, url: str, body: str, headers: Mapping[str, str] | None = None) -> Any:
        """POST a raw text body (e.g. a hex transaction to Esplora's /tx)."""
        h = {"content-type": "text/plain", **dict(headers or {})}
        return self._call("POST", url, content=body.encode(), headers=h)

    def close(self) -> None:
        self._client.close()


def default_http_client() -> HttpClient:
    return HttpxClient()
