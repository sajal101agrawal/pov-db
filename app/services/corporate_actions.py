from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any, Iterable

from app.services.calculations import (
    MAX_ABS_RV_LOG_RETURN,
    rsi,
    yang_zhang_realized_vol,
)


RV_CALCULATION_VERSION = 2
SUSPICIOUS_OVERNIGHT_GAP = 0.20
RV_WINDOWS = (10, 20, 30, 60, 90)

STATUS_CLEAN = "CLEAN"
STATUS_ADJUSTED = "CORPORATE_ACTION_ADJUSTED"
STATUS_PENDING = "UNRELIABLE_ACTION_FACTOR"
STATUS_SUSPICIOUS = "UNRELIABLE_SUSPICIOUS_GAP"
STATUS_INVALID = "UNRELIABLE_INVALID_SERIES"
STATUS_INSUFFICIENT = "INSUFFICIENT_HISTORY"
STATUS_SPARSE = "SPARSE_HISTORY"

USABLE_RV_STATUSES = {STATUS_CLEAN, STATUS_ADJUSTED}

ADJUSTING_ACTION_TYPES = {
    "BONUS",
    "SPLIT",
    "CONSOLIDATION",
    "DIVIDEND",
    "RIGHTS",
    "DEMERGER",
    "MERGER",
    "OTHER_PRICE_ADJUSTMENT",
}

NUMERIC_AUDIT_SCALES = {
    "rv_10": 8,
    "rv_20": 8,
    "rv_30": 8,
    "rv_60": 8,
    "rv_90": 8,
    "rv_10_raw": 8,
    "rv_20_raw": 8,
    "rv_30_raw": 8,
    "rv_60_raw": 8,
    "rv_90_raw": 8,
    "vrp": 8,
    "iv30_rv30_ratio": 8,
    "daily_rsi": 4,
    "weekly_rsi": 4,
}

STATE_AUDIT_FIELDS = (
    "rv_data_status",
    "rv_calculation_version",
    "vrp_signal_enabled",
)


@dataclass(frozen=True)
class AdjustmentResult:
    rows: list[dict[str, Any]]
    status: str
    actions: list[dict[str, Any]]
    suspicious_gaps: list[dict[str, Any]]

    @property
    def usable(self) -> bool:
        return self.status in USABLE_RV_STATUSES


def classify_nse_action(description: str) -> str | None:
    text = _normalise(description)
    if "bonus" in text:
        return "BONUS"
    if "split" in text or "sub division" in text or "subdivision" in text:
        return "SPLIT"
    if "consolidation" in text or "consolidate" in text:
        return "CONSOLIDATION"
    if "rights" in text or "right issue" in text:
        return "RIGHTS"
    if "demerger" in text or "de merger" in text or "hive off" in text:
        return "DEMERGER"
    if "merger" in text or "amalgamation" in text:
        return "MERGER"
    if "dividend" in text:
        return "DIVIDEND"
    if any(
        keyword in text
        for keyword in (
            "capital reduction",
            "reduction of capital",
            "reorganisation of capital",
            "reorganization of capital",
            "scheme of arrangement",
            "spin off",
        )
    ):
        return "OTHER_PRICE_ADJUSTMENT"
    return None


def parse_action_terms(description: str, face_value: float | None = None) -> dict[str, Any]:
    """Parse NSE action text into a pre-ex-date price multiplier or factor inputs.

    ``price_multiplier`` is multiplied into every pre-ex-date OHLC field. For a
    1:1 bonus it is 0.5; for a split from face value 10 to 2 it is 0.2.
    """

    action_type = classify_nse_action(description)
    result: dict[str, Any] = {
        "action_type": action_type,
        "price_multiplier": None,
        "cash_amount": None,
        "rights_new_shares": None,
        "rights_held_shares": None,
        "subscription_price": None,
        "adjustment_status": "PENDING_FACTOR",
        "factor_source": None,
    }
    if action_type is None:
        return result

    ratio = _parse_ratio(description)
    if action_type == "BONUS" and ratio:
        new_shares, held_shares = ratio
        denominator = new_shares + held_shares
        if denominator > 0:
            result.update(
                price_multiplier=held_shares / denominator,
                adjustment_status="VERIFIED",
                factor_source="NSE_DECLARED_TERMS",
            )
        return result

    if action_type in {"SPLIT", "CONSOLIDATION"}:
        values = re.search(
            r"from\s+(?:rs|re)\.?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*"
            r"(?:per\s+share\s+)?to\s+(?:rs|re)\.?\s*([0-9]+(?:\.[0-9]+)?)",
            description,
            flags=re.IGNORECASE,
        )
        if values:
            old_face, new_face = (float(values.group(1)), float(values.group(2)))
            if old_face > 0 and new_face > 0:
                result.update(
                    price_multiplier=new_face / old_face,
                    adjustment_status="VERIFIED",
                    factor_source="NSE_DECLARED_TERMS",
                )
        return result

    if action_type == "DIVIDEND":
        amounts = re.findall(
            r"(?:rs|re)\.?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*per\s+(?:share|sh)",
            description,
            flags=re.IGNORECASE,
        )
        if amounts:
            result["cash_amount"] = sum(float(value) for value in amounts)
        return result

    if action_type == "RIGHTS" and ratio:
        new_shares, held_shares = ratio
        result["rights_new_shares"] = new_shares
        result["rights_held_shares"] = held_shares
        premium = re.search(
            r"@\s*premium\s*(?:rs|re)?\.?\s*([0-9]+(?:\.[0-9]+)?)",
            description,
            flags=re.IGNORECASE,
        )
        explicit_price = re.search(
            r"@\s*(?:rs|re)\.?\s*([0-9]+(?:\.[0-9]+)?)",
            description,
            flags=re.IGNORECASE,
        )
        if premium and face_value is not None:
            result["subscription_price"] = float(premium.group(1)) + float(face_value)
        elif explicit_price:
            result["subscription_price"] = float(explicit_price.group(1))
        elif "at par" in description.lower() and face_value is not None:
            result["subscription_price"] = float(face_value)
        return result

    return result


def derive_price_multiplier(
    action: dict[str, Any],
    previous_close: float | None,
    *,
    same_date_action_count: int = 1,
) -> tuple[float | None, str | None]:
    existing = _positive_float(action.get("price_multiplier"))
    if existing is not None:
        return existing, str(action.get("factor_source") or "NSE_DECLARED_TERMS")
    if previous_close is None or previous_close <= 0:
        return None, None

    action_type = str(action.get("action_type") or "")
    # Cash/rights terms can be ambiguous when combined with a share action on
    # the same ex-date. Keep the window disabled until a verified combined
    # factor is supplied instead of guessing the order of adjustments.
    if same_date_action_count > 1 and action_type in {"DIVIDEND", "RIGHTS"}:
        return None, None

    if action_type == "DIVIDEND":
        cash_amount = _positive_float(action.get("cash_amount"))
        if cash_amount is None or cash_amount >= previous_close:
            return None, None
        return (previous_close - cash_amount) / previous_close, "PRIOR_CLOSE_FORMULA"

    if action_type == "RIGHTS":
        new_shares = _positive_float(action.get("rights_new_shares"))
        held_shares = _positive_float(action.get("rights_held_shares"))
        subscription_price = _positive_float(action.get("subscription_price"))
        if new_shares is None or held_shares is None or subscription_price is None:
            return None, None
        terp = (held_shares * previous_close + new_shares * subscription_price) / (
            held_shares + new_shares
        )
        multiplier = terp / previous_close
        return (multiplier, "PRIOR_CLOSE_FORMULA") if multiplier > 0 else (None, None)

    return None, None


def adjust_ohlc_for_actions(
    rows: Iterable[dict[str, Any]],
    actions: Iterable[dict[str, Any]],
    *,
    suspicious_gap_threshold: float = SUSPICIOUS_OVERNIGHT_GAP,
) -> AdjustmentResult:
    selected = [dict(row) for row in rows]
    if not selected:
        return AdjustmentResult([], STATUS_INSUFFICIENT, [], [])
    start_date = selected[0]["trade_date"]
    end_date = selected[-1]["trade_date"]
    relevant = [
        dict(action)
        for action in actions
        if action.get("action_type") in ADJUSTING_ACTION_TYPES
        and start_date < action.get("ex_date") <= end_date
        and action.get("adjustment_status") != "IGNORED"
    ]
    pending = [
        action
        for action in relevant
        if action.get("adjustment_status") != "VERIFIED"
        or _positive_float(action.get("price_multiplier")) is None
    ]
    if pending:
        return AdjustmentResult(selected, STATUS_PENDING, _action_details(relevant), [])

    adjusted: list[dict[str, Any]] = []
    for row in selected:
        multiplier = math.prod(
            float(action["price_multiplier"])
            for action in relevant
            if row["trade_date"] < action["ex_date"]
        )
        item = dict(row)
        for field in ("open", "high", "low", "close"):
            if item.get(field) is not None:
                item[field] = float(item[field]) * multiplier
        adjusted.append(item)

    gaps = suspicious_overnight_gaps(adjusted, suspicious_gap_threshold)
    if gaps:
        return AdjustmentResult(adjusted, STATUS_SUSPICIOUS, _action_details(relevant), gaps)
    status = STATUS_ADJUSTED if relevant else STATUS_CLEAN
    return AdjustmentResult(adjusted, status, _action_details(relevant), [])


def suspicious_overnight_gaps(
    rows: Iterable[dict[str, Any]], threshold: float = SUSPICIOUS_OVERNIGHT_GAP
) -> list[dict[str, Any]]:
    selected = list(rows)
    gaps: list[dict[str, Any]] = []
    for previous, current in zip(selected, selected[1:]):
        previous_close = _positive_float(previous.get("close"))
        current_open = _positive_float(current.get("open"))
        if previous_close is None or current_open is None:
            continue
        value = current_open / previous_close - 1.0
        if abs(value) > threshold:
            gaps.append({"trade_date": current["trade_date"].isoformat(), "overnight_gap": value})
    return gaps


def calculate_price_series_metrics(
    ohlc: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    trade_date: date,
) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in ohlc
        if row.get("trade_date")
        and _positive_float(row.get("open")) is not None
        and _positive_float(row.get("high")) is not None
        and _positive_float(row.get("low")) is not None
        and _positive_float(row.get("close")) is not None
    ]
    metrics: dict[str, Any] = {}
    details: dict[str, Any] = {"windows": {}}
    for window in RV_WINDOWS:
        raw_value, adjusted_value, result = _rv_window(rows, actions, trade_date, window)
        metrics[f"rv_{window}_raw"] = raw_value
        metrics[f"rv_{window}"] = adjusted_value
        details["windows"][f"rv_{window}"] = {
            "status": result.status,
            "actions": result.actions,
            "suspicious_gaps": result.suspicious_gaps,
        }

    rsi_result = adjust_ohlc_for_actions(rows, actions)
    if rsi_result.usable:
        closes = [float(row["close"]) for row in rsi_result.rows]
        metrics["daily_rsi"] = rsi(closes, 14)
        metrics["weekly_rsi"] = rsi(_weekly_closes(rsi_result.rows), 14)
    else:
        metrics["daily_rsi"] = None
        metrics["weekly_rsi"] = None
    details["rsi"] = {
        "status": rsi_result.status,
        "actions": rsi_result.actions,
        "suspicious_gaps": rsi_result.suspicious_gaps,
    }

    metrics["rv_data_status"] = details["windows"]["rv_30"]["status"]
    metrics["rv_adjustment_details"] = _compact_adjustment_details(details)
    metrics["rv_calculation_version"] = RV_CALCULATION_VERSION
    return metrics


def _rv_window(
    rows: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    trade_date: date,
    window: int,
) -> tuple[float | None, float | None, AdjustmentResult]:
    if len(rows) < window + 1:
        result = AdjustmentResult([], STATUS_INSUFFICIENT, [], [])
        return None, None, result
    selected = rows[-(window + 1) :]
    if (trade_date - selected[0]["trade_date"]).days > int(window * 2.5) + 10:
        result = AdjustmentResult(selected, STATUS_SPARSE, [], [])
        return None, None, result

    raw_value = _yang_zhang(selected, max_abs_log_return=None)
    result = adjust_ohlc_for_actions(selected, actions)
    if not result.usable:
        return raw_value, None, result
    adjusted_value = _yang_zhang(result.rows)
    if adjusted_value is None:
        result = AdjustmentResult(
            result.rows, STATUS_INVALID, result.actions, result.suspicious_gaps
        )
    return raw_value, adjusted_value, result


def _yang_zhang(
    rows: list[dict[str, Any]],
    *,
    max_abs_log_return: float | None = MAX_ABS_RV_LOG_RETURN,
) -> float | None:
    return yang_zhang_realized_vol(
        [float(row["open"]) for row in rows],
        [float(row["high"]) for row in rows],
        [float(row["low"]) for row in rows],
        [float(row["close"]) for row in rows],
        max_abs_log_return=max_abs_log_return,
    )


def materially_changed_metric_values(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if any(
        not _same_number(
            old.get(field),
            new.get(field),
            scale=scale,
        )
        for field, scale in NUMERIC_AUDIT_SCALES.items()
    ):
        return True
    return any(old.get(field) != new.get(field) for field in STATE_AUDIT_FIELDS)


def metric_audit_values(values: dict[str, Any]) -> dict[str, Any]:
    numeric = {
        field: (
            round(float(values[field]), scale)
            if values.get(field) is not None
            else None
        )
        for field, scale in NUMERIC_AUDIT_SCALES.items()
    }
    return {
        **numeric,
        **{field: values.get(field) for field in STATE_AUDIT_FIELDS},
    }


def _same_number(left: Any, right: Any, *, scale: int) -> bool:
    if left is None or right is None:
        return left is right
    return round(float(left), scale) == round(float(right), scale)


def _weekly_closes(rows: Iterable[dict[str, Any]]) -> list[float]:
    weekly: dict[tuple[int, int], float] = {}
    for row in rows:
        if row.get("close") is not None:
            weekly[row["trade_date"].isocalendar()[:2]] = float(row["close"])
    return list(weekly.values())


def _action_details(actions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": action.get("id"),
            "ex_date": action["ex_date"].isoformat(),
            "action_type": action.get("action_type"),
            "description": action.get("description"),
            "price_multiplier": (
                float(action["price_multiplier"])
                if action.get("price_multiplier") is not None
                else None
            ),
            "adjustment_status": action.get("adjustment_status"),
        }
        for action in actions
    ]


def _compact_adjustment_details(details: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"windows": {}}
    for name, value in details["windows"].items():
        if (
            value["status"] == STATUS_CLEAN
            and not value["actions"]
            and not value["suspicious_gaps"]
        ):
            continue
        item = {"status": value["status"]}
        if value["actions"]:
            item["actions"] = value["actions"]
        if value["suspicious_gaps"]:
            item["suspicious_gaps"] = value["suspicious_gaps"]
        compact["windows"][name] = item
    rsi_value = details["rsi"]
    if rsi_value["status"] != STATUS_CLEAN or rsi_value["actions"] or rsi_value["suspicious_gaps"]:
        compact["rsi"] = {"status": rsi_value["status"]}
        if rsi_value["actions"]:
            compact["rsi"]["actions"] = rsi_value["actions"]
        if rsi_value["suspicious_gaps"]:
            compact["rsi"]["suspicious_gaps"] = rsi_value["suspicious_gaps"]
    return compact if compact["windows"] or "rsi" in compact else {}


def _parse_ratio(description: str) -> tuple[float, float] | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", description)
    if not match:
        return None
    first, second = float(match.group(1)), float(match.group(2))
    return (first, second) if first > 0 and second > 0 else None


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
