from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.sources.nse import NSEArchiveClient


PRICE_TOLERANCE = 0.01


def _diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return abs(float(a) - float(b))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate DB rows against authoritative NSE bhavcopy files.")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--end", help="End date YYYY-MM-DD. Defaults to latest DB trade date.")
    parser.add_argument("--output", default="data/validation_market_data.json")
    args = parser.parse_args()

    settings = get_settings()
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
    repo = MarketRepository(await get_pool())
    try:
        end = date.fromisoformat(args.end) if args.end else await repo.latest_trade_date()
        if end is None:
            raise RuntimeError("No latest options date found and --end was not supplied.")
        dates = [
            row["trade_date"]
            for row in await repo.pool.fetch(
                """
                SELECT DISTINCT trade_date
                FROM equity_historical
                WHERE trade_date <= $1
                  AND symbol = ANY($2::varchar[])
                GROUP BY trade_date
                HAVING COUNT(DISTINCT symbol) = $3
                ORDER BY trade_date DESC
                LIMIT $4
                """,
                end,
                symbols,
                len(symbols),
                args.days,
            )
        ]
        dates = list(reversed(dates))

        client = NSEArchiveClient(settings.nse_request_delay_seconds)
        results = []
        mismatches = []
        for trade_date in dates:
            source_cm = {row.symbol: row for row in await client.fetch_cm(trade_date) if row.symbol in symbols}
            source_fo = await client.fetch_fo(trade_date)
            source_fo_counts = {
                symbol: sum(1 for row in source_fo if row.symbol == symbol)
                for symbol in symbols
            }
            db_equity = {
                row["symbol"]: dict(row)
                for row in await repo.pool.fetch(
                    """
                    SELECT symbol, open::float, high::float, low::float, close::float, volume
                    FROM equity_historical
                    WHERE trade_date = $1 AND symbol = ANY($2::varchar[])
                    """,
                    trade_date,
                    symbols,
                )
            }
            db_fo_counts = {
                row["symbol"]: row["rows"]
                for row in await repo.pool.fetch(
                    """
                    SELECT symbol, COUNT(*) AS rows
                    FROM options_historical
                    WHERE trade_date = $1 AND symbol = ANY($2::varchar[])
                    GROUP BY symbol
                    """,
                    trade_date,
                    symbols,
                )
            }

            for symbol in symbols:
                source = source_cm.get(symbol)
                db = db_equity.get(symbol)
                checks = {
                    "equity_present_source": source is not None,
                    "equity_present_db": db is not None,
                    "fo_source_rows": source_fo_counts.get(symbol, 0),
                    "fo_db_rows": db_fo_counts.get(symbol, 0),
                }
                if source and db:
                    for field in ("open", "high", "low", "close"):
                        checks[f"{field}_diff"] = _diff(getattr(source, field), db[field])
                    checks["volume_diff"] = abs((source.volume or 0) - (db["volume"] or 0))
                    if any((checks[f"{field}_diff"] or 0) > PRICE_TOLERANCE for field in ("open", "high", "low", "close")):
                        mismatches.append({"trade_date": trade_date.isoformat(), "symbol": symbol, "type": "equity_ohlc", "checks": checks})
                    if checks["volume_diff"] != 0:
                        mismatches.append({"trade_date": trade_date.isoformat(), "symbol": symbol, "type": "equity_volume", "checks": checks})
                if checks["fo_source_rows"] != checks["fo_db_rows"]:
                    mismatches.append({"trade_date": trade_date.isoformat(), "symbol": symbol, "type": "fo_row_count", "checks": checks})
                results.append({"trade_date": trade_date.isoformat(), "symbol": symbol, **checks})

        report = {
            "symbols": symbols,
            "dates": [item.isoformat() for item in dates],
            "price_tolerance": PRICE_TOLERANCE,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "checks": results,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, default=str))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
