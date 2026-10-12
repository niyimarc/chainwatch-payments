"""Tiny HTTP abstraction so chain sources can be mocked and transports swapped.

The default client uses ``httpx`` (installed by the ``[bitcoin]``, ``[tron]``
and ``[evm]`` extras). Anything with ``get_json`` / ``post_json`` works, so
you can plug in requests, aiohttp-in-a-thread, a caching layer, etc.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .errors import ChainSourceError, MissingDependency

__all__ = ["HttpClient", "HttpxClient", "default_http_client"]


@runtime_checkable
class HttpClient(Protocol):
    def get_json(self, url: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> Any: ...

    def post_json(self, url: str, payload: Any,
                  headers: Mapping[str, str] | None = None) -> Any: ...


class HttpxClient:
    def __init__(self, timeout: float = 15.0, client: Any = None) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MissingDependency(
                "httpx is required for the default chain sources: "
                "pip install 'chainwatch-payments[bitcoin]' (or [tron], [evm], [all])"
            ) from exc
        self._httpx = httpx
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def _call(self, method: str, url: str, **kw: Any) -> Any:
        try:
            r = self._client.request(method, url, **kw)
            r.raise_for_status()
            if not r.content:
                return None
            ctype = r.headers.get("content-type", "")
            return r.json() if "json" in ctype or r.content[:1] in (b"{", b"[") else r.text
        except self._httpx.HTTPError as exc:
            raise ChainSourceError(f"{method} {url} failed: {exc}") from exc
        except ValueError as exc:
            raise ChainSourceError(f"{method} {url} returned invalid JSON") from exc

    def get_json(self, url, params=None, headers=None):
        return self._call("GET", url, params=dict(params or {}), headers=dict(headers or {}))

    def post_json(self, url, payload, headers=None):
        return self._call("POST", url, json=payload, headers=dict(headers or {}))

    def close(self) -> None:
        self._client.close()


def default_http_client() -> HttpClient:
    return HttpxClient()
