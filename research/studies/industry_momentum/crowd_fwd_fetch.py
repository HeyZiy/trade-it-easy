# -*- coding: utf-8 -*-
# crowd_fwd_fetch —— 全市场 ETF 日线本地缓存（量价热度前瞻实验取数，2026-10-06）
#
# 目的：为 crowd_fwd_v1 本地实验提供全市场在市 ETF 的 qfq 收盘 + amount 全历史，
#   一次取齐落盘，之后实验离线跑。
# 口径：data_provider.bars.get_etf_daily(adjust='qfq')（份额折算前复权单点；
#   amount 元口径不复权）；名单 = get_etf_universe()（新浪当前在市表，
#   已退市不在列 → 下游实验自带幸存者偏差，须在报告声明）。
# 断点续传：已缓存的码跳过；失败码记 _cache/etf_fetch_failed.json（时间戳），
#   重跑自动重试失败码。每 25 只落盘一次，中断不丢进度。
# 产出：_cache/etf_daily_full.pkl = dict[code -> DataFrame(index=date, [close(qfq), amount])]
#       _cache/etf_universe.pkl  = DataFrame[code, name]

import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from data_provider.bars import get_etf_daily, get_etf_universe

HERE = Path(__file__).resolve().parent
CACHE = HERE / '_cache'
DAILY_PKL = CACHE / 'etf_daily_full.pkl'
UNI_PKL = CACHE / 'etf_universe.pkl'
FAIL_JSON = CACHE / 'etf_fetch_failed.json'
SAVE_EVERY = 25


def main():
    CACHE.mkdir(exist_ok=True)
    uni = get_etf_universe()
    if uni is None:
        print('FATAL: universe 获取失败', flush=True)
        return 1
    uni.to_pickle(UNI_PKL)
    codes = uni['code'].tolist()
    print(f'universe {len(codes)} 只', flush=True)

    daily = {}
    if DAILY_PKL.exists():
        daily = pd.read_pickle(DAILY_PKL)
        print(f'已有缓存 {len(daily)} 只，续传', flush=True)
    failed = {}
    if FAIL_JSON.exists():
        failed = json.loads(FAIL_JSON.read_text(encoding='utf-8'))

    todo = [c for c in codes if c not in daily]
    print(f'待取 {len(todo)} 只', flush=True)
    n_ok = n_fail = 0
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        df = get_etf_daily(code, adjust='qfq')
        if df is None or df.empty:
            failed[code] = time.strftime('%Y-%m-%d %H:%M:%S')
            n_fail += 1
        else:
            daily[code] = df.set_index('date')[['close', 'amount']].astype(float)
            failed.pop(code, None)
            n_ok += 1
        if i % SAVE_EVERY == 0:
            pd.to_pickle(daily, DAILY_PKL)
            FAIL_JSON.write_text(json.dumps(failed, ensure_ascii=False, indent=0),
                                 encoding='utf-8')
            rate = i / max(time.time() - t0, 1e-9)
            remain = (len(todo) - i) / rate / 60 if rate > 0 else -1
            print(f'[{i}/{len(todo)}] 本次 ok {n_ok} fail {n_fail} | '
                  f'{rate:.1f} 只/秒 | 剩余≈{remain:.0f} 分钟', flush=True)
    pd.to_pickle(daily, DAILY_PKL)
    FAIL_JSON.write_text(json.dumps(failed, ensure_ascii=False, indent=0),
                         encoding='utf-8')
    print(f'完成：缓存 {len(daily)} 只 / 本次 ok {n_ok} fail {n_fail} / '
          f'用时 {round((time.time() - t0) / 60, 1)} 分钟', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
