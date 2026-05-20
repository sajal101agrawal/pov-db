from __future__ import annotations

from datetime import date

from app.sources.nse import NSEArchiveClient
from app.sources.samco import SamcoBhavcopyClient


class BhavcopySource:
    def __init__(self, nse: NSEArchiveClient, samco: SamcoBhavcopyClient) -> None:
        self.nse = nse
        self.samco = samco

    async def fetch_fo(self, trade_date: date):
        errors: list[str] = []
        for client in (self.samco, self.nse):
            try:
                return await client.fetch_fo(trade_date)
            except Exception as exc:  # noqa: BLE001 - collect fallback diagnostics
                errors.append(f"{client.__class__.__name__}: {exc}")
        raise RuntimeError("; ".join(errors))

    async def fetch_cm(self, trade_date: date):
        errors: list[str] = []
        for client in (self.samco, self.nse):
            try:
                return await client.fetch_cm(trade_date)
            except Exception as exc:  # noqa: BLE001 - collect fallback diagnostics
                errors.append(f"{client.__class__.__name__}: {exc}")
        raise RuntimeError("; ".join(errors))
