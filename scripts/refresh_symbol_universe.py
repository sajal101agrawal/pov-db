from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.sources.nse import NSEArchiveClient
from app.sources.nse_metadata import NSEMetadataClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh active symbol_universe from NSE F&O bhavcopy underlyings.")
    parser.add_argument("--date", help="Bhavcopy date YYYY-MM-DD. Defaults to latest options date in DB.")
    parser.add_argument("--deactivate-missing", action="store_true", help="Mark existing symbols inactive when absent from the NSE F&O file.")
    parser.add_argument(
        "--enrich-quote",
        action="store_true",
        help="Fetch NSE quote-equity metadata for sector, industry, and tick size. Slower but fills more fields.",
    )
    args = parser.parse_args()

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    try:
        trade_date = date.fromisoformat(args.date) if args.date else await repo.latest_trade_date()
        if trade_date is None:
            raise RuntimeError("No latest options date found and --date was not supplied.")

        rows = await NSEArchiveClient(settings.nse_request_delay_seconds).fetch_fo(trade_date)
        symbols: dict[str, str] = {}
        for row in rows:
            symbol_type = "index" if row.instrument_type == "OPTIDX" else "individual_securities"
            symbols[row.symbol] = symbol_type

        if args.deactivate_missing:
            await repo.pool.execute("UPDATE symbol_universe SET is_active = FALSE, updated_at = NOW()")

        inserted = await repo.upsert_discovered_symbols(
            [{"symbol": symbol, "symbol_type": symbol_type} for symbol, symbol_type in sorted(symbols.items())]
        )
        metadata = await NSEMetadataClient(
            settings.nse_request_delay_seconds,
            settings.source_retry_attempts,
            settings.source_retry_base_delay_seconds,
            settings.source_retry_max_delay_seconds,
        ).fetch_metadata(set(symbols), enrich_quote=args.enrich_quote)
        metadata_count = await repo.upsert_symbol_metadata(metadata)
        print(
            {
                "trade_date": trade_date.isoformat(),
                "active_symbols": len(symbols),
                "upserted": inserted,
                "metadata_upserted": metadata_count,
                "quote_enriched": args.enrich_quote,
                "deactivate_missing": args.deactivate_missing,
            }
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
