# -*- coding: utf-8 -*-
# dividend_pool_probe_v1 —— 红利资产纳入行业动量池探针 v1（2026-10-06）
#
# 问题：红利/股息 ETF 目前在名称剔除词表里（归"风格"剔）。如果把红利资产
#   放回候选，过成熟+流动+相关去重三关后，v3_3_1 规则会选中它吗？选中后
#   组合表现变好还是变差？
# 口径（复用生产引擎原函数，零重写）：
#   - 池 = filter_by_name + build_pool（成熟/流动/250 日相关贪心去重），
#     每 20 交易日重建，bars 按 as_of 截断；去重按流动性降序贪心，红利与
#     行业票公平竞争（追加进候选列表，排序在 build_pool 内完成）；
#   - 规则 = lM_v3_3_1 判定核：score>0 降序买满 3 等权空槽（per=min(现金/
#     空槽, 总值/TOPN)，一次算定）；score≤0 连续 3 日确认清仓；现价≤成本
#     ×0.92 当日清仓；出池持仓直评（数据不足继续持有）；停牌日缺数不评估。
#   - 三个口径：A=现行池；B1=+A 股红利（保留 513 结构剔除）；B2=+全部
#     红利（含港股通红利，豁免 513 与全部名称剔除词，仅按红利/股息命名命中）。
# 数据：_cache/etf_daily_full.pkl（qfq+amount，新浪在市表）。
# 已知偏差（结论须携带）：
#   - qfq 只折算份额折算跳变（|ret|>25%），不做分红除息调整——红利 ETF 年
#     分红约 4~6%，本探针系统性低估红利收益同量级（方向上不利于红利），
#     且大额除息日可能误触发跳水清零；
#   - 幸存者偏差：名单=当前在市表，已退市不在列；
#   - 本地无停牌/涨跌停约束，按收盘价成交，万 1 双边佣金、无滑点；
#   - 本地探针只回答方向，采纳须走平台单文件回测。
# 产出：reports/dividend_pool_probe_v1/ 下 summary.csv + nav.csv + yearly.csv
#   + trades_*.csv + README.md。
#
# 结果回填（2026-10-06，窗口 2024-01-02~2026-09-30，666 交易日）：
#   A_现行池    final 1.2071  cagr +7.14%  maxDD -26.44%  98 笔  敞口 0.992
#   B1_+A股红利 final 1.0609  cagr +2.19%  maxDD -26.43%  99 笔  红利占位 197 槽位日/7 笔
#   B2_+全部红利 final 1.0477 cagr +1.72%  maxDD -27.65%  100 笔 红利占位 236 槽位日/10 笔
# 年度：2024 A -2.0% vs B1 -9.9%（红利拖累）；2025 A +29.6% vs B1 +21.0%
#   （红利挤占主趋势槽位）；2026 A -4.9% vs B1 -2.6%（防御小补）。
# 结论：红利会被选中（约 10% 槽位日，主力=红利低波 512890/515100），但在
#   v3_3_1 规则下纳入是减分的——收益 -14.6pt（B1），回撤不改善。矫正 qfq
#   分红低估（约 +0.5%/年，量级 ~1.5pt）后方向不变。机制：红利分数"正而平"
#   （R² 高、斜率低），只在行业集体哑火时进 top3，接住的恰是组合最需要
#   弹性的空窗，且低波标的进出摩擦吃掉薄利。收线：维持剔除词表不动。
#   （本地绝对水平低于平台 v3_3_1 +62.51%，本地-平台偏差已在头注声明，
#   三口径同引擎对比只取方向。）

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.etf.industry_momentum import (build_pool, filter_by_name,
                                       momentum_score, SCORE_DAYS, TOPN)

# 生产 src 尚为 v3_1 口径（无止损常量）；此处取 lM_v3_3_1 的限亏线字面量
STOP_COST_PCT = 0.08

HERE = Path(__file__).resolve().parent
CACHE = HERE / '_cache'
OUT = HERE / 'reports' / 'dividend_pool_probe_v1'
START = '2024-01-01'
REBUILD_EVERY = 20
SCORE_EXIT_CONFIRM = 3
COMMISSION = 0.0001
DIV_PAT = ('红利', '股息')


def run_variant(bars, close_wide, cands, all_days, start_i, name_of, tag):
    """v3_3_1 判定核本地回测。返回 (净值序列, 交易表, 统计)。"""
    C = close_wide.values                       # [day, code] 对齐 all_days
    codes = list(close_wide.columns)
    col = {c: j for j, c in enumerate(codes)}

    # 池重建网格（bars 按 as_of 截断传入，生产同式；bars 带真实 amount）
    grid = list(all_days[start_i::REBUILD_EVERY])
    pools = []
    for gi, g in enumerate(grid):
        end = grid[gi + 1] if gi + 1 < len(grid) else all_days[-1]
        bars_g = {c: df.loc[:g] for c, df in bars.items()}
        members, _ = build_pool(cands, bars_g, as_of=g)
        pools.append((g, end, {m['code'] for m in members}))
    pool_of = {}
    for g, end, members in pools:
        for d in all_days:
            if g <= d < end or (end == all_days[-1] and d >= g):
                pool_of[d] = members

    def score_at(c, di):
        j = col[c]
        if di < SCORE_DAYS:
            return None
        tail = C[di - SCORE_DAYS:di, j]
        last = C[di, j]
        if not np.isfinite(tail).all() or not np.isfinite(last):
            return None
        return momentum_score(list(tail), float(last))

    cash = 1.0
    pos = {}                              # code -> dict(shares, avg_cost, neg_run, is_div)
    curve, trades = [], []
    div_hold_days = 0
    hold_days = {}                        # code -> 持有交易日数（持仓画像）
    slot_days = 0                         # 已占槽位日累计（敞口画像）
    for di, d in enumerate(all_days):
        if di < start_i:
            continue
        row = C[di]
        members = pool_of.get(d, set())
        held = [c for c in pos if pos[c]['shares'] > 0]

        # 先卖
        for c in held:
            j = col[c]
            if not np.isfinite(row[j]):   # 停牌/缺数不评估，仍持有
                continue
            p = pos[c]
            s = score_at(c, di)
            if s is None:                 # 数据不足不评估（出池直评口径）
                continue
            below_cost = row[j] <= p['avg_cost'] * (1.0 - STOP_COST_PCT)
            p['neg_run'] = 0 if s > 0 else p.get('neg_run', 0) + 1
            if not below_cost and p['neg_run'] < SCORE_EXIT_CONFIRM:
                continue
            reason = ('跌破成本%.0f%%' % (STOP_COST_PCT * 100)) if below_cost \
                else 'score转负x%d日' % p['neg_run']
            cash += p['shares'] * row[j] * (1 - COMMISSION)
            trades.append({'date': d, 'code': c, 'name': name_of[c],
                           'action': 'sell', 'reason': reason,
                           'is_div': p['is_div'],
                           'pnl_pct': row[j] / p['avg_cost'] - 1})
            del pos[c]

        # 后买：持仓先按今日价估值，再定每槽预算（生产 per 一次算定口径）
        for c in pos:
            j = col[c]
            if np.isfinite(row[j]):
                pos[c]['last_px'] = float(row[j])
        held = [c for c in pos if pos[c]['shares'] > 0]
        slots = TOPN - len(held)
        if slots > 0:
            sleeve = cash + sum(p['shares'] * p['last_px'] for p in pos.values())
            per = min(cash / slots, sleeve / TOPN)
            scored = [(score_at(c, di), c) for c in members if c not in held]
            scored = [(s, c) for s, c in scored
                      if s is not None and s > 0 and np.isfinite(row[col[c]])]
            scored.sort(key=lambda x: -x[0])      # 生产同式：分数降序
            picks = [c for _, c in scored[:slots]]
            for c in picks:
                px = float(row[col[c]])
                shares = per * (1 - COMMISSION) / px
                pos[c] = {'shares': shares, 'avg_cost': px, 'neg_run': 0,
                          'is_div': any(k in name_of[c] for k in DIV_PAT),
                          'last_px': px}
                cash -= per
                trades.append({'date': d, 'code': c, 'name': name_of[c],
                               'action': 'buy', 'reason': 'score>0',
                               'is_div': pos[c]['is_div'], 'pnl_pct': 0.0})
        div_hold_days += sum(1 for p in pos.values()
                             if p['shares'] > 0 and p['is_div'])
        for c, p in pos.items():
            if p['shares'] > 0:
                hold_days[c] = hold_days.get(c, 0) + 1
        slot_days += sum(1 for p in pos.values() if p['shares'] > 0)
        tv = cash + sum(p['shares'] * p['last_px'] for p in pos.values())
        curve.append((d, tv))

    nav = pd.Series({d: v for d, v in curve}).sort_index()
    dd = (nav / nav.cummax() - 1).min()
    years = len(nav) / 244.0
    cagr = nav.iloc[-1] ** (1 / years) - 1
    div_sells = [t for t in trades if t['is_div'] and t['action'] == 'sell']
    n_days = len(nav)
    stats = {
        'variant': tag, 'final': round(float(nav.iloc[-1]), 4),
        'cagr': round(float(cagr), 4), 'maxdd': round(float(dd), 4),
        'n_trades': len([t for t in trades if t['action'] == 'sell']),
        'exposure': round(slot_days / (n_days * TOPN), 3),
        'div_hold_days': div_hold_days, 'div_trades': len(div_sells),
        'div_win': round(float(np.mean([t['pnl_pct'] > 0 for t in div_sells])), 3)
        if div_sells else None,
        'top_held': '; '.join(f"{name_of.get(c, c)}({n})" for c, n in
                              sorted(hold_days.items(), key=lambda x: -x[1])[:5]),
    }
    yearly = nav.groupby(nav.index.str[:4]).agg(['first', 'last'])
    yr_ret = (yearly['last'] / yearly['first'] - 1).round(3).to_dict()
    return nav, pd.DataFrame(trades), stats, yr_ret


# ── 主流程 ──
OUT.mkdir(parents=True, exist_ok=True)
daily = pd.read_pickle(CACHE / 'etf_daily_full.pkl')
uni = pd.read_pickle(CACHE / 'etf_universe.pkl')
name_of = dict(zip(uni['code'], uni['name']))

base_cands = filter_by_name(uni.to_dict('records'))
div_all = [{'code': r['code'], 'name': r['name']}
           for r in uni.to_dict('records')
           if any(k in str(r['name']) for k in DIV_PAT) and r['code'] in daily]
div_a = [e for e in div_all if not e['code'].startswith('513')]

codes_a = {e['code'] for e in base_cands} & set(daily)
codes_b1 = codes_a | {e['code'] for e in div_a}
codes_b2 = codes_a | {e['code'] for e in div_all}

all_days = sorted(set().union(*[set(df.index) for df in daily.values()]))
start_i = next(i for i, d in enumerate(all_days) if d >= START)


def wide(code_set, col_name):
    w = pd.DataFrame({c: daily[c][col_name] for c in sorted(code_set)})
    return w.reindex(all_days)


print(f'窗口 {all_days[start_i]}~{all_days[-1]}（{len(all_days) - start_i} 交易日）')
print(f'红利候选：A 股 {len(div_a)} 只 / 全部 {len(div_all)} 只', flush=True)

results, navs, tdfs, yearlys = [], {}, {}, {}
for tag, cands, code_set in [('A_现行池', base_cands, codes_a),
                             ('B1_加A股红利', base_cands + div_a, codes_b1),
                             ('B2_加全部红利', base_cands + div_all, codes_b2)]:
    bars = {c: daily[c] for c in code_set}
    cw = wide(code_set, 'close')
    print(f'跑 {tag}（候选 {len(cands)} 只）...', flush=True)
    nav, tdf, st, yr = run_variant(bars, cw, cands, all_days, start_i, name_of, tag)
    results.append(st)
    navs[tag] = nav
    tdfs[tag] = tdf
    yearlys[tag] = yr
    tdf.to_csv(OUT / f'trades_{tag}.csv', index=False, encoding='utf-8-sig')

summary = pd.DataFrame(results)
summary.to_csv(OUT / 'summary.csv', index=False, encoding='utf-8-sig')
pd.DataFrame(navs).to_csv(OUT / 'nav.csv', encoding='utf-8-sig')
pd.DataFrame(yearlys).to_csv(OUT / 'yearly.csv', encoding='utf-8-sig')

print(summary.to_string(index=False))
print('\n年度收益：')
print(pd.DataFrame(yearlys).to_string())
tdfs['B2_加全部红利'].to_csv(OUT / 'trades_B2.csv', index=False,
                             encoding='utf-8-sig')

print(summary.to_string(index=False))
div_sells = tdfs['B2_加全部红利']
div_sells = div_sells[(div_sells['is_div']) & (div_sells['action'] == 'sell')]
if len(div_sells):
    print('\nB2 红利平仓明细：')
    print(div_sells[['date', 'code', 'name', 'reason', 'pnl_pct']].to_string(index=False))
print('产出 ->', OUT)
