"""行情原始表、价格矩阵、执行快照与 ETF 复权；不管理登录态。"""
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List
import pandas as pd
from data_provider.types import DataFetchError
from .client import client
from ._utils import (_chunks, _tgw_codes, _normalize_map, _plain_code,
                     _resolve_cache_dir, date_range)

AMAZINGDATA_CACHE_DIR = _resolve_cache_dir()
_KLINE_CHUNK = 200
_FACTOR_CHUNK = 500
_INFO_CHUNK = 32
_SHANGHAI = timezone(timedelta(hours=8))


def get_latest_snapshot(code: str, *, now=None):
    """沪深股票/ETF 当天最近五分钟的快照；午休及盘后取本时段末尾。

    query_snapshot 是历史查询接口，不承诺实时推送。只接受查询窗口内、
    不晚于请求时间的数据；当日无快照或时间无法验证时交给上层回退。
    """
    tgw_codes = _tgw_codes([code])
    if not tgw_codes:
        return None
    now = pd.Timestamp(now if now is not None else datetime.now(_SHANGHAI))
    now = now.tz_localize(_SHANGHAI) if now.tzinfo is None else now.tz_convert(_SHANGHAI)
    # SDK 时钟参数精确到毫秒，时区由交易所（北京时间）定义。
    end = now.floor('ms')
    midday = end.replace(hour=11, minute=30, second=0, microsecond=0)
    afternoon = end.replace(hour=13, minute=0, second=0, microsecond=0)
    close = end.replace(hour=15, minute=0, second=0, microsecond=0)
    if midday < end < afternoon:
        end = midday
    end = min(end, close)
    begin = max(end - timedelta(minutes=5), end.normalize())

    def clock_value(stamp):
        return int(stamp.strftime('%H%M%S')) * 1000 + stamp.microsecond // 1000

    day = int(end.strftime('%Y%m%d'))
    try:
        raw = client.get_market_data().query_snapshot(
            tgw_codes, begin_date=day, end_date=day,
            begin_time=clock_value(begin), end_time=clock_value(end))
    except SystemExit as exc:
        raise DataFetchError('AmazingData 快照查询被中断') from exc
    except Exception as exc:
        raise DataFetchError(f'AmazingData 最新快照查询失败: {exc}') from exc
    frame = _normalize_map(raw, tgw_codes).get(_plain_code(tgw_codes[0]))
    if frame is None or frame.empty:
        return None
    if 'trade_time' in frame.columns:
        raw_times = frame['trade_time']
    elif isinstance(frame.index, pd.DatetimeIndex):
        raw_times = frame.index
    else:
        return None
    times = pd.DatetimeIndex(pd.to_datetime(raw_times, errors='coerce'))
    times = times.tz_localize(_SHANGHAI) if times.tz is None else times.tz_convert(_SHANGHAI)
    valid = (times >= begin) & (times <= end)
    if not valid.any():
        return None
    # 按交易所时间选择，不依赖 SDK 返回顺序；最新一笔价格无效时不偷用旧笔。
    return frame.iloc[valid.nonzero()[0][times[valid].argmax()]]


def get_raw_kline(code_list, begin_date, end_date, period=None):
    """返回 SDK 格式代码→原始表。显式后缀保留指数/股票的区别，价格未复权。"""
    begin, end = date_range(begin_date, end_date)
    if not code_list:
        return {}
    if period is None:
        from AmazingData.utils.constant import Period
        period = Period.day
    market = client.get_market_data()
    result = {}
    for batch in _chunks(code_list, _KLINE_CHUNK):
        try:
            raw = market.query_kline(batch, begin_date=begin, end_date=end,
                                     period=getattr(period, "value", period))
        except SystemExit as exc:
            raise DataFetchError("AmazingData 查询被中断") from exc
        except Exception as exc:
            raise DataFetchError(f"AmazingData query_kline 失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise DataFetchError("AmazingData query_kline 返回类型无效")
        for code in batch:
            frame = raw.get(code)
            if frame is not None and not frame.empty:
                result[code] = frame
    return result


def fetch_kline(codes, begin_date, end_date):
    """策略批量接口：6 位代码→SDK 原始表，保持现有调用口径。"""
    return {_plain_code(code): frame for code, frame in
            get_raw_kline(_tgw_codes(codes), begin_date, end_date).items()}


def get_kline(code_list, begin_date, end_date, period=None, fields=None):
    """官方价格矩阵接口：日期×代码；多字段列为 (field, code)，价格未复权。"""
    raw = get_raw_kline(code_list, begin_date, end_date, period)
    fields = ["open", "high", "low", "close", "volume", "amount"] if fields is None else fields
    matrices = {}
    for field in fields:
        series = {}
        for code, frame in raw.items():
            if field not in frame.columns:
                continue
            if "kline_time" not in frame.columns:
                raise DataFetchError("AmazingData K线缺少 kline_time")
            values = frame.set_index("kline_time")[field].copy()
            values.index = pd.DatetimeIndex(values.index)
            if values.index.hasnans or not values.index.is_unique:
                raise DataFetchError(f"AmazingData {code} K线日期无效或重复")
            series[code] = values.sort_index()
        if series:
            matrices[field] = pd.DataFrame(series)
    if not matrices:
        return pd.DataFrame()
    if len(matrices) == 1:
        return next(iter(matrices.values()))
    return pd.concat(matrices, axis=1, names=["field", "code"])


def get_close_price(code_list, begin_date, end_date):
    return get_kline(code_list, begin_date, end_date, fields=["close"])


def get_benchmark(benchmark_code="000300.SH", begin_date=None, end_date=None):
    if begin_date is None or end_date is None:
        from .basics import get_calendar
        calendar = get_calendar(data_type="int")
        begin_date = calendar[0] if begin_date is None else begin_date
        end_date = calendar[-1] if end_date is None else end_date
    close = get_close_price([benchmark_code], begin_date, end_date)
    if not close.empty:
        close = close.rename(columns={benchmark_code: "close"})
        close.index.name = "date"
    return close

def fetch_execution_data(codes: List[str], begin: pd.Timestamp, end: pd.Timestamp):
    """当天证券信息和短窗口行情；不查次日才发布的历史信息表。"""
    if not codes:
        return pd.DataFrame(), {}
    try:
        base = client.get_base_data()
        info = base.get_code_info(security_type='EXTRA_STOCK_A_SH_SZ')
        day = int(end.strftime('%Y%m%d'))

        def clock_value(stamp):
            return int(stamp.strftime('%H%M%S')) * 1000 + stamp.microsecond // 1000

        frames = {}
        for batch in _chunks(_tgw_codes(codes), _INFO_CHUNK):
            raw = client.get_market_data().query_snapshot(
                batch, begin_date=day, end_date=day,
                begin_time=clock_value(begin), end_time=clock_value(end))
            frames.update(_normalize_map(raw, batch))
        return info, frames
    except Exception as exc:
        raise DataFetchError(f'AmazingData 当天执行状态查询失败: {exc}') from exc


def fetch_adj_factors(codes: List[str], begin_date: str,
                      end_date: str) -> pd.DataFrame:
    """get_adj_factor 单次复权因子：index=交易日期，column=TGW 代码。

    仅作比例调整用（收益率/相对强弱对序列常数缩放不敏感），无需归一化。
    """
    base = client.get_base_data()
    os.makedirs(AMAZINGDATA_CACHE_DIR, exist_ok=True)
    frames = []
    for batch in _chunks(_tgw_codes(codes), _FACTOR_CHUNK):
        try:
            raw = base.get_adj_factor(
                batch, local_path=AMAZINGDATA_CACHE_DIR, is_local=False)
        except Exception as e:
            raise DataFetchError(f"AmazingData get_adj_factor 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            raw = raw.copy(deep=False)
            raw.index = pd.to_datetime(raw.index).normalize()
            # _factor_series 对齐窗口；保留窗口起点之前一条供向前填充。
            before = raw.loc[raw.index < pd.Timestamp(begin_date)].tail(1)
            window = raw.loc[(raw.index >= pd.Timestamp(begin_date)) &
                             (raw.index <= pd.Timestamp(end_date))]
            frames.append(pd.concat([before, window]).copy())
        del raw
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def fetch_etf_hfq_bars(codes: List[str], end_date: str) -> Dict[str, pd.DataFrame]:
    """ETF 供应商后复权 OHLC（date/hfq_high/hfq_low/hfq_close），供通道观察。

    get_adj_factor 给的是事件当日乘数（除权日 1.98，次日回到 1.0），必须按日
    累积相乘再乘原始 OHLC；直接乘当日因子会把除权日算成暴跌。
    缺行情、缺因子或复权价异常的代码不返回，由调用方按失败处理（不补旧价）。
    """
    begin = (pd.Timestamp(end_date) - pd.DateOffset(years=3)).strftime('%Y-%m-%d')
    klines = fetch_kline(codes, begin, end_date)
    factors = fetch_adj_factors(codes, begin, end_date)
    factor_columns = {_plain_code(column): column for column in factors.columns}
    bars: Dict[str, pd.DataFrame] = {}
    for code, raw in klines.items():
        column = factor_columns.get(code)
        if column is None:
            continue
        dates = pd.to_datetime(raw['kline_time'], errors='coerce').dt.normalize()
        if dates.isna().any() or dates.duplicated().any():
            continue
        prices = raw[['high', 'low', 'close']].apply(pd.to_numeric, errors='coerce')
        prices.index = pd.DatetimeIndex(dates)
        prices = prices.sort_index()
        factor = factors[column].reindex(prices.index).ffill()
        if factor.isna().any():
            continue
        adjusted = prices.mul(factor.cumprod(), axis=0)
        if (adjusted.isna().any().any() or (adjusted <= 0).any().any()
                or (adjusted.high < adjusted.low).any()):
            continue
        out = adjusted.rename(columns=lambda column: f'hfq_{column}')
        out.insert(0, 'date', prices.index.strftime('%Y-%m-%d'))
        bars[code] = out.reset_index(drop=True)
    return bars
