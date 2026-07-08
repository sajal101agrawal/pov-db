from __future__ import annotations

from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
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

    async def _fetch_csv_text(self, trade_date: date, segment: str) -> str:
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
            csv_text = _csv_text_from_content(response.content)
            if csv_text is not None:
                return csv_text

            download_url = _extract_download_url(response.text, str(response.url))
            if not download_url:
                snippet = response.text.strip().replace("\n", " ")[:300]
                raise RuntimeError(
                    f"Samco did not return a CSV, zip, or download link for {segment} "
                    f"bhavcopy: {snippet}"
                )

            download = await retry_async(
                lambda: client.get(download_url),
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
            )
            download.raise_for_status()
            csv_text = _csv_text_from_content(download.content)
            if csv_text is None:
                snippet = download.text.strip().replace("\n", " ")[:300]
                raise RuntimeError(
                    f"Samco download did not return a CSV or zip for {segment} bhavcopy: {snippet}"
                )
            return csv_text

    async def fetch_fo(self, trade_date: date):
        csv_text = await self._fetch_csv_text(trade_date, "NSEFO")
        return parse_fo_bhavcopy(csv_text, trade_date, source="samco:NSEFO")

    async def fetch_cm(self, trade_date: date):
        csv_text = await self._fetch_csv_text(trade_date, "NSE")
        return parse_cm_bhavcopy(csv_text, trade_date, source="samco:NSE")


class _FirstLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.href is not None or tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.href = unescape(value)
                return


def _csv_text_from_content(content: bytes) -> str | None:
    if content.startswith(b"PK"):
        try:
            return unzip_first_csv(content)
        except BadZipFile:
            return None
    text = content.decode("utf-8-sig", errors="replace")
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line.startswith("<") or "," not in first_line:
        return None
    return text


def _extract_download_url(html: str, base_url: str = SAMCO_URL) -> str | None:
    parser = _FirstLinkParser()
    parser.feed(html)
    return urljoin(base_url, parser.href) if parser.href else None
