# -*- coding: utf-8 -*-
"""温度闸完整账户重放：混合 37 只排名轮动的条件行业准入对照。

问题：事件级已证行业入场亏损集中在温度高位（见 EXPERIMENTS.md 温度计一节）。
本脚本在本地重放混合 37 只排名轮动账户，单变量加"行业 ETF 仅在温度分位低于
阈值时有资格参与第一名竞争"，对照无闸基线与原 15 只真实账户。

重放口径：
- 信号：pool_control_signals.csv（平台真实逐日分数/合格状态/排名），不本地
  重算评分；合格 = status=='eligible'；候选按 rank37 升序取首位为目标。
- 价格：乘法复权空间（market.csv event_close）；10:40 决策价按当日
  event_close/close 比例折算入同一空间；费用按名义额万二、最低 5 元、
  100 份整手；分红经复权隐含再投资，与平台账户（分红入现金）有口径差，
  以持仓序列匹配率与期末净值相对差报告对账。
- 闸（primary，资格语义）：目标 = 通过闸的合格候选中 rank37 最小者；被闸
  的行业同时失去"仍是第一名"的持有依据（等同掉出池）。闸（entry 变体）：
  仅约束新买入，已持有行业仍按原排名保位。温度分位缺失（上市未满 120 日）
  视为被闸（fail-closed）。
- 执行失败（决策价缺失/停牌）：卖出失败继续持有，买入失败持币，次日重试。

从仓库根运行：python research/studies/etf_rotation/temperature_gate_replay.py
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results' / 'temperature' / 'gate'
SIGNALS = HERE / 'pool_control_signals.csv'
DECISIONS = HERE / 'pool_control_decisions.csv'
NAV = HERE / 'pool_control_nav.csv'
MARKET = HERE / '_cache' / 'pool37_event_adjusted_20260930' / 'market.csv'
PANEL = HERE / '_cache' / 'temperature_panel.pkl'

INDUSTRY22 = {
    '515170', '512660', '512690', '512400', '159928', '515220', '512010',
    '515210', '512170', '159870', '512800', '159996', '512000', '512200',
    '512480', '159611', '515880', '516110', '512720', '512980', '515030',
    '515790',
}

ORIGINAL15 = {
    '510300', '159915', '515180', '512100', '588000', '513500', '513100',
    '159920', '513520', '513030', '518880', '159980', '159985', '511090',
    '511010',
}

CASH0 = 100000.0
FEE_RATE, FEE_MIN = 2e-4, 5.0


def load_inputs():
    sig = pd.read_csv(SIGNALS, dtype={'code': str})
    sig['code6'] = sig['code'].str.split('.').str[0]
    sig['date'] = pd.to_datetime(sig['date']).dt.strftime('%Y%m%d').astype(int)

    mk = pd.read_csv(MARKET, dtype={'code': str})
    mk['code'] = mk['code'].str.zfill(6)
    mk['date'] = pd.to_datetime(mk['date']).dt.strftime('%Y%m%d').astype(int)
    mk['factor'] = mk['event_close'] / mk['close']
    px = {}   # code -> Series(event_close indexed by date)
    fac = {}
    for code, g in mk.groupby('code'):
        px[code] = g.set_index('date')['event_close'].astype(float)
        fac[code] = g.set_index('date')['factor'].astype(float)

    with open(PANEL, 'rb') as f:
        panel = pickle.load(f)
    temp = {c: p['temp_pct'] for c, p in panel.items()}

    dec = pd.read_csv(DECISIONS, dtype={'target': str})
    dec = dec[dec['account'] == '37排名'].copy()
    dec['date'] = pd.to_datetime(dec['date']).dt.strftime('%Y%m%d').astype(int)

    nav = pd.read_csv(NAV)
    nav['date'] = pd.to_datetime(nav['date']).dt.strftime('%Y%m%d').astype(int)
    return sig, px, fac, temp, dec, nav


def replay(days, sig_by_day, px, fac, temp, gate=None, gate_entry_only=False,
           pool='37'):
    """gate: None 或阈值（temp_pct < gate 才可当目标）。

    pool: '37' 混合池（rank37）、'15' 原池（rank15）、'22' 纯行业池——
    信号表无 rank22 列，行业池内排名由 score 降序现算（与平台 rank 一致性
    由调用方校验）。
    """
    rank_col = {'37': 'rank37', '15': 'rank15', '22': None}[pool]
    cash, shares, holding = CASH0, 0.0, None
    nav_rows, trades = [], []

    def gate_ok(code, d):
        if gate is None or code not in INDUSTRY22:
            return True
        t = temp.get(code)
        if t is None or d not in t.index:
            return False
        v = t.loc[d]
        return bool(pd.notna(v) and v < gate)

    for d in days:
        s = sig_by_day.get(d)
        if s is None:
            nav_rows.append((d, cash + shares * px[holding].get(d, np.nan)
                             if holding else cash, holding))
            continue
        elig = s[(s['status'] == 'eligible') & s['decision_price'].notna()
                 & (s['paused'] == 0)]
        if pool == '15':   # rank15 对行业为 NaN，直接限定池成员防漏入
            elig = elig[elig['code6'].isin(ORIGINAL15)]
        elif pool == '22':
            elig = elig[elig['code6'].isin(INDUSTRY22)]
        elig = (elig.sort_values('score', ascending=False) if rank_col is None
                else elig.sort_values(rank_col))
        top_ungated = elig['code6'].iloc[0] if len(elig) else None
        gated = elig[elig['code6'].apply(gate_ok, d=d)]
        target = gated['code6'].iloc[0] if len(gated) else None
        # entry-only 变体：已持有的行业仍是未闸第一名时保位，仅新买入过闸
        if (gate is not None and gate_entry_only and holding in INDUSTRY22
                and holding == top_ungated):
            target = holding

        # 卖出：持有者非目标（含清仓目标为空）
        if holding is not None and holding != target:
            price_raw = s[s['code6'] == holding]['decision_price']
            f = fac[holding].get(d, np.nan)
            if len(price_raw) and pd.notna(price_raw.iloc[0]) and pd.notna(f):
                p = price_raw.iloc[0] * f
                value = shares * p
                fee = max(value * FEE_RATE, FEE_MIN)
                cash += value - fee
                trades.append((d, 'sell', holding, shares, p))
                shares, holding = 0.0, None
        # 买入：无持仓且目标存在
        if holding is None and target is not None:
            price_raw = s[s['code6'] == target]['decision_price']
            f = fac[target].get(d, np.nan)
            if len(price_raw) and pd.notna(price_raw.iloc[0]) and pd.notna(f):
                p = price_raw.iloc[0] * f
                lots = int(cash / (p * 100))          # 预留费用的整手
                while lots > 0:
                    value = lots * 100 * p
                    fee = max(value * FEE_RATE, FEE_MIN)
                    if value + fee <= cash:
                        break
                    lots -= 1
                if lots > 0:
                    shares = lots * 100
                    value = shares * p
                    fee = max(value * FEE_RATE, FEE_MIN)
                    cash -= value + fee
                    holding = target
                    trades.append((d, 'buy', holding, shares, p))
        mark = px[holding].get(d, np.nan) if holding else 0.0
        nav_rows.append((d, cash + shares * mark, holding))
    return pd.DataFrame(nav_rows, columns=['date', 'nav', 'holding']), trades


def metrics(nv: pd.Series) -> dict:
    ret = nv.iloc[-1] / nv.iloc[0] - 1
    days = len(nv)
    ann = (nv.iloc[-1] / nv.iloc[0]) ** (250 / days) - 1
    dd = (nv / nv.cummax() - 1).min()
    return {'final': float(nv.iloc[-1]), 'total_return_pct': float(ret * 100),
            'annualized_pct': float(ann * 100), 'max_dd_pct': float(dd * 100)}


def yearly(nv: pd.Series, dates: pd.Series) -> dict:
    df = pd.DataFrame({'y': dates.astype(str).str[:4], 'nv': nv.values})
    last = df.groupby('y')['nv'].last()
    prev_end = last.shift(1)
    prev_end.iloc[0] = CASH0
    return {y: round((last[y] / prev_end[y] - 1) * 100, 2) for y in df['y'].unique()}


def main():
    sig, px, fac, temp, dec, nav = load_inputs()
    days = sorted(sig['date'].unique())
    sig_by_day = {d: g for d, g in sig.groupby('date')}

    variants = [('无闸基线', dict(gate=None)),
                ('资格闸<0.5', dict(gate=0.5)),
                ('资格闸<2/3', dict(gate=2 / 3)),
                ('资格闸<0.8', dict(gate=0.8)),
                ('资格闸<0.9', dict(gate=0.9)),
                ('仅入场闸<2/3', dict(gate=2 / 3, gate_entry_only=True)),
                ('原15只_引擎内', dict(gate=None, pool='15'))]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out, navs = {}, {}
    for name, kw in variants:
        path, trades = replay(days, sig_by_day, px, fac, temp, **kw)
        nv = path['nav'].ffill()
        out[name] = {'metrics': metrics(nv), 'yearly': yearly(nv, path['date']),
                     'n_trades': len(trades)}
        navs[name] = path
        print(f"\n== {name} ==")
        print({k: round(v, 2) for k, v in out[name]['metrics'].items()},
              '| 交易', len(trades))
        hold = path['holding']
        ind_days = int(hold.isin(INDUSTRY22).sum())
        out[name]['industry_hold_days'] = ind_days
        out[name]['industry_hold_share'] = float(ind_days / max(int(path['holding'].notna().sum()), 1))
        print(f"行业持仓天数 {ind_days} / 总持仓天数 {int(path['holding'].notna().sum())}")

    # 对账：无闸基线 vs 平台 37排名
    base = navs['无闸基线']
    plat = nav[nav['account'] == '37排名'].set_index('date')
    cmp_days = [d for d in base['date'] if d in plat.index]
    base_nav = base.set_index('date').loc[cmp_days, 'nav']
    plat_nav = plat.loc[cmp_days, 'nav']
    rel = (base_nav / plat_nav - 1).abs()
    dec_seq = dec.set_index('date')['target'].dropna().astype(str).str.split('.').str[0]
    common = [d for d in dec_seq.index if d in dict(zip(base['date'], base['holding']))]
    mine = base.set_index('date').loc[common, 'holding'].astype(str)
    match = (mine.values == dec_seq.loc[common].values).mean()
    out['对账'] = {
        '期末净值_重放': float(base['nav'].iloc[-1]),
        '期末净值_平台': float(plat['nav'].iloc[-1]),
        '相对差_中位': float(rel.median()), '相对差_最大': float(rel.max()),
        '持仓序列与平台一致率': float(match), '对账天数': len(common)}
    print('\n== 对账（无闸 vs 平台37排名）==')
    print(json.dumps(out['对账'], ensure_ascii=False, indent=1))

    # 原池参照（平台真实 15排名）以及分年对照表
    tab = pd.DataFrame({k: out[k]['yearly'] for k in ('无闸基线', '资格闸<2/3', '原15只_引擎内')})
    print('\n== 分年收益（%，重放口径）==')
    print(tab.to_string())
    plat15 = nav[nav['account'] == '15排名'].set_index('date')['nav']
    out['原15只_平台真实'] = {'metrics': metrics(plat15)}
    print('\n原15只（平台真实）:', {k: round(v, 2) for k, v in out['原15只_平台真实']['metrics'].items()})

    pd.DataFrame({k: v.set_index('date')['nav'] for k, v in navs.items()}).to_csv(
        RESULTS / 'nav_series.csv', encoding='utf-8-sig')
    with open(RESULTS / 'replay_stats.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=float)
    print(f'\n结果写入 {RESULTS}')


if __name__ == '__main__':
    main()
