#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
시장 신호 대시보드 — 데이터 수집기
- 실행: python3 fetch_data.py  ->  data.json의 자동 지표를 최신값으로 갱신
- API 키 불필요: FRED는 공개 CSV(fredgraph.csv), 환율은 open.er-api.com, CAPE는 multpl 파싱
- 자동 갱신 대상: us.hy, us.vix, us.cape, us.trend(SP500 200일선), kr.fx
- 수동 유지: kr.valuation/flows/bok/vol, us.breadth (무료 소스 없음 → 기존값 유지, src_date로 구분)
- 실패한 지표는 기존값을 그대로 두고 stderr에 기록 (파이프라인 전체는 계속 진행)
"""
import json, os, re, subprocess, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def fetch(url, timeout=30, browser_ua=False):
    # urllib은 FRED에서 간헐 타임아웃 → curl 사용 (로컬·GitHub 러너 모두 내장)
    # FRED는 브라우저 UA를 보내면 빈 응답/차단 → 기본 curl UA 사용. multpl은 반대로 브라우저 UA 필요.
    cmd = ["curl", "-sL", "--max-time", str(timeout)]
    if browser_ua:
        cmd += ["-A", UA]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        raise ConnectionError(f"curl failed ({r.returncode}): {url}")
    return r.stdout

def fred_series(series_id, days=400):
    """FRED 공개 CSV에서 (date, value) 리스트 반환. 결측치('.')는 제외."""
    start = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    csv = fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}")
    rows = []
    for line in csv.strip().splitlines()[1:]:
        date, _, val = line.partition(",")
        val = val.strip()
        if val and val != ".":
            rows.append((date, float(val)))
    if not rows:
        raise ValueError(f"{series_id}: no data")
    return rows

def mmdd(date_str):
    return date_str[5:7] + "-" + date_str[8:10]

def upd_hy(d):
    date, v = fred_series("BAMLH0A0HYM2", days=30)[-1]
    bp = round(v * 100)
    label = "저점권" if bp < 300 else ("주의 구간" if bp <= 400 else "경고 구간")
    d["us"]["hy"].update({"num": bp, "val": f"{bp}bp · {label}", "src_date": mmdd(date)})

def upd_vix(d):
    date, v = fred_series("VIXCLS", days=30)[-1]
    label = "잠잠" if v < 20 else ("경계" if v <= 28 else "공포")
    d["us"]["vix"].update({"num": round(v, 1), "val": f"{v:.0f} · {label}", "src_date": mmdd(date)})

def upd_trend_us(d):
    rows = fred_series("SP500", days=420)
    closes = [v for _, v in rows]
    if len(closes) < 210:
        raise ValueError("SP500: not enough history for 200d MA")
    ma_now = sum(closes[-200:]) / 200
    ma_prev = sum(closes[-220:-20]) / 200  # 약 한 달 전 200일선 (기울기 판정)
    last = closes[-1]
    date = rows[-1][0]
    if last > ma_now and ma_now > ma_prev:
        tone, val, note = "green", "상승 · 200일선 위", f"S&P {last:,.0f} > 200일선 {ma_now:,.0f} · 우상향"
    elif last > ma_now:
        tone, val, note = "amber", "200일선 위 · 기울기 둔화", f"S&P {last:,.0f} > 200일선 {ma_now:,.0f} · 선 평탄/하락"
    else:
        tone, val, note = "red", "200일선 이탈", f"S&P {last:,.0f} < 200일선 {ma_now:,.0f}"
    d["us"]["trend"].update({"num": 1 if tone == "green" else 0, "val": val,
                             "tone": tone, "note": note, "src_date": mmdd(date)})

def upd_fx(d):
    body = json.loads(fetch("https://open.er-api.com/v6/latest/USD"))
    v = round(body["rates"]["KRW"])
    label = "안정" if v < 1350 else ("약세" if v <= 1520 else "급락 경고")
    d["kr"]["fx"].update({"num": v, "val": f"~{v:,}원 · {label}",
                          "src_date": datetime.now(KST).strftime("%m-%d")})
    d["kr"]["fx"].pop("note", None)

def upd_cape(d):
    html = fetch("https://www.multpl.com/shiller-pe/table/by-month", browser_ua=True)
    m = re.search(r'Current Shiller PE Ratio is ([\d.]+)', html)
    if not m:
        raise ValueError("multpl: CAPE value not found")
    v = float(m.group(1))
    label = "매우 비쌈" if v > 40 else ("비쌈" if v >= 38 else "보통")
    d["us"]["cape"].update({"num": v, "val": f"{label} · CAPE {v:.1f}",
                            "src_date": datetime.now(KST).strftime("%m-%d")})

def main():
    path = os.path.join(HERE, "data.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ok, failed = [], []
    for name, fn in [("hy", upd_hy), ("vix", upd_vix), ("trend_us", upd_trend_us),
                     ("fx", upd_fx), ("cape", upd_cape)]:
        try:
            fn(data)
            ok.append(name)
        except Exception as e:
            failed.append(name)
            print(f"[fetch fail] {name}: {e}", file=sys.stderr)
    data["asOf"] = datetime.now(KST).strftime("%Y-%m-%d")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"updated: {', '.join(ok) or '없음'} / kept previous: {', '.join(failed) or '없음'}")
    print("manual (기존값 유지): us.breadth, kr.valuation, kr.flows, kr.bok, kr.vol")

if __name__ == "__main__":
    main()
