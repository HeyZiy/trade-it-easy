"""行情复查的真实错误模式：拆股、动态四舍五入及停牌不可成交端点。"""
import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from types import ModuleType, SimpleNamespace

from research.studies.value_revenue.internal.audit_quotes import compare_windows, make_summary
from research.studies.value_revenue.internal.build_quote_audit_platform import render


def endpoints(raw_entry, raw_exit, factor_entry=1, factor_exit=1, paused_entry=0, paused_exit=0):
    rows = []
    for date, price, factor, paused in [('2025-07-01', raw_entry, factor_entry, paused_entry),
                                        ('2025-08-01', raw_exit, factor_exit, paused_exit)]:
        rows.append(dict(code='600001.XSHG', date=date, raw_open=price, raw_factor=factor,
                         raw_paused=paused, pre_open=price * factor / 10, pre_factor=factor / 10,
                         pre_paused=paused, post_open=price * factor, post_factor=factor,
                         post_paused=paused))
    return pd.DataFrame(rows)


def returns(value, entry_valid=True):
    return pd.DataFrame([dict(code='600001.XSHG', signal='2025-06-30', entry='2025-07-01',
                              exit='2025-08-01', horizon=20, future_return=value, entry_valid=entry_valid)])


def test_split_is_adjustment_not_fifty_percent_loss():
    row = compare_windows(returns(0), endpoints(10, 5, factor_exit=2)).iloc[0]
    assert row.raw_return == -.5
    assert row.reference_return == pytest.approx(0)
    assert row.pre_post_difference == pytest.approx(0)
    assert row.replayed_return == pytest.approx(0)
    assert row.adjustment_present


def test_dynamic_rounding_is_separated_from_basis_mismatch():
    quotes = endpoints(10, 9.8, factor_exit=1.02345)
    expected = 9.8 / round(10 / 1.02345, 2) - 1
    out = compare_windows(returns(expected), quotes)
    assert out.iloc[0].export_minus_replayed == pytest.approx(0)
    assert abs(out.iloc[0].export_minus_reference) > 1e-5
    assert out.iloc[0].pre_post_difference == pytest.approx(0)
    summary = make_summary(out).iloc[0]
    assert summary.replay_mismatch_1e8 == 0
    assert summary.adjustment_windows == 1


@pytest.mark.parametrize('side', ['entry', 'exit'])
def test_paused_quote_is_missing_even_if_provider_fills_price(side):
    quotes = endpoints(10, 10, paused_entry=int(side == 'entry'), paused_exit=int(side == 'exit'))
    row = compare_windows(returns(np.nan, entry_valid=side != 'entry'), quotes).iloc[0]
    assert row.endpoint_class == side + '_paused'
    assert pd.isna(row.reference_return)
    assert not row.entry_status_disagrees


def test_historical_missing_quote_and_valid_recheck_are_flagged_without_backfilling():
    out = compare_windows(returns(np.nan), endpoints(10, 11))
    assert pd.isna(out.iloc[0].future_return)
    assert out.iloc[0].reference_return == pytest.approx(.1)
    assert make_summary(out).iloc[0].missing_now_available == 1


def test_status_only_fill_explains_pause_without_using_filled_price_for_returns():
    quotes = endpoints(10, np.nan)
    quotes.loc[1, 'raw_paused'] = np.nan
    quotes['status_paused'] = [np.nan, 1]
    quotes['status_open'] = [np.nan, 10]
    row = compare_windows(returns(np.nan), quotes).iloc[0]
    assert row.endpoint_class == 'exit_paused'
    assert pd.isna(row.exit_raw_open)
    assert pd.isna(row.reference_return)


def test_security_end_does_not_assign_fictitious_total_loss():
    info = {'600001.XSHG': {'end_date': '2025-07-31'}}
    out = compare_windows(returns(np.nan), endpoints(10, np.nan), info)
    assert out.iloc[0].endpoint_class == 'exit_after_security_end'
    assert pd.isna(out.iloc[0].future_return)
    assert pd.isna(out.iloc[0].reference_return)
    assert make_summary(out).iloc[0].exit_after_security_end == 1


def test_platform_auditor_rechecks_prices_without_engine_or_orders(monkeypatch, tmp_path):
    import sys
    import json
    data = returns(0)
    data.to_csv(tmp_path / 'expectations_v1_returns.csv', index=False)
    (tmp_path / 'expectations_v1_metadata.json').write_text(
        json.dumps({'first_day': '2025-06-30', 'last_day': '2025-08-01'}), encoding='utf-8')
    module = ModuleType('jqdata')

    def get_price(codes, **kwargs):
        assert kwargs['round'] is False
        assert kwargs['fill_paused'] is False
        exit_day = kwargs['end_date'] == '2025-08-01'
        price = 5 if exit_day else 10
        factor = 2 if exit_day else 1
        if kwargs['fq'] == 'post':
            price *= factor
        elif kwargs['fq'] == 'pre':
            price *= factor / 10
        return pd.DataFrame([dict(code=c, time=kwargs['end_date'], open=price,
                                  factor=factor, paused=0) for c in codes])

    module.get_price = get_price
    module.get_security_info = lambda code: SimpleNamespace(end_date='2200-01-01')
    # 与之前平台同名注入问题一致；生成器必须保护内置引用。
    module.any, module.sum = np.any, np.sum
    monkeypatch.setitem(sys.modules, 'jqdata', module)
    monkeypatch.chdir(tmp_path)
    namespace = {}
    exec(compile(render(), '<quote-audit-jq>', 'exec'), namespace)
    out = pd.read_csv(tmp_path / 'expectations_quote_audit_2025-06-30_2025-08-01' / 'comparison.csv')
    assert out.reference_return.iloc[0] == pytest.approx(0)
    assert out.replayed_return.iloc[0] == pytest.approx(0)
    assert (tmp_path / 'expectations_quote_audit_2025-06-30_2025-08-01.zip').exists()


def test_platform_auditor_generated_source_is_current():
    target = Path(__file__).resolve().parents[1] / 'research/studies/value_revenue/internal/quote_audit_jq.py'
    assert target.read_text(encoding='utf-8') == render()
