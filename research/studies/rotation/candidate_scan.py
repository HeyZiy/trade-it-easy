# 候选新成员筛查：与现有16只池的相关性 + 全期年化/波动
# 规则：与池内任一成员日收益相关>0.5 默认不进（README 构池硬规则4）
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_attribution as la

CANDIDATES = {
    # 行业/主题（A股beta，检验是否与宽基重复）
    '512480': '半导体', '159995': '芯片', '515030': '新能源车', '515790': '光伏',
    '512000': '券商', '512660': '军工', '512690': '酒', '159992': '创新药',
    '512170': '医疗', '159928': '消费', '515000': '科技',
    # 第二引擎候选（检验是否有独立暴露）
    '161226': '白银LOF', '162411': '华宝油气', '164824': '印度基金',
    '508000': '张江REIT', '180101': '蛇口产园REIT',
}
pool16 = [c for c in la.POOL if c != '159981']
pool_ret = la.panel[pool16].pct_change()

rows = []
for c, name in CANDIDATES.items():
    f = la.DATA / f"{c}.csv"
    if not f.exists():
        rows.append((c, name, None))
        continue
    df = pd.read_csv(f, usecols=['date', 'hfq_close'], parse_dates=['date'])
    s = df.set_index('date')['hfq_close']
    ret = s.pct_change()
    joined = pd.concat([pool_ret, ret.rename('cand')], axis=1).dropna()
    if len(joined) < 60:
        rows.append((c, name, None))
        continue
    corrs = joined[pool16].corrwith(joined['cand'])
    top = corrs.abs().idxmax()
    max_corr = corrs[top]
    sub = s.dropna()
    ann = (sub.iloc[-1] / sub.iloc[0]) ** (252 / len(sub)) - 1
    vol = ret.std() * np.sqrt(252)
    rows.append((c, name, (max_corr, la.NAMES[top], ann, vol,
                           str(sub.index[0].date()), str(sub.index[-1].date()), len(joined))))

print(f"{'代码':<7}{'名称':<14}{'与池内最高相关':>14}{'对象':>10}{'全期年化':>9}{'波动':>7}  区间")
for c, name, r in sorted(rows, key=lambda x: (x[2] is not None and abs(x[2][0]) < 0.5, x[2][0] if x[2] else 9), reverse=True):
    if r is None:
        print(f"{c:<7}{name:<14}  本地缺数据（LOF可能需按501018方式补取）")
        continue
    mc, obj, ann, vol, d0, d1, n = r
    verdict = '✓低相关' if abs(mc) < 0.5 else ('△临界' if abs(mc) < 0.6 else '✗重复')
    print(f"{c:<7}{name:<14}{mc:>14.2f}{obj:>10}{ann:>9.1%}{vol:>7.1%}  {d0}~{d1} {verdict}")
