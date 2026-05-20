from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import httpx

from app.utils.retry import retry_async


RESULT_KEYWORDS = ("financial result", "financial results", "results", "result/dividend", "results/dividend")


class NSECorporateEventsClient:
    def __init__(
        self,
        request_delay_seconds: float = 0.35,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def fetch_result_events(self, symbols: list[str]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            await self._prime_session(client)
            for symbol in symbols:
                events.extend(await self._fetch_symbol(client, symbol.upper()))
                await asyncio.sleep(self.request_delay_seconds)
        return events

    async def _prime_session(self, client: httpx.AsyncClient) -> None:
        await retry_async(
            lambda: client.get(
                "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar",
                headers=_headers(),
            ),
            attempts=self.retry_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
        )

    async def _fetch_symbol(self, client: httpx.AsyncClient, symbol: str) -> list[dict[str, Any]]:
        response = await retry_async(
            lambda: client.get(
                f"https://www.nseindia.com/api/event-calendar?index=equities&symbol={symbol}",
                headers=_headers(
                    "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
                    f"?symbol={symbol}&tabIndex=equity"
                ),
            ),
            attempts=self.retry_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        result_events: list[dict[str, Any]] = []
        for row in rows:
            event_date = _parse_nse_date(row.get("date"))
            if not event_date:
                continue
            purpose = str(row.get("purpose") or "")
            description = str(row.get("bm_desc") or purpose)
            searchable = f"{purpose} {description}".lower()
            if not any(keyword in searchable for keyword in RESULT_KEYWORDS):
                continue
            result_events.append(
                {
                    "symbol": symbol,
                    "event_date": event_date,
                    "event_type": "RESULT",
                    "description": description[:2000],
                    "source": "nse:event-calendar",
                }
            )
        return result_events


def _parse_nse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers
