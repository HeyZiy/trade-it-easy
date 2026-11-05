# -*- coding: utf-8 -*-
"""把各层已生成的 daily 曲线画成交互 HTML：净值、水下回撤、仓位三联。

只读 results/ 下已有的 daily CSV，不重算任何账户；净值按期初资金归一，
期初=1 且不含首日损益，因此首日买卖造成的跳变会显示出来。
从仓库根运行：
    python -m research.studies.timing.plot_curves                 # 全部层全部标的
    python -m research.studies.timing.plot_curves --layer level1 --code 513180
输出：results/<层>/plots/<代码>.html；plotly.min.js 由 plotly 写到同目录，
均为可再生产物，不入库。
"""
import argparse

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .common import backtest as base

CAPITAL = base.CAPITAL
NAMES = {asset.etf_code: asset.name for asset in base.ASSETS}
# 每层的 daily 文件布局不同：一级与两份族扫描按代码命名，二级放在标的目录下。
LAYOUTS = {
    "level1": ("results/level1", "daily_{code}.csv"),
    "level2": ("results/level2/{code}", "daily.csv"),
    "donchian": ("results/supplementary/donchian", "daily_{code}.csv"),
    "atr_channel": ("results/supplementary/atr_channel", "daily_{code}.csv"),
}
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b",
          "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ff9896"]
ROWS = [dict(height=0.46, title="净值倍数（期初=1）"),
        dict(height=0.30, title="水下回撤%"),
        dict(height=0.24, title="仓位%")]


def daily_files(layer, codes):
    root, pattern = LAYOUTS[layer]
    out = []
    for code in codes:
        path = base.STUDY / root.replace("{code}", code) / pattern.format(code=code)
        if path.exists():
            out.append((code, path))
    return out


def curves(path):
    df = pd.read_csv(path, parse_dates=["date"], usecols=["strategy", "date", "nav", "exposure"])
    frames = {}
    for name, g in df.groupby("strategy", sort=False):
        g = g.sort_values("date")
        nav = (g.nav / CAPITAL).round(6)
        frames[name] = pd.DataFrame({
            "date": g.date.values,
            "nav": nav.values,
            "underwater": ((nav / nav.cummax() - 1) * 100).round(3).values,
            "exposure": (g.exposure * 100).round(2).values,
        })
    return frames


def build_figure(layer, code, path, frames):
    names = list(frames)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.045,
                        row_heights=[r["height"] for r in ROWS],
                        subplot_titles=[r["title"] for r in ROWS])
    for i, name in enumerate(names):
        c = COLORS[i % len(COLORS)]
        d = frames[name]
        width = 2.2 if name == "长拿" else 1.4
        dash = "dot" if name == "长拿" else None
        fig.add_trace(go.Scatter(x=d.date, y=d.nav, name=name, legendgroup=name,
                                 line=dict(color=c, width=width, dash=dash),
                                 hovertemplate="%{y:.3f}倍<extra>" + name + "</extra>"), row=1, col=1)
        fig.add_trace(go.Scatter(x=d.date, y=d.underwater, name=name, legendgroup=name,
                                 showlegend=False, line=dict(color=c, width=1.2),
                                 fill="tozeroy", opacity=0.55,
                                 hovertemplate="%{y:.1f}%<extra>" + name + "</extra>"), row=2, col=1)
        fig.add_trace(go.Scatter(x=d.date, y=d.exposure, name=name, legendgroup=name,
                                 showlegend=False, line=dict(color=c, width=1),
                                 hovertemplate="%{y:.0f}%<extra>" + name + "</extra>"), row=3, col=1)
    window = f"{frames[names[0]].date.iloc[0]:%Y-%m-%d} 至 {frames[names[0]].date.iloc[-1]:%Y-%m-%d}"
    fig.update_layout(
        title=dict(text=f"{layer} · {NAMES.get(code, code)}（{code}）｜{len(names)}条规则｜{window}",
                   x=0.01, font=dict(size=15)),
        height=920, hovermode="x unified", legend=dict(orientation="h", y=-0.06, x=0),
        margin=dict(l=60, r=30, t=90, b=60), template="plotly_white",
        hoverlabel=dict(namelength=-1))
    fig.update_yaxes(row=3, range=[-5, 105])
    fig.update_xaxes(row=3, rangeslider=dict(visible=False))
    fig.add_annotation(text="长拿虚线为基准；点图例可隐藏，双击图例只留该条。",
                       xref="paper", yref="paper", x=1, y=1.06, showarrow=False,
                       font=dict(size=11, color="#666"))
    return fig


def logscale_buttons():
    return [dict(label="对数坐标", method="relayout", args=[{"yaxis.type": "log"}]),
            dict(label="线性坐标", method="relayout", args=[{"yaxis.type": "linear"}])]


def main():
    ap = argparse.ArgumentParser(description="把 daily 曲线画成交互 HTML")
    ap.add_argument("--layer", default="all", choices=[*LAYOUTS, "all"])
    ap.add_argument("--code", default=None, help="只画该代码，默认全部标的")
    args = ap.parse_args()
    layers = list(LAYOUTS) if args.layer == "all" else [args.layer]
    codes = [args.code] if args.code else [a.etf_code for a in base.ASSETS]
    written = 0
    for layer in layers:
        out_dir = base.STUDY / LAYOUTS[layer][0].replace("{code}", "") / "plots"
        for code, path in daily_files(layer, codes):
            frames = curves(path)
            if not frames:
                print(f"跳过 {layer}/{code}：无曲线")
                continue
            fig = build_figure(layer, code, path, frames)
            fig.update_layout(updatemenus=[dict(buttons=logscale_buttons(), x=0.01, y=1.13,
                                                direction="right", showactive=True)])
            out_dir.mkdir(parents=True, exist_ok=True)
            fig.write_html(out_dir / f"{code}.html", include_plotlyjs="directory",
                           full_html=True, config=dict(displaylogo=False, scrollZoom=True))
            written += 1
            print(f"{layer}/{code} -> {(out_dir / f'{code}.html').relative_to(base.STUDY)}")
    print(f"共 {written} 张图")


if __name__ == "__main__":
    main()
