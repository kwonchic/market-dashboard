"""Strict, stdlib-only parser for the BOK's official base-rate history table.

A history row is a rate-change date, not the latest Monetary Policy Board decision.
Never use chart data: its final point may merely extend the line to today's date.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re

BOK_HISTORY_URL = 'https://www.bok.or.kr/portal/singl/baseRate/list.do?menuNo=200656'
KST = timezone(timedelta(hours=9))


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.caption = False
        self.ignored = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.ignored += 1
        if self.ignored:
            return
        if tag == 'table':
            if self.table is not None:
                raise ValueError('bok_nested_table')
            self.table = {'caption': '', 'rows': []}
        elif self.table is not None:
            if tag == 'caption':
                self.caption = True
            elif tag == 'tr':
                if self.row is not None:
                    raise ValueError('bok_malformed_row')
                self.row = []
            elif tag in ('td', 'th'):
                if self.row is None or self.cell is not None:
                    raise ValueError('bok_malformed_cell')
                if any(k in ('rowspan', 'colspan') and v != '1' for k, v in attrs) and tag == 'td':
                    raise ValueError('bok_ambiguous_data_span')
                self.cell = [tag, '']

    def handle_data(self, data):
        if self.ignored:
            return
        self.text.append(data)
        if self.table is not None:
            if self.caption:
                self.table['caption'] += data
            if self.cell is not None:
                self.cell[1] += data

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignored = max(0, self.ignored - 1)
            return
        if self.ignored:
            return
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(tuple(self.cell))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.cell is not None:
                raise ValueError('bok_unclosed_cell')
            self.table['rows'].append(self.row)
            self.row = None
        elif tag == 'caption':
            self.caption = False
        elif tag == 'table' and self.table is not None:
            if self.row is not None:
                raise ValueError('bok_unclosed_row')
            self.tables.append(self.table)
            self.table = None


def _compact(value):
    return re.sub(r'\s+', '', value)


def parse_bok_history(html, *, observed_at=None):
    """Return the current published rate and last actual change, or raise ValueError."""
    observed_at = observed_at or datetime.now(KST)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError('bok_observed_at_timezone_required')
    observed_at = observed_at.astimezone(KST)
    if not isinstance(html, str) or len(html) > 2_000_000:
        raise ValueError('bok_html_invalid')
    parser = _Tables()
    parser.feed(html)
    parser.close()
    if parser.table is not None:
        raise ValueError('bok_unclosed_table')
    matches = [t for t in parser.tables if _compact(t['caption']) == '한국은행기준금리추이']
    if len(matches) != 1:
        raise ValueError('bok_unique_history_table_required')
    rows = matches[0]['rows']
    if not rows or [(_compact(v)) for k, v in rows[0] if k == 'th'] not in (
        ['변경일자', '기준금리'], ['변경일자', '기준금리(%)']
    ) or any(k != 'th' for k, _ in rows[0]):
        raise ValueError('bok_history_headers_changed')
    # Actual BOK page describes the graph/table unit in an accessible text span.
    unit_text = _compact(''.join(parser.text))
    if (_compact(rows[0][-1][1]) != '기준금리(%)'
            and '한국은행기준금리를년도별%로나타낸그래프' not in unit_text):
        raise ValueError('bok_percent_unit_required')
    values = []
    for row in rows[1:]:
        if len(row) != 3 or any(k != 'td' for k, _ in row):
            raise ValueError('bok_history_row_invalid')
        year, month_day, rate = [_compact(v) for _, v in row]
        match = re.fullmatch(r'(\d{1,2})월(\d{1,2})일', month_day)
        if not re.fullmatch(r'\d{4}', year) or not match or not re.fullmatch(r'\d{1,2}(?:\.\d{1,2})?', rate):
            raise ValueError('bok_history_value_invalid')
        try:
            changed = date(int(year), int(match[1]), int(match[2]))
        except ValueError as exc:
            raise ValueError('bok_history_date_invalid') from exc
        number = Decimal(rate)
        if not Decimal('0') <= number <= Decimal('30') or changed > observed_at.date():
            raise ValueError('bok_history_range_or_future_date')
        if values and changed >= values[-1][0]:
            raise ValueError('bok_history_dates_not_unique_descending')
        values.append((changed, number))
    if len(values) < 2:
        raise ValueError('bok_history_insufficient')
    # Even if a hold row appears, do not relabel it as a new rate-change date.
    current = values[0][1]
    last_same = 0
    while last_same + 1 < len(values) and values[last_same + 1][1] == current:
        last_same += 1
    if last_same + 1 == len(values):
        raise ValueError('bok_prior_distinct_rate_required')
    changed = values[last_same][0]
    previous = values[last_same + 1][1]
    direction = '인상' if current > previous else '인하'
    return {
        'num': float(current),
        'val': f'{current:.2f}% (마지막 변경 {changed.isoformat()})',
        'src_date': changed.strftime('%m-%d'),
        'rate_change_date': changed.isoformat(),
        'observed_at': observed_at.isoformat(timespec='seconds'),
        'auto': True,
        # Preserve the old color mapping, explicitly referring to LAST CHANGE only.
        'tone': 'red' if direction == '인상' else 'green',
        'tone_basis': 'last_rate_change',
        'note': (f'한국은행 기준금리 추이표: 마지막 변경 {changed.isoformat()}, '
                 f'{previous:.2f}% → {current:.2f}% ({abs(current-previous):.2f}%p {direction}). '
                 '최근 금통위 결정일·동결 여부를 뜻하지 않음. '
                 f'조회 {observed_at.isoformat(timespec="seconds")}'),
        'source': 'BOK',
        'source_url': BOK_HISTORY_URL,
    }
