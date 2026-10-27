# -*- coding: utf-8 -*-
"""经典因子对照：温度/盈利广度是否只是动量的化身。

研究问题（用户提出的核心质疑）：行业侧经典因子（动量、波动率等）多轮
检验无果后启用的两个"怪因子"——温度计与盈利广度——是否只是经典动量
的重新包装？若温度的预测力在控制近期涨幅后消失，"标签资金主导"机制
叙事就是多余假设，规则应简化为"别追涨多的行业"；若广度只是长期动量
的影子，低温×有赞助单元同样不成立。

预注册设计（2026-10-26 定稿，先于取数）：
- 样本：温度计检验的 352 次有温度行业入场事件（events.csv），广度取
  sponsorship/events.csv 同一事件的 breadth_sq（点时单季口径）。
- 经典因子（全部用 t-1 及以前的收盘，无未来数据，TGW 复权 close）：
  ret20 = close[t-21] → close[t-1] 的涨幅（温度平滑窗同长度）；
  mom12_1 = close[t-252] → close[t-21] 的涨幅（12-1 月动量）。
- 检验 A（温度 vs 短期动量）：
  A1 Spearman(temp_pct, ret20)——共线性总量；
  A2 ret20 三分位（分位边界在 352 事件上一次计算）内，温度 ≥2/3 vs
  <2/3 的 excess_h10 均值差——温度在涨幅层内的条件区分力；
  A3 残差法：rank(temp) 对 rank(ret20) 线性回归，残差按中位数分高低，
  两组 excess_h10 均值差——剔除线性动量成分后的温度读数。
- 检验 B（广度 vs 长期动量）：
  B1 Spearman(breadth, mom12_1)；
  B2 mom12_1 三分位内，广度 ≥0.5 vs <0.5 的 excess_h10 均值差；
  B3 低温桶（temp<2/3，唯一正单元所在层）内：mom12_1 中位数分高低，
  各自的 excess_h10——看正单元是否只是"低温+高长期动量"。
- 判读规则（先固定）：某因子"通过控制"= 条件差在 ≥2/3 的层内保持
  原符号且全样本条件差同号；描述性对照，不做显著性断言。
- 边界照旧：事件窗口重叠、时间聚集、样本内。

从仓库根运行：python research/studies/industry_momentum/study_classic_factors.py
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EVENTS_CSV = HERE / 'results' / 'temperature' / 'events.csv'
SPONSOR_CSV = HERE / 'results' / 'sponsorship' / 'events.csv'
KLINE_CACHE = HERE / '_cache' / 'temperature_kline.pkl'
OUT_DIR = HERE / 'results' / 'classic_factors'

TEMP_HIGH = 2 / 3
BREADTH_SPLIT = 0.5


def _ret_window(closes: pd.Series, t: int, back_start: int, back_end: int):
    """close[t-back_start] → close[t-back_end] 涨幅；索引不齐或历史不足为 NaN。

    back_start/back_end 为交易日偏移（如 21/1 = ret20；252/21 = 12-1 动量），
    用绝对位置定位而非 shift，避免停牌缺口错位：取索引中 t 的位置 i，
    起点 = 索引第 i-back_start 个交易日。
    """
    if t not in closes.index:
        return np.nan
    i = closes.index.get_loc(t)
    j0, j1 = i - back_start, i - back_end
    if j0 < 0 or j1 <= j0:
        return np.nan
    c0, c1 = closes.iloc[j0], closes.iloc[j1]
    if not (np.isfinite(c0) and np.isfinite(c1) and c0 > 0):
        return np.nan
    return c1 / c0 - 1.0


def run_self_tests() -> int:
    n = 0

    def check(name, cond):
        nonlocal n
        assert cond, f'自检失败: {name}'
        n += 1

    idx = [20260101 + k for k in range(1, 301)]  # 连续"交易日"
    closes = pd.Series(np.linspace(100.0, 200.0, 300), index=idx)
    # ret20：i=299（t=20260300? 用末位索引）——直接取 i=299: t=idx[-1]
    t = idx[-1]
    r20 = _ret_window(closes, t, 21, 1)
    expect = closes.iloc[-2] / closes.iloc[-22] - 1
    check('ret20 窗口', abs(r20 - expect) < 1e-12)
    m = _ret_window(closes, t, 252, 21)
    expect_m = closes.iloc[-22] / closes.iloc[-253] - 1
    check('mom12_1 窗口', abs(m - expect_m) < 1e-12)
    check('历史不足→NaN', np.isnan(_ret_window(closes, idx[50], 252, 21)))
    check('t 不在索引→NaN', np.isnan(_ret_window(closes, 99999999, 21, 1)))
    print(f'自检 {n} 项通过', flush=True)
    return n


def main():
    n_tests = run_self_tests()

    ev = pd.read_csv(EVENTS_CSV, encoding='utf-8-sig', dtype={'code': str})
    ev['code'] = ev['code'].str.zfill(6)
    ev['date_int'] = ev['date_int'].astype(int)
    ev = ev[ev['temp_pct'].notna()].copy()
    sp = pd.read_csv(SPONSOR_CSV, encoding='utf-8-sig', dtype={'code': str})
    sp['code'] = sp['code'].str.zfill(6)
    sp['date_int'] = sp['date_int'].astype(int)
    ev = ev.merge(sp[['code', 'date_int', 'breadth_sq']], on=['code', 'date_int'],
                  how='left')

    with open(KLINE_CACHE, 'rb') as f:
        klines = pickle.load(f)
    r20, m121 = [], []
    for _, r in ev.iterrows():
        k = klines.get(r['code'])
        if k is None:
            r20.append(np.nan)
            m121.append(np.nan)
            continue
        closes = k['close'].astype(float)
        r20.append(_ret_window(closes, int(r['date_int']), 21, 1))
        m121.append(_ret_window(closes, int(r['date_int']), 252, 21))
    ev['ret20'] = r20
    ev['mom12_1'] = m121

    ev['excess_h10'] = pd.to_numeric(ev['excess_h10'], errors='coerce') * 100

    def cell_mean(df):
        x = df['excess_h10'].dropna()
        return (round(float(x.mean()), 2), int(len(x))) if len(x) else (None, 0)

    def tercile_split(df, col):
        """三分位边界在全样本一次计算；层内 温度/广度 切分的均值差。"""
        q1, q2 = df[col].quantile([1 / 3, 2 / 3])
        out = {}
        for name, m in (('低', df[col] <= q1), ('中', (df[col] > q1) & (df[col] <= q2)),
                        ('高', df[col] > q2)):
            sub = df[m]
            hi, nhi = cell_mean(sub[sub['temp_pct'] >= TEMP_HIGH])
            lo, nlo = cell_mean(sub[sub['temp_pct'] < TEMP_HIGH])
            out[name] = {'n': int(len(sub)), '温度高': hi, '温度高_n': nhi,
                         '温度低': lo, '温度低_n': nlo,
                         '差_pp': round(hi - lo, 2) if hi is not None and lo is not None else None}
        return out

    out = {'events': int(len(ev)), 'self_tests_passed': n_tests,
           'ret20_coverage': int(ev['ret20'].notna().sum()),
           'mom12_1_coverage': int(ev['mom12_1'].notna().sum()),
           'breadth_coverage': int(ev['breadth_sq'].notna().sum())}

    # ── 检验 A：温度 vs 短期动量 ──
    a = {}
    a['A1_spearman_temp_ret20'] = round(float(
        ev[['temp_pct', 'ret20']].dropna().corr(method='spearman').iloc[0, 1]), 3)
    a['A1_spearman_temp_mom12_1'] = round(float(
        ev[['temp_pct', 'mom12_1']].dropna().corr(method='spearman').iloc[0, 1]), 3)
    a['A2_ret20三分位内温度切分'] = tercile_split(ev, 'ret20')
    # A3 残差法
    sub = ev.dropna(subset=['temp_pct', 'ret20', 'excess_h10']).copy()
    rt, rr = sub['temp_pct'].rank(), sub['ret20'].rank()
    resid = rt - np.polyval(np.polyfit(rr, rt, 1), rr)
    sub['temp_resid'] = resid
    med = sub['temp_resid'].median()
    hi_r, nhi = cell_mean(sub[sub['temp_resid'] > med])
    lo_r, nlo = cell_mean(sub[sub['temp_resid'] <= med])
    a['A3_残差温度中位切分'] = {
        '残差高(动量解释不掉的温度)': hi_r, 'n': nhi,
        '残差低': lo_r, 'n2': nlo,
        '差_pp': round(hi_r - lo_r, 2) if hi_r is not None and lo_r is not None else None}
    out['检验A_温度vs短期动量'] = a

    # ── 检验 B：广度 vs 长期动量 ──
    b = {}
    b['B1_spearman_breadth_mom12_1'] = round(float(
        ev[['breadth_sq', 'mom12_1']].dropna().corr(method='spearman').iloc[0, 1]), 3)
    # B2：mom 三分位内广度切分
    q1, q2 = ev['mom12_1'].quantile([1 / 3, 2 / 3])
    b2 = {}
    for name, m in (('低', ev['mom12_1'] <= q1), ('中', (ev['mom12_1'] > q1) & (ev['mom12_1'] <= q2)),
                    ('高', ev['mom12_1'] > q2)):
        s = ev[m]
        y, ny = cell_mean(s[s['breadth_sq'] >= BREADTH_SPLIT])
        no, nn = cell_mean(s[s['breadth_sq'] < BREADTH_SPLIT])
        b2[name] = {'n': int(len(s)), '有赞助': y, '有赞助_n': ny,
                    '无赞助': no, '无赞助_n': nn,
                    '差_pp': round(y - no, 2) if y is not None and no is not None else None}
    b['B2_mom三分位内广度切分'] = b2
    # B3：低温桶内 mom 中位切分；及 低温×赞助 内 mom 切分
    lo_t = ev[ev['temp_pct'] < TEMP_HIGH]
    med_m = lo_t['mom12_1'].median()
    hm, nhm = cell_mean(lo_t[lo_t['mom12_1'] > med_m])
    lm, nlm = cell_mean(lo_t[lo_t['mom12_1'] <= med_m])
    b['B3_低温桶内mom中位切分'] = {'n': int(len(lo_t)), '高mom': hm, '高mom_n': nhm,
                                 '低mom': lm, '低mom_n': nlm}
    lo_sp = lo_t[lo_t['breadth_sq'] >= BREADTH_SPLIT]  # 唯一正单元
    med_s = lo_sp['mom12_1'].median()
    hs, nhs = cell_mean(lo_sp[lo_sp['mom12_1'] > med_s])
    ls, nls = cell_mean(lo_sp[lo_sp['mom12_1'] <= med_s])
    b['B3_低温×赞助内mom中位切分'] = {'n': int(len(lo_sp)), '高mom': hs, '高mom_n': nhs,
                                     '低mom': ls, '低mom_n': nls}
    out['检验B_广度vs长期动量'] = b

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ev_out = ev.drop(columns=[c for c in ('excess_h5', 'excess_h20') if c in ev.columns],
                     errors='ignore')
    ev_out.to_csv(OUT_DIR / 'events_classic.csv', index=False)
    with open(OUT_DIR / 'summary.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=float)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=float))
    print(f'\n结果写入 {OUT_DIR}')


if __name__ == '__main__':
    main()
