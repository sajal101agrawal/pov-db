from __future__ import annotations

from datetime import date, timedelta


class IndiaRiskFreeRateClient:
    """NSE-compatible risk-free source for option IV calculations.

    NSE's option-chain note documents that it applies a fixed 10% interest
    rate while computing displayed implied volatility. Use that convention
    for IV parity with NSE instead of mixing in Yahoo ^IRX, which is the US
    13-week T-bill.
    """

    def __init__(self, fallback_rate: float = 0.10) -> None:
        self.fallback_rate = fallback_rate

    async def fetch_91d_rate(self, start: date, end: date) -> list[tuple[date, float, str]]:
        rows: list[tuple[date, float, str]] = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                rows.append((current, self.fallback_rate, "fixed:nse_iv_10pct"))
            current += timedelta(days=1)
        return rows
