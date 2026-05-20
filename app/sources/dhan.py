from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx

from app.utils.retry import retry_async


class DhanOptionChainClient:
    """Thin DhanHQ v2 option-chain client.

    Dhan requires the underlying security id, not the NSE trading symbol. Keep
    that mapping outside this client so the live layer can support indices,
    equities, and later a broker-neutral instrument master.
    """

    base_url = "https://api.dhan.co/v2"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        min_interval_seconds: float = 3.0,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self.min_interval_seconds = min_interval_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def expiry_list(self, underlying_scrip: int, underlying_seg: str) -> list[date]:
        payload = await self._post(
            "/optionchain/expirylist",
            {"UnderlyingScrip": underlying_scrip, "UnderlyingSeg": underlying_seg},
        )
        return [date.fromisoformat(item) for item in payload.get("data", [])]

    async def option_chain(self, underlying_scrip: int, underlying_seg: str, expiry: date) -> dict[str, Any]:
        return await self._post(
            "/optionchain",
            {
                "UnderlyingScrip": underlying_scrip,
                "UnderlyingSeg": underlying_seg,
                "Expiry": expiry.isoformat(),
            },
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.min_interval_seconds)
        headers = {
            "Content-Type": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        }
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
            response = await retry_async(
                lambda: client.post(path, json=payload),
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
            )
            response.raise_for_status()
            return response.json()
