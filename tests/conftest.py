from __future__ import annotations

from typing import Any, Callable

import pytest

from chainwatch_payments.crypto import secp256k1

# Account-level public keys for the well-known BIP39 test mnemonic
# "abandon abandon ... abandon about" (no passphrase). Public data only.
ZPUB_84 = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
VPUB_84_TEST = "vpub5Y6cjg78GGuNLsaPhmYsiw4gYX3HoQiRBiSwDaBXKUafCt9bNwWQiitDk5VZ5BVxYnQdwoTyXSs2JHRPAgjAvtbBrf8ZhDYe2jWAqvZVnsc"
XPUB_44 = "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj"
YPUB_49 = "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP"
ETH_XPUB = "xpub6DCoCpSuQZB2jawqnGMEPS63ePKWkwWPH4TU45Q7LPXWuNd8TMtVxRrgjtEshuqpK3mdhaWHPFsBngh5GFZaM6si3yZdUsT8ddYM3PwnATt"
TRX_XPUB = "xpub6D1AabNHCupeiLM65ZR9UStMhJ1vCpyV4XbZdyhMZBiJXALQtmn9p42VTQckoHVn8WNqS7dqnJokZHAHcHGoaQgmv8D45oNUKx6DZMNZBCd"


@pytest.fixture(params=["fast", "pure"])
def crypto_impl(request, monkeypatch):
    """Run a test against both libsecp256k1 (if installed) and pure Python."""
    if request.param == "fast" and secp256k1._coincurve is None:
        pytest.skip("coincurve not installed")
    monkeypatch.setattr(secp256k1, "_force_pure", request.param == "pure")
    return request.param


class FakeHttp:
    """Route table HTTP mock. Never touches the network."""

    def __init__(self) -> None:
        self.get_routes: dict[str, Any] = {}
        self.post_routes: dict[str, Any] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def on_get(self, url: str, response: Any | Callable[[dict], Any]) -> None:
        self.get_routes[url] = response

    def on_post(self, url: str, response: Any | Callable[[Any], Any]) -> None:
        self.post_routes[url] = response

    def get_json(self, url, params=None, headers=None):
        self.calls.append(("GET", url, dict(params or {})))
        if url not in self.get_routes:
            raise AssertionError(f"unexpected GET {url}")
        r = self.get_routes[url]
        return r(dict(params or {})) if callable(r) else r

    def post_json(self, url, payload, headers=None):
        self.calls.append(("POST", url, payload))
        if url not in self.post_routes:
            raise AssertionError(f"unexpected POST {url}")
        r = self.post_routes[url]
        return r(payload) if callable(r) else r


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Belt and braces: the default HTTP client must never be built in tests."""
    import chainwatch_payments.http as h

    def boom():
        raise AssertionError("tests must inject a fake HTTP client / source")

    for mod in ("bitcoin", "tron", "evm"):
        monkeypatch.setattr(f"chainwatch_payments.backends.{mod}.default_http_client", boom)
    monkeypatch.setattr(h, "default_http_client", boom)
