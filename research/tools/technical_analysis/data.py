"""行情准备；调用方显式选择 raw 或前复权。"""
import pandas as pd


def forward_adjust(kline, backward_factor, code):
    """OHLC × 因子 / 区间末因子；不调整成交量和成交额。

    因子缺失、非正时抛出异常；禁止在复权请求失败后隐式返回原始价。
    """
    if backward_factor is None or code not in backward_factor:
        raise ValueError(f'{code} 缺少复权因子')
    result = kline.copy()
    dates = pd.to_datetime(result['kline_time'] if 'kline_time' in result else result.index)
    factor = backward_factor[code].copy()
    factor.index = pd.to_datetime(factor.index)
    factor = factor.sort_index()
    factor = factor[~factor.index.duplicated(keep='last')]
    aligned = factor.reindex(dates, method='ffill')
    if aligned.empty or aligned.isna().any() or not (aligned > 0).all():
        raise ValueError(f'{code} 区间内复权因子缺失或非正')
    ratio = aligned.to_numpy() / aligned.iloc[-1]
    for field in ('open', 'high', 'low', 'close'):
        result[field] = result[field] * ratio
    return result


def fetch_kline(code, begin_date, end_date, *, adjust='raw'):
    """返回按 kline_time 排序的行情；adjust 可选 raw/pre，无隐式股票判断。"""
    if adjust not in ('raw', 'pre'):
        raise ValueError('adjust 必须为 raw 或 pre')
    from data_provider.fetchers.amazingdata.market import get_raw_kline

    frame = get_raw_kline([code], begin_date, end_date).get(code)
    if frame is None or frame.empty:
        raise ValueError(f'{code} 行情为空')
    frame = frame.copy()
    frame['kline_time'] = pd.to_datetime(frame['kline_time'])
    frame = frame.sort_values('kline_time').set_index('kline_time', drop=False)
    if adjust == 'pre':
        from data_provider.fetchers.amazingdata.client import client
        factors = client.get_base_data().get_backward_factor([code], is_local=False)
        frame = forward_adjust(frame, factors, code)
    return frame
