from __future__ import annotations

from datetime import date
from zipfile import BadZipFile

import httpx

from app.sources.parsers import parse_cm_bhavcopy, parse_fo_bhavcopy, unzip_first_csv
from app.utils.retry import retry_async


SAMCO_URL = "https://www.samco.in/bse_nse_mcx/getBhavcopy"
SAMCO_HEADERS = {
    "accept": "text/html, */*; q=0.01",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://www.samco.in",
    "referer": "https://www.samco.in/bhavcopy-nse-bse-mcx",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


class SamcoBhavcopyClient:
    def __init__(
        self,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def _fetch_zip(self, trade_date: date, segment: str) -> bytes:
        data = {
            "start_date": trade_date.isoformat(),
            "end_date": trade_date.isoformat(),
            "bhavcopy_data[]": segment,
            "show_or_down": "1",
        }
        async with httpx.AsyncClient(headers=SAMCO_HEADERS, follow_redirects=True, timeout=60) as client:
            response = await retry_async(
                lambda: client.post(SAMCO_URL, data=data),
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
            )
            response.raise_for_status()
            return response.content

    async def fetch_fo(self, trade_date: date):
        content = await self._fetch_zip(trade_date, "NSEFO")
        try:
            csv_text = unzip_first_csv(content)
        except BadZipFile as exc:
            raise RuntimeError("Samco did not return a zip for NSEFO bhavcopy") from exc
        return parse_fo_bhavcopy(csv_text, trade_date, source="samco:NSEFO")

    async def fetch_cm(self, trade_date: date):
        content = await self._fetch_zip(trade_date, "NSE")
        try:
            csv_text = unzip_first_csv(content)
        except BadZipFile as exc:
            raise RuntimeError("Samco did not return a zip for NSE bhavcopy") from exc
        return parse_cm_bhavcopy(csv_text, trade_date, source="samco:NSE")
