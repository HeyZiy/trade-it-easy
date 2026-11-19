# 验证"能化与原油/有色/豆粕互相顶位"的机制：
# A. 每次出场去向 + 出场时自身分数（>0 被顶位 / <=0或nan 自身信号消失）
# B. 商品间日收益相关性（板块 beta 共振程度）
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

COMMODITY = {'501018', '159980', '159985', '159981'}
res = la.run_backtest(la.POOL)
tr = pd.DataFrame(res['transitions'], columns=['date', 'old', 'new', 'old_score'])
tr['year'] = tr['date'].dt.year

print("== A1. 各商品出场去向与出场原因 ==")
for c in ['159981', '501018', '159980', '159985']:
    sub = tr[tr.old == c]
    n = len(sub)
    to_comm = sub.new.isin(COMMODITY).sum()
    to_other = sub.new.notna().sum() - to_comm
    to_cash = sub.new.isna().sum()
    forced = ((sub.old_score <= 0) | sub.old_score.isna()).sum()
    print(f"{la.NAMES[c]}({c}) 出场{n}次: 去商品{to_comm} / 去股债汇{to_other} / 空仓{to_cash} | "
          f"自身分数消失{forced}次({forced / n:.0%}) vs 分数仍为正被顶位{n - forced}次({(n - forced) / n:.0%})")

print("\n== A2. 全池对照：商品 vs 非商品出场去向 ==")
for label, mask in [('商品出场', tr.old.isin(COMMODITY)), ('非商品出场', ~tr.old.isin(COMMODITY))]:
    sub = tr[mask]
    n = len(sub)
    to_comm = sub.new.isin(COMMODITY).sum()
    forced = ((sub.old_score <= 0) | sub.old_score.isna()).sum()
    print(f"{label} {n}次: 去商品{to_comm / n:.0%} | 自身分数消失{forced / n:.0%} vs 被顶位{(n - forced) / n:.0%}")

print("\n== A3. 能化(159981) 全部出场明细 ==")
sub = tr[tr.old == '159981']
for _, r in sub.iterrows():
    dst = la.NAMES[r.new] if pd.notna(r.new) else '空仓'
    reason = f"分数{r.old_score:.2f}被顶位" if pd.notna(r.old_score) and r.old_score > 0 else "自身分数消失"
    print(f"  {r.date.date()} → {dst:<6} ({reason})")

print("\n== B. 日收益相关系数（上市至2026-09-30，重叠区间）==")
cols = ['159981', '501018', '159980', '159985', '518880', '513100', '159915']
rets = la.panel[cols].pct_change()
corr = rets.corr().round(2)
corr.index = [la.NAMES[c] for c in cols]
corr.columns = [la.NAMES[c] for c in cols]
print(corr.to_string())

print("\n== B2. 能化被顶位出场时，顶位者是谁 ==")
sub = tr[(tr.old == '159981') & (tr.old_score > 0)]
print(sub.new.map(lambda c: la.NAMES.get(c, '空仓')).value_counts().to_string())
