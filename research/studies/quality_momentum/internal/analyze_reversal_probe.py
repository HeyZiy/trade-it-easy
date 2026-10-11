# -*- coding: utf-8 -*-
"""池内反转探针：同一质量池，把排序分数换成20日反转，测与动量的互补性。

问题（2026-10-11，动机：策略内部手术全部证伪后，回撤问题的剩余出路是
找一个与动量低相关、逆风期错峰的兄弟策略；池内反转与v4_1_2共用池子、
选票方向相反，是基础设施零成本的候选）：

1) 信号强度：池内每轮"20日跌幅最深"候选的实际跌幅分布——
   池内若无有意义的超跌（低波+质量池对暴跌股格外严），方向当场关闭；
2) 重合度：反转top6 vs 动量top6（预期接近0，重合反而说明信号坏）；
3) 前瞻收益：两条链 top6 的 r20/r60，及共同持仓票的配对差；
4) 时间维度（本次重点）：两链分年表现是否此强彼弱；动量逆风区
   （2021/09/09~2023/12/20、2024/06/04~2025/04/09）内反转链是否占优；
   最终判读在组合层——v4_1_2 日净值与反转链的50/50等权组合回撤对照。

口径与边界：
- 池成员来自 v4_1_2 members 导出（平台口径：全池可排名成员、平台排名与
  12-2动量分数），每信号日取首次出现，剔除 rank/momentum 缺测行。
- 20日反转分数 = 本地后复权收盘 T/T-20 - 1（端点缺测即剔除，与平台
  "缺收盘不参与排名"口径一致）；平台动量排名保持平台原值不改算。
- 前瞻收益：T+1收盘至 T+1+h 收盘（h=20/60），缺口/末端剔除不填零，
  对齐 rank_validity 探针口径；配对差仅在两链共同持仓票内计算。
- 轮级复合链：每信号日 top6 等权 r20，逐轮复利（20日一期、全额再投资、
  无费用、无行业上限/留存/现金拖累）——一阶反事实，只用于比较两把
  尺子与组合层回撤形态，不代表可执行策略；平台采纳需单独回测。
- 50/50组合：v4_1_2 日净值与反转链净值各半（链净值按轮步进、日界外推），
  报告日收益相关、月收益相关与组合最大回撤。
- 样本内观察，无多重比较修正；本探针只回答"方向要不要开"。

运行：python research/studies/quality_momentum/internal/analyze_reversal_probe.py
（项目 .conda 环境；全部本地数据，不访问聚宽）
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
EXPORTS = STUDY / 'exports'
OUT = STUDY / 'reports' / 'reversal_probe_2026-10-11'
REPORT = STUDY / 'reports' / 'reversal_probe_2026-10-11.md'

START = '2016-03-01'          # r20 前视需2016-03前的行情，之前信号剔除
TOP_N = 6
HORIZONS = (20, 60)
ANN = 245.0
HEADWINDS = {                 # 动量逆风区（v4_1_2 实测回撤/超额回撤区间）
    '逆风A_2021-09~2023-12': ('2021-09-09', '2023-12-20'),
    '逆风B_2024-06~2025-04': ('2024-06-04', '2025-04-09'),
}


def require(cond, msg):
    if not cond:
        raise ValueError(msg)


def sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_members():
    members = pd.read_csv(EXPORTS / 'pool_rotation_v4_1_2_members.csv',
                          encoding='utf-8-sig')
    members['signal_day'] = pd.to_datetime(members['signal_day'])
    members = members[members['rank'].notna() & members['momentum'].notna()]
    members = members.sort_values(['signal_day', 'rank', 'code'])
    members = members.drop_duplicates(['signal_day', 'code'])
    require(not members.empty, 'members为空')
    require(members['momentum'].between(-1, 20).all(), 'momentum量纲异常')
    return members


def load_close_wide(codes):
    """后复权收盘宽表：index=全市场交易日并集，列=code。"""
    series = {}
    for code in codes:
        path = CACHE / f'bars_hfq_{code[:6]}.pkl'
        if not path.exists():
            continue
        frame = pd.read_pickle(path)
        require(list(frame.columns) == ['date', 'close'], f'{code}行情结构错误')
        dates = pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize()
        require(dates.is_unique and dates.is_monotonic_increasing, f'{code}行情乱序')
        close = pd.to_numeric(frame.close, errors='raise')
        require(np.isfinite(close).all() and (close > 0).all(), f'{code}行情含无效值')
        series[code] = pd.Series(close.to_numpy(), index=dates)
    require(len(series) >= len(codes) * 0.95, '行情缓存缺失过多')
    wide = pd.DataFrame(series)
    require(wide.index.is_unique and wide.index.is_monotonic_increasing, '宽表索引异常')
    return wide


def build_signal_table(members, wide):
    """每信号：平台动量排名 + 本地20日反转分数 + r20/r60 前瞻。"""
    index = wide.index
    pos = {d: i for i, d in enumerate(index)}
    values = {c: wide[c].to_numpy() for c in wide.columns}
    idx_of = {c: i for i, c in enumerate(wide.columns)}

    rows = []
    for day, group in members.groupby('signal_day'):
        base = pos.get(pd.Timestamp(day).normalize())
        if base is None or base < 21 or base + 1 + max(HORIZONS) >= len(index):
            continue
        lookback = {}
        fwd = {}
        for code, i in idx_of.items():
            arr = values[code]
            c_t, c_lb = arr[base], arr[base - 20]
            if np.isfinite(c_t) and np.isfinite(c_lb):
                lookback[code] = c_t / c_lb - 1.0
            c_in, c_20, c_60 = arr[base + 1], arr[base + 1 + 20], arr[base + 1 + 60]
            r20 = c_20 / c_in - 1.0 if np.isfinite(c_in) and np.isfinite(c_20) else np.nan
            r60 = c_60 / c_in - 1.0 if np.isfinite(c_in) and np.isfinite(c_60) else np.nan
            if np.isfinite(c_in):
                fwd[code] = (r20, r60)
        for row in group.itertuples():
            if row.code not in lookback or row.code not in fwd:
                continue
            r20, r60 = fwd[row.code]
            rows.append({'signal_day': day, 'code': row.code, 'rank': int(row.rank),
                         'momentum': float(row.momentum), 'rev_lb': lookback[row.code],
                         'r20': r20, 'r60': r60})
    table = pd.DataFrame(rows)
    require(not table.empty, '信号表为空')
    require(not table.duplicated(['signal_day', 'code']).any(), '信号表重复')
    return table


def add_rev_rank(table):
    """反转排名：r20_lb 升序（跌最深=1），同分按代码升序；缺反转分不参与。"""
    table = table.sort_values(['signal_day', 'rev_lb', 'code'])
    table['rev_rank'] = table.groupby('signal_day').cumcount() + 1
    return table


def pct(x):
    return f'{x * 100:+.2f}%'


def summarize_forward(table, top):
    """top='rank'（动量）或'rev_rank'（反转）的 top6 前瞻统计。"""
    heads = table[table[top] <= TOP_N]
    out = {}
    for h in HORIZONS:
        s = heads[f'r{h}'].dropna()
        out[h] = dict(count=len(s), mean=float(s.mean()), median=float(s.median()),
                      win=float((s > 0).mean()))
    return out, heads


def chain_metrics(contrib):
    """contrib：按信号日序的每轮 r20（小数）。返回链指标。"""
    c = pd.Series(contrib).dropna()
    eq = (1 + c).cumprod()
    years = len(c) * 20 / ANN
    ann = eq.iloc[-1] ** (1 / years) - 1
    mdd = (eq / eq.cummax() - 1).min()
    return dict(rounds=len(c), total=float(eq.iloc[-1] - 1), ann=float(ann), mdd=float(mdd))


def year_table(sig, mom_contrib, rev_contrib):
    years = range(pd.Timestamp(sig.min()).year, pd.Timestamp(sig.max()).year + 1)
    rows = []
    df = pd.DataFrame({'signal_day': pd.to_datetime(sig), 'mom': mom_contrib,
                       'rev': rev_contrib}).dropna()
    for y in years:
        seg = df[df['signal_day'].dt.year == y]
        if seg.empty:
            rows.append({'year': y, 'mom': np.nan, 'rev': np.nan})
            continue
        mom_eq = float((1 + seg['mom']).prod() - 1)
        rev_eq = float((1 + seg['rev']).prod() - 1)
        rows.append({'year': y, 'mom': mom_eq, 'rev': rev_eq,
                     'diff': rev_eq - mom_eq, 'rounds': len(seg)})
    return pd.DataFrame(rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    members = load_members()
    wide = load_close_wide(sorted(members['code'].unique()))
    table = build_signal_table(members, wide)
    table = add_rev_rank(table)
    table = table[table['signal_day'] >= pd.Timestamp(START)]
    sig_days = table['signal_day'].nunique()
    require(sig_days >= 100, f'有效信号轮不足: {sig_days}')

    # ---- 1) 信号强度：反转候选的跌幅分布 ----
    rev_top = table[table['rev_rank'] <= TOP_N]
    pool_lb = table.groupby('signal_day')['rev_lb']
    per_day_depth = rev_top.groupby('signal_day')['rev_lb'].mean()
    depth_desc = rev_top['rev_lb'].describe(percentiles=[.1, .25, .5, .75, .9])
    weak_days = int((per_day_depth > -0.05).sum())

    # ---- 2) 重合度 ----
    mom_top = table[table['rank'] <= TOP_N]
    overlap = (mom_top.merge(rev_top, on=['signal_day', 'code'])
               .groupby('signal_day').size().reindex(range(sig_days), fill_value=0))
    overlap_per_day = mom_top.groupby('signal_day')['code'].apply(
        lambda s: s.isin(rev_top.loc[rev_top['signal_day'].isin(s.index),
                                     'code']).sum())
    # 逐轮重合：对每个信号日取两份名单求交集
    mom_sets = mom_top.groupby('signal_day')['code'].apply(set)
    rev_sets = rev_top.groupby('signal_day')['code'].apply(set)
    overlap_counts = pd.Series({d: len(mom_sets[d] & rev_sets[d]) for d in mom_sets.index})
    zero_overlap = int((overlap_counts == 0).sum())

    # ---- 3) 前瞻收益 ----
    mom_stats, mom_head = summarize_forward(table, 'rank')
    rev_stats, rev_head = summarize_forward(table, 'rev_rank')
    paired = (mom_head.merge(rev_head, on=['signal_day', 'code'], suffixes=('_m', '_r')))
    paired_diff = {h: dict(count=len(paired[f'r{h}_m'].dropna()),
                           mean=float((paired[f'r{h}_r'] - paired[f'r{h}_m']).mean()),
                           median=float((paired[f'r{h}_r'] - paired[f'r{h}_m']).median()))
                   for h in HORIZONS if f'r{h}_m' in paired}

    # ---- 4) 轮级链 + 组合层 ----
    mom_day = mom_head[mom_head['r20'].notna()].groupby('signal_day')['r20'].mean()
    rev_day = rev_head[rev_head['r20'].notna()].groupby('signal_day')['r20'].mean()
    chain_days = mom_day.index.union(rev_day.index)
    mom_contrib = mom_day.reindex(chain_days)
    rev_contrib = rev_day.reindex(chain_days)
    mom_chain = chain_metrics(mom_contrib)
    rev_chain = chain_metrics(rev_contrib)
    both = pd.DataFrame({'mom': mom_contrib, 'rev': rev_contrib}).dropna()
    chain_corr = float(both['mom'].corr(both['rev']))
    years = year_table(both.index, both['mom'], both['rev'])

    # v4_1_2 nav 与反转链的50/50组合
    nav = pd.read_csv(EXPORTS / 'pool_rotation_v4_1_2_nav.csv', encoding='utf-8-sig')
    nav['date'] = pd.to_datetime(nav['date'])
    nav = nav.set_index('date').sort_index()
    nav_ret = nav['equity'].pct_change().fillna(0)
    rev_eq = (1 + rev_contrib.dropna()).cumprod()
    rev_eq.index = rev_eq.index + pd.Timedelta(days=1)   # 执行日近似：信号次日
    rev_daily = rev_eq.reindex(nav.index, method='ffill').pct_change().fillna(0)
    common = rev_daily.index[(rev_eq.index[0] <= rev_daily.index)]
    rev_daily = rev_daily[common]
    nav_ret_c = nav_ret[common]
    combo_ret = 0.5 * nav_ret_c + 0.5 * rev_daily
    def curve_stats(r):
        eq = (1 + r).cumprod()
        dd = (eq / eq.cummax() - 1).min()
        yrs = len(r) / ANN
        return dict(total=float(eq.iloc[-1] - 1), ann=float(eq.iloc[-1] ** (1 / yrs) - 1),
                    mdd=float(dd), vol=float(r.std() * np.sqrt(ANN)),
                    sharpe=float(r.mean() / r.std() * np.sqrt(ANN)))
    solo_nav, solo_rev, combo = curve_stats(nav_ret_c), curve_stats(rev_daily), curve_stats(combo_ret)
    daily_corr = float(nav_ret_c.corr(rev_daily))
    monthly = pd.DataFrame({'nav': nav_ret_c, 'rev': rev_daily}).resample('ME').sum()
    monthly_corr = float(monthly['nav'].corr(monthly['rev']))

    headwind_rows = []
    for name, (a, b) in HEADWINDS.items():
        seg_nav = nav_ret_c[(nav_ret_c.index >= a) & (nav_ret_c.index <= b)]
        seg_rev = rev_daily[(rev_daily.index >= a) & (rev_daily.index <= b)]
        headwind_rows.append({
            'window': name,
            'nav_cum': float((1 + seg_nav).prod() - 1),
            'rev_cum': float((1 + seg_rev).prod() - 1),
            'combo_dd': float(((0.5 * (1 + seg_nav).cumprod() + 0.5 * (1 + seg_rev).cumprod())
                               / (0.5 * (1 + seg_nav).cumprod() + 0.5 * (1 + seg_rev).cumprod()).cummax() - 1).min()),
        })

    # ---- 导出 ----
    table.to_csv(OUT / 'signals.csv', index=False, encoding='utf-8-sig')
    years.to_csv(OUT / 'year_table.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame({'equity': rev_eq}).to_csv(OUT / 'rev_chain_equity.csv', encoding='utf-8-sig')
    hashes = {p.name: sha(p) for p in (EXPORTS / 'pool_rotation_v4_1_2_members.csv',
                                       EXPORTS / 'pool_rotation_v4_1_2_nav.csv')}

    # ---- 报告 ----
    lines = []
    w = lines.append
    w('# 池内反转探针（v4_1_2 质量池，20日反转 vs 12-2动量）2026-10-11\n')
    w('动机：策略内部手术全部证伪后，回撤的剩余出路是低相关兄弟策略；池内反转与'
      'v4_1_2 共用质量池、选票方向相反。判读顺序：信号强度→重合度→前瞻→组合层。\n')
    w(f'- 样本：{sig_days} 轮（{table.signal_day.min().date()}~{table.signal_day.max().date()}），'
      f'池代码 {wide.shape[1]} 只，信号行 {len(table)}')
    w(f'- 数据指纹：{json.dumps(hashes, ensure_ascii=False)}\n')
    w('## 一、信号强度：池内反转候选的跌幅深度\n')
    w('每轮反转 top6 的 20日回看收益（rev_lb）均值分布：')
    w(f'- 均值 {pct(per_day_depth.mean())}，中位 {pct(per_day_depth.median())}，'
      f'10分位 {pct(depth_desc["10%"])}，90分位 {pct(depth_desc["90%"])}')
    w(f'- 跌幅不足5%（均值 > -5%）的弱信号轮：{weak_days}/{sig_days}')
    w('- 全池回看收益分布：' + ' / '.join(
        f'p{q} {pct(np.nanpercentile(table["rev_lb"], q))}' for q in (5, 25, 50, 75, 95)) + '\n')
    w('## 二、重合度\n')
    w(f'- 反转 top6 与动量 top6 平均重合 {overlap_counts.mean():.2f}/6；'
      f'零重合轮 {zero_overlap}/{sig_days}（{zero_overlap / sig_days:.0%}）\n')
    w('## 三、前瞻收益\n')
    for name, st in (('动量top6', mom_stats), ('反转top6', rev_stats)):
        row = ' | '.join(f"r{h}: n={st[h]['count']} 均值{pct(st[h]['mean'])} "
                         f"中位{pct(st[h]['median'])} 胜率{st[h]['win']:.0%}" for h in HORIZONS)
        w(f'- {name}：{row}')
    if paired_diff:
        w('- 共同持仓配对差（反转-动量，同一票同窗口）：' + ' | '.join(
            f"r{h}: n={v['count']} 均值{pct(v['mean'])} 中位{pct(v['median'])}"
            for h, v in paired_diff.items()))
    w('')
    w('## 四、轮级复合链（每轮top6等权r20，一阶反事实）\n')
    w('| 链 | 轮数 | 区间累计 | 年化 | 最大回撤 |')
    w('|---|---|---|---|---|')
    for name, m in (('动量', mom_chain), ('反转', rev_chain)):
        w(f"| {name} | {m['rounds']} | {pct(m['total'])} | {pct(m['ann'])} | {pct(m['mdd'])} |")
    w(f'- 两链轮收益相关：{chain_corr:+.3f}\n')
    w('### 分年（此强彼弱检验）\n')
    w('| 年 | 动量链 | 反转链 | 差(反转-动量) | 轮数 |')
    w('|---|---|---|---|---|')
    for r in years.itertuples():
        w(f"| {r.year} | {pct(r.mom)} | {pct(r.rev)} | {pct(r.diff)} | {r.rounds} |")
    neg_years = int((years['diff'] < 0).sum())
    w(f'\n反转链占优年份：{len(years) - neg_years}/{len(years)}\n')
    w('## 五、组合层：v4_1_2 与反转链 50/50\n')
    w('| 组合 | 累计 | 年化 | 波动 | 最大回撤 | 夏普 |')
    w('|---|---|---|---|---|---|')
    for name, s in (('v4_1_2 单独', solo_nav), ('反转链单独', solo_rev), ('50/50', combo)):
        w(f"| {name} | {pct(s['total'])} | {pct(s['ann'])} | {pct(s['vol'])} "
          f"| {pct(s['mdd'])} | {s['sharpe']:.2f} |")
    w(f'- 日收益相关 {daily_corr:+.3f}，月收益相关 {monthly_corr:+.3f}\n')
    w('### 动量逆风区内表现\n')
    w('| 区间 | v4_1_2 | 反转链 | 50/50区间回撤 |')
    w('|---|---|---|---|')
    for r in headwind_rows:
        w(f"| {r['window']} | {pct(r['nav_cum'])} | {pct(r['rev_cum'])} | {pct(r['combo_dd'])} |")
    w('\n## 结论\n')
    w('（回填处）')
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print(f'signals={len(table)} days={sig_days} codes={wide.shape[1]}')
    print(f'overlap/day={overlap_counts.mean():.2f} zero={zero_overlap}')
    print(f'mom_chain={mom_chain}')
    print(f'rev_chain={rev_chain}')
    print(f'corr daily={daily_corr:+.3f} monthly={monthly_corr:+.3f}')
    print(f'combo={combo}')
    for r in headwind_rows:
        print(r)


if __name__ == '__main__':
    main()
