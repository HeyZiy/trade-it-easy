"""诊断价格尺度、缺测与回测边界，避免把近似路径误当作执行收益。"""

import numpy as np
import pandas as pd
import pytest

from research.studies.quality_momentum.internal.analyze_stop_loss import Prices, episode_amounts


def price_world(stop=180):
    history = pd.bdate_range('2024-01-02', periods=180)
    prices = Prices.__new__(Prices)
    prices.calendar = history[40:stop]
    raw = pd.Series(10.0 + np.arange(len(history)) * .1, index=history)
    adjusted = raw * 2
    prices.get = lambda code: (raw, adjusted)
    episode = dict(code='600000.XSHG', entry_date=history[40], exit_date=history[45],
                   first_fill_price=raw.iloc[40], exit_fill_price=raw.iloc[45])
    return prices, raw, adjusted, episode


def test_prior_window_uses_pre_backtest_history_and_excludes_entry_day():
    prices, raw, adjusted, episode = price_world()
    adjusted.iloc[40:] = 1000  # 入场后价格不得改变入场前指标。
    result = prices.path(episode)
    assert result['prior20_return'] == pytest.approx(raw.iloc[39] / raw.iloc[19] - 1)


def test_missing_holding_close_does_not_fabricate_extrema_or_discard_valid_future_window():
    prices, raw, adjusted, episode = price_world()
    raw.iloc[42] = adjusted.iloc[42] = np.nan
    result = prices.path(episode)
    assert result['path_status'] == 'missing_holding_close'
    assert np.isnan(result['mae_close']) and np.isnan(result['mfe_close'])
    assert result['approx_exit_return'] == pytest.approx(14.5 / 14.0 - 1)
    assert result['post20_status'] == 'ok'


def test_future_windows_do_not_extend_past_account_backtest_end():
    prices, _, _, episode = price_world(stop=50)
    result = prices.path(episode)
    assert result['post5_status'] == 'end_of_backtest'
    assert 'post5_from_exit' not in result


def test_split_adjustment_keeps_fill_and_close_prices_on_same_scale():
    prices, raw, adjusted, episode = price_world()
    raw[:] = 10.0
    adjusted[:] = 20.0
    raw.iloc[43:] = 5.0
    episode['first_fill_price'], episode['exit_fill_price'] = 10.0, 5.0
    result = prices.path(episode)
    assert result['approx_exit_return'] == pytest.approx(0)
    assert result['mae_close'] == result['mfe_close'] == 0


def test_trade_cash_difference_uses_exported_fee_once():
    # 费用已含卖出税，订单现金差不得再次扣税；也不冒称含分红完整收益。
    orders = pd.DataFrame([dict(is_buy=True, notional=1000., price=10., filled=100., commission=5.),
                           dict(is_buy=False, notional=1100., price=11., filled=100., commission=6.1)])
    result = episode_amounts({'is_closed': True}, orders)
    assert result['trade_cash_difference'] == pytest.approx(88.9)
