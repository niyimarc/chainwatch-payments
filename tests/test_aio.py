from __future__ import annotations

import asyncio

from chainwatch_payments import ChainSourceError, PaymentRegistry, State
from chainwatch_payments.aio import AsyncPaymentRegistry
from chainwatch_payments.backends import MockBackend


def test_async_confirm_and_confirm_many():
    btc = MockBackend("BTC", "regtest", default_confirmations=1)
    bad = MockBackend("LTC", "regtest")

    def down(*a, **k):
        raise ChainSourceError("down")

    bad.get_incoming = down
    reg = PaymentRegistry()
    reg.register("BTC", "regtest", btc)
    reg.register("LTC", "regtest", bad)
    areg = AsyncPaymentRegistry(reg)

    a1, a2 = areg.derive_address("BTC", "regtest", 1), asyncio.run(areg.aderive_address("BTC", "regtest", 2))
    btc.credit(a1, "1", mined=True)

    async def go():
        single = await areg.confirm("BTC", "regtest", a1, "1")
        many = await areg.confirm_many([
            dict(asset="BTC", network="regtest", address=a1, expected_amount="1"),
            dict(asset="BTC", network="regtest", address=a2, expected_amount="1"),
            dict(asset="LTC", network="regtest", address="x", expected_amount="1"),
        ])
        return single, many, await areg.current_height("BTC", "regtest")

    single, many, height = asyncio.run(go())
    assert single.state is State.CONFIRMED
    assert many[0].state is State.CONFIRMED and many[1].state is State.NONE
    assert isinstance(many[2], ChainSourceError)
    assert height == 1
