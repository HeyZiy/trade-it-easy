# -*- coding: utf-8 -*-
"""前置结构诊断：滚动 Hurst、方差比与一阶自相关，并按 regime 对照已有多起点结果。

不重跑回测，只读取 level1 的 rolling_cohorts.csv 与行情缓存。
每个多起点窗口的起点之前取 504 个交易日（不足时退到至少 252 日）计算结构指标，
避免前视；再按「趋势 / 反转 / 随机 / 混合」分组，检验趋势族与反转族的超额是否随
结构指标单调变化。

从仓库根运行 python -m research.studies.timing.experiments.regime_diagnosis。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    __package__ = "research.studies.timing.experiments"

from .. import web_rules
from ..common import backtest as base

OUT = base.RESULTS / "regime"
LEVEL1 = base.RESULTS / "level1"
WINDOW, MIN_WINDOW = 504, 252
H_TOL = 0.03
TREND_FAMILIES = {"均线交叉", "均线择时", "通道突破", "时序动量", "量价突破"}
REVERSAL_FAMILIES = {"网格", "超跌反转"}


# ---------------------------------------------------------------- 结构指标
def hurst_dfa(close, min_scale=8, points=12):
    """DFA 的 Hurst 指数；返回 NaN 表示有效尺度不足。"""
    price = np.asarray(close, dtype=float)
    if len(price) < MIN_WINDOW or not np.isfinite(price).all() or (price <= 0).any():
        return np.nan
    logp = np.log(price)
    r = np.diff(logp)
    y = np.cumsum(r - r.mean())
    max_scale = len(y) // 4
    if max_scale <= min_scale:
        return np.nan
    scales = np.unique(np.round(np.logspace(np.log10(min_scale), np.log10(max_scale), points))
                       .astype(int))
    xs, ys = [], []
    for scale in scales:
        segments = len(y) // scale
        if segments < 2:
            continue
        resid = []
        for seq in (y, y[::-1]):
            for k in range(segments):
                seg = seq[k * scale:(k + 1) * scale]
                x = np.arange(scale)
                coef = np.polyfit(x, seg, 1)
                resid.append(seg - np.polyval(coef, x))
        f = np.sqrt(np.mean(np.concatenate(resid) ** 2))
        if f > 0:
            xs.append(np.log(scale))
            ys.append(np.log(f))
    if len(xs) < 3:
        return np.nan
    return float(np.polyfit(xs, ys, 1)[0])


def variance_ratio(close, q):
    """Lo-MacKinlay 方差比：q 期对数收益方差 / (q × 日收益方差)。"""
    price = np.asarray(close, dtype=float)
    if len(price) <= q + 1 or (price <= 0).any():
        return np.nan
    r = np.diff(np.log(price))
    mu = r.mean()
    var1 = ((r - mu) ** 2).sum() / (len(r) - 1)
    rq = np.log(price[q:] / price[:-q])
    varq = ((rq - q * mu) ** 2).sum() / len(rq)
    return float(varq / (q * var1)) if var1 > 0 else np.nan


def autocorr1(close):
    r = pd.Series(np.diff(np.log(np.asarray(close, dtype=float))))
    return float(r.autocorr(lag=1))


def classify(h, vr20, ac1):
    if not np.isfinite([h, vr20, ac1]).all():
        return "不可用"
    if abs(h - 0.5) <= H_TOL:
        return "随机"
    if h > 0.5 and vr20 > 1 and ac1 > 0:
        return "趋势"
    if h < 0.5 and vr20 < 1 and ac1 < 0:
        return "反转"
    return "混合"


def structure_at(history, start):
    prior = history.loc[history.index < start, "close"].tail(WINDOW)
    if len(prior) < MIN_WINDOW:
        return {"H": np.nan, "VR2": np.nan, "VR5": np.nan, "VR10": np.nan,
                "VR20": np.nan, "AC1": np.nan, "窗口日数": len(prior), "regime": "不可用"}
    close = prior.to_numpy()
    row = {"H": hurst_dfa(close), "AC1": autocorr1(close), "窗口日数": len(close)}
    for q in (2, 5, 10, 20):
        row[f"VR{q}"] = variance_ratio(close, q)
    row["regime"] = classify(row["H"], row["VR20"], row["AC1"])
    return row


# ---------------------------------------------------------------- 汇总
def family_group(family):
    if family in TREND_FAMILIES:
        return "趋势族"
    if family in REVERSAL_FAMILIES:
        return "反转族"
    return "其他"


def group_stats(df):
    return pd.Series({
        "组合数": len(df),
        "标的数": df.code.nunique(),
        "超额年化中位pp": df["较长拿年化差pp"].median(),
        "回撤改善中位pp": df["较长拿回撤改善pp"].median(),
        "跑赢比例%": (df["较长拿年化差pp"] > 0).mean() * 100,
        "均改善比例%": ((df["较长拿年化差pp"] > 0) & (df["较长拿回撤改善pp"] > 0)).mean() * 100,
    })


def regime_table(df):
    rows = []
    for (regime, group), g in df.groupby(["regime", "族组"], sort=False):
        rows.append({"regime": regime, "族组": group, **group_stats(g).to_dict()})
    return pd.DataFrame(rows)


def tercile_table(df):
    work = df.copy()
    work["H组"] = pd.qcut(work["H"], 3, labels=["低H", "中H", "高H"], duplicates="drop")
    rows = []
    for (bucket, group), g in work.groupby(["H组", "族组"], observed=True, sort=False):
        rows.append({"H组": str(bucket), "族组": group, **group_stats(g).to_dict()})
    return pd.DataFrame(rows)


def spearman(df, x, y):
    sub = df[[x, y]].dropna()
    return float(sub[x].corr(sub[y], method="spearman")), len(sub)


def _pick(table, key, value, group):
    sub = table[(table[key] == value) & (table["族组"] == group)]
    return sub.iloc[0] if not sub.empty else None


def _fmt(row, col, digits=2):
    return "—" if row is None else f"{row[col]:.{digits}f}"


def write_summary(structure, cohorts, regime_stats, terciles):
    lines = ["# 前置结构诊断", "",
             f"每个多起点窗口的起点之前取 {WINDOW} 个交易日（不足时至少 {MIN_WINDOW} 日）计算：",
             "DFA Hurst、Lo-MacKinlay 方差比 VR(q)、日收益一阶自相关 AC1。",
             f"H 在 0.5±{H_TOL} 记为随机；趋势需 H>0.5、VR20>1、AC1>0 同时成立；",
             "反转需三者反向同时成立；其余记为混合。", "",
             "## 每只标的的诊断分布", ""]
    dist = (structure[structure.regime != "不可用"]
            .groupby(["code", "标的", "regime"]).size().unstack(fill_value=0))
    dist.index = dist.index.get_level_values("标的")
    lines += [dist.to_markdown(), ""]

    lines += ["## 3 年窗口：regime × 族组", "",
              "超额与回撤改善均相对同标的长拿；组合 = 标的 × 规则 × 窗口。", "",
              regime_stats.round(2).to_markdown(index=False), ""]

    lines += ["## 3 年窗口：H 三分位 × 族组", "",
              "按全部窗口的 H 三等分，用于检查结论是否只是分组阈值造成。", "",
              terciles.round(2).to_markdown(index=False), ""]

    rh, nh = spearman(cohorts[cohorts["族组"] == "趋势族"], "H", "较长拿年化差pp")
    rv, nv = spearman(cohorts[cohorts["族组"] == "反转族"], "H", "较长拿年化差pp")
    lines += ["## 单调性检验（Spearman）", "",
              "| 族组 | 与 H 的秩相关 | 名义样本 |",
              "| --- | ---: | ---: |",
              f"| 趋势族 | {rh:.3f} | {nh} |",
              f"| 反转族 | {rv:.3f} | {nv} |", "",
              "窗口高度重叠、同一规则跨窗口相关，名义样本只用于看方向，不能当独立观测。", ""]

    tr, tv = _pick(regime_stats, "regime", "趋势", "趋势族"), \
             _pick(regime_stats, "regime", "趋势", "反转族")
    lo_t, lo_r = _pick(terciles, "H组", "低H", "趋势族"), _pick(terciles, "H组", "低H", "反转族")
    hi_t, hi_r = _pick(terciles, "H组", "高H", "趋势族"), _pick(terciles, "H组", "高H", "反转族")
    if rv > 0 and rh > 0:
        verdict = "结构指标没有指向「H 低该扫反转」：两族的超额都与 H 同向，高 H 时一起变好、低 H 时一起变差。"
    elif rv < 0 < rh:
        verdict = "方向与前置假设一致：H 越高趋势族越好、反转族越差。"
    else:
        verdict = "方向不一致或不显著，结构指标不能单独决定扫哪一族。"
    lines += ["## 判读", "",
              f"- 趋势 regime 内，趋势族超额中位 {_fmt(tr, '超额年化中位pp')}pp、跑赢 "
              f"{_fmt(tr, '跑赢比例%')}%；反转族 {_fmt(tv, '超额年化中位pp')}pp、"
              f"{_fmt(tv, '跑赢比例%')}%。",
              f"- H 低组：趋势族 {_fmt(lo_t, '超额年化中位pp')}pp、反转族 "
              f"{_fmt(lo_r, '超额年化中位pp')}pp；H 高组：趋势族 "
              f"{_fmt(hi_t, '超额年化中位pp')}pp、反转族 {_fmt(hi_r, '超额年化中位pp')}pp。",
              f"- 与 H 的秩相关：趋势族 {rh:.3f}，反转族 {rv:.3f}。",
              f"- {verdict}", ""]

    counts = cohorts.groupby("regime").size()
    lines += ["## 样本量", "",
              "3 年窗口组合数：" + "、".join(f"{k} {v}" for k, v in counts.items()), "",
              "regime 由窗口起点之前的数据判定，窗口内表现不参与分组，避免前视。", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cohorts = pd.read_csv(LEVEL1 / "rolling_cohorts.csv", encoding="utf-8-sig",
                          parse_dates=["start", "end"])
    cohorts = cohorts[cohorts.years == 3].copy()
    cohorts["code"] = cohorts["code"].astype(str)
    cohorts["族"] = cohorts["strategy"].map(web_rules.FAMILY)
    cohorts["族组"] = cohorts["族"].map(family_group)

    rows = []
    for asset in base.ASSETS:
        code = asset.etf_code
        history = base.load_etf(code)
        starts = cohorts.loc[cohorts.code == code, "start"].dropna().unique()
        for start in sorted(starts):
            rows.append({"code": code, "标的": asset.name, "start": pd.Timestamp(start),
                         **structure_at(history, pd.Timestamp(start))})
    structure = pd.DataFrame(rows)
    merged = cohorts.merge(structure, on=["code", "标的", "start"], how="left")

    usable = merged[merged.regime != "不可用"].copy()
    regime_stats = regime_table(usable)
    terciles = tercile_table(usable)

    structure.to_csv(OUT / "structure.csv", index=False, encoding="utf-8-sig")
    merged.drop(columns=["族组"]).to_csv(
        OUT / "regime_cohorts.csv", index=False, encoding="utf-8-sig")
    regime_stats.round(6).to_csv(OUT / "regime_stats.csv", index=False, encoding="utf-8-sig")
    terciles.round(6).to_csv(OUT / "tercile_stats.csv", index=False, encoding="utf-8-sig")
    write_summary(structure, usable, regime_stats, terciles)

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {"rolling_cohorts": hashlib.sha256(
            (LEVEL1 / "rolling_cohorts.csv").read_bytes()).hexdigest()},
        "window": WINDOW, "min_window": MIN_WINDOW, "h_tolerance": H_TOL,
        "years": 3, "trend_families": sorted(TREND_FAMILIES),
        "reversal_families": sorted(REVERSAL_FAMILIES),
        "observations": len(usable), "versions": {"pandas": pd.__version__,
                                                  "numpy": np.__version__},
    }
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(regime_stats.round(2).to_string(index=False))
    print(f"\n结果：{OUT / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
