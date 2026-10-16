"""二级测试：单标的、固定代表参数组；目前仅运行恒生科技513180。

python research/studies/broad_index_strategies/run_level2.py
不扫描参数网格，不按结果选参数，不修改一级规则。
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

from . import run_level1 as holding

base, dc, atr = holding.base, holding.dc, holding.atr
ASSET_CODE = "513180"
OUT = base.STUDY / "results" / "level2" / ASSET_CODE

# 一次列明固定规则，不从测试结果动态生成候选或推荐参数。
# 唐奇安保留20/10、55/20两组代表规则，20/20仅作退出周期对照。
# 日频均线只改变周期；ATR主组只改变通道宽度，ATR14是周期对照。
PRESETS = {
    "长拿": ("buyhold", {}),
    "网格": ("grid", {}),
    "月末MA200": ("monthly_ma", {"window": 200}),
    "唐奇安20/10": ("donchian", {"entry": 20, "exit": 10}),
    "唐奇安55/20": ("donchian", {"entry": 55, "exit": 20}),
    "唐奇安20/20": ("donchian", {"entry": 20, "exit": 20}),
    "日频MA20": ("daily_ma", {"window": 20}),
    "日频MA60": ("daily_ma", {"window": 60}),
    "日频MA120": ("daily_ma", {"window": 120}),
    "日频MA200": ("daily_ma", {"window": 200}),
    "MA20_ATR20×1.0_下轨退出": ("atr", {"ma_period": 20, "atr_period": 20, "multiple": 1.}),
    "MA20_ATR20×1.5_下轨退出": ("atr", {"ma_period": 20, "atr_period": 20, "multiple": 1.5}),
    "MA20_ATR20×2.0_下轨退出": ("atr", {"ma_period": 20, "atr_period": 20, "multiple": 2.}),
    "MA20_ATR14×1.5_下轨退出": ("atr", {"ma_period": 20, "atr_period": 14, "multiple": 1.5}),
}
HOLDING_YEARS = (1, 3, 5)


def build_runners(history):
    runners = {}
    for name, (family, params) in PRESETS.items():
        if family == "buyhold":
            runners[name] = base.sim_buyhold
            continue
        if family == "grid":
            runners[name] = base.sim_grid
            continue
        if family == "monthly_ma":
            target = base.build_trend_target(history, **params)
        elif family == "daily_ma":
            target = dc.daily_ma_target(history, **params)
        elif family == "donchian":
            target = dc.build_target(history, **params)
        elif family == "atr":
            target = dc.targets_from_signals(atr.channel_signals(history, **params))
        else:
            raise ValueError(f"未知规则类型：{family}")
        runners[name] = partial(base.sim_target, target=target)
    return runners


def write_report(asset, met, rolling, window):
    lines = [f"# 二级测试：{asset.name}", "",
             f"窗口：{window['start']}至{window['end']}。固定{len(PRESETS)}条规则，只作并列对照，不按结果挑选最优参数。",
             "沿用一级MA200共同起点、后复权行情、初始100万元、单边成本0.06%、现金零利息。",
             "信号在收盘确认，次日开盘执行；网格维持原月度移动中枢及日内路径。", "",
             "## 固定规则", "",
             pd.DataFrame([{"规则": name, "类型": family, "参数": json.dumps(params, ensure_ascii=False)}
                           for name, (family, params) in PRESETS.items()]).set_index("规则").to_markdown(), "",
             "日频均线为收盘高于均线满仓、否则空仓；月末MA200仅在月末产生信号。",
             "ATR均为SMA20、Wilder ATR、上轨入场/下轨退出；周期14与20的差异单独列明。",
             "唐奇安采用此前日线最高/最低价通道，不含信号当日。", "",
             "## 全期体验", ""]
    cols = ["年化收益%", "最大回撤%", "最长水下交易日", "深回撤时间占比%", "平均仓位%", "交易次数"]
    lines += [met[cols].round(2).rename_axis("规则").to_markdown(), ""]
    for years in HOLDING_YEARS:
        rows = rolling[rolling.years == years].set_index("strategy").reindex(
            [name for name in PRESETS if name != "长拿"])
        lines += [f"## 固定{years}年多起点", ""]
        if rows["窗口数"].notna().sum() == 0:
            lines += ["历史长度不足，没有完整窗口，结果留空。", ""]
            continue
        cols = ["窗口数", "超额年化中位数pp", "回撤改善中位数pp", "水下缩短交易日中位数",
                "收益回撤均改善比例%", "三项均不差比例%"]
        lines += [rows[cols].round(2).rename_axis("规则").to_markdown(), ""]
    lines += ["## 使用范围", "",
              "每月用相同初始资金重新建账，同起点配对长拿；起点前行情只用于指标，不继承虚拟持仓。",
              "月度起点窗口重叠，不能当作独立样本。水下时间包含期末未修复区间。",
              "全期与多起点结果用于描述预先列明的规则，不作为未见样本的选参验证。",
              "本次不改变标的、不扫描邻域、不测试更换起点或指数历史，也不把胜出设置写回一级。", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    asset = next(a for a in base.ASSETS if a.etf_code == ASSET_CODE)
    OUT.mkdir(parents=True, exist_ok=True)
    history = base.load_etf(ASSET_CODE)
    px = history.loc[base.window_start(history):]
    runners = build_runners(history)
    met, curves = holding.evaluate_window(px, runners)
    met.round(6).to_csv(OUT / "metrics.csv", encoding="utf-8-sig")
    base.annual_returns({name: curve.nav for name, curve in curves.items()}).round(6).to_csv(
        OUT / "annual.csv", encoding="utf-8-sig")
    pd.concat(curves, names=["strategy", "date"]).reset_index().to_csv(
        OUT / "daily.csv", index=False, encoding="utf-8-sig")
    # 全期成交可追溯；同一执行器再生成台账，并核对账户路径。
    trades = []
    for name, run in runners.items():
        ledger = []
        replay, _ = run(px, ledger=ledger)
        pd.testing.assert_frame_equal(replay, curves[name])
        trades.extend({"strategy": name, **trade} for trade in ledger)
    pd.DataFrame(trades).to_csv(OUT / "trades.csv", index=False, encoding="utf-8-sig")
    cohorts, summaries = [], []
    for years in HOLDING_YEARS:
        rows = holding.rolling_cohorts(history, runners, years)
        if not rows.empty:
            cohorts.append(rows)
        summary = base.experience.rolling_summary(rows).reset_index().assign(years=years)
        if not summary.empty:
            summaries.append(summary)
        print(f"{ASSET_CODE} {years}年多起点：{len(rows)}条配对记录", flush=True)
    all_cohorts = pd.concat(cohorts, ignore_index=True) if cohorts else pd.DataFrame()
    roll = pd.concat(summaries, ignore_index=True) if summaries else pd.DataFrame(
        columns=["strategy", "years", *base.experience.SUMMARY_COLS])
    all_cohorts.round(6).to_csv(OUT / "rolling_cohorts.csv", index=False, encoding="utf-8-sig")
    roll.round(6).to_csv(OUT / "rolling_summary.csv", index=False, encoding="utf-8-sig")
    window = {"start": str(px.index[0].date()), "end": str(px.index[-1].date())}
    write_report(asset, met, roll, window)
    files = [Path(module.__file__) for module in (base, dc, atr, holding, base.experience, base.asset_config)]
    files += [Path(__file__), base.PE_CACHE / f"etf_hfq_{ASSET_CODE}.csv"]
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "code": ASSET_CODE,
                "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "presets": PRESETS, "window": window, "holding_years": HOLDING_YEARS,
                "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT, "ann": base.ANN,
                "select_best_parameter": False, "initial_state": "cash",
                "overlapping_windows": True, "cutoff": "2026-09-30",
                "versions": {"pandas": pd.__version__}}
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(met[["年化收益%", "最大回撤%", "最长水下交易日"]].round(2).to_markdown(), flush=True)
    print(f"结果：{OUT / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
