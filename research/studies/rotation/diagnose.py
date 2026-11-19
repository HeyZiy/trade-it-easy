# 诊断：为什么某成员在动量轮动里是负贡献？三层证据，不依赖"剔除后更好看"。
# 1) 价格层：全期年化收益/波动（有没有可持续趋势）
# 2) 信号层：同一 score>0 信号下，未来10日收益（动量是否延续；描述性统计，区间收益有重叠）
# 3) 交易层：组合内持有段的段数/胜率/平均段收益/平均持有天数（是否 whipsaw）
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

res = la.run_backtest(la.POOL)
seg = pd.DataFrame(res['segments'], columns=['code', 'days', 'ret'])

print("== 交易层：持有段明细统计（17只基线）==")
print(f"{'标的':<6}{'名称':<8}{'段数':>4}{'胜率':>7}{'平均段收益':>10}{'中位段收益':>10}{'平均天数':>8}{'最差段':>9}{'平均每段盈亏(元)':>14}")
for c in la.POOL:
    s = seg[seg.code == c]
    if len(s) == 0:
        continue
    pnl_per_seg = res['pnl_by'][c] / len(s)
    print(f"{c:<6}{la.NAMES[c]:<8}{len(s):>4}{(s.ret > 0).mean():>7.0%}"
          f"{s.ret.mean():>10.2%}{s.ret.median():>10.2%}{s.days.mean():>8.1f}"
          f"{s.ret.min():>9.1%}{pnl_per_seg:>14,.0f}")

print("\n== 价格层 + 信号层（独立统计，不依赖组合；样本为各标的上市至2026-09-30）==")
print(f"{'标的':<6}{'名称':<8}{'全期年化':>9}{'年化波动':>8}"
      f"{'信号日占比':>9}{'信号后10日均值':>12}{'信号后10日胜率':>12}{'非信号后10日均值':>13}")
records = []
for j, c in enumerate(la.POOL):
    close = la.panel[c]
    first = close.first_valid_index()
    sub = close.loc[first:]
    n = len(sub)
    ann_ret = (sub.iloc[-1] / sub.iloc[0]) ** (252 / n) - 1
    vol = sub.pct_change().std() * np.sqrt(252)

    scores = np.array([la.score(j, i) for i in range(la.M_DAYS, len(la.panel))])
    fwd10 = (close.shift(-10) / close - 1).iloc[la.M_DAYS:].to_numpy()
    sig = (scores > la.SCORE_LO) & (scores < la.SCORE_HI)
    valid = np.isfinite(fwd10) & np.isfinite(scores)
    sig_valid = valid & sig
    nsig_valid = valid & ~sig
    m_sig = fwd10[sig_valid].mean() if sig_valid.sum() else np.nan
    w_sig = (fwd10[sig_valid] > 0).mean() if sig_valid.sum() else np.nan
    m_nsig = fwd10[nsig_valid].mean() if nsig_valid.sum() else np.nan
    print(f"{c:<6}{la.NAMES[c]:<8}{ann_ret:>9.1%}{vol:>8.1%}{sig_valid.sum() / valid.sum():>9.1%}"
          f"{m_sig:>12.2%}{w_sig:>12.1%}{m_nsig:>13.2%}")
    records.append({'code': c, 'name': la.NAMES[c], 'ann_ret': ann_ret, 'vol': vol,
                    'sig_ratio': sig_valid.sum() / valid.sum(), 'fwd_sig': m_sig,
                    'win_sig': w_sig, 'fwd_nsig': m_nsig})

print("\n== 能源两只对照：信号质量差在哪 ==")
for r in records:
    if r['code'] in ('501018', '159981', '159985'):
        print(f"  {r['name']}({r['code']}): 全期年化 {r['ann_ret']:+.1%}, 波动 {r['vol']:.1%}, "
              f"信号后10日 {r['fwd_sig']:+.2%}(胜率 {r['win_sig']:.0%}) vs 非信号 {r['fwd_nsig']:+.2%}")

print("\n== 能源化工(159981) 全部持有段 ==")
s = seg[seg.code == '159981'].copy()
s['ret'] = s['ret'].map(lambda v: f"{v:+.1%}")
print(s.to_string(index=False))
