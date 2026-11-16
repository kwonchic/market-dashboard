import copy
import importlib.util
import unittest
from test_kr_flows import make_records, NOW_MORNING
import kr_flows as kf
import generate as gen

class Crosscheck(unittest.TestCase):
    def setUp(self):
        self.records = make_records(n=30)
        self.dates = [r['date'] for r in self.records]

    def test_exact_window_and_evidence(self):
        f = kf.compute_flows_from_records(self.records, NOW_MORNING, trading_dates=self.dates)
        self.assertEqual(f['window_validation'], 'kospi_candles')
        self.assertEqual(len(f['validated_trading_dates']),20)

    def test_missing_middle_or_latest_rejected_even_with_20_records(self):
        for idx in (0,9):
            records=copy.deepcopy(self.records); del records[idx]
            with self.subTest(idx=idx), self.assertRaises(kf.FlowsDataError):
                kf.compute_flows_from_records(records,NOW_MORNING,trading_dates=self.dates)

    def test_actual_holiday_gap_agreed_by_candles(self):
        del self.records[4]; del self.dates[4]
        self.assertEqual(kf.compute_flows_from_records(self.records,NOW_MORNING,trading_dates=self.dates)['window_validation'],'kospi_candles')

    def test_insufficient_and_future_candles_rejected(self):
        for dates in (self.dates[:19], ['2099-01-01']+self.dates):
            with self.assertRaises(kf.FlowsDataError):
                kf.compute_flows_from_records(self.records,NOW_MORNING,trading_dates=dates)

    def test_incomplete_today_excluded_on_both_sides(self):
        from test_kr_flows import rec
        from datetime import date
        records=[rec(date(2026,11,17),1,1)]+self.records
        f=kf.compute_flows_from_records(records,NOW_MORNING,trading_dates=['2026-11-17']+self.dates)
        self.assertEqual(f['src_date_iso'],'2026-11-16')

    def test_failed_previous_day_and_future_not_used_by_meter(self):
        for fields in ({'src_date_iso':'2026-11-16','collect_status':'failed'}, {'src_date_iso':'2099-01-01'}):
            f={'tone':'green','val':'순매수',**fields}
            gen._gate_flows(f,{'generatedAt':NOW_MORNING.isoformat()})
            self.assertEqual(f['tone'],'gray')
            self.assertIn(f['fresh']['state'],('stale','invalid'))

    def test_unverified_aggregation_not_claimed_as_complete_window(self):
        f=kf.compute_flows_from_records(self.records,NOW_MORNING)
        self.assertEqual(kf.flows_freshness(f,NOW_MORNING)['state'],'unknown')

    def test_collector_fetches_independent_candles(self):
        spec=importlib.util.spec_from_file_location('collector','scripts/fetch_toss_market.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        outer=self
        class Client:
            def get(self,path,params):
                if path.endswith('investor-trading'): return {'records':outer.records}
                outer.assertTrue(path.endswith('/KOSPI/candles'))
                outer.assertEqual(params,{'interval':'1d','count':'40'})
                return {'candles':[{'timestamp':d} for d in outer.dates]}
        f,raw=module.compute_flows(Client(),NOW_MORNING)
        self.assertEqual(f['window_validation'],'kospi_candles')
        self.assertEqual(raw['trading_day_reference']['dates'],self.dates)
