from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Any

import httpx

from app.services.corporate_actions import classify_nse_action, parse_action_terms
from app.utils.retry import retry_async


NSE_ACTIONS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
NSE_ACTIONS_API = "https://www.nseindia.com/api/corporates-corporateActions"


class NSECorporateActionsClient:
    def __init__(
        self,
        request_delay_seconds: float = 0.35,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
        chunk_days: int = 180,
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.chunk_days = max(1, chunk_days)

    async def fetch_actions(
        self,
        start: date,
        end: date,
        symbols: list[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if end < start:
            return []
        allowed = {symbol.upper() for symbol in symbols} if symbols else None
        actions: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            await self._prime_session(client)
            chunk_start = start
            while chunk_start <= end:
                chunk_end = min(end, chunk_start + timedelta(days=self.chunk_days - 1))
                rows = await self._fetch_range(client, chunk_start, chunk_end)
                for row in rows:
                    action = parse_nse_corporate_action(row)
                    if action is None or (allowed is not None and action["symbol"] not in allowed):
                        continue
                    actions[action["source_key"]] = action
                chunk_start = chunk_end + timedelta(days=1)
                if chunk_start <= end:
                    await asyncio.sleep(self.request_delay_seconds)
        return sorted(
            actions.values(), key=lambda item: (item["ex_date"], item["symbol"], item["source_key"])
        )

    async def _prime_session(self, client: httpx.AsyncClient) -> None:
        response = await retry_async(
            lambda: client.get(NSE_ACTIONS_PAGE, headers=_headers()),
            attempts=self.retry_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
        )
        response.raise_for_status()

    async def _fetch_range(
        self, client: httpx.AsyncClient, start: date, end: date
    ) -> list[dict[str, Any]]:
        response = await retry_async(
            lambda: client.get(
                NSE_ACTIONS_API,
                params={
                    "index": "equities",
                    "from_date": start.strftime("%d-%m-%Y"),
                    "to_date": end.strftime("%d-%m-%Y"),
                },
                headers=_headers(NSE_ACTIONS_PAGE),
            ),
            attempts=self.retry_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        return [row for row in rows if isinstance(row, dict)]


def parse_nse_corporate_action(row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "").strip().upper()
    description = str(row.get("subject") or "").strip()
    ex_date = _parse_nse_date(row.get("exDate"))
    series = str(row.get("series") or "").strip().upper()
    action_type = classify_nse_action(description)
    if not symbol or not description or not ex_date or series != "EQ" or action_type is None:
        return None

    face_value = _float_or_none(row.get("faceVal"))
    terms = parse_action_terms(description, face_value)
    source_key_payload = f"{symbol}|{ex_date.isoformat()}|{description}"
    source_key = hashlib.sha256(source_key_payload.encode("utf-8")).hexdigest()
    return {
        "symbol": symbol,
        "ex_date": ex_date,
        "record_date": _parse_nse_date(row.get("recDate")),
        "action_type": action_type,
        "description": description,
        "face_value": face_value,
        "price_multiplier": terms["price_multiplier"],
        "cash_amount": terms["cash_amount"],
        "rights_new_shares": terms["rights_new_shares"],
        "rights_held_shares": terms["rights_held_shares"],
        "subscription_price": terms["subscription_price"],
        "adjustment_status": terms["adjustment_status"],
        "factor_source": terms["factor_source"],
        "source": "nse:corporate-actions",
        "source_key": source_key,
        "raw_payload": json.dumps(row, default=str),
    }


def _parse_nse_date(value: Any) -> date | None:
    if not value or value == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, "", "-"} else None
    except (TypeError, ValueError):
        return None


def _headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers
