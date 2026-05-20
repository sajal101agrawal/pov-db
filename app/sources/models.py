from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class OptionBhavcopyRow:
    symbol: str
    trade_date: date
    expiry_date: date
    strike_price: float
    option_type: str
    instrument_type: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    settle_price: float | None
    num_contracts: int | None
    contract_value: float | None
    open_interest: int | None
    change_in_oi: int | None
    source: str


@dataclass(frozen=True)
class EquityBhavcopyRow:
    symbol: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    turnover: float | None
    delivery_volume: int | None
    source: str


@dataclass(frozen=True)
class EquityBar:
    symbol: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    source: str
