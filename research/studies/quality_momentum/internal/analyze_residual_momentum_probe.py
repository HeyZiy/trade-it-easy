# -*- coding: utf-8 -*-
"""残差动量探针（v4_1_2 池，Blitz-Huij-Martens 落地）：换排序分数而非仓位。

问题（2026-10-11）：12-2动量分数混着"跟大盘涨"与"个股自己走"两部分。残差动量
把日收益对中证800回归取残差，只用残差复合收益重新排名。仓位/池/轮换规则全不变，
只换尺子。检验两步：排名变化量 → 换票的前瞻收益差。

口径（预声明，不扫描）：
- 样本：exports/pool_rotation_v4_1_2_members.csv 全部可排名成员（rank非空）；
  前瞻收益复用 rank_validity 输出（(signal_day,code) 键，与版本无关）。
- 窗口与原策略对齐：T-1收盘为终点取253根收盘，日收益252个；β/α用全部252个日收益
  对中证800日收益OLS估计（截至T-1，无前视）；残差动量 = 前231个日收益（T-252..T-22，
  即12-2动量窗口）残差的复合收益 Π(1+ε)-1。
- 有效性：253根收盘任一缺失则该票该轮不可排名（与策略"需253根有效收盘"一致）；
  指数/个股缓存始于2015-06，2016上半年回看窗口不足的轮次剔除并报告。
- 换票前瞻对比：换入/换出票的r20与r60；轮级模拟为每轮top6等权r20复合
  （r20自T+1收盘起算，与下一轮买入存在1日错位，两条链同构，只影响水平不影响对比）。

预声明判读（直接说数字，不设通过线）：
1) 排名变化量：新旧前6平均重合度、全池Spearman。重合度≥5/6则变化太小，直接关闭；
2) 重合度低才看：换入vs换出的r20/r60差、两条链复合净值的年化/回撤/分年差，
   并检查差距是否集中于单一年份。

运行：.conda\\python.exe research/studies/quality_momentum/internal/analyze_residual_momentum_probe.py
（项目 .conda 环境；行情全部来自本地缓存，不访问聚宽）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
EXPORTS = STUDY / 'exports'
CACHE = STUDY.parent / 'roe_quality' / '_cache' / 'pool'
FWD = STUDY / 'reports' / 'rank_validity_v4_1_2026-10-10' / 'member_forward_returns.csv'
OUT = STUDY / 'reports' / 'residual_momentum_probe_2026-10-11'
REPORT = STUDY / 'reports' / 'residual_momentum_probe_2026-10-11.md'
CLOSE_WINDOW = 253      # T-253..T-1
RET_WINDOW = 252        # T-252..T-1 的日收益
MOM_WINDOW = 231        # T-252..T-22 的日收益（12-2动量窗口）
SKIP_RECENT = 21
TOP_N = 6
ANNUAL_DAYS = 245.0


def require(cond, msg):
    if not cond:
        raise SystemExit(f'FAIL: {msg}')


def load_calendar():
    idx = pd.read_pickle(CACHE / 'idx_000906.pkl')
    cal = pd.DatetimeIndex(pd.to_datetime(idx.index)).normalize()
    require(cal.is_unique and cal.is_monotonic_increasing, '指数日历无效')
    bench = pd.Series(pd.to_numeric(idx.to_numpy(), errors='raise'), index=cal)
    return cal, bench


def load_closes(codes, cal):
    data = {}
    for code in codes:
        path = CACHE / f'bars_hfq_{code[:6]}.pkl'
        if not path.exists():
            continue
        frame = pd.read_pickle(path)
        dates = pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize()
        series = pd.Series(pd.to_numeric(frame.close, errors='raise').to_numpy(), index=dates)
        series = series[~series.index.duplicated(keep='last')].sort_index()
        data[code] = series.reindex(cal)
    return pd.DataFrame(data)


def main():
    members = pd.read_csv(EXPORTS / 'pool_rotation_v4_1_2_members.csv', encoding='utf-8-sig')
    fwd = pd.read_csv(FWD, encoding='utf-8-sig')
    require({'signal_day', 'code', 'rank', 'momentum'} <= set(members.columns), 'members缺列')
    require({'signal_day', 'code', 'r20', 'r20_status', 'r60', 'r60_status'} <= set(fwd.columns),
            'forward缺列')
    members = members.loc[members['rank'].notna()].copy()
    members['dt'] = pd.to_datetime(members.signal_day)
    fwd2 = fwd[['signal_day', 'code', 'r20', 'r20_status', 'r60', 'r60_status']].copy()
    fwd2['dt'] = pd.to_datetime(fwd2.signal_day)
    fwd2 = fwd2.drop(columns='signal_day').rename(columns={'dt': 'signal_day_dt'})
    fwd2 = fwd2.rename(columns={'signal_day_dt': 'dt'})  # 统一键名 dt

    cal, bench_close = load_calendar()
    bench_ret = bench_close.pct_change()
    codes = sorted(members.code.unique())
    closes = load_closes(codes, cal)
    require(len(closes.columns) > 0, '无个股收盘缓存')
    R = closes.pct_change()

    rows = []
    for day, grp in members.groupby('dt'):
        q = cal.get_loc(day)
        if q < CLOSE_WINDOW:
            rows.append({'signal_day': day, 'status': 'insufficient_index'})
            continue
        p = q - 1                              # T-1 的日历位置
        close_win = closes.iloc[p - CLOSE_WINDOW + 1: p + 1]
        complete = close_win.notna().all()     # 253根收盘齐全（列维度）
        ret_win = R.iloc[p - RET_WINDOW + 1: p + 1]        # 252×K
        mom_win = ret_win.iloc[:MOM_WINDOW]                # 231×K
        mkt_all = bench_ret.iloc[p - RET_WINDOW + 1: p + 1]
        mkt_mom = mkt_all.iloc[:MOM_WINDOW]
        codes_day = [c for c in grp.code.unique() if c in closes.columns]
        resid = {}
        for code in codes_day:
            if not bool(complete.get(code, False)):
                continue
            w = ret_win[code].to_numpy()
            wm = mom_win[code].to_numpy()
            m = mkt_all.to_numpy()
            mm = mkt_mom.to_numpy()
            var = float(np.var(m, ddof=1))
            if var <= 0 or not np.isfinite(w).all():
                continue
            beta = float(np.cov(w, m, ddof=1)[0, 1]) / var
            alpha = float(np.mean(w)) - beta * float(np.mean(m))
            eps = wm - alpha - beta * mm
            resid[code] = float(np.prod(1.0 + eps) - 1.0)
        rows.append({'signal_day': day, 'status': 'ok', 'n_ranked': len(grp),
                     'n_residual': len(resid), 'residual': resid})
    rounds = pd.DataFrame([r for r in rows if r['status'] == 'ok'])
    skipped = [r for r in rows if r['status'] != 'ok']
    require(len(rounds) > 50, f'可计算轮次过少: {len(rounds)}')

    # 逐轮排名对比与换票明细
    detail = []
    for r in rounds.itertuples():
        day = r.signal_day
        grp = members.loc[members.dt.eq(day), ['code', 'rank', 'momentum']].copy()
        grp['resid_mom'] = grp.code.map(r.residual)
        valid = grp.dropna(subset=['resid_mom'])
        require(not valid.code.duplicated().any(), f'{day} 成员重复')
        old_top6 = set(grp.loc[grp['rank'] <= TOP_N, 'code'])
        new_order = valid.sort_values(['resid_mom', 'code'], ascending=[False, True])
        new_top6 = set(new_order.code.head(TOP_N))
        fwd_day = fwd2.loc[fwd2.dt.eq(day), ['code', 'r20', 'r20_status', 'r60', 'r60_status']]
        for name, codeset in (('in', new_top6 - old_top6), ('out', old_top6 - new_top6)):
            for code in sorted(codeset):
                f = fwd_day.loc[fwd_day.code.eq(code)]
                detail.append({'signal_day': day, 'kind': name, 'code': code,
                               'r20': float(f.r20.iloc[0]) if len(f) else np.nan,
                               'r60': float(f.r60.iloc[0]) if len(f) else np.nan})
        detail.append({'signal_day': day, 'kind': 'overlap', 'code': '',
                       'r20': len(new_top6 & old_top6), 'r60': np.nan})
        detail.append({'signal_day': day, 'kind': 'spearman', 'code': '',
                       'r20': float(valid.momentum.corr(valid.resid_mom, method='spearman'))
                       if len(valid) >= 3 else np.nan,
                       'r60': np.nan})
    det = pd.DataFrame(detail)

    overlaps = det.loc[det.kind.eq('overlap'), 'r20']
    rhos = det.loc[det.kind.eq('spearman'), 'r20'].dropna()
    mean_overlap = float(overlaps.mean())
    ins = det.loc[det.kind.eq('in')].dropna(subset=['r20'])
    outs = det.loc[det.kind.eq('out')].dropna(subset=['r20'])

    # 轮级复合：每轮新旧top6等权r20（两链同剔除无效轮）
    chain_rows = []
    for r in rounds.itertuples():
        day = r.signal_day
        grp = members.loc[members.dt.eq(day), ['code', 'rank']].copy()
        grp['resid_mom'] = grp.code.map(r.residual)
        old_top6 = list(grp.loc[grp['rank'] <= TOP_N].sort_values('rank').code)
        new_order = grp.dropna(subset=['resid_mom']).sort_values(
            ['resid_mom', 'code'], ascending=[False, True])
        new_top6 = list(new_order.code.head(TOP_N))
        fwd_day = fwd2.loc[fwd2.dt.eq(day)].set_index('code')
        r20o = [fwd_day.r20.get(c, np.nan) for c in old_top6]
        r20n = [fwd_day.r20.get(c, np.nan) for c in new_top6]
        if not np.isfinite(r20o).all() or not np.isfinite(r20n).all():
            continue
        chain_rows.append({'signal_day': day, 'old_r20': float(np.mean(r20o)),
                           'new_r20': float(np.mean(r20n))})
    chain = pd.DataFrame(chain_rows).reset_index(drop=True)
    chain['old_nav'] = (1 + chain.old_r20).cumprod()
    chain['new_nav'] = (1 + chain.new_r20).cumprod()
    n_years = len(chain) * 20 / ANNUAL_DAYS
    ann_old = chain.old_nav.iloc[-1] ** (1 / n_years) - 1
    ann_new = chain.new_nav.iloc[-1] ** (1 / n_years) - 1
    dd_old = float((chain.old_nav / chain.old_nav.cummax() - 1).min())
    dd_new = float((chain.new_nav / chain.new_nav.cummax() - 1).min())
    chain['year'] = pd.to_datetime(chain.signal_day).dt.year
    # 分年：链内按年复合
    yearly = []
    for year, g in chain.groupby('year'):
        yearly.append({'year': year, 'old': (1 + g.old_r20).prod() - 1,
                       'new': (1 + g.new_r20).prod() - 1})
    yearly = pd.DataFrame(yearly).set_index('year')
    yearly['diff_pp'] = (yearly['new'] - yearly['old']) * 100

    lines = [
        '# 残差动量探针（v4_1_2 池，2026-10-11）', '',
        f'可计算轮次 {len(rounds)}/{len(rounds) + len(skipped)}'
        f'（剔除 {len(skipped)} 轮：缓存始于2015-06，回看253根不足）。'
        'β/α用252个日收益对中证800OLS，残差动量取12-2窗口231日残差复合。', '',
        '## 第一步：排名变化量', '',
        f'- 新旧前6平均重合度：{mean_overlap:.2f}/{TOP_N}'
        f'（分布 {overlaps.value_counts().sort_index().to_dict()}）',
        f'- 全池新旧分数 Spearman：均值 {rhos.mean():.3f}，'
        f'中位 {rhos.median():.3f}，min {rhos.min():.3f}', '',
        f'## 第二步：换票前瞻收益（r20/r60）', '',
        f'- 换入 n={len(ins)}：r20 中位 {ins.r20.median():+.2%} 均值 {ins.r20.mean():+.2%}；'
        f'r60 中位 {ins.r60.dropna().median() if ins.r60.notna().any() else float("nan"):+.2%}',
        f'- 换出 n={len(outs)}：r20 中位 {outs.r20.median():+.2%} 均值 {outs.r20.mean():+.2%}；'
        f'r60 中位 {outs.r60.dropna().median() if outs.r60.notna().any() else float("nan"):+.2%}', '',
        '## 轮级复合（每轮top6等权r20，两条链同起点同剔除）', '',
        f'- 轮数 {len(chain)}（约{n_years:.1f}年）；旧链年化 {ann_old:+.2%}，新链年化 {ann_new:+.2%}',
        f'- 链内最大回撤：旧 {dd_old:.2%}，新 {dd_new:.2%}', '',
        '### 分年（链内复合）', '',
        yearly.round(4).to_markdown(), '',
        '注：一阶近似，未含留存机制/行业上限的二阶效应；r20链与真实轮动存在1日错位，'
        '两条链同构。判读见报告正文数字。',
    ]
    OUT.mkdir(exist_ok=True)
    det.to_csv(OUT / 'swap_detail.csv', index=False, encoding='utf-8-sig')
    chain.to_csv(OUT / 'chain_r20.csv', index=False, encoding='utf-8-sig')
    REPORT.write_text('\n'.join(lines), encoding='utf-8')

    print(f'轮次 {len(rounds)}/{len(rounds)+len(skipped)}')
    print(f'前6重合度均值 {mean_overlap:.2f}/{TOP_N} 分布 {overlaps.value_counts().sort_index().to_dict()}')
    print(f'Spearman 均值 {rhos.mean():.3f} 中位 {rhos.median():.3f}')
    print(f'换入 r20 {ins.r20.median():+.2%}/{ins.r20.mean():+.2%}  换出 {outs.r20.median():+.2%}/{outs.r20.mean():+.2%}')
    print(f'链: 旧 {ann_old:+.2%} 回撤{dd_old:.2%}  新 {ann_new:+.2%} 回撤{dd_new:.2%}')
    print(yearly.round(4).to_string())


if __name__ == '__main__':
    main()
