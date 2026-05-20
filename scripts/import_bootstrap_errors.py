from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import failed bootstrap JSONL records into error_log.")
    parser.add_argument("--log-file", required=True)
    args = parser.parse_args()

    repo = MarketRepository(await get_pool())
    inserted = 0
    try:
        with Path(args.log_file).open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("event") != "failed":
                    continue
                trade_date = date.fromisoformat(record["trade_date"]) if record.get("trade_date") else None
                await repo.log_error(
                    "bootstrap_history_import",
                    "DataSourceFetchFailure",
                    record,
                    trade_date=trade_date,
                    source="bootstrap_jsonl",
                )
                inserted += 1
        print({"log_file": args.log_file, "inserted": inserted})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
