#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOSPI 투자자별(외국인·기관) 수급: 완료 거래일 20개 선별·검증·집계 + 수집 아티팩트 적용 + 신선도 판정.

stdlib 전용(네트워크·자격증명 없음). 수집기(scripts/fetch_toss_market.py)와
화면 생성기(generate.py), 테스트가 공통으로 쓴다.

원칙
- 운영 수집은 별도 KOSPI 일봉 날짜와 최신 완료 20일을 정확히 대조한다.
- 날짜 참조 없이 집계하는 호환 경로는 unverified이며 최신 판단에서 제외한다.
- 당일 레코드는 KST 기준 장 마감 후 확정 시각(COMPLETE_CUTOFF) 이전이면 미완료로 제외.
- 중복(같은 날짜)·주말·결측·비정상 단위는 조용히 고치지 않고 FlowsDataError.
- 정확히 WINDOW(20)개 완료 거래일이 없으면 실패. 실패 시 이전 값은 수정하지 않고 상태만 표시.
"""
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
WINDOW = 20
SHORT = 5
REQUEST_COUNT = 30            # 당일 미완료·휴장 여유분 포함 (20개 요청 시 장중엔 19개만 남는 문제)
COMPLETE_CUTOFF = time(16, 0)  # KST. 15:30 장 마감 후 투자자별 집계 확정 여유
MAX_ABS_DAILY_WON = 1e14       # 하루 한 투자자 매수/매도 금액 상한(100조원). 넘으면 단위 오류로 본다
LEGS = ("foreigner", "institution")


class FlowsDataError(ValueError):
    pass


def _kst_date(raw):
    s = str(raw or "").strip()
    if not s:
        raise FlowsDataError("missing date")
    if "T" in s and len(s) > 10:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise FlowsDataError(f"bad date {s!r}") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).date()
    try:
        return date.fromisoformat(s[:10])
    except ValueError as e:
        raise FlowsDataError(f"bad date {s!r}") from e


def _amount(rec, leg, key):
    try:
        v = rec[leg][key]
    except (KeyError, TypeError):
        raise FlowsDataError(f"missing {leg}.{key} on {rec.get('date')}")
    if v is None or v == "":
        raise FlowsDataError(f"missing {leg}.{key} on {rec.get('date')}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise FlowsDataError(f"non-numeric {leg}.{key} on {rec.get('date')}")
    if f != f or f < 0:
        raise FlowsDataError(f"invalid {leg}.{key} on {rec.get('date')}")
    if f > MAX_ABS_DAILY_WON:
        raise FlowsDataError(f"unit check failed: {leg}.{key}={f:.3e} won on {rec.get('date')}")
    return f


def _normalize(rec):
    d = _kst_date(rec.get("date"))
    vals = {leg: (_amount(rec, leg, "buyAmount"), _amount(rec, leg, "sellAmount")) for leg in LEGS}
    return d, vals


def select_completed_records(records, now_kst, window=WINDOW):
    """최신순 완료 거래일 레코드 window개 반환(각 원소: {date, foreigner, institution} 원 단위 float)."""
    if not isinstance(records, list):
        raise FlowsDataError("records is not a list")
    now_kst = now_kst.astimezone(KST)
    today = now_kst.date()
    by_date = {}
    for r in records:
        d, vals = _normalize(r)
        if d.weekday() >= 5:
            raise FlowsDataError(f"weekend record {d.isoformat()}")
        if d > today:
            continue  # 미래 날짜: 사용 안 함
        if d == today and now_kst.time() < COMPLETE_CUTOFF:
            continue  # 당일 장 미완료
        if d in by_date:
            if by_date[d] != vals:
                raise FlowsDataError(f"conflicting duplicate {d.isoformat()}")
            continue
        by_date[d] = vals
    days = sorted(by_date, reverse=True)
    if len(days) < window:
        raise FlowsDataError(f"insufficient completed trading days: {len(days)} < {window}")
    out = []
    for d in days[:window]:
        v = by_date[d]
        out.append({"date": d.isoformat(),
                    **{leg: {"buy": v[leg][0], "sell": v[leg][1], "net": v[leg][0] - v[leg][1]} for leg in LEGS}})
    return out


def completed_candle_dates(trading_dates, now_kst, window=WINDOW):
    """Independent KOSPI candle dates, never a weekday/holiday estimate."""
    if not isinstance(trading_dates, list):
        raise FlowsDataError("missing KOSPI candle dates")
    now = now_kst.astimezone(KST)
    days = []
    for raw in trading_dates:
        d = _kst_date(raw)
        if d > now.date() or d.weekday() >= 5:
            raise FlowsDataError("invalid KOSPI candle date")
        if d == now.date() and now.time() < COMPLETE_CUTOFF:
            continue
        if d in days:
            raise FlowsDataError("duplicate KOSPI candle date")
        days.append(d)
    days.sort(reverse=True)
    if len(days) < window:
        raise FlowsDataError("insufficient KOSPI candle dates")
    return [d.isoformat() for d in days[:window]]


def compute_flows_from_records(records, now_kst, source="toss-openapi:/api/v1/market-indicators/KOSPI/investor-trading", trading_dates=None):
    sel = select_completed_records(records, now_kst)
    if trading_dates is not None:
        expected = completed_candle_dates(trading_dates, now_kst)
        actual = [r["date"] for r in sel]
        if actual != expected:
            raise FlowsDataError("completed trading-day window differs from KOSPI candles")
    f5 = sum(r["foreigner"]["net"] for r in sel[:SHORT]) / 1e12
    f20 = sum(r["foreigner"]["net"] for r in sel) / 1e12
    i5 = sum(r["institution"]["net"] for r in sel[:SHORT]) / 1e12
    if f5 > 0 and f20 > 0:
        tone, val = "green", "외국인 순매수 지속"
    elif f5 < 0 and f20 < 0:
        tone, val = "red", "외국인 순매도 지속"
    else:
        tone, val = "amber", "외국인 순매수/도 전환 구간"
    end, start = sel[0]["date"], sel[-1]["date"]
    return {
        "num": round(f5, 2), "auto": True, "tone": tone, "val": val,
        "note": f"외국인 5일 {f5:+.2f}조 · 20일 {f20:+.2f}조 · 기관 5일 {i5:+.2f}조",
        "src_date": end[5:7] + "-" + end[8:10],
        "src_date_iso": end,
        "window": {"start": start, "end": end, "trading_days": len(sel)},
        "f5_jo": round(f5, 4), "f20_jo": round(f20, 4), "i5_jo": round(i5, 4),
        "unit": "KRW; 집계는 조원(1e12)",
        "source": source,
        "observed_at": now_kst.astimezone(KST).isoformat(timespec="seconds"),
        "collect_status": "ok",
        "window_validation": "kospi_candles" if trading_dates is not None else "unverified",
        "validated_trading_dates": [r["date"] for r in sel] if trading_dates is not None else [],
    }


# ── 수집 아티팩트 → data.json 적용 ─────────────────────────────────────────
REPLACE_KEYS = {"ranking", "samsung_pbr", "bok"}


def apply_artifact(data, artifact, now_kst):
    """수집 아티팩트를 data(dict)에 반영. kr.* 필드만 건드린다(US·watchlist 비침해).

    성공 필드: 값 갱신. 실패 필드: 이전 값 유지 + 상태만 표시(flows).
    같은 아티팩트를 여러 번 적용해도 결과 동일(멱등).
    """
    kr = data.setdefault("kr", {})
    collected_at = artifact.get("collected_at")
    for path, val in (artifact.get("fields") or {}).items():
        mkt, _, key = path.partition(".")
        if mkt != "kr" or not key:
            continue
        if key in REPLACE_KEYS:
            kr[key] = val
        else:
            kr.setdefault(key, {}).update(val)
        if key == "flows":
            kr["flows"]["collect_status"] = "ok"
            kr["flows"]["last_success_at"] = val.get("observed_at") or collected_at
            kr["flows"]["last_attempt_at"] = collected_at
            kr["flows"].pop("last_error", None)
    failed = artifact.get("failed") or {}
    if "kr.flows" in failed:
        f = kr.setdefault("flows", {})
        f["collect_status"] = "failed"
        f["last_attempt_at"] = collected_at
        f["last_error"] = str(failed["kr.flows"])[:200]
    data["generatedAt"] = now_kst.astimezone(KST).isoformat(timespec="seconds")
    data["asOf"] = now_kst.astimezone(KST).date().isoformat()
    return data


# ── 신선도 ─────────────────────────────────────────────────────────────────
def expected_latest_trading_day(now_kst):
    """휴장일 달력 없이 평일 기준으로 '가장 최근 완료됐어야 할' 거래일."""
    now_kst = now_kst.astimezone(KST)
    d = now_kst.date()
    if d.weekday() >= 5 or now_kst.time() < COMPLETE_CUTOFF:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _weekdays_between(a, b):
    """a < b 일 때 (a, b] 구간 평일 수."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def flows_freshness(flows, now_kst, stale_after=2):
    """state: fresh | delayed(1~2평일 지연, 휴장 가능) | stale | unknown(연도 포함 기준일 없음)."""
    expected = expected_latest_trading_day(now_kst)
    iso = (flows or {}).get("src_date_iso")
    failed = (flows or {}).get("collect_status") == "failed"
    out = {"expected": expected.isoformat(), "src": iso, "lag_weekdays": None, "failed": failed}
    if not iso:
        out["state"] = "unknown"
        return out
    try:
        src = date.fromisoformat(str(iso)[:10])
    except ValueError:
        out["state"] = "unknown"
        return out
    if src > now_kst.astimezone(KST).date() or src > expected:
        out["state"] = "invalid"
        return out
    if failed:
        out["state"] = "stale"
        return out
    if (flows or {}).get("window_validation") == "unverified":
        out["state"] = "unknown"
        return out
    lag = _weekdays_between(src, expected) if src < expected else 0
    out["lag_weekdays"] = lag
    if lag == 0:
        out["state"] = "delayed" if failed else "fresh"
    elif lag <= stale_after:
        out["state"] = "delayed"
    else:
        out["state"] = "stale"
    return out


def parse_generated(data):
    """data.generatedAt(ISO) → aware datetime. 없으면 asOf 날짜 23:59 KST(보수적), 둘 다 없으면 None."""
    g = data.get("generatedAt")
    if g:
        try:
            dt = datetime.fromisoformat(str(g).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=KST)
        except ValueError:
            pass
    a = data.get("asOf")
    if a:
        try:
            return datetime.combine(date.fromisoformat(str(a)[:10]), time(23, 59), KST)
        except ValueError:
            pass
    return None
