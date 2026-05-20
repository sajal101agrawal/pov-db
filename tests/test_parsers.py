from __future__ import annotations

from datetime import date

from app.sources.parsers import parse_cm_bhavcopy, parse_fo_bhavcopy


def test_parse_fo_bhavcopy_filters_options() -> None:
    text = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI
OPTSTK,SBIN,27-Jun-2024,800,CE,10,12,8,9,9,100,123.4,1000,50
FUTSTK,SBIN,27-Jun-2024,0,XX,800,805,790,801,801,10,1,1,0
"""
    rows = parse_fo_bhavcopy(text, date(2024, 6, 1), "unit")
    assert len(rows) == 1
    assert rows[0].symbol == "SBIN"
    assert rows[0].option_type == "CE"
    assert rows[0].settle_price == 9


def test_parse_cm_bhavcopy_supports_legacy_columns() -> None:
    text = """SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY,TOTTRDVAL,DELIV_QTY
SBIN,EQ,800,810,790,805,1000,805000,500
SBIN,BL,800,810,790,805,1000,805000,500
"""
    rows = parse_cm_bhavcopy(text, date(2024, 6, 1), "unit")
    assert len(rows) == 1
    assert rows[0].symbol == "SBIN"
    assert rows[0].close == 805


def test_parse_cm_bhavcopy_supports_current_nse_columns() -> None:
    text = """TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol,TtlTrfVal
SBIN,EQ,800,810,790,805,1000,805000
SGBJUN28,GB,100,101,99,100,10,1000
"""
    rows = parse_cm_bhavcopy(text, date(2025, 4, 24), "unit")
    assert len(rows) == 1
    assert rows[0].symbol == "SBIN"
    assert rows[0].close == 805


def test_parse_fo_bhavcopy_supports_current_nse_columns() -> None:
    text = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,OpnPric,HghPric,LwPric,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal
2025-04-24,2025-04-24,FO,NSE,STO,ABCAPITAL,2025-05-29,185.00,CE,0.00,0.00,0.00,12.05,22.25,13500,0,100,1234.5
"""
    rows = parse_fo_bhavcopy(text, date(2025, 4, 24), "unit")
    assert len(rows) == 1
    assert rows[0].symbol == "ABCAPITAL"
    assert rows[0].instrument_type == "OPTSTK"
    assert rows[0].settle_price == 22.25
