# Formula Audit

Last audited: 2026-05-20.

## Unit Convention

Volatility and interest-rate values are stored as decimals.

| Stored value | Meaning |
|---|---|
| `0.2650` | `26.50%` annualized volatility |
| `0.10` | `10%` annualized interest rate |
| `-0.032` VRP | IV is 3.2 vol points below lagged RV |
| `63.43` win rate | `63.43%` because win-rate fields are percentage-point fields |

Use this rule consistently:

- IV/RV/VRP/skew/forward-vol/result-move fields are decimal volatility or decimal return values.
- `daily_rsi`, `weekly_rsi`, percentiles, `win_rate`, and `vrp_win_rate` are stored on a `0..100` scale.
- `straddle_pnl.underlying_move_pct` is a percent-point value because the strategy report expects price move in `%`.

## Risk-Free Rate

NSE option-chain notes state that `10%` interest rate is applied while computing implied volatility.
The platform therefore uses `interest_rates.rate = 0.10` with source `fixed:nse_iv_10pct`.

This choice is intentional for NSE-IV parity. It is not a live RBI treasury curve. If a future
analytics view needs true risk-free term structure, add it as a separate source/tenor and do not
overwrite the NSE-IV convention row.

## Contract-Level IV and Greeks

Inputs:

- Option market price: `settle_price` first, falling back to `close`
- Spot: same-day underlying `equity_historical.close`
- Strike: `options_historical.strike_price`
- Time: calendar days to exchange expiry divided by 365
- Rate: latest `interest_rates.rate` on or before `trade_date`, tenor `91d`

Formulas:

- IV: Black-Scholes-Merton bisection solve, bounded to `0..500%`.
- Delta/gamma/theta/vega/rho: standard BSM Greeks.
- Theta is per calendar day.
- Vega and rho are per 1% move.
- If IV cannot be solved, all Greeks are stored `NULL`, not `0`, so downstream skew cannot accidentally use a fake delta.

Expected IV-null reasons:

- Option has no meaningful market price.
- Price violates no-arbitrage bounds.
- Deep ITM/OTM contracts where bisection cannot bracket a volatility.
- Stale zero-volume contract prices in bhavcopy.

## Constant-Maturity IV

`iv_30`, `iv_60`, and `iv_90` are synthetic constant-maturity IVs. They are not the raw IV of the selected exchange expiry.

For each expiry, the pipeline takes ATM IV:

```text
ATM_IV = average(ATM_CE_IV, ATM_PE_IV)
```

Then it interpolates in variance space:

```text
T1 = near_dte / 365
T2 = far_dte / 365
T  = target_dte / 365

variance_target = (
  ((T2 - T) / (T2 - T1)) * near_iv^2 * T1
  + ((T - T1) / (T2 - T1)) * far_iv^2 * T2
) / T

iv_target = sqrt(variance_target)
```

If no expiry exists on one side of the target, the nearest available expiry IV is used for the synthetic constant-maturity value.

Important DTE convention:

- `expiry_30d`, `expiry_60d`, `expiry_90d` store the first, second, and third monthly exchange-expiry buckets available on that trade date. For index symbols with weeklies, each calendar month is collapsed to its latest expiry.
- `dte_30`, `dte_60`, `dte_90` store the actual calendar-day difference between that selected expiry and `trade_date`.
- Example: on `2021-06-14`, RELIANCE monthly expiries include `2021-06-24`, `2021-07-29`, and `2021-08-26`, so the selected DTEs are `10`, `45`, and `73`.
- Constant-maturity formulas still use the target horizons `30`, `60`, and `90`; the DTE columns are metadata for the selected exchange expiries.

## Realized Volatility

RV is annualized close-to-close realized volatility:

```text
log_return_t = ln(close_t / close_{t-1})
rv_N = sample_std(last N log returns) * sqrt(252)
```

The pipeline computes `rv_10`, `rv_20`, and `rv_30`.

Null policy:

- `rv_10` needs 11 closes.
- `rv_20` needs 21 closes.
- `rv_30` needs 31 closes.
- The bootstrap refuses sparse windows that jump across long date gaps.

## VRP

VRP follows the architecture lag convention:

```text
vrp(today) = iv_30(today) - rv_30(20 trading days before today)
```

This avoids comparing today’s forward-looking IV with overlapping realized-vol windows.

Null policy:

- `vrp` is `NULL` until there is a valid lagged `rv_30`.

## Forward Volatility and Ratios

Forward vol between 30 and 60 days:

```text
fwdv_3060 = sqrt((iv_60^2 * 60 - iv_30^2 * 30) / 30)
```

Forward factor:

```text
fwdfct_3060 = fwdv_3060 / iv_30
```

Slope:

```text
iv_slope_3060 = (iv_60 - iv_30) / 30
```

Ratios:

```text
iv30_rv30_ratio  = iv_30 / rv_30
iv30_fev30_ratio = iv_30 / fev_30
```

Null policy:

- Any denominator `NULL` or `<= 0` makes the ratio `NULL`.
- Negative forward variance makes `fwdv_3060` `NULL`.

## Skew

Delta skew:

```text
skew_D = IV(put closest to abs(delta)=D) - IV(call closest to delta=D)
```

Computed for `D = 0.20`, `0.25`, and `0.30`.

`smoothed_skew` is the average of valid `skew_20`, `skew_25`, and `skew_30`.

## RSI

RSI uses simple average gains/losses:

```text
RS = avg_gain / avg_loss
RSI = 100 - (100 / (1 + RS))
```

- Daily RSI needs 15 daily closes.
- Weekly RSI uses last close per ISO week and needs 15 weekly closes.

## Straddle Backtest

The strategy is the initial short ATM straddle strategy from the BE spec:

1. For each symbol and trade date, use `equity_historical.open` as entry spot.
2. Use the selected 30-day horizon exchange expiry (`expiry_30d`).
3. Pick the strike nearest to entry spot.
4. Entry = ATM CE open + ATM PE open.
5. Exit = same ATM CE close + same ATM PE close.
6. PnL = entry total - exit total.
7. `is_winner = pnl > 0`.
8. `has_result_event` is true when `events` has a same-day `RESULT`.

This aligns with the strategy document’s “short straddle entry at morning open, exit at EOD close” method.
