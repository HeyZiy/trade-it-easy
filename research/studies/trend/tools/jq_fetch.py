# -*- coding: utf-8 -*-
"""方案A尸检前置：jqdatasdk 一次性拉数并落盘缓存（研究期 2025-06-23~2026-06-30）。

设计（与 trend_v3 对齐的近似，记录在 meta 里）：
  - 宇宙：末日快照取主板+非ST（v3 是逐日快照，尸检用静态快照），固定种子抽 400 只；
  - 日线 OHLCV：400 只 + 门控指数 399317.XSHE，区间全量；
  - 换手率：get_valuation 逐日实际值（替代 v3 的 T-1 估值表外推，更真）；
  - 停牌/NaN 保留原样，由尸检端的 _has_nan 逻辑拒绝（与 v3 行为一致）。
缓存：research/studies/trend/_cache/jq_daily.pkl（重复运行不重拉，不耗额度）。
"""
import os
import pickle
import random
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv
import jqdatasdk as jq
from jqdatasdk import (auth, get_query_count, get_trade_days,
                       get_all_securities, get_price, get_valuation)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '_cache'))
CACHE = os.path.join(CACHE_DIR, 'jq_daily.pkl')

START, END = '2025-06-23', '2026-06-30'   # 试用账号数据边界内
N_SAMPLE = 400
SEED = 42
GATE_INDEX = '399317.XSHE'
FIELDS = ['open', 'high', 'low', 'close', 'volume']


def mainboard_non_st(dt_snapshot):
    df = get_all_securities('stock', date=dt_snapshot)
    out = {}
    for code, row in df.iterrows():
        ok = (code.startswith('60') and code.endswith('XSHG')) or \
             (code[:3] in ('000', '001', '002', '003') and code.endswith('XSHE'))
        if ok and 'ST' not in row['display_name']:
            out[code] = row['display_name']
    return out


def main():
    load_dotenv(os.path.join(REPO, '.env'))
    auth(os.environ['JQ_USERNAME'], os.environ['JQ_PASSWORD'])
    before = get_query_count()
    print('额度 before:', before)

    if os.path.exists(CACHE):
        sys.exit('缓存已存在 %s，如需重拉先删除' % CACHE)

    days = [str(d) for d in get_trade_days(START, END)]
    print('交易日数:', len(days))

    pool = mainboard_non_st(days[-1])
    print('主板非ST（末日快照）:', len(pool))
    rng = random.Random(SEED)
    codes = sorted(rng.sample(sorted(pool), N_SAMPLE))

    # ── 日线 OHLCV（100 只/片；新版 SDK 返回长表 time/code/字段）──
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    day_pos = {d.strftime('%Y-%m-%d'): k for k, d in enumerate(idx)}
    bars = {}
    for i in range(0, len(codes), 100):
        chunk = codes[i:i + 100]
        df = get_price(chunk, end_date=END, count=len(days), frequency='daily',
                       fields=FIELDS, skip_paused=False, fq='pre')
        if df is None or df.empty:
            print('警告：片 %d 返回空' % i)
            continue
        if isinstance(df.columns, pd.MultiIndex):   # 旧版宽表兼容
            wide = df.reindex(idx)
            for code in chunk:
                if code in wide.columns.get_level_values(0):
                    bars[code] = {f: wide[code][f].to_numpy(dtype=float)
                                  for f in FIELDS}
        else:                                       # 新版长表
            arr_long = {}
            for code, gdf in df.groupby('code'):
                gdf = gdf.drop_duplicates(subset='time', keep='last')
                slot = {f: np.full(len(days), np.nan) for f in FIELDS}
                for t, row in zip(gdf['time'],
                                  gdf[FIELDS].to_numpy(dtype=float)):
                    k = day_pos.get(pd.Timestamp(t).strftime('%Y-%m-%d'))
                    if k is not None:
                        for j, f in enumerate(FIELDS):
                            slot[f][k] = row[j]
                arr_long[code] = slot
            for code, slot in arr_long.items():
                bars[code] = slot
        print('bars 片 %d 完成，累计 %d 只' % (i, len(bars)))

    miss = [c for c in codes if c not in bars]
    if miss:
        print('缺 bars 的票:', len(miss), miss[:5])
    codes = [c for c in codes if c in bars]

    # ── 门控指数（与个股同用长表解析）──
    gdf = get_price([GATE_INDEX], end_date=END, count=len(days),
                    frequency='daily', fields=FIELDS)
    gate_bars = {f: np.full(len(days), np.nan) for f in FIELDS}
    if gdf is not None and not gdf.empty:
        if isinstance(gdf.columns, pd.MultiIndex):
            w = gdf.reindex(idx)
            for f in FIELDS:
                gate_bars[f] = w[GATE_INDEX][f].to_numpy(dtype=float)
        else:
            gdf = gdf.drop_duplicates(subset='time', keep='last')
            for t, row in zip(gdf['time'], gdf[FIELDS].to_numpy(dtype=float)):
                k = day_pos.get(pd.Timestamp(t).strftime('%Y-%m-%d'))
                if k is not None:
                    for j, f in enumerate(FIELDS):
                        gate_bars[f][k] = row[j]

    # ── 逐日换手率（实际值；新版返回长表 code/day/turnover_ratio）──
    tv = []
    for i in range(0, len(codes), 100):
        d = get_valuation(codes[i:i + 100], start_date=START, end_date=END,
                          fields=['turnover_ratio'])
        if d is not None and not d.empty:
            tv.append(d)
        print('估值片 %d 完成' % i)
    tv = pd.concat(tv) if tv else pd.DataFrame(
        columns=['code', 'day', 'turnover_ratio'])
    tv['day'] = tv['day'].map(lambda x: pd.Timestamp(str(x)).strftime('%Y-%m-%d'))
    turnover = {c: np.full(len(days), np.nan) for c in codes}
    for c, gdf2 in tv.groupby('code'):
        if c not in turnover:
            continue
        pos = {d: k for k, d in enumerate(days)}
        for d, tr in zip(gdf2['day'], gdf2['turnover_ratio']):
            k = pos.get(str(d))
            if k is not None:
                turnover[c][k] = float(tr)
    print('换手覆盖: %d/%d 只' % (len([c for c in codes
                                       if np.isfinite(turnover[c]).any()]), len(codes)))

    obj = {'meta': {'start': START, 'end': END, 'seed': SEED,
                    'n_sample': N_SAMPLE, 'note':
                    '静态主板快照；turnover 用逐日实际值替代 v3 的 T-1 外推'},
           'days': days, 'codes': codes, 'names': {c: pool.get(c, c) for c in codes},
           'bars': bars, 'gate_bars': gate_bars, 'turnover': turnover}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE, 'wb') as f:
        pickle.dump(obj, f)
    print('缓存写入:', CACHE, '大小MB=%.1f' % (os.path.getsize(CACHE) / 1e6))
    print('额度 after:', get_query_count())


if __name__ == '__main__':
    main()
