# 多窗口集成(15/20/25均值) vs 单窗口，检验能否降低参数敏感性
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

pool = [c for c in la.POOL if c != '159981']
variants = [('集成15/20/25/30(窄窗对照)', dict(mode='ens', ens_windows=(15, 20, 25, 30))),
            ('集成5/20/60(宽跨度)', dict(mode='ens', ens_windows=(5, 20, 60))),
            ('集成10/20/60(宽跨度)', dict(mode='ens', ens_windows=(10, 20, 60))),
            ('集成10/25/60(宽跨度)', dict(mode='ens', ens_windows=(10, 25, 60))),
            ('集成5/15/30/60(宽跨度)', dict(mode='ens', ens_windows=(5, 15, 30, 60)))]
for label, kw in variants:
    res = la.run_backtest(pool, **kw)
    m = la.metrics(res)
    rets, prev = {}, la.INIT
    for t, v in m['yearly'].items():
        rets[t.year] = v / prev - 1
        prev = v
    legs = {la.NAMES[c]: round(res['pnl_by'][c] / 1e4, 1) for c in ('159915', '513100', '501018', '518880')}
    print(f"{label}: 年化{m['cagr']:.2%} 回撤{m['mdd']:.1%} 交易{res['events']}次 空仓{res['empty_days']}天")
    print('  逐年:', {y: f'{rets[y]:+.0%}' for y in sorted(rets)})
    print('  四腿盈亏(万):', legs)
