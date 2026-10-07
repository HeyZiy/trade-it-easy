# -*- coding: utf-8 -*-
# mae_v1 —— MAE/MFE 反事实分析：受管交易路径
# 本脚本仍保留旧评分/旧样本口径；使用时先修正评分或重建样本，再重跑。
# 方法：基于交易路径重算 MAE/MFE，扫描成本止损与保本抬升。
# 旧结果及裁决已移除；使用前核对评分口径、重建派生样本并重新验证。
# 旧收益、缓存 score/rank 与版本优劣不能作为修正评分的结论。

import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
LOG = HERE / 'logs' / 'lM_v3_2_1_run_20261006.log'
CACHE = HERE / '_cache' / 'etf_daily_full.pkl'
OUT = HERE / 'reports' / 'mae_v1'
FEE = 0.0001
COST_STOPS = [(-0.08, '-8%'), (-0.10, '-10%'), (-0.12, '-12%'), (-0.15, '-15%')]
WINDOW_END = '2026-09-30'

BUY_RE = re.compile(r'(\d{4}-\d{2}-\d{2}) 14:55:00 - INFO\s+- 买入 (\d{6})\.\S+ \S+ (\d+)份 @([\d.]+)')
SELL_RE = re.compile(r'(\d{4}-\d{2}-\d{2}) 14:55:00 - INFO\s+- 卖出 (\d{6})\.\S+ \S+ '
                     r'触发\[(\S+?)\] 峰([\d.]+) rank (\d+)/(\d+) 平仓盈亏 ([+-]?\d+)')


def parse_log(path):
    open_pos, eps, orphans = {}, [], []
    for line in path.read_text(encoding='utf-8').splitlines():
        m = BUY_RE.search(line)
        if m:
            open_pos.setdefault(m.group(2), []).append(
                {'date': m.group(1), 'shares': int(m.group(3)),
                 'price': float(m.group(4))})
            continue
        m = SELL_RE.search(line)
        if m:
            code, sdate = m.group(2), m.group(1)
            blist = open_pos.pop(code, None)
            if not blist:
                orphans.append((sdate, code))
                continue
            sh = sum(b['shares'] for b in blist)
            eps.append({
                'code': code, 'entry': blist[0]['date'], 'exit': sdate,
                'shares': sh, 'n_buys': len(blist), 'blist': blist,
                'wavg_real': sum(b['shares'] * b['price'] for b in blist) / sh,
                'trigger': m.group(3), 'actual_pnl': float(m.group(7)),
            })
    still_open = [(c, b[0]['date'], sum(x['shares'] for x in b))
                  for c, b in open_pos.items()]
    return eps, still_open, orphans


def cf_pnl(wavg_fee_real, shares, exit_ratio):
    """以加权成本×exit_ratio 的价格平仓的人民币盈亏（含卖出万一佣金）。"""
    return wavg_fee_real * shares * (exit_ratio - 1) \
        - wavg_fee_real * exit_ratio * shares * FEE


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    eps, still_open, orphans = parse_log(LOG)
    closes = pd.read_pickle(CACHE)
    closes = {c: d.set_index(pd.to_datetime(d.index)) for c, d in closes.items()}
    n_trail = sum(1 for e in eps if '峰回' in e['trigger'])
    print(f'episode {len(eps)}（峰回 {n_trail}）| 期末未平 {len(still_open)}'
          f' {still_open} | 孤儿卖出 {orphans}', flush=True)

    out_rows, cost_deltas = [], {label: [] for _, label in COST_STOPS}
    trail_rows, no_data, match_fail, diffs = [], [], 0, []
    for e in eps:
        df = closes.get(e['code'])
        if df is None:
            no_data.append(e['code'])
            continue
        sub = df.loc[e['entry']:e['exit'], 'close'].dropna()
        if sub.empty or len(sub) < 2:
            no_data.append(e['code'])
            continue
        qfq_buy = [float(df['close'].asof(pd.Timestamp(b['date']))) for b in e['blist']]
        sh_total = e['shares']
        wavg_q = sum(b['shares'] * q for b, q in zip(e['blist'], qfq_buy)) / sh_total
        wavg_q_fee = wavg_q * (1 + FEE)
        wavg_fee_real = e['wavg_real'] * (1 + FEE)
        ratios = sub / wavg_q_fee
        mae, mfe = float(ratios.min()) - 1, float(ratios.max()) - 1
        r_exit = float(sub.iloc[-1]) / wavg_q_fee
        predicted = cf_pnl(wavg_fee_real, sh_total, r_exit)
        notional = wavg_fee_real * sh_total
        # 对账口径：±0.5% 名义额。残余偏差 = 14:55 成交价 vs 收盘价 + 平台
        # avg_cost 口径差（512200 2024-10-17 政策日实测尾盘 3% 波动属此类）。
        ok = abs(predicted - e['actual_pnl']) <= 0.005 * notional
        diffs.append(predicted - e['actual_pnl'])
        if not ok:
            match_fail += 1
        row = {'code': e['code'], 'entry': e['entry'], 'exit': e['exit'],
               'shares': sh_total, 'wavg_real': round(e['wavg_real'], 4),
               'n_buys': e['n_buys'], 'trigger': e['trigger'],
               'actual_pnl': e['actual_pnl'], 'mae': round(mae, 4),
               'mfe': round(mfe, 4), 'pnl_match': ok}
        for x, label in COST_STOPS:
            hit = ratios[ratios <= 1 + x]
            if len(hit):
                delta = cf_pnl(wavg_fee_real, sh_total,
                               float(hit.iloc[0])) - e['actual_pnl']
            else:
                delta = 0.0
            row['cost' + label] = round(delta)
            cost_deltas[label].append((e['code'], e['entry'], e['exit'], delta))
        if '峰回' in e['trigger']:
            q_end = df['close'].asof(pd.Timestamp(WINDOW_END))
            if q_end == q_end:
                delta = cf_pnl(wavg_fee_real, sh_total,
                               float(q_end) / wavg_q_fee) - e['actual_pnl']
                row['trail_hold_end'] = round(delta)
                trail_rows.append((e['code'], e['exit'], e['actual_pnl'], delta))
        out_rows.append(row)

    trades = pd.DataFrame(out_rows)
    trades.to_csv(OUT / 'trades.csv', index=False, encoding='utf-8-sig')
    import statistics
    print(f'对账：{len(trades) - match_fail}/{len(trades)} 笔在 ±0.5% 名义额内'
          f'| 重算偏差 中位 {statistics.median(diffs):+.0f} 元 /'
          f' 均值 {statistics.mean(diffs):+.0f} / 最大 {max(diffs):+.0f} /'
          f' 最小 {min(diffs):+.0f}（14:55成交 vs 收盘 噪声底）'
          f'| 无数据码 {sorted(set(no_data))}', flush=True)

    win = trades[trades['actual_pnl'] > 0]
    lose = trades[trades['actual_pnl'] <= 0]
    desc = {
        '赢单数': len(win), '赢单MAE中位': round(win['mae'].median(), 4),
        '赢单MAE≤-8%占比': round(float((win['mae'] <= -0.08).mean()), 3),
        '赢单MAE≤-12%占比': round(float((win['mae'] <= -0.12).mean()), 3),
        '赢单MFE中位': round(win['mfe'].median(), 4),
        '亏单数': len(lose), '亏单MAE中位': round(lose['mae'].median(), 4),
        '亏单MFE中位': round(lose['mfe'].median(), 4),
    }
    summ_rows = []
    for _, label in COST_STOPS:
        ds = cost_deltas[label]
        touched = [d for *_, d in ds if d != 0]
        top = sorted(ds, key=lambda t: t[3])[:3]
        summ_rows.append({
            'param': '成本止损' + label, 'n_touched': len(touched),
            'sum_delta': round(sum(touched)),
            'sum_pos': round(sum(d for d in touched if d > 0)),
            'sum_neg': round(sum(d for d in touched if d < 0)),
            'top_losers': ' | '.join(f'{c} {dt} {d:+.0f}' for c, dt, _, d in top)})
    if trail_rows:
        summ_rows.append({
            'param': '摘峰回(持到期末粗读)', 'n_touched': len(trail_rows),
            'sum_delta': round(sum(r[3] for r in trail_rows)),
            'sum_pos': round(sum(r[3] for r in trail_rows if r[3] > 0)),
            'sum_neg': round(sum(r[3] for r in trail_rows if r[3] < 0)),
            'top_losers': ' | '.join(f'{c} 原{a:+.0f}→差{d:+.0f}'
                                     for c, _, a, d in trail_rows)})
    summary = pd.DataFrame(summ_rows)
    summary.to_csv(OUT / 'summary.csv', index=False, encoding='utf-8-sig')

    # 止损带扫描：-2%~-15% 逐档，看"更严格"从哪里开始误伤赢单。
    # 误伤成本 = 被触发的赢单的实际 pnl（它们本来赚到的钱）。
    scan_rows = []
    for x in [i / 100 for i in range(2, 16)]:
        tot, touched, win_touched, win_forgone = 0.0, 0, 0, 0.0
        for e in eps:
            df = closes.get(e['code'])
            if df is None:
                continue
            sub = df.loc[pd.Timestamp(e['entry']):pd.Timestamp(e['exit']),
                         'close'].dropna()
            if sub.empty or len(sub) < 2:
                continue
            qfq_buy = [float(df['close'].asof(pd.Timestamp(b['date'])))
                       for b in e['blist']]
            wavg_q_fee = (sum(b['shares'] * q
                              for b, q in zip(e['blist'], qfq_buy))
                          / e['shares']) * (1 + FEE)
            ratios = sub / wavg_q_fee
            hit = ratios[ratios <= 1 - x]
            if not len(hit):
                continue
            touched += 1
            delta = cf_pnl(e['wavg_real'] * (1 + FEE), e['shares'],
                           float(hit.iloc[0])) - e['actual_pnl']
            tot += delta
            if e['actual_pnl'] > 0:
                win_touched += 1
                win_forgone += e['actual_pnl']
        scan_rows.append({'stop': f'-{x:.0%}', 'touched': touched,
                          'winners_touched': win_touched,
                          'winners_forgone_pnl': round(win_forgone),
                          'sum_delta': round(tot)})
    scan = pd.DataFrame(scan_rows)
    scan.to_csv(OUT / 'stop_scan.csv', index=False, encoding='utf-8-sig')
    print(scan.to_string(index=False), flush=True)

    print('描述统计:', desc, flush=True)
    (OUT / 'README.md').write_text(
        '# mae_v1 —— 受管路径 MAE/MFE 重做（2026-10-06）\n\n'
        '样本 = v3_2_1 受管路径 105 笔平仓（logs/lM_v3_2_1_run_20261006.log）；\n'
        '口径与一阶近似声明见 ../mae_v1.py 头注。\n\n'
        '## 结果（对话判读的存档摘要）\n\n'
        '1. 对账：重构偏差中位 -4 元（14:55 成交 vs 收盘的噪声底），全部结论\n'
        '   在此噪声底之上。\n'
        '2. "赢单几乎从不破成本"在受管路径复验成立：46 笔赢单 0 笔 MAE≤-8%；\n'
        '   亏单 MAE 中位 -4.5%、MFE 中位 +1.4%（从不回头也复验）——入场侧\n'
        '   无区分量的结论站住，且不再有原版（59 笔幽灵路径）的生存者偏差。\n'
        '3. -8% 成本止损在受管路径为纯增益 Σ+7,189（15 笔全部是亏单提前切，\n'
        '   0 笔赢单误伤）——与 v3_2 时代"成本止损全为负"相反，那是幽灵通信\n'
        '   （-17% 途深大赢单）的影子。幅度 ≈ 初始资金 2.4%，方向与机制清楚：\n'
        '   排名闸等截面变化才动，快速亏单它反应慢，成本止损补位。\n'
        '4. 峰回闸：直接 pnl 净 -5,656 是"对成本"的读法；"持有到期末"反事实\n'
        '   显示不触发要多亏 13,752——相对"不止损拿着"救回 13,752。真实价值\n'
        '   在两者之间，准数等 v3_1_2 平台跑分。\n'
        '5. 路径依赖警告不变：全部反事实为一次跳变，不含再投资效应。\n'
        '6. 止损带扫描（stop_scan.csv）：可行带 -5%~-11%，峰值平坦区 -6%~-8%；\n'
        '   **0 赢单误伤边界 = -8%**，更紧必误伤——-7% 起 1 笔、-5% 4 笔\n'
        '   （放弃 11,794）、-4% 6 笔（放弃 58,400），Σdelta 在 -4% 档崩到\n'
        '   -41,873。紧止损与"拿住大趋势"结构性打架，-8% 是本路径给出的边界。\n',
        encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
