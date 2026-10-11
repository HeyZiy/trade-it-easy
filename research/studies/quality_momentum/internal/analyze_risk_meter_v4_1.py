# -*- coding: utf-8 -*-
"""风险计量探针 v4_1：残差波动率 vs 总波动率，及池过滤阈值的观测扫描。

问题（2026-10-10）：
1) 残差波动率（截至T-1的60个日收益对中证800一元回归，残差std年化×100）
   能否识别池内成员未来60日收盘跌幅<=-15%（观测刻度，非策略实际大亏），
   区分力是否超过现有vol60？
2) 顺带扫描阈值的观测敏感性：vol60阈值若调低（20/25/30），被排除的入池观测
   历史前瞻收益如何；财务阈值（ROE/净利同比/PE/PB）近阈值带的分组表现；
   残差波动率与vol60各自在池分位数处切一刀的对照。

口径与边界：
- 样本：v4_1 members 全部可排名池成员；前瞻收益沿用排名有效期探针输出
  （T+1收盘起算，60日；缺口/末端剔除不填零）。
- 窗口：信号日T取截至T-1的61根收盘→60个日收益；停牌日按前值填充（收益记0），
  上市不足窗口的观测剔除。离线总波动与平台vol60做Spearman交叉核验。
- 一阶反事实：只看被排除观测自身的前瞻收益，不含池变小后排名/行业上限变化的
  二阶效应；全部样本内，采纳需平台变体回测，不做多随机种子。
- 60日-15%是观察刻度；与实际策略已实现大亏（30轮/409轮）不是同一统计。

运行：python research/studies/quality_momentum/internal/analyze_risk_meter_v4_1.py
（项目 .conda 环境；行情全部来自本地缓存，不访问聚宽）
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
CACHE = STUDY.parent / 'roe_quality' / '_cache' / 'pool'
EXPORTS = STUDY / 'exports'
MEMBERS = EXPORTS / 'pool_rotation_v4_1_members.csv'
FWD = STUDY / 'reports' / 'rank_validity_v4_1_2026-10-10' / 'member_forward_returns.csv'
OUT = STUDY / 'reports' / 'risk_meter_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'risk_meter_v4_1_2026-10-10.md'
BLOWUP = -0.15
SQRT250 = math.sqrt(250.0)
FIN_BANDS = {
    'roe_ann': [12, 15, 20, np.inf],
    'profit_yoy': [10, 20, 50, np.inf],
    'pe': [0, 15, 30, np.inf],
    'pb': [0, 2, 5, np.inf],
}


def require(cond, msg):
    if not cond:
        raise ValueError(msg)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_members():
    members = pd.read_csv(MEMBERS, encoding='utf-8-sig')
    need = {'signal_day', 'code', 'rank', 'ranking_status', 'momentum', 'vol60',
            'roe_ann', 'profit_yoy', 'pe', 'pb'}
    require(need <= set(members.columns), 'members缺列: %s' % (need - set(members.columns)))
    members['signal_day'] = pd.to_datetime(members.signal_day)
    members['code6'] = members.code.str[:6]
    ranked = members[members.ranking_status.eq('ranked')].copy()
    require(not ranked.duplicated(['signal_day', 'code']).any(), '成员重复')
    fwd = pd.read_csv(FWD, encoding='utf-8-sig')
    fwd['signal_day'] = pd.to_datetime(fwd.signal_day)
    df = ranked.merge(fwd, on=['signal_day', 'code'], how='inner', suffixes=('', '_fwd'))
    df = df.drop(columns=[c for c in df.columns if c.endswith('_fwd')])
    require(len(df) == len(fwd), f'合并缺口 df={len(df)} fwd={len(fwd)}')
    dropped = len(ranked) - len(fwd)
    require(dropped >= 0, 'fwd多于ranked')
    return df


def build_returns(df):
    idx = pd.read_pickle(CACHE / 'idx_000906.pkl')
    mkt = pd.Series(pd.to_numeric(idx).to_numpy(),
                    index=pd.DatetimeIndex(pd.to_datetime(idx.index)).normalize())
    mkt = mkt[~mkt.index.duplicated()].sort_index()
    require(mkt.gt(0).all(), '指数含无效值')
    pos = {d: i for i, d in enumerate(mkt.index)}
    missing_days = [d for d in df.signal_day.unique() if d not in pos]
    require(not missing_days, '信号日不在日历: %s' % missing_days[:3])
    mkt_ret = mkt.pct_change().to_numpy()

    codes = sorted(df.code6.unique())
    rets, skipped = {}, []
    for code in codes:
        path = CACHE / f'bars_hfq_{code}.pkl'
        if not path.exists():
            skipped.append(code)
            continue
        frame = pd.read_pickle(path)
        require(list(frame.columns) == ['date', 'close'], f'{code}行情结构错误')
        s = pd.Series(pd.to_numeric(frame.close).to_numpy(),
                      index=pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize())
        s = s[~s.index.duplicated()].sort_index().reindex(mkt.index).ffill()
        rets[code] = s.pct_change().to_numpy()

    records = []
    for p, code in zip(df.signal_day.map(pos).to_numpy(), df.code6.to_numpy()):
        r = rets.get(code)
        if r is None or p < 61:
            records.append(dict(resid_vol=np.nan, beta=np.nan, r2=np.nan,
                                vol_off=np.nan, window='missing'))
            continue
        y, x = r[p - 60:p], mkt_ret[p - 60:p]
        if np.isnan(y).any() or np.isnan(x).any():
            records.append(dict(resid_vol=np.nan, beta=np.nan, r2=np.nan,
                                vol_off=np.nan, window='invalid'))
            continue
        mx, my = x.mean(), y.mean()
        var_x = float(((x - mx) ** 2).mean())
        if var_x <= 0:
            records.append(dict(resid_vol=np.nan, beta=np.nan, r2=np.nan,
                                vol_off=np.nan, window='invalid'))
            continue
        beta = float(((x - mx) * (y - my)).mean() / var_x)
        resid = y - (my - beta * mx) - beta * x
        var_y = float(y.var(ddof=1))
        records.append(dict(
            resid_vol=float(resid.std(ddof=1) * SQRT250 * 100),
            beta=beta,
            r2=float(1 - resid.var(ddof=1) / var_y) if var_y > 0 else np.nan,
            vol_off=float(y.std(ddof=1) * SQRT250 * 100),
            window='ok'))
    meters = pd.DataFrame(records, index=df.index)
    out = pd.concat([df, meters], axis=1)
    return out, skipped


def spearman(frame, a, b):
    pair = frame[[a, b]].dropna()
    return float(pair[a].corr(pair[b], method='spearman'))


def band_stats(frame, col, edges):
    frame = frame.copy()
    frame['band'] = pd.cut(frame[col], bins=edges, right=False)
    rows = []
    for band, g in frame.groupby('band', observed=True):
        ok = g[g.r60_status.eq('ok')]
        rows.append(dict(band=str(band), n_all=len(g), n_ok=len(ok),
                         meter_med=float(ok[col].median()) if len(ok) else np.nan,
                         blowups=int(ok.blowup.sum()) if len(ok) else 0,
                         blowup_rate=float(ok.blowup.mean()) if len(ok) else np.nan,
                         r60_med=float(ok.r60.median()) if len(ok) else np.nan,
                         r60_mean=float(ok.r60.mean()) if len(ok) else np.nan,
                         r20_med=float(ok.r20.median()) if len(ok) else np.nan))
    return rows


def cut_stats(frame, col, quantiles):
    rows = []
    ok = frame[frame.r60_status.eq('ok')]
    for q in quantiles:
        v = float(frame[col].quantile(q))
        hi, lo = ok[ok[col] > v], ok[ok[col] <= v]
        rows.append(dict(meter=col, quantile=q, cut=v,
                         excluded_share=float(len(hi) / len(ok)),
                         excluded_n=len(hi),
                         excluded_r60_med=float(hi.r60.median()),
                         excluded_blowup_rate=float(hi.blowup.mean()),
                         kept_r60_med=float(lo.r60.median()),
                         kept_blowup_rate=float(lo.blowup.mean())))
    return rows


def main():
    df = load_members()
    df, skipped_codes = build_returns(df)
    ok_win = df[df.window.eq('ok')].copy()
    ok_win['blowup'] = ok_win.r60_status.eq('ok') & ok_win.r60.le(BLOWUP)
    df['blowup'] = df.r60_status.eq('ok') & df.r60.le(BLOWUP)
    ok60 = df[df.r60_status.eq('ok')]

    sanity = dict(
        spearman_off_vs_platform=spearman(ok_win, 'vol_off', 'vol60'),
        median_abs_diff=float((ok_win.vol_off - ok_win.vol60).abs().median()),
        platform_vol60_median=float(ok_win.vol60.median()))
    overall = dict(
        rows=len(df), window_ok=len(ok_win),
        window_invalid=int(df.window.eq('invalid').sum()),
        window_missing=int(df.window.eq('missing').sum()),
        skipped_codes=len(skipped_codes),
        n_ok60=len(ok60), blowups=int(ok60.blowup.sum()),
        blowup_rate=float(ok60.blowup.mean()))

    # 爆雷组 vs 其余：两个计量各自的中位数（全池 + 前6名子样本）
    separation = []
    for scope, frame in (('all_members', ok_win[ok_win.r60_status.eq('ok')]),
                         ('top6', ok_win[ok_win.r60_status.eq('ok') & ok_win['rank'].le(6)])):
        blow, rest = frame[frame.blowup], frame[~frame.blowup]
        separation.append(dict(scope=scope, n=len(frame), blowups=len(blow),
                               resid_blowup_med=float(blow.resid_vol.median()),
                               resid_rest_med=float(rest.resid_vol.median()),
                               vol_blowup_med=float(blow.vol60.median()),
                               vol_rest_med=float(rest.vol60.median()),
                               beta_blowup_med=float(blow.beta.median()),
                               beta_rest_med=float(rest.beta.median())))

    qs = [float(ok_win.resid_vol.quantile(q)) for q in (0.25, 0.5, 0.75)]
    resid_q = band_stats(ok_win, 'resid_vol', [-np.inf, qs[0], qs[1], qs[2], np.inf])
    vol_q = band_stats(ok_win, 'vol60',
                       [-np.inf] + [float(ok_win.vol60.quantile(q)) for q in (0.25, 0.5, 0.75)]
                       + [np.inf])
    vol_fixed = band_stats(ok_win, 'vol60', [0, 20, 25, 30, 35])
    cuts = (cut_stats(ok_win, 'resid_vol', [0.5, 0.75, 0.8, 0.9])
            + cut_stats(ok_win, 'vol60', [0.5, 0.75, 0.8, 0.9]))

    # 新信息检验：vol60低/高 × 残差低/高 的爆雷率（各取自身中位数分割）
    resid_med = float(ok_win.resid_vol.median())
    vol_med = float(ok_win.vol60.median())
    cross_frame = ok_win[ok_win.r60_status.eq('ok')].copy()
    cross_frame['vol_side'] = np.where(cross_frame.vol60 <= vol_med, 'vol低半', 'vol高半')
    cross_frame['resid_side'] = np.where(cross_frame.resid_vol <= resid_med,
                                         'resid低半', 'resid高半')
    cross_rows = []
    for (vs, rs), g in cross_frame.groupby(['vol_side', 'resid_side']):
        cross_rows.append(dict(vol=vs, resid=rs, n=len(g), blowups=int(g.blowup.sum()),
                               blowup_rate=float(g.blowup.mean()),
                               r60_med=float(g.r60.median())))

    corr = {f'{a}_vs_{b}': spearman(ok_win, a, b) for a, b in
            (('resid_vol', 'vol60'), ('resid_vol', 'vol_off'), ('resid_vol', 'momentum'),
             ('resid_vol', 'prior20'), ('vol60', 'momentum'), ('vol_off', 'vol60'))}
    fin = {col: band_stats(ok_win, col, edges) for col, edges in FIN_BANDS.items()}

    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / 'member_risk_meters.csv', index=False, encoding='utf-8-sig')
    for name, rows in (('band_resid_quartiles', resid_q), ('band_vol_quartiles', vol_q),
                      ('band_vol_fixed', vol_fixed), ('cut_stats', cuts),
                      ('cross_vol_resid', cross_rows), ('separation', separation)):
        pd.DataFrame(rows).to_csv(OUT / f'{name}.csv', index=False, encoding='utf-8-sig')
    for col, rows in fin.items():
        pd.DataFrame(rows).to_csv(OUT / f'band_{col}.csv', index=False, encoding='utf-8-sig')

    summary = dict(date='2026-10-10', overall=overall, sanity=sanity,
                   separation=separation, correlations=corr,
                   resid_quartiles=resid_q, vol_quartiles=vol_q,
                   vol_fixed_bands=vol_fixed, cut_stats=cuts,
                   cross_vol_resid=cross_rows, resid_median=resid_med, vol_median=vol_med,
                   fin_bands=fin,
                   sha256={'members': sha(MEMBERS), 'forward': sha(FWD)},
                   methods=dict(
                       resid='60日收益对中证800一元回归残差std年化×100；停牌前值填充收益记0；'
                             '窗口=截至T-1的61根收盘；上市不足窗口剔除',
                       blowup='r60_status==ok且60日收盘收益<=-15%；观察刻度，'
                              '非策略实际大亏（后者30/409轮）',
                       boundary='一阶反事实：不含池变化对排名/行业上限/选股的二阶效应；'
                                '样本内观测，采纳需平台变体回测'))
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')
    write_report(summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('sha256',)},
                     ensure_ascii=True, default=str))
    print('报告：', REPORT)


def write_report(s):
    o, sanity = s['overall'], s['sanity']

    def band_table(rows, label):
        text = [f'| {label} | 观测数 | 计量中位 | 爆雷数 | 爆雷率 | r60中位 | r20中位 |',
                '| --- | --- | --- | --- | --- | --- | --- |']
        for r in rows:
            text.append('| %s | %d | %.1f | %d | %.1f%% | %+.2f%% | %+.2f%% |' % (
                r['band'], r['n_ok'], r['meter_med'], r['blowups'],
                r['blowup_rate'] * 100, r['r60_med'] * 100, r['r20_med'] * 100))
        return '\n'.join(text)

    lines = [
        '# 4.1 风险计量探针：残差波动率 vs vol60（2026-10-10）', '',
        f'样本{o["rows"]}条池成员观测，窗口有效{o["window_ok"]}条（上市不足/缺缓存剔除'
        f'{o["window_invalid"] + o["window_missing"]}条，缺缓存股票{o["skipped_codes"]}只）。'
        f'60日可观察{o["n_ok60"]}条，其中60日收盘跌幅≤-15%共{o["blowups"]}条'
        f'（{o["blowup_rate"]:.1%}）——这是观察刻度，与策略实际大亏（30/409轮）不是同一统计。', '',
        f'口径核验：离线总波动与平台vol60的Spearman相关{spearman_fmt(sanity)}；'
        f'平台vol60池中位数{sanity["platform_vol60_median"]:.1f}。', '',
        '## 爆雷组 vs 其余：两个计量的中位数', '',
        '| 样本 | 观测 | 爆雷 | 残差·爆雷中位 | 残差·其余中位 | vol60·爆雷中位 | vol60·其余中位 | β·爆雷 | β·其余 |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for r in s['separation']:
        lines.append('| %s | %d | %d | %.1f | %.1f | %.1f | %.1f | %.2f | %.2f |' % (
            r['scope'], r['n'], r['blowups'], r['resid_blowup_med'], r['resid_rest_med'],
            r['vol_blowup_med'], r['vol_rest_med'], r['beta_blowup_med'], r['beta_rest_med']))
    lines += ['', '## 分位数带：残差波动率', '', band_table(s['resid_quartiles'], '残差带'), '',
              '## 分位数带：vol60', '', band_table(s['vol_quartiles'], 'vol60带'), '',
              '## 新信息检验：相关性', '']
    for k, v in s['correlations'].items():
        lines.append(f'- {k}：{v:.3f}')
    lines += ['', f'分割取全池中位数（残差{s["resid_median"]:.1f}、vol60{s["vol_median"]:.1f}）：', '',
              '| vol60侧 | 残差侧 | 观测 | 爆雷数 | 爆雷率 | r60中位 |', '| --- | --- | --- | --- | --- | --- |']
    for r in s['cross_vol_resid']:
        lines.append('| %s | %s | %d | %d | %.1f%% | %+.2f%% |' % (
            r['vol'], r['resid'], r['n'], r['blowups'], r['blowup_rate'] * 100,
            r['r60_med'] * 100))
    lines += ['', '## 阈值刻度：分位数处切一刀的对照（60日可观察样本）', '',
              '| 计量 | 分位 | 切点 | 排除占比 | 排除·r60中位 | 排除·爆雷率 | 保留·r60中位 | 保留·爆雷率 |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for r in s['cut_stats']:
        lines.append('| %s | q%.0f | %.1f | %.1f%% | %+.2f%% | %.1f%% | %+.2f%% | %.1f%% |' % (
            r['meter'], r['quantile'] * 100, r['cut'], r['excluded_share'] * 100,
            r['excluded_r60_med'] * 100, r['excluded_blowup_rate'] * 100,
            r['kept_r60_med'] * 100, r['kept_blowup_rate'] * 100))
    lines += ['', '## vol60固定带（现阈值35）', '', band_table(s['vol_fixed_bands'], 'vol60带'), '',
              '## 财务阈值近阈带（观测）', '']
    for col in ('roe_ann', 'profit_yoy', 'pe', 'pb'):
        lines += [f'**{col}**', '', band_table(s['fin_bands'][col], col), '']
    lines += ['## 边界与判读', '',
              '全部为一阶反事实：只看被排除观测自身的前瞻收益，不含池变小后排名、'
              '行业上限、选股变化的二阶效应；样本内观测，任何采纳都需要平台变体回测。'
              '停牌按前值填充（收益记0）是对平台skip_paused=False的近似；'
              '残差波动率的回归窗口=vol60同窗（截至T-1的61根收盘），仅对中证800一元回归，'
              '未引入多因子。', '', '## 复现', '',
              '`python research/studies/quality_momentum/internal/analyze_risk_meter_v4_1.py`'
              '（项目 .conda 环境）；明细见同日目录member_risk_meters.csv等。', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def spearman_fmt(sanity):
    return '%.3f（中位绝对差%.1f）' % (sanity['spearman_off_vs_platform'], sanity['median_abs_diff'])


if __name__ == '__main__':
    main()
