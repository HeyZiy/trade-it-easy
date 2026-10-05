"""共享命令行：输入曲线、取得指数、运行纯计算模块并输出报告。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

import pandas as pd

from .analysis import RegimeConfig, analyze_returns
from .data import CurveSpec, HERE, KINDS, fetch_index, read_index, read_returns
from .report import write_report


def main(argv: list[str] | None = None, *, defaults: dict | None = None) -> None:
    defaults = defaults or {}
    parser = argparse.ArgumentParser(description="按已完成月/周均线状态诊断任意研究的逐日收益曲线")
    parser.add_argument("--results", nargs="+", type=Path, required="results" not in defaults)
    parser.add_argument("--format", choices=KINDS, default="auto", help="auto只按约定列名识别，不猜测单位")
    parser.add_argument("--columns", nargs="+", help="日期、策略、可选基准列；#N表示从0开始的列位置")
    parser.add_argument("--initial-nav", type=float, default=1, help="nav格式首日前的策略净值/资金")
    parser.add_argument("--initial-benchmark-nav", type=float, default=1)
    parser.add_argument("--index-csv", type=Path, help="离线指数日线，列date,close；需含均线预热期")
    parser.add_argument("--index-symbol", default="sz399317", help="Sina指数代码，默认国证A指")
    parser.add_argument("--index-name", help="报告中的指数名称；离线未指定则显示市场指数")
    parser.add_argument("--benchmark-name", default="CSV基准", help="曲线自身的基准名称，不影响状态指数")
    parser.add_argument("--cache", type=Path, help="行情缓存；默认共享目录下按指数代码隔离")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--month-window", type=int, default=10)
    parser.add_argument("--week-window", type=int, default=20)
    parser.add_argument("--annualization-days", type=int, default=252)
    parser.add_argument("--exclude-years", nargs="+", type=int, default=(), help="可选年份剔除诊断，默认不剔除")
    parser.add_argument("--out", type=Path, default=HERE / "reports" / "market_regimes")
    parser.set_defaults(**defaults)
    args = parser.parse_args(argv)
    spec = CurveSpec(args.format, tuple(args.columns) if args.columns else None,
                     args.initial_nav, args.initial_benchmark_nav)
    config = RegimeConfig(args.month_window, args.week_window, args.annualization_days, tuple(args.exclude_years))
    curves = [(path, read_returns(path, spec)) for path in args.results]
    if args.index_csv:
        index = read_index(args.index_csv)
        source = {"source": f"本地指数CSV {args.index_csv.resolve()}", "source_url": None,
                  "index_symbol": None, "index_file": str(args.index_csv.resolve())}
        index_name = args.index_name or "市场指数"
    else:
        index, source = fetch_index(args.index_symbol, args.cache, args.refresh)
        index_name = args.index_name or {"sz399317": "国证A指", "sh000906": "中证800"}.get(args.index_symbol, args.index_symbol)
    stems = [path.stem for path, _ in curves]
    names = [path.stem if stems.count(path.stem) == 1 else f"{path.parent.name}_{path.stem}" for path, _ in curves]
    if len(set(names)) != len(names):
        raise ValueError("输入文件名重复且父目录名相同，请分别运行或调整文件名")
    analyses, inputs = [], []
    # 全部输入计算成功后才写报告，输入数据和网络访问都在纯计算模块之外。
    for name, (path, curve) in zip(names, curves):
        analysis = analyze_returns(curve, index, config)
        analyses.append({"name": name, **vars(analysis)})
        inputs.append({"name": name, "path": str(path.resolve()), "format": curve.attrs["input_kind"],
                       "columns": spec.columns, "initial_nav": spec.initial_nav,
                       "initial_benchmark_nav": spec.initial_benchmark_nav})
    metadata = {**source, **asdict(config), "index_name": index_name, "benchmark_name": args.benchmark_name,
                "index_start": str(index["date"].iloc[0].date()), "index_end": str(index["date"].iloc[-1].date()),
                "timing": "latest calendar month-end / Friday strictly before return date",
                "annualization": "conditional geometric daily mean",
                "inputs": inputs, "created_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat()}
    args.out.mkdir(parents=True, exist_ok=True)
    for item in analyses:
        target = args.out / item["name"]
        target.mkdir(exist_ok=True)
        for name in ("daily", "episodes", "summary", "year_states"):
            item[name].to_csv(target / f"{name}.csv", index=False, encoding="utf-8-sig")
        print(f"\n{item['name']} ({len(item['daily'])} days, {len(item['episodes'])} episodes)")
        columns = ["state_label", "days", "episodes", "strategy_annualized", "index_annualized", "relative_index_annualized"]
        if config.exclude_years:
            columns.append("excluded_years_annualized")
        display = item["summary"][columns].copy()
        for col in columns[3:]:
            display[col] = display[col].map(lambda value: f"{value:.2%}" if pd.notna(value) else "—")
        print(display.to_string(index=False))
    (args.out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(analyses, args.out, metadata)
    print(f"\nReport: {(args.out / 'report.html').resolve()}")
