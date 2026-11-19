# -*- coding: utf-8 -*-
"""解析 pool_audit.py 在聚宽输出的 [AUDIT] 日志，产出证据报告。

用法：
    python research/studies/quality_momentum/audit_parse.py <日志文件> [--out 报告路径]

日志可以直接从聚宽回测的「日志」面板全选复制、存成 txt。
非 [AUDIT] 行一律忽略，因此混着平台自己的输出也没关系。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TAG = re.compile(r'\[AUDIT\]([A-Z][A-Z0-9]*)\s*(.*)')

DOC = {'total': 2.8381, 'cagr': 0.1386, 'maxdd': -0.2694, 'trades': 112}
POST_MARKS = (20, 60, 120)


def parse(path: Path) -> dict:
    data = {'CFG': '', 'EQ': {}, 'EQ0': None, 'WGT': {}, 'POOL': {}, 'BMK': {}, 'BMK0': None,
            'TRD': [], 'FILL': [], 'FILLSTAT': {}, 'EXR': {}, 'YR': [], 'SUM': {}, 'ERR': [], 'POST': [], 'ENTRY': [], 'MAE': [],
            'CORP': [], 'INIT': None, 'END': False}
    for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        match = TAG.search(line)
        if not match:
            continue
        tag, body = match.group(1), match.group(2).strip()
        # 压缩日志把多条记录用 ~ 连成一行（不能用 |，出池原因本身可能含 |）
        records = [rec for rec in body.split('~') if rec.strip()]
        if tag == 'CFG':
            data['CFG'] = body
        elif tag == 'INIT':
            data['INIT'] = float(body)
        elif tag == 'END':
            data['END'] = True
        elif tag in ('EQ', 'POOL', 'BMK'):
            for rec in records:
                try:
                    key, value = rec.split(',')
                    data[tag][key] = int(value) if tag == 'POOL' else float(value)
                except ValueError:
                    continue
        elif tag == 'EQ0':
            data['EQ0'] = body
        elif tag == 'WGT':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 5:
                    continue
                data['WGT'][parts[0]] = dict(zip(('count', 'invested', 'max_w', 'hhi'),
                                                 (int(parts[1]), float(parts[2]), float(parts[3]),
                                                  float(parts[4]))))
        elif tag == 'BMK0':
            data['BMK0'] = body.split(',')[1]
        elif tag == 'TRD':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 9:
                    data['ERR'].append('TRD 字段不足：%s' % rec[:60])
                    continue
                data['TRD'].append({'code': parts[0], 'entry': parts[1], 'exit': parts[2],
                                    'entry_px': float(parts[3]), 'exit_px': float(parts[4]),
                                    'shares': int(parts[5]), 'pnl': float(parts[6]),
                                    'hold_td': int(parts[7]), 'reason': parts[8],
                                    'ret_cash': float(parts[9]) if len(parts) > 9 else np.nan})
        elif tag == 'FILL':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 6:
                    continue
                data['FILL'].append({'code': parts[0], 'day': parts[1], 'quote': float(parts[2]),
                                     'fill': float(parts[3]), 'bar_open': float(parts[4]),
                                     'pos_price': float(parts[5])})
        elif tag == 'FILLS':
            parts = body.split(',')
            if parts and '=' in body:
                item = dict(p.split('=', 1) for p in parts if '=' in p)
                data['FILLSTAT'][parts[0]] = {k: float(v) for k, v in item.items()}
        elif tag == 'MAE':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 3:
                    continue
                data['MAE'].append({'code': parts[0], 'entry': parts[1], 'mae': float(parts[2])})
        elif tag == 'ENTRY':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 6:
                    continue
                data['ENTRY'].append({'code': parts[0], 'entry': parts[1], 'growth': float(parts[2]),
                                    'roe': float(parts[3]), 'pe': float(parts[4]), 'pb': float(parts[5])})
        elif tag == 'POST':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 5:
                    continue
                data['POST'].append({'code': parts[0], 'reason': parts[1], 'day': parts[2],
                                    'age': int(parts[3]), 'ret': float(parts[4])})
        elif tag == 'EXR':
            for rec in records:
                try:
                    reason, count = rec.split(',')
                    data['EXR'][reason] = int(count)
                except ValueError:
                    continue
        elif tag == 'YR':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 5:
                    continue
                data['YR'].append({'year': int(parts[0]), 'ret': float(parts[1]),
                                   'dd': float(parts[2]), 'worst_day': parts[3],
                                   'trades': int(parts[4])})
        elif tag == 'CORP':
            for rec in records:
                parts = rec.split(',')
                if len(parts) < 3:
                    continue
                data['CORP'].append({'code': parts[0], 'day': parts[1], 'shares': int(parts[2])})
        elif tag == 'SUM':
            for item in body.split():
                key, value = item.split('=')
                data['SUM'][key] = float(value)
        elif tag == 'ERR':
            data['ERR'].append(body)
    return data


def monthly_frames(data: dict):
    equity = pd.Series(data['EQ'], dtype='float64').sort_index()
    if equity.empty:
        return equity, pd.Series(dtype='float64')
    bench = pd.Series(data['BMK'], dtype='float64').reindex(equity.index)
    if data['BMK0'] and bench.isna().all():
        bench = bench.fillna(float(data['BMK0']))
    return equity, bench


def yearly_from_monthly(equity: pd.Series, bench: pd.Series, base_bench: float | None,
                        base_strategy: float | None = None):
    rows = []
    for year in sorted({int(k[:4]) for k in equity.index}):
        keys = [k for k in equity.index if k[:4] == str(year)]
        prev_keys = [k for k in equity.index if k < keys[0]]
        strat_start = equity[prev_keys[-1]] if prev_keys else (base_strategy or equity[keys[0]])
        bench_start = bench[prev_keys[-1]] if prev_keys else base_bench
        bench_end = bench[keys[-1]]
        rows.append({'year': year,
                     'ret': equity[keys[-1]] / strat_start - 1.0,
                     'bench': (bench_end / bench_start - 1.0) if bench_start else np.nan})
    frame = pd.DataFrame(rows).set_index('year')
    frame['excess'] = frame['ret'] - frame['bench']
    return frame


def rolling_sharpe(equity: pd.Series, window: int = 36):
    monthly = equity.pct_change().dropna()
    if len(monthly) < window:
        return pd.Series(dtype='float64')
    mean = monthly.rolling(window).mean()
    std = monthly.rolling(window).std(ddof=1)
    return (mean / std * np.sqrt(12)).dropna()


def alpha_beta(equity: pd.Series, bench: pd.Series):
    ret = equity.pct_change()
    bmk = bench.pct_change()
    both = pd.concat([ret, bmk], axis=1).dropna()
    if len(both) < 12:
        return np.nan, np.nan, np.nan
    x = both.iloc[:, 1].values
    y = both.iloc[:, 0].values
    design = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    te = resid.std(ddof=2) * np.sqrt(12) if len(y) > 2 else np.nan
    ir = (y - x).mean() / (y - x).std(ddof=1) * np.sqrt(12) if (y - x).std(ddof=1) > 0 else np.nan
    return coef[0] * 12, coef[1], ir


def trade_stats(trades: list) -> dict:
    if not trades:
        return {}
    pnl = np.array([t['pnl'] for t in trades], dtype='float64')
    gains = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    total = pnl.sum()
    ordered = np.sort(pnl)[::-1]
    top5 = ordered[:5].sum() / total if total else np.nan
    top1 = ordered[:1].sum() / total if total else np.nan
    return {
        'n': len(pnl),
        'win_rate': len(gains) / len(pnl),
        'avg_win': gains.mean() if len(gains) else 0.0,
        'avg_loss': abs(losses.mean()) if len(losses) else np.nan,
        'pl_ratio': (gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else np.nan,
        'expectancy': pnl.mean(),
        'median_hold': float(np.median([t['hold_td'] for t in trades])),
        'max_hold': int(np.max([t['hold_td'] for t in trades])),
        'top1_share': top1,
        'top5_share': top5,
        'gross_total': total,
    }


def section(title: str) -> str:
    return '## %s\n\n' % title


def build_report(data: dict, path: Path) -> str:
    equity, bench = monthly_frames(data)
    out = ['# 质量池审计报告\n\n', '- 来源日志：`%s`\n- 配置：`%s`\n' % (path.name, data['CFG'] or '未读取到'), '\n']
    if not data.get('END'):
        out.append('- **日志里没有 `[AUDIT]END` 收尾标记，可能被平台截断，下面统计不完整**\n\n')
    flags = []

    if data['ERR']:
        out.append(section('异常行'))
        out.append('```\n%s\n```\n\n' % '\n'.join(data['ERR']))

    # ── 1 自检 ──
    summary = data['SUM']
    init = summary.get('init') or data.get('INIT')
    if init and not equity.empty:
        # 用「初始资金 → 期末权益」重算累计与年化，避免首日盈亏被吞掉
        final = float(equity.iloc[-1])
        summary['total'] = final / init - 1.0
        span = (summary.get('days') or 0) / 250.0 or summary.get('span') or (len(equity) / 12.0)
        summary['cagr'] = (final / init) ** (1.0 / span) - 1.0
    opened_at_end = sum(1 for t in data['TRD'] if t['reason'] == 'open_at_end')
    closed = (summary.get('trades', 0) - opened_at_end) if summary.get('trades') is not None else None
    out.append(section('一、自检（与 docstring 基线对比）'))
    rows = [('累计收益', summary.get('total'), DOC['total'], '4.5%'),
            ('年化收益', summary.get('cagr'), DOC['cagr'], '4.5%'),
            ('最大回撤', summary.get('maxdd'), DOC['maxdd'], '4.5%'),
            ('平仓笔数', closed, DOC['trades'], '9.0%')]
    out.append('| 指标 | 本次 | 基线 | 偏差 |\n|---|---|---|---|\n')
    for name, actual, expect, tol in rows:
        if actual is None:
            out.append('| %s | 未输出 | %s | - |\n' % (name, expect))
            continue
        dev = abs(actual - expect) / abs(expect) if expect else np.nan
        out.append('| %s | %.4f | %.4f | %.1f%% |\n' % (name, actual, expect, dev * 100))
        if dev > 0.05 and name in ('累计收益', '年化收益', '最大回撤'):
            flags.append('%s 与基线偏差 %.1f%%，埋点可能改变了执行路径，需先排查' % (name, dev * 100))
    if summary.get('init'):
        out.append('\n- 初始资金 %.0f｜期末权益 %.0f｜全期毛盈亏台账 %.0f\n'
                   % (summary['init'],
                      summary['init'] * (1 + summary.get('total', 0)),
                      sum(t['pnl'] for t in data['TRD'])))
    if opened_at_end:
        out.append('- 平仓笔数已剔除期末未平仓结转的 %d 笔（台账总笔数 %d）\n' % (opened_at_end, summary.get('trades')))
    out.append('\n')

    # ── 2 逐年 ──
    if equity.empty:
        out.append('\n未读取到月末净值 `[AUDIT]EQ`，后续统计无法进行。\n')
        return ''.join(out)

    bench_base = float(data['BMK0']) if data['BMK0'] else (bench.dropna().iloc[0] if bench.notna().any() else None)
    # 首年基准用初始资金，而不是首日权益（首日权益已包含当天盈亏）
    strat_base = data.get('INIT') or summary.get('init') or (float(data['EQ0'].split(',')[1]) if data['EQ0'] else None)
    bench = bench.ffill() if bench.notna().any() else bench
    yearly = yearly_from_monthly(equity, bench, bench_base, strat_base)
    if data['YR']:
        yr = pd.DataFrame(data['YR']).set_index('year')
        yearly = yearly.join(yr[['dd', 'worst_day', 'trades']])

    out.append(section('二、逐年表现'))
    out.append('| 年份 | 策略 | 基准 | 超额 | 年内最大回撤 | 回撤低点 | 平仓笔数 |\n|---|---|---|---|---|---|---|\n')
    for year, row in yearly.iterrows():
        out.append('| %d | %+.2f%% | %+.2f%% | %+.2f%% | %s | %s | %s |\n'
                   % (year, row['ret'] * 100, row['bench'] * 100, row['excess'] * 100,
                      '%.2f%%' % (row['dd'] * 100) if 'dd' in row and pd.notna(row['dd']) else '-',
                      row.get('worst_day', '-') if 'worst_day' in row else '-',
                      int(row['trades']) if 'trades' in row and pd.notna(row['trades']) else '-'))
    negative_years = int((yearly['ret'] < 0).sum())
    under_years = int((yearly['excess'] < 0).sum())
    out.append('\n%d 个完整年度中，%d 年绝对收益为负，%d 年跑输基准。\n\n' % (len(yearly), negative_years, under_years))

    # ── 3 月度与滚动 ──
    monthly = equity.pct_change().dropna()
    bmonthly = bench.pct_change().reindex(monthly.index)
    alpha, beta, ir = alpha_beta(equity, bench)
    roll = rolling_sharpe(equity)
    out.append(section('三、月度分布与滚动稳定性'))
    out.append('- 月度胜率：%.1f%%（%d/%d）\n' % ((monthly > 0).mean() * 100, int((monthly > 0).sum()), len(monthly)))
    out.append('- 月度年化 alpha：%.2f%%｜beta：%.2f｜信息比率：%.2f\n' % (alpha * 100, beta, ir))
    if len(roll):
        out.append('- 滚动 36 月夏普：中位 %.2f｜最差 %.2f（截至 %s）｜为负月份占比 %.0f%%\n'
                   % (roll.median(), roll.min(), roll.idxmin(), (roll < 0).mean() * 100))
        if roll.min() < 0:
            flags.append('滚动 36 月夏普在 %s 处转负，策略存在明确的失效窗口' % roll.idxmin())
    worst = monthly.nsmallest(8).sort_index()
    out.append('\n最差 8 个月：\n\n| 月份 | 策略 | 基准 | 超额 |\n|---|---|---|---|\n')
    for key, value in worst.items():
        b = bmonthly.get(key, np.nan)
        out.append('| %s | %+.2f%% | %s | %+.2f%% |\n'
                   % (key, value * 100, '%+.2f%%' % (b * 100) if pd.notna(b) else '-',
                      (value - b) * 100 if pd.notna(b) else np.nan))
    out.append('\n')

    # ── 4 成交口径 ──
    out.append(section('四、成交口径探针'))
    st = data.get('FILLSTAT') or {}
    if not data['FILL'] and not st:
        out.append('未读取到成交价探针，无法判断平台的建仓成交价口径。\n\n')
    else:
        if data['FILL']:
            fills = pd.DataFrame(data['FILL'])
            for column, ref in (('open', 'bar_open'), ('close', 'pos_price'), ('quote', 'quote')):
                dev = (fills['fill'] / fills[ref] - 1) * 10000
                n = len(fills)
                vals = {'n': n, 'abs_med': dev.abs().median(), 'sign_med': dev.median(),
                        'pos': (dev > 0).mean()}
                st[column] = vals
        n = int(st.get('quote', st.get('open', {})).get('n', 0))
        out.append('- 建仓样本 %d 笔\n' % n)
        for key, label in (('open', '当日开盘价'), ('close', '当日收盘价'), ('quote', '09:31 报价')):
            item = st.get(key)
            if not item:
                out.append('- 成交价与%s偏离：缺少数据\n' % label)
                continue
            out.append('- 成交价与%s偏离中位：%.0f bp（带符号 %+.0f bp%s）\n'
                       % (label, item['abs_med'], item['sign_med'],
                          '，正向占比 %.0f%%' % (item['pos'] * 100) if key == 'quote' else ''))
        out.append('\n')
        quote_item = st.get('quote')
        open_item = st.get('open')
        close_item = st.get('close')
        if quote_item and quote_item['abs_med'] <= 30 and quote_item['pos'] >= 0.90:
            out.append('判定：**按 09:31 报价成交，并叠加约 %+.0f bp 的买方向滑点**。'
                       '平台撮合用的就是下单时刻的报价，不是收盘价，因此 09:31 的涨跌停与停牌过滤'
                       '用的是同一时刻的价格，逻辑自洽。\n\n' % quote_item['sign_med'])
            if open_item and close_item:
                out.append('- 与开盘价、收盘价的 %.0f / %.0f bp 偏离，反映的是这些标的当日自身的跳空与波幅，'
                           '不是成交机制的偏差。\n\n' % (open_item['abs_med'], close_item['abs_med']))
        elif open_item and open_item['abs_med'] <= 10 and (not close_item or open_item['abs_med'] <= close_item['abs_med']):
            out.append('判定：**按开盘价成交**，与实盘 09:31 开盘执行基本一致。\n\n')
        elif close_item and close_item['abs_med'] <= 10:
            out.append('判定：**按当日收盘价成交**。\n\n')
            flags.append('日频回测按收盘价成交，而实盘是 09:31 开盘执行：回测收益含当日盘中约 4 小时的信息，'
                         '实盘拿不到；这段差异必须在评价收益前扣掉')
        else:
            out.append('判定：成交价与开收盘都不吻合，口径不明，需要补行情再核。\n\n')

    # ── 5 交易 ──
    stats = trade_stats(data['TRD'])
    out.append(section('五、逐笔交易结构'))
    if not stats:
        out.append('未读取到 `[AUDIT]TRD`。\n\n')
    else:
        out.append('- 台账 %d 笔（含期末未平仓）｜胜率 %.1f%%｜平均盈利 %.0f 元｜平均亏损 %.0f 元｜盈亏比 %.2f\n'
                   % (stats['n'], stats['win_rate'] * 100, stats['avg_win'],
                      stats['avg_loss'] if np.isfinite(stats['avg_loss']) else 0,
                      stats['pl_ratio'] if np.isfinite(stats['pl_ratio']) else 0))
        out.append('- 单笔期望 %.0f 元｜持仓中位数 %d 个交易日｜最长 %d 个交易日\n'
                   % (stats['expectancy'], stats['median_hold'], stats['max_hold']))
        corp = data.get('CORP') or []
        if corp:
            out.append('- **送转校正**：%d 只票共 %d 次股数增加，已按"不记成本"处理（未校正前这部分会把盈利记成亏损）\n'
                       % (len({c['code'] for c in corp}), len(corp)))
        init = summary.get('init') or 0
        gain = init * summary.get('total', 0)
        top5_abs = sorted((t['pnl'] for t in data['TRD']), reverse=True)[:5]
        top5_sum = sum(top5_abs)
        if gain > 0:
            out.append('- 前 5 笔毛利润合计 %.0f 元，相当于全期权益增加额（%.0f 元）的 %.1f%%\n'
                       % (top5_sum, gain, top5_sum / gain * 100))
        out.append('- 按台账口径：最大单笔占台账总盈利 %.1f%%｜前 5 笔合计 %.1f%%\n'
                   % (stats['top1_share'] * 100, stats['top5_share'] * 100))
        if gain > 0 and abs(sum(t['pnl'] for t in data['TRD'])) > 0:
            ratio = gain / sum(t['pnl'] for t in data['TRD'])
            out.append('- 台账毛盈亏合计仅相当于权益增加额的 %.0f%%，说明台账未计入分红与部分持有收益，'
                       '**集中度以「权益增加额」为分母的那一行更可靠**\n' % (100 / ratio))
        opened = sum(1 for t in data['TRD'] if t['reason'] == 'open_at_end')
        out.append('- 盈亏为按 09:31 委托价估算的**毛盈亏**（不含佣金、印花税、滑点、分红）；其中 %d 笔为期末未平仓、按最后价结转\n\n'
                   % opened)
        if gain > 0 and top5_sum / gain > 0.40:
            flags.append('前 5 笔毛利润占权益增加额 %.0f%%，收益高度集中于少数交易，'
                         '任何一笔的执行或数据偏差都会改写下场'
                         % (top5_sum / gain * 100))
        if data['EXR']:
            out.append('| 出池原因 | 笔数 | 占比 |\n|---|---|---|\n')
            total_exit = sum(data['EXR'].values())
            for reason, count in sorted(data['EXR'].items(), key=lambda kv: -kv[1]):
                out.append('| %s | %d | %.1f%% |\n' % (reason, count, count / total_exit * 100))
            out.append('\n')
        by_year = pd.Series([int(t['exit'][:4]) for t in data['TRD']]).value_counts().sort_index()
        out.append('平仓笔数按年份：%s\n\n' % '，'.join('%d年 %d 笔' % (y, c) for y, c in by_year.items()))

        out.append('### 执行容差（人工下单糙到什么程度这个策略就不成立）\n\n')
        years = max(len(equity) / 12.0, 1e-6)
        avg_equity = float(equity.mean())
        notional = sum(t['shares'] * (t['entry_px'] + t['exit_px']) for t in data['TRD'])
        turnover = notional / avg_equity / years if avg_equity > 0 else np.nan
        out.append('- 年均换手（双边成交额 / 平均权益）：%.2f 倍\n' % turnover)
        out.append('- 平均每笔调仓的单边金额约占总权益 %.1f%%\n\n'
                   % (1.0 / 6.0 * 100))
        base_cagr = (summary.get('cagr') or 0.1386) * 100
        out.append('| 单边额外摩擦 | 年化拖累 | %.1f%% 年化折后 |\n|---|---|---|\n' % base_cagr)
        for bp in (0.002, 0.005, 0.01, 0.02, 0.03):
            drag = turnover * bp
            out.append('| %.1f%% | -%.2f 个百分点 | %.1f%% |\n'
                       % (bp * 100, drag * 100, base_cagr - drag * 100))
        out.append('\n回测的 0.2% 价差参数相当于单边约 0.1%，已计入上述净值；'
                   '上表是“比回测再糙多少”的边际影响。\n\n')

    # ── 5c 入场时离门槛多近 ──
    entry = data.get('ENTRY') or []
    if entry:
        ef = pd.DataFrame(entry)
        ef = ef[ef['growth'] > -900]
        trades = pd.DataFrame(data['TRD'])
        merged = ef.merge(trades, on=['code', 'entry'], how='inner')
        out.append('### 入场时离门槛有多近（决定该不该收紧入场）\n\n')
        if merged.empty:
            out.append('`[AUDIT]ENTRY` 与台账未能按（代码+建仓日）对上，样本不足。\n\n')
        else:
            merged['killed'] = merged['reason'].str.contains('profit_growth_low', na=False)
            out.append('| 入场增速 | 笔数 | 被增速门槛清掉 | 中位持有 | 中位盈亏 | 平均盈亏 |\n'
                       '|---|---|---|---|---|---|\n')
            for label, lo, hi in (('10–15%', 10, 15), ('15–20%', 15, 20),
                                  ('20–30%', 20, 30), ('>30%', 30, 1e9)):
                part = merged[(merged['growth'] >= lo) & (merged['growth'] < hi)]
                if not part.empty:
                    out.append('| %s | %d | %.0f%% | %.0f 天 | %+.0f 元 | %+.0f 元 |\n'
                               % (label, len(part), part['killed'].mean() * 100,
                                  part['hold_td'].median(), part['pnl'].median(),
                                  part['pnl'].mean()))
            out.append('| 合计 | %d | %.0f%% | %.0f 天 | %+.0f 元 | %+.0f 元 |\n\n'
                       % (len(merged), merged['killed'].mean() * 100, merged['hold_td'].median(),
                          merged['pnl'].median(), merged['pnl'].mean()))
            low = merged[merged['growth'] < 15]
            high = merged[merged['growth'] >= 20]
            if not low.empty and not high.empty:
                out.append('- 入场增速低于 15%% 的 %d 笔，被增速门槛清掉 %.0f%%；'
                           '高于 20%% 的 %d 笔，被清掉 %.0f%%\n'
                           % (len(low), low['killed'].mean() * 100,
                              len(high), high['killed'].mean() * 100))
                if low['killed'].mean() > high['killed'].mean() + 0.15:
                    flags.append('入场增速贴着 10%% 门槛的票，被增速门槛清掉的概率比高增速组高 '
                                 '%.0f 个百分点：问题在"买得太将就"，收紧入场有据'
                                 % ((low['killed'].mean() - high['killed'].mean()) * 100))
                else:
                    out.append('- 两组的淘汰率差距不足 15 个百分点，说明被杀不是因为入场时贴线，'
                               '收紧增速门槛大概率无益\n')
            out.append('\n')

    # ── 5d 如果加止损 ──
    mae = data.get('MAE') or []
    if mae:
        mf = pd.DataFrame(mae)
        trades = pd.DataFrame(data['TRD'])
        m = trades.merge(mf, on=['code', 'entry'], how='inner')
        m = m[m['reason'] != 'open_at_end']
        out.append('### 如果加个股止损（用持有期内最大不利偏移模拟）\n\n')
        if m.empty:
            out.append('`[AUDIT]MAE` 与台账未能对上，样本不足。\n\n')
        else:
            m['val'] = m['shares'] * m['entry_px']
            # 收益率优先用策略侧记的现金流口径（pnl / 成本），它天然抗除权；
            # 退化时才用价差，那个口径在送转票上是错的。
            m['ret'] = m['ret_cash'].where(m['ret_cash'].notna(),
                                           m['exit_px'] / m['entry_px'] - 1)
            out.append('| 止损线 | 触发笔数 | 其中最终赚钱（误杀） | 误杀掉的利润 | 组合盈亏变化 |\n'
                       '|---|---|---|---|---|\n')
            for th in (0.05, 0.08, 0.10, 0.15, 0.20):
                trig = m['mae'] <= -th
                sim = np.where(trig, -th, m['ret'])
                delta = (m['val'] * (sim - m['ret'])).sum()
                killed = m[trig & (m['ret'] > 0)]
                lost = (killed['val'] * (killed['ret'] + th)).sum()
                out.append('| -%.0f%% | %d | %d | %+.0f 元 | %+.0f 元 |\n'
                           % (th * 100, int(trig.sum()), len(killed), lost, delta))
            out.append('\n判读：`组合盈亏变化` 是"把触发的那部分仓位截断在止损线"相对原策略的差值，'
                       '正数说明止损赚钱。它**不含**腾出的现金另买别的票的收益，也不含止损后的再建仓成本，'
                       '所以只能当上界参考。\n\n')
            win = m[m['ret'] > 0.2]
            if not win.empty:
                out.append('- 最终收益 >20%% 的 %d 笔赢家，其中 %.0f%% 曾在持有期内跌破 -10%%，'
                           '%.0f%% 跌破 -20%%（跌破 -10%% 的止损线就会把它们砍在坑里）\n'
                           % (len(win), (win['mae'] <= -0.10).mean() * 100,
                              (win['mae'] <= -0.20).mean() * 100))
                flags_w = win[win['mae'] <= -0.10]
                if not flags_w.empty:
                    flags.append('%d 笔大赢家在启动前曾跌破 -10%%（最深 %.0f%%），'
                                 '-10%% 止损会把它们砍在坑里'
                                 % (len(flags_w), flags_w['mae'].min() * 100))
            out.append('\n')
        if turnover * 0.01 > 0.05:
            flags.append('执行容差很窄：单边多滑 1%% 就吃掉 %.1f 个百分点的年化，人工延迟与冲击成本必须是可控的'
                         % (turnover * 1.0))

    # ── 5b 卖出之后 ──
    out.append(section('六、卖出之后还涨不涨（判断卖出规则是否切早了）'))
    post = data.get('POST') or []
    if not post:
        out.append('未读取到 `[AUDIT]POST`。这一节是唯一能区分「卖出规则创造了收益」'
                   '和「卖出规则只是恰好在上涨后兑现」的证据，缺它就只剩记账。\n\n')
    else:
        frame = pd.DataFrame(post)
        covered = frame.groupby(['code', 'day']).ngroups
        out.append('- 跟踪样本 %d 条，覆盖 %d 笔卖出（每笔最多 3 个时间点）\n' % (len(frame), covered))
        out.append('\n**按出场原因分组：卖出后 %s 个交易日的涨跌幅中位数**\n\n'
                   % '/'.join(str(m) for m in POST_MARKS))
        out.append('| 出场原因 | 笔数 | %s | 卖出后上涨占比 | 判定 |\n|---|---|---|---|---|\n'
                   % ' | '.join('后 %d 日' % m for m in POST_MARKS))
        for reason, part in frame.groupby('reason'):
            if len(part) < 3:
                continue
            cells = []
            meds = {}
            for m in POST_MARKS:
                seg = part[part['age'] == m]['ret']
                med = seg.median() * 100 if len(seg) else np.nan
                meds[m] = med
                cells.append('-' if pd.isna(med) else '%+.1f%%' % med)
            up = (part[part['age'] == max(POST_MARKS)]['ret'] > 0).mean() * 100
            mid = meds.get(60, np.nan)
            if pd.isna(mid):
                verdict = '样本不足'
            elif mid > 5:
                verdict = '**切早了**：卖掉后还在涨'
            elif mid < -5:
                verdict = '**及时**：卖掉后继续跌'
            else:
                verdict = '差不多'
            out.append('| %s | %d | %s | %.0f%% | %s |\n'
                       % (reason, len(part) // len(POST_MARKS), ' | '.join(cells),
                          up, verdict))
        out.append('\n判定口径：按各出场原因分组，看卖出后 60 个交易日的漂移。'
                   '因为 PB 或 PE 触顶必然意味着价格已经涨过，「触顶出场全是盈利」是同义反复，'
                   '不能当作收益来源的证据；只有在卖出**之后**还涨，才说明这条规则切早了。\n\n')
        flagged = []
        for reason, part in frame.groupby('reason'):
            seg = part[part['age'] == 60]['ret']
            if len(seg) >= 5 and seg.median() > 0.05:
                flagged.append(reason)
        if flagged:
            flags.append('这些卖出规则卖出后 60 日仍在上涨 5%% 以上，疑似过早在兑现：%s'
                         % '、'.join(flagged))

    # ── 6 集中度 ──
    out.append(section('七、仓位集中度（权重漂移的实际后果）'))
    wgt = pd.DataFrame(data['WGT'].values(), index=list(data['WGT'].keys()))
    if wgt.empty:
        out.append('未读取到 `[AUDIT]WGT`。\n\n')
    else:
        breach35 = (wgt['max_w'] > 0.35).mean()
        breach45 = (wgt['max_w'] > 0.45).mean()
        out.append('- 平均最大单票权重 %.1f%%｜历史最高 %.1f%%\n'
                   % (wgt['max_w'].mean() * 100, wgt['max_w'].max() * 100))
        out.append('- 平均仓位 %.1f%%｜平均持仓只数 %.1f\n'
                   % (wgt['invested'].mean() * 100, wgt['count'].mean()))
        out.append('- 单票权重超过 35%% 的月份占比 %.0f%%，超过 45%% 的占比 %.0f%%\n\n'
                   % (breach35 * 100, breach45 * 100))
        if breach45 > 0.05:
            flags.append('有 %.0f%% 的月份单票权重超过 45%%，尾部风险实际未受约束' % (breach45 * 100))
        if wgt['invested'].mean() < 0.85:
            flags.append('平均仓位仅 %.0f%%，现金拖累明显' % (wgt['invested'].mean() * 100))

    # ── 6 池规模 ──
    out.append(section('八、质量池宽度'))
    pool = pd.Series(data['POOL'], dtype='float64').sort_index()
    if pool.empty:
        out.append('未读取到 `[AUDIT]POOL`。\n\n')
    else:
        by_year_pool = pool.groupby([k[:4] for k in pool.index]).agg(['mean', 'min', 'max'])
        out.append('| 年份 | 月均只数 | 最小 | 最大 |\n|---|---|---|---|\n')
        for year, row in by_year_pool.iterrows():
            out.append('| %s | %.0f | %.0f | %.0f |\n' % (year, row['mean'], row['min'], row['max']))
        out.append('\n全期：月均 %.0f 只，最小 %d 只，最大 %d 只。从 %d 只里取前 6 名，等于取前 %.0f%%。\n\n'
                   % (pool.mean(), pool.min(), pool.max(), pool.min(), 6 / pool.min() * 100))
        if pool.min() < 30:
            flags.append('池子最小只有 %d 只，横截面信号过薄，排名近乎无统计意义' % pool.min())

    # ── 7 结论 ──
    out.append(section('九、诊断标记'))
    if flags:
        for flag in flags:
            out.append('- **%s**\n' % flag)
    else:
        out.append('- 未触发预设的异常阈值。\n')
    out.append('\n阈值口径：基线偏差 >5%、前 5 笔贡献 >40%、单票 >45% 月份 >5%、平均仓位 <85%、池规模 <30 只、滚动 36 月夏普转负。\n')
    return ''.join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description='解析质量池审计日志')
    parser.add_argument('log', type=Path, help='聚宽导出的日志文件')
    parser.add_argument('--out', type=Path, default=None, help='报告输出路径')
    parser.add_argument('--init', type=float, default=None,
                        help='初始资金。仅用于补旧日志（缺 [AUDIT]INIT 行时，用回测设置的初始资金）')
    args = parser.parse_args()
    if not args.log.exists():
        print('找不到日志：%s' % args.log, file=sys.stderr)
        return 1
    data = parse(args.log)
    if args.init is not None:
        data['INIT'] = args.init
        data['SUM']['init'] = args.init
    report = build_report(data, args.log)
    if args.out is None:
        args.out = args.log.with_suffix('.audit.md')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding='utf-8')
    print(report)
    print('已写入：%s' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())

