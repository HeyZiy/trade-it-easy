# -*- coding: utf-8 -*-
"""对比 40 日与 20 日调仓周期（用户平台导出 result_40.csv / result_20.csv）。

只读两份日频日志：时间、基准收益、策略收益（累计%）、当日盈亏、买卖额、cash_pct、
holdings、turnover_pct。费用明细、订单、持有期不在日志内，做边界声明。
result_20 期末收益与既有 v4_1 导出一致，作为交叉核验。
运行：python research/studies/quality_momentum/internal/analyze_rotate_period_v4_1.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
OUT = STUDY / 'reports' / 'rotate_period_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'rotate_period_v4_1_2026-10-10.md'
EXPORTS = STUDY / 'exports'
FILES = {'rotate20': EXPORTS / 'result_20.csv', 'rotate40': EXPORTS / 'result_40.csv'}
NAV_V41 = EXPORTS / 'pool_rotation_v4_1_nav.csv'
INITIAL = 100000.0
ANNUAL_DAYS = 245.0


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    frame = pd.read_csv(path, encoding='gb18030')
    frame['时间'] = pd.to_datetime(frame['时间'])
    require(not frame['时间'].duplicated().any() and frame['时间'].is_monotonic_increasing,
            f'{path.name}日期重复/乱序')
    frame['equity'] = INITIAL * (1 + frame['策略收益'] / 100)
    frame['benchmark'] = INITIAL * (1 + frame['基准收益'] / 100)
    frame['return'] = frame.equity.pct_change().fillna(0)
    frame['drawdown'] = frame.equity / frame.equity.cummax() - 1
    relative = frame.equity / frame.benchmark
    frame['relative_dd'] = relative / relative.cummax() - 1
    return frame


def metrics(frame, name):
    days = len(frame)
    years = days / ANNUAL_DAYS
    r = frame['return']
    turnover = (frame['当日买入'] + frame['当日卖出']).sum()
    return dict(version=name, end_equity=float(frame.equity.iloc[-1]),
                cumulative_return=float(frame.equity.iloc[-1] / INITIAL - 1),
                annual_return=float((frame.equity.iloc[-1] / INITIAL) ** (1 / years) - 1),
                max_drawdown=float(frame.drawdown.min()),
                relative_max_drawdown=float(frame.relative_dd.min()),
                annual_vol=float(r.std(ddof=1) * np.sqrt(ANNUAL_DAYS)),
                sharpe=float(r.mean() / r.std(ddof=1) * np.sqrt(ANNUAL_DAYS)),
                average_cash_pct=float(frame.cash_pct.mean()),
                average_holdings=float(frame.holdings.mean()),
                turnover_total=float(turnover),
                turnover_per_year=float(turnover / years),
                trade_days=int((frame['当日买入'].gt(0) | frame['当日卖出'].gt(0)).sum()))


def peak_before(frame, trough_pos):
    peak_pos = frame.equity.iloc[:trough_pos + 1].idxmax()
    return str(frame.loc[peak_pos, '时间'].date())


def annual_table(frame, name):
    rows = []
    for year, idx in frame.groupby(frame['时间'].dt.year).groups.items():
        rows.append(dict(year=int(year), **{name: float((1 + frame.loc[idx, 'return']).prod() - 1)},
                         days=len(idx)))
    return rows


def main():
    frames = {name: load(path) for name, path in FILES.items()}
    a, b = frames['rotate20'], frames['rotate40']
    require(a['时间'].equals(b['时间']), '两版日历不同')
    require(np.allclose(a.benchmark, b.benchmark), '两版基准不同')
    # 交叉核验：result_20 与既有 v4_1 导出
    nav = pd.read_csv(NAV_V41, encoding='utf-8-sig')
    nav['date'] = pd.to_datetime(nav.date)
    merged = a.merge(nav[['date', 'equity']], left_on=a['时间'].dt.normalize(),
                     right_on='date', how='inner')
    require(len(merged) == len(nav), f'交叉核验日期未全覆盖: {len(merged)}/{len(nav)}')
    nav_diff = float((merged.equity_x - merged.equity_y).abs().max())
    require(nav_diff < 50, f'result_20 与 v4_1 导出权益差异过大: {nav_diff}')

    m20, m40 = metrics(a, 'rotate20'), metrics(b, 'rotate40')
    for m in (m20, m40):
        trough = a if m['version'] == 'rotate20' else b
        pos = trough.drawdown.idxmin()
        m['max_dd_start'] = peak_before(trough, pos)
        m['max_dd_end'] = str(trough.loc[pos, '时间'].date())
    perf = pd.DataFrame([m20, m40])
    annual = pd.DataFrame(annual_table(a, 'rotate20')).merge(
        pd.DataFrame(annual_table(b, 'rotate40')), on=['year', 'days'])
    annual['diff_40_minus_20'] = annual.rotate40 - annual.rotate20

    summary = dict(
        date='2026-10-10', period='2016-01-04至2026-09-01', initial_equity=INITIAL,
        days=len(a), nav_cross_check_max_abs_diff=nav_diff,
        performance={m['version']: {k: v for k, v in m.items() if k != 'version'}
                     for m in (m20, m40)},
        annual={str(r.year): {'rotate20': r.rotate20, 'rotate40': r.rotate40,
                              'diff': r.diff_40_minus_20} for r in annual.itertuples()},
        sha256={path.name: sha(path) for path in FILES.values()},
        methods=dict(
            equity='权益=100000×(1+策略收益%/100)，日志收益为累计百分比，含分红与费用；'
                   '无订单/费用/持有期明细，费用与换手只能看总额',
            cross_check='result_20 与既有 pool_rotation_v4_1 导出逐日权益对照，最大绝对差'
                        f'{nav_diff:.2f}元（累计%两位小数舍入范围内）',
            baseline='rotate20即v4_1原规格（ROTATE_EVERY=20）；rotate40为唯一变更变量的对照',
            boundary='单次平台回测，无多随机种子；期限选择由离线排名探针预判，事后仅报告不重复扫描'),
    )
    OUT.mkdir(parents=True, exist_ok=True)
    perf.to_csv(OUT / 'performance.csv', index=False, encoding='utf-8-sig')
    annual.to_csv(OUT / 'annual.csv', index=False, encoding='utf-8-sig')
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    write_report(summary, perf, annual)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('sha256',)},
                     ensure_ascii=True, default=str))


def write_report(summary, perf, annual):
    perf = perf.set_index('version').to_dict('index')

    def row(m, label, key, fmt, scale=1):
        return f'| {label} | {fmt.format(m["rotate20"][key] * scale)} | {fmt.format(m["rotate40"][key] * scale)} |'

    lines = [
        '# 4.1 调仓周期对照：40日 vs 20日（2026-10-10）', '',
        f'用户在平台跑的唯一变量对照（ROTATE_EVERY=40 vs 20），区间{summary["period"]}，'
        f'{summary["days"]}个交易日，初始资金{summary["initial_equity"]:,.0f}元。'
        f'交叉核验：result_20与既有v4_1导出逐日权益最大差{summary["nav_cross_check_max_abs_diff"]:.2f}元。', '',
        '## 总体表现', '',
        '| 指标 | 20日（=v4_1基线） | 40日 |', '| --- | --- | --- |',
        row(perf, '期末权益/元', 'end_equity', '{:,.0f}'),
        row(perf, '累计收益', 'cumulative_return', '{:.2%}'),
        row(perf, '年化收益', 'annual_return', '{:.2%}'),
        row(perf, '最大回撤', 'max_drawdown', '{:.2%}'),
        row(perf, '超额最大回撤', 'relative_max_drawdown', '{:.2%}'),
        row(perf, '年化波动', 'annual_vol', '{:.2%}'),
        row(perf, '夏普', 'sharpe', '{:.3f}'),
        row(perf, '平均现金', 'average_cash_pct', '{:.1%}'),
        row(perf, '平均持仓数', 'average_holdings', '{:.2f}'),
        row(perf, '总成交额/元', 'turnover_total', '{:,.0f}'),
        row(perf, '有交易天数', 'trade_days', '{:d}'), '',
        f'最大回撤区间：20日 {m_start(summary, "rotate20")}至'
        f'{summary["performance"]["rotate20"]["max_dd_end"]}；'
        f'40日 {m_start(summary, "rotate40")}至{summary["performance"]["rotate40"]["max_dd_end"]}。', '',
        '## 分年收益', '',
        '| 年份 | 20日 | 40日 | 40减20 |', '| --- | --- | --- | --- |']
    for r in annual.itertuples():
        lines.append(f'| {r.year} | {r.rotate20:.2%} | {r.rotate40:.2%} | {r.diff_40_minus_20:+.2%} |')
    lines += [
        '', '## 读法与边界', '',
        '40日为唯一变更变量的单次对照；无多随机种子、无参数扫描，未搜其他周期。'
        '日志无订单与费用明细，费用影响只能通过成交额间接比较。'
        '回撤区间来自日频权益路径，盘中更深回撤不可见。', '',
        '## 复现', '',
        '`python research/studies/quality_momentum/internal/analyze_rotate_period_v4_1.py`（项目 .conda 环境）', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print('报告：', REPORT)


def m_start(summary, version):
    return summary['performance'][version]['max_dd_start']


if __name__ == '__main__':
    main()
