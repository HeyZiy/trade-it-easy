# -*- coding: utf-8 -*-
"""行业估值面板构建：22 只行业 ETF 的时点 PE 及其自身历史分位。

研究问题（经典因子对照第三战）：双闸（温度×盈利广度）的行业选择是否
只是"避开贵行业、买便宜行业"的估值效应？若低温×赞助正单元集中在
低估值分位、高温亏损集中在高估值分位，机制叙事需让位于估值解释。

口径（预注册，2026-10-26 定稿）：
- 个股 TTM 归母净利 = 最新可见的连续四个单季（STATEMENT_TYPE=2）
  归母净利之和；点时纪律同 sponsorship（披露日≤t-1、同报告期最晚版）。
- 总股本 = equity_structure 的 CHANGE_DATE 时点最新值（缺历史起点时
  用最早一条，隐含"上市以来股本不变"近似）。
- 个股市值 = 总股本 × 原始收盘（temperature_kline，未复权）。
- 行业 PE(t) = Σ成分股市值(t) ÷ Σ成分股 TTM(t)；TTM≤0 的个股净利
  原值计入分母（负利润压低分母→行业 PE 抬高，官方指数口径，不剔除）。
- 分位 = PE 的自身 expanding 分位（min_obs=250 交易日，约一年，取
  t-1 值，与温度计同法无未来数据）。
- 成分窗口 INDATE≤t<OUTDATE，与温度计分母同源。

从仓库根运行（TGW SDK 需写公共文档目录，须在非沙箱终端）：
  python research/studies/industry_momentum/valuation_build.py
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from data_provider.fetchers import amazingdata_info as adi  # noqa: E402
from research.studies.industry_momentum.temperature_build import SW_MAP  # noqa: E402

BEGIN, END = '1990-01-01', '2026-09-30'   # 股本按变动日期查询，须从最早拉
                                          # （多年未变动的股本只有很早一条记录，
                                          #   窗口起点晚于变动日 → 历史全缺，
                                          #   同 quality_pool 的 EQUITY_HISTORY_BEGIN）
MEMBERS_CACHE = HERE / '_cache' / 'temperature_members.pkl'
KLINE_CACHE = HERE / '_cache' / 'temperature_kline.pkl'
INCOME_CACHE = HERE / '_cache' / 'sponsorship_income.pkl'
EQUITY_CACHE = HERE / '_cache' / 'valuation_equity.pkl'
PANEL_CACHE = HERE / '_cache' / 'valuation_panel.pkl'

CHUNK = 128
MIN_PCT_OBS = 250


def fetch_equity_cached(codes: list[str]) -> dict[str, pd.DataFrame]:
    """总股本历史（CHANGE_DATE/TOT_SHARE），断点续拉。"""
    done: dict[str, pd.DataFrame] = {}
    if EQUITY_CACHE.exists():
        with open(EQUITY_CACHE, 'rb') as f:
            done = pickle.load(f)
    todo = [c for c in codes if c not in done]
    print(f'股本待拉 {len(todo)} / {len(codes)} 只（缓存已有 {len(done)}）', flush=True)
    for i in range(0, len(todo), CHUNK):
        batch = todo[i:i + CHUNK]
        try:
            got = adi.fetch_equity_structure(batch, BEGIN, END)
        except Exception as e:
            print(f'  批 {i // CHUNK + 1} 失败（{batch[0]}~{batch[-1]}）：{e}', flush=True)
            continue
        n_new = 0
        for code, df in got.items():
            if df is None or df.empty:
                continue
            c6 = str(code).split('.')[0]
            done[c6] = df[['CHANGE_DATE', 'TOT_SHARE']].copy()
            n_new += 1
        print(f'equity {min(i + CHUNK, len(todo))}/{len(todo)} 完成（新增 {n_new}）', flush=True)
        EQUITY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(EQUITY_CACHE, 'wb') as f:
            pickle.dump(done, f)
    return done


def _date_int(s: pd.Series) -> pd.Series:
    """CHANGE_DATE 任意形态 → int YYYYMMDD。"""
    if pd.api.types.is_numeric_dtype(s):
        txt = s.astype('Int64').astype(str)
        ts = pd.to_datetime(txt, format='%Y%m%d', errors='coerce')
    else:
        ts = pd.to_datetime(s, errors='coerce')
    return ts.dt.strftime('%Y%m%d').astype(float).fillna(-1).astype(np.int64)


def build_ttm_timeline(income: dict) -> dict:
    """每股披露时间线 → 逐披露日的 TTM（连续四个单季之和）。

    沿用 study_sponsorship.build_snapshots 的点时纪律，但状态是
    "最近四个单季报告期" 的滚动窗口：每披露一条单季，窗口更新为最近
    四个已见报告期（不足四个 → None，不硬凑）。
    """
    out = {}
    for code, df in income.items():
        need = {'REPORTING_PERIOD', 'ANN_DATE', 'ACTUAL_ANN_DATE',
                'STATEMENT_TYPE', 'NET_PRO_EXCL_MIN_INT_INC'}
        if df is None or df.empty or not need <= set(df.columns):
            continue
        d = df.copy()
        act = (_date_int(d['ACTUAL_ANN_DATE'])
               if 'ACTUAL_ANN_DATE' in d.columns else np.full(len(d), -1))
        ann = _date_int(d['ANN_DATE'])
        d['ann'] = np.where(act > 0, act, ann)
        d['period'] = _date_int(d['REPORTING_PERIOD'])
        d['np'] = pd.to_numeric(d['NET_PRO_EXCL_MIN_INT_INC'], errors='coerce')
        d['st'] = pd.to_numeric(d['STATEMENT_TYPE'], errors='coerce')
        d = d[(d['ann'] > 0) & (d['period'] > 0) & (d['st'] == 2)]
        d = d.dropna(subset=['np'])
        if d.empty:
            continue
        d = d.sort_values(['ann', 'period'], kind='stable')
        # 同报告期取最晚披露（更正报）：ann 升序扫描，后写覆盖
        quarter: dict[int, float] = {}
        rows = []
        for ann_d, grp in d.groupby('ann', sort=True):
            for _, r in grp.iterrows():
                quarter[int(r['period'])] = float(r['np'])
            periods = sorted(quarter)
            last4 = periods[-4:]
            if len(last4) == 4:
                # 连续性检查：四个报告期恰为连续季度（YYYYMMDD 相邻）
                ok = all((last4[k + 1] - last4[k]) in (30000, 60000, 310000, 920000, 1000000)
                         or _month_gap(last4[k], last4[k + 1]) == 3
                         for k in range(3))
                if ok:
                    rows.append((int(ann_d), sum(quarter[p] for p in last4)))
        if rows:
            arr = np.array(rows, dtype=float)
            out[code] = (arr[:, 0].astype(np.int64), arr[:, 1])
    return out


def _month_gap(p1: int, p2: int) -> int:
    """报告期间隔月数（3=连续季度），跨年正确。"""
    y1, m1 = p1 // 10000, (p1 // 100) % 100
    y2, m2 = p2 // 10000, (p2 // 100) % 100
    return (y2 - y1) * 12 + (m2 - m1)


def main():
    with open(MEMBERS_CACHE, 'rb') as f:
        members = pickle.load(f)
    with open(KLINE_CACHE, 'rb') as f:
        klines = pickle.load(f)
    with open(INCOME_CACHE, 'rb') as f:
        income = pickle.load(f)

    stock_union = sorted({str(c).split('.')[0]
                          for df in members.values() for c in df['CON_CODE']})
    print(f'成分股并集 {len(stock_union)} 只', flush=True)

    equity = fetch_equity_cached(stock_union)
    print(f'股本缓存 {len(equity)} 只', flush=True)

    print('构建 TTM 时间线…', flush=True)
    ttm = build_ttm_timeline(income)
    print(f'TTM 时间线 {len(ttm)} 只', flush=True)

    # 交易日历：取某只大盘股日线索引（温度计同法）
    cal_src = next((klines[c] for c in ('000001', '600000')
                    if c in klines), None)
    if cal_src is None:
        cal_src = next(iter(klines.values()))
    days = np.array(sorted({int(d) for d in cal_src.index
                            if 20160101 <= int(d) <= 20260930}), dtype=np.int64)
    n_days = len(days)

    # 个股逐日 TTM / 股本 / 收盘（时点可见）
    ttm_daily, cap_daily = {}, {}
    for code in stock_union:
        t = ttm.get(code)
        if t is None:
            continue
        anns, vals = t
        i = np.searchsorted(anns, days - 1, side='right') - 1  # 披露≤t-1
        vis = np.full(n_days, np.nan)
        hit = i >= 0
        vis[hit] = vals[i[hit]]
        ttm_daily[code] = vis

        eq = equity.get(code)
        k = klines.get(code)
        if eq is None or k is None:
            continue
        e = eq.copy()
        e['cd'] = _date_int(e['CHANGE_DATE'])
        e = e[e['cd'] > 0].sort_values('cd')
        shares = pd.to_numeric(e['TOT_SHARE'], errors='coerce').to_numpy()
        cds = e['cd'].to_numpy()
        j = np.searchsorted(cds, days, side='right') - 1
        sh = np.full(n_days, np.nan)
        hit2 = j >= 0
        sh[hit2] = shares[j[hit2]]
        # 多年未变动的股本只有一条近期快照记录（CHANGE_DATE=快照日而非
        # 真实变动日），首条记录之前的日子按"股本不变"近似回溯填充；
        # 历史增发的股票市值会被高估，PE 分位只吸收部分该误差，登记边界。
        first = shares[np.argmax(np.isfinite(shares))] if np.isfinite(shares).any() else np.nan
        sh[~hit2] = first
        closes = k['close'].astype(float).reindex(days).to_numpy()
        # TOT_SHARE 单位万股、close 单位元 → 市值万元；净利单位元，
        # 统一到元：cap(万元)×1e4。量纲错只差常数倍（分位不变），仍修正。
        cap_daily[code] = sh * closes * 1e4

    # 行业聚合
    panel = {}
    for etf, indices in SW_MAP.items():
        sum_cap = np.zeros(n_days)
        sum_ttm = np.zeros(n_days)
        seen: set[str] = set()
        for idx in indices:
            df = members[idx]
            for con, ind, o in zip(df['CON_CODE'].astype(str), df['INDATE'],
                                   df.get('OUTDATE')):
                c6 = con.split('.')[0]
                if c6 in seen:
                    continue
                seen.add(c6)
                m = (days >= np.int64(ind))
                if pd.notna(o):
                    m &= (days < np.int64(o))
                if c6 in cap_daily:
                    sum_cap[m] += np.nan_to_num(cap_daily[c6][m])
                if c6 in ttm_daily:
                    sum_ttm[m] += np.nan_to_num(ttm_daily[c6][m])
        with np.errstate(invalid='ignore', divide='ignore'):
            pe = np.where(sum_ttm != 0, sum_cap / sum_ttm, np.nan)
        s = pd.Series(pe, index=days)
        # 自身 expanding 分位，取 t-1（shift 同温度计）；向量化 rank
        pct = (s.shift(1).expanding(min_periods=MIN_PCT_OBS)
               .rank(pct=True))
        panel[etf] = pd.DataFrame({'pe': pe, 'pe_pct': pct.to_numpy(),
                                   'sum_cap': sum_cap, 'sum_ttm': sum_ttm},
                                  index=days)
        valid = s.dropna()
        print(f'{etf}: PE 有效 {len(valid)} 天，中位 {valid.median():.1f}，'
              f'首个分位日 {int(panel[etf]["pe_pct"].first_valid_index() or -1)}',
              flush=True)

    with open(PANEL_CACHE, 'wb') as f:
        pickle.dump(panel, f)
    print(f'\n面板写入 {PANEL_CACHE}')


if __name__ == '__main__':
    main()
