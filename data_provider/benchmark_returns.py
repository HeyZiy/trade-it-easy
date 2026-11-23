"""账户基准的成分日收益，以 A 股收盘日对齐。"""

from datetime import date, timedelta
from itertools import pairwise
import logging
import math

import requests

from data_provider.fund_nav import fund_daily_returns

logger = logging.getLogger(__name__)
CSI_HISTORY = "https://www.csindex.com.cn/csindex-home/perf/index-perf"


def _index_levels(code, start, end):
    response = requests.get(CSI_HISTORY, params={"indexCode": code,
                            "startDate": start.replace("-", ""), "endDate": end.replace("-", "")},
                            timeout=(3, 8))
    response.raise_for_status()
    result = response.json()
    if result["code"] != "200":
        raise ValueError(f"中证指数查询失败：{result.get('msg')}")
    return {date.fromisoformat(row["tradeDate"]).isoformat(): float(row["close"])
            for row in result["data"] if row["indexCode"] == code and row["close"] is not None}


def _index_returns(code, sessions):
    levels = _index_levels(code, sessions[0], sessions[-1])
    return {day: levels[day] / levels[previous] - 1 for previous, day in pairwise(sessions)
            if all(math.isfinite(levels.get(key, 0)) and levels.get(key, 0) > 0 for key in (previous, day))}


def _money_returns(code, sessions):
    values = fund_daily_returns(code, sessions[0], sessions[-1], money_fund=True)
    result = {}
    for previous, day in pairwise(sessions):
        start, end = date.fromisoformat(previous), date.fromisoformat(day)
        daily = [values.get((start + timedelta(days=step)).isoformat())
                 for step in range(1, (end - start).days + 1)]
        if None not in daily:
            result[day] = math.prod(1 + value for value in daily) - 1
    return result


def _fund_returns(code, sessions):
    if code == "001821":
        return _money_returns(code, sessions)
    values = fund_daily_returns(code, sessions[0], sessions[-1])
    if code == "513500":
        # 海外净值含同日晚间美股收盘。下一 A 股估值日才使用，待公布时留空。
        return {day: values[previous] for previous, day in pairwise(sessions) if previous in values}
    return {day: values[day] for day in sessions[1:] if day in values}


def benchmark_returns(codes, sessions):
    """每个成分在整个请求区间取一次，缺数据留空供下次回补。"""
    results = {}
    for code in codes:
        try:
            load = _index_returns if code.startswith("H") else _fund_returns
            results[code] = load(code, sessions)
        except Exception as error:
            logger.warning("基准成分 %s 收益未更新：%s", code, error)
    return results
