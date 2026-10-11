# -*- coding: utf-8 -*-
"""动量逆风状态探针：Daniel-Moskowitz式两维度四状态（预声明设计）。

用户预声明（2026-10-11，不扫窗口、不加维度）：
- 维度1 市场前期状态：沪深300过去120日收益是否为负（截至信号日T的前一收盘T-1）
- 维度2 市场近期反弹：沪深300最近20日收益是否为正（同口径）
- 四状态：A=前期涨+近期涨；B=前期涨+近期跌；C=前期跌+近期跌；D=前期跌+近期涨（重点）
- 观测量：每轮（20个交易日，链轮不重叠）动量链的超额收益
  （动量链r20 − 沪深300同窗口收益）、亏损概率（超额<0占比）、左尾损失（超额5%分位）。
- 动量链：反转探针signals.csv按轮聚合rank<=6的r20均值（与rev_chain_equity复算校验）。
- 边界：收盘近似；沪深300代理"市场"；状态边界（恰好为0）归入上涨侧；
  链轮2016-03-07起，120日历史不足的前期剔除；D状态含单一样本事件，
  任何规则不可据此训练。
运行：.conda\\python.exe research/studies/quality_momentum/internal/analyze_momentum_regime_probe.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = STUDY.parents[2]
CACHE = ROOT / 'research/studies/roe_quality/_cache/pool'
SRC = STUDY / 'reports' / 'reversal_probe_2026-10-11' / 'signals.csv'
EQUITY = STUDY / 'reports' / 'reversal_probe_2026-10-11' / 'rev_chain_equity.csv'
OUT = STUDY / 'reports' / 'momentum_regime_probe_2026-10-11'
REPORT = STUDY / 'reports' / 'momentum_regime_probe_2026-10-11.md'
STATES = ('A', 'B', 'C', 'D')
START, END = '2015-06-01', '2026-09-30'
STATE_DESC = {
    'A': '前期涨+近期涨（上涨延续）',
    'B': '前期涨+近期跌（上涨后调整）',
    'C': '前期跌+近期跌（熊市延续）',
    'D': '前期跌+近期涨（熊后反弹，重点观察）',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_csi300():
    """沪深300日线（新浪指数接口，单次单线程；东财接口被本机代理拦截），
    缓存复用。返回截取START~END的close序列。"""
    path = CACHE / 'idx_000300.pkl'
    if path.exists():
        return pd.read_pickle(path)
    import akshare as ak
    frame = ak.stock_zh_index_daily(symbol='sh000300')
    require(frame is not None and len(frame) > 0, '沪深300抓取为空')
    frame = frame.rename(columns={'date': 'date', 'close': 'close'})[['date', 'close']]
    series = pd.Series(pd.to_numeric(frame.close, errors='raise').to_numpy(),
                       index=pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize())
    require(series.index.is_unique and series.index.is_monotonic_increasing, '沪深300乱序')
    series = series.loc[START:END]
    require(len(series) > 2000, f'沪深300覆盖不足 {len(series)}')
    series.to_pickle(path)
    return series


def main():
    signals = pd.read_csv(SRC, parse_dates=['signal_day'])
    require(not signals.empty, 'signals为空')
    top6 = signals[signals['rank'].le(6)]
    chain = top6.groupby('signal_day')['r20'].mean().rename('chain_r20')
    require(len(chain) >= 100, f'链轮过少 {len(chain)}')

    # 与反转探针年表校验（注意：rev_chain_equity.csv是反转链净值，不能作动量链锚）
    yt = pd.read_csv(STUDY / 'reports' / 'reversal_probe_2026-10-11' / 'year_table.csv')
    chain = chain.sort_index()
    my_year = (1 + chain).groupby(chain.index.year).prod() - 1
    yt_mom = yt.set_index('year')['mom']
    common_years = my_year.index.intersection(yt_mom.index)
    require(len(common_years) >= 10, f'年表可比年过少 {len(common_years)}')
    eq_check = float(np.max(np.abs(
        my_year[common_years].to_numpy() - yt_mom[common_years].to_numpy())))
    print(f'[校验] 动量链复算 vs 反转探针年表：{len(common_years)} 年，最大年累计差 {eq_check:.2e}',
          flush=True)

    c300 = load_csi300()
    calendar_src = pd.read_pickle(CACHE / 'idx_000906.pkl')
    calendar = pd.DatetimeIndex(pd.to_datetime(calendar_src.index)).normalize().sort_values()
    pos = {day: i for i, day in enumerate(calendar)}
    c300 = c300.reindex(calendar)
    arr = c300.to_numpy(dtype=float)

    rows = []
    for day, ret in chain.items():
        base = pos.get(day)
        if base is None or base < 121 or base + 21 >= len(calendar):
            continue
        c_prev, c_21, c_121 = arr[base - 1], arr[base - 21], arr[base - 121]
        if not (np.isfinite(c_prev) and np.isfinite(c_21) and np.isfinite(c_121)):
            continue
        prior120 = c_prev / c_121 - 1
        recent20 = c_prev / c_21 - 1
        state = ('A' if prior120 >= 0 else 'C') if recent20 >= 0 else \
                ('B' if prior120 >= 0 else 'D')
        entry, exit_close = arr[base + 1], arr[base + 21]
        if not (np.isfinite(entry) and np.isfinite(exit_close)):
            continue
        mkt_r20 = exit_close / entry - 1
        rows.append(dict(signal_day=day, prior120=prior120, recent20=recent20,
                         state=state, chain_r20=ret, mkt_r20=mkt_r20,
                         excess=ret - mkt_r20))
    reg = pd.DataFrame(rows)
    require(not reg.empty, '无有效观测')

    table = []
    for s in STATES:
        sub = reg[reg.state.eq(s)]
        if sub.empty:
            table.append(dict(state=s, n=0))
            continue
        ex = sub.excess
        table.append(dict(
            state=s, n=int(len(sub)),
            mean_excess=float(ex.mean()), median_excess=float(ex.median()),
            loss_prob=float(ex.lt(0).mean()), p5_excess=float(ex.quantile(0.05)),
            min_excess=float(ex.min()),
            mean_chain_r20=float(sub.chain_r20.mean()),
            mean_mkt_r20=float(sub.mkt_r20.mean())))
    state_table = pd.DataFrame(table)
    OUT.mkdir(parents=True, exist_ok=True)
    reg.to_csv(OUT / 'round_states.csv', index=False, encoding='utf-8-sig')
    state_table.to_csv(OUT / 'state_table.csv', index=False, encoding='utf-8-sig')

    d_rounds = reg[reg.state.eq('D')].copy()
    d_rounds['year'] = d_rounds.signal_day.dt.year
    d_rounds.to_csv(OUT / 'd_rounds.csv', index=False, encoding='utf-8-sig')

    summary = dict(
        date='2026-10-11',
        design='预声明两维度四状态，只看下期20日动量链超额/亏损概率/左尾，不扫窗口',
        rounds=int(len(reg)), eq_check_max_abs=eq_check,
        states={r['state']: r for r in table},
        d_round_dates=[str(d.date()) for d in d_rounds.signal_day],
    )
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')
    print(json.dumps(clean(summary), ensure_ascii=False, default=str), flush=True)
    write_report(state_table, d_rounds, summary)


def clean(obj):
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return float(obj) if np.isfinite(obj) else None
    return obj


def fmt(v, pct=True):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return '—'
    return f'{v:.2%}' if pct else str(v)


def write_report(state_table, d_rounds, summary):
    lines = [
        '# 动量逆风状态探针：两维度四状态（2026-10-11）', '',
        f"预声明设计：市场前期状态=沪深300过去120日收益正负、市场近期反弹=最近20日收益正负"
        f"（均截至信号日前一收盘）；四状态A/B/C/D；只看下期20日动量链超额收益"
        f"（−沪深300同窗口）、亏损概率、左尾损失（5%分位），不扫窗口。", '',
        f"链轮 {summary['rounds']} 轮（与反转探针equity复算校验：最大累计差 "
        f"{summary['eq_check_max_abs']:.2e}）；轮间不重叠（20交易日一轮）。", '',
        '| 状态 | 定义 | 轮数 | 超额均值 | 超额中位 | 亏损概率 | 左尾p5 | 最差 | 链r20均值 | 市场20日均值 |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for r in state_table.to_dict('records'):
        if r.get('n', 0) == 0:
            lines.append(f"| {r['state']} | {STATE_DESC[r['state']]} | 0 | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r['state']} | {STATE_DESC[r['state']]} | {r['n']} "
            f"| {fmt(r['mean_excess'])} | {fmt(r['median_excess'])} "
            f"| {fmt(r['loss_prob'])} | {fmt(r['p5_excess'])} | {fmt(r['min_excess'])} "
            f"| {fmt(r['mean_chain_r20'])} | {fmt(r['mean_mkt_r20'])} |")
    lines += [
        '', f"D状态轮明细（{len(d_rounds)} 轮）：",
        '| 信号日 | 链r20 | 市场20日 | 超额 |', '| --- | --- | --- | --- |',
    ]
    for rec in d_rounds.itertuples():
        lines.append(f"| {rec.signal_day.date()} | {fmt(rec.chain_r20)} "
                     f"| {fmt(rec.mkt_r20)} | {fmt(rec.excess)} |")
    lines += [
        '', '## 边界', '',
        '收盘近似；沪深300代理市场；状态边界（恰好0）归入上涨侧；120日历史不足的前期剔除；'
        '轮间不重叠，统计不修正自相关。D状态仅覆盖个别历史事件（含2024-09风格崩塌单一样本），'
        '样本量不足以训练或验证实时规则，本探针只回答"历史分状态下动量链表现是否有梯度"。', '',
        '明细见 ' + str(OUT.name) + '/（round_states、state_table、d_rounds、summary.json）。',
    ]
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
