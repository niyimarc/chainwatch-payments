"""Async facade for asyncio frameworks (FastAPI, Starlette, aiohttp, ...).

The chain sources use blocking HTTP, so each call runs in a worker thread via
``asyncio.to_thread``. That keeps your event loop responsive without a second
implementation of every backend. Address derivation is pure CPU and fast, so
it is offered both ways.

    from chainwatch_payments.aio import AsyncPaymentRegistry

    areg = AsyncPaymentRegistry(registry)
    status = await areg.confirm("BTC", "mainnet", address, "0.001")
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from .registry import PaymentRegistry
from .types import PaymentStatus

__all__ = ["AsyncPaymentRegistry"]


class AsyncPaymentRegistry:
    def __init__(self, registry: PaymentRegistry | None = None) -> None:
        self.registry = registry or PaymentRegistry()

    # pass-through for configuration
    def register(self, *args: Any, **kwargs: Any) -> None:
        self.registry.register(*args, **kwargs)

    def derive_address(self, asset: str, network: str, index: int) -> str:
        return self.registry.derive_address(asset, network, index)

    async def aderive_address(self, asset: str, network: str, index: int) -> str:
        return await asyncio.to_thread(self.registry.derive_address, asset, network, index)

    async def confirm(
        self,
        asset: str,
        network: str,
        address: str,
        expected_amount: Decimal | int | str,
        required_confirmations: int | None = None,
        not_before: datetime | None = None,
        **kwargs: Any,
    ) -> PaymentStatus:
        return await asyncio.to_thread(
            self.registry.confirm, asset, network, address, expected_amount,
            required_confirmations, not_before, **kwargs,
        )

    async def confirm_many(self, requests: list[dict[str, Any]], *, concurrency: int = 8) -> list[Any]:
        """Run several ``confirm`` calls concurrently (bounded). Each request is
        a dict of ``confirm`` keyword arguments. Failures are returned as the
        exception object in that position, so one bad chain read doesn't lose
        the others."""
        sem = asyncio.Semaphore(concurrency)

        async def one(req: dict[str, Any]) -> Any:
            async with sem:
                return await self.confirm(**req)

        return list(await asyncio.gather(*(one(r) for r in requests), return_exceptions=True))

    async def current_height(self, asset: str, network: str) -> int | None:
        return await asyncio.to_thread(self.registry.current_height, asset, network)
