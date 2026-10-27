# -*- coding: utf-8 -*-
"""温度×盈利广度双闸完整账户重放：混合 37 只排名轮动的双条件准入裁决。

2026-10-27混排研究已结束；本文件仅用于复核既有结果。

问题：事件级 2×2 检验发现"低温度×有赞助"是唯一正单元（EXPERIMENTS.md
基本面赞助一节），可裁决假设为"温度闸×盈利广度双条件或使混合池超越而非
追平原 15 池"。本脚本在本地重放混合 37 只排名轮动账户，单变量加闸：
行业 ETF 仅在温度分位低于阈值且盈利广度不低于阈值时有资格参与第一名
竞争，对照无闸、单温度闸、单广度闸、双闸仅入场变体与原 15 只。

口径：
- 引擎复用 temperature_gate_replay.py（同装载、指标与循环结构）：平台
  真实信号不本地重算（合格=status==eligible，候选按 rank37 升序）；
  乘法复权空间；万二最低 5 元、100 份整手；10:40 决策价按当日
  event_close/close 折算；决策价缺失卖出失败继续持有、买入失败持币。
- 温度：temperature_panel.pkl 的 temp_pct 当日值（面板构建已 shift(1)，
  不含当日成交），缺失视为被闸（fail-closed，与已登记温度闸重放一致）。
- 广度：决策日前一自然日时点可见的最新单季归母净利同比>0 的活跃成分股
  占比，点时纪律与成分口径同 study_sponsorship.py（披露日≤前一自然日、
  同报告期取最晚披露、STATEMENT_TYPE=2 单季、INDATE≤t<OUTDATE）；单季
  覆盖率<0.6 或无可见成员视为广度无效，被闸（fail-closed）。
- 资格语义：被闸行业同时失去"仍是第一名"的持有依据；双闸另设仅入场
  变体（已持有行业仍是未闸第一名时保位）。
- 校验（不过则本轮全部无效）：①逐日广度面板须与已登记事件级
  results/sponsorship/events.csv 的 352 次事件逐项一致；②无闸基线、
  温度单闸、原 15 只须复现已登记温度闸重放锚点（年化 13.32%/33.98%/
  36.40%/36.80%，行业持仓天数 1408/684/515/0）。

从仓库根运行：python research/studies/industry_momentum/temperature_breadth_replay.py
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results' / 'temperature' / 'dual_gate'
MEMBERS_CACHE = HERE / '_cache' / 'temperature_members.pkl'
INCOME_CACHE = HERE / '_cache' / 'sponsorship_income.pkl'
SPONSOR_EVENTS = HERE / 'results' / 'sponsorship' / 'events.csv'

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.industry_momentum.temperature_gate_replay import (  # noqa: E402
    CASH0, FEE_MIN, FEE_RATE, INDUSTRY22, ORIGINAL15,
    load_inputs, metrics, yearly,
)
from research.studies.industry_momentum.temperature_build import SW_MAP  # noqa: E402
from research.studies.industry_momentum.study_sponsorship import build_snapshots  # noqa: E402

TEMP_TH_PRIMARY = 2 / 3   # 事件级 2×2 的温度边界
TEMP_TH_TIGHT = 0.5       # 已登记温度闸扫描的最紧档
BREADTH_TH = 0.5          # 盈利广度主切分（事件级预注册）
COVERAGE_FLOOR = 0.6      # 广度有效下限（事件级预注册口径）

# 已登记锚点（EXPERIMENTS.md 温度闸重放表 + 纯行业22重放表，引擎校验用；
# industry_hold_days 为 None 表示该锚点不校验此项）
ANCHORS = {
    '无闸基线': {'annualized_pct': 13.32, 'max_dd_pct': -42.12, 'industry_hold_days': 1408},
    '温度闸<2/3': {'annualized_pct': 33.98, 'max_dd_pct': -27.06, 'industry_hold_days': 684},
    '温度闸<0.5': {'annualized_pct': 36.40, 'max_dd_pct': -25.50, 'industry_hold_days': 515},
    '原15只_引擎内': {'annualized_pct': 36.80, 'max_dd_pct': -18.99, 'industry_hold_days': 0},
    '纯行业22_无闸': {'annualized_pct': 5.33, 'max_dd_pct': -35.85,
                     'industry_hold_days': None},
}


def build_daily_breadth(trading_days: np.ndarray) -> dict:
    """逐 ETF 逐日广度面板：{etf: DataFrame(breadth/coverage/n_active/n_comp)}。

    breadth = 时点可见单季 YoY>0 的活跃成分股 / 可计算成分股；
    coverage = 可计算 / 活跃。可见性 = 披露日（ACTUAL_ANN_DATE 缺则
    ANN_DATE）≤ 决策日前一自然日；成分窗口 INDATE≤t<OUTDATE，与
    study_sponsorship.py 事件级口径逐项一致。
    """
    with open(MEMBERS_CACHE, 'rb') as f:
        members = pickle.load(f)
    with open(INCOME_CACHE, 'rb') as f:
        income = pickle.load(f)

    snaps = {}
    for code, df in income.items():
        s = build_snapshots(df)
        if not s.empty:
            snaps[code] = (s['ann'].to_numpy(), s['sq_yoy'].to_numpy(dtype=float))

    n_days = len(trading_days)
    asof = (pd.to_datetime(pd.Series(trading_days).astype(str), format='%Y%m%d')
            - pd.Timedelta(days=1)).dt.strftime('%Y%m%d').astype(int).to_numpy()

    out = {}
    for etf, indices in SW_MAP.items():
        windows = {}
        for idx in indices:
            df = members[idx]
            outs = (df['OUTDATE'] if 'OUTDATE' in df.columns
                    else pd.Series(np.nan, index=df.index))
            for con, ind, o in zip(df['CON_CODE'].astype(str), df['INDATE'], outs):
                if pd.isna(ind):
                    continue
                windows.setdefault(con.split('.')[0], []).append((ind, o))
        n_active = np.zeros(n_days, np.int64)
        n_comp = np.zeros(n_days, np.int64)
        n_pos = np.zeros(n_days, np.int64)
        for code, wins in windows.items():
            active = np.zeros(n_days, bool)
            for ind, o in wins:
                m = trading_days >= np.int64(ind)
                if pd.notna(o):
                    m &= trading_days < np.int64(o)
                active |= m
            n_active += active
            sn = snaps.get(code)
            if sn is None:
                continue
            anns, sq_y = sn
            i = np.searchsorted(anns, asof, side='right') - 1
            vis = np.full(n_days, np.nan)
            hit = i >= 0
            vis[hit] = sq_y[i[hit]]
            comp = active & np.isfinite(vis)
            n_comp += comp
            n_pos += comp & (vis > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            breadth = np.where(n_comp > 0, n_pos / n_comp, np.nan)
            coverage = np.where(n_active > 0, n_comp / n_active, np.nan)
        out[etf] = pd.DataFrame(
            {'breadth': breadth, 'coverage': coverage,
             'n_active': n_active, 'n_comp': n_comp}, index=trading_days)
    return out


def check_against_events(br: dict) -> dict:
    """逐日广度面板 vs 已登记事件级 events.csv 的一致性校验。"""
    ev = pd.read_csv(SPONSOR_EVENTS, encoding='utf-8-sig', dtype={'code': str})
    n_chk = n_match = 0
    max_diff = 0.0
    mismatch = []
    for _, r in ev.iterrows():
        code, d = r['code'], int(r['date_int'])
        bp = br.get(code)
        if bp is None or d not in bp.index:
            continue
        b, cov = bp.at[d, 'breadth'], bp.at[d, 'coverage']
        n_act = int(bp.at[d, 'n_active'])
        n_chk += 1
        ok = True
        if pd.notna(r['breadth_sq']) and pd.notna(b):
            diff = abs(float(r['breadth_sq']) - float(b))
            max_diff = max(max_diff, diff)
            ok = diff < 1e-9
        else:
            ok = pd.isna(r['breadth_sq']) and pd.isna(b)
        if int(r['n_members']) != n_act:
            ok = False
        if ok:
            n_match += 1
        elif len(mismatch) < 5:
            mismatch.append([code, d, float(r['breadth_sq']) if pd.notna(r['breadth_sq']) else None,
                             float(b) if pd.notna(b) else None, int(r['n_members']), n_act])
    return {'checked': n_chk, 'matched': n_match, 'max_abs_diff': max_diff,
            'mismatch_examples': mismatch}


def make_gate(temp: dict, br: dict, temp_th=None, br_th=None):
    """资格闸：行业 ETF 须过全部启用条件；温度/广度缺失一律 fail-closed。"""

    def gate(code, d):
        if code not in INDUSTRY22:
            return True
        if temp_th is not None:
            t = temp.get(code)
            if t is None or d not in t.index:
                return False
            v = t.loc[d]
            if not bool(pd.notna(v) and v < temp_th):
                return False
        if br_th is not None:
            bp = br.get(code)
            if bp is None or d not in bp.index:
                return False
            b, cov = bp.at[d, 'breadth'], bp.at[d, 'coverage']
            if not (pd.notna(b) and pd.notna(cov)
                    and cov >= COVERAGE_FLOOR and b >= br_th):
                return False
        return True

    return gate


def replay(days, sig_by_day, px, fac, gate_fn=None, gate_entry_only=False, pool='37'):
    """与 temperature_gate_replay.replay 同构；仅闸条件泛化为 gate_fn(code, d)。

    pool='22'：纯行业池，行业池内排名由 score 降序现算（与
    temperature_industry22_replay.py 同法，score 序与平台 rank37 一致率
    已在该脚本逐日验证 100%）。
    """
    rank_col = {'37': 'rank37', '15': 'rank15', '22': None}[pool]
    cash, shares, holding = CASH0, 0.0, None
    nav_rows, trades = [], []

    def gate_ok(code, d):
        return True if gate_fn is None else gate_fn(code, d)

    for d in days:
        s = sig_by_day.get(d)
        if s is None:
            nav_rows.append((d, cash + shares * px[holding].get(d, np.nan)
                             if holding else cash, holding))
            continue
        elig = s[(s['status'] == 'eligible') & s['decision_price'].notna()
                 & (s['paused'] == 0)]
        if pool == '15':
            elig = elig[elig['code6'].isin(ORIGINAL15)]
        elif pool == '22':
            elig = elig[elig['code6'].isin(INDUSTRY22)]
        elig = (elig.sort_values('score', ascending=False) if rank_col is None
                else elig.sort_values(rank_col))
        top_ungated = elig['code6'].iloc[0] if len(elig) else None
        gated = elig[elig['code6'].apply(gate_ok, d=d)]
        target = gated['code6'].iloc[0] if len(gated) else None
        if (gate_fn is not None and gate_entry_only and holding in INDUSTRY22
                and holding == top_ungated):
            target = holding

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
        if holding is None and target is not None:
            price_raw = s[s['code6'] == target]['decision_price']
            f = fac[target].get(d, np.nan)
            if len(price_raw) and pd.notna(price_raw.iloc[0]) and pd.notna(f):
                p = price_raw.iloc[0] * f
                lots = int(cash / (p * 100))
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


def mechanism_stats(br: dict, temp: dict, days_arr: np.ndarray) -> dict:
    """22 只行业 ETF × 全部交易日的温度×广度格子（闸的逐日咬合量）。"""
    cells = {'温度过且广度过': 0, '温度过广度挡': 0, '温度挡广度过': 0,
             '两者都挡': 0, '温度缺失': 0}
    for code in sorted(INDUSTRY22):
        t = temp.get(code)
        tv = (t.reindex(days_arr) if t is not None
              else pd.Series(np.nan, index=days_arr))
        bp = br.get(code)
        if bp is None:
            b = cov = pd.Series(np.nan, index=days_arr)
        else:
            b, cov = bp['breadth'], bp['coverage']
        temp_ok = tv.notna() & (tv < TEMP_TH_PRIMARY)
        temp_na = tv.isna()
        br_ok = (b.notna() & cov.notna() & (cov >= COVERAGE_FLOOR)
                 & (b >= BREADTH_TH))
        cells['温度过且广度过'] += int((temp_ok & br_ok).sum())
        cells['温度过广度挡'] += int((temp_ok & ~br_ok).sum())
        cells['温度挡广度过'] += int((~temp_ok & ~temp_na & br_ok).sum())
        cells['两者都挡'] += int((~temp_ok & ~temp_na & ~br_ok).sum())
        cells['温度缺失'] += int(temp_na.sum())
    return cells


def main():
    sig, px, fac, temp, dec, nav = load_inputs()
    days = sorted(sig['date'].unique())
    days_arr = np.array(days, dtype=np.int64)
    sig_by_day = {d: g for d, g in sig.groupby('date')}

    print('构建逐日盈利广度面板…', flush=True)
    br = build_daily_breadth(days_arr)
    check = check_against_events(br)
    print(f"广度与事件级一致性：{check['matched']}/{check['checked']} 匹配，"
          f"最大差 {check['max_abs_diff']:.2e}", flush=True)

    variants = [
        ('无闸基线', dict()),
        ('温度闸<2/3', dict(gate_fn=make_gate(temp, br, temp_th=TEMP_TH_PRIMARY))),
        ('温度闸<0.5', dict(gate_fn=make_gate(temp, br, temp_th=TEMP_TH_TIGHT))),
        ('赞助闸>=0.5', dict(gate_fn=make_gate(temp, br, br_th=BREADTH_TH))),
        ('双闸<2/3且>=0.5',
         dict(gate_fn=make_gate(temp, br, temp_th=TEMP_TH_PRIMARY, br_th=BREADTH_TH))),
        ('双闸<0.5且>=0.5',
         dict(gate_fn=make_gate(temp, br, temp_th=TEMP_TH_TIGHT, br_th=BREADTH_TH))),
        ('双闸仅入场<2/3且>=0.5',
         dict(gate_fn=make_gate(temp, br, temp_th=TEMP_TH_PRIMARY, br_th=BREADTH_TH),
              gate_entry_only=True)),
        ('原15只_引擎内', dict(pool='15')),
        ('纯行业22_无闸', dict(pool='22')),
        ('纯行业22_双闸<2/3且>=0.5',
         dict(pool='22', gate_fn=make_gate(temp, br, temp_th=TEMP_TH_PRIMARY,
                                           br_th=BREADTH_TH))),
        ('纯行业22_双闸<0.9且>=0.5',
         dict(pool='22', gate_fn=make_gate(temp, br, temp_th=0.9,
                                           br_th=BREADTH_TH))),
    ]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out, navs = {}, {}
    for name, kw in variants:
        path, trades = replay(days, sig_by_day, px, fac, **kw)
        nv = path['nav'].ffill()
        out[name] = {'metrics': metrics(nv), 'yearly': yearly(nv, path['date']),
                     'n_trades': len(trades)}
        navs[name] = path
        hold = path['holding']
        ind_days = int(hold.isin(INDUSTRY22).sum())
        out[name]['industry_hold_days'] = ind_days
        out[name]['industry_hold_share'] = float(
            ind_days / max(int(hold.notna().sum()), 1))
        out[name]['cash_days'] = int(hold.isna().sum())   # 持币天数（纯行业对照用）
        print(f"\n== {name} ==")
        print({k: round(v, 2) for k, v in out[name]['metrics'].items()},
              '| 交易', len(trades), '| 行业持仓天数', ind_days,
              '| 持币天数', out[name]['cash_days'])

    # 引擎校验：四个锚点须复现已登记温度闸重放
    eng = {'anchors': ANCHORS, 'replay': {}, 'pass': True}
    for name, a in ANCHORS.items():
        m = out[name]['metrics']
        ind = out[name]['industry_hold_days']
        ok = (abs(m['annualized_pct'] - a['annualized_pct']) < 0.05
              and abs(m['max_dd_pct'] - a['max_dd_pct']) < 0.05
              and (a['industry_hold_days'] is None
                   or ind == a['industry_hold_days']))
        eng['replay'][name] = {
            'annualized_pct': m['annualized_pct'], 'max_dd_pct': m['max_dd_pct'],
            'industry_hold_days': out[name]['industry_hold_days'],
            '与登记一致': bool(ok)}
        eng['pass'] = eng['pass'] and bool(ok)
    out['引擎校验'] = eng
    print('\n== 引擎校验（对已登记温度闸重放锚点）==')
    print(json.dumps(eng['replay'], ensure_ascii=False, indent=1))

    # 对账：无闸基线 vs 平台 37排名（同已登记温度闸重放）
    base = navs['无闸基线']
    plat = nav[nav['account'] == '37排名'].set_index('date')
    cmp_days = [d for d in base['date'] if d in plat.index]
    base_nav = base.set_index('date').loc[cmp_days, 'nav']
    plat_nav = plat.loc[cmp_days, 'nav']
    rel = (base_nav / plat_nav - 1).abs()
    dec_seq = dec.set_index('date')['target'].dropna().astype(str).str.split('.').str[0]
    common = [d for d in dec_seq.index
              if d in dict(zip(base['date'], base['holding']))]
    mine = base.set_index('date').loc[common, 'holding'].astype(str)
    match = (mine.values == dec_seq.loc[common].values).mean()
    out['对账'] = {
        '期末净值_重放': float(base['nav'].iloc[-1]),
        '期末净值_平台': float(plat['nav'].iloc[-1]),
        '相对差_中位': float(rel.median()), '相对差_最大': float(rel.max()),
        '持仓序列与平台一致率': float(match), '对账天数': len(common)}
    print('\n== 对账（无闸 vs 平台37排名）==')
    print(json.dumps(out['对账'], ensure_ascii=False, indent=1))

    # 机制统计
    mech = {'温度2/3×广度0.5逐日格子_22ETF合计':
            mechanism_stats(br, temp, days_arr)}

    def hold_diff(a, b):
        ha = navs[a]['holding'].astype(str).to_numpy()
        hb = navs[b]['holding'].astype(str).to_numpy()
        return int((ha != hb).sum())

    mech['持仓不同天数'] = {
        '双闸2/3 vs 温度闸2/3': hold_diff('双闸<2/3且>=0.5', '温度闸<2/3'),
        '双闸0.5 vs 温度闸0.5': hold_diff('双闸<0.5且>=0.5', '温度闸<0.5'),
        '温度闸2/3 vs 无闸': hold_diff('温度闸<2/3', '无闸基线'),
        '双闸2/3 vs 无闸': hold_diff('双闸<2/3且>=0.5', '无闸基线'),
        '赞助闸 vs 无闸': hold_diff('赞助闸>=0.5', '无闸基线'),
    }
    out['机制统计'] = mech
    out['广度事件级一致性'] = check
    out['广度面板'] = {
        c: {'有效天数': int(br[c]['breadth'].notna().sum()),
            '覆盖率中位': float(br[c]['coverage'].median()),
            '广度均值': float(br[c]['breadth'].mean())}
        for c in sorted(br)}

    # 分年表与平台 15 参照
    tab = pd.DataFrame({k: out[k]['yearly'] for k in
                        ('无闸基线', '温度闸<2/3', '双闸<2/3且>=0.5',
                         '原15只_引擎内', '纯行业22_无闸',
                         '纯行业22_双闸<2/3且>=0.5',
                         '纯行业22_双闸<0.9且>=0.5')})
    print('\n== 分年收益（%，重放口径）==')
    print(tab.to_string())
    plat15 = nav[nav['account'] == '15排名'].set_index('date')['nav']
    out['原15只_平台真实'] = {'metrics': metrics(plat15)}
    print('\n原15只（平台真实）:',
          {k: round(v, 2) for k, v in out['原15只_平台真实']['metrics'].items()})

    pd.DataFrame({k: v.set_index('date')['nav'] for k, v in navs.items()}).to_csv(
        RESULTS / 'nav_series.csv', encoding='utf-8-sig')
    br_df = pd.concat(br, names=['etf', 'date'])
    br_df.to_csv(RESULTS / 'breadth_panel.csv', encoding='utf-8-sig')
    with open(RESULTS / 'replay_stats.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果写入 {RESULTS}")

    if not eng['pass'] or check['matched'] != check['checked']:
        print('\n!! 校验未通过（引擎锚点或广度一致性），结果已写盘但不得解读 !!')
        sys.exit(1)


if __name__ == '__main__':
    main()
