# -*- coding: utf-8 -*-
"""网格重定中枢频率对照：每月（主结果） vs 每季 vs 不重定。

其余机制与主研究完全一致：±10档、步长3%、资金20等份、期初买10份、
T+1（当日买入不可卖）、跳空按开盘价改善成交、O-L-H-C 主路径。
逐标的比较三种中枢频率的年化、回撤、Sharpe、平均仓位与交易次数；
仅报告对照，不按结果更改主结果参数。
从仓库根运行：python -m research.studies.broad_index_strategies.experiments.grid_reanchor
输出：results/supplementary/grid_reanchor/ 下 CSV + 控制台对比表。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from ..common import backtest as base

OUT = base.STUDY / "results" / "supplementary" / "grid_reanchor"
CASES = {"每月重定中枢": dict(reanchor=True, reanchor_every=1),
         "每季重定中枢": dict(reanchor=True, reanchor_every=3),
         "不重定中枢": dict(reanchor=False)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for asset in base.ASSETS:
        history = base.load_etf(asset.etf_code)
        px = history.loc[base.window_start(history):]  # 与主回测同窗口
        hold, hold_count = base.sim_buyhold(px)
        hold_metrics = base.metrics(hold.nav, hold.exposure, hold_count)
        tables = {}
        for name, kwargs in CASES.items():
            res, trades = base.sim_grid(px, **kwargs)
            met = base.metrics(res["nav"], res["exposure"], trades)
            met.update(base.experience.metric_differences(met, hold_metrics))
            met.update(base.experience.relative_metrics(res.nav, hold.nav))
            tables[name] = met
            rows.append({"标的": asset.name, "code": asset.etf_code, "中枢频率": name,
                         **met})
        comp = pd.DataFrame(tables).T
        print(f"\n## {asset.name}（{asset.etf_code}）")
        print(comp.round(2).to_markdown())
    all_rows = pd.DataFrame(rows)
    all_rows.round(4).to_csv(OUT / "metrics_by_frequency.csv", index=False, encoding="utf-8-sig")
    show = ["年化收益%", "最大回撤%", "最长水下交易日", "Calmar", "平均仓位%", "交易次数"]
    summary = all_rows.pivot(index="标的", columns="中枢频率", values=show).swaplevel(axis=1)
    summary = summary.reindex(columns=pd.MultiIndex.from_product(
        [list(CASES), show], names=["中枢频率", "指标"]))
    summary.to_csv(OUT / "summary_pivot.csv", encoding="utf-8-sig")
    print("\n## 汇总（标的 × 频率 × 指标）")
    print(summary.round(2).to_markdown())
    files = [Path(__file__), Path(base.__file__), Path(base.experience.__file__), Path(base.asset_config.__file__),
             *(base.PE_CACHE / f"etf_hfq_{asset.etf_code}.csv" for asset in base.ASSETS)]
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT,
                "step": base.GRID_STEP, "n_each_side": base.GRID_N,
                "cases": {k: {kk: vv for kk, vv in v.items()} for k, v in CASES.items()},
                "intraday_path": "O-L-H-C", "cutoff": "2026-09-30",
                "windows": {a.etf_code: {"start": str(base.window_start(base.load_etf(a.etf_code)).date()),
                                         "end": str(base.load_etf(a.etf_code).index[-1].date())}
                            for a in base.ASSETS},
                "versions": {"pandas": pd.__version__}}
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
