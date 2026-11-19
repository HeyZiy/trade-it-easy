# 持有前k名等权 vs 满仓第一名（k=1兼作与原实现的一致性校验）
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

pool16 = [c for c in la.POOL if c != '159981']


def show(label, res):
    m = la.metrics(res)
    rets, prev = {}, la.INIT
    for t, v in m['yearly'].items():
        rets[t.year] = v / prev - 1
        prev = v
    keys = {y: f'{rets[y]:+.0%}' for y in (2018, 2022, 2024, 2025, 2026)}
    legs = {la.NAMES[c]: round(res['pnl_by'].get(c, 0) / 1e4, 1)
            for c in ('159915', '513100', '501018', '518880', '159981')}
    print(f"{label}: 年化{m['cagr']:.2%} 回撤{m['mdd']:.1%} 交易{res['events']} "
          f"佣金{res['fees'] / 1e4:.1f}万 空仓{res['empty_days']}天")
    print(f"   逐年{keys}")
    print(f"   盈亏(万){legs}")


print('== 校验：topk k=1 应等于原单持实现 ==')
res_a = la.run_backtest(pool16, m_days=25)
res_b = la.run_backtest_topk(pool16, k=1, m_days=25)
ma, mb = la.metrics(res_a), la.metrics(res_b)
print(f"原实现 年化{ma['cagr']:.4%} | topk(k=1) 年化{mb['cagr']:.4%} | "
      f"净值差 {abs(ma['final'] - mb['final']):.2f}")

print('\n== 四窗集成评分，16只池：k=1/2/3 ==')
for k in (1, 2, 3):
    res = la.run_backtest_topk(pool16, k=k, mode='ens', ens_windows=(15, 20, 25, 30))
    show(f"集成 k={k}", res)

print('\n== 四窗集成评分，17只池(含能化) k=2：前二共存能否化解能化漏血 ==')
res = la.run_backtest_topk(la.POOL, k=2, mode='ens', ens_windows=(15, 20, 25, 30))
show('集成 k=2 含能化', res)

print('\n== 单窗25评分，16只池 k=2（口径稳健性对照）==')
res = la.run_backtest_topk(pool16, k=2, m_days=25)
show('单窗25 k=2', res)
