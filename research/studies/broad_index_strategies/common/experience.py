"""一级、二级共享的持有体验指标：回撤深度、持续时间及相对长拿的取舍。"""
import numpy as np
import pandas as pd


DEEP_DRAWDOWN = .20
TOLERANCE = 1e-10
SUMMARY_COLS = [
    "窗口数", "跑赢长拿比例%", "超额年化中位数pp", "超额年化P10pp",
    "超额年化P90pp", "最差窗口回撤%", "回撤改善中位数pp",
    "回撤改善比例%", "水下缩短比例%", "收益回撤均改善比例%",
    "收益降低但回撤改善比例%", "收益增加但回撤恶化比例%",
    "收益回撤均恶化比例%", "三项均不差比例%",
    "回撤改善时年化差中位数pp", "水下缩短交易日中位数",
    "最长水下交易日P90", "期末未修复比例%", "深回撤时间占比中位数%",
    "落后长拿时间占比中位数%",
]


def _validate_nav(nav):
    if (nav.empty or not isinstance(nav.index, pd.DatetimeIndex)
            or not nav.index.is_monotonic_increasing or not nav.index.is_unique
            or nav.index.hasnans):
        raise ValueError("净值须有非空、唯一、升序的交易日索引")
    if not np.isfinite(nav.to_numpy(dtype=float)).all() or (nav <= 0).any():
        raise ValueError("净值须为有限正数，缺失估值不能跳过")


def _runs(mask):
    """返回连续True段的首尾位置（包含端点）。"""
    padded = np.r_[False, mask, False].astype(int)
    changes = np.diff(padded)
    return np.flatnonzero(changes == 1), np.flatnonzero(changes == -1) - 1


def _drawdown_state(nav, initial):
    _validate_nav(nav)
    if not np.isfinite(initial) or initial <= 0:
        raise ValueError("期初资金须为有限正数")
    values = nav.to_numpy(dtype=float)
    peaks = np.maximum.accumulate(np.r_[initial, values])[1:]
    drawdown = values / peaks - 1
    starts, ends = _runs(drawdown < -TOLERANCE)
    # 日历跨度从前高日算到恢复日；未恢复则截至样本末日。
    peak_positions = np.maximum(starts - 1, 0)
    observed_ends = np.minimum(ends + 1, len(nav) - 1)
    days = (nav.index[observed_ends] - nav.index[peak_positions]).days.to_numpy()
    return drawdown, starts, ends, days


def drawdown_metrics(nav, initial, deep_drawdown=DEEP_DRAWDOWN):
    if not 0 < deep_drawdown < 1:
        raise ValueError("深回撤阈值须介于0和1之间")
    dd, starts, ends, days = _drawdown_state(nav, initial)
    lengths = ends - starts + 1
    unfinished = bool(len(ends) and ends[-1] == len(nav) - 1)
    return {
        "最长水下交易日": int(lengths.max()) if len(lengths) else 0,
        "最长水下日历天": int(days.max()) if len(days) else 0,
        "当前水下交易日": int(lengths[-1]) if unfinished else 0,
        "当前水下日历天": int(days[-1]) if unfinished else 0,
        "期末未修复": int(unfinished),
        "期末回撤%": dd[-1] * 100,
        "水下时间占比%": float(np.mean(dd < -TOLERANCE) * 100),
        "深回撤时间占比%": float(np.mean(dd <= -deep_drawdown + TOLERANCE) * 100),
    }


def drawdown_periods(nav, initial):
    """逐段列出前高、谷底及恢复日；未修复段恢复日留空，持续时间为已观察下界。"""
    dd, starts, ends, days = _drawdown_state(nav, initial)
    columns = ["前高日", "水下开始日", "谷底日", "恢复日", "观察截止日",
               "水下交易日", "跨度日历天", "段最大回撤%", "已修复", "前高为期初资金"]
    rows = []
    for start, end, duration in zip(starts, ends, days):
        recovered = end + 1 < len(nav)
        stop = end + 1 if recovered else end
        trough = start + int(np.argmin(dd[start:end + 1]))
        rows.append([nav.index[max(start - 1, 0)], nav.index[start], nav.index[trough],
                     nav.index[stop] if recovered else pd.NaT, nav.index[stop],
                     int(end - start + 1), int(duration), float(dd[trough] * 100),
                     recovered, start == 0])
    return pd.DataFrame(rows, columns=columns)


def relative_metrics(nav, benchmark):
    _validate_nav(nav)
    _validate_nav(benchmark)
    if not nav.index.equals(benchmark.index):
        raise ValueError("策略与长拿必须使用完全相同的交易日")
    relative = nav.to_numpy() / benchmark.to_numpy() - 1
    behind = relative < -TOLERANCE
    starts, ends = _runs(behind)
    return {
        "落后长拿时间占比%": float(behind.mean() * 100),
        "最长连续落后长拿交易日": int((ends - starts + 1).max()) if len(starts) else 0,
        "最大相对落后%": float(min(0., relative.min()) * 100),
        "期末相对长拿差%": float(relative[-1] * 100),
    }


def metric_differences(metrics, hold):
    return {
        "较长拿年化差pp": metrics["年化收益%"] - hold["年化收益%"],
        "较长拿回撤改善pp": metrics["最大回撤%"] - hold["最大回撤%"],
        "较长拿水下缩短交易日": hold["最长水下交易日"] - metrics["最长水下交易日"],
        "较长拿水下缩短日历天": hold["最长水下日历天"] - metrics["最长水下日历天"],
        "较长拿深回撤时间减少pp": hold["深回撤时间占比%"] - metrics["深回撤时间占比%"],
    }


def add_benchmark_comparison(metrics, curves):
    rows = {name: {**metric_differences(row, metrics.loc["长拿"]),
                   **relative_metrics(curves[name].nav, curves["长拿"].nav)}
            for name, row in metrics.iterrows()}
    return metrics.join(pd.DataFrame.from_dict(rows, orient="index"))


def rolling_windows(index, years):
    if not isinstance(years, int) or years < 1:
        raise ValueError("持有年数须为正整数")
    starts = index.to_series().groupby(index.to_period("M")).min()
    for start in starts:
        end = start + pd.DateOffset(years=years)
        if end <= index[-1]:
            yield start, end


def cohort_row(name, px, years, metrics, hold, curve, hold_curve):
    return {"strategy": name, "start": px.index[0], "end": px.index[-1], "years": years,
            **metrics, **{f"长拿{k}": hold[k] for k in
                          ("年化收益%", "最大回撤%", "最长水下交易日", "最长水下日历天")},
            **metric_differences(metrics, hold), **relative_metrics(curve.nav, hold_curve.nav)}


def rolling_summary(cohorts):
    if cohorts.empty:
        return pd.DataFrame(columns=SUMMARY_COLS).rename_axis("strategy")
    rows = {}
    for name, group in cohorts.groupby("strategy", sort=False):
        excess = group["较长拿年化差pp"]
        dd = group["较长拿回撤改善pp"]
        water = group["较长拿水下缩短交易日"]
        up, down = excess > TOLERANCE, excess < -TOLERANCE
        better, worse = dd > TOLERANCE, dd < -TOLERANCE
        pct = lambda mask: float(mask.mean() * 100)
        rows[name] = {
            "窗口数": len(group), "跑赢长拿比例%": pct(up),
            "超额年化中位数pp": excess.median(), "超额年化P10pp": excess.quantile(.1),
            "超额年化P90pp": excess.quantile(.9), "最差窗口回撤%": group["最大回撤%"].min(),
            "回撤改善中位数pp": dd.median(), "回撤改善比例%": pct(better),
            "水下缩短比例%": pct(water > 0), "收益回撤均改善比例%": pct(up & better),
            "收益降低但回撤改善比例%": pct(down & better),
            "收益增加但回撤恶化比例%": pct(up & worse), "收益回撤均恶化比例%": pct(down & worse),
            "三项均不差比例%": pct((excess >= -TOLERANCE) & (dd >= -TOLERANCE) & (water >= 0)),
            "回撤改善时年化差中位数pp": excess[better].median(),
            "水下缩短交易日中位数": water.median(),
            "最长水下交易日P90": group["最长水下交易日"].quantile(.9),
            "期末未修复比例%": pct(group["期末未修复"] > 0),
            "深回撤时间占比中位数%": group["深回撤时间占比%"].median(),
            "落后长拿时间占比中位数%": group["落后长拿时间占比%"].median(),
        }
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("strategy")[SUMMARY_COLS]
