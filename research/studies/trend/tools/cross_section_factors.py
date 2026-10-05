# -*- coding: utf-8 -*-
"""方案A后续：趋势结构内的截面分化因子研究（离线，数据 _cache/jq_daily.pkl）。

问题从"何时买（形态择时）"换成"同一天里买哪只（截面排序）"：
宇宙 = gate 放行日 + v3 趋势结构内 + 换手≤12 的样本票；
方法 = 每日对因子做截面秩相关（Spearman IC，对 R20）+ 三分位分组前瞻收益，
       跨日聚合看均值、t 值、胜率与右尾（MFE p90）单调性。
时序映射与尸检一致：评估日 i（用到 close[i]）→ 成交 i+1 → 前瞻 i+2..i+21。
"""
import os
import pickle

import numpy as np
import pandas as pd

if __package__:
    from . import mfe_autopsy as A
else:
    import mfe_autopsy as A

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURNOVER_MAX = 12.0


def build():
    with open(os.path.join(HERE, '_cache', 'jq_daily.pkl'), 'rb') as f:
        data = pickle.load(f)
    days, codes = data['days'], data['codes']
    bars_all, turn_all = data['bars'], data['turnover']
    gc = data['gate_bars']['close']
    n = len(days)
    # 指数 20 日动量（相对强度基准）
    idx_mom20 = np.full(n, np.nan)
    for j in range(20, n):
        if np.isfinite(gc[j]) and np.isfinite(gc[j - 20]) and gc[j - 20] > 0:
            idx_mom20[j] = gc[j] / gc[j - 20] - 1.0

    rows = []
    I0, I1 = A.BARS_NEEDED - 1, n - 2 - A.FWD
    for i in range(I0, I1 + 1):
        j = i + 1
        # 门控（与尸检同口径：成交日 j 用截至 j 的 25 收盘）
        if j + 1 < A.GATE_LOOKBACK:
            continue
        cl = [float(x) for x in gc[j - A.GATE_LOOKBACK + 1: j + 1] if x == x]
        if len(cl) < A.GATE_LOOKBACK:
            continue
        ma20 = sum(cl[-20:]) / 20.0
        if not (cl[-1] > ma20 and ma20 >= sum(cl[-25:-5]) / 20.0):
            continue
        for code in codes:
            tr = turn_all[code][i]
            if not (tr == tr) or tr > TURNOVER_MAX:
                continue
            b = bars_all[code]
            sl = {f: b[f][i - A.BARS_NEEDED + 1: i + 1] for f in
                  ('open', 'high', 'low', 'close', 'volume')}
            if A._has_nan(sl['close']):
                continue
            if not A.structure_ok(sl):
                continue
            c = sl['close']
            v = [float(x) for x in sl['volume']]
            price = float(c[-1])
            if price <= 0:
                continue
            entry = float(b['close'][j])
            fw_c = b['close'][j + 1: j + 1 + A.FWD]
            fw_h = b['high'][j + 1: j + 1 + A.FWD]
            fw_l = b['low'][j + 1: j + 1 + A.FWD]
            if not (entry > 0) or entry != entry or len(fw_c) < A.FWD \
                    or np.all(np.isnan(fw_c)) or fw_c[-1] != fw_c[-1]:
                continue
            # 因子（全部只用截至评估日 i 的数据）
            mom20 = price / float(c[-21]) - 1.0 if c[-21] > 0 else np.nan
            mom5 = price / float(c[-6]) - 1.0 if c[-6] > 0 else np.nan
            mom_skip5 = (float(c[-6]) / float(c[-26]) - 1.0) if c[-26] > 0 else np.nan
            dd_hi60 = price / float(np.nanmax(b['high'][i - 59: i + 1])) - 1.0
            low60 = float(np.nanmin(c[-60:]))
            pos60 = price / low60 - 1.0 if low60 > 0 else np.nan
            ma20s = A._sma(c, 20)
            bias20 = (price - ma20s) / ma20s * 100 if ma20s else np.nan
            p5 = v[-6:-1]
            p5f = [x for x in p5 if x == x]
            vr1 = v[-1] / (sum(p5f) / len(p5f)) if p5f and sum(p5f) > 0 else np.nan
            atr = A._atr20([float(x) for x in sl['high']],
                           [float(x) for x in sl['low']], c)
            atrpct = atr / price * 100 if atr else np.nan
            rs20 = mom20 - idx_mom20[i] if np.isfinite(idx_mom20[i]) else np.nan
            rows.append(dict(
                day=days[i], code=code,
                r20=(fw_c[-1] / entry - 1) * 100,
                mfe=(np.nanmax(fw_h) / entry - 1) * 100,
                mae=(np.nanmin(fw_l) / entry - 1) * 100,
                mom20=mom20 * 100, mom5=mom5 * 100, mom_skip5=mom_skip5 * 100,
                rs20=rs20 * 100, dd_hi60=dd_hi60 * 100, pos60=pos60 * 100,
                bias20=bias20, vr1=vr1, atrpct=atrpct))
    return pd.DataFrame(rows)


def study(df):
    feats = [c for c in df.columns if c not in ('day', 'code', 'r20', 'mfe', 'mae')]
    out = []
    for day, g in df.groupby('day'):
        if len(g) < 20:
            continue
        gr = g.rank()
        for f in feats:
            m = g[f].notna() & gr['r20'].notna()
            if m.sum() < 20:
                continue
            ic = np.corrcoef(gr[f][m], gr['r20'][m])[0, 1]
            terc = pd.qcut(gr[f][m], 3, labels=False, duplicates='drop')
            top = g['r20'][m][terc == 2].mean()
            bot = g['r20'][m][terc == 0].mean()
            mfe_t = g['mfe'][m][terc == 2].mean()
            mfe_b = g['mfe'][m][terc == 0].mean()
            out.append(dict(day=day, f=f, ic=ic, spread=top - bot,
                            mfe_sp=mfe_t - mfe_b, n=len(g)))
    panel = pd.DataFrame(out)
    n_terr = max(1, df.groupby('day').size().ge(20).sum())
    print('有效交易日 %d，日均截面 %d 只' % (n_terr, df.groupby('day').size().mean()))
    rows = []
    for f, g in panel.groupby('f'):
        ic, sp = g['ic'].dropna(), g['spread'].dropna()
        rows.append(dict(
            factor=f, ic_mean=ic.mean(), ic_t=ic.mean() / (ic.std() / max(len(ic), 1) + 1e-12),
            ic_pos=(ic > 0).mean() * 100, spread=sp.mean(),
            spread_t=sp.mean() / (sp.std() / max(len(sp), 1) + 1e-12),
            mfe_sp=g['mfe_sp'].dropna().mean()))
    res = pd.DataFrame(rows).sort_values('ic_t', key=lambda s: s.abs(), ascending=False)
    pd.set_option('display.width', 200)
    print(res.to_string(index=False, float_format=lambda x: '%8.3f' % x))
    # 半月稳定性：|t| 显著因子的分组收益前后段对比
    print('\n── 显著因子分段稳定性（IC 前后半段）──')
    half = sorted(df['day'].unique())[len(df['day'].unique()) // 2]
    for f in res.head(4)['factor']:
        g = panel[panel.f == f]
        a = g[g.day < half]['ic'].mean()
        b = g[g.day >= half]['ic'].mean()
        print('%-10s 前段 IC=%6.3f | 后段 IC=%6.3f' % (f, a, b))
    return res


if __name__ == '__main__':
    df = build()
    print('截面样本: %d 票·日' % len(df))
    df.to_csv(os.path.join(HERE, '_cache', 'cross_section_raw.csv'),
              index=False, encoding='utf-8-sig')
    study(df)
