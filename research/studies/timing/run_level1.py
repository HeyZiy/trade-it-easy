"""统一比较长期配置的收益、回撤及水下时间；只读取现有缓存。

从仓库根运行 python research/studies/broad_index_strategies/run_level1.py。
全期与每月重新建账的1/3/5年窗口均使用相同规则、成本和初始资金。
"""
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "research.studies.broad_index_strategies"

from .common import atr_channel as atr

dc = atr.dc
base = atr.base
experience = base.experience
OUT = base.STUDY / "results" / "level1"
ATR_CASE = "MA20_ATR20×1.5_下轨退出"


def strategy_runners(history, pe=None):
    targets = {"月末MA200": base.build_trend_target(history),
               "唐奇安20/10": dc.build_target(history, 20, 10),
               "唐奇安55/20": dc.build_target(history, 55, 20),
               ATR_CASE: dc.targets_from_signals(atr.channel_signals(history, *atr.CASES[ATR_CASE]))}
    if pe is not None:
        # 保留每一个明确的估值决定，确保新窗口首日不会漏掉此前已经生效的分位。
        # 分位处于中间区间仍无目标，维持新账户空仓的原规则。
        targets["估值"] = base.build_val_target(history, pe, changes_only=False)
    return {"长拿": base.sim_buyhold, "网格": base.sim_grid,
            **{name: partial(base.sim_target, target=target) for name, target in targets.items()}}


def evaluate_window(px, runners):
    curves, rows = {}, {}
    for name, run in runners.items():
        curve, count = run(px)
        curves[name] = curve
        rows[name] = base.metrics(curve.nav, curve.exposure, count)
    met = pd.DataFrame.from_dict(rows, orient="index").rename_axis("strategy")
    return experience.add_benchmark_comparison(met, curves), curves


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


def write_summary(metrics, rolling):
    lines = ["# 持有体验对照", "", "数据截至2026-09-30；各标的全期窗口不同，不按跨标的收益排名。",
             "最长水下时间统计连续低于此前净值高点的交易日；回到前高即结束。",
             "期末未恢复的区间计入持续时间，其最终恢复时间尚未知。", ""]
    focus = ["年化收益%", "最大回撤%", "最长水下交易日", "期末未修复", "深回撤时间占比%"]
    for (code, name), group in metrics.groupby(["code", "标的"], sort=False):
        lines += [f"## {name}（{code}）", "",
                  f"窗口：{group.start.iloc[0]}至{group.end.iloc[0]}。", "",
                  group.set_index("strategy")[focus].round(2).to_markdown(), ""]
        three = rolling[(rolling.code == code) & (rolling.years == 3)]
        if not three.empty:
            cols = ["窗口数", "超额年化中位数pp", "回撤改善中位数pp", "水下缩短交易日中位数",
                    "收益回撤均改善比例%", "三项均不差比例%"]
            lines += ["每月新建账户、固定3年持有期（窗口重叠，不是独立样本）：", "",
                      three.set_index("strategy")[cols].round(2).to_markdown(), ""]
    lines += ["1/3/5年完整结果见rolling_summary.csv；逐次窗口见rolling_cohorts.csv。",
              "深回撤采用20%描述阈值，不作为策略筛选条件；未做同仓位对照或总分排序。", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    full, cohorts, summaries, windows = [], [], [], {}
    files = {Path(module.__file__) for module in (base, dc, atr, experience, base.asset_config)}
    files.add(Path(__file__))
    for asset in base.ASSETS:
        code = asset.etf_code
        history = base.load_etf(code)
        pe = base.load_pe(asset.index_code) if asset.valuation is not None else None
        runners = strategy_runners(history, pe)
        px = history.loc[base.window_start(history):]
        met, curves = evaluate_window(px, runners)
        start, end = str(px.index[0].date()), str(px.index[-1].date())
        windows[code] = {"start": start, "end": end, "strategies": list(runners)}
        full.append(met.reset_index().assign(code=code, 标的=asset.name, start=start, end=end))
        pd.concat(curves, names=["strategy", "date"]).reset_index().to_csv(
            OUT / f"daily_{code}.csv", index=False, encoding="utf-8-sig")
        pd.concat({name: experience.drawdown_periods(curve.nav, base.CAPITAL)
                   for name, curve in curves.items()}, names=["strategy", "episode"]).reset_index().to_csv(
            OUT / f"drawdowns_{code}.csv", index=False, encoding="utf-8-sig")
        for years in (1, 3, 5):
            rows = rolling_cohorts(history, runners, years)
            cohorts.append(rows.assign(code=code, 标的=asset.name))
            summaries.append(experience.rolling_summary(rows).reset_index().assign(
                code=code, 标的=asset.name, years=years))
        files.add(base.PE_CACHE / f"etf_hfq_{code}.csv")
        if pe is not None:
            files.add(base.PE_CACHE / f"pe_{asset.index_code}.csv")
        print(f"{asset.name} 全期及1/3/5年多起点完成", flush=True)
    metrics = pd.concat(full, ignore_index=True)
    rolling = pd.concat(summaries, ignore_index=True)
    metrics.round(6).to_csv(OUT / "metrics_all.csv", index=False, encoding="utf-8-sig")
    pd.concat(cohorts, ignore_index=True).round(6).to_csv(
        OUT / "rolling_cohorts.csv", index=False, encoding="utf-8-sig")
    rolling.round(6).to_csv(OUT / "rolling_summary.csv", index=False, encoding="utf-8-sig")
    write_summary(metrics, rolling)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},
        "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT, "ann": base.ANN,
        "cutoff": "2026-09-30", "deep_drawdown": experience.DEEP_DRAWDOWN,
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
