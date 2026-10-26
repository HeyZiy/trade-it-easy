# -*- coding: utf-8 -*-
"""温度计面板构建：22 只行业 ETF 的 ETF/成分股成交额比。

研究问题：ETF 端成交额相对成分股端（申万时点成分聚合）的比值刻画"定价权
在标签资金还是选股资金"。本脚本只建面板，不做事件检验（见
study_temperature_gauge.py）。

口径：
- 分母 = ETF 对应申万指数时点成分（INDATE<=t<OUTDATE）当日 amount 之和
  （TGW query_kline，元）；分子 = 该 ETF 当日 amount（TGW，同单位）。
- ETF→申万映射见 SW_MAP；多指数组为并集。ETF 跟踪指数与申万口径不完全
  一致（如 512010 跟踪沪深300医药、分母为全医药生物），比值水平有 ETF 间
  常数性偏差，横截比较一律用自身历史分位。
- ratio_20 = 近 20 个交易日比值均值；pct = 截至 t-1 的 expanding 分位
  （min_obs=120，不足为 NaN，供事件连接时自然剔除早期事件）。

从仓库根运行：python research/studies/etf_rotation/temperature_build.py [--no-cache]
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from data_provider.fetchers import amazingdata_info as adi  # noqa: E402

BEGIN, END = '2016-01-01', '2026-09-30'

# ETF -> 申万指数（801xxx.SI）。多指数 = 并集（成员区间重叠按一次计）。
SW_MAP = {
    '515170': ['801120.SI'],                    # 食品饮料
    '512660': ['801740.SI'],                    # 军工 -> 国防军工
    '512690': ['801125.SI', '801126.SI'],       # 酒 -> 白酒Ⅱ+非白酒
    '512400': ['801050.SI'],                    # 有色 -> 有色金属
    '159928': ['801120.SI', '801010.SI'],       # 主要消费 -> 食品饮料+农林牧渔
    '515220': ['801950.SI'],                    # 煤炭
    '512010': ['801150.SI'],                    # 医药 -> 医药生物（宽于300医药）
    '515210': ['801040.SI'],                    # 钢铁
    '512170': ['801153.SI', '801156.SI'],       # 医疗 -> 医疗器械+医疗服务
    '159870': ['801030.SI'],                    # 化工 -> 基础化工
    '512800': ['801780.SI'],                    # 银行
    '159996': ['801110.SI'],                    # 家电 -> 家用电器
    '512000': ['801193.SI'],                    # 券商 -> 证券Ⅱ
    '512200': ['801180.SI'],                    # 房地产
    '512480': ['801081.SI'],                    # 半导体（电子二级）
    '159611': ['801161.SI'],                    # 电力（公用事业二级）
    '515880': ['801770.SI'],                    # 通信
    '516110': ['801880.SI'],                    # 汽车
    '512720': ['801750.SI'],                    # 计算机
    '512980': ['801760.SI'],                    # 传媒
    '515030': ['801737.SI', '801056.SI', '801093.SI'],  # 新能源车 -> 电池+能源金属+汽车零部件
    '515790': ['801735.SI'],                    # 光伏 -> 光伏设备
}

MEMBERS_CACHE = HERE / '_cache' / 'temperature_members.pkl'
PANEL_CACHE = HERE / '_cache' / 'temperature_panel.pkl'
KLINE_CACHE = HERE / '_cache' / 'temperature_kline.pkl'

MIN_PCT_OBS = 120
SMOOTH = 20


def fetch_members() -> dict[str, pd.DataFrame]:
    """各申万指数时点成分表（缓存）。"""
    if MEMBERS_CACHE.exists():
        with open(MEMBERS_CACHE, 'rb') as f:
            return pickle.load(f)
    out: dict[str, pd.DataFrame] = {}
    for idx in sorted({i for v in SW_MAP.values() for i in v}):
        raw = adi.fetch_industry_members(idx)
        df = raw[idx] if isinstance(raw, dict) else raw
        df = df[['CON_CODE', 'INDATE', 'OUTDATE']].copy()
        df['INDATE'] = pd.to_numeric(df['INDATE'], errors='coerce')
        df['OUTDATE'] = pd.to_numeric(df['OUTDATE'], errors='coerce')
        out[idx] = df.dropna(subset=['CON_CODE', 'INDATE'])
        print(f'members {idx}: {len(df)} 行', flush=True)
    MEMBERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMBERS_CACHE, 'wb') as f:
        pickle.dump(out, f)
    return out


def _six(tgw_code: str) -> str:
    return tgw_code.split('.')[0]


def fetch_klines(codes: list[str]) -> dict[str, pd.DataFrame]:
    """日线（date/close/high/amount，元），键为 6 位代码；含逐股缓存续拉。"""
    done: dict[str, pd.DataFrame] = {}
    if KLINE_CACHE.exists():
        with open(KLINE_CACHE, 'rb') as f:
            done = pickle.load(f)
    todo = [c for c in codes if c not in done]
    CHUNK = 150
    for i in range(0, len(todo), CHUNK):
        batch = todo[i:i + CHUNK]
        got = adi.fetch_kline(batch, BEGIN, END)
        for tgw, df in got.items():
            slim = df[['kline_time', 'close', 'high', 'amount']].copy()
            slim['date'] = pd.to_datetime(slim['kline_time']).dt.strftime('%Y%m%d')
            slim['date'] = pd.to_numeric(slim['date'])
            slim = slim.drop(columns=['kline_time']).set_index('date')
            done[_six(tgw)] = slim
        print(f'kline {min(i + CHUNK, len(todo))}/{len(todo)} 完成', flush=True)
        with open(KLINE_CACHE, 'wb') as f:  # 每 chunk 落盘，可断点续拉
            pickle.dump(done, f)
    return done


def limit_ratio_thresholds(code6: str) -> float:
    """涨停近似阈值：主板 10%，创业/科创 20%（ST 5% 无法从代码判定，漏计）。"""
    return 0.198 if code6[:2] in ('30', '68') else 0.098


def build_denominator(members: dict, klines: dict, calendar: pd.Index):
    """逐 ETF 日频分母：成分 amount 合计、活跃数、近涨停数（收盘=最高且涨幅过阈值）。"""
    per_stock = {}
    for code6, df in klines.items():
        if code6.startswith(('51', '15', '56', '58')):  # ETF 不进分母
            continue
        ret = df['close'].pct_change()
        near_limit = (df['close'] == df['high']) & (
            ret >= limit_ratio_thresholds(code6))
        per_stock[code6] = pd.DataFrame({
            'amount': df['amount'], 'near_limit': near_limit.astype(float)})
    panels = {}
    for etf, indices in SW_MAP.items():
        sum_amt = pd.Series(0.0, index=calendar)
        sum_lim = pd.Series(0.0, index=calendar)
        n_active = pd.Series(0.0, index=calendar)
        seen: set[str] = set()
        for idx in indices:
            for _, row in members[idx].iterrows():
                c = _six(str(row['CON_CODE']))
                if c in seen or c not in per_stock:  # 多指数去重：一股只计一次
                    continue
                seen.add(c)
                ps = per_stock[c]
                m = (ps.index >= row['INDATE'])
                if pd.notna(row['OUTDATE']):
                    m &= (ps.index < row['OUTDATE'])
                sl = ps[m]
                if sl.empty:
                    continue
                sum_amt.loc[sl.index] += sl['amount'].fillna(0.0)
                sum_lim.loc[sl.index] += sl['near_limit'].fillna(0.0)
                n_active.loc[sl.index] += 1.0
        panels[etf] = pd.DataFrame({
            'denom_amount': sum_amt, 'n_members': n_active,
            'n_near_limit': sum_lim})
        print(f'denom {etf}: 成员 {len(seen)}', flush=True)
    return panels


def build_temperature(etf_klines: dict, denom: dict) -> dict[str, pd.DataFrame]:
    out = {}
    for etf, k in etf_klines.items():
        d = denom[etf].reindex(k.index)
        ratio = k['amount'] / d['denom_amount'].replace(0.0, np.nan)
        smooth = ratio.rolling(SMOOTH, min_periods=SMOOTH).mean()
        # expanding 分位：截至 t-1（shift 后 t 日分位不含当日），事件取 t-1 值
        base = smooth.shift(1)
        pct = base.expanding(min_periods=MIN_PCT_OBS).apply(
            lambda s: (s <= s.iloc[-1]).mean(), raw=False)
        out[etf] = pd.DataFrame({
            'etf_amount': k['amount'], 'denom_amount': d['denom_amount'],
            'n_members': d['n_members'], 'n_near_limit': d['n_near_limit'],
            'ratio': ratio, 'ratio_20': smooth, 'temp_pct': pct})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-cache', action='store_true', help='重建成分缓存')
    args = ap.parse_args()
    if args.no_cache and MEMBERS_CACHE.exists():
        MEMBERS_CACHE.unlink()

    members = fetch_members()
    stock_union = sorted({_six(str(c)) for df in members.values()
                          for c in df['CON_CODE']})
    etf_codes = list(SW_MAP)
    print(f'成分股并集 {len(stock_union)} 只 + ETF {len(etf_codes)} 只', flush=True)

    klines = fetch_klines(stock_union + etf_codes)
    etf_klines = {c: klines[c] for c in etf_codes if c in klines}
    stock_klines = {c: df for c, df in klines.items() if c not in etf_codes}
    calendar = stock_klines['000001'].index if '000001' in stock_klines else \
        next(iter(stock_klines.values())).index
    cal = calendar[(calendar >= 20160101) & (calendar <= 20260930)]

    denom = build_denominator(members, klines, cal)
    panel = build_temperature(etf_klines, denom)
    with open(PANEL_CACHE, 'wb') as f:
        pickle.dump(panel, f)

    print('\n== 面板体检 ==')
    for etf, df in list(panel.items())[:22]:
        r = df['ratio_20'].dropna()
        if r.empty:
            print(f'{etf}: 无有效比值')
            continue
        print(f'{etf}: {len(r)} 天 ratio_20 中位 {r.median():.4f} '
              f'P10 {r.quantile(.1):.4f} P90 {r.quantile(.9):.4f} '
              f'首个分位日 {df["temp_pct"].first_valid_index()}')


if __name__ == '__main__':
    main()
