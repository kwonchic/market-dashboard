#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the deterministic daily watchlist report (stdlib unittest only)."""
import copy, json, math, os, sys, unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import watchlist as wl  # noqa: E402


def load_fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


WILDER_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
                 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]


def ramp_payload(n=60, volumes=None, gmtoffset=32400, first_ts=1_760_000_000):
    """closes = 1..n, one bar per day: every indicator is hand-checkable."""
    closes = [float(i) for i in range(1, n + 1)]
    vols = volumes if volumes is not None else [100] * n
    stamps = [first_ts + i * 86400 for i in range(n)]
    regular = {"start": stamps[-1], "end": stamps[-1] + 23400}  # valid regular-session metadata (last bar's session)
    return {"chart": {"result": [{
        "meta": {"currency": "KRW", "symbol": "T.KS", "gmtoffset": gmtoffset,
                 "currentTradingPeriod": {"regular": regular}},
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes, "volume": vols}]},
    }], "error": None}}


# ── Slice 1: URL safety, payload normalization, indicators ──────────────────
class ChartUrlTests(unittest.TestCase):
    def test_default_symbols_build_exact_https_yahoo_url(self):
        url = wl.build_chart_url("005930.KS")
        self.assertEqual(
            url, "https://query1.finance.yahoo.com/v8/finance/chart/005930.KS?range=1y&interval=1d")

    def test_symbol_is_url_encoded_and_cannot_change_host_or_path(self):
        for bad in ("AAPL/../../x", "AAPL@evil.com", "AAPL?x=1", "AAPL#f", "", "A" * 40,
                    "http://evil.com", "AAPL EVIL", "AAPL\n", None, 123):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wl.build_chart_url(bad)

    def test_caret_symbol_is_percent_encoded(self):
        url = wl.build_chart_url("^GSPC")
        self.assertIn("/chart/%5EGSPC?", url)

    def test_validate_url_rejects_other_scheme_or_host(self):
        for bad in ("http://query1.finance.yahoo.com/v8/finance/chart/AAPL",
                    "https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
                    "https://query1.finance.yahoo.com.evil.com/v8/finance/chart/AAPL",
                    "https://user@query1.finance.yahoo.com/v8/finance/chart/AAPL",
                    "https://query1.finance.yahoo.com:8443/v8/finance/chart/AAPL"):
            with self.assertRaises(ValueError, msg=bad):
                wl.validate_yahoo_url(bad)


class NormalizeChartTests(unittest.TestCase):
    def test_valid_fixture_is_normalized_and_null_closes_dropped(self):
        norm = wl.normalize_chart(load_fixture("chart_005930_ks_downtrend.json"))
        self.assertEqual(norm["currency"], "KRW")
        self.assertEqual(len(norm["rows"]), 250)  # 251 bars, 1 null close dropped
        ts = [r[0] for r in norm["rows"]]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(math.isfinite(r[1]) and r[1] > 0 for r in norm["rows"]))

    def test_quote_date_uses_exchange_offset_not_host_timezone(self):
        # 2026-11-13 00:00 UTC == 09:00 KST: the Seoul trading date is 11-13
        ks = wl.normalize_chart(load_fixture("chart_005930_ks_downtrend.json"))
        self.assertEqual(wl.quote_date(ks), "2026-11-13")
        # 2026-11-13 14:30 UTC == 09:30 EST: New York trading date is 11-13
        us = wl.normalize_chart(load_fixture("chart_aapl_uptrend.json"))
        self.assertEqual(wl.quote_date(us), "2026-11-13")

    def test_quote_date_rolls_over_with_positive_offset(self):
        # 2026-11-12 23:30 UTC is already 2026-11-13 08:30 in KST
        p = ramp_payload(n=60, first_ts=1_794_526_200 - 59 * 86400)  # last bar = 2026-11-12T23:30Z
        norm = wl.normalize_chart(p)
        self.assertEqual(wl.quote_date(norm), "2026-11-13")

    def test_too_short_payload_rejected(self):
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(load_fixture("chart_short.json"))

    def test_malformed_fixture_rejected(self):
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(load_fixture("chart_malformed.json"))

    def test_yahoo_error_object_rejected(self):
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(load_fixture("chart_error.json"))

    def test_garbage_shapes_rejected(self):
        for bad in (None, [], "x", {}, {"chart": None}, {"chart": {"result": []}},
                    {"chart": {"result": [None]}}, {"chart": {"result": [{}]}}):
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(bad)

    def test_non_finite_or_non_positive_or_bool_closes_rejected(self):
        for poison in (float("nan"), float("inf"), -float("inf"), 0.0, -3.0, True, "12"):
            p = ramp_payload()
            p["chart"]["result"][0]["indicators"]["quote"][0]["close"][30] = poison
            with self.assertRaises(wl.WatchlistDataError, msg=repr(poison)):
                wl.normalize_chart(p)

    def test_non_finite_volume_rejected_but_null_volume_allowed(self):
        p = ramp_payload()
        p["chart"]["result"][0]["indicators"]["quote"][0]["volume"][10] = float("inf")
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(p)
        p = ramp_payload()
        p["chart"]["result"][0]["indicators"]["quote"][0]["volume"][10] = None
        norm = wl.normalize_chart(p)
        self.assertIsNone(norm["rows"][10][2])

    def test_unsorted_or_duplicate_timestamps_rejected(self):
        p = ramp_payload()
        p["chart"]["result"][0]["timestamp"][20] = p["chart"]["result"][0]["timestamp"][19]
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(p)

    def test_length_mismatch_rejected(self):
        p = ramp_payload()
        p["chart"]["result"][0]["indicators"]["quote"][0]["close"].pop()
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(p)

    def test_currency_must_be_three_letters(self):
        for bad in (None, "", "krw", "KRWW", "K\"R", 5):
            p = ramp_payload()
            p["chart"]["result"][0]["meta"]["currency"] = bad
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(p)

    def test_in_progress_session_bar_is_excluded(self):
        p = ramp_payload(n=60)
        res = p["chart"]["result"][0]
        last = res["timestamp"][-1]
        res["meta"]["currentTradingPeriod"] = {"regular": {"start": last - 60, "end": last + 6 * 3600}}
        norm = wl.normalize_chart(p, now_ts=last + 3600)  # session still open
        self.assertEqual(len(norm["rows"]), 59)
        self.assertTrue(norm["intraday_excluded"])
        norm = wl.normalize_chart(p, now_ts=last + 7 * 3600)  # session over
        self.assertEqual(len(norm["rows"]), 60)
        self.assertFalse(norm["intraday_excluded"])


class RegularSessionMetadataTests(unittest.TestCase):
    """Fail closed: without trustworthy regular-session bounds an unfinished daily bar can't be ruled out."""

    def _payload(self, regular):
        p = ramp_payload(n=60)
        p["chart"]["result"][0]["meta"]["currentTradingPeriod"] = {"regular": regular}
        return p

    def test_missing_current_trading_period_rejected(self):
        for now in (None, 1_800_000_000):
            p = ramp_payload(n=60)
            del p["chart"]["result"][0]["meta"]["currentTradingPeriod"]
            with self.assertRaises(wl.WatchlistDataError, msg=repr(now)):
                wl.normalize_chart(p, now_ts=now)

    def test_missing_regular_key_rejected(self):
        p = ramp_payload(n=60)
        p["chart"]["result"][0]["meta"]["currentTradingPeriod"] = {"pre": {"start": 1, "end": 2}}
        with self.assertRaises(wl.WatchlistDataError):
            wl.normalize_chart(p, now_ts=1_800_000_000)

    def test_current_trading_period_not_a_dict_rejected(self):
        for bad in (None, [], "regular", 5):
            p = ramp_payload(n=60)
            p["chart"]["result"][0]["meta"]["currentTradingPeriod"] = bad
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(p, now_ts=1_800_000_000)

    def test_regular_not_a_dict_rejected(self):
        for bad in (None, [], [1, 2], "x", 5, True):
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(self._payload(bad), now_ts=1_800_000_000)

    def test_missing_start_or_end_rejected(self):
        for bad in ({"end": 2_000_000_000}, {"start": 1_700_000_000}, {}):
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(self._payload(bad), now_ts=1_800_000_000)

    def test_non_int_bounds_rejected(self):
        good = 1_700_000_000
        cases = [
            {"start": 1_700_000_000.0, "end": good + 3600},   # float
            {"start": good, "end": good + 3600.5},
            {"start": True, "end": good + 3600},               # bool is not a timestamp
            {"start": good, "end": True},
            {"start": str(good), "end": good + 3600},          # string
            {"start": good, "end": None},
            {"start": float("nan"), "end": good + 3600},
        ]
        for bad in cases:
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(self._payload(bad), now_ts=1_800_000_000)

    def test_non_positive_bounds_rejected(self):
        for bad in ({"start": 0, "end": 3600}, {"start": -10, "end": 3600}, {"start": -20, "end": -10}):
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(self._payload(bad), now_ts=1_800_000_000)

    def test_reversed_or_empty_window_rejected(self):
        for bad in ({"start": 1_700_003_600, "end": 1_700_000_000}, {"start": 1_700_000_000, "end": 1_700_000_000}):
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.normalize_chart(self._payload(bad), now_ts=1_800_000_000)

    def test_active_session_excludes_last_bar_boundaries(self):
        p = ramp_payload(n=60)
        last = p["chart"]["result"][0]["timestamp"][-1]
        p = self._payload({"start": last, "end": last + 3600})
        for now in (last, last + 1, last + 3599):  # [start, end) is active
            norm = wl.normalize_chart(p, now_ts=now)
            self.assertEqual(len(norm["rows"]), 59, msg=now)
            self.assertTrue(norm["intraday_excluded"], msg=now)

    def test_closed_session_retains_last_bar_boundaries(self):
        p = ramp_payload(n=60)
        last = p["chart"]["result"][0]["timestamp"][-1]
        p = self._payload({"start": last, "end": last + 3600})
        for now in (last + 3600, last + 7200):  # end is exclusive -> closed
            norm = wl.normalize_chart(p, now_ts=now)
            self.assertEqual(len(norm["rows"]), 60, msg=now)
            self.assertFalse(norm["intraday_excluded"], msg=now)

    def test_active_session_keeps_bar_that_belongs_to_earlier_session(self):
        p = ramp_payload(n=60)
        last = p["chart"]["result"][0]["timestamp"][-1]
        p = self._payload({"start": last + 86400, "end": last + 86400 + 3600})
        norm = wl.normalize_chart(p, now_ts=last + 86400 + 60)
        self.assertEqual(len(norm["rows"]), 60)
        self.assertFalse(norm["intraday_excluded"])

    def test_synthetic_fixtures_carry_valid_regular_session(self):
        for name in ("chart_aapl_uptrend.json", "chart_005930_ks_downtrend.json"):
            reg = load_fixture(name)["chart"]["result"][0]["meta"]["currentTradingPeriod"]["regular"]
            self.assertIsInstance(reg["start"], int)
            self.assertIsInstance(reg["end"], int)
            self.assertLess(reg["start"], reg["end"])


class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.ind = wl.compute_indicators(wl.normalize_chart(ramp_payload(n=60)))

    def test_latest_close_and_points(self):
        self.assertEqual(self.ind["close"], 60.0)
        self.assertEqual(self.ind["points"], 60)

    def test_returns(self):
        self.assertAlmostEqual(self.ind["ret_1d"], 60 / 59 - 1, places=9)
        self.assertAlmostEqual(self.ind["ret_20d"], 60 / 40 - 1, places=9)  # 21st-from-last close = 40

    def test_simple_moving_averages(self):
        self.assertAlmostEqual(self.ind["sma20"], 50.5, places=9)   # mean(41..60)
        self.assertAlmostEqual(self.ind["sma50"], 35.5, places=9)   # mean(11..60)
        self.assertIsNone(self.ind["sma200"])                       # only 60 points

    def test_sma200_with_enough_history(self):
        ind = wl.compute_indicators(wl.normalize_chart(ramp_payload(n=220)))
        self.assertAlmostEqual(ind["sma200"], sum(range(21, 221)) / 200, places=9)

    def test_rsi_all_gains_is_100_and_flat_is_50(self):
        self.assertEqual(self.ind["rsi14"], 100.0)
        flat = ramp_payload(n=60)
        flat["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [10.0] * 60
        self.assertEqual(wl.compute_indicators(wl.normalize_chart(flat))["rsi14"], 50.0)

    def test_rsi_seed_matches_hand_computed_wilder_value(self):
        # Wilder's textbook closes. 14 changes: gains sum 3.34, losses sum 1.40.
        # Seed = simple mean of the first 14 changes -> RS = 3.34/1.40 -> RSI = 70.46
        # (StockCharts shows 70.53 because its sheet rounds the averages first.)
        self.assertEqual(round(wl.wilder_rsi(WILDER_CLOSES, 14), 2), 70.46)

    def test_rsi_wilder_smoothing_step_after_seed(self):
        seed_gain, seed_loss = 3.34 / 14, 1.40 / 14
        # next close 46.03 (change -0.25): Wilder smoothing avg = (prev * 13 + current) / 14
        gain, loss = seed_gain * 13 / 14, (seed_loss * 13 + 0.25) / 14
        expected = 100 - 100 / (1 + gain / loss)
        self.assertAlmostEqual(wl.wilder_rsi(WILDER_CLOSES + [46.03], 14), expected, places=9)

    def test_rsi_needs_15_closes(self):
        self.assertIsNone(wl.wilder_rsi([1.0] * 14, 14))

    def test_volume_ratio_uses_prior_20_sessions(self):
        vols = [100] * 59 + [250]
        ind = wl.compute_indicators(wl.normalize_chart(ramp_payload(n=60, volumes=vols)))
        self.assertEqual(ind["volume"], 250)
        self.assertEqual(ind["volume_avg20"], 100.0)
        self.assertAlmostEqual(ind["volume_ratio"], 2.5, places=9)

    def test_volume_ratio_missing_when_volume_absent_or_zero(self):
        ind = wl.compute_indicators(wl.normalize_chart(ramp_payload(n=60, volumes=[None] * 60)))
        self.assertIsNone(ind["volume_ratio"])
        ind = wl.compute_indicators(wl.normalize_chart(ramp_payload(n=60, volumes=[0] * 60)))
        self.assertIsNone(ind["volume_ratio"])

    def test_fixture_end_to_end_values(self):
        norm = wl.normalize_chart(load_fixture("chart_aapl_uptrend.json"))
        ind = wl.compute_indicators(norm)
        closes = [r[1] for r in norm["rows"]]
        self.assertEqual(ind["points"], 251)
        self.assertEqual(ind["close"], closes[-1])
        self.assertAlmostEqual(ind["sma200"], sum(closes[-200:]) / 200, places=9)
        self.assertGreater(ind["volume_ratio"], 1.5)
        self.assertTrue(0 <= ind["rsi14"] <= 100)
        self.assertTrue(all(math.isfinite(ind[k]) for k in
                            ("close", "ret_1d", "ret_20d", "sma20", "sma50", "sma200", "rsi14", "volume_ratio")))


# ── Slice 2: score, labels, missing/non-finite inputs ───────────────────────
def full_ind(**kw):
    """Neutral-but-complete indicator set: every rule contributes 0 points."""
    base = {"close": 100.0, "points": 250, "ret_1d": 0.0, "ret_20d": 0.0,
            "sma20": 100.0, "sma50": 100.0, "sma200": 100.0, "rsi14": 50.0,
            "volume": 100.0, "volume_avg20": 100.0, "volume_ratio": 1.0}
    base.update(kw)
    return base


def only(**kw):
    """Only the given inputs exist (everything else missing)."""
    base = {k: None for k in ("ret_1d", "ret_20d", "sma20", "sma50", "sma200", "rsi14",
                              "volume", "volume_avg20", "volume_ratio")}
    base.update({"close": 100.0, "points": 250})
    base.update(kw)
    return base


class ScoreRuleTests(unittest.TestCase):
    def test_labels_constant(self):
        self.assertEqual(wl.LABELS, ("우호", "관망", "주의"))

    def test_neutral_complete_input_scores_50_and_wait(self):
        r = wl.score_indicators(full_ind())
        self.assertEqual((r["score"], r["label"], r["quality"]), (50, "관망", "ok"))
        self.assertIsInstance(r["score"], int)

    def test_each_rule_moves_score_by_documented_points(self):
        # close = 100; only the given input exists, so exactly one rule fires
        cases = [
            (dict(sma20=90.0), +6), (dict(sma20=110.0), -6),
            (dict(sma50=90.0), +8), (dict(sma50=110.0), -8),
            (dict(sma200=90.0), +10), (dict(sma200=110.0), -10),
        ]
        for kw, delta in cases:
            r = wl.score_indicators(only(**kw))
            self.assertEqual(r["score"], 50 + delta, msg=str(kw))

    def test_sma20_vs_sma50_cross_rule(self):
        # close equals SMA20 (0 pts) so only the SMA50 rule and the cross rule fire
        r = wl.score_indicators(only(close=100.0, sma20=100.0, sma50=90.0))
        self.assertEqual(r["score"], 50 + 8 + 6)   # above SMA50 (+8), SMA20 > SMA50 (+6)
        r = wl.score_indicators(only(close=100.0, sma20=100.0, sma50=110.0))
        self.assertEqual(r["score"], 50 - 8 - 6)   # below SMA50 (-8), SMA20 < SMA50 (-6)
        # SMA20 == SMA50 -> no cross points
        self.assertEqual(wl.score_indicators(only(sma20=100.0, sma50=100.0))["score"], 50)

    def test_price_equal_to_average_adds_nothing(self):
        self.assertEqual(wl.score_indicators(only(sma200=100.0))["score"], 50)

    def test_return_20d_is_bounded(self):
        for ret, delta in ((0.0, 0), (0.10, 5), (0.16, 8), (0.50, 8), (-0.10, -5), (-0.16, -8), (-0.90, -8)):
            self.assertEqual(wl.score_indicators(only(ret_20d=ret))["score"], 50 + delta, msg=str(ret))

    def test_rsi_overbought_and_oversold_boundaries(self):
        for rsi, delta in ((69.99, 0), (70.0, -8), (95.0, -8), (30.01, 0), (30.0, 4), (5.0, 4), (50.0, 0)):
            self.assertEqual(wl.score_indicators(only(rsi14=rsi))["score"], 50 + delta, msg=str(rsi))

    def test_volume_confirmation_needs_direction(self):
        cases = [(1.5, 0.01, +4), (1.49, 0.01, 0), (3.0, -0.01, -4), (3.0, 0.0, 0), (3.0, None, 0), (0.5, 0.02, 0)]
        for ratio, r1, delta in cases:
            r = wl.score_indicators(only(volume_ratio=ratio, ret_1d=r1))
            self.assertEqual(r["score"], 50 + delta, msg=str((ratio, r1)))

    def test_half_points_round_half_up_and_score_is_clamped(self):
        self.assertEqual(wl.score_indicators(only(ret_20d=0.03))["score"], 52)   # +1.5 -> 51.5 -> 52
        worst = full_ind(sma20=200.0, sma50=300.0, sma200=400.0, ret_20d=-0.9, rsi14=90.0,
                         volume_ratio=3.0, ret_1d=-0.05)
        self.assertEqual(wl.score_indicators(worst)["score"], 0)  # 50-6-8-10-6-8-8-4 = 0 -> clamp lower bound
        best = full_ind(sma20=50.0, sma50=40.0, sma200=30.0, ret_20d=0.9, rsi14=20.0,
                        volume_ratio=3.0, ret_1d=0.05)
        self.assertEqual(wl.score_indicators(best)["score"], 50 + 6 + 8 + 10 + 6 + 8 + 4 + 4)  # 96

    def test_strong_uptrend_hand_computed(self):
        ind = full_ind(close=120.0, sma20=110.0, sma50=100.0, sma200=90.0, ret_20d=0.10, rsi14=60.0)
        r = wl.score_indicators(ind)
        self.assertEqual(r["score"], 50 + 6 + 8 + 10 + 6 + 5)  # 85
        self.assertEqual(r["label"], "우호")

    def test_downtrend_hand_computed(self):
        ind = full_ind(close=80.0, sma20=90.0, sma50=100.0, sma200=110.0, ret_20d=-0.10, rsi14=45.0)
        r = wl.score_indicators(ind)
        self.assertEqual(r["score"], 50 - 6 - 8 - 10 - 6 - 5)  # 15
        self.assertEqual(r["label"], "주의")

    def test_formula_lines_disclose_every_weight(self):
        text = " ".join(wl.formula_lines())
        for token in ("50", "+6", "-6", "+8", "-8", "+10", "-10", "±8", "70", "30", "+4", "1.5", "65", "40", "64"):
            self.assertIn(token, text)


class LabelBoundaryTests(unittest.TestCase):
    def test_label_thresholds(self):
        for score, label in ((100, "우호"), (65, "우호"), (64, "관망"), (50, "관망"), (41, "관망"),
                             (40, "주의"), (0, "주의")):
            self.assertEqual(wl.label_for(score), label, msg=str(score))


class MissingAndInvalidInputTests(unittest.TestCase):
    def test_all_optional_inputs_missing_is_neutral_partial(self):
        r = wl.score_indicators(only())
        self.assertEqual((r["score"], r["label"], r["quality"]), (50, "관망", "partial"))
        self.assertGreaterEqual(len(r["reasons"]), 2)
        self.assertTrue(any("누락" in c["text"] or "부족" in c["text"] for c in r["reasons"]))

    def test_missing_input_can_never_make_label_favorable(self):
        bullish_but_no_sma200 = full_ind(close=120.0, sma20=110.0, sma50=100.0, sma200=None,
                                         ret_20d=0.10, rsi14=60.0)  # raw 75
        r = wl.score_indicators(bullish_but_no_sma200)
        self.assertEqual(r["quality"], "partial")
        self.assertEqual(r["score"], 64)
        self.assertEqual(r["label"], "관망")
        self.assertIn("sma200", r["missing"])

    def test_missing_volume_marks_partial(self):
        r = wl.score_indicators(full_ind(volume_ratio=None))
        self.assertEqual((r["quality"], r["missing"]), ("partial", ["volume_ratio"]))

    def test_missing_input_does_not_hide_a_known_warning(self):
        r = wl.score_indicators(full_ind(close=80.0, sma20=90.0, sma50=100.0, sma200=None, ret_20d=-0.10))
        self.assertEqual(r["label"], "주의")

    def test_non_finite_and_bool_values_are_treated_as_missing(self):
        for poison in (float("nan"), float("inf"), -float("inf"), True, "90", [1]):
            r = wl.score_indicators(full_ind(sma20=poison))
            self.assertEqual(r["score"], 50, msg=repr(poison))
            self.assertIn("sma20", r["missing"], msg=repr(poison))
            self.assertEqual(r["quality"], "partial")

    def test_invalid_close_is_rejected(self):
        for bad in (None, float("nan"), float("inf"), 0.0, -1.0, True):
            with self.assertRaises(wl.WatchlistDataError, msg=repr(bad)):
                wl.score_indicators(full_ind(close=bad))


class ReasonChipTests(unittest.TestCase):
    def test_chips_are_2_to_4_and_come_from_real_conditions(self):
        up = wl.score_indicators(full_ind(close=120.0, sma20=110.0, sma50=100.0, sma200=90.0,
                                          ret_20d=0.10, rsi14=60.0, ret_1d=0.01, volume_ratio=1.8))
        texts = [c["text"] for c in up["reasons"]]
        self.assertTrue(2 <= len(texts) <= 4, texts)
        self.assertTrue(any("200일선 위" in t for t in texts), texts)
        self.assertTrue(all(c["tone"] in ("pos", "neg", "neu") for c in up["reasons"]))
        down = wl.score_indicators(full_ind(close=80.0, sma20=90.0, sma50=100.0, sma200=110.0,
                                            ret_20d=-0.10, rsi14=45.0))
        dtexts = [c["text"] for c in down["reasons"]]
        self.assertTrue(any("200일선 아래" in t for t in dtexts), dtexts)
        self.assertFalse(any("200일선 위" in t for t in dtexts))

    def test_chips_prioritize_largest_adjustments_and_report_actual_numbers(self):
        r = wl.score_indicators(full_ind(close=120.0, sma20=110.0, sma50=100.0, sma200=90.0,
                                         ret_20d=0.10, rsi14=75.0))
        texts = [c["text"] for c in r["reasons"]]
        self.assertEqual(len(texts), 4)
        self.assertTrue(any("RSI" in t and "75" in t for t in texts), texts)   # -8 overbought is a top item
        self.assertTrue(any("200일선" in t for t in texts), texts)             # 10 pts
        self.assertTrue(any("+33.3%" in t for t in texts), texts)              # 120 vs 90

    def test_partial_data_adds_missing_chip(self):
        r = wl.score_indicators(full_ind(sma200=None))
        self.assertIn("누락", r["reasons"][0]["text"])

    def test_chips_never_contain_advice_or_ai_wording(self):
        for ind in (full_ind(), only(), full_ind(close=120.0, sma20=110.0, sma50=100.0, sma200=90.0, rsi14=80.0)):
            for c in wl.score_indicators(ind)["reasons"]:
                for banned in ("AI", "매수", "매도", "추천", "목표가"):
                    self.assertNotIn(banned, c["text"])

    def test_deterministic(self):
        ind = full_ind(close=120.0, sma20=110.0, sma50=100.0, sma200=90.0, ret_20d=0.07, rsi14=66.0)
        self.assertEqual(wl.score_indicators(ind), wl.score_indicators(copy.deepcopy(ind)))


# ── Slice 3: stock objects, per-symbol failure isolation ────────────────────
import re as _re  # noqa: E402
import fetch_data  # noqa: E402

NOW_TS = 1_794_528_000  # 2026-11-13T00:00Z + 24h -> after both sessions closed


def with_symbol(payload, symbol):
    p = copy.deepcopy(payload)
    p["chart"]["result"][0]["meta"]["symbol"] = symbol
    return p


def fixture_for(symbol):
    if symbol == "AAPL":
        return load_fixture("chart_aapl_uptrend.json")
    if symbol == "SKHY":
        # ~90 real bars on NASDAQ; SMA200 must be None (partial) — do not fabricate
        p = ramp_payload(n=90, gmtoffset=-18000)
        p["chart"]["result"][0]["meta"]["symbol"] = "SKHY"
        p["chart"]["result"][0]["meta"]["currency"] = "USD"
        return p
    if symbol.endswith(".KS") or symbol.endswith(".KQ"):
        return with_symbol(load_fixture("chart_005930_ks_downtrend.json"), symbol)
    return with_symbol(load_fixture("chart_aapl_uptrend.json"), symbol)


def make_fetcher(outcomes, seen=None):
    """outcomes: symbol -> payload dict | str body | Exception instance. Transport stand-in only."""
    def fetch(url):
        if seen is not None:
            seen.append(url)
        sym = _re.search(r"/chart/([^?]+)\?", url).group(1)
        out = outcomes[sym]
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)
    return fetch


ALL_OK = {sym: fixture_for(sym) for sym, _ in (
    ("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"), ("AAPL", "Apple"),
    ("FLNC", "플루언스 에너지"), ("CPER", "CPER"), ("CRCL", "써클 인터넷 그룹"),
    ("SKHY", "SK하이닉스 ADR"), ("NTRA", "나테라"), ("BMNR", "비트마인 이머션 테크놀로지스"),
    ("NEE", "넥스트에라 에너지"), ("NU", "누 홀딩스"), ("ALAB", "아스테라 랩스"),
    ("VRT", "버티브 홀딩스"), ("NVTS", "나비타스 세미컨덕터"), ("QBTS", "디웨이브 퀀텀"),
    ("SMR", "뉴스케일파워"), ("COHR", "코히런트"), ("CRDO", "크레도 테크놀로지 그룹 홀딩"),
)}


class DefaultsTests(unittest.TestCase):
    def test_default_watchlist_has_exactly_18_symbols_in_exact_order(self):
        expected = [
            ("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"), ("AAPL", "Apple"),
            ("FLNC", "플루언스 에너지"), ("CPER", "CPER"), ("CRCL", "써클 인터넷 그룹"),
            ("SKHY", "SK하이닉스 ADR"), ("NTRA", "나테라"), ("BMNR", "비트마인 이머션 테크놀로지스"),
            ("NEE", "넥스트에라 에너지"), ("NU", "누 홀딩스"), ("ALAB", "아스테라 랩스"),
            ("VRT", "버티브 홀딩스"), ("NVTS", "나비타스 세미컨덕터"), ("QBTS", "디웨이브 퀀텀"),
            ("SMR", "뉴스케일파워"), ("COHR", "코히런트"), ("CRDO", "크레도 테크놀로지 그룹 홀딩"),
        ]
        self.assertEqual(len(wl.DEFAULT_WATCHLIST), 18)
        self.assertEqual(list(wl.DEFAULT_WATCHLIST), expected)


class BuildStockTests(unittest.TestCase):
    def test_build_stock_from_fixture(self):
        s = wl.build_stock("AAPL", "Apple", load_fixture("chart_aapl_uptrend.json"))
        self.assertEqual((s["symbol"], s["name"], s["status"], s["error"]), ("AAPL", "Apple", "ok", None))
        self.assertEqual((s["currency"], s["src_date"], s["points"], s["quality"]), ("USD", "2026-11-13", 251, "ok"))
        self.assertIsInstance(s["score"], int)
        self.assertIn(s["label"], wl.LABELS)
        self.assertTrue(2 <= len(s["reasons"]) <= 4)
        for k in ("close", "ret_1d", "ret_20d", "sma20", "sma50", "sma200", "rsi14", "volume_ratio"):
            self.assertTrue(math.isfinite(s[k]), k)

    def test_symbol_mismatch_in_payload_is_rejected(self):
        with self.assertRaises(wl.WatchlistDataError):
            wl.build_stock("000660.KS", "SK하이닉스", load_fixture("chart_aapl_uptrend.json"))

    def test_unavailable_stock_is_explicit_neutral_without_numbers(self):
        s = wl.unavailable_stock("AAPL", "Apple")
        self.assertEqual((s["status"], s["error"], s["score"], s["label"], s["src_date"]),
                         ("unavailable", "no_data", None, None, None))
        self.assertEqual(s["reasons"], [])
        for k in ("close", "ret_1d", "ret_20d", "sma20", "sma50", "sma200", "rsi14", "volume_ratio", "points"):
            self.assertIsNone(s[k], k)


class RefreshStockTests(unittest.TestCase):
    def setUp(self):
        self.prior = wl.build_stock("AAPL", "Apple", load_fixture("chart_aapl_uptrend.json"))

    def test_success_replaces_prior_and_clears_error(self):
        stale = dict(self.prior, status="stale", error="fetch_failed", src_date="2026-11-01", close=1.0)
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher(ALL_OK), stale, NOW_TS)
        self.assertEqual((s["status"], s["error"], s["src_date"]), ("ok", None, "2026-11-13"))
        self.assertNotEqual(s["close"], 1.0)

    def test_requests_only_the_validated_chart_url(self):
        seen = []
        wl.refresh_stock("AAPL", "Apple", make_fetcher(ALL_OK, seen), None, NOW_TS)
        self.assertEqual(seen, [wl.build_chart_url("AAPL")])

    def test_transport_failure_preserves_prior_as_stale_with_original_date(self):
        boom = ConnectionError("curl failed (28): https://query1.finance.yahoo.com/v8/finance/chart/AAPL?token=SECRET123")
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": boom}), self.prior, NOW_TS)
        self.assertEqual((s["status"], s["error"]), ("stale", "fetch_failed"))
        for k in ("src_date", "close", "score", "label", "sma200", "rsi14", "reasons", "points"):
            self.assertEqual(s[k], self.prior[k], k)
        blob = json.dumps(s, ensure_ascii=False)
        for leak in ("SECRET123", "curl", "https://", "query1", "token"):
            self.assertNotIn(leak, blob)

    def test_malformed_body_is_invalid_payload_and_body_is_not_copied(self):
        body = "<html>Too Many Requests SECRET-BODY</html>"
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": body}), self.prior, NOW_TS)
        self.assertEqual((s["status"], s["error"], s["src_date"]), ("stale", "invalid_payload", self.prior["src_date"]))
        self.assertNotIn("SECRET-BODY", json.dumps(s, ensure_ascii=False))

    def test_malformed_and_short_and_error_fixtures_never_become_data(self):
        for name in ("chart_malformed.json", "chart_short.json", "chart_error.json"):
            s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": load_fixture(name)}), self.prior, NOW_TS)
            self.assertEqual((s["status"], s["error"]), ("stale", "invalid_payload"), name)
            self.assertEqual(s["close"], self.prior["close"])

    def test_failure_without_prior_is_unavailable_not_fabricated(self):
        for prior in (None, {}, "junk", {"symbol": "AAPL", "status": "ok"}):
            s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": TimeoutError("x")}), prior, NOW_TS)
            self.assertEqual((s["status"], s["error"], s["score"], s["close"]), ("unavailable", "fetch_failed", None, None), repr(prior))

    def test_prior_is_sanitized_and_must_match_symbol(self):
        tainted = dict(self.prior, secret="/Users/x/.env", html="<b>x</b>")
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": OSError("x")}), tainted, NOW_TS)
        self.assertNotIn("secret", s)
        self.assertNotIn("html", s)
        other = dict(self.prior, symbol="MSFT")
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": OSError("x")}), other, NOW_TS)
        self.assertEqual(s["status"], "unavailable")

    def test_prior_with_non_finite_numbers_is_not_trusted(self):
        bad = dict(self.prior, close=float("nan"))
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": OSError("x")}), bad, NOW_TS)
        self.assertEqual(s["status"], "unavailable")

    def test_failure_logging_never_contains_url_or_exception_text(self):
        logs = []
        boom = ConnectionError("curl failed: https://query1.finance.yahoo.com/?k=SECRET")
        wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": boom}), self.prior, NOW_TS, log=logs.append)
        joined = " ".join(logs)
        self.assertIn("AAPL", joined)
        self.assertIn("fetch_failed", joined)
        for leak in ("SECRET", "https://", "curl"):
            self.assertNotIn(leak, joined)


class MissingSessionMetadataFailSoftTests(unittest.TestCase):
    def _body(self):
        p = copy.deepcopy(ALL_OK["AAPL"])
        del p["chart"]["result"][0]["meta"]["currentTradingPeriod"]
        return p

    def test_prior_kept_as_stale_when_session_metadata_missing(self):
        prior = wl.build_stock("AAPL", "Apple", ALL_OK["AAPL"], NOW_TS)
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": self._body()}), prior, NOW_TS)
        self.assertEqual((s["status"], s["error"], s["src_date"]), ("stale", "invalid_payload", prior["src_date"]))
        self.assertEqual(s["close"], prior["close"])

    def test_unavailable_when_no_prior_and_session_metadata_missing(self):
        s = wl.refresh_stock("AAPL", "Apple", make_fetcher({"AAPL": self._body()}), None, NOW_TS)
        self.assertEqual((s["status"], s["error"], s["score"], s["close"]), ("unavailable", "invalid_payload", None, None))


class RefreshWatchlistTests(unittest.TestCase):
    def test_one_symbol_failure_does_not_break_the_others(self):
        outcomes = dict(ALL_OK)
        outcomes["000660.KS"] = ConnectionError("down")
        wlist = wl.refresh_watchlist(make_fetcher(outcomes), None, NOW_TS)
        items = {i["symbol"]: i for i in wlist["items"]}
        syms = [i["symbol"] for i in wlist["items"]]
        self.assertEqual(syms, [sym for sym, _ in wl.DEFAULT_WATCHLIST])
        self.assertEqual(len(syms), 18)
        self.assertEqual(items["005930.KS"]["status"], "ok")
        self.assertEqual(items["AAPL"]["status"], "ok")
        self.assertEqual(items["000660.KS"]["status"], "unavailable")

    def test_prior_watchlist_items_are_matched_by_symbol(self):
        first = wl.refresh_watchlist(make_fetcher(ALL_OK), None, NOW_TS)
        outcomes = {sym: OSError("x") for sym in ALL_OK}
        outcomes["AAPL"] = ALL_OK["AAPL"]
        second = wl.refresh_watchlist(make_fetcher(outcomes), first, NOW_TS)
        items = {i["symbol"]: i for i in second["items"]}
        self.assertEqual(items["AAPL"]["status"], "ok")
        self.assertEqual(len(second["items"]), 18)
        for sym in ("005930.KS", "000660.KS"):
            prev = {i["symbol"]: i for i in first["items"]}[sym]
            self.assertEqual(items[sym]["status"], "stale")
            self.assertEqual(items[sym]["src_date"], prev["src_date"])
            self.assertEqual(items[sym]["score"], prev["score"])

    def test_unexpected_exception_type_in_one_symbol_is_contained(self):
        outcomes = dict(ALL_OK)
        outcomes["AAPL"] = RuntimeError("surprise")
        wlist = wl.refresh_watchlist(make_fetcher(outcomes), None, NOW_TS)
        result = {i["symbol"]: i["status"] for i in wlist["items"]}
        self.assertEqual(result["005930.KS"], "ok")
        self.assertEqual(result["000660.KS"], "ok")
        self.assertEqual(result["AAPL"], "unavailable")

    def test_garbage_prior_watchlist_is_ignored(self):
        for prior in (None, [], "x", {"items": "nope"}, {"items": [None, 3, {"symbol": "AAPL"}]}):
            wlist = wl.refresh_watchlist(make_fetcher(ALL_OK), prior, NOW_TS)
            self.assertEqual(len(wlist["items"]), 18)

    def test_source_and_formula_version_are_recorded(self):
        wlist = wl.refresh_watchlist(make_fetcher(ALL_OK), None, NOW_TS)
        self.assertEqual(wlist["source"], "Yahoo Finance chart")
        self.assertEqual(wlist["formula"], wl.FORMULA_VERSION)
        self.assertEqual(len(wlist["items"]), 18)


class FetchDataIntegrationTests(unittest.TestCase):
    def test_upd_watchlist_only_adds_watchlist_and_keeps_market_schema(self):
        data = {"asOf": "2026-11-13", "us": {"vix": {"num": 20.3}}, "kr": {"fx": {"num": 1400}}}
        before = copy.deepcopy(data)
        fetch_data.upd_watchlist(data, fetcher=make_fetcher(ALL_OK), now_ts=NOW_TS)
        self.assertEqual(data["asOf"], before["asOf"])
        self.assertEqual(data["us"], before["us"])
        self.assertEqual(data["kr"], before["kr"])
        self.assertEqual(len(data["watchlist"]["items"]), 18)
        json.dumps(data, allow_nan=False)  # strictly valid JSON (no NaN/Infinity)

    def test_fetch_data_registers_watchlist_step(self):
        names = [n for n, _ in fetch_data.STEPS]
        self.assertIn("watchlist", names)
        self.assertEqual(names[:6], ["hy", "vix", "trend_us", "real_yield", "fx", "cape"])

    def test_fetch_data_step_order_unchanged(self):
        names = [n for n, _ in fetch_data.STEPS]
        self.assertEqual(names[:6], ["hy", "vix", "trend_us", "real_yield", "fx", "cape"])


import subprocess  # noqa: E402
from unittest import mock  # noqa: E402


def fake_curl(status="200", seen=None, body_for=None):
    """subprocess.run stand-in: records argv, answers like `curl ... -w '\\n%{http_code}'` would."""
    def run(cmd, **kw):
        if seen is not None:
            seen.append(list(cmd))
        sym = _re.search(r"/chart/([^?]+)\?", cmd[-1])
        body = json.dumps(ALL_OK[sym.group(1)]) if sym and sym.group(1) in ALL_OK else (body_for or "legacy-body")
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{body}\n{status}", stderr="")
    return run


def run_default_watchlist_fetch(seen, status="200"):
    data = {}
    with mock.patch.object(fetch_data.subprocess, "run", fake_curl(status, seen)):
        fetch_data.upd_watchlist(data, now_ts=NOW_TS)
    return data["watchlist"]


class WatchlistNoRedirectTests(unittest.TestCase):
    """Watchlist fetches never follow redirects: validating only the first URL is meaningless otherwise."""

    def test_watchlist_command_has_no_follow_flag_and_keeps_https_only(self):
        seen = []
        run_default_watchlist_fetch(seen)
        self.assertEqual(len(seen), 18)
        for cmd in seen:
            flags = [a for a in cmd[:-1] if a.startswith("-") and not a.startswith("--")]
            self.assertFalse(any("L" in f for f in flags), msg=cmd)  # no -L / -sL
            self.assertEqual(cmd[0], "curl")
            self.assertEqual(cmd[1], "-q", msg=cmd)  # -q must be the first option or it is ignored
            self.assertIn("--no-location", cmd)
            self.assertIn("-s", cmd)
            self.assertNotIn("--location", cmd)
            self.assertNotIn("--location-trusted", cmd)
            i = cmd.index("--proto")
            self.assertEqual(cmd[i + 1], "=https")
            j = cmd.index("--proto-redir")
            self.assertEqual(cmd[j + 1], "=https")
            self.assertTrue(cmd[-1].startswith("https://query1.finance.yahoo.com/v8/finance/chart/"))

    def test_watchlist_default_fetch_still_yields_ok_stocks(self):
        wlist = run_default_watchlist_fetch([])
        self.assertEqual([i["status"] for i in wlist["items"]], ["ok"] * 18)

    def test_redirect_status_is_never_accepted_as_stock_data(self):
        for status in ("301", "302", "303", "307", "308"):
            wlist = run_default_watchlist_fetch([], status=status)
            for item in wlist["items"]:
                self.assertNotEqual(item["status"], "ok", msg=status)
                self.assertEqual(item["status"], "unavailable", msg=status)
                self.assertEqual(item["error"], "fetch_failed", msg=status)

    def test_fetch_without_redirects_rejects_non_2xx(self):
        for status in ("302", "404", "500", "000", "abc"):
            with mock.patch.object(fetch_data.subprocess, "run", fake_curl(status)):
                with self.assertRaises(ConnectionError, msg=status):
                    fetch_data.fetch("https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
                                     follow_redirects=False)

    def test_fetch_without_redirects_returns_body_only(self):
        with mock.patch.object(fetch_data.subprocess, "run", fake_curl("200", body_for="{\"a\": 1}")):
            self.assertEqual(fetch_data.fetch("https://example.invalid/x", follow_redirects=False), "{\"a\": 1}")


import contextlib, http.server, tempfile, threading  # noqa: E402


class _RedirectLab:
    """Two loopback HTTP servers on ephemeral ports: A answers 302 -> B; B counts hits and answers 200."""

    DEST_BODY = "REDIRECT-DESTINATION-REACHED"

    def __enter__(self):
        lab = self
        lab.hits_b = 0
        lock = threading.Lock()

        class B(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                with lock:
                    lab.hits_b += 1
                payload = lab.DEST_BODY.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.srv_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B)
        url_b = f"http://127.0.0.1:{self.srv_b.server_address[1]}/dest"

        class A(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", url_b)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv_a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
        self.url_a = f"http://127.0.0.1:{self.srv_a.server_address[1]}/start"
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.srv_a, self.srv_b)]
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc):
        for s in (self.srv_a, self.srv_b):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join(timeout=5)
        return False


@contextlib.contextmanager
def _ambient_curlrc(content):
    """Temporarily point CURL_HOME at a temp dir holding .curlrc; always restore the environment."""
    keys = ("CURL_HOME", "NO_PROXY", "no_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory() as home:
        with open(os.path.join(home, ".curlrc"), "w", encoding="utf-8") as f:
            f.write(content)
        os.environ["CURL_HOME"] = home
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"  # keep loopback off any ambient proxy
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class AmbientCurlConfigRedirectTests(unittest.TestCase):
    """Real installed curl: omitting -L is not enough when CURL_HOME/.curlrc says `location`."""

    def test_curlrc_location_cannot_make_no_redirect_fetch_follow(self):
        with _RedirectLab() as lab, _ambient_curlrc("location\n"):
            # Precondition (control): the ambient config really does make plain `curl -s` follow to B.
            ctl = subprocess.run(["curl", "-s", "--max-time", "10", lab.url_a],
                                 capture_output=True, text=True)
            self.assertEqual(ctl.stdout, lab.DEST_BODY, msg="control: .curlrc `location` not effective")
            self.assertEqual(lab.hits_b, 1)
            lab.hits_b = 0

            with self.assertRaises(ConnectionError):
                body = fetch_data.fetch(lab.url_a, follow_redirects=False)
                self.fail(f"redirect destination accepted as data: {body!r}")
            self.assertEqual(lab.hits_b, 0, msg="redirect destination was contacted")

    def test_environment_restored_after_helper(self):
        before = {k: os.environ.get(k) for k in ("CURL_HOME", "NO_PROXY", "no_proxy")}
        with _ambient_curlrc("location\n"):
            self.assertIn("CURL_HOME", os.environ)
        self.assertEqual(before, {k: os.environ.get(k) for k in before})


class LegacyFetchUnchangedTests(unittest.TestCase):
    """Regression guards: non-watchlist fetches keep today's redirect-following command and raw stdout."""

    def _cmd(self, **kw):
        seen = []

        def run(cmd, **k):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="raw-body", stderr="")
        with mock.patch.object(fetch_data.subprocess, "run", run):
            out = fetch_data.fetch("https://fred.stlouisfed.org/x", **kw)
        return seen[0], out

    def test_default_fetch_still_follows_redirects_and_returns_stdout_verbatim(self):
        cmd, out = self._cmd()
        self.assertEqual(cmd, ["curl", "-sL", "--max-time", "30", "https://fred.stlouisfed.org/x"])
        self.assertEqual(out, "raw-body")

    def test_https_only_alone_still_follows_redirects(self):
        cmd, _ = self._cmd(https_only=True)
        self.assertIn("-sL", cmd)
        self.assertEqual(cmd[cmd.index("--proto-redir") + 1], "=https")

    def test_browser_ua_still_sent(self):
        cmd, _ = self._cmd(browser_ua=True)
        self.assertEqual(cmd[cmd.index("-A") + 1], fetch_data.UA)
        self.assertIn("-sL", cmd)


# ── Slice 4: freshness + generator (third CSS-only tab) ─────────────────────
import generate as gen  # noqa: E402


def repo_data():
    with open(os.path.join(ROOT, "data.json"), encoding="utf-8") as f:
        d = json.load(f)
    d["asOf"] = "2026-11-14"
    return d


def ok_watchlist():
    return wl.refresh_watchlist(make_fetcher(ALL_OK), None, NOW_TS)


def page_for(watchlist, **extra):
    d = repo_data()
    if watchlist is None:
        d.pop("watchlist", None)
    else:
        d["watchlist"] = watchlist
    d.update(extra)
    return gen.build_page(d)


def cards_of(html_text):
    return ["<article class=\"wcard" + part.split("</article>")[0] + "</article>"
            for part in html_text.split("<article class=\"wcard")[1:]]


def wl_pane(html_text):
    a = html_text.index("<section class=\"pane pane-wl\"")
    return html_text[a:html_text.index("</section>", a)]


class FreshnessTests(unittest.TestCase):
    def test_states(self):
        ok = dict(status="ok", src_date="2026-11-13")
        self.assertEqual(wl.freshness(ok, "2026-11-14"), ("fresh", 1))
        self.assertEqual(wl.freshness(dict(ok, src_date="2026-11-10"), "2026-11-14"), ("fresh", 4))
        self.assertEqual(wl.freshness(dict(ok, src_date="2026-11-09"), "2026-11-14"), ("delayed", 5))
        self.assertEqual(wl.freshness(dict(ok, status="stale"), "2026-11-14"), ("stale", 1))
        self.assertEqual(wl.freshness(dict(status="unavailable", src_date=None), "2026-11-14"), ("unavailable", None))

    def test_bad_or_future_dates_are_unknown(self):
        self.assertEqual(wl.freshness(dict(status="ok", src_date="junk"), "2026-11-14")[0], "unknown")
        self.assertEqual(wl.freshness(dict(status="ok", src_date="2026-11-20"), "2026-11-14")[0], "unknown")
        self.assertEqual(wl.freshness(dict(status="ok", src_date="2026-11-13"), "bad")[0], "unknown")


class WatchlistTabTests(unittest.TestCase):
    def setUp(self):
        self.page = page_for(ok_watchlist())
        self.pane = wl_pane(self.page)

    def test_third_tab_exists_and_two_original_tabs_remain(self):
        for needle in ('id="t-us"', 'id="t-kr"', 'id="t-wl"', 'for="t-wl"', "📌 관심 종목",
                       '#t-wl:checked ~ .content .pane-wl{display:block}',
                       '<section class="pane pane-us">', '<section class="pane pane-kr">',
                       "🇺🇸</span> 미국 증시", "🇰🇷</span> 국내 증시"):
            self.assertIn(needle, self.page)
        self.assertEqual(self.page.count('type="radio"'), 3)
        self.assertIn('id="t-us" class="tabs-radio" checked', self.page)  # default tab unchanged
        # Dialog behavior requires JS; must be in a <script> block, never inline event handlers
        self.assertIn("<script", self.page.lower())
        self.assertNotRegex(self.page.lower(), r"\son[a-z]+=")

    def test_us_and_kr_panes_are_byte_identical_to_before(self):
        d = repo_data()
        d["watchlist"] = ok_watchlist()
        self.assertIn(gen.pane_html("us", d), self.page)
        self.assertIn(gen.pane_html("kr", d), self.page)

    def test_exactly_eighteen_compact_cards_in_order(self):
        cards = cards_of(self.page)
        self.assertEqual(len(cards), 18)
        for card, (sym, name) in zip(cards, wl.DEFAULT_WATCHLIST):
            self.assertIn(sym, card)
            # name may be truncated — check at least partial Korean/English match
            self.assertTrue(name[:4] in card or sym in card, f"{sym}: name or sym not in card")

    def test_compact_card_shows_summary_fields(self):
        """Compact card: name/symbol, score, label, price, 1-day return, freshness."""
        items = {i["symbol"]: i for i in ok_watchlist()["items"]}
        cards = cards_of(self.page)
        # First card (005930.KS) and third card (AAPL) spot-checked
        for idx, (sym, _) in [(0, ("005930.KS", "삼성전자")), (2, ("AAPL", "Apple"))]:
            card = cards[idx]
            it = items[sym]
            self.assertIn(f'{it["score"]}', card)
            self.assertIn(it["label"], card)
            # price in card (formatted)
            self.assertIn(sym, card)
        self.assertIn("KRW", cards[0])
        self.assertIn("USD", cards[2])

    def test_detail_fields_are_in_dialog_json_data(self):
        """Full detail (SMA20/50/200, RSI14, volume ratio, reasons, etc.) lives in the JSON data block."""
        pane = wl_pane(self.page)
        # JSON data block must exist
        self.assertIn('id="wl-items"', pane)
        start = pane.index('id="wl-items"')
        block_start = pane.index('>', start) + 1
        block_end = pane.index('</script>', block_start)
        items_data = json.loads(pane[block_start:block_end])
        self.assertEqual(len(items_data), 18)
        ok_items = [i for i in items_data if i.get("status") == "ok"]
        self.assertTrue(ok_items, "need at least one ok item in JSON data")
        for field in ("close", "ret_1d", "ret_20d", "sma20", "sma50", "sma200",
                      "rsi14", "volume_ratio", "src_date", "score", "label",
                      "currency", "chips", "quality"):
            self.assertIn(field, ok_items[0], f"detail field '{field}' missing from JSON data")

    def test_header_summary_and_rules_based_statement(self):
        self.assertIn("관심 종목 일일 리포트", self.pane)
        self.assertIn("규칙 기반", self.pane)
        self.assertIn("투자 조언이 아닙니다", self.pane)
        self.assertIn("2026-11-13", self.pane)            # data date
        counts = dict((k, int(v)) for v, k in _re.findall(r'class="wsum-item[^"]*"><b>(\d+)</b><span>(.*?)</span>', self.pane))
        items = ok_watchlist()["items"]
        self.assertEqual(counts["우호"], sum(1 for i in items if i["label"] == "우호"))
        self.assertEqual(counts["관망"], sum(1 for i in items if i["label"] == "관망"))
        self.assertEqual(counts["주의"], sum(1 for i in items if i["label"] == "주의"))
        self.assertEqual(counts["데이터 없음"], 0)
        self.assertEqual(sum(counts.values()), 18)

    def test_no_ai_claim_anywhere(self):
        for banned in ("AI가 생성", "AI 분석 결과", "AI 리포트", "AI 추천", "인공지능", "GPT", "LLM"):
            self.assertNotIn(banned, self.page)
        # the only "AI" mention allowed in the pane is the explicit negation
        stripped = self.pane.replace("AI 분석이 아닌", "")
        self.assertNotIn("AI", stripped)

    def test_no_invented_news_targets_or_advice(self):
        # the collapsed limits note may *state* that these are not covered; the cards/header must not mention them
        body = _re.sub(r'<details class="wformula".*?</details>', "", self.pane, flags=_re.S)
        # 뉴스케일파워(SMR)는 종목명이라 허용 — 그 외 '뉴스' 언급은 금지
        body = body.replace("뉴스케일파워", "").replace("\\uB274\\uC2A4", "")
        for banned in ("목표가", "뉴스", "실적", "사세요", "파세요", "촉매", "AI 분석 결과"):
            self.assertNotIn(banned, body)
        self.assertNotRegex(body, r"(?<!과)(매수|매도)")   # "과매도"(RSI 용어)는 허용, 매매 권유 단어는 금지

    def test_formula_and_limits_are_in_a_collapsed_details(self):
        m = _re.search(r'<details class="wformula"([^>]*)>(.*?)</details>', self.pane, _re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("open", m.group(1))
        body = m.group(2)
        for line in wl.formula_lines():
            self.assertIn(_html_escape(line), body)
        for token in ("Wilder", "Yahoo Finance", "지연", "누락", "뉴스·실적·목표가"):
            self.assertIn(token, body)

    def test_no_secrets_local_paths_or_tracking_params(self):
        for bad in ("/Users/", "/home/", "api_key", "apikey", "secret", "utm_", "token=", "sdk-kim-builds"):
            self.assertNotIn(bad, self.page.lower())

    def test_watchlist_css_has_no_text_smaller_than_12px(self):
        css = self.page[self.page.index("/* 관심 종목 탭 */"):self.page.index(".disclaimer{")]
        sizes = [float(x) for x in _re.findall(r"font-size:([\d.]+)px", css)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 12.0, sorted(set(sizes)))

    def test_css_guards_for_mobile(self):
        for rule in ("min-height:44px", "overflow-wrap:anywhere", "min-width:0"):
            self.assertIn(rule, self.page)

    def test_compact_grid_has_two_columns_at_mobile_breakpoint(self):
        """Mobile 360/390px default: 2 columns."""
        self.assertIn("repeat(2,", self.page)

    def test_compact_grid_has_four_columns_at_tablet_breakpoint(self):
        """Tablet: 4 columns."""
        self.assertIn("repeat(4,", self.page)

    def test_compact_grid_has_six_columns_at_desktop_breakpoint(self):
        """Desktop: 6 columns."""
        self.assertIn("repeat(6,", self.page)


def _html_escape(t):
    import html
    return html.escape(t)


class WatchlistDegradedStateTests(unittest.TestCase):
    def test_missing_watchlist_renders_18_unavailable_cards(self):
        page = page_for(None)
        cards = cards_of(page)
        self.assertEqual(len(cards), 18)
        for card, (sym, name) in zip(cards, wl.DEFAULT_WATCHLIST):
            self.assertIn(sym, card)
            self.assertIn("데이터 없음", card)
            self.assertNotIn("/100", card)
            self.assertNotRegex(card, r"\d{4}-\d{2}-\d{2}")   # no invented source date
        counts = dict((k, int(v)) for v, k in _re.findall(r'class="wsum-item[^"]*"><b>(\d+)</b><span>(.*?)</span>', wl_pane(page)))
        self.assertEqual(counts["데이터 없음"], 18)

    def test_stale_card_keeps_original_date_and_fixed_reason_only(self):
        w = ok_watchlist()
        w["items"][2] = dict(w["items"][2], status="stale", error="fetch_failed", src_date="2026-11-06")
        page = page_for(w)
        card = cards_of(page)[2]
        self.assertIn("2026-11-06", card)          # compact card: original date + status pill
        self.assertIn("갱신 실패", card)
        detail = dialog_json(page)[2]              # popup detail: fixed reason + previous-value caveat
        self.assertEqual(detail["src_date"], "2026-11-06")
        self.assertIn(wl.SAFE_ERRORS["fetch_failed"], detail["warn"])
        self.assertIn("이전 값", detail["warn"])

    def test_unknown_error_code_never_echoed(self):
        w = ok_watchlist()
        w["items"][0] = dict(w["items"][0], status="stale", error="<b>curl failed https://x</b>")
        page = page_for(w)
        self.assertNotIn("curl failed", page)
        self.assertNotIn("https://x", page)

    def test_delayed_source_date_is_flagged(self):
        w = ok_watchlist()
        w["items"][1] = dict(w["items"][1], src_date="2026-11-02")
        card = cards_of(page_for(w))[1]
        self.assertIn("지연", card)
        self.assertIn("2026-11-02", card)

    def test_every_dynamic_field_is_html_escaped(self):
        w = ok_watchlist()
        evil = dict(w["items"][0], name="<script>alert(1)</script>__KR__",
                    reasons=[{"text": '"><img src=x onerror=alert(2)>', "tone": "pos"},
                             {"text": "<b>bold</b>", "tone": "<i>x</i>"}],
                    currency="<b>X</b>")
        w["items"][0] = evil
        w["items"][1] = dict(w["items"][1], label="<u>우호</u>", src_date="<i>2026</i>")
        page = page_for(w)
        for raw in ("<script>alert(1)", "<img src=x", "<b>bold</b>", "<i>x</i>", "<i>2026</i>", "<b>X</b>", "<u>우호</u>"):
            self.assertNotIn(raw, page)
        self.assertEqual(len(cards_of(page)), 18)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn('id="t-wl"', page)       # placeholder collision (__KR__) did not corrupt the page
        stripped = page.replace("&lt;script&gt;alert(1)&lt;/script&gt;__KR__", "")
        stripped = stripped.replace("\\u003cscript\\u003ealert(1)\\u003c/script\\u003e__KR__", "")
        self.assertNotIn("__KR__", stripped)

    def test_non_finite_and_wrong_typed_numbers_render_as_dash(self):
        w = ok_watchlist()
        w["items"][0] = dict(w["items"][0], sma50=float("nan"), rsi14=float("inf"), ret_20d="12", volume_ratio=True)
        page = page_for(w)
        self.assertNotRegex(" ".join(cards_of(page)).lower(), r"\b(nan|infinity|inf|true)\b")
        self.assertNotRegex(_re.search(r'id="wl-items">(.*?)</script>', page, _re.S).group(1), r"[:\[,](NaN|-?Infinity)[,\]}]")
        detail = dialog_json(page)[0]              # missing numbers are dashes/null in the popup data
        vals = {m["k"]: m["v"] for m in detail["metrics"]}
        self.assertEqual((vals["SMA50"], vals["RSI14"], vals["20일"], vals["거래량"]), ("—",) * 4)
        self.assertIsNone(detail["sma50"])

    def test_malformed_items_and_extra_items(self):
        w = ok_watchlist()
        # Keep all items valid except first two; inject an extra unknown symbol
        orig_items = list(w["items"])
        w["items"] = [None, "x"] + orig_items[2:] + [dict(orig_items[0], symbol="MSFT", name="Microsoft")]
        page = page_for(w)
        cards = cards_of(page)
        self.assertEqual(len(cards), 18)               # defaults only; extra MSFT ignored
        self.assertNotIn("MSFT", page)
        self.assertIn("데이터 없음", cards[0])    # 005930.KS — None
        self.assertIn("데이터 없음", cards[1])    # 000660.KS — "x"
        self.assertNotIn("데이터 없음", cards[2]) # AAPL — ok

    def test_asof_semantics_unchanged(self):
        page = page_for(ok_watchlist(), asOf="2026-11-14")
        self.assertEqual(page.count("데이터 기준 · <b>2026-11-14</b>"), 2)   # us + kr panes keep asOf


# ── Slice 6: compact cards + single reusable <dialog> ───────────────────────
def _script_blocks(html_text):
    return _re.findall(r"<script([^>]*)>(.*?)</script>", html_text, _re.S)


def behavior_js(html_text):
    js = [body for attrs, body in _script_blocks(html_text) if "application/json" not in attrs]
    assert len(js) == 1, f"expected exactly one behavior script, got {len(js)}"
    return js[0]


def dialog_json(html_text):
    blocks = [body for attrs, body in _script_blocks(html_text) if "application/json" in attrs]
    assert len(blocks) == 1
    return json.loads(blocks[0])


def wl_css(page):
    return page[page.index("/* 관심 종목 탭 */"):page.index(".disclaimer{")]


class CompactCardMarkupTests(unittest.TestCase):
    def setUp(self):
        self.page = page_for(ok_watchlist())
        self.pane = wl_pane(self.page)
        self.cards = cards_of(self.page)

    def test_each_card_is_one_semantic_button_with_index_and_accessible_name(self):
        for i, (card, (sym, name)) in enumerate(zip(self.cards, wl.DEFAULT_WATCHLIST)):
            self.assertEqual(card.count("<button"), 1, sym)
            self.assertIn('type="button"', card)
            self.assertIn(f'data-idx="{i}"', card)
            self.assertIn('aria-haspopup="dialog"', card)
            self.assertRegex(card, r'aria-label="[^"]*' + _re.escape(sym))

    def test_full_names_are_rendered_not_truncated_in_markup(self):
        for card, (sym, name) in zip(self.cards, wl.DEFAULT_WATCHLIST):
            self.assertIn(_html_escape(name), card)

    def test_compact_card_omits_detail_metrics(self):
        for card in self.cards:
            for detail in ("SMA20", "SMA50", "SMA200", "RSI14", "wchips", "wgrid"):
                self.assertNotIn(detail, card)

    def test_compact_card_shows_return_price_and_freshness(self):
        items = {i["symbol"]: i for i in ok_watchlist()["items"]}
        card = self.cards[2]
        self.assertIn(f'{items["AAPL"]["ret_1d"] * 100:+.1f}%', card)
        self.assertIn("최신", card)
        self.assertIn(f'{items["AAPL"]["close"]:,.2f}', card)

    def test_unavailable_and_stale_status_visible_on_compact_card(self):
        w = ok_watchlist()
        w["items"][2] = dict(w["items"][2], status="stale", error="fetch_failed")
        cards = cards_of(page_for(w))
        self.assertIn("갱신 실패", cards[2])
        self.assertIn("데이터 없음", cards_of(page_for(None))[0])

    def test_cards_are_buttons_even_when_unavailable(self):
        for card in cards_of(page_for(None)):
            self.assertEqual(card.count("<button"), 1)


class DialogMarkupTests(unittest.TestCase):
    def setUp(self):
        self.page = page_for(ok_watchlist())
        self.pane = wl_pane(self.page)

    def test_exactly_one_native_dialog(self):
        self.assertEqual(self.page.count("<dialog"), 1)
        self.assertEqual(self.pane.count("<dialog"), 1)
        self.assertIn('aria-labelledby="wl-dlg-title"', self.pane)
        self.assertNotIn("<div role=\"dialog\"", self.page)

    def test_dialog_has_visible_labelled_close_button(self):
        m = _re.search(r'<button[^>]*class="wl-close"[^>]*>(.*?)</button>', self.pane, _re.S)
        self.assertIsNotNone(m)
        self.assertIn('aria-label="닫기"', m.group(0))
        self.assertIn('type="button"', m.group(0))
        self.assertTrue(m.group(1).strip())

    def test_dialog_has_slots_for_all_detail_content(self):
        for slot in ("wl-dlg-title", "wl-dlg-sym", "wl-dlg-label", "wl-dlg-score", "wl-dlg-price",
                     "wl-dlg-fresh", "wl-dlg-date", "wl-dlg-warn", "wl-dlg-metrics",
                     "wl-dlg-chips", "wl-dlg-foot"):
            self.assertIn(f'id="{slot}"', self.pane)

    def test_noscript_explains_detail_requires_js(self):
        self.assertRegex(self.pane, r"<noscript>.*?JavaScript.*?</noscript>")

    def test_json_has_one_entry_per_symbol_in_order_with_detail_metrics(self):
        data = dialog_json(self.page)
        self.assertEqual([d["symbol"] for d in data], [s for s, _ in wl.DEFAULT_WATCHLIST])
        self.assertEqual([d["name"] for d in data], [n for _, n in wl.DEFAULT_WATCHLIST])
        d = data[2]
        labels = [m["k"] for m in d["metrics"]]
        self.assertEqual(labels, ["1일", "20일", "SMA20", "SMA50", "SMA200", "RSI14", "거래량"])
        self.assertTrue(d["chips"])
        self.assertEqual(d["src_date"], "2026-11-13")
        self.assertIn("Yahoo Finance", d["foot"])

    def test_json_block_cannot_break_out_of_script(self):
        w = ok_watchlist()
        w["items"][0] = dict(w["items"][0], name="</script><script>alert(1)</script>",
                             reasons=[{"text": "</script><b>x", "tone": "pos"}, {"text": "&<>", "tone": "neu"}])
        page = page_for(w)
        self.assertNotIn("</script><script>alert(1)", page)
        self.assertEqual(len(_script_blocks(page)), 2)
        data = dialog_json(page)  # still parses
        self.assertEqual(len(data), 18)

    def test_skhy_sma200_is_explicitly_missing_not_fabricated(self):
        data = dialog_json(self.page)
        skhy = next(d for d in data if d["symbol"] == "SKHY")
        sma200 = next(m for m in skhy["metrics"] if m["k"] == "SMA200")
        self.assertEqual(sma200["v"], "—")
        self.assertIn("누락", skhy["warn"])
        item ={i["symbol"]: i for i in ok_watchlist()["items"]}["SKHY"]
        self.assertEqual((item["quality"], item["sma200"]), ("partial", None))
        self.assertLessEqual(item["score"], wl.PARTIAL_SCORE_CAP)


class DialogScriptTests(unittest.TestCase):
    def setUp(self):
        self.page = page_for(ok_watchlist())
        self.js = behavior_js(self.page)

    def test_no_innerhtml_or_unsafe_sinks(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                    "new Function", "srcdoc"):
            self.assertNotIn(bad, self.js)
        self.assertIn("textContent", self.js)
        self.assertIn("JSON.parse", self.js)

    def test_script_has_no_network_or_tracking(self):
        for bad in ("fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "document.cookie", "http://", "https://"):
            self.assertNotIn(bad, self.js)

    def test_uses_native_modal_api_and_single_dialog_lookup(self):
        self.assertIn("showModal()", self.js)
        self.assertIn("wl-dialog", self.js)
        self.assertEqual(self.js.count("showModal()"), 1)

    def test_close_paths_button_backdrop_and_escape_cancel(self):
        self.assertIn("wl-close", self.js)
        self.assertIn(".close()", self.js)
        self.assertIn("e.target === dlg", self.js)          # backdrop click
        self.assertIn("'close'", self.js)                   # Escape also fires native close event
        self.assertNotIn("preventDefault", self.js)         # never swallow Escape (cancel)

    def test_focus_restoration_and_scroll_lock(self):
        self.assertIn(".focus()", self.js)
        self.assertIn("overflow", self.js)
        self.assertIn("'hidden'", self.js)
        self.assertRegex(self.js, r"overflow\s*=\s*''")

    def test_no_inline_handlers_in_markup(self):
        self.assertNotRegex(self.page.lower(), r"\son[a-z]+=")
        self.assertNotIn("javascript:", self.page.lower())


class CompactCssTests(unittest.TestCase):
    def setUp(self):
        self.page = page_for(ok_watchlist())
        self.css = wl_css(self.page)

    def test_mobile_default_two_columns_then_four_then_six(self):
        self.assertRegex(self.css, r"\.wlist\{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)\)")
        m4 = _re.search(r"@media\(min-width:(\d+)px\)\{\.wlist\{[^}]*repeat\(4,", self.css)
        m6 = _re.search(r"@media\(min-width:(\d+)px\)\{\.wlist\{[^}]*repeat\(6,", self.css)
        self.assertIsNotNone(m4)
        self.assertIsNotNone(m6)
        self.assertLessEqual(int(m4.group(1)), 768)
        self.assertGreater(int(m4.group(1)), 390)
        self.assertGreater(int(m6.group(1)), int(m4.group(1)))
        self.assertLessEqual(int(m6.group(1)), 1100)

    def test_card_button_min_target_44px_and_wrapping_guards(self):
        btn = _re.search(r"\.wbtn\{([^}]*)\}", self.css).group(1)
        self.assertIn("min-height:44px", btn)
        self.assertIn("min-width:0", btn)
        self.assertIn("overflow-wrap:anywhere", self.css)
        close = _re.search(r"\.wl-close\{([^}]*)\}", self.css).group(1)
        self.assertIn("min-height:44px", close)
        self.assertIn("min-width:44px", close)

    def test_name_is_clamped_safely(self):
        self.assertRegex(self.css, r"\.wname\{[^}]*-webkit-line-clamp:2")

    def test_dialog_is_mobile_friendly_scrolls_and_has_safe_area(self):
        self.assertRegex(self.css, r"\.wdlg\{[^}]*max-height:")
        self.assertRegex(self.css, r"\.wdlg\{[^}]*overflow-y:auto")
        self.assertIn("env(safe-area-inset-bottom)", self.css)
        self.assertIn(".wdlg::backdrop", self.css)

    def test_no_font_size_below_12px_anywhere_in_watchlist_css(self):
        sizes = [float(x) for x in _re.findall(r"font-size:([\d.]+)px", self.css)]
        self.assertGreaterEqual(min(sizes), 12.0)


class UsKrUnchangedTests(unittest.TestCase):
    def test_us_kr_panes_identical_with_and_without_watchlist(self):
        d = repo_data()
        with_wl = gen.build_page(dict(d, watchlist=ok_watchlist()))
        without = gen.build_page({k: v for k, v in d.items() if k != "watchlist"})
        for mkt in ("us", "kr"):
            pane = gen.pane_html(mkt, d)
            self.assertIn(pane, with_wl)
            self.assertIn(pane, without)
        self.assertEqual(with_wl.count("<dialog"), 1)


class EighteenFetchIsolationTests(unittest.TestCase):
    def test_each_single_failure_is_isolated_across_all_18(self):
        syms = [s for s, _ in wl.DEFAULT_WATCHLIST]
        for bad in syms:
            outcomes = dict(ALL_OK)
            outcomes[bad] = ConnectionError("down")
            out = wl.refresh_watchlist(make_fetcher(outcomes), None, NOW_TS)["items"]
            self.assertEqual({i["symbol"]: i["status"] for i in out},
                             {s: ("unavailable" if s == bad else "ok") for s in syms}, bad)

    def test_skhy_short_history_is_partial_and_status_ok(self):
        item = {i["symbol"]: i for i in ok_watchlist()["items"]}["SKHY"]
        self.assertEqual((item["status"], item["quality"], item["sma200"]), ("ok", "partial", None))
        self.assertIsNotNone(item["sma50"])

    def test_all_18_states_render_for_fresh_stale_unavailable(self):
        w = ok_watchlist()
        w["items"][3] = dict(w["items"][3], status="stale", error="fetch_failed")
        w["items"][4] = wl.unavailable_stock("CPER", "CPER")
        w["items"][5] = dict(w["items"][5], src_date="2026-11-01")
        cards = cards_of(page_for(w))
        self.assertEqual(len(cards), 18)
        self.assertIn("갱신 실패", cards[3])
        self.assertIn("데이터 없음", cards[4])
        self.assertIn("지연", cards[5])
        self.assertIn("최신", cards[0])


# ── Slice 5: workflow runs the deterministic tests before fetch/generate ────
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        wf_dir = os.path.join(ROOT, ".github", "workflows")
        self.files = sorted(os.listdir(wf_dir))
        with open(os.path.join(wf_dir, "update.yml"), encoding="utf-8") as f:
            self.text = f.read()

    def test_single_workflow_file(self):
        self.assertEqual(self.files, ["update.yml"])

    def test_tests_run_before_fetch_and_generate(self):
        i_test = self.text.index("python3 -m unittest discover -s tests")
        i_fetch = self.text.index("python3 fetch_data.py")
        i_gen = self.text.index("python3 generate.py")
        self.assertLess(i_test, i_fetch)
        self.assertLess(i_test, i_gen)
        step_names = _re.findall(r"- name: (.+)", self.text)
        self.assertEqual(step_names[0], "run deterministic tests")

    def test_schedules_unchanged(self):
        crons = _re.findall(r"- cron: '([^']+)'", self.text)
        self.assertEqual(crons, ["30 23 * * 0-4", "00 07 * * 1-5", "30 12 * * 0-5", "50 14 * * 0-5"])
        self.assertIn("workflow_dispatch:", self.text)

    def test_no_secrets_notifications_or_extra_actions(self):
        self.assertNotIn("secrets.", self.text)
        self.assertEqual(_re.findall(r"uses: (\S+)", self.text), ["actions/checkout@v4"])
        for bad in ("slack", "webhook", "curl -X POST", "OPENAI", "ANTHROPIC", "pip install", "npm "):
            self.assertNotIn(bad.lower(), self.text.lower())


if __name__ == "__main__":
    unittest.main()
