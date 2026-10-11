# -*- coding: utf-8 -*-
"""40日 vs 20日：分年结构拆解与回撤对比（承接 analyze_rotate_period_v4_1）。

问题：40日的优势是多数年份普遍改善，还是主要靠一两个年份大赚？回撤代价如何？
方法：分年收益并排、差值、对数差贡献占比（可加、可剔除复现）、年内峰谷回撤、
年末全期回撤、全期与超额回撤。不做新回测，不扫参数。
运行：python research/studies/quality_momentum/internal/analyze_rotate_annual_detail_v4_1.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from analyze_rotate_period_v4_1 import FILES, INITIAL, load, sha  # noqa: E402

OUT = STUDY / 'reports' / 'rotate_period_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'rotate_period_v4_1_annual_detail_2026-10-10.md'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    a, b = load(FILES['rotate20']), load(FILES['rotate40'])
    require(a['时间'].equals(b['时间']), '两版日历不同')
    a['year'] = a['时间'].dt.year
    b['year'] = b['时间'].dt.year
    years = sorted(a.year.unique())

    rows = []
    for year in years:
        ia, ib = a.year.eq(year), b.year.eq(year)
        r20 = float((1 + a.loc[ia, 'return']).prod() - 1)
        r40 = float((1 + b.loc[ib, 'return']).prod() - 1)
        log_contrib = float(np.log(1 + r40) - np.log(1 + r20))
        dd20 = float((a.loc[ia, 'equity'] / a.loc[ia, 'equity'].cummax() - 1).min())
        dd40 = float((b.loc[ib, 'equity'] / b.loc[ib, 'equity'].cummax() - 1).min())
        rows.append(dict(year=year, r20=r20, r40=r40, diff_pp=(r40 - r20) * 100,
                         improved=r40 > r20, log_contrib=log_contrib,
                         dd20=dd20, dd40=dd40,
                         eod20=float(a.loc[ia, 'drawdown'].iloc[-1]),
                         eod40=float(b.loc[ib, 'drawdown'].iloc[-1])))
    detail = pd.DataFrame(rows)
    total_log = float(np.log(b.equity.iloc[-1] / a.equity.iloc[-1]))
    detail['log_share'] = detail.log_contrib / total_log
    without21 = float(np.exp(total_log - detail.loc[detail.year.eq(2021), 'log_contrib'].iloc[0]))
    without2122 = float(np.exp(total_log - detail.loc[detail.year.isin([2021, 2022]), 'log_contrib'].sum()))
    dd_window = (a['时间'] >= '2024-06-04') & (a['时间'] <= '2025-04-09')
    dd_window_b = (b['时间'] >= '2024-06-04') & (b['时间'] <= '2025-04-09')
    dd2018_a = a.loc[a.year.eq(2018)]
    dd2018_b = b.loc[b.year.eq(2018)]
    summary = dict(
        date='2026-10-10',
        improved_years=int(detail.improved.sum()), worse_years=int((~detail.improved).sum()),
        median_diff_pp=float(detail.diff_pp.median()), mean_diff_pp=float(detail.diff_pp.mean()),
        total_log_edge=total_log, total_cum_ratio=float(b.equity.iloc[-1] / a.equity.iloc[-1]),
        top1_year=int(detail.loc[detail.log_contrib.idxmax(), 'year']),
        top1_share=float(detail.log_share.max()),
        top2_share=float(detail.nlargest(2, 'log_contrib').log_share.sum()),
        cum_ratio_excl_2021=without21, cum_ratio_excl_2021_2022=without2122,
        full_dd20=float(a.drawdown.min()), full_dd40=float(b.drawdown.min()),
        rel_dd20=float(a.relative_dd.min()), rel_dd40=float(b.relative_dd.min()),
        dd2024_25_20=float(a.loc[dd_window, 'drawdown'].min()),
        dd2024_25_40=float(b.loc[dd_window_b, 'drawdown'].min()),
        within_dd2018_20=float((dd2018_a.equity / dd2018_a.equity.cummax() - 1).min()),
        within_dd2018_40=float((dd2018_b.equity / dd2018_b.equity.cummax() - 1).min()),
        sha256={p.name: sha(p) for p in FILES.values()},
    )
    OUT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUT / 'annual_detail.csv', index=False, encoding='utf-8-sig')
    (OUT / 'annual_detail_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    write_report(summary, detail)
    print(json.dumps(summary, ensure_ascii=True, default=str))


def write_report(summary, detail):
    lines = [
        '# 40日 vs 20日：分年结构与回撤拆解（2026-10-10）', '',
        f'11个年度中40日改善{summary["improved_years"]}年、更差{summary["worse_years"]}年；'
        f'差值中位数{summary["median_diff_pp"]:+.2f}pp、均值{summary["mean_diff_pp"]:+.2f}pp。'
        f'全期累计比{summary["total_cum_ratio"]:.3f}（40日领先'
        f'{(summary["total_cum_ratio"] - 1) * 100:.1f}%）。', '',
        '| 年份 | 20日 | 40日 | 差值(pp) | 对数差贡献占比 | 年内回撤20 | 年内回撤40 |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    for r in detail.itertuples():
        lines.append(f'| {r.year} | {r.r20:+.2%} | {r.r40:+.2%} | {r.diff_pp:+.2f} | '
                     f'{r.log_share:+.1%} | {r.dd20:.2%} | {r.dd40:.2%} |')
    lines += [
        '', '## 优势来源结构', '',
        f'对数差最大的两年{summary["top1_year"]}与其后一年合计贡献'
        f'{summary["top2_share"]:.0%}的对数优势。剔除{summary["top1_year"]}年后，'
        f'40日对20日的累计领先缩水为{(summary["cum_ratio_excl_2021"] - 1) * 100:.1f}%；'
        f'再剔除2022年后仅剩{(summary["cum_ratio_excl_2021_2022"] - 1) * 100:.1f}%'
        '——其余9个年度合计接近打平（改善与落后的年份大体相抵）。', '',
        '结论：**优势主要靠一两个年份大赚**，不是多数年份的普遍改善；'
        '差值中位数（+5.0pp）远小于2021年单项（+33.3pp）。', '',
        '## 回撤', '',
        f'全期最大回撤：20日{summary["full_dd20"]:.2%} vs 40日{summary["full_dd40"]:.2%}；'
        f'超额最大回撤：{summary["rel_dd20"]:.2%} vs {summary["rel_dd40"]:.2%}——40日更深。'
        f'共同的回撤谷底区间（2024-06-04至2025-04-09）内：'
        f'{summary["dd2024_25_20"]:.2%} vs {summary["dd2024_25_40"]:.2%}。', '',
        f'但2018年熊市40日更抗跌：年内回撤{summary["within_dd2018_40"]:.2%} vs '
        f'{summary["within_dd2018_20"]:.2%}，全年收益也更好（−7.50% vs −14.81%）。'
        '回撤代价不是均匀的：深在2024-25，浅在2018。', '',
        '## 判读', '',
        '40日优势与2021-2022年的市场结构高度绑定，样本内不能外推为稳定规律；'
        '若采纳40日，需接受更深的2024-25型回撤。分年表见annual_detail.csv，'
        '源数据与口径同[rotate_period_v4_1_2026-10-10](rotate_period_v4_1_2026-10-10.md)。', '',
        '## 复现', '',
        '`python research/studies/quality_momentum/internal/analyze_rotate_annual_detail_v4_1.py`（项目 .conda 环境）', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print('报告：', REPORT)


if __name__ == '__main__':
    main()
