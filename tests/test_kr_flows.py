#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KR 외국인/기관 수급(20 완료거래일) 집계·검증 + 수집 아티팩트 적용 + 화면 기준일 분리 테스트.

stdlib unittest만 사용 (GitHub 러너에서도 requests 없이 실행 가능).
모든 수치는 mock 레코드 기반이며 실데이터 복구 증거가 아니다.
"""
import copy, json, os, sys, unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import kr_flows as kf  # noqa: E402
import generate as gen  # noqa: E402

KST = timezone(timedelta(hours=9))


def weekdays_back(end, n):
    """end(포함)부터 거꾸로 평일 n개 (최신 -> 과거)."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def rec(d, f_net, i_net, base=5_000_000_000_000):
    """f_net/i_net: 원 단위 순매수. buy/sell로 분해."""
    return {
        "date": d.isoformat(),
        "foreigner": {"buyAmount": str(base + f_net), "sellAmount": str(base)},
        "institution": {"buyAmount": str(base + i_net), "sellAmount": str(base)},
    }


def make_records(end=date(2026, 11, 16), n=20, f_unit=1e11, i_unit=-5e10):
    # i번째(0=최신) 거래일 외국인 순매수 = f_unit*(i+1), 기관 = i_unit
    return [rec(d, int(f_unit * (i + 1)), int(i_unit)) for i, d in enumerate(weekdays_back(end, n))]


NOW_MORNING = datetime(2026, 11, 17, 8, 40, tzinfo=KST)   # 화요일 장전
NOW_EVENING = datetime(2026, 11, 17, 21, 35, tzinfo=KST)  # 화요일 장후


class SelectCompletedRecords(unittest.TestCase):
    def test_exact_20_completed_newest_first(self):
        recs = make_records()
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(len(sel), 20)
        self.assertEqual(sel[0]["date"], "2026-11-16")
        self.assertEqual(sel[-1]["date"], "2026-10-20")

    def test_today_incomplete_excluded_before_close(self):
        recs = [rec(date(2026, 11, 17), 9e12, 0)] + make_records()
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(sel[0]["date"], "2026-11-16")
        self.assertEqual(len(sel), 20)

    def test_today_included_after_cutoff(self):
        recs = make_records(end=date(2026, 11, 17), n=21)
        sel = kf.select_completed_records(recs, NOW_EVENING)
        self.assertEqual(sel[0]["date"], "2026-11-17")
        self.assertEqual(len(sel), 20)

    def test_19_records_insufficient(self):
        with self.assertRaises(kf.FlowsDataError) as cm:
            kf.select_completed_records(make_records(n=19), NOW_MORNING)
        self.assertIn("insufficient", str(cm.exception))

    def test_20_with_today_partial_only_19_completed(self):
        # 과거 count=20 요청 시 장중 당일 레코드가 섞이면 완료일은 19개뿐 → 실패해야 함
        recs = [rec(date(2026, 11, 17), 1, 1)] + make_records(n=19)
        with self.assertRaises(kf.FlowsDataError):
            kf.select_completed_records(recs, NOW_MORNING)

    def test_identical_duplicates_collapsed(self):
        recs = make_records(n=21)
        recs.insert(3, copy.deepcopy(recs[3]))
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(len({r["date"] for r in sel}), 20)

    def test_conflicting_duplicates_rejected(self):
        recs = make_records(n=21)
        dup = copy.deepcopy(recs[3]); dup["foreigner"]["buyAmount"] = "1"
        recs.insert(3, dup)
        with self.assertRaises(kf.FlowsDataError) as cm:
            kf.select_completed_records(recs, NOW_MORNING)
        self.assertIn("duplicate", str(cm.exception))

    def test_ascending_order_normalized(self):
        recs = list(reversed(make_records()))
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(sel[0]["date"], "2026-11-16")
        dates = [r["date"] for r in sel]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_weekend_record_rejected(self):
        recs = make_records()
        recs[2]["date"] = "2026-11-15"  # 일요일
        with self.assertRaises(kf.FlowsDataError) as cm:
            kf.select_completed_records(recs, NOW_MORNING)
        self.assertIn("weekend", str(cm.exception))

    def test_holiday_gap_is_fine(self):
        # 평일 휴장(레코드 없음)은 정상: 거래일은 레코드가 정의한다
        recs = make_records(n=22)
        del recs[4]
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(len(sel), 20)

    def test_future_record_excluded(self):
        recs = [rec(date(2026, 11, 18), 1, 1)] + make_records()
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(sel[0]["date"], "2026-11-16")

    def test_utc_timestamp_converted_to_kst_date(self):
        recs = make_records(n=21)
        # 2026-11-16 15:00 UTC == 2026-11-17 00:00 KST → 당일(미완료)로 취급되어 제외
        recs.insert(0, dict(rec(date(2026, 11, 17), 5, 5), date="2026-11-16T15:00:00Z"))
        sel = kf.select_completed_records(recs, NOW_MORNING)
        self.assertEqual(sel[0]["date"], "2026-11-16")
        self.assertEqual(len(sel), 20)

    def test_missing_amount_rejected(self):
        recs = make_records()
        del recs[5]["foreigner"]["sellAmount"]
        with self.assertRaises(kf.FlowsDataError) as cm:
            kf.select_completed_records(recs, NOW_MORNING)
        self.assertIn("missing", str(cm.exception))

    def test_unit_sanity_rejects_absurd_amounts(self):
        recs = make_records()
        recs[0]["foreigner"]["buyAmount"] = str(5e15)  # 하루 5천조원: 단위 오류
        with self.assertRaises(kf.FlowsDataError) as cm:
            kf.select_completed_records(recs, NOW_MORNING)
        self.assertIn("unit", str(cm.exception))


class ComputeFlows(unittest.TestCase):
    def test_sums_in_jo_and_iso_dates(self):
        recs = make_records()
        out = kf.compute_flows_from_records(recs, NOW_MORNING)
        # 외국인 5일 = 1e11*(1+..+5)=1.5e12 → 1.5조, 20일 = 1e11*210 = 21조, 기관 5일 = -0.25조
        self.assertAlmostEqual(out["f5_jo"], 1.5)
        self.assertAlmostEqual(out["f20_jo"], 21.0)
        self.assertAlmostEqual(out["i5_jo"], -0.25)
        self.assertEqual(out["num"], 1.5)
        self.assertEqual(out["src_date_iso"], "2026-11-16")
        self.assertEqual(out["src_date"], "11-16")
        self.assertEqual(out["window"], {"start": "2026-10-20", "end": "2026-11-16", "trading_days": 20})
        self.assertEqual(out["tone"], "green")
        self.assertIn("+1.50조", out["note"])
        self.assertEqual(out["collect_status"], "ok")
        self.assertTrue(out["observed_at"].startswith("2026-11-17T08:40"))

    def test_red_when_both_negative(self):
        out = kf.compute_flows_from_records(make_records(f_unit=-1e11), NOW_MORNING)
        self.assertEqual(out["tone"], "red")


class ApplyArtifact(unittest.TestCase):
    def base(self):
        return {"asOf": "2026-09-29", "us": {"vix": {"num": 15}}, "watchlist": {"items": [1]},
                "kr": {"flows": {"num": -6.69, "src_date": "09-29", "tone": "red", "val": "x", "note": "old"},
                       "fx": {"num": 1360}, "trend": {}, "vol": {}}}

    def test_failed_flows_keep_old_values_and_mark_failed(self):
        d = self.base()
        art = {"collected_at": "2026-11-17T08:40:00+09:00", "fields": {"kr.fx": {"num": 1400}},
               "failed": {"kr.flows": "request_failed 401"}}
        kf.apply_artifact(d, art, NOW_MORNING)
        f = d["kr"]["flows"]
        self.assertEqual((f["num"], f["src_date"], f["note"]), (-6.69, "09-29", "old"))
        self.assertEqual(f["collect_status"], "failed")
        self.assertEqual(f["last_attempt_at"], "2026-11-17T08:40:00+09:00")
        self.assertNotIn("last_success_at", f)
        self.assertEqual(d["kr"]["fx"]["num"], 1400)
        self.assertEqual(d["us"], {"vix": {"num": 15}})          # US 비침해
        self.assertEqual(d["watchlist"], {"items": [1]})          # watchlist 비침해
        self.assertTrue(d["generatedAt"].startswith("2026-11-17T08:40"))

    def test_success_sets_last_success(self):
        d = self.base()
        flows = kf.compute_flows_from_records(make_records(), NOW_MORNING)
        art = {"collected_at": flows["observed_at"], "fields": {"kr.flows": flows}, "failed": {}}
        kf.apply_artifact(d, art, NOW_MORNING)
        f = d["kr"]["flows"]
        self.assertEqual(f["src_date_iso"], "2026-11-16")
        self.assertEqual(f["collect_status"], "ok")
        self.assertEqual(f["last_success_at"], flows["observed_at"])
        self.assertNotIn("last_error", f)

    def test_apply_is_idempotent(self):
        flows = kf.compute_flows_from_records(make_records(), NOW_MORNING)
        art = {"collected_at": flows["observed_at"], "fields": {"kr.flows": flows}, "failed": {}}
        a, b = self.base(), self.base()
        kf.apply_artifact(a, art, NOW_MORNING)
        kf.apply_artifact(b, art, NOW_MORNING); kf.apply_artifact(b, art, NOW_MORNING)
        self.assertEqual(a, b)


class FlowsFreshness(unittest.TestCase):
    def test_fresh_when_latest_completed_day(self):
        st = kf.flows_freshness({"src_date_iso": "2026-11-16"}, NOW_MORNING)
        self.assertEqual(st["state"], "fresh")
        self.assertEqual(st["expected"], "2026-11-16")

    def test_monday_morning_expects_friday(self):
        st = kf.flows_freshness({"src_date_iso": "2026-11-13"}, datetime(2026, 11, 16, 8, 35, tzinfo=KST))
        self.assertEqual(st["state"], "fresh")

    def test_one_day_lag_is_delayed(self):
        st = kf.flows_freshness({"src_date_iso": "2026-11-13"}, NOW_MORNING)
        self.assertEqual(st["state"], "delayed")
        self.assertEqual(st["lag_weekdays"], 1)

    def test_old_is_stale(self):
        st = kf.flows_freshness({"src_date_iso": "2026-09-29"}, NOW_MORNING)
        self.assertEqual(st["state"], "stale")

    def test_legacy_mmdd_without_year_is_unknown(self):
        st = kf.flows_freshness({"src_date": "09-29"}, NOW_MORNING)
        self.assertEqual(st["state"], "unknown")

    def test_failed_collect_never_fresh(self):
        st = kf.flows_freshness({"src_date_iso": "2026-11-16", "collect_status": "failed"}, NOW_MORNING)
        self.assertNotEqual(st["state"], "fresh")


def page_data(flows, as_of="2026-11-17", generated="2026-11-17T08:40:00+09:00"):
    # Synthetic neutral snapshot: nightly data.json and wall-clock changes must
    # not change the unrelated indicators or saturate this regression's meter.
    d = {
        mkt: {key: {"num": 0, "val": "고정 시험 지표", "tone": "gray", "auto": False}
              for key in gen.META[mkt]}
        for mkt in ("us", "kr")
    }
    d["watchlist"] = {"items": []}
    d["asOf"] = as_of
    if generated:
        d["generatedAt"] = generated
    else:
        d.pop("generatedAt", None)
    d["kr"]["flows"] = copy.deepcopy(flows)
    return d


STALE_LEGACY = {"num": -6.69, "val": "외국인 순매도 지속", "src_date": "09-29", "auto": True, "tone": "red",
                "note": "외국인 5일 -6.69조 · 20일 -19.40조 · 기관 5일 +0.59조"}


class PageRendering(unittest.TestCase):
    def test_stale_legacy_flows_warned_not_claimed_fresh(self):
        html = gen.pane_html("kr", page_data(STALE_LEGACY))
        self.assertIn("수급 기준일", html)
        self.assertIn("09-29", html)
        self.assertIn("확인 필요", html)
        self.assertIn('class="flows-warn', html)
        self.assertNotIn("토스증권 실측상 <b>외국인 순매도 지속</b>", html)

    def test_stale_flows_not_claimed_in_tiles_and_listed_as_old(self):
        html = gen.pane_html("kr", page_data(STALE_LEGACY))
        self.assertNotIn("싸지만 외국인 순매도 전환", html)
        self.assertIn("외국인 수급 최신 기준일 미확인", html)
        self.assertIn("<li>외국인 수급</li>", html)
        fresh = dict(STALE_LEGACY, src_date_iso="2026-11-16", src_date="11-16", collect_status="ok")
        html2 = gen.pane_html("kr", page_data(fresh))
        self.assertIn("싸지만 외국인 순매도 지속", html2)
        self.assertNotIn("<li>외국인 수급</li>", html2)

    def test_stale_flows_excluded_from_kr_judgment(self):
        d = page_data(STALE_LEGACY)
        inds, pos, _ = gen.compute("kr", d)
        self.assertEqual(inds["flows"]["tone"], "gray")
        fresh = dict(STALE_LEGACY, src_date_iso="2026-11-16", src_date="11-16", collect_status="ok")
        inds2, pos2, _ = gen.compute("kr", page_data(fresh))
        self.assertEqual(inds2["flows"]["tone"], "red")
        self.assertEqual((pos, pos2), (50, 58))

    def test_stale_exclusion_at_and_above_meter_cap(self):
        fresh = dict(STALE_LEGACY, src_date_iso="2026-11-16", src_date="11-16", collect_status="ok")
        # Raw pressure 74 vs82 crosses cap; 82 vs90 saturates both. Equal
        # displayed scores at the cap are correct, not proof of a broken gate.
        for red_keys, expected in ((('valuation', 'trend'), (74, 78)),
                                   (('valuation', 'trend', 'vol'), (78, 78))):
            with self.subTest(red_keys=red_keys):
                stale_data, fresh_data = page_data(STALE_LEGACY), page_data(fresh)
                for d in (stale_data, fresh_data):
                    for key in red_keys:
                        d['kr'][key]['tone'] = 'red'
                stale_inds, stale_pos, _ = gen.compute('kr', stale_data)
                fresh_inds, fresh_pos, _ = gen.compute('kr', fresh_data)
                self.assertEqual(stale_inds['flows']['tone'], 'gray')
                self.assertEqual(fresh_inds['flows']['tone'], 'red')
                self.assertEqual((stale_pos, fresh_pos), expected)

    def test_page_fixture_is_independent_of_live_file_and_clock(self):
        with patch('builtins.open', side_effect=AssertionError('live file read')):
            first = page_data(STALE_LEGACY)
            second = page_data(STALE_LEGACY)
            self.assertEqual(first, second)
            self.assertEqual(first['generatedAt'], NOW_MORNING.isoformat())
            self.assertEqual(kf.parse_generated(first), NOW_MORNING)
            self.assertEqual(gen.compute('kr', first)[1], 50)
        first['kr']['flows']['tone'] = 'green'
        self.assertEqual(second['kr']['flows']['tone'], 'red')
        self.assertEqual(STALE_LEGACY['tone'], 'red')

    def test_fresh_flows_shows_iso_basis_without_warning(self):
        fresh = dict(STALE_LEGACY, src_date_iso="2026-11-16", src_date="11-16", collect_status="ok",
                     window={"start": "2026-10-20", "end": "2026-11-16", "trading_days": 20})
        html = gen.pane_html("kr", page_data(fresh))
        self.assertIn("수급 기준일 <b>2026-11-16</b>", html)
        self.assertIn("2026-10-20~2026-11-16 · 20거래일", html)
        self.assertNotIn('class="flows-warn', html)

    def test_generated_time_separate_from_flows_basis(self):
        html = gen.pane_html("kr", page_data(STALE_LEGACY))
        self.assertIn("페이지 생성 · <b>2026-11-17 08:40 KST</b>", html)

    def test_generated_falls_back_to_asof(self):
        html = gen.pane_html("kr", page_data(STALE_LEGACY, generated=None))
        self.assertIn("데이터 기준 · <b>2026-11-17</b> (페이지 생성일)", html)
        self.assertNotIn("페이지 생성 · <b>", html)

    def test_us_pane_untouched_by_flows_state(self):
        a = gen.pane_html("us", page_data(STALE_LEGACY))
        fresh = dict(STALE_LEGACY, src_date_iso="2026-11-16", collect_status="ok")
        b = gen.pane_html("us", page_data(fresh))
        self.assertEqual(a, b)

    def test_full_page_builds(self):
        page = gen.build_page(page_data(STALE_LEGACY))
        self.assertIn("관심 종목", page)
        self.assertIn("수급 기준일", page)


if __name__ == "__main__":
    unittest.main()
