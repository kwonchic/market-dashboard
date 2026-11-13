#!/usr/bin/env python3
"""Deterministically (re)generate the local Yahoo-chart-shaped fixtures.

These are SYNTHETIC payloads that follow the Yahoo Finance chart JSON schema
(chart.result[0].meta / timestamp / indicators.quote[0]); they are not recorded
live responses. Run: python3 tests/make_fixtures.py
"""
import json, math, os
from datetime import date, datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
LAST_DAY = date(2026, 11, 13)  # a Friday


def trading_days(n, last=LAST_DAY):
    days, d = [], last
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def ts(d, hour, minute):
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).timestamp())


def chart(symbol, currency, tz_name, gmtoffset, open_utc, n, closes, volumes):
    days = trading_days(n)
    stamps = [ts(d, *open_utc) for d in days]
    return {"chart": {"result": [{
        "meta": {"currency": currency, "symbol": symbol, "exchangeTimezoneName": tz_name,
                 "gmtoffset": gmtoffset, "instrumentType": "EQUITY", "dataGranularity": "1d", "range": "1y"},
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes, "volume": volumes, "open": closes, "high": closes, "low": closes}]},
    }], "error": None}}


def series(base, slope, amp, n):
    return [round(base * (1 + slope * i) + amp * math.sin(i / 5.0), 2) for i in range(n)]


def main():
    os.makedirs(OUT, exist_ok=True)
    n = 251
    # AAPL: steady uptrend, last-day volume spike on an up day
    aapl = series(200.0, 0.0012, 3.0, n)
    aapl_vol = [50_000_000 + (i % 7) * 1_000_000 for i in range(n)]
    aapl_vol[-1] = 90_000_000
    # Samsung: downtrend, with a null close (halted day) in the middle
    sam = series(90000.0, -0.0010, 900.0, n)
    sam = [round(c / 100) * 100 for c in sam]
    sam_vol = [15_000_000 + (i % 5) * 500_000 for i in range(n)]
    sam_closes = list(sam)
    sam_closes[100] = None
    sam_vol[100] = None
    files = {
        "chart_aapl_uptrend.json": chart("AAPL", "USD", "America/New_York", -18000, (14, 30), n, aapl, aapl_vol),
        "chart_005930_ks_downtrend.json": chart("005930.KS", "KRW", "Asia/Seoul", 32400, (0, 0), n, sam_closes, sam_vol),
        "chart_short.json": chart("SHORT", "USD", "America/New_York", -18000, (14, 30), 30,
                                  series(10.0, 0.001, 0.1, 30), [1000] * 30),
        "chart_error.json": {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}},
        # malformed: string close, mismatched lengths, missing meta.currency
        "chart_malformed.json": {"chart": {"result": [{
            "meta": {"symbol": "BAD"},
            "timestamp": [1700000000, 1700086400, "x"],
            "indicators": {"quote": [{"close": [1.0, "oops"], "volume": [1, 2, 3]}]}}], "error": None}},
    }
    for name, payload in files.items():
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        print("wrote", name)


if __name__ == "__main__":
    main()
