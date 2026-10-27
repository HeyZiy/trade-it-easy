# -*- coding: utf-8 -*-
"""基本面赞助面板构建：22 只行业 ETF 对应申万成分股的点时利润表缓存。

研究问题：高温行业入场的亏损是"标签资金主导"的普遍现象，还是集中在
没有基本面赞助（盈利未改善）的行业？（检验见 study_sponsorship.py）

预注册口径（本脚本只建数据面板，不做检验）：
- 成员：与温度计分母完全一致的申万时点成分（复用 temperature_members.pkl，
  INDATE<=t<OUTDATE）。
- 利润表：TGW get_income，REPORTING_PERIOD 2014-01-01 至 2026-09-30，
  STATEMENT_TYPE 1（合并累计）与 2（合并单季）均保留；字段
  REPORTING_PERIOD / ANN_DATE / ACTUAL_ANN_DATE / STATEMENT_TYPE /
  NET_PRO_EXCL_MIN_INT_INC（归母净利润，元）。
- 2014 起拉是为了 2016 年事件的"单季同比"能找到 2015/2014 同期基期。
- 点时纪律在检验脚本执行：可见性 = ACTUAL_ANN_DATE（缺则 ANN_DATE）<= 入场日
  前一自然日；同报告期取最晚披露版本（与 src/quality_pool/feeds.py::_visible
  同口径，此处独立实现避免研究脚本依赖生产模块内部函数）。

从仓库根运行：
  D:\\App\\Conda\\python.exe research/studies/industry_momentum/sponsorship_build.py
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from data_provider.fetchers import amazingdata_info as adi  # noqa: E402

BEGIN, END = '2014-01-01', '2026-09-30'
MEMBERS_CACHE = HERE / '_cache' / 'temperature_members.pkl'
INCOME_CACHE = HERE / '_cache' / 'sponsorship_income.pkl'
CHUNK = 128  # 每次调用内含 4 个 SDK 批（_INFO_CHUNK=32），逐块落盘断点续拉

INCOME_COLUMNS = ['REPORTING_PERIOD', 'ANN_DATE', 'ACTUAL_ANN_DATE',
                  'STATEMENT_TYPE', 'NET_PRO_EXCL_MIN_INT_INC']


def fetch_income_cached(codes: list[str]) -> dict[str, pd.DataFrame]:
    done: dict[str, pd.DataFrame] = {}
    if INCOME_CACHE.exists():
        with open(INCOME_CACHE, 'rb') as f:
            done = pickle.load(f)
    todo = [c for c in codes if c not in done]
    print(f'利润表待拉 {len(todo)} / {len(codes)} 只（缓存已有 {len(done)}）', flush=True)
    for i in range(0, len(todo), CHUNK):
        batch = todo[i:i + CHUNK]
        try:
            got = adi.fetch_income(batch, BEGIN, END)
        except Exception as e:  # 断点续拉：单块失败不弃全局
            print(f'  批 {i // CHUNK + 1} 失败（{batch[0]}~{batch[-1]}）：{e}', flush=True)
            continue
        for code, df in got.items():
            cols = [c for c in INCOME_COLUMNS if c in df.columns]
            done[code] = df[cols].copy()
        print(f'income {min(i + CHUNK, len(todo))}/{len(todo)} 完成', flush=True)
        INCOME_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(INCOME_CACHE, 'wb') as f:
            pickle.dump(done, f)
    return done


def main():
    with open(MEMBERS_CACHE, 'rb') as f:
        members = pickle.load(f)
    stock_union = sorted({str(c).split('.')[0]
                          for df in members.values() for c in df['CON_CODE']})
    print(f'成分股并集 {len(stock_union)} 只（与温度计分母同源）', flush=True)

    income = fetch_income_cached(stock_union)
    print(f'利润表缓存 {len(income)} 只', flush=True)

    # 面板体检：报表类型覆盖与年份分布
    rows = []
    for code, df in income.items():
        for st, df_t in df.groupby('STATEMENT_TYPE'):
            rows.append({'code': code, 'type': str(st), 'n': len(df_t),
                         'p_min': str(df_t['REPORTING_PERIOD'].min()),
                         'p_max': str(df_t['REPORTING_PERIOD'].max())})
    cov = pd.DataFrame(rows)
    if cov.empty:
        print('警告：无任何利润表数据')
        return
    print('\n== 面板体检 ==')
    for st, g in cov.groupby('type'):
        n_stock = g['code'].nunique()
        print(f'STATEMENT_TYPE={st}: {n_stock} 只, 报告期 {g["p_min"].min()}~{g["p_max"].max()}')
    # 单季（TYPE=2）逐年份覆盖
    t2 = cov[cov['type'] == '2']
    if not t2.empty:
        per = income
        year_rows = {}
        for code, df in per.items():
            d2 = df[pd.to_numeric(df['STATEMENT_TYPE'], errors='coerce') == 2]
            if d2.empty:
                continue
            p = pd.to_datetime(d2['REPORTING_PERIOD'].astype(str), errors='coerce')
            for y in p.dt.year.dropna().unique():
                year_rows[int(y)] = year_rows.get(int(y), 0) + 1
        print('单季报表有数据的股票数（按报告期年份）：',
              dict(sorted(year_rows.items())))


if __name__ == '__main__':
    main()
