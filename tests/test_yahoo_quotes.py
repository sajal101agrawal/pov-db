from __future__ import annotations

from app.sources.yahoo import normalize_live_chart, yahoo_ticker


def test_yahoo_ticker_uses_index_mapping() -> None:
    assert yahoo_ticker("NIFTY") == "^NSEI"
    assert yahoo_ticker("BANKNIFTY", "BANKNIFTY.NS") == "^NSEBANK"
    assert yahoo_ticker("RELIANCE") == "RELIANCE.NS"


def test_normalize_live_chart_extracts_intraday_quote() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 1420.5,
                        "previousClose": 1400.0,
                        "regularMarketTime": 1716532200,
                        "marketState": "REGULAR",
                    },
                    "timestamp": [1716532140, 1716532200],
                    "indicators": {
                        "quote": [
                            {
                                "open": [1410.0, 1420.0],
                                "high": [1421.0, 1430.0],
                                "low": [1401.0, 1419.0],
                                "close": [1420.0, 1420.5],
                                "volume": [100000, 23456],
                            }
                        ]
                    },
                }
            ]
        }
    }

    quote = normalize_live_chart("RELIANCE", "RELIANCE.NS", payload)

    assert quote is not None
    assert quote["symbol"] == "RELIANCE"
    assert quote["current_price"] == 1420.5
    assert quote["high"] == 1430.0
    assert quote["volume"] == 123456
    assert quote["provider"] == "yahoo"
    assert quote["regular_market_time"] is not None
