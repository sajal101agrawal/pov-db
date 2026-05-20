from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass
from typing import Any

import httpx

from app.sources.nse import NSE_HEADERS
from app.utils.retry import retry_async


EQUITY_LIST_URLS = (
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
)

INDEX_LIST_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "banknifty": "https://archives.nseindia.com/content/indices/ind_niftybanklist.csv",
}


@dataclass(frozen=True)
class SymbolMetadata:
    symbol: str
    company_name: str | None = None
    isin: str | None = None
    sector: str | None = None
    industry: str | None = None
    lot_size: int | None = None
    tick_size: float | None = None
    is_nifty50: bool = False
    is_nifty100: bool = False
    is_banknifty: bool = False
    is_midcap: bool = False


class NSEMetadataClient:
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

    async def fetch_metadata(self, symbols: set[str] | None = None, enrich_quote: bool = False) -> list[SymbolMetadata]:
        equity_rows = await self.fetch_equity_list()
        index_members = await self.fetch_index_members()
        selected = {symbol.upper() for symbol in symbols} if symbols else set(equity_rows)
        records: dict[str, SymbolMetadata] = {}
        for symbol in sorted(selected):
            base = equity_rows.get(symbol, {})
            records[symbol] = SymbolMetadata(
                symbol=symbol,
                company_name=base.get("company_name"),
                isin=base.get("isin"),
                lot_size=_int(base.get("lot_size")),
                is_nifty50=symbol in index_members["nifty50"],
                is_nifty100=symbol in index_members["nifty100"],
                is_banknifty=symbol in index_members["banknifty"],
                is_midcap=symbol in index_members["nifty500"] and symbol not in index_members["nifty100"],
                industry=base.get("industry"),
            )
        for symbol, industry in index_members["nifty500_industry"].items():
            if symbol in records:
                current = records[symbol]
                records[symbol] = SymbolMetadata(
                    **{**current.__dict__, "industry": current.industry or industry}
                )

        if enrich_quote:
            async with httpx.AsyncClient(headers=NSE_HEADERS, follow_redirects=True, timeout=30) as client:
                await client.get("https://www.nseindia.com")
                for symbol in sorted(records):
                    await asyncio.sleep(self.request_delay_seconds)
                    quote = await self.fetch_quote_metadata(client, symbol)
                    if not quote:
                        continue
                    current = records[symbol]
                    records[symbol] = SymbolMetadata(
                        **{
                            **current.__dict__,
                            "company_name": quote.get("company_name") or current.company_name,
                            "isin": quote.get("isin") or current.isin,
                            "sector": quote.get("sector") or current.sector,
                            "industry": quote.get("industry") or current.industry,
                            "tick_size": quote.get("tick_size") or current.tick_size,
                        }
                    )
        return list(records.values())

    async def fetch_equity_list(self) -> dict[str, dict[str, Any]]:
        text = await self._download_first_text(list(EQUITY_LIST_URLS))
        rows: dict[str, dict[str, Any]] = {}
        reader = csv.DictReader(io.StringIO(text))
        for raw in reader:
            symbol = _clean(raw.get("SYMBOL"))
            if not symbol:
                continue
            rows[symbol] = {
                "company_name": _clean(raw.get("NAME OF COMPANY")),
                "isin": _clean(raw.get("ISIN NUMBER")),
                "lot_size": _clean(raw.get("MARKET LOT")),
            }
        return rows

    async def fetch_index_members(self) -> dict[str, Any]:
        output: dict[str, Any] = {
            "nifty50": set(),
            "nifty100": set(),
            "nifty500": set(),
            "banknifty": set(),
            "nifty500_industry": {},
        }
        for key, url in INDEX_LIST_URLS.items():
            try:
                text = await self._download_first_text([url])
            except Exception:
                continue
            reader = csv.DictReader(io.StringIO(text))
            for raw in reader:
                symbol = _clean(raw.get("Symbol") or raw.get("SYMBOL"))
                if not symbol:
                    continue
                output[key].add(symbol)
                industry = _clean(raw.get("Industry"))
                if key == "nifty500" and industry:
                    output["nifty500_industry"][symbol] = industry
        return output

    async def fetch_quote_metadata(self, client: httpx.AsyncClient, symbol: str) -> dict[str, Any] | None:
        try:
            response = await retry_async(
                lambda: client.get("https://www.nseindia.com/api/quote-equity", params={"symbol": symbol}),
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        info = payload.get("info") or {}
        metadata = payload.get("metadata") or {}
        industry_info = payload.get("industryInfo") or {}
        price_info = payload.get("priceInfo") or {}
        return {
            "company_name": info.get("companyName") or metadata.get("companyName"),
            "isin": info.get("isin") or metadata.get("isin"),
            "sector": industry_info.get("sector") or metadata.get("pdSectorInd"),
            "industry": industry_info.get("industry") or info.get("industry") or metadata.get("industry"),
            "tick_size": _float(price_info.get("tickSize")),
        }

    async def _download_first_text(self, urls: list[str]) -> str:
        async with httpx.AsyncClient(headers=NSE_HEADERS, follow_redirects=True, timeout=30) as client:
            last_error: Exception | None = None
            for url in urls:
                await asyncio.sleep(self.request_delay_seconds)
                try:
                    response = await retry_async(
                        lambda url=url: client.get(url),
                        attempts=self.retry_attempts,
                        base_delay_seconds=self.retry_base_delay_seconds,
                        max_delay_seconds=self.retry_max_delay_seconds,
                    )
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    return response.text
                except Exception as exc:  # noqa: BLE001 - try fallback archive host
                    last_error = exc
            raise RuntimeError(f"all NSE metadata urls failed: {last_error}")


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() if text and text.isupper() else text or None


def _int(value: object) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
