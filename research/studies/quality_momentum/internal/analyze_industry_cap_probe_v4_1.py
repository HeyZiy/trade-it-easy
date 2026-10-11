# -*- coding: utf-8 -*-
"""行业上限离线探针 v4_1：被行业上限挡掉的候选 vs 同轮实际新开仓的60日前瞻收益。

问题（2026-10-10）：v4_1_5（顺延补位）否决后机制修正为"前6名门槛+20天观察期"
被破坏，而非行业约束软化——行业上限本身是否有正价值仍未被检验。本探针用 v4_1
导出 signals.cap_blocked 记录的被挡候选，对比同轮实际新开仓（members.selected
且非 kept）的 60 日前瞻收益，判断"去行业上限"变体是否值得上平台。

口径与边界：
- 样本：v4_1 全部 130 轮中 cap_blocked 非空的轮次及其候选。
- 前瞻收益与 rank_validity 探针同口径：T+1 收盘起算 60 日（r60_status=ok）。
- 对照：同轮实际新开仓的等权平均 r60；该轮无新开仓（空位留现金）时对照=现金(0)。
  被挡票与同轮新仓对空位的竞争是近似配对（多个空位时不做逐一归属），一阶反事实，
  不含组合二阶效应；全部样本内，采纳需平台变体回测。
- 基线是 v4_1 导出（保留名次留存线），但 cap_blocked 逻辑与 v4_1_2 相同
  （补位均只看当期前6名、行业受限跳过且不顺延），检验的是行业上限规则本身。

预声明判读（60日窗口）：
- 被挡组比同轮新仓差 ≥2pp → 行业上限有保护价值 → 变体不跑
- |差| < 2pp → 收益中性；去上限方向上提高行业集中度、与回撤第一诉求相悖 → 不跑
- 被挡组好 ≥2pp → 上限在拖累收益 → 上平台测去行业上限变体（判读以回撤为一票否决）

运行：.conda\\python.exe research/studies/quality_momentum/internal/analyze_industry_cap_probe_v4_1.py
（项目 .conda 环境；全部本地数据，不访问聚宽）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
FWD = STUDY / 'reports' / 'rank_validity_v4_1_2026-10-10' / 'member_forward_returns.csv'
EXPORTS = STUDY / 'exports'
OUT = STUDY / 'reports' / 'industry_cap_probe_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'industry_cap_probe_v4_1_2026-10-10.md'


def require(cond, msg):
    if not cond:
        raise SystemExit(f'FAIL: {msg}')


def main():
    signals = pd.read_csv(EXPORTS / 'pool_rotation_v4_1_signals.csv', encoding='utf-8-sig')
    members = pd.read_csv(EXPORTS / 'pool_rotation_v4_1_members.csv', encoding='utf-8-sig')
    fwd = pd.read_csv(FWD, encoding='utf-8-sig')
    require({'signal_day', 'cap_blocked'} <= set(signals.columns), 'signals缺列')
    require({'signal_day', 'code', 'selected', 'kept', 'rank'} <= set(members.columns), 'members缺列')
    require({'signal_day', 'code', 'r60', 'r60_status'} <= set(fwd.columns), 'forward缺列')

    blocked = []
    for row in signals.itertuples():
        text = '' if pd.isna(row.cap_blocked) else str(row.cap_blocked).strip()
        for code in filter(None, (c.strip() for c in text.split(';'))):
            blocked.append({'signal_day': row.signal_day, 'code': code})
    blocked = pd.DataFrame(blocked)
    require(not blocked.empty and not blocked.duplicated(['signal_day', 'code']).any(),
            '被挡候选为空或重复')

    new_buys = members.loc[members.selected.eq(True) & ~members.kept.eq(True),
                           ['signal_day', 'code', 'rank']]
    r60 = fwd.loc[fwd.r60_status.eq('ok'), ['signal_day', 'code', 'r60']]

    blocked = (blocked.merge(r60, on=['signal_day', 'code'], how='left')
               .merge(members[['signal_day', 'code', 'rank']].rename(columns={'rank': 'blocked_rank'}),
                      on=['signal_day', 'code'], how='left'))
    dropped = int(blocked.r60.isna().sum()) + int(blocked.blocked_rank.isna().sum())
    blocked = blocked.dropna(subset=['r60', 'blocked_rank']).copy()
    blocked['year'] = blocked.signal_day.str[:4].astype(int)

    new_r60 = new_buys.merge(r60, on=['signal_day', 'code'], how='inner')
    per_round_new = new_r60.groupby('signal_day').r60.mean()
    blocked['round_has_new'] = blocked.signal_day.isin(per_round_new.index)
    # 当轮有新仓：对照=同轮新仓等权均值；无新仓（空位留现金）：对照=现金0
    blocked['benchmark_r60'] = blocked.signal_day.map(per_round_new).fillna(0.0)
    blocked['diff'] = blocked.r60 - blocked.benchmark_r60

    def dist(series):
        return (f'n={len(series)} 均值{series.mean():+.2%} 中位{series.median():+.2%} '
                f'胜率{(series > 0).mean():.0%}')

    overall_diff = blocked['diff']
    with_new = blocked.loc[blocked.round_has_new, 'diff']
    without_new = blocked.loc[~blocked.round_has_new, 'diff']
    verdict_diff = overall_diff.mean()
    if verdict_diff <= -0.02:
        verdict = '被挡组显著更差：行业上限有保护价值，去上限变体不跑。'
    elif verdict_diff < 0.02:
        verdict = ('差距在±2pp内：行业上限对收益中性；去上限方向上提高行业集中度，'
                   '与回撤第一诉求相悖，不跑。')
    else:
        verdict = ('被挡组显著更好：行业上限在拖累收益，可上平台测去行业上限变体'
                   '（预声明判读以回撤为一票否决）。')

    lines = [
        '# 行业上限离线探针 v4_1（2026-10-10）', '',
        '被行业上限挡掉的候选 vs 同轮实际新开仓的 60 日前瞻收益'
        '（T+1 收盘起算，与 rank_validity 同口径）。对照缺失时（空位留现金）以现金 0 为对照。'
        f'被挡样本 r60/排名缺失剔除 {dropped} 笔。', '',
        f'## 总量', '',
        f'- 发生拦截的轮次：{blocked.signal_day.nunique()} 轮；被挡候选：{len(blocked)} 笔'
        f'（单轮最多 {blocked.groupby("signal_day").size().max()} 笔）',
        f'- 被挡时的名次分布：{blocked.blocked_rank.value_counts().sort_index().to_dict()}', '',
        f'## 收益对比（60日）', '',
        f'- 被挡候选：{dist(blocked.r60)}',
        f'- 对照（同轮新仓均值，无新仓轮=现金0）：{dist(blocked.benchmark_r60)}', '',
        f'### 差值（被挡 − 对照）', '',
        f'- 全部：{dist(overall_diff)}',
        f'- 同轮有新仓：{dist(with_new)}' if len(with_new) else '- 同轮有新仓：无样本',
        f'- 同轮无新仓（对照现金）：{dist(without_new)}' if len(without_new) else '- 同轮无新仓：无样本', '',
        '### 按信号年汇总', '',
        blocked.groupby('year').agg(n=('diff', 'size'), blocked_r60=('r60', 'mean'),
                                    benchmark=('benchmark_r60', 'mean'),
                                    diff=('diff', 'mean')).round(4).to_markdown(), '',
        '## 预声明判读', '',
        f'均值差 {verdict_diff:+.2%} → {verdict}', '',
        '注：样本仅数十笔，方向性证据；一阶反事实不含组合二阶效应。',
    ]
    OUT.mkdir(exist_ok=True)
    blocked.sort_values(['signal_day', 'blocked_rank']).to_csv(
        OUT / 'cap_blocked_episodes.csv', index=False, encoding='utf-8-sig')
    REPORT.write_text('\n'.join(lines), encoding='utf-8')

    print(f'拦截轮 {blocked.signal_day.nunique()}，被挡候选 {len(blocked)} 笔（剔除 {dropped}）')
    print(f'被挡组 r60: {dist(blocked.r60)}')
    print(f'对照组    : {dist(blocked.benchmark_r60)}')
    print(f'差值全部  : {dist(overall_diff)}')
    if len(with_new):
        print(f'差值有新仓: {dist(with_new)}')
    if len(without_new):
        print(f'差值无新仓: {dist(without_new)}')
    print(f'判读: 均值差 {verdict_diff:+.2%} → {verdict}')


if __name__ == '__main__':
    main()
