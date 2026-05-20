from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.sources.nse_events import NSECorporateEventsClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load NSE result event dates into events.")
    parser.add_argument("--symbols", help="Comma-separated symbol list. Defaults to active universe.")
    args = parser.parse_args()

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    try:
        symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else await repo.active_symbols()
        client = NSECorporateEventsClient(settings.nse_request_delay_seconds)
        rows = await client.fetch_result_events(symbols)
        inserted = await repo.upsert_events(rows)
        print({"symbols": len(symbols), "events": inserted})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
