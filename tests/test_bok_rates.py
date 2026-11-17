"""No credentials/network: strict official-table and collector integration regressions."""
import copy
from datetime import datetime, timezone, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bok_rates import BOK_HISTORY_URL, parse_bok_history
import kr_flows

NOW = datetime(2026, 11, 17, 18, tzinfo=timezone(timedelta(hours=9)))


def page(rows, unit='%', extra=''):
    return (extra + '<span>한국은행 기준금리를 년도별 ' + unit + '로 나타낸 그래프</span>'
            '<table><caption>한국은행 기준금리 추이</caption><thead><tr>'
            '<th colspan="2">변경일자</th><th>기준금리</th></tr></thead><tbody>'
            + ''.join('<tr>' + ''.join('<td>' + v + '</td>' for v in r) + '</tr>' for r in rows)
            + '</tbody></table>')


RISE = [('2026', '08월 27일', '3.00'), ('2026', '07월 16일', '2.75')]


class BOKRateTests(unittest.TestCase):
    def parse(self, html):
        return parse_bok_history(html, observed_at=NOW)

    def test_rise_is_last_change_not_current_policy(self):
        r = self.parse(page(RISE))
        self.assertEqual((r['num'], r['rate_change_date'], r['tone']), (3.0, '2026-08-27', 'red'))
        self.assertEqual(r['observed_at'], NOW.isoformat(timespec='seconds'))
        self.assertNotIn('인상', r['val'])
        self.assertIn('최근 금통위 결정일·동결 여부를 뜻하지 않음', r['note'])
        self.assertEqual(r['tone_basis'], 'last_rate_change')

    def test_cut_preserves_old_tone_mapping(self):
        r = self.parse(page([('2026', '08월 27일', '2.50'), ('2026', '07월 16일', '2.75')]))
        self.assertEqual((r['num'], r['tone']), (2.5, 'green'))

    def test_hold_prose_does_not_relabel_date(self):
        r = self.parse(page(RISE, extra='<p>통화정책방향 (2026.10.22) 기준금리를 연 3.00%로 유지</p>'))
        self.assertEqual(r['rate_change_date'], '2026-08-27')
        self.assertNotIn('10-22', str(r))

    def test_equal_rate_row_does_not_become_change(self):
        r = self.parse(page([('2026', '10월 22일', '3.00')] + RISE))
        self.assertEqual(r['rate_change_date'], '2026-08-27')

    def test_chart_endpoint_is_not_a_change(self):
        r = self.parse(page(RISE, extra='<script>var chart=[["2026/11/17",9.99]];</script>'))
        self.assertEqual((r['num'], r['rate_change_date']), (3.0, '2026-08-27'))

    def test_missing_or_multiple_table_fails_closed(self):
        for html in ('<p>기준금리 3.00%</p>', page(RISE) + page(RISE), page([])):
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.parse(html)

    def test_conflicting_dates_future_and_invalid_date_fail(self):
        for rows in ([RISE[0], ('2026', '08월 27일', '2.75')],
                     [('2026', '12월 01일', '3.00'), RISE[1]],
                     [('2026', '02월 30일', '3.00'), RISE[1]], list(reversed(RISE))):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.parse(page(rows))

    def test_wrong_units_values_and_structure_fail(self):
        for html in (page(RISE, unit='bp'), page(RISE, unit=''),
                     page([('2026', '08월 27일', '300bp'), RISE[1]]),
                     page([('2026', '08월 27일', '3.00 2.75'), RISE[1]]),
                     page([('2026', '08월 27일', 'NaN'), RISE[1]]),
                     page([('2026', '08월 27일', '99.00'), RISE[1]]),
                     page(RISE).replace('변경일자', '결정일자'),
                     page(RISE).replace('<td>3.00</td>', '<td rowspan="2">3.00</td>')):
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.parse(html)

    def test_timezone_required(self):
        with self.assertRaisesRegex(ValueError, 'timezone'):
            parse_bok_history(page(RISE), observed_at=NOW.replace(tzinfo=None))

    def test_collector_uses_only_public_bok_and_fails_closed(self):
        spec = importlib.util.spec_from_file_location('fetch_bok_test', ROOT/'scripts/fetch_toss_market.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        class Response:
            text = page(RISE)
            def raise_for_status(self):
                pass
        class HTTP:
            calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()
        http = HTTP()
        with patch.object(mod, 'requests', http):
            self.assertEqual(mod.fetch_bok_base_rate()['num'], 3.0)
            self.assertEqual(http.calls[0][0], BOK_HISTORY_URL)
            with patch.object(Response, 'text', '<html>error</html>'), self.assertRaises(ValueError):
                mod.fetch_bok_base_rate()
        original = {'num': 3.0, 'val': 'previous', 'src_date': 'old', 'tone': 'red'}
        data = {'kr': {'bok': copy.deepcopy(original)}}
        kr_flows.apply_artifact(data, {'fields': {}, 'failed': {'kr.bok': 'bok_unique_history_table_required'}}, NOW)
        self.assertEqual(data['kr']['bok'], original)


if __name__ == '__main__':
    unittest.main()
