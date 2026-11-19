# 验证策略本质：纯权益池 vs 全池（商品是不是熊市备用引擎）
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

equity = la.POOL[:10]
no_nengyuan = [c for c in la.POOL if c != '159981']
for label, pool in [('纯权益10只', equity), ('全池16只(剔能化)', no_nengyuan)]:
    res = la.run_backtest(pool)
    m = la.metrics(res)
    rets, prev = {}, la.INIT
    for t, v in m['yearly'].items():
        rets[t.year] = v / prev - 1
        prev = v
    print(f"{label}: 年化{m['cagr']:.2%} 回撤{m['mdd']:.1%} 空仓{res['empty_days']}天")
    print('  逐年:', {y: f'{r:+.0%}' for y, r in rets.items()})
    print('  空仓日分布:', res['nav'][res['nav']['hold'].isna()].index.year.value_counts().to_dict())
