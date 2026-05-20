from __future__ import annotations

import asyncio
from datetime import date

import httpx

from app.sources.parsers import parse_cm_bhavcopy, parse_fo_bhavcopy, unzip_first_csv
from app.utils.retry import retry_async


NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}


class NSEArchiveClient:
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

    def fo_urls(self, trade_date: date) -> list[str]:
        y = trade_date.strftime("%Y")
        m = trade_date.strftime("%b").upper()
        legacy = trade_date.strftime("%d%b%Y").upper()
        compact = trade_date.strftime("%Y%m%d")
        return [
            f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{compact}_F_0000.csv.zip",
            f"https://archives.nseindia.com/content/historical/DERIVATIVES/{y}/{m}/fo{legacy}bhav.csv.zip",
            f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{y}/{m}/fo{legacy}bhav.csv.zip",
        ]

    def cm_urls(self, trade_date: date) -> list[str]:
        y = trade_date.strftime("%Y")
        m = trade_date.strftime("%b").upper()
        legacy = trade_date.strftime("%d%b%Y").upper()
        compact = trade_date.strftime("%Y%m%d")
        return [
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{compact}_F_0000.csv.zip",
            f"https://archives.nseindia.com/content/historical/EQUITIES/{y}/{m}/cm{legacy}bhav.csv.zip",
            f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{y}/{m}/cm{legacy}bhav.csv.zip",
        ]

    async def _download_first(self, urls: list[str]) -> tuple[bytes, str]:
        async with httpx.AsyncClient(headers=NSE_HEADERS, follow_redirects=True, timeout=45) as client:
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
                    return response.content, url
                except Exception as exc:  # noqa: BLE001 - preserve fallback chain
                    last_error = exc
            raise RuntimeError(f"all NSE urls failed: {last_error}")

    async def fetch_fo(self, trade_date: date):
        content, url = await self._download_first(self.fo_urls(trade_date))
        return parse_fo_bhavcopy(unzip_first_csv(content), trade_date, source=f"nse:{url}")

    async def fetch_cm(self, trade_date: date):
        content, url = await self._download_first(self.cm_urls(trade_date))
        return parse_cm_bhavcopy(unzip_first_csv(content), trade_date, source=f"nse:{url}")
