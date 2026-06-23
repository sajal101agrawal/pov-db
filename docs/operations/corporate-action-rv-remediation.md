# Corporate-Action RV Remediation

## Scope

NSE cash-market OHLC in `equity_historical` remains raw and must never be rewritten. Same-day
spot, ATM selection, IV/Greeks, and straddle PnL depend on the price scale that traded on that
date. Corporate-action adjustment is applied only to an in-memory cross-day OHLC window before
Yang-Zhang RV and RSI are calculated.

The source is NSE's equity corporate-actions feed. The loader stores the original declaration,
factor inputs, factor provenance, and raw payload in `corporate_actions`.

## Factor policy

`price_multiplier` is multiplied into pre-ex-date open, high, low, and close values.

| Action | Automatic handling |
|---|---|
| Bonus | Declared new:held ratio; `held / (new + held)` |
| Split/consolidation | New face value divided by old face value |
| Dividend | `(previous_close - cash_per_share) / previous_close` |
| Rights | Theoretical ex-rights price divided by previous close |
| Demerger/merger/ambiguous action | `PENDING_FACTOR`; affected RV and VRP are disabled |

Cash/rights factors are not guessed when another action shares the same ex-date. Supply a verified
combined factor manually:

```sql
UPDATE corporate_actions
SET price_multiplier = :verified_multiplier,
    adjustment_status = 'VERIFIED',
    factor_source = 'MANUAL',
    updated_at = NOW()
WHERE id = :action_id;
```

The next recomputation will use the manual value, and future NSE syncs preserve it.

## Data-status policy

- `CLEAN`: no action or unexplained gap in the RV30 window.
- `CORPORATE_ACTION_ADJUSTED`: verified action factor applied before RV30.
- `UNRELIABLE_ACTION_FACTOR`: action exists but has no verified factor.
- `UNRELIABLE_SUSPICIOUS_GAP`: adjusted overnight gap still exceeds 20%.
- `INSUFFICIENT_HISTORY` / `SPARSE_HISTORY`: existing history policies.

Only `CLEAN` and `CORPORATE_ACTION_ADJUSTED` rows can set `vrp_signal_enabled=true`. APIs return
`vrp=null` when the flag is false. Raw diagnostic values remain in `rv_*_raw`.

Frontend behavior:

- For `CORPORATE_ACTION_ADJUSTED`, display: “Corporate action detected in the RV window; adjusted
  OHLC was used.”
- For any `UNRELIABLE_*` status, display: “RV is unreliable due to an unresolved price event; VRP
  signal is disabled.”
- Use `rv_adjustment_details` for the action type, ex-date, factor, and affected RV windows.

## Production rollout

1. Take a PostgreSQL backup.
2. Deploy the additive schema/code update:

   ```bash
   scripts/deploy_prod.sh
   ```

3. Preview the all-symbol migration. This is read-only:

   ```bash
   docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
     python scripts/backfill_corporate_action_metrics.py
   ```

4. Execute it during a maintenance window:

   ```bash
   docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
     python scripts/backfill_corporate_action_metrics.py --execute
   ```

The backfill processes every symbol already present in `symbol_daily_metrics`; it does not use only
the current F&O universe. It synchronizes NSE actions, resolves safe factors, recomputes RV/RSI/VRP,
rebuilds VRP percentiles and aggregates, and invalidates analytics caches. It does not recalculate
option IV/Greeks or straddle rows.

Material old/new metric values are written to `analytics_metric_audit` under the run recorded in
`analytics_backfill_runs`.

5. Inspect unresolved actions and validation:

   ```bash
   docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
     python scripts/validate_database.py
   ```

Unresolved actions are allowed only when affected rows are marked unreliable and VRP is disabled.
`legacy_rv_calculation`, `invalid_vrp_signal`, and clean/raw mismatch counts must be zero.

6. Verify Reliance around 28-Oct-2024:

   ```sql
   SELECT trade_date, rv_30_raw, rv_30, rv_data_status, vrp, vrp_signal_enabled
   FROM symbol_daily_metrics
   WHERE symbol = 'RELIANCE'
     AND trade_date BETWEEN DATE '2024-10-25' AND DATE '2024-12-15'
   ORDER BY trade_date;
   ```

Expected: affected rows use the `Bonus 1:1` multiplier `0.5`; adjusted RV excludes the mechanical
50% overnight step. Raw RV retains it for audit.

Authoritative NSE bhavcopy spot-check used during implementation:

- 25-Oct-2024 raw close: `2655.70`
- 28-Oct-2024 raw open: `1337.00`
- raw overnight gap: `-49.66%`
- adjusted prior close at factor `0.5`: `1327.85`
- adjusted overnight gap: `+0.69%`

## Ongoing operations

Daily ETL re-syncs the trailing 180 calendar days and next 45 calendar days of NSE actions before
calculating metrics. Loading scheduled actions ahead of the ex-date avoids relying on same-day
publication timing. If NSE action sync fails after retries, the EOD run fails rather than publishing
an unchecked signal. A 20% unexplained overnight-gap check is a second safety net.

For an action entered or corrected later, rerun the backfill for that symbol. Include enough later
history for the longest RV/RSI window; the script also refreshes the subsequent percentile tail.
