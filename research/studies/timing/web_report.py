"""Shared account results for the ETF timing web reader."""

import logging
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache

import numpy as np
import pandas as pd

from . import scan

logger = logging.getLogger(__name__)
SOURCE = "东方财富后复权日线"
PERIODS = (1, 3, 5)
FULL_FIELDS = ("年化收益%", "最大回撤%", "最长水下交易日", "平均仓位%", "交易次数",
               "较长拿年化差pp", "较长拿回撤改善pp", "较长拿水下缩短交易日")
ROLL_FIELDS = ("窗口数", "超额年化中位数pp", "回撤改善中位数pp", "水下缩短交易日中位数",
               "跑赢长拿比例%", "三项均不差比例%")
ARCHIVE_GROUPS = {
    "archive": "原研究 · 代表规则",
    "detail": "恒生科技 · 固定参数对照",
    "cycle": "Cycle · 量价共同覆盖期",
    "cycle-peer": "Cycle · 恒生科技同期",
}


def _records(frame, fields):
    selected = frame[[field for field in fields if field in frame]]
    return selected.astype(object).where(selected.notna(), None).to_dict("records")


def _rolling(frame):
    return _records(frame, ("strategy", "years", *ROLL_FIELDS))


def _group(metrics, frames, rolling):
    if "长拿" not in frames:
        raise ValueError("报告缺少同窗口长拿基准")
    benchmark = frames["长拿"].index
    if (benchmark.empty or not benchmark.is_unique or not benchmark.is_monotonic_increasing
            or metrics.strategy.duplicated().any() or set(metrics.strategy) != frames.keys()
            or any(not frame.index.equals(benchmark) for frame in frames.values())):
        raise ValueError("指标与长拿曲线不在同一交易日窗口")
    dates = benchmark.strftime("%Y-%m-%d").tolist()
    for field, value in (("start", dates[0]), ("end", dates[-1])):
        if field in metrics and not metrics[field].eq(value).all():
            raise ValueError("指标日期与曲线不一致")
    reset = metrics.set_index("strategy")
    for field in FULL_FIELDS[-3:]:
        if field not in reset:
            reset[field] = [scan.experience.metric_differences(row, reset.loc["长拿"])[field]
                            for _, row in reset.iterrows()]
    series = {}
    for name, frame in frames.items():
        nav = frame.nav.to_numpy(dtype=float) / scan.base.CAPITAL
        exposure = frame.exposure.to_numpy(dtype=float) * 100
        if not (np.isfinite(nav).all() and (nav > 0).all() and np.isfinite(exposure).all()):
            raise ValueError("账户曲线包含无效净值或仓位")
        drawdown = (nav / np.maximum.accumulate(np.maximum(nav, 1)) - 1) * 100
        series[name] = [nav.round(6).tolist(), drawdown.round(3).tolist(), exposure.round(2).tolist()]
    return {"start": dates[0], "end": dates[-1], "dates": dates, "series": series,
            "cost_pct": scan.base.COST * 100,
            "metrics": _records(reset.reset_index(), ("strategy", *FULL_FIELDS)), "rolling": _rolling(rolling)}


def _read(path, code=None):
    if not path.is_file():
        return pd.DataFrame()
    frame = pd.read_csv(path, dtype={"code": str})
    return frame[frame.code == code] if code is not None else frame


def archive_catalog(archive):
    folder = archive / "level1"
    metrics = _read(folder / "metrics_all.csv")
    if metrics.empty:
        return {}
    return {row["code"]: {"code": row["code"], "name": row["标的"], "end": row["end"]}
            for row in metrics[metrics.strategy == "长拿"].to_dict("records")
            if (folder / f'daily_{row["code"]}.csv').is_file()}


def archive_groups(archive, code, known):
    groups = {}
    if known:
        groups["archive"] = ARCHIVE_GROUPS["archive"]
    detail = archive / code
    if code == "513180" and all((detail / p).is_file() for p in ("daily.csv", "metrics.csv")):
        groups["detail"] = ARCHIVE_GROUPS["detail"]
    cycle = archive / "cycle"
    for key, period in (("cycle", "全期"), ("cycle-peer", "恒科同期")):
        if (cycle / "metrics.csv").is_file() and (cycle / f"{code}_{period}" / "daily.csv").is_file():
            groups[key] = ARCHIVE_GROUPS[key]
    return groups


def archive_group(archive, code, key):
    if key == "archive":
        folder = archive / "level1"
        metrics = _read(folder / "metrics_all.csv", code)
        rolling = _read(folder / "rolling_summary.csv", code)
        daily = folder / f"daily_{code}.csv"
    elif key == "detail":
        folder = archive / code
        metrics = _read(folder / "metrics.csv")
        rolling = _read(folder / "rolling_summary.csv")
        daily = folder / "daily.csv"
    else:
        folder = archive / "cycle"
        period = "全期" if key == "cycle" else "恒科同期"
        metrics = _read(folder / "metrics.csv", code).query("period == @period")
        rolling = (_read(folder / "rolling_summary.csv", code).assign(years=3)
                   if key == "cycle" else pd.DataFrame())
        daily = folder / f"{code}_{period}" / "daily.csv"
    raw = pd.read_csv(daily, parse_dates=["date"], usecols=["strategy", "date", "nav", "exposure"])
    frames = {name: rows.set_index("date").sort_index() for name, rows in raw.groupby("strategy", sort=False)}
    group = _group(metrics, frames, rolling)
    group["source"] = SOURCE + (" + AmazingData 成交量" if key.startswith("cycle") else "")
    return group


@lru_cache(maxsize=1)
def _listed_etfs(day):
    from data_provider.bars import get_etf_universe

    listed = get_etf_universe()
    if listed is None or listed.empty:
        raise ValueError("ETF 名单暂不可用，请稍后重试。")
    return dict(zip(listed.code, listed.name))


def _price_history(rows, cutoff):
    history = rows.set_index(pd.to_datetime(rows["date"], errors="raise"))
    fields = ["open", "high", "low", "close"]
    history = history[[f"hfq_{field}" for field in fields]].set_axis(fields, axis=1).sort_index()
    history = history.loc[:cutoff].apply(pd.to_numeric, errors="coerce")
    if (history.empty or not history.index.is_unique or history.index.hasnans
            or not np.isfinite(history.to_numpy()).all() or (history <= 0).any().any()
            or (history.high < history.max(axis=1)).any() or (history.low > history.min(axis=1)).any()):
        raise ValueError("复权行情有缺失或价格异常，无法生成可靠的报告。")
    return history


def load_cached(code, cache):
    """Read one qualified AmazingData snapshot without loading the cross-asset tables."""
    from research.tools.etf_data.cache import AD_SOURCE, cache_is_fresh

    path = cache / "etf_daily" / f"{code}.csv"
    if not path.is_file():
        return None
    try:
        security = _read(cache / "etf_securities.csv", code)
        if len(security) != 1:
            return None
        asset = security.iloc[0]
        cutoff = asset.last_bar_date
        if (not asset.current_listed or asset.source != AD_SOURCE
                or not cache_is_fresh(path, cutoff)):
            return None
        rows = pd.read_csv(path)
        if not rows.quality.eq("ok").all():
            return None
        return str(asset.display_name), _price_history(rows, cutoff), "AmazingData 后复权 · 本地研究快照"
    except (OSError, ValueError, KeyError, AttributeError):
        return None


def load_live(code):
    """Verify a listed ETF, then read provider-adjusted OHLC without modifying the research cache."""
    from data_provider.bars import get_etf_daily_em
    from data_provider.fetchers.amazingdata import market
    from data_provider.fetchers.amazingdata.client import is_configured

    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.date()
    cutoff = today if now.time() >= time(15, 30) else today - timedelta(days=1)
    name = _listed_etfs(today).get(code)
    if name is None:
        raise ValueError("在当前沪深上市 ETF 名单中未找到该代码。")
    end = cutoff.isoformat()
    try:
        return name, _price_history(get_etf_daily_em(code, end), end), SOURCE
    except Exception as error:
        logger.warning("ETF %s 东方财富后复权失败 (%s)", code, type(error).__name__)
        if not is_configured():
            raise ValueError("东方财富后复权日线不可用，当前工作台未配置 AmazingData 备用来源。") from error
    try:
        rows = market.fetch_etf_hfq_bars([code], end, begin_date="1990-01-01")[code]
        return name, _price_history(rows, end), "AmazingData 后复权 · 在线"
    except Exception as error:
        logger.warning("ETF %s AmazingData 后复权失败 (%s)", code, type(error).__name__)
        raise ValueError("东方财富和 AmazingData 均未返回可用的后复权日线，请稍后重试。") from error


def full_report(history, source):
    start = scan.base.window_start(history)
    window = history.loc[start:]
    if len(window) < 2:
        raise ValueError("MA200 预热后至少需要两个交易日，当前行情不足。")
    runners = scan.strategy_runners(history)
    metrics, curves, _ = scan.evaluate_window(window, runners)
    return {**_group(metrics.reset_index(), curves, pd.DataFrame()), "source": source}, runners


def rolling_report(history, runners) -> list[dict]:
    rows = []
    for years in PERIODS:
        cohorts = scan.rolling_cohorts(history, runners, years)
        summary = scan.experience.rolling_summary(cohorts)
        rows.extend(_rolling(summary.reset_index().assign(years=years)))
    return rows
