#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
토스증권 Open API 시장데이터 -> market-dashboard data.json의 kr.trend/flows/fx/vol 갱신.

- 계좌/자산/주문/조건주문 API는 호출하지 않는다 (market-only guard로 강제 차단).
- Client ID/Secret/access token은 어떤 경로로도 출력·저장하지 않는다.
- 실행: TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수로 자격증명 주입 후
    python3 scripts/fetch_toss_market.py --collect-out ARTIFACT.json   # 수집만(데이터 파일 안 건드림)
    python3 scripts/fetch_toss_market.py --apply ARTIFACT.json --data data.json   # 적용만(네트워크·자격증명 불필요)
    python3 scripts/fetch_toss_market.py   # (구) 수집+적용을 저장소 data.json에 바로
- 종료코드(수집): 0=전부 성공, 10=일부 실패(kr.flows 실패 포함), 11=전부 실패/클라이언트 초기화 실패
"""
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import kr_flows as kf  # noqa: E402  (stdlib 전용: 완료 20거래일 선별·검증·집계)

try:
    import requests
except ImportError:  # --apply 경로는 requests 없이도 동작해야 한다
    requests = None
KST = timezone(timedelta(hours=9))

BASE = os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com")
CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")

FORBIDDEN_PATH_PARTS = (
    "/accounts", "/holdings", "/orders", "/buying-power", "/sellable-quantity",
    "/commissions", "/conditional-orders",
)


def redact(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "<REDACTED>")
    return text


def safe_error(resp):
    try:
        body = resp.json()
        err = body.get("error", body)
        return {
            "status": resp.status_code,
            "code": err.get("code"),
            "requestId": err.get("requestId"),
            "message": err.get("message"),
        }
    except Exception:
        return {"status": resp.status_code, "body": redact(resp.text[:300], [CLIENT_ID, CLIENT_SECRET])}


class TossClient:
    def __init__(self):
        if not CLIENT_ID or not CLIENT_SECRET:
            raise RuntimeError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 필요합니다.")
        self._token = None

    def token(self):
        if self._token:
            return self._token
        r = requests.post(
            f"{BASE}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"token_failed: {safe_error(r)}")
        access = r.json().get("access_token")
        if not access:
            raise RuntimeError("token_missing_in_response")
        self._token = access
        return access

    def get(self, path, params=None, retries=3):
        if any(x in path for x in FORBIDDEN_PATH_PARTS):
            raise RuntimeError(f"forbidden_market_only_guard: {path}")
        for attempt in range(retries):
            r = requests.get(
                f"{BASE}{path}",
                headers={"Authorization": f"Bearer {self.token()}"},
                params=params or {},
                timeout=20,
            )
            if r.status_code == 429:
                wait = 1.5 * (attempt + 1)
                time.sleep(wait)
                continue
            if not r.ok:
                raise RuntimeError(f"request_failed {path}: {safe_error(r)}")
            return r.json()["result"]
        raise RuntimeError(f"rate_limited_after_retries: {path}")


def fetch_candles(client, symbol, min_points=220):
    """최신 -> 과거 순으로 candles를 모아 chronological(과거->최신) close 리스트 반환."""
    all_candles = []
    before = None
    while len(all_candles) < min_points:
        params = {"interval": "1d", "count": "200"}
        if before:
            params["before"] = before
        page = client.get(f"/api/v1/market-indicators/{symbol}/candles", params)
        candles = page["candles"]
        if not candles:
            break
        all_candles.extend(candles)
        before = page.get("nextBefore")
        if not before:
            break
    all_candles.reverse()  # 과거 -> 최신
    dates = [c["timestamp"][:10] for c in all_candles]
    closes = [float(c["closePrice"]) for c in all_candles]
    return dates, closes


def compute_trend(dates, closes):
    last = closes[-1]
    date = dates[-1]
    out = {
        "num": None, "val": None, "tone": "gray", "note": None,
        "src_date": date[5:7] + "-" + date[8:10], "auto": True,
    }
    if len(closes) < 50:
        raise ValueError("insufficient candles for trend (need >=50)")
    ma50 = sum(closes[-50:]) / 50
    r20 = (last / closes[-21]) - 1 if len(closes) >= 21 else None
    dd63 = None
    if len(closes) >= 63:
        dd63 = (last / max(closes[-63:])) - 1
    above50 = last > ma50

    ma_now = ma200_up = above200 = golden = None
    if len(closes) >= 200:
        ma_now = sum(closes[-200:]) / 200
        above200 = last > ma_now
        golden = ma50 > ma_now
        if len(closes) >= 220:
            ma_prev = sum(closes[-220:-20]) / 200
            ma200_up = ma_now > ma_prev

    if above200 is True and ma200_up is True:
        tone, val = "green", "상승 · 200일선 위"
    elif above200 is True:
        tone, val = "amber", "200일선 위 · 기울기 확인 필요"
    elif above200 is False:
        tone, val = "red", "200일선 이탈"
    else:
        tone, val = ("green", "50일선 위") if above50 else ("amber", "50일선 이탈")

    note_parts = [f"{last:,.2f} / 50일선 {ma50:,.2f}"]
    if ma_now is not None:
        note_parts.append(f"200일선 {ma_now:,.2f}")
    if dd63 is not None:
        note_parts.append(f"63일 고점대비 {dd63*100:.1f}%")
    if r20 is not None:
        note_parts.append(f"20일 {r20*100:.1f}%")

    out.update({
        "num": 1 if tone == "green" else 0,
        "val": val, "tone": tone, "note": " · ".join(note_parts),
        "above50": above50, "above200": above200, "ma200_up": ma200_up, "golden": golden,
        "dd63": round(dd63, 4) if dd63 is not None else None,
        "r20": round(r20, 4) if r20 is not None else None,
    })
    return out


def compute_vol(dates, closes):
    rets = [(closes[i] / closes[i - 1]) - 1 for i in range(len(closes) - 20, len(closes)) if i > 0]
    if not rets:
        raise ValueError("insufficient candles for vol")
    sd = statistics.pstdev(rets) * 100
    last3 = rets[-3:]
    max_move = max(abs(r) for r in last3) * 100 if last3 else 0.0
    tone = "green" if sd < 1.2 else ("amber" if sd <= 2.0 else "red")
    return {
        "num": round(sd, 1), "auto": True, "tone": tone,
        "val": f"20일 변동성 ±{sd:.1f}%",
        "note": f"최근 3거래일 최대 등락폭 {max_move:.1f}% · 20일 일간수익률 표준편차 {sd:.2f}%",
        "src_date": dates[-1][5:7] + "-" + dates[-1][8:10],
    }


def compute_flows(client, now_kst=None):
    """완료 거래일 20개 기준 외국인 5/20일·기관 5일 순매수(조원). 원자료(records)도 함께 반환."""
    now_kst = now_kst or datetime.now(KST)
    params = {"interval": "1d", "count": str(kf.REQUEST_COUNT)}
    body = client.get("/api/v1/market-indicators/KOSPI/investor-trading", params)
    records = body["records"]
    flows = kf.compute_flows_from_records(records, now_kst)
    raw = {"endpoint": "/api/v1/market-indicators/KOSPI/investor-trading", "params": params,
           "observed_at": flows["observed_at"], "records": records}
    return flows, raw


def compute_fx(client):
    body = client.get("/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"})
    rate = float(body["rate"])
    tone = "green" if rate < 1350 else ("amber" if rate <= 1520 else "red")
    label = {"green": "안정", "amber": "약세", "red": "급락 경고"}[tone]
    date = body["validFrom"][:10]
    return {
        "num": round(rate), "auto": True, "tone": tone,
        "val": f"~{round(rate):,}원 · {label}",
        "note": f"validFrom {body['validFrom']}",
        "src_date": date[5:7] + "-" + date[8:10],
    }


def compute_ranking(client):
    body = client.get("/api/v1/rankings", {
        "marketCountry": "KR", "type": "MARKET_TRADING_AMOUNT", "duration": "realtime",
        "count": "20", "excludeInvestmentCaution": "true",
    })
    return {
        "rankedAt": body["rankedAt"],
        "symbols": [r["symbol"] for r in body["rankings"]],
    }


def fetch_samsung_pbr():
    """Valueline 투자지표 페이지에서 삼성전자 현재 PBR을 읽는다.

    Toss Open API는 PBR을 제공하지 않으므로 가격/수급은 Toss, PBR은
    출처를 명시한 외부 투자지표로만 표시한다. 실패하면 기존값을 유지한다.
    """
    url = "https://www.valueline.co.kr/finance/investment/005930"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    text = r.text
    date_match = re.search(r"기준\s*[:：]\s*(\d{2}/\d{2})", text)
    pbr_match = re.search(r"<dt>\s*PBR\s*</dt>\s*<dd>\s*([0-9]+(?:\.[0-9]+)?)\s*배\s*</dd>", text, re.S)
    if not pbr_match:
        pbr_match = re.search(r"PBR\s*([0-9]+(?:\.[0-9]+)?)\s*배", text)
    if not pbr_match:
        pbr_match = re.search(r"PBR[^0-9]{0,80}([0-9]+(?:\.[0-9]+)?)", text, re.S)
    if not pbr_match:
        raise ValueError("samsung_pbr_not_found")
    pbr = float(pbr_match.group(1))
    src_date = date_match.group(1).replace("/", "-") if date_match else datetime.now(KST).strftime("%m-%d")
    tone = "green" if pbr <= 1.0 else ("amber" if pbr <= 1.5 else "red")
    gap = ((pbr / 1.0) - 1) * 100
    return {
        "num": round(pbr, 2),
        "val": f"삼성전자 PBR {pbr:.2f}배",
        "src_date": src_date,
        "auto": True,
        "tone": tone,
        "source": "Valueline",
        "threshold": 1.0,
        "note": f"사용자 기준: PBR 1.0 이하 강매수. 현재는 기준보다 {gap:.0f}% 높음.",
    }


def fetch_bok_base_rate():
    """한국은행 홈페이지에서 최신 기준금리를 파싱한다.

    kr.bok가 수동 고정값으로 남아 시장 판단 신뢰를 훼손한 사고(2026-09-02) 후속.
    실패하면 기존값 유지하되, 성공 시 auto=True로 표시한다.
    """
    url = "https://www.bok.or.kr/portal/main/main.do"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    text = r.text
    m = re.search(
        r"통화정책방향 \((\d{4})\.(\d{1,2})\.(\d{1,2})\).*?"
        r"기준금리를\s*([0-9.]+)%에서\s*([0-9.]+)%로\s*<br/>\s*([0-9.]+)%p\s*상향",
        text,
        re.S,
    )
    direction = "인상"
    if not m:
        m = re.search(
            r"통화정책방향 \((\d{4})\.(\d{1,2})\.(\d{1,2})\).*?"
            r"기준금리를\s*([0-9.]+)%에서\s*([0-9.]+)%로\s*<br/>\s*([0-9.]+)%p\s*하향",
            text,
            re.S,
        )
        direction = "인하"
    if not m:
        m2 = re.search(r"기준금리를 현재의\s*([0-9.]+)%\s*수준에서\s*([0-9.]+)%로\s*(상향|하향)", text, re.S)
        if not m2:
            raise ValueError("bok_base_rate_not_found")
        prev, current, dir_word = float(m2.group(1)), float(m2.group(2)), m2.group(3)
        direction = "인상" if dir_word == "상향" else "인하"
        src_date = datetime.now(KST).strftime("%m-%d")
        delta = abs(current - prev)
    else:
        y, mo, d, prev_s, current_s, delta_s = m.groups()
        prev, current, delta = float(prev_s), float(current_s), float(delta_s)
        src_date = f"{int(mo):02d}-{int(d):02d}"
    tone = "red" if direction == "인상" else ("green" if direction == "인하" else "amber")
    return {
        "num": round(current, 2),
        "val": f"{current:.2f}%로 {direction}({'긴축' if direction == '인상' else '완화'})",
        "src_date": src_date,
        "auto": True,
        "tone": tone,
        "note": f"한국은행 통화정책방향: {prev:.2f}% → {current:.2f}% ({delta:.2f}%p {direction})",
        "source": "BOK",
    }


def collect(client, now_kst=None):
    """시장 데이터만 조회해 아티팩트(dict)로 반환. data.json은 건드리지 않는다."""
    now_kst = now_kst or datetime.now(KST)
    fields, failed, raw = {}, {}, {}

    def step(names, fn):
        try:
            out = fn()
            for n, v in zip(names, out):
                fields[n] = v
        except Exception as e:  # 실패는 기록만, 다른 지표는 계속
            for n in names:
                failed[n] = redact(str(e), [CLIENT_ID, CLIENT_SECRET])[:300]

    def trend_vol():
        dates, closes = fetch_candles(client, "KOSPI")
        return compute_trend(dates, closes), compute_vol(dates, closes)

    def flows():
        f, r = compute_flows(client, now_kst)
        raw["kr.flows"] = r
        return (f,)

    step(("kr.trend", "kr.vol"), trend_vol)
    step(("kr.flows",), flows)
    step(("kr.fx",), lambda: (compute_fx(client),))
    step(("kr.ranking",), lambda: (compute_ranking(client),))
    step(("kr.samsung_pbr",), lambda: (fetch_samsung_pbr(),))
    step(("kr.bok",), lambda: (fetch_bok_base_rate(),))
    return {"schema": 1, "collected_at": now_kst.isoformat(timespec="seconds"),
            "fields": fields, "failed": failed, "raw": raw}


def _write_json_atomic(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _exit_code(artifact):
    if not artifact["fields"]:
        return 11
    return 10 if artifact["failed"] else 0


def _report(artifact):
    print(f"updated: {', '.join(artifact['fields']) or '없음'}")
    if artifact["failed"]:
        print("failed (기존값 유지):", file=sys.stderr)
        for k, v in artifact["failed"].items():
            print(f"  - {k}: {v}", file=sys.stderr)
    print("manual (기존값 유지): kr.valuation")


def apply_to_file(artifact, data_path, now_kst=None):
    now_kst = now_kst or datetime.now(KST)
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    kf.apply_artifact(data, artifact, now_kst)
    data["note_freshness"] = ("페이지 생성 시각과 지표별 기준일은 다르다. 국내 수급은 '수급 기준일'(완료 거래일, 연도 포함)을 "
                              "따로 표시하며, 수집 실패 시 이전 값을 지연/실패로 표시한다. 출처: 국내 가격·수급 토스증권 Open API, "
                              "삼성전자 PBR Valueline, 기준금리 한국은행.")
    _write_json_atomic(data_path, data)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-out")
    ap.add_argument("--apply")
    ap.add_argument("--data", default=os.path.join(HERE, "data.json"))
    a = ap.parse_args(argv)
    if a.apply:
        with open(a.apply, encoding="utf-8") as f:
            artifact = json.load(f)
        apply_to_file(artifact, a.data)
        print(f"applied {a.apply} -> {a.data}")
        return 0
    try:
        client = TossClient()
        artifact = collect(client)
    except Exception as e:
        now = datetime.now(KST)
        artifact = {"schema": 1, "collected_at": now.isoformat(timespec="seconds"), "fields": {},
                    "failed": {"kr.client": redact(str(e), [CLIENT_ID, CLIENT_SECRET])[:300],
                               "kr.flows": "client_unavailable"}, "raw": {}}
    _report(artifact)
    if a.collect_out:
        _write_json_atomic(a.collect_out, artifact)
        print(f"artifact -> {a.collect_out}")
    else:
        apply_to_file(artifact, a.data)
    return _exit_code(artifact)


if __name__ == "__main__":
    sys.exit(main())
