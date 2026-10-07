# -*- coding: utf-8 -*-
# dive_ma20_census_v1 —— 跳水清零 × MA20 位置 census：集成建议 a 的资格预检（2026-10-07）
#
# 问题（外部建议 a/集成 a）：momentum_score 的跳水否决（近 3 个日环比 min<0.95
#   → score=0）是 new.py 抄来的未消融先验；v3_3 实测约一半平仓来自 score=0.0000，
#   TOP1 通信 +93% 被切在 0.0000 后继续涨到 3.033。建议 a = 跳水清零仅在价格
#   跌破 MA20×0.985 时生效，站上 MA20 的大跌视为假摔不清零。本 census 只回答
#   资格问题：**在清零触发日上，MA20 上方组与下方组的事后走势有没有区分度？**
#   有 → 值得开 v3_3_3 平台单变量；没有 → a 不用跑就地收线。
#
# 数据与口径：
#   - 持仓 episode = v3_2_1 受管路径 105 笔（logs/lM_v3_2_1_run_20261006.log，
#     2061 持仓日；与 v3_3_1 出场规则不同但票/池同源，信号行为 census 可迁，
#     与 volume_exit_census_v1 同一局限声明）；
#   - 清零触发日 t：c_t/c_{t-1}、c_{t-1}/c_{t-2}、c_{t-2}/c_{t-3} 的 min < 0.95
#     （与生产 momentum_score 同式，c 为 qfq 收盘）；
#   - MA20 = 截至t 的最近 20 根 qfq 收盘均值；"下方"= c_t < MA20×0.985；
#   - 前瞻收益 = 个股自身 c_{t+k}/c_t − 1（k=5/10/20，不截断于 episode 末——
#     股价在出场后仍存在；样本条件于"清零日在持仓期内"）；
#   - 对照组 = 同 episode 内非清零持仓日。
# 判线（事先约定，跑完不改）：上方组 fwd20 中位 ≥ 下方组 + 2pp 且上方组
#   fwd20 胜率 ≥ 55% → MA20 门有区分度，开 v3_3_3；两组差 < 1pp → 无区分度，
#   a 就地收线；介于其间 → 看方向与 n 再议。
# 已知局限：① 清零日在 v3_2_1 路径不构成出场（该路径无 score 出场），样本测的
#   是信号的判别力而非改规则后的组合效应；② 买入侧排名也会被 gated score 改动，
#   本 census 不覆盖；③ qfq 不调分红。
# 产出：reports/dive_ma20_census_v1/（census.csv + README.md）。

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mae_v1 import parse_log, LOG, CACHE, FEE  # 复用 episode 解析与缓存路径

OUT = HERE / 'reports' / 'dive_ma20_census_v1'
DIVE = 0.95
MA_BUF = 0.985
HORIZONS = [5, 10, 20]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    eps, still_open, orphans = parse_log(LOG)
    closes = pd.read_pickle(CACHE)
    closes = {c: d.set_index(pd.to_datetime(d.index)) for c, d in closes.items()}

    rows = []
    for e in eps:
        df = closes.get(e['code'])
        if df is None:
            continue
        s = df['close'].dropna()                     # 个股全历史（qfq）
        if len(s) < 25:
            continue
        dates = s.index
        i_entry = dates.searchsorted(pd.Timestamp(e['entry']))
        i_exit = dates.searchsorted(pd.Timestamp(e['exit']), side='right')
        c = s.values
        for i in range(max(20, i_entry), min(i_exit, len(c))):
            ratio_min = min(c[i] / c[i - 1], c[i - 1] / c[i - 2],
                            c[i - 2] / c[i - 3])
            veto = ratio_min < DIVE
            ma20 = c[i - 19:i + 1].mean()
            below = c[i] < ma20 * MA_BUF
            cls = ('veto_below' if (veto and below) else
                   'veto_above' if veto else 'nonveto')
            row = {'code': e['code'], 'date': str(dates[i].date()), 'cls': cls,
                   'ratio_min': round(ratio_min, 4)}
            for k in HORIZONS:
                row[f'fwd{k}'] = (c[i + k] / c[i] - 1
                                  if i + k < len(c) else float('nan'))
            rows.append(row)
    panel = pd.DataFrame(rows)
    n_veto = (panel['cls'] != 'nonveto').sum()
    print(f'持仓日 {len(panel)} | 清零触发日 {n_veto} '
          f'（上方 {int((panel["cls"] == "veto_above").sum())} / '
          f'下方 {int((panel["cls"] == "veto_below").sum())}）', flush=True)

    tab = []
    for cls in ['veto_above', 'veto_below', 'nonveto']:
        g = panel[panel['cls'] == cls]
        r = {'cls': cls, 'n': len(g)}
        for k in HORIZONS:
            f = g[f'fwd{k}'].dropna()
            r[f'fwd{k}_med%'] = round(f.median() * 100, 2)
            r[f'fwd{k}_win%'] = round((f > 0).mean() * 100, 1)
        tab.append(r)
    table = pd.DataFrame(tab)
    table.to_csv(OUT / 'census.csv', index=False, encoding='utf-8-sig')
    print(table.to_string(index=False), flush=True)

    # 判线：上方组 vs 下方组 fwd20 的区分度
    up = panel[panel['cls'] == 'veto_above']['fwd20'].dropna()
    dn = panel[panel['cls'] == 'veto_below']['fwd20'].dropna()
    if len(up) and len(dn):
        gap = up.median() - dn.median()
        print(f'\n判线：上方组 fwd20 中位 {up.median() * 100:+.2f}%（n={len(up)}）'
              f' vs 下方组 {dn.median() * 100:+.2f}%（n={len(dn)}）'
              f' → 差 {gap * 100:+.2f}pp', flush=True)
        if gap >= 0.02 and (up > 0).mean() >= 0.55:
            print('→ 有区分度：值得开 v3_3_3 平台单变量', flush=True)
        elif abs(gap) < 0.01:
            print('→ 无区分度：a 就地收线', flush=True)
        else:
            print('→ 介于其间：看方向与样本量再议', flush=True)
    (OUT / 'README.md').write_text(
        '# dive_ma20_census_v1 —— 跳水清零 × MA20 位置 census（2026-10-07）\n\n'
        '判线与口径见 ../dive_ma20_census_v1.py 头注。census.csv 为三组前瞻收益。\n',
        encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
