"""供应商分段边界、复权来源和缺失语义，避免研究快照默默损坏。"""
import math
import hashlib
import json
import types

import pandas as pd
import pytest
import requests

from research.tools.etf_data import fetch_tencent as fetcher
from research.tools.etf_data.fetch_tencent import combine, intervals, parse_response


def payload(rows, key='day'):
    return {'code': 0, 'data': {'sh515880': {key: rows}}}


ROW = ['2020-01-02', '1.00', '1.02', '1.03', '.99', '100']


def test_two_year_windows_cover_exact_requested_range_without_overlap():
    windows = list(intervals('2015-10-01', '2020-09-30'))
    assert windows == [('2015-10-01', '2016-12-31'),
                       ('2017-01-01', '2018-12-31'),
                       ('2019-01-01', '2020-09-30')]
    with pytest.raises(ValueError):
        list(intervals('2021-01-01', '2020-01-01'))


def test_unadjusted_response_cannot_substitute_for_hfq():
    with pytest.raises(ValueError, match='hfqday'):
        parse_response(payload([ROW]), 'sh515880', 'hfq', '2020-01-01', '2020-12-31')


def test_explicit_empty_prelisting_response_contains_no_guessed_adjustment():
    frame = parse_response(payload([]), 'sh515880', 'hfq', '2015-01-01', '2016-12-31')
    assert frame.empty


def test_day_response_requires_opt_in_and_keeps_response_basis():
    frame = parse_response(payload([ROW]), 'sh515880', 'hfq', '2020-01-01',
                           '2020-12-31', allow_day_response=True)
    assert frame.attrs['response_key'] == 'day'


def test_alternate_endpoint_extra_history_is_clipped_only_with_full_coverage():
    dates = pd.bdate_range(end='2026-09-30', periods=640).strftime('%Y-%m-%d')
    rows = [[date, *ROW[1:]] for date in dates]
    frame = parse_response(payload(rows), 'sh515880', '', dates[200], dates[-1],
                           allow_leading_history=True)
    assert len(frame) == 440
    assert frame.iloc[0]['date'] == dates[200]
    assert frame.attrs['leading_rows_discarded'] == 200
    with pytest.raises(ValueError, match='历史被截断'):
        parse_response(payload(rows), 'sh515880', '', '2016-01-01', dates[-1],
                       allow_leading_history=True)


@pytest.mark.parametrize('rows,match', [
    ([ROW, ROW], '日期重复'),
    ([ROW] * 640, '截断上限'),
    ([['2021-01-01', *ROW[1:]]], '超出请求日期'),
])
def test_malformed_or_out_of_window_data_is_rejected(rows, match):
    with pytest.raises(ValueError, match=match):
        parse_response(payload(rows), 'sh515880', '', '2020-01-01', '2020-12-31')


def test_vendor_prices_stay_distinct_and_missing_amount_is_not_fabricated():
    raw = parse_response(payload([ROW]), 'sh515880', '', '2020-01-01', '2020-12-31')
    hfq = raw.copy()
    for field in ['open', 'close', 'high', 'low']:
        hfq[field] *= 2
    out = combine(raw, hfq)
    assert out.iloc[0]['close'] == 1.02
    assert out.iloc[0]['hfq_close'] == 2.04
    assert out.iloc[0]['volume'] == 10000
    assert math.isnan(out.iloc[0]['amount'])
    assert out.iloc[0]['quality'] == 'ok'


def test_mismatched_price_dates_are_not_silently_dropped():
    raw = parse_response(payload([ROW]), 'sh515880', '', '2020-01-01', '2020-12-31')
    hfq = raw.copy()
    hfq['date'] = '2020-01-03'
    with pytest.raises(ValueError, match='日期不齐'):
        combine(raw, hfq)


def test_zero_activity_and_invalid_prices_remain_explicit():
    raw = parse_response(payload([ROW]), 'sh515880', '', '2020-01-01', '2020-12-31')
    raw['volume'] = 0
    assert combine(raw, raw).iloc[0]['quality'] == 'zero_activity'
    raw['close'] = float('nan')
    assert combine(raw, raw).iloc[0]['quality'] == 'invalid_price'


def test_strict_cache_read_rejects_day_basis_and_failed_refresh_preserves_file(tmp_path, monkeypatch):
    daily = tmp_path / 'etf_daily'
    daily.mkdir()
    path = daily / '515880.csv'
    original = b'date,close\n2020-01-02,1.02\n'
    path.write_bytes(original)
    meta = {'source': fetcher.SOURCE, 'schema_version': fetcher.VERSION,
            'requested_start': '2020-01-01', 'requested_end': '2020-12-31',
            'hfq_day_response_rows': 1, 'sha256': hashlib.sha256(original).hexdigest()}
    path.with_suffix('.json').write_text(json.dumps(meta), encoding='utf-8')
    assert fetcher.fetch_code('515880', '2020-01-01', '2020-12-31', tmp_path,
                              allow_day_response=True) == meta
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise requests.ConnectionError('offline')

    monkeypatch.setattr(fetcher.LOCAL, 'session', types.SimpleNamespace(get=unavailable), raising=False)
    with pytest.raises(requests.ConnectionError, match='offline'):
        fetcher.fetch_code('515880', '2020-01-01', '2020-12-31', tmp_path)
    assert len(calls) == 2
    assert path.read_bytes() == original
