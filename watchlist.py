#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심 종목 일일 리포트 — 규칙 기반(결정론) 계산 모듈
- 표준 라이브러리만 사용. 네트워크 전송(curl)은 호출하는 쪽(fetch_data.py)이 fetcher로 주입한다.
- 데이터: Yahoo Finance chart JSON (키 불필요). 지연·누락 가능.
- AI/검색 API를 쓰지 않는다. 점수는 아래 공개된 소수의 규칙으로만 계산된다.
"""
import json, math, re, time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

YAHOO_HOST = "query1.finance.yahoo.com"
CHART_PATH = "/v8/finance/chart/"
SYMBOL_RE = re.compile(r"[A-Za-z0-9^][A-Za-z0-9.\-^=]{0,19}")
CURRENCY_RE = re.compile(r"[A-Z]{3}")
MIN_POINTS = 50  # 이보다 짧으면 payload 거부 (SMA50·RSI·20일 수익률 최소 조건)


class WatchlistDataError(ValueError):
    """payload가 비었거나 깨졌을 때. 메시지는 내부 진단용이며 공개 데이터에 복사하지 않는다."""


# ── URL ──────────────────────────────────────────────────────────────────
def validate_yahoo_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("https required")
    if parts.hostname != YAHOO_HOST or parts.username or parts.password or parts.port is not None:
        raise ValueError("unexpected host")
    if not parts.path.startswith(CHART_PATH):
        raise ValueError("unexpected path")
    return url


def build_chart_url(symbol):
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid symbol")
    url = f"https://{YAHOO_HOST}{CHART_PATH}{quote(symbol, safe='')}?range=1y&interval=1d"
    return validate_yahoo_url(url)


# ── payload 정규화 ───────────────────────────────────────────────────────
def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _regular_session(meta):
    """meta.currentTradingPeriod.regular -> (start, end). 진짜 int(bool 제외)·양수·start<end가 아니면 거부."""
    period = meta.get("currentTradingPeriod")
    reg = period.get("regular") if isinstance(period, dict) else None
    if not isinstance(reg, dict):
        raise WatchlistDataError("missing regular session")
    start, end = reg.get("start"), reg.get("end")
    for v in (start, end):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise WatchlistDataError("bad regular session bounds")
    if start >= end:
        raise WatchlistDataError("bad regular session window")
    return start, end


def normalize_chart(payload, now_ts=None):
    """Yahoo chart JSON -> {"currency", "gmtoffset", "rows": [(ts, close, volume|None)], "intraday_excluded"}.

    - close가 null인 봉은 버린다(거래정지 등). close가 숫자가 아니거나 비유한/0 이하면 payload 전체 거부.
    - meta.currentTradingPeriod.regular(start<end 양의 int)가 없거나 깨졌으면 payload 거부(WatchlistDataError).
    - now_ts가 정규장 [start,end) 안이고 마지막 봉이 그 정규장에 속하면 그 봉은 제외한다(미완성 봉 방지).
    """
    try:
        chart = payload["chart"]
        if chart.get("error"):
            raise WatchlistDataError("yahoo error object")
        res = chart["result"][0]
        meta, stamps = res["meta"], res["timestamp"]
        quote_ = res["indicators"]["quote"][0]
        closes = quote_["close"]
    except WatchlistDataError:
        raise
    except Exception:
        raise WatchlistDataError("unexpected payload shape")
    volumes = quote_.get("volume")
    if not (isinstance(meta, dict) and isinstance(stamps, list) and isinstance(closes, list)):
        raise WatchlistDataError("unexpected payload types")
    if volumes is None:
        volumes = [None] * len(stamps)
    if not isinstance(volumes, list) or len(closes) != len(stamps) or len(volumes) != len(stamps):
        raise WatchlistDataError("length mismatch")
    currency = meta.get("currency")
    if not isinstance(currency, str) or not CURRENCY_RE.fullmatch(currency):
        raise WatchlistDataError("bad currency")
    gmtoffset = meta.get("gmtoffset", 0)
    if not isinstance(gmtoffset, int) or isinstance(gmtoffset, bool) or abs(gmtoffset) > 14 * 3600:
        raise WatchlistDataError("bad gmtoffset")

    rows, prev_ts = [], None
    for ts, close, vol in zip(stamps, closes, volumes):
        if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
            raise WatchlistDataError("bad timestamp")
        if prev_ts is not None and ts <= prev_ts:
            raise WatchlistDataError("timestamps not strictly increasing")
        prev_ts = ts
        if vol is not None and (not is_num(vol) or vol < 0):
            raise WatchlistDataError("bad volume")
        if close is None:
            continue
        if not is_num(close) or close <= 0:
            raise WatchlistDataError("bad close")
        rows.append((ts, float(close), None if vol is None else float(vol)))

    # 정규장 구간은 항상 필수: 없거나 깨졌으면 마지막 봉이 미완성인지 판단할 수 없으므로 payload를 거부한다(fail closed).
    start, end = _regular_session(meta)
    intraday_excluded = False
    if now_ts is not None and rows and start <= now_ts < end and rows[-1][0] >= start:
        rows = rows[:-1]
        intraday_excluded = True
    if len(rows) < MIN_POINTS:
        raise WatchlistDataError("too few points")
    return {"currency": currency, "gmtoffset": gmtoffset, "rows": rows, "intraday_excluded": intraday_excluded,
            "symbol": meta.get("symbol")}


def quote_date(norm):
    """마지막 봉의 거래소 현지 날짜(YYYY-MM-DD). 호스트 타임존에 의존하지 않는다."""
    tz = timezone(timedelta(seconds=norm["gmtoffset"]))
    return datetime.fromtimestamp(norm["rows"][-1][0], tz=tz).date().isoformat()


# ── 지표 ─────────────────────────────────────────────────────────────────
def _sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def wilder_rsi(closes, period=14):
    """Wilder RSI. 첫 평균 = 처음 `period`개 변화량의 단순평균, 이후 avg = (prev*(period-1) + 현재)/period.
    RSI = 100 - 100/(1+avgGain/avgLoss). avgLoss=0이면 100(상승만), 변화 전무면 50. 종가 period+1개 미만이면 None."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def compute_indicators(norm):
    rows = norm["rows"]
    closes = [r[1] for r in rows]
    vols = [r[2] for r in rows]
    last = closes[-1]
    volume = vols[-1] if vols[-1] is not None and vols[-1] > 0 else None
    prior = vols[-21:-1]
    volume_avg20 = None
    if len(prior) == 20 and all(v is not None for v in prior):
        avg = sum(prior) / 20
        volume_avg20 = avg if avg > 0 else None
    return {
        "close": last,
        "points": len(closes),
        "ret_1d": last / closes[-2] - 1 if len(closes) >= 2 else None,
        "ret_20d": last / closes[-21] - 1 if len(closes) >= 21 else None,
        "sma20": _sma(closes, 20),
        "sma50": _sma(closes, 50),
        "sma200": _sma(closes, 200),
        "rsi14": wilder_rsi(closes, 14),
        "volume": volume,
        "volume_avg20": volume_avg20,
        "volume_ratio": volume / volume_avg20 if volume is not None and volume_avg20 is not None else None,
    }


# ── 점수 (결정론 · 공개 규칙) ────────────────────────────────────────────
LABELS = ("우호", "관망", "주의")
BASE_SCORE = 50
W_SMA20, W_SMA50, W_SMA200, W_CROSS = 6, 8, 10, 6   # 종가 vs 이동평균 / SMA20 vs SMA50 (±)
RET20_CAP, RET20_PTS_PER_PCT = 8, 0.5               # 20일 수익률 1%p당 0.5점, ±8점 상한
RSI_OVERBOUGHT, RSI_OVERBOUGHT_PTS = 70, -8
RSI_OVERSOLD, RSI_OVERSOLD_PTS = 30, 4
VOL_RATIO_MIN, VOL_PTS = 1.5, 4                     # 거래량 ≥ 20일 평균 1.5배 & 1일 등락 방향 일치
LABEL_FAVORABLE_MIN, LABEL_CAUTION_MAX = 65, 40     # ≥65 우호 · ≤40 주의 · 그 외 관망
PARTIAL_SCORE_CAP = LABEL_FAVORABLE_MIN - 1         # 입력이 하나라도 없으면 우호 불가
REQUIRED = ("sma20", "sma50", "sma200", "ret_20d", "ret_1d", "rsi14", "volume_ratio")
MAX_CHIPS = 4


def label_for(score):
    if score >= LABEL_FAVORABLE_MIN:
        return LABELS[0]
    if score <= LABEL_CAUTION_MAX:
        return LABELS[2]
    return LABELS[1]


def formula_lines():
    return [
        f"시작 {BASE_SCORE}점에서 아래 항목을 더하고 뺍니다 (정수로 반올림, 0~100).",
        f"종가 vs 20일선(SMA20): 위 +{W_SMA20} · 아래 -{W_SMA20}",
        f"종가 vs 50일선(SMA50): 위 +{W_SMA50} · 아래 -{W_SMA50}",
        f"종가 vs 200일선(SMA200): 위 +{W_SMA200} · 아래 -{W_SMA200}",
        f"SMA20 vs SMA50: SMA20이 위 +{W_CROSS} · 아래 -{W_CROSS}",
        f"20거래일 수익률: 1%p당 {RET20_PTS_PER_PCT}점, 최대 ±{RET20_CAP}점",
        f"RSI14: {RSI_OVERBOUGHT} 이상(과열) {RSI_OVERBOUGHT_PTS} · {RSI_OVERSOLD} 이하(과매도) +{RSI_OVERSOLD_PTS}",
        f"거래량 확인: 직전 20일 평균의 {VOL_RATIO_MIN}배 이상이면 당일 상승 +{VOL_PTS} · 하락 -{VOL_PTS}",
        f"라벨: {LABEL_FAVORABLE_MIN}점 이상 우호 · {LABEL_CAUTION_MAX}점 이하 주의 · 그 사이 관망",
        f"입력이 하나라도 없으면 없는 항목은 0점(중립)으로 두고 품질을 '일부 누락'으로 표시하며, "
        f"점수는 최대 {PARTIAL_SCORE_CAP}점으로 제한해 우호가 되지 않게 합니다.",
    ]


def _num(ind, key):
    v = ind.get(key)
    return float(v) if is_num(v) else None


def _rel(close, avg, w, name):
    """종가-이동평균 관계 항목 -> (points, chip text)."""
    pct = f"{(close / avg - 1) * 100:+.1f}%"
    if close > avg:
        return w, f"{name} 위 ({pct})"
    if close < avg:
        return -w, f"{name} 아래 ({pct})"
    return 0, f"{name}과 동일"


def score_indicators(ind):
    """compute_indicators() 결과 -> {"score", "label", "quality", "missing", "reasons"}.
    사용할 수 없는 값(None/비유한/bool/문자열)은 '없음'으로 보고 0점 처리한다. 종가가 무효면 거부."""
    close = _num(ind, "close")
    if close is None or close <= 0:
        raise WatchlistDataError("invalid close")
    v = {k: _num(ind, k) for k in REQUIRED}
    missing = [k for k in REQUIRED if v[k] is None]

    cands = []  # (points, text) in fixed priority order
    for key, w, name in (("sma200", W_SMA200, "200일선"), ("sma50", W_SMA50, "50일선"), ("sma20", W_SMA20, "20일선")):
        if v[key] is not None:
            cands.append(_rel(close, v[key], w, name))
    if v["sma20"] is not None and v["sma50"] is not None:
        if v["sma20"] > v["sma50"]:
            cands.append((W_CROSS, "20일선이 50일선 위"))
        elif v["sma20"] < v["sma50"]:
            cands.append((-W_CROSS, "20일선이 50일선 아래"))
    if v["ret_20d"] is not None:
        pts = round(max(-RET20_CAP, min(RET20_CAP, v["ret_20d"] * 100 * RET20_PTS_PER_PCT)), 6)
        cands.append((pts, f"20일 수익률 {v['ret_20d'] * 100:+.1f}%"))
    if v["rsi14"] is not None:
        r = v["rsi14"]
        if r >= RSI_OVERBOUGHT:
            cands.append((RSI_OVERBOUGHT_PTS, f"RSI {r:.0f} 과열"))
        elif r <= RSI_OVERSOLD:
            cands.append((RSI_OVERSOLD_PTS, f"RSI {r:.0f} 과매도"))
        else:
            cands.append((0, f"RSI {r:.0f} 중립"))
    if v["volume_ratio"] is not None:
        ratio, r1 = v["volume_ratio"], v["ret_1d"]
        if ratio >= VOL_RATIO_MIN and r1 is not None and r1 > 0:
            cands.append((VOL_PTS, f"거래량 평균 {ratio:.1f}배 · 상승 동반"))
        elif ratio >= VOL_RATIO_MIN and r1 is not None and r1 < 0:
            cands.append((-VOL_PTS, f"거래량 평균 {ratio:.1f}배 · 하락 동반"))
        else:
            cands.append((0, f"거래량 평균 {ratio:.1f}배"))

    raw = BASE_SCORE + sum(p for p, _ in cands)
    score = max(0, min(100, int(math.floor(raw + 0.5))))
    quality = "partial" if missing else "ok"
    if missing:
        score = min(score, PARTIAL_SCORE_CAP)

    ranked = sorted(range(len(cands)), key=lambda i: (-abs(cands[i][0]), i))
    chips = []
    if missing:
        chips.append({"text": "일부 지표 누락 · 중립 처리", "tone": "neg"})
    for i in ranked[: MAX_CHIPS - len(chips)]:
        p, text = cands[i]
        chips.append({"text": text, "tone": "pos" if p > 0 else ("neg" if p < 0 else "neu")})
    if len(chips) < 2:
        chips.append({"text": "근거 지표 부족 · 중립 처리", "tone": "neu"})
    return {"score": score, "label": label_for(score), "quality": quality, "missing": missing, "reasons": chips}


# ── 종목 객체 · 종목별 독립 갱신 ─────────────────────────────────────────
DEFAULT_WATCHLIST = (("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"), ("AAPL", "Apple"))
SOURCE_NAME = "Yahoo Finance chart"
FORMULA_VERSION = 1

# 공개 데이터/HTML에는 아래 고정 사유 코드만 기록한다 (예외 메시지·URL·응답 본문은 절대 복사하지 않음).
SAFE_ERRORS = {
    "fetch_failed": "데이터 조회 실패",
    "invalid_payload": "데이터 형식 오류",
    "no_data": "데이터 없음",
}
NUM_FIELDS = ("close", "ret_1d", "ret_20d", "sma20", "sma50", "sma200", "rsi14", "volume_ratio")
STOCK_FIELDS = ("symbol", "name", "status", "error", "src_date", "currency", "points", "quality",
                "score", "label", "reasons") + NUM_FIELDS
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ROUND = {"close": 4, "ret_1d": 6, "ret_20d": 6, "sma20": 4, "sma50": 4, "sma200": 4, "rsi14": 2, "volume_ratio": 3}


def unavailable_stock(symbol, name, error="no_data"):
    s = {k: None for k in STOCK_FIELDS}
    s.update(symbol=symbol, name=name, status="unavailable", error=error, reasons=[])
    return s


def build_stock(symbol, name, payload, now_ts=None):
    norm = normalize_chart(payload, now_ts)
    got = norm.get("symbol")
    if not isinstance(got, str) or got.upper() != symbol.upper():
        raise WatchlistDataError("symbol mismatch")
    ind = compute_indicators(norm)
    sc = score_indicators(ind)
    s = unavailable_stock(symbol, name, error=None)
    s.update(status="ok", src_date=quote_date(norm), currency=norm["currency"], points=ind["points"],
             quality=sc["quality"], score=sc["score"], label=sc["label"], reasons=sc["reasons"])
    for k in NUM_FIELDS:
        v = ind[k]
        s[k] = round(v, _ROUND[k]) if v is not None else None
    return s


def _valid_date(text):
    if not isinstance(text, str) or not _DATE_RE.fullmatch(text):
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _sanitize_prior(prior, symbol, name):
    """이전 종목 객체를 화이트리스트로만 복원. 신뢰할 수 없으면 None (조작·손상된 값을 되살리지 않는다)."""
    if not isinstance(prior, dict) or prior.get("symbol") != symbol:
        return None
    if prior.get("status") not in ("ok", "stale") or not _valid_date(prior.get("src_date")):
        return None
    score = prior.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        return None
    if prior.get("label") not in LABELS or prior.get("quality") not in ("ok", "partial"):
        return None
    cur = prior.get("currency")
    pts = prior.get("points")
    if not isinstance(cur, str) or not CURRENCY_RE.fullmatch(cur):
        return None
    if not isinstance(pts, int) or isinstance(pts, bool) or pts < 0:
        return None
    clean = unavailable_stock(symbol, name, error=None)
    clean.update(status=prior["status"], src_date=prior["src_date"], currency=cur, points=pts,
                 quality=prior["quality"], score=score, label=prior["label"])
    for k in NUM_FIELDS:
        v = prior.get(k)
        if v is not None and not is_num(v):
            return None
        clean[k] = v
    if clean["close"] is None or clean["close"] <= 0:
        return None
    reasons = prior.get("reasons")
    if not isinstance(reasons, list) or len(reasons) > MAX_CHIPS:
        return None
    out = []
    for c in reasons:
        if not (isinstance(c, dict) and isinstance(c.get("text"), str) and len(c["text"]) <= 80
                and c.get("tone") in ("pos", "neg", "neu")):
            return None
        out.append({"text": c["text"], "tone": c["tone"]})
    clean["reasons"] = out
    return clean


def refresh_stock(symbol, name, fetcher, prior, now_ts=None, log=None):
    """한 종목을 독립적으로 갱신. 어떤 예외도 밖으로 내보내지 않는다.
    실패 시: 신뢰할 수 있는 이전 객체는 stale로 표시해 그대로 보존(src_date 유지), 없으면 unavailable."""
    now_ts = int(time.time()) if now_ts is None else now_ts
    code = None
    try:
        body = fetcher(build_chart_url(symbol))
    except Exception:
        code = "fetch_failed"
    else:
        try:
            return build_stock(symbol, name, json.loads(body), now_ts)
        except Exception:
            code = "invalid_payload"
    if log:
        log(f"[watchlist] {symbol}: {code}")
    kept = _sanitize_prior(prior, symbol, name)
    if kept is None:
        return unavailable_stock(symbol, name, error=code)
    kept.update(status="stale", error=code)
    return kept


def refresh_watchlist(fetcher, prior_watchlist, now_ts=None, log=None):
    prior_items = {}
    if isinstance(prior_watchlist, dict) and isinstance(prior_watchlist.get("items"), list):
        for it in prior_watchlist["items"]:
            if isinstance(it, dict) and isinstance(it.get("symbol"), str):
                prior_items[it["symbol"]] = it
    items = [refresh_stock(sym, name, fetcher, prior_items.get(sym), now_ts, log) for sym, name in DEFAULT_WATCHLIST]
    return {"source": SOURCE_NAME, "formula": FORMULA_VERSION, "items": items}


# ── 신선도 ───────────────────────────────────────────────────────────────
FRESH_MAX_DAYS = 4  # 주말·연휴를 감안해 기준일이 데이터 기준일(asOf)에서 4일 이내면 '최신'
RSI_FORMULA = ("RSI14 (Wilder): 처음 14개 종가 변화량의 단순평균을 시작값으로, 이후 "
               "평균 = (직전 평균 × 13 + 오늘 값) ÷ 14 로 상승폭·하락폭을 각각 평활. "
               "RSI = 100 − 100 ÷ (1 + 평균상승 ÷ 평균하락). 하락이 전혀 없으면 100, 변화가 전혀 없으면 50.")


def _parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date() if isinstance(text, str) and _DATE_RE.fullmatch(text) else None
    except ValueError:
        return None


def freshness(item, as_of):
    """-> (state, age_days). state: fresh | delayed | stale(갱신 실패) | unavailable | unknown."""
    status = item.get("status") if isinstance(item, dict) else None
    if status == "unavailable":
        return "unavailable", None
    if status not in ("ok", "stale"):
        return "unknown", None
    src, ref = _parse_date(item.get("src_date")), _parse_date(as_of)
    if src is None or ref is None:
        return "unknown", None
    age = (ref - src).days
    if age < 0:
        return "unknown", None
    if status == "stale":
        return "stale", age
    return ("fresh" if age <= FRESH_MAX_DAYS else "delayed"), age
