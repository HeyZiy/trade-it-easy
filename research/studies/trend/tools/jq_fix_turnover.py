# -*- coding: utf-8 -*-
"""补拉 _cache/jq_daily.pkl 的换手率（新版 SDK get_valuation 每票约 100 天上限）。

按 80 交易日窗口分段拉全区间，覆盖 turnover 后回写缓存。幂等：重复跑只是重拉。
"""
import os
import pickle

import numpy as np
import pandas as pd
from dotenv import load_dotenv
import jqdatasdk as jq
from jqdatasdk import auth, get_valuation, get_query_count

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
CACHE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '_cache', 'jq_daily.pkl'))
WIN = 80

load_dotenv(os.path.join(REPO, '.env'))
auth(os.environ['JQ_USERNAME'], os.environ['JQ_PASSWORD'])
print('额度 before:', get_query_count())

with open(CACHE, 'rb') as f:
    data = pickle.load(f)
days, codes = data['days'], data['codes']
pos = {d: k for k, d in enumerate(days)}

frames = []
for i in range(0, len(days), WIN):
    seg = days[i:i + WIN]
    s, e = seg[0], seg[-1]
    for c0 in range(0, len(codes), 100):
        d = get_valuation(codes[c0:c0 + 100], start_date=s, end_date=e,
                          fields=['turnover_ratio'])
        if d is not None and not d.empty:
            frames.append(d)
    print('估值段 %s~%s 完成' % (s, e))

tv = pd.concat(frames)
tv['day'] = tv['day'].map(lambda x: pd.Timestamp(str(x)).strftime('%Y-%m-%d'))
turn = {c: np.full(len(days), np.nan) for c in codes}
for c, gdf in tv.groupby('code'):
    if c not in turn:
        continue
    for dd, tr in zip(gdf['day'], gdf['turnover_ratio']):
        k = pos.get(str(dd))
        if k is not None:
            turn[c][k] = float(tr)

data['turnover'] = turn
with open(CACHE, 'wb') as f:
    pickle.dump(data, f)
cov = sum(1 for c in codes if np.isfinite(turn[c]).any())
last = max(int(np.where(np.isfinite(turn[c]))[0][-1]) for c in codes)
print('换手覆盖: %d/%d 只，最远到 %s（共 %d 天）' % (cov, len(codes),
                                                  days[last], len(days)))
print('额度 after:', get_query_count())
