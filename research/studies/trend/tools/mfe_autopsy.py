# -*- coding: utf-8 -*-
"""方案A信号尸检：v3 回踩信号有没有右尾（离线，零额度，数据来自 _cache/jq_daily.pkl）。

时序映射（对齐 v3 引擎语义）：v3 在交易日 T 的 14:55 用截至 T-1 的 61 根日线判信号、
当日收盘附近成交。本脚本等价：评估日 i（窗口含 close[i]）→ 成交日 i+1（入场价
close[i+1]）→ 前瞻 20 日 = i+2..i+21 的 high/low/close。

判据（预先声明）：
  - 信号组 MFE20 分布 ≈ 随机对照组 → 信号死（右尾不存在），换票池；
  - 信号组显著右偏但 MAE20 深到多数会被 2.5ATR 出场线先打掉 → 消费侧问题；
  - 都满足不了（右尾有、路径也拿得住）却没在回测兑现 → 回头查执行差异。
"""
import math
import os
import pickle
import random
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BARS_NEEDED = 61
ATR_N = 20
K_ATR = 2.5
TURNOVER_MAX = 12.0
FROM_60D_LOW_MAX = 80.0
MA5_HOLD_TOLERANCE = 0.995
PULLBACK_TOUCH_TOL = 1.005
ATR_EXP_THR = 1.3
GATE_LOOKBACK = 25
FWD = 20
MIN_SCORE_BUY = 60          # v3 买侧只消费 score>=60


def _has_nan(vals):
    for x in vals:
        if x != x:
            return True
    return False


def _sma(vals, n):
    w = vals[-n:]
    if len(w) < n or _has_nan(w):
        return None
    return sum(w) / float(n)


def _tr_series(h, l, c):
    trs = []
    for i in range(1, len(c)):
        pc = c[i - 1]
        trs.append(max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc)))
    return trs


def _atr20(h, l, c):
    n = ATR_N + 1
    if len(c) < n or _has_nan(h[-n:]) or _has_nan(l[-n:]) or _has_nan(c[-n:]):
        return None
    atr = sum(_tr_series(h[-n:], l[-n:], c[-n:])) / float(ATR_N)
    return atr if atr > 0 else None


def _position_penalty(pos):
    if pos <= 25:
        return 0.0
    if pos >= 75:
        return 20.0
    return (pos - 25) * 0.4


def _score_ma5_family(bias, vr, pct, tr, cp, ls, above_count, pos, cap):
    if 0.0 <= bias <= 2.0:
        bias_score = 25 - abs(bias - 0.5) * 10
    elif -1.0 <= bias < 0.0:
        bias_score = 20 - abs(bias) * 5
    else:
        bias_score = max(0, 10 - abs(bias - 2.0) * 5)
    if 0.5 <= vr <= 0.9:
        vol_score = 20 - abs(vr - 0.7) * 25
    elif 0.4 <= vr < 0.5:
        vol_score = 12
    elif 0.9 < vr <= 1.05:
        vol_score = 10 - (vr - 0.9) * 66
    else:
        vol_score = max(0, 5)
    if -2.0 <= pct <= 1.0:
        pct_score = 15 - abs(pct) * 3
    elif -4.0 <= pct < -2.0 or 1.0 < pct <= 4.0:
        pct_score = 10 - abs(pct - 1.0) * 2
    else:
        pct_score = max(0, 5)
    if 4.0 <= tr <= 7.0:
        tr_score = 15
    elif 3.0 <= tr < 4.0 or 7.0 < tr <= 10.0:
        tr_score = 10
    else:
        tr_score = max(0, 5)
    id_score = 15 if (cp > 0.6 and ls > 0.2) else (10 if (cp > 0.4 and ls > 0.1) else 5)
    score = bias_score + vol_score + pct_score + tr_score + id_score + min(10, above_count * 2)
    score -= _position_penalty(pos)
    return min(cap, max(50, score))


def _score_ma10(bias, vr, pct, tr, cp, ls, touches, pos):
    if -3.0 <= bias <= 0:
        bias_score = 15 - abs(bias + 1.0) * 3
    else:
        bias_score = max(0, 8 - abs(bias) * 2)
    vol_score = (20 - abs(vr - 0.6) * 20) if 0.4 <= vr <= 0.9 else max(0, 8)
    if -4.0 <= pct <= 1.0:
        pct_score = 15 - abs(pct + 1.0) * 2
    else:
        pct_score = max(0, 5)
    if 3.0 <= tr <= 8.0:
        tr_score = 20 - abs(tr - 5.0) * 3
    else:
        tr_score = max(0, 8)
    id_score = 10 if (cp > 0.4 and ls > 0.1) else 5
    score = bias_score + vol_score + pct_score + tr_score + id_score + (15 if touches else 0)
    score -= _position_penalty(pos)
    return min(85, max(30, score))


def detect_signal(bars, turnover):
    """trend_v3.detect_signal 逐字复刻（去 log/tally 诊断分支）。"""
    o, h, l, c, v = (bars['open'], bars['high'], bars['low'],
                    bars['close'], bars['volume'])
    o = [float(x) for x in o]
    c = [float(x) for x in c]
    h = [float(x) for x in h]
    l = [float(x) for x in l]
    if len(c) < BARS_NEEDED or _has_nan(c[-BARS_NEEDED:]):
        return None
    ma5, ma10, ma20 = _sma(c, 5), _sma(c, 10), _sma(c, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return None
    price = c[-1]
    if price <= 0:
        return None
    ma20_prev = sum(c[-21:-1]) / 20.0
    if not (ma10 > ma20 and price > ma20 and ma20 >= ma20_prev):
        return None

    bias5 = (price - ma5) / ma5 * 100
    pct = (price / c[-2] - 1) * 100 if c[-2] > 0 else 0.0
    holds = price >= ma5 * MA5_HOLD_TOLERANCE
    rng = h[-1] - l[-1]
    cp = (price - l[-1]) / rng if rng > 0 else 0.5
    ls = (min(o[-1], price) - l[-1]) / rng if rng > 0 else 0.1
    low60 = min(c[-60:])
    pos_gain = (price - low60) / low60 * 100 if low60 > 0 else 0.0

    p5 = v[-6:-1]
    p5 = [float(x) for x in p5 if x == x]
    vr = (float(v[-1]) / (sum(p5) / len(p5))) if p5 and sum(p5) > 0 else 1.0

    above = 0
    for i in range(len(c) - 6, len(c) - 1):
        m5i = sum(c[i - 4:i + 1]) / 5.0
        if c[i] > m5i:
            above += 1

    trs = _tr_series(h, l, c)
    if len(trs) >= 20:
        a5 = sum(trs[-5:]) / 5.0
        a20 = sum(trs[-20:]) / 20.0
        if a20 > 0 and a5 / a20 > ATR_EXP_THR:
            return None                      # C1 剔除
    ma5_prev = _sma(c[:-1], 5)
    if ma5_prev is not None and ma5 < ma5_prev:
        return None                          # D1 剔除

    overext = pos_gain >= FROM_60D_LOW_MAX
    sig = None
    setup = holds and not overext
    if setup:
        shape = pct < 0 or l[-1] <= ma5 * PULLBACK_TOUCH_TOL
        if shape:
            sig = ('pullback_ma5', _score_ma5_family(
                bias5, vr, pct, turnover, cp, ls, above, pos_gain, 95))
        else:
            sig = ('near_ma5', _score_ma5_family(
                bias5, vr, pct, turnover, cp, ls, above, pos_gain, 79))
    elif (l[-1] <= ma10 * 1.01 and price >= ma10
          and not overext and not holds):
        sig = ('pullback_ma10', _score_ma10(
            bias5, vr, pct, turnover, cp, ls, True, pos_gain))
    return sig


def structure_ok(bars):
    """v3 趋势结构条件（ma10>ma20、价>ma20、MA20 不降），对照子组用。"""
    c = [float(x) for x in bars['close']]
    if len(c) < 21 or _has_nan(c[-21:]):
        return False
    ma10, ma20 = _sma(c, 10), _sma(c, 20)
    if ma10 is None or ma20 is None:
        return False
    ma20_prev = sum(c[-21:-1]) / 20.0
    return ma10 > ma20 and c[-1] > ma20 and ma20 >= ma20_prev


def main():
    with open(os.path.join(HERE, '_cache', 'jq_daily.pkl'), 'rb') as f:
        data = pickle.load(f)
    days, codes = data['days'], data['codes']
    bars_all, turn_all = data['bars'], data['turnover']
    gate_c = data['gate_bars']['close']
    n = len(days)

    # 门控：成交日 j 用截至 j 的 25 个收盘（对齐 v3 在 T 日 14:55 的口径）
    def gate_open(j):
        if j + 1 < GATE_LOOKBACK:
            return False
        cl = [float(x) for x in gate_c[j - GATE_LOOKBACK + 1: j + 1] if x == x]
        if len(cl) < GATE_LOOKBACK:
            return False
        ma20 = sum(cl[-20:]) / 20.0
        ma20_prev = sum(cl[-25:-5]) / 20.0
        return cl[-1] > ma20 and ma20 >= ma20_prev

    I0 = BARS_NEEDED - 1          # 首个可评估 i（窗口满 61）
    I1 = n - 2 - FWD              # 需 i+1 成交 + i+2..i+21 前瞻

    events, ctrl_pool = [], []
    for i in range(I0, I1 + 1):
        j = i + 1                 # 成交日
        if not gate_open(j):
            continue
        for code in codes:
            tr = turn_all[code][i]
            if not (tr == tr) or tr > TURNOVER_MAX:
                continue
            b = bars_all[code]
            sl = {f: b[f][i - BARS_NEEDED + 1: i + 1] for f in
                  ('open', 'high', 'low', 'close', 'volume')}
            if _has_nan(sl['close']):
                continue
            sig = detect_signal(sl, float(tr))
            if sig is None:
                if structure_ok(sl):   # 结构内但未出信号=对照子组
                    ctrl_pool.append((i, j, code, 'struct'))
                else:
                    ctrl_pool.append((i, j, code, 'outside'))
                continue
            typ, score = sig
            atr = _atr20([float(x) for x in sl['high']],
                         [float(x) for x in sl['low']],
                         [float(x) for x in sl['close']])
            entry = float(b['close'][j])
            if atr is None or not (entry > 0) or entry != entry:
                continue
            fw_h = b['high'][j + 1: j + 1 + FWD]
            fw_l = b['low'][j + 1: j + 1 + FWD]
            fw_c = b['close'][j + 1: j + 1 + FWD]
            if len(fw_c) < FWD or np.all(np.isnan(fw_c)):
                continue
            mfe = (np.nanmax(fw_h) / entry - 1) * 100
            mae = (np.nanmin(fw_l) / entry - 1) * 100
            r = (fw_c[-1] / entry - 1) * 100 if fw_c[-1] == fw_c[-1] else np.nan
            stop_pct = K_ATR * atr / entry * 100
            events.append(dict(sig_day=days[i], buy_day=days[j], code=code,
                               type=typ, score=round(score, 1),
                               mfe=round(mfe, 2), mae=round(mae, 2),
                               r20=r, stop_pct=round(stop_pct, 2)))

    ev = pd.DataFrame(events)
    n_ev = len(ev)
    rng = random.Random(7)
    n_ctrl = max(10 * n_ev, 1)
    picks = rng.sample(ctrl_pool, min(n_ctrl, len(ctrl_pool))) \
        if len(ctrl_pool) > n_ctrl else ctrl_pool
    ctrl_rows = []
    for i, j, code, grp in picks:
        b = bars_all[code]
        entry = float(b['close'][j])
        if not (entry > 0) or entry != entry:
            continue
        fw_h = b['high'][j + 1: j + 1 + FWD]
        fw_l = b['low'][j + 1: j + 1 + FWD]
        fw_c = b['close'][j + 1: j + 1 + FWD]
        if len(fw_c) < FWD or np.all(np.isnan(fw_c)):
            continue
        ctrl_rows.append(dict(
            grp=grp,
            mfe=(np.nanmax(fw_h) / entry - 1) * 100,
            mae=(np.nanmin(fw_l) / entry - 1) * 100,
            r20=(fw_c[-1] / entry - 1) * 100 if fw_c[-1] == fw_c[-1] else np.nan))
    ct = pd.DataFrame(ctrl_rows)

    def line(name, s):
        s = s.dropna()
        if s.empty:
            return '%-22s n=0' % name
        q = s.quantile([.1, .25, .5, .75, .9]).to_dict()
        return ('%-22s n=%-5d mean=%6.2f p10=%6.2f p25=%6.2f p50=%6.2f '
                'p75=%6.2f p90=%6.2f' % (name, len(s), s.mean(), q[.1],
                                         q[.25], q[.5], q[.75], q[.9]))

    print('信号窗: %s ~ %s | 事件 %d | 对照 %d（结构内 %d / 结构外 %d）'
          % (days[I0], days[min(I1 + 1, n - 1)], n_ev, len(ct),
             int((ct.grp == 'struct').sum()) if not ct.empty else 0,
             int((ct.grp == 'outside').sum()) if not ct.empty else 0))
    if n_ev:
        print('按月分布:', ev.sig_day.str[:7].value_counts().sort_index().to_dict())
        print('类型分布:', ev.type.value_counts().to_dict())
        print('score>=60 事件:', int((ev.score >= MIN_SCORE_BUY).sum()))
    print('── MFE20(%%) ──')
    print(line('信号全部', ev.mfe if n_ev else pd.Series(dtype=float)))
    if n_ev:
        print(line('信号 score>=60', ev.loc[ev.score >= MIN_SCORE_BUY, 'mfe']))
    print(line('对照-结构内', ct.loc[ct.grp == 'struct', 'mfe'] if not ct.empty
               else pd.Series(dtype=float)))
    print(line('对照-结构外', ct.loc[ct.grp == 'outside', 'mfe'] if not ct.empty
               else pd.Series(dtype=float)))
    print('── MAE20(%%) ──')
    print(line('信号全部', ev.mae if n_ev else pd.Series(dtype=float)))
    print(line('对照-结构内', ct.loc[ct.grp == 'struct', 'mae'] if not ct.empty
               else pd.Series(dtype=float)))
    print(line('对照-结构外', ct.loc[ct.grp == 'outside', 'mae'] if not ct.empty
               else pd.Series(dtype=float)))
    print('── R20(%%) ──')
    print(line('信号全部', ev.r20 if n_ev else pd.Series(dtype=float)))
    print(line('对照-结构内', ct.loc[ct.grp == 'struct', 'r20'] if not ct.empty
               else pd.Series(dtype=float)))
    print(line('对照-结构外', ct.loc[ct.grp == 'outside', 'r20'] if not ct.empty
               else pd.Series(dtype=float)))
    if n_ev:
        tail10 = ev[ev.mfe >= 10]
        killed = tail10[tail10.mae <= -tail10.stop_pct]
        print('右尾可达性: MFE20>=10%% 的事件 %d/%d，其中 MAE20 先破 2.5ATR 出场线'
              '（按日级近似，路径未知）的 %d 个（%.0f%%）'
              % (len(tail10), n_ev, len(killed),
                 100.0 * len(killed) / max(len(tail10), 1)))
        p = ev[['mfe', 'mae']].abs()
        print('信号组 |MFE|/|MAE| 中位 = %.2f（>1.88 才有正期望@胜率35%%）'
              % (p['mfe'] / p['mae'].replace(0, np.nan)).median())
    ev_out = os.path.join(HERE, '_cache', 'autopsy_events.csv')
    ev.to_csv(ev_out, index=False, encoding='utf-8-sig')
    print('事件明细:', ev_out)


if __name__ == '__main__':
    main()
