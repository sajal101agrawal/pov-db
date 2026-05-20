from __future__ import annotations

import csv
import io
from datetime import date, datetime
from zipfile import ZipFile

from app.sources.models import EquityBhavcopyRow, OptionBhavcopyRow


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "nan", "None"}:
        return None
    return float(text)


def _int(value: str | None) -> int | None:
    parsed = _float(value)
    return int(parsed) if parsed is not None else None


def _date(value: str | None) -> date:
    if not value:
        raise ValueError("missing date")
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unsupported date format: {text}")


def unzip_first_csv(content: bytes) -> str:
    with ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError("zip does not contain a CSV file")
        return archive.read(names[0]).decode("utf-8-sig")


def parse_fo_bhavcopy(csv_text: str, trade_date: date, source: str) -> list[OptionBhavcopyRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[OptionBhavcopyRow] = []
    for raw in reader:
        instrument = (raw.get("INSTRUMENT") or raw.get("Instr") or "").strip()
        fin_type = (raw.get("FinInstrmTp") or "").strip()
        if not instrument and fin_type:
            instrument = {"STO": "OPTSTK", "IDO": "OPTIDX"}.get(fin_type, fin_type)
        option_type = (raw.get("OPTION_TYP") or raw.get("OPTION_TYPE") or "").strip()
        if not option_type:
            option_type = (raw.get("OptnTp") or "").strip()
        if instrument not in {"OPTSTK", "OPTIDX"} or option_type not in {"CE", "PE"}:
            continue
        rows.append(
            OptionBhavcopyRow(
                symbol=(raw.get("SYMBOL") or raw.get("TckrSymb") or "").strip().upper(),
                trade_date=trade_date,
                expiry_date=_date(raw.get("EXPIRY_DT") or raw.get("XpryDt")),
                strike_price=_float(raw.get("STRIKE_PR") or raw.get("StrkPric")) or 0.0,
                option_type=option_type,
                instrument_type=instrument,
                open=_float(raw.get("OPEN") or raw.get("OpnPric")),
                high=_float(raw.get("HIGH") or raw.get("HghPric")),
                low=_float(raw.get("LOW") or raw.get("LwPric")),
                close=_float(raw.get("CLOSE") or raw.get("ClsPric")),
                settle_price=_float(raw.get("SETTLE_PR") or raw.get("SttlmPric")),
                num_contracts=_int(raw.get("CONTRACTS") or raw.get("TtlTradgVol")),
                contract_value=_float(raw.get("VAL_INLAKH") or raw.get("TtlTrfVal")),
                open_interest=_int(raw.get("OPEN_INT") or raw.get("OpnIntrst")),
                change_in_oi=_int(raw.get("CHG_IN_OI") or raw.get("ChngInOpnIntrst")),
                source=source,
            )
        )
    return rows


def parse_cm_bhavcopy(csv_text: str, trade_date: date, source: str) -> list[EquityBhavcopyRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[EquityBhavcopyRow] = []
    for raw in reader:
        series = (raw.get("SERIES") or raw.get("series") or raw.get("SctySrs") or "").strip()
        if series and series not in {"EQ", "BE"}:
            continue
        symbol = (raw.get("SYMBOL") or raw.get("TckrSymb") or raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            EquityBhavcopyRow(
                symbol=symbol,
                trade_date=trade_date,
                open=_float(raw.get("OPEN") or raw.get("OpnPric")),
                high=_float(raw.get("HIGH") or raw.get("HghPric")),
                low=_float(raw.get("LOW") or raw.get("LwPric")),
                close=_float(raw.get("CLOSE") or raw.get("ClsPric")),
                volume=_int(raw.get("TOTTRDQTY") or raw.get("TtlTradgVol")),
                turnover=_float(raw.get("TOTTRDVAL") or raw.get("TtlTrfVal")),
                delivery_volume=_int(raw.get("DELIV_QTY")),
                source=source,
            )
        )
    return rows
