"""Shared account results for the ETF timing web reader."""

import logging
import re
import threading
from datetime import datetime, time, timedelta, timezone
from time import monotonic, sleep

import numpy as np
import pandas as pd

from . import scan
from . import web_rules

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
            "metrics": _records(reset.reset_index(), ("strategy", "族", *FULL_FIELDS)), "rolling": _rolling(rolling)}


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


def load_cached(code, cache):
    """Read one qualified AmazingData snapshot without loading the cross-asset tables."""
    from data_provider.etf import adjusted_etf_history
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
        return str(asset.display_name), adjusted_etf_history(rows, cutoff, with_volume=True), "AmazingData 后复权 · 本地研究快照"
    except (OSError, ValueError, KeyError, AttributeError):
        return None


def load_live(code):
    """Load historical adjusted OHLC; current listing status is not a requirement."""
    from data_provider.etf import EASTMONEY, etf_daily_bars
    from data_provider.security_names import security_name

    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.date()
    cutoff = today if now.time() >= time(15, 30) else today - timedelta(days=1)
    name = security_name(code)
    series = etf_daily_bars([code], cutoff.isoformat(), policy="online_research", with_volume=True).get(code)
    if series is None:
        raise ValueError("未获取到可用的后复权日线，请稍后重试。")
    source = SOURCE if series.source == EASTMONEY else "AmazingData 后复权 · 在线"
    return name, series.history, source


_PE_LOCK = threading.Lock()
_PE_LAST = [0.0]
_PE_MIN_INTERVAL = 2.0  # 乐咕相邻请求最小间隔（秒），避开反爬限速
_PE_RETRIES = 4


def _legulegu_pe_frame(index_code):
    """限速 + 429 退避重试 + 每次重取 csrf 地拉乐咕 PE；空数据视为覆盖缺口直接报错。"""
    import requests
    from akshare.stock_feature.stock_a_indicator import get_cookie_csrf
    from research.tools.etf_data.fetch_em import _legulegu_token, _pe_frame

    symbol = web_rules.index_symbol(index_code)
    url = "https://legulegu.com/api/stockdata/index-basic-pe"
    csrf_url = "https://legulegu.com/stockdata/sz50-ttm-lyr"
    last_error = "未知错误"
    for attempt in range(_PE_RETRIES):
        with _PE_LOCK:
            wait = _PE_MIN_INTERVAL - (monotonic() - _PE_LAST[0])
            if wait > 0:
                sleep(wait)
            _PE_LAST[0] = monotonic()
        try:
            response = requests.get(url, params={"token": _legulegu_token(), "indexCode": symbol},
                                    **get_cookie_csrf(url=csrf_url), timeout=30)
            if response.status_code == 429:
                last_error = "乐咕限速(429)"
                sleep(min(2 ** attempt * 2, 30))
                continue
            response.raise_for_status()
            data = response.json().get("data")
            if not data:
                raise ValueError(f"乐咕无指数 {index_code} 的 PE 数据，无法生成估值行。")
            return _pe_frame(pd.DataFrame(data), "date", "ttmPe")
        except ValueError:
            raise  # 覆盖缺口是真实结果，不重试
        except Exception as exc:
            last_error = str(exc)
            sleep(min(2 ** attempt, 8))
    raise ValueError(f"乐咕 PE 取数失败（{last_error}），请稍后重试。")


def load_pe(index_code, pe_dir, refresh=False):
    """按用户声明的指数代码从乐咕现取 PE 全历史，缓存于网页自有目录；取空即报错。

    不读研究侧 index_pe_lg 快照、不依赖标的配置；能否生成估值行由上游响应决定。
    """
    if not isinstance(index_code, str) or re.fullmatch(r"[0-9]{6}", index_code) is None:
        raise ValueError("指数代码须为 6 位数字。")
    path = pe_dir / f"pe_{index_code}.csv"
    if not refresh and path.is_file():
        cached = pd.read_csv(path)
        if not cached.empty:
            pe = pd.Series(pd.to_numeric(cached["pe"]).to_numpy(),
                           index=pd.to_datetime(cached["date"])).sort_index()
            return pe, str(cached["date"].iloc[-1])
    frame = _legulegu_pe_frame(index_code)
    if frame.empty:
        raise ValueError(f"指数 {index_code} 的 PE 数据为空。")
    pe_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    pe = pd.Series(frame["pe"].to_numpy(), index=pd.to_datetime(frame["date"])).sort_index()
    return pe, str(frame["date"].iloc[-1])


def full_report(history, source, pe=None, index_code=None, pe_as_of=None):
    start = web_rules.window_start(history)
    window = history.loc[start:]
    if len(window) < 2:
        raise ValueError("预热后至少需要两个交易日，当前行情不足。")
    runners = web_rules.strategy_runners(history, pe=pe)
    metrics, curves, _ = scan.evaluate_window(window, runners)
    metrics = metrics.reset_index()
    metrics["族"] = metrics["strategy"].map(web_rules.FAMILY)
    group = _group(metrics, curves, pd.DataFrame())
    group["source"] = source
    group["index_code"] = index_code
    group["pe_as_of"] = pe_as_of
    return group, runners


def rolling_report(history, runners) -> list[dict]:
    star = {name: runners[name] for name in web_rules.STAR if name in runners}
    rows = []
    for years in PERIODS:
        cohorts = web_rules.rolling_cohorts(history, star, years)
        summary = scan.experience.rolling_summary(cohorts)
        rows.extend(_rolling(summary.reset_index().assign(years=years)))
    return rows
