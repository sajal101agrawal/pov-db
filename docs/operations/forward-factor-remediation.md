# Call/Put Forward Factor Production Remediation

Last validated locally: 2026-06-23.

## Scope

This change retains `fwdfct_3060` as the existing Average Forward Factor and adds:

- `call_iv_30/60/90` and `put_iv_30/60/90`
- `call_fwdfct_3060` and `put_fwdfct_3060`
- the Golden Mispricing OR signal (`Call Forward Factor > 16%` or `Put Forward Factor > 16%`)

The daily EOD pipeline and live NSE option-chain overlay calculate these fields automatically for
all subsequent runs. The historical backfill is a one-time operation for rows already in the
production database.

## Production Sequence

Deploy first. `scripts/deploy_prod.sh` applies the additive columns before the API/worker restart:

```bash
scripts/deploy_prod.sh
```

Preview the historical scope. This command does not write metrics:

```bash
docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
  python scripts/backfill_forward_factors.py
```

Execute after checking the reported date range, symbol count, and metric-row count:

```bash
docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
  python scripts/backfill_forward_factors.py --execute
```

Optional bounds reduce a rerun to a symbol/date subset:

```bash
docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
  python scripts/backfill_forward_factors.py \
  --symbols RELIANCE,TCS --start 2025-01-01 --end 2025-12-31 --execute
```

For an intentional one-command deployment after the preview:

```bash
RUN_FORWARD_FACTOR_BACKFILL=1 scripts/deploy_prod.sh
```

## What Happens to Existing Values

The backfill reads stored `options_historical.iv` and same-day `equity_historical.close`, selects
the closest ATM strike independently for each expiry, and recomputes only the eight new call/put
columns. It does not overwrite `iv_30/60/90`, `fwdv_3060`, or `fwdfct_3060`, so the prior Average
Forward Factor remains unchanged.

Only nearest-strike CE/PE rows are loaded for each symbol/date/expiry, avoiding a full option-chain
transfer into Python. Changes are committed per symbol. Every materially changed row is recorded in
`analytics_metric_audit`, and the run status/summary is recorded in `analytics_backfill_runs`.
Dashboard, history, all-dashboard, and term-structure caches are invalidated after success.

The script is restart-safe and idempotent. A completed second run over unchanged data reports
`updated: 0` and `audited_changes: 0`.

## Verification

Check the latest populated rows:

```sql
SELECT symbol, trade_date,
       fwdfct_3060 AS average_forward_factor,
       call_fwdfct_3060, put_fwdfct_3060
FROM symbol_daily_metrics
ORDER BY trade_date DESC, symbol
LIMIT 50;
```

Check run and audit lineage:

```sql
SELECT run_id, status, started_at, completed_at, parameters, summary
FROM analytics_backfill_runs
ORDER BY started_at DESC
LIMIT 5;
```

Run the normal database validator after the backfill:

```bash
docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
  python scripts/validate_database.py
```

Before historical execution, existing rows legitimately return null call/put fields, show no
call/put rating, and do not enter the Golden strategy. New daily rows are still populated normally.
