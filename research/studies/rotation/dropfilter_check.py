# 跌幅过滤器消融：近3日任一日跌超5%分数归0，在当前各配置下开/关对照
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

pool = [c for c in la.POOL if c != '159981']


def show(label, res):
    m = la.metrics(res)
    rets, prev = {}, la.INIT
    for t, v in m['yearly'].items():
        rets[t.year] = v / prev - 1
        prev = v
    keys = {y: f'{rets[y]:+.0%}' for y in (2018, 2020, 2022, 2024, 2025, 2026)}
    print(f"{label}: 年化{m['cagr']:.2%} 回撤{m['mdd']:.1%} 期末{m['final']:,.0f} "
          f"交易{res['events']} 佣金{res['fees'] / 1e4:.1f}万 空仓{res['empty_days']}天")
    print(f"   逐年{keys}")


configs = [
    ('推荐配置 四窗集成k=2', dict(mode='ens', ens_windows=(15, 20, 25, 30), k=2)),
    ('四窗集成k=1', dict(mode='ens', ens_windows=(15, 20, 25, 30), k=1)),
    ('单窗25 k=1(原策略形态)', dict(m_days=25, k=1)),
]
for label, kw in configs:
    k = kw.pop('k')
    for flag in (True, False):
        res = la.run_backtest_topk(pool, k=k, drop_filter=flag, **kw)
        show(f"{label} 过滤{'开' if flag else '关'}", res)
    print()
