# -*- coding: utf-8 -*-
"""波动率缩放反事实探针（v4_1_2，Barroso & Santa-Clara 2015 落地）。

问题（2026-10-11）：回撤分解确认回撤来自 6 只集中 × 动量逆风，个股规则层已摸透。
检验组合层方案：按策略自身波动率动态缩放整体仓位，能否在收益损失可控的前提下
压低最大回撤。一阶反事实：不重跑回测，直接对 v4_1_2 日净值收益缩放。

方法（预声明主口径，不做参数扫描）：
- σ̂_t = 过去126交易日日收益std × √252（策略自身波动，可预测性是文献核心事实）
- σ*_t = 扩张窗口（截至t-1）std × √252，样本≥252后启用，之前不缩放（无前视）
- w_t = min(1, σ*_t / σ̂_t)：只减仓不加杠杆
- r'_t = w_t × r_t − |w_t − w_{t-1}| × 0.001（缩放调仓成本，单边0.1%）
- 现金收益对照版：减仓部分按 2%/年 计息
- 超额回撤为 additive 近似（策略日收益减基准日收益的累计曲线回撤），
  与平台口径不同，只用于原版/缩放版同口径对照。

敏感性（60/252日窗口、全样本常数目标）未实现：按预声明纪律不扫描，如需另行预声明。

运行：.conda\\python.exe research/studies/quality_momentum/internal/analyze_vol_scaled_v412.py
（项目 .conda 环境；nav 为 exports/ 平台导出，基准为本地 idx_000906 缓存）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
EXPORTS = STUDY / 'exports'
CACHE = STUDY.parent / 'roe_quality' / '_cache' / 'pool'
OUT = STUDY / 'reports' / 'vol_scaled_v412_2026-10-11'
REPORT = STUDY / 'reports' / 'vol_scaled_v412_2026-10-11.md'
WINDOW = 126
MIN_EXPANDING = 252
COST = 0.001
CASH_RATE = 0.02
ANNUAL_DAYS = 245.0
HEADWIND_1 = ('2021-09-09', '2023-12-20')   # v4_1_2 最大回撤区间
HEADWIND_2 = ('2024-06-04', '2025-04-09')   # 动量逆风段（v4_1 系超额回撤主体）


def require(cond, msg):
    if not cond:
        raise SystemExit(f'FAIL: {msg}')


def period_stats(r):
    """输入日收益Series（已索引日期），返回指标dict。"""
    equity = (1.0 + r).cumprod()
    total = equity.iloc[-1] - 1.0
    ann = (1.0 + total) ** (ANNUAL_DAYS / len(r)) - 1.0
    vol = r.std() * np.sqrt(ANNUAL_DAYS)
    sharpe = r.mean() / r.std() * np.sqrt(ANNUAL_DAYS)
    dd = equity / equity.cummax() - 1.0
    trough = dd.idxmin()
    peak = equity.loc[:trough].idxmax()
    after = equity.loc[trough:]
    recovered = after[after >= equity.loc[peak]].index
    end = recovered[0] if len(recovered) else None
    monthly = equity.resample('ME').last().pct_change().dropna()
    return {'年化': ann, '总收益': total, '年化波动': vol, '夏普': sharpe,
            '最大回撤': dd.min(), '回撤峰': peak, '回撤谷': trough, '回撤修复': end,
            '最差单月': monthly.min()}


def excess_dd(r, dates, bench):
    """additive 超额日收益累计曲线的最大回撤。"""
    rb = bench.reindex(dates).pct_change().fillna(0.0)
    ex = (r - rb).cumsum()
    return float((ex - ex.cummax()).min())


def main():
    nav = pd.read_csv(EXPORTS / 'pool_rotation_v4_1_2_nav.csv', encoding='utf-8-sig')
    nav['date'] = pd.to_datetime(nav['date'])
    nav = nav.set_index('date').sort_index()
    require(not nav.index.duplicated().any(), '净值日期重复')
    require((nav.equity > 0).all(), '净值含非正值')
    r = nav.equity.pct_change().fillna(0.0)

    sigma_hat = r.rolling(WINDOW).std().shift(1) * np.sqrt(ANNUAL_DAYS)
    sigma_star = r.expanding(MIN_EXPANDING).std().shift(1) * np.sqrt(ANNUAL_DAYS)
    w = (sigma_star / sigma_hat).clip(upper=1.0).fillna(1.0)
    dw = w.diff().fillna(0.0).abs()
    r_cost = w * r - dw * COST
    r_cash = w * r - dw * COST + (1.0 - w) * CASH_RATE / ANNUAL_DAYS

    idx = pd.read_pickle(CACHE / 'idx_000906.pkl')
    if isinstance(idx, pd.DataFrame):
        require('close' in idx.columns, '基准缓存缺close列')
        values = idx['close']
    else:
        values = idx
    bench = pd.Series(pd.to_numeric(values, errors='raise').to_numpy(),
                      index=pd.DatetimeIndex(pd.to_datetime(values.index)).normalize())
    bench = bench[~bench.index.duplicated(keep='last')].sort_index()
    bench = bench.reindex(r.index).ffill()
    require(bench.notna().all(), '基准指数与账户日历不齐')

    orig, scaled, cashv = period_stats(r), period_stats(r_cost), period_stats(r_cash)
    ex_orig = excess_dd(r, r.index, bench)
    ex_scaled = excess_dd(r_cost, r.index, bench)

    rows = []
    for key in ('年化', '年化波动', '夏普', '最大回撤', '最差单月'):
        rows.append({'指标': key, '原版': orig[key], '缩放(不计现金息)': scaled[key],
                     '缩放(现金2%/年)': cashv[key]})
    table = pd.DataFrame(rows).set_index('指标')

    peak = pd.Timestamp(HEADWIND_1[0]); end = pd.Timestamp(HEADWIND_1[1])
    peak2 = pd.Timestamp(HEADWIND_2[0]); end2 = pd.Timestamp(HEADWIND_2[1])
    w1 = w.loc[peak:end]; w2 = w.loc[peak2:end2]

    annual = pd.DataFrame({
        '原版': (1 + r).groupby(r.index.year).prod() - 1,
        '缩放': (1 + r_cost).groupby(r.index.year).prod() - 1,
    })
    annual['diff_pp'] = (annual['缩放'] - annual['原版']) * 100

    lines = [
        '# 波动率缩放反事实（v4_1_2，2026-10-11）', '',
        'Barroso-Santa-Clara 2015 落地：w = min(1, 扩张窗口波动/过去126日波动)，'
        f'只减仓不加杠杆，调仓成本单边0.1%。一阶反事实（直接缩放日收益，不重跑回测）；'
        '超额回撤为 additive 近似，仅作同口径对照。前一年（样本<252日）不缩放。', '',
        '## 总对照', '',
        table.map(lambda v: f'{v:.2%}' if abs(v) < 10 else f'{v:.3f}').to_markdown(), '',
        f'- 最大回撤区间：原版 {orig["回撤峰"].date()}~{orig["回撤谷"].date()}' 
        f'（修复 {orig["回撤修复"].date() if orig["回撤修复"] is not None else "未"}）；'
        f'缩放版 {scaled["回撤峰"].date()}~{scaled["回撤谷"].date()}'
        f'（修复 {scaled["回撤修复"].date() if scaled["回撤修复"] is not None else "未"}）', '',
        f'- 超额最大回撤（additive近似）：原版 {ex_orig:.2%} → 缩放 {ex_scaled:.2%}', '',
        '## 缩放诊断', '',
        f'- 缩放生效（w<1）时间占比：{(w < 1).mean():.1%}；w 均值：{w.mean():.3f}',
        f'- 逆风区均值 w：2021/09~2023/12 {w1.mean():.3f}；2024/06~2025/04 {w2.mean():.3f}',
        f'- 缩放调仓成本合计拖累：{dw.sum() * COST:.2%}（全程累计）', '',
        '## 分年对照', '',
        annual.round(4).to_markdown(), '',
    ]
    OUT.mkdir(exist_ok=True)
    pd.DataFrame({'r': r, 'w': w, 'r_scaled': r_cost, 'r_cash': r_cash,
                  'equity_orig': (1 + r).cumprod(), 'equity_scaled': (1 + r_cost).cumprod()}
                 ).to_csv(OUT / 'vol_scaled_daily.csv', encoding='utf-8-sig')
    REPORT.write_text('\n'.join(lines), encoding='utf-8')

    print(table.round(4).to_string())
    print(f'回撤区间 原版 {orig["回撤峰"].date()}~{orig["回撤谷"].date()} 修复'
          f' {orig["回撤修复"]}；缩放 {scaled["回撤峰"].date()}~{scaled["回撤谷"].date()} 修复 {scaled["回撤修复"]}')
    print(f'超额最大回撤(additive): {ex_orig:.2%} -> {ex_scaled:.2%}')
    print(f'w<1占比 {(w<1).mean():.1%} w均值 {w.mean():.3f} 逆风区w1 {w1.mean():.3f} w2 {w2.mean():.3f}')
    print(annual.round(4).to_string())


if __name__ == '__main__':
    main()
