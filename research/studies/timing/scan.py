"""统一比较长期配置的收益、回撤及水下时间；只读取现有缓存。

从仓库根运行 python -m research.studies.timing.scan。
规则集复用网页大表的 26 条规则行（按族分组，PE 分位取 20/80，放量突破需成交量），
全部标的同起点同窗口：第 272 个有效收盘（RV20 的 252 日分位预热完成）起至 2026-09-30。
全期与每月重新建账的1/3/5年窗口均使用相同规则、成本和初始资金。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "research.studies.timing"

from . import web_rules
from .common import atr_channel as atr
from .common import donchian as dc
from .common import experience
from .common import backtest as base

OUT = base.RESULTS / "level1"
ATR_CASE = "MA20+ATR×1.5"
VOLUME_DIR = base.DATA / "etf_daily_ad" / "etf_daily"
STAR = web_rules.STAR


def load_history(code):
    """后复权 OHLC 左连接东财日线成交量；缺成交量时仅少“放量突破”这一条规则。"""
    history = base.load_etf(code)
    path = VOLUME_DIR / f"{code}.csv"
    if not path.exists():
        return history
    volume = pd.read_csv(path, index_col="date", parse_dates=True, usecols=["date", "volume"])
    return history.join(volume, how="left")


def strategy_runners(history, pe=None):
    """复用网页大表规则集；PE 分位取 20/80，放量突破需成交量列。"""
    return web_rules.strategy_runners(history, pe)


def evaluate_window(px, runners, collect_ledgers=False):
    curves, rows, ledgers = {}, {}, {}
    for name, run in runners.items():
        ledger = [] if collect_ledgers else None
        curve, count = run(px, ledger=ledger)
        curves[name] = curve
        rows[name] = base.metrics(curve.nav, curve.exposure, count)
        if collect_ledgers:
            ledgers[name] = ledger
    met = pd.DataFrame.from_dict(rows, orient="index").rename_axis("strategy")
    if collect_ledgers:
        met = base.attach_round_trips(met, ledgers)
    return experience.add_benchmark_comparison(met, curves), curves, ledgers


def rolling_cohorts(history, runners, years):
    available = history.loc[base.window_start(history):]
    rows = []
    for start, end in experience.rolling_windows(available.index, years):
        px = available.loc[(available.index >= start) & (available.index < end)]
        curves, metrics = {}, {}
        for name, run in runners.items():
            curve, count = run(px)
            curves[name] = curve
            metrics[name] = base.metrics(curve.nav, curve.exposure, count)
        for name in runners:
            if name != "长拿":
                rows.append(experience.cohort_row(name, px, years, metrics[name], metrics["长拿"],
                                                  curves[name], curves["长拿"]))
    return pd.DataFrame(rows)


def family_medians(metrics):
    """族内跨标的、跨规则的中位数；仅描述分布，不据此排序挑规则。"""
    rows = {}
    for family, group in metrics.groupby("族", sort=False):
        rows[family] = {
            "规则数": group.strategy.nunique(),
            "标的数": group.code.nunique(),
            "年化收益中位数%": group["年化收益%"].median(),
            "最大回撤中位数%": group["最大回撤%"].median(),
            "平均仓位中位数%": group["平均仓位%"].median(),
            "较长拿年化差中位pp": group["较长拿年化差pp"].median(),
            "较长拿回撤改善中位pp": group["较长拿回撤改善pp"].median(),
            "跑赢长拿比例%": (group["较长拿年化差pp"] > 0).mean() * 100,
            "回撤改善比例%": (group["较长拿回撤改善pp"] > 0).mean() * 100,
        }
    return pd.DataFrame(rows).T.rename_axis("族")


def family_rolling_medians(rolling):
    """3年多起点的族内中位数；窗口在族内各规则、各标的上重叠，不作独立样本。"""
    three = rolling[rolling.years == 3]
    rows = {}
    for family, group in three.groupby("族", sort=False):
        rows[family] = {
            "规则数": group.strategy.nunique(),
            "窗口数中位": group["窗口数"].median(),
            "跑赢长拿比例中位%": group["跑赢长拿比例%"].median(),
            "超额年化中位pp": group["超额年化中位数pp"].median(),
            "回撤改善中位pp": group["回撤改善中位数pp"].median(),
            "收益回撤均改善比例中位%": group["收益回撤均改善比例%"].median(),
            "三项均不差比例中位%": group["三项均不差比例%"].median(),
            "水下缩短交易日中位": group["水下缩短交易日中位数"].median(),
        }
    return pd.DataFrame(rows).T.rename_axis("族")


def write_summary(metrics, rolling):
    lines = ["# 持有体验对照", "",
             f"数据截至2026-09-30；{metrics.code.nunique()}只ETF各自从第{base.WARMUP}个有效收盘起算，"
             "窗口长度不同，不按跨标的收益排名。",
             "规则按族展开，参数事先固定；信号收盘确认、下一交易日开盘执行，"
             "单边成本0.06%，按100股取整。",
             "最长水下时间统计连续低于此前净值高点的交易日；期末未修复区间计入持续时间。", "",
             "## 各族跨标的中位数", "",
             "同一族内每个标的规则数相同；比例列为族内全部（标的×规则）组合的占比。", "",
             family_medians(metrics).round(2).to_markdown(), ""]
    focus = ["年化收益%", "最大回撤%", "最长水下交易日", "平均仓位%", "期末未修复", "深回撤时间占比%"]
    for (code, name), group in metrics.groupby(["code", "标的"], sort=False):
        lines += [f"## {name}（{code}）", "",
                  f"窗口：{group.start.iloc[0]}至{group.end.iloc[0]}。", "",
                  group.set_index("strategy")[["族", *focus]].round(2).to_markdown(), ""]
        star = group[group.strategy.isin(STAR) & group.strategy.ne("长拿")]
        three = rolling[(rolling.code == code) & (rolling.years == 3)
                        & (rolling.strategy.isin(STAR))]
        if not three.empty:
            cols = ["窗口数", "超额年化中位数pp", "回撤改善中位数pp", "水下缩短交易日中位数",
                    "收益回撤均改善比例%", "三项均不差比例%"]
            star = star.set_index("strategy").join(three.set_index("strategy")[cols])
            lines += ["每月新建账户、固定3年持有期的代表规则"
                      "（窗口重叠，不是独立样本）：", "",
                      star[[*cols]].round(2).to_markdown(), ""]
    lines += ["## 固定3年多起点的族内中位数", ""]
    family_roll = family_rolling_medians(rolling)
    if family_roll.empty:
        lines += ["历史长度不足，没有完整3年窗口，结果留空。", ""]
    else:
        lines += [family_roll.round(2).to_markdown(), ""]
    lines += ["全部规则的1/3/5年结果见rolling_summary.csv与rolling_cohorts.csv。",
              "深回撤采用20%描述阈值，不作为策略筛选条件；未做同仓位对照或总分排序。", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    full, cohorts, summaries, windows = [], [], [], {}
    files = {Path(module.__file__) for module in
             (base, dc, atr, experience, web_rules, base.asset_config)}
    files.add(Path(__file__))
    for asset in base.ASSETS:
        code = asset.etf_code
        history = load_history(code)
        pe = base.load_pe(asset.index_code) if asset.valuation is not None else None
        runners = strategy_runners(history, pe)
        px = history.loc[base.window_start(history):]
        met, curves, ledgers = evaluate_window(px, runners, collect_ledgers=True)
        start, end = str(px.index[0].date()), str(px.index[-1].date())
        windows[code] = {"start": start, "end": end, "strategies": list(runners)}
        frame = met.reset_index()
        frame.insert(1, "族", frame["strategy"].map(web_rules.FAMILY))
        full.append(frame.assign(code=code, 标的=asset.name, start=start, end=end))
        pd.concat(curves, names=["strategy", "date"]).reset_index().to_csv(
            OUT / f"daily_{code}.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([{"strategy": name, **trade}
                      for name, ledger in ledgers.items() for trade in ledger]).to_csv(
            OUT / f"trades_{code}.csv", index=False, encoding="utf-8-sig")
        pd.concat({name: experience.drawdown_periods(curve.nav, base.CAPITAL)
                   for name, curve in curves.items()}, names=["strategy", "episode"]).reset_index().to_csv(
            OUT / f"drawdowns_{code}.csv", index=False, encoding="utf-8-sig")
        for years in (1, 3, 5):
            rows = rolling_cohorts(history, runners, years)
            cohorts.append(rows.assign(code=code, 标的=asset.name))
            summaries.append(experience.rolling_summary(rows).reset_index().assign(
                code=code, 标的=asset.name, years=years))
        files.add(base.ETF_DATA / f"etf_hfq_{code}.csv")
        if pe is not None:
            files.add(base.PE_DATA / f"pe_{asset.index_code}.csv")
        print(f"{asset.name} 全期及1/3/5年多起点完成", flush=True)
    metrics = pd.concat(full, ignore_index=True)
    rolling = pd.concat(summaries, ignore_index=True)
    rolling.insert(1, "族", rolling["strategy"].map(web_rules.FAMILY))
    metrics.round(6).to_csv(OUT / "metrics_all.csv", index=False, encoding="utf-8-sig")
    pd.concat(cohorts, ignore_index=True).round(6).to_csv(
        OUT / "rolling_cohorts.csv", index=False, encoding="utf-8-sig")
    rolling.round(6).to_csv(OUT / "rolling_summary.csv", index=False, encoding="utf-8-sig")
    write_summary(metrics, rolling)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},
        "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT, "ann": base.ANN,
        "cutoff": "2026-09-30", "warmup": base.WARMUP,
        "pe_quantiles": [web_rules.PE_LO, web_rules.PE_HI],
        "deep_drawdown": experience.DEEP_DRAWDOWN,
        "underwater": {"initial_capital_included": True, "recovery_includes_equality": True,
                       "recovery_day_excluded_from_underwater_sessions": True,
                       "unfinished_periods_included": True, "relative_tolerance": experience.TOLERANCE},
        "rolling": {"years": [1, 3, 5], "frequency": "monthly", "fresh_cash": True,
                    "overlapping_windows": True, "pre_window_history_for_indicators_only": True},
        "windows": windows, "versions": {"pandas": pd.__version__},
    }
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"结果：{OUT / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
