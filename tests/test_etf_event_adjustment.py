"""捕获腾讯加法式复权的真实失真，并验证显式现金/份额事件。"""
import pandas as pd
import pytest

from research.tools.etf_data.event_adjustment import build_prices, parse_events


def test_real_510300_pair_keeps_return_instead_of_vendor_offset_distortion():
    # 已复现：腾讯hfq 1.683→1.818只有8.02%；原始价与聚宽约9.40%。
    raw = pd.DataFrame({'date': ['2024-09-27', '2024-09-30'],
                        'close': [3.869, 4.233], 'hfq_close': [1.683, 1.818]})
    out = build_prices(raw, [])
    result = out.event_close.iloc[1] / out.event_close.iloc[0] - 1
    jq = 5.112 / 4.673 - 1
    vendor = 1.818 / 1.683 - 1
    assert abs(result - jq) < .001
    assert abs(vendor - jq) > .01


def test_cash_dividend_does_not_create_artificial_loss():
    raw = pd.DataFrame({'date': ['2026-01-16', '2026-01-19'], 'close': [10., 9.]})
    out = build_prices(raw, [{'date': '2026-01-19', 'cash_per_share': 1., 'split_ratio': 1.}])
    assert out.event_close.tolist() == pytest.approx([10., 10.])


def test_explicit_share_split_preserves_price_path():
    raw = pd.DataFrame({'date': ['2026-01-16', '2026-01-19'], 'close': [12., 2.]})
    out = build_prices(raw, [{'date': '2026-01-16', 'cash_per_share': 0., 'split_ratio': 6.}])
    assert out.event_close.tolist() == [12., 12.]


def test_no_future_event_changes_earlier_prices():
    raw = pd.DataFrame({'date': ['2026-01-16', '2026-01-19'], 'close': [10., 9.]})
    event = {'date': '2026-01-20', 'cash_per_share': 1., 'split_ratio': 1.}
    assert build_prices(raw, [event]).event_close.tolist() == [10., 9.]


def test_simultaneous_event_units_are_not_guessed():
    raw = pd.DataFrame({'date': ['2026-01-16', '2026-01-19'], 'close': [10., 9.]})
    event = {'date': '2026-01-19', 'effective_date': '2026-01-19',
             'cash_per_share': 1., 'split_ratio': 2.}
    with pytest.raises(ValueError, match='单位'):
        build_prices(raw, [event])


def test_cash_unit_and_split_ratio_come_from_explicit_event_tables():
    html = '''<table><tr><th>除息日</th><th>每10份分红</th></tr>
    <tr><td>2026-01-19</td><td>每10份派现金1.2300元</td></tr></table>
    <table><tr><th>拆分折算日</th><th>拆分折算比例</th></tr>
    <tr><td>2025-01-02</td><td>1:6.0000</td></tr></table>'''
    events = parse_events(html)
    assert events[0]['split_ratio'] == 6.
    assert events[1]['cash_per_share'] == pytest.approx(.123)


def test_split_registration_day_is_not_the_price_ex_date():
    raw = pd.DataFrame({'date': ['2025-07-31', '2025-08-01', '2025-08-04'],
                        'close': [1.146, 1.138, .572]})
    out = build_prices(raw, [{'date': '2025-08-01', 'cash_per_share': 0., 'split_ratio': 2.}])
    assert out.event_close.tolist() == pytest.approx([1.146, 1.138, 1.144])
