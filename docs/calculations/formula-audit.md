# Formula Audit

Last audited: 2026-06-23.

## Unit Convention

Volatility and interest-rate values are stored as decimals.

| Stored value | Meaning |
|---|---|
| `0.2650` | `26.50%` annualized volatility |
| `0.10` | `10%` annualized interest rate |
| `-0.032` VRP | Lagged IV is 3.2 vol points below current RV |
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

The same expiry-specific ATM strike is also retained by option side:

```text
ATM strike = strike closest to the same-day underlying close
Call ATM IV = valid CE IV at the ATM strike
Put ATM IV  = valid PE IV at the ATM strike
```

Variance interpolation is performed independently for the average, call, and put series. The
persisted fields are `iv_30/60/90`, `call_iv_30/60/90`, and `put_iv_30/60/90`. A missing call leg
is never replaced by the put IV (and vice versa). The average series continues to average whichever
valid ATM legs are available, preserving the historical behavior.

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

RV is annualized Yang-Zhang realized volatility:

```text
overnight_return_t = ln(open_t / close_{t-1})
open_to_close_return_t = ln(close_t / open_t)

RS_t =
  ln(high_t / close_t) * ln(high_t / open_t)
  + ln(low_t / close_t) * ln(low_t / open_t)

k = 0.34 / (1.34 + ((n + 1) / (n - 1)))

YZ_variance =
  variance(overnight_return)
  + k * variance(open_to_close_return)
  + (1 - k) * average(RS)

rv_N = sqrt(YZ_variance * 252)
```

RV is stored as decimal annualized volatility. `0.142` means `14.2%`; it is not multiplied by
`100` before storage.

The pipeline computes `rv_10`, `rv_20`, `rv_30`, `rv_60`, and `rv_90`.

Corporate-action and null policy:

- `rv_10` needs 11 closes.
- `rv_20` needs 21 closes.
- `rv_30` needs 31 closes.
- `rv_60` needs 61 OHLC bars.
- `rv_90` needs 91 OHLC bars.
- The bootstrap refuses sparse windows that jump across long date gaps.
- Raw NSE OHLC remains unchanged in `equity_historical`.
- Every RV window loads corporate actions whose ex-date crosses that window and applies the
  verified pre-ex-date multiplier to open, high, low, and close before Yang-Zhang is evaluated.
- `rv_10/20/30/60/90` are canonical adjusted values. `rv_*_raw` retains the unadjusted diagnostic
  calculation.
- A pending action factor or unexplained adjusted overnight gap above 20% makes that window
  unreliable. Canonical RV is `NULL`; VRP signal generation is disabled.
- `rv_data_status` describes the RV30/VRP window. Per-window details are stored in
  `rv_adjustment_details`.

## VRP

VRP follows the 20-trading-day shifted-IV convention:

```text
vrp(today) = iv_30(20 trading days before today) - rv_30(today)
```

This compares the earlier forward-looking implied volatility against subsequently realized
volatility. Both values are decimal annualized volatilities, so no `* 100` conversion is applied.

Null policy:

- `vrp` is `NULL` until there is both valid current `rv_30` and valid lagged `iv_30`.

## Forward Volatility and Ratios

Forward vol between 30 and 60 days:

```text
fwdv_3060 = sqrt((iv_60^2 * 60 - iv_30^2 * 30) / 30)
```

Average Forward Factor (legacy field retained for API compatibility):

```text
fwdfct_3060 = (iv_30 / fwdv_3060) - 1
```

For live data, the average IV term used by `fwdfct_3060` requires both ATM call IV and ATM put IV
for that expiry. If a tenor has only one side available, call/put factors can still be calculated
independently, but the average forward factor remains null.

Call and Put Forward Factors apply the same formula independently:

```text
call_fwdv_3060 = sqrt((call_iv_60^2 * 60 - call_iv_30^2 * 30) / 30)
call_fwdfct_3060 = (call_iv_30 / call_fwdv_3060) - 1

put_fwdv_3060 = sqrt((put_iv_60^2 * 60 - put_iv_30^2 * 30) / 30)
put_fwdfct_3060 = (put_iv_30 / put_fwdv_3060) - 1
```

Dashboard rating and the Golden Mispricing Strategy use the required OR rule. Equivalently, the
screening value is `max(call_fwdfct_3060, put_fwdfct_3060)`, ignoring null sides. A symbol crosses
the strategy threshold when either available side is greater than `0.16` (`16%`).

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
- Negative forward variance makes the corresponding average/call/put factor `NULL`.

## Skew

Delta skew:

```text
skew_D = IV(put closest to abs(delta)=D) - IV(call closest to delta=D)
```

Computed for `D = 0.20`, `0.25`, and `0.30`.

Skew is calculated from the expiry chain closest to a 30-calendar-day horizon. This is intentional:
`expiry_30d` remains first monthly expiry metadata, but near-expiry chains can make delta skew unstable
when only a few calendar days remain.

IV legs above `2.0` (`200%`) and absolute skew above `0.75` (`75` vol points) are treated as
unsafe analytics inputs and stored as `NULL` in daily metrics rather than as extreme values.

`smoothed_skew` is the average of valid `skew_20`, `skew_25`, and `skew_30`.

## RSI

RSI uses Wilder's smoothing:

```text
initial_avg_gain = average(gains over first 14 periods)
initial_avg_loss = average(losses over first 14 periods)
avg_gain_t = ((avg_gain_t-1 * 13) + gain_t) / 14
avg_loss_t = ((avg_loss_t-1 * 13) + loss_t) / 14
RS = avg_gain / avg_loss
RSI = 100 - (100 / (1 + RS))
```

- Daily RSI needs 15 daily closes.
- Weekly RSI uses last close per ISO week and needs 15 weekly closes.

## Percentiles and Averages

Null observations are excluded from percentile, average, ratio, and win-rate denominators.

- A percentile is stored as `NULL` when the current metric value is `NULL`.
- For a non-null current value, the trailing history uses only non-null observations for that
  metric on that symbol.
- Aggregate PnL averages use only valid `straddle_pnl` rows where `skip_reason IS NULL`.
- Aggregate VRP averages and `vrp_win_rate` use non-null rows where
  `vrp_signal_enabled=true`, not only dates where the daily straddle row is valid.
- Result-event averages are `NULL` when no loaded result-event rows overlap the symbol history.
- Ratios return `NULL` when the denominator is `NULL`, zero, or negative.

## Straddle Backtest

The strategy is the initial short ATM straddle strategy from the BE spec:

1. For each symbol and trade date, use `equity_historical.open` as entry spot.
2. Use `symbol_daily_metrics.expiry_30d`, the first monthly exchange-expiry bucket available
   on that trade date.
3. Pick the strike nearest to entry spot.
4. Entry = ATM CE open + ATM PE open.
5. Exit = same ATM CE close + same ATM PE close.
6. PnL = entry total - exit total.
7. `is_winner = pnl > 0`.
8. `has_result_event` is true when `events` has a same-day `RESULT`.

`underlying_move_pct` is context only:

```text
underlying_move_pct = (underlying_close - underlying_open) / underlying_open * 100
```

It is not an input into `pnl`; it explains how much the underlying moved during the same session.

Historical NSE bhavcopy is one EOD file per trading day. It contains daily OHLC, not intraday
timestamps. Therefore the historical backtest approximates:

- morning entry with the option contract `OPEN`
- EOD exit with the same option contract `CLOSE`

Daily straddle PnL does not use previous-day option prices. Previous-day entry is used only for
earnings-event analytics, where the strategy enters one trading day before the result and exits on
the next trading-day close after the result.

This aligns with the strategy document’s “short straddle entry at morning open, exit at EOD close”
method at daily-bhavcopy granularity. The “30DTE” strategy bucket is implemented as the platform’s
`expiry_30d` bucket, not the synthetic `iv_30` interpolation horizon. True timed intraday entry/exit
needs a separate intraday or live option-chain feed.

## Earnings Event Backtest

Earnings analytics use a different timing rule from the daily straddle report:

1. Event source identifies a `RESULT` event date.
2. Entry date is the previous loaded trading day before the result date.
3. Exit date is the next loaded trading day after the result date.
4. Entry premium uses the entry-date ATM short straddle close proxy (`total_exit` in `straddle_pnl`,
   because the daily straddle table names the same value as the EOD close).
5. Exit premium uses the same strike and expiry option closes on the exit date.
6. Earnings PnL is short-straddle credit minus exit cost.
7. Actual result move is `abs(exit_underlying_close - entry_underlying_close) / entry_underlying_close`.
8. Implied result move is `entry_premium / entry_underlying_close`.

This implements the requested “one trading day before result announcement to next trading-day close
after result” convention using EOD bhavcopy data. The table names are historical, but the event
aggregate formula uses the correct EOD entry and EOD exit fields.
