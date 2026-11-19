# 跨资产轮动收益归因与变体对比
# 口径：T 日收盘价出信号并成交；hfq_close；单边万二最低 5 元，零滑点。
# 501018 为新浪源、无分红无拆分故 hfq=原始价。
# 用法（项目根目录，.conda 环境）：
#   .conda/python.exe research/studies/rotation/local_attribution.py            # 17只基线详细报告
#   .conda/python.exe research/studies/rotation/local_attribution.py --compare  # 剔能化/切换缓冲 变体对比
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(r"D:\Documents\PythonProject\trade-it-easy\research\data\etf_daily_ad\etf_daily")
POOL = [  # 17 只研究基线，实验可选子集
    '510300', '159915', '515180', '512100', '588000',
    '513500', '513100', '159920', '513520', '513030',
    '518880', '159980', '159985', '511090', '511010', '501018', '159981',
]
NAMES = {
    '510300': '沪深300', '159915': '创业板', '515180': '中证红利', '512100': '中证1000',
    '588000': '科创50', '513500': '标普500', '513100': '纳指100', '159920': '恒生',
    '513520': '日经225', '513030': '德国DAX', '518880': '黄金', '159980': '有色金属',
    '159985': '豆粕', '511090': '30年国债', '511010': '5年国债', '501018': '南方原油', '159981': '能源化工',
}
M_DAYS, SCORE_LO, SCORE_HI = 25, 0.0, 5.0
COMM, MIN_COMM = 0.0002, 5.0
START, INIT, END = '2016-01-01', 100_000.0, '2026-09-30'

series = {}
for c in POOL:
    df = pd.read_csv(DATA / f"{c}.csv", usecols=['date', 'hfq_close'], parse_dates=['date'])
    series[c] = df.set_index('date')['hfq_close']
panel = pd.DataFrame(series).sort_index().ffill()  # 停牌沿用昨收，同 skip_paused=False
panel = panel[panel.index >= '2015-01-01']  # 留足warmup
panel = panel[panel.index <= END]  # 统一截止，个别源更新的数据不外延
dates = panel.index[panel.index >= START]
offset = panel.index.get_loc(dates[0])
px = panel.values


def score(col, i, m_days=M_DAYS, drop_filter=True):
    """截至第i行(含)最近m_days+1个有效价，复刻 加权动量×R²。
    drop_filter: 近3日任一日跌超5%分数归0（消融开关）。"""
    window = px[i - m_days:i + 1, col]
    if not np.isfinite(window).all():
        return np.nan
    y = np.log(window)
    x = np.arange(len(y))
    w = np.linspace(1, 2, len(y))
    slope, intercept = np.polyfit(x, y, 1, w=w)
    ann = math.exp(slope * 250) - 1.0
    ew = w ** 2
    mean_y = np.average(y, weights=ew)
    resid = y - (slope * x + intercept)
    ss_res = np.sum(ew * resid ** 2)
    ss_tot = np.sum(ew * (y - mean_y) ** 2)
    r2 = np.clip(1 - ss_res / ss_tot, 0, 1) if ss_tot > 0 else 0.0
    s = ann * r2
    if drop_filter:
        r1, r2d, r3 = window[-1] / window[-2], window[-2] / window[-3], window[-3] / window[-4]
        if r1 < 0.95 or r2d < 0.95 or r3 < 0.95:
            return 0.0
    return s


def score_voladj(col, i, vol_days=25, m_days=M_DAYS):
    """风险调整动量：ann×R²/年化波动。量纲与原score不同，不启用score>5过热上限，
    保留下限>0与近3日跌幅过滤。vol_days为波动率窗口（用户提到的20日亦可）。"""
    window = px[i - m_days:i + 1, col]
    vol_window = px[i - vol_days:i + 1, col]
    if not np.isfinite(window).all() or not np.isfinite(vol_window).all():
        return np.nan
    y = np.log(window)
    x = np.arange(len(y))
    w = np.linspace(1, 2, len(y))
    slope, intercept = np.polyfit(x, y, 1, w=w)
    ann = math.exp(slope * 250) - 1.0
    ew = w ** 2
    mean_y = np.average(y, weights=ew)
    resid = y - (slope * x + intercept)
    ss_res = np.sum(ew * resid ** 2)
    ss_tot = np.sum(ew * (y - mean_y) ** 2)
    r2 = np.clip(1 - ss_res / ss_tot, 0, 1) if ss_tot > 0 else 0.0
    rets = np.diff(np.log(vol_window))
    vol = rets.std() * math.sqrt(250)
    if vol <= 0:
        return np.nan
    s = ann * r2 / vol
    r1, r2d, r3 = window[-1] / window[-2], window[-2] / window[-3], window[-3] / window[-4]
    if r1 < 0.95 or r2d < 0.95 or r3 < 0.95:
        return 0.0
    return s


def run_backtest(pool_codes, switch_margin=0.0, mode='raw', vol_days=25, m_days=M_DAYS,
                 ens_windows=(15, 20, 25), drop_filter=True):
    """pool_codes: 参与排名的代码（POOL子集）；switch_margin: 挑战者分数需高出持仓该比例才换。
    mode='raw' 原始ann×R²(0<s<5)；mode='voladj' 风险调整ann×R²/vol(s>0，无过热上限)。
    m_days: 动量回看窗口（默认25，稳定性扫描见 --mscan）。"""
    cols = [POOL.index(c) for c in pool_codes]

    def sc(col, i):
        if mode == 'voladj':
            return score_voladj(col, i, vol_days, m_days)
        if mode == 'ens':
            # 多窗口集成：同量纲分数取平均，降低单一窗口依赖。
            # 近3日跌幅过滤只看末端4价，与窗口无关，各子分数过滤结果一致，不会被稀释。
            return float(np.mean([score(col, i, md, drop_filter) for md in ens_windows]))
        return score(col, i, m_days, drop_filter)

    def eligible(s):
        return s > 0 if mode == 'voladj' else (SCORE_LO < s < SCORE_HI)

    cash, pos_col, pos_val = INIT, None, 0.0
    nav_records = []
    hold_days, pnl_by = {c: 0 for c in pool_codes}, {c: 0.0 for c in pool_codes}
    pnl_by_year = {}
    total_fees, switch_events, empty_days, blocked_switches = 0.0, 0, 0, 0
    transitions = []
    prev_row = None

    for i in range(offset, len(panel)):
        t = panel.index[i]
        if prev_row is not None and pos_col is not None:
            r = px[i, pos_col] / px[prev_row, pos_col] - 1.0
            pnl = pos_val * r
            pos_val += pnl
            code = POOL[pos_col]
            pnl_by[code] += pnl
            yr = pnl_by_year.setdefault(t.year, {c: 0.0 for c in pool_codes})
            yr[code] += pnl
        best_col, best_score = None, -np.inf
        for col in cols:
            s = sc(col, i)
            if np.isfinite(s) and eligible(s) and s > best_score:
                best_col, best_score = col, s
        # 切换缓冲：持仓仍健康（分数>0且未被过滤）且挑战者优势不足 → 不换
        if (switch_margin > 0 and best_col is not None and pos_col is not None
                and best_col != pos_col):
            hold_s = sc(pos_col, i)
            if np.isfinite(hold_s) and hold_s > 0 and best_score < hold_s * (1 + switch_margin):
                best_col = pos_col
                blocked_switches += 1
        if best_col != pos_col:
            if pos_col is not None:
                old_s = sc(pos_col, i)  # 出场时自身分数：>0=被顶位，<=0/nan=自身信号消失
                transitions.append((t, POOL[pos_col],
                                    POOL[best_col] if best_col is not None else None, old_s))
                fee = max(pos_val * COMM, MIN_COMM)
                cash, total_fees = pos_val - fee, total_fees + fee
                pos_col, pos_val = None, 0.0
                switch_events += 1
            if best_col is not None:
                buy_val = cash / (1 + COMM)
                fee = max(buy_val * COMM, MIN_COMM)
                pos_val, cash = buy_val, cash - buy_val - fee
                total_fees += fee
                pos_col = best_col
                switch_events += 1
        nav = cash + pos_val
        nav_records.append((t, nav, POOL[pos_col] if pos_col is not None else None))
        if pos_col is not None:
            hold_days[POOL[pos_col]] += 1
        else:
            empty_days += 1
        prev_row = i

    nav_df = pd.DataFrame(nav_records, columns=['date', 'nav', 'hold']).set_index('date')
    seg_id = nav_df['hold'].ne(nav_df['hold'].shift()).cumsum()
    seg_pnl = []
    for _, seg in nav_df.groupby(seg_id):
        code = seg['hold'].iloc[0]
        if code is None:
            continue
        days = len(seg)
        pnl = seg['nav'].iloc[-1] / seg['nav'].iloc[0] - 1 if days > 1 else 0.0
        seg_pnl.append((code, days, pnl))
    return {
        'pool': pool_codes, 'margin': switch_margin, 'nav': nav_df,
        'hold_days': hold_days, 'pnl_by': pnl_by, 'pnl_by_year': pnl_by_year,
        'fees': total_fees, 'events': switch_events, 'empty_days': empty_days,
        'segments': seg_pnl, 'blocked': blocked_switches, 'transitions': transitions,
        'mode': mode, 'vol_days': vol_days,
    }


def run_backtest_topk(pool_codes, k=2, mode='raw', m_days=M_DAYS, ens_windows=(15, 20, 25),
                      drop_filter=True):
    """持有评分前k名、等权配置。目标成员集合变化时才调仓并重新配平等权；
    成员不变则任仓位随涨跌漂移。候选不足k只有几只配几只，无候选空仓。
    k=1 应与 run_backtest 单持第一名结果一致（用于校验）。"""
    cols = [POOL.index(c) for c in pool_codes]

    def sc(col, i):
        if mode == 'ens':
            return float(np.mean([score(col, i, md, drop_filter) for md in ens_windows]))
        return score(col, i, m_days, drop_filter)

    cash = INIT
    holdings = {}  # col -> 市值
    nav_records, pnl_by, pnl_by_year = [], {c: 0.0 for c in pool_codes}, {}
    total_fees, trade_count, empty_days = 0.0, 0, 0
    prev_row = None

    for i in range(offset, len(panel)):
        t = panel.index[i]
        if prev_row is not None:
            for col in list(holdings):
                r = px[i, col] / px[prev_row, col] - 1.0
                pnl = holdings[col] * r
                holdings[col] += pnl
                code = POOL[col]
                pnl_by[code] += pnl
                yr = pnl_by_year.setdefault(t.year, {c: 0.0 for c in pool_codes})
                yr[code] += pnl
        ranked = []
        for col in cols:
            s = sc(col, i)
            if np.isfinite(s) and SCORE_LO < s < SCORE_HI:
                ranked.append((s, col))
        ranked.sort(reverse=True)
        target = [col for _, col in ranked[:k]]

        if set(target) != set(holdings):
            for col in list(holdings):
                if col not in target:
                    fee = max(holdings[col] * COMM, MIN_COMM)
                    cash += holdings[col] - fee
                    total_fees += fee
                    trade_count += 1
                    del holdings[col]
            if target:
                equity = cash + sum(holdings.values())
                target_val = equity / len(target)
                for col in target:
                    diff = target_val - holdings.get(col, 0.0)
                    if abs(diff) > 1e-6:
                        fee = max(abs(diff) * COMM, MIN_COMM)
                        total_fees += fee
                        trade_count += 1
                        if diff > 0:
                            cash -= diff + fee
                            holdings[col] = holdings.get(col, 0.0) + diff
                        else:
                            cash += -diff - fee
                            holdings[col] += diff
        nav = cash + sum(holdings.values())
        nav_records.append((t, nav, '+'.join(POOL[c] for c in holdings) if holdings else None))
        if holdings:
            pass
        else:
            empty_days += 1
        prev_row = i

    nav_df = pd.DataFrame(nav_records, columns=['date', 'nav', 'hold']).set_index('date')
    return {'nav': nav_df, 'pnl_by': pnl_by, 'pnl_by_year': pnl_by_year,
            'fees': total_fees, 'events': trade_count, 'empty_days': empty_days, 'k': k}


def metrics(res):
    nav = res['nav']['nav']
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = (nav.iloc[-1] / INIT) ** (1 / years) - 1
    mdd = (nav / nav.cummax() - 1).min()
    yearly = nav.resample('YE').last()
    return {'final': nav.iloc[-1], 'cagr': cagr, 'mdd': mdd, 'yearly': yearly}


def report(res):
    nav = res['nav']['nav']
    m = metrics(res)
    pnl_by, hold_days, pnl_by_year = res['pnl_by'], res['hold_days'], res['pnl_by_year']
    seg_pnl = res['segments']
    total_pnl = nav.iloc[-1] - INIT
    print(f"区间 {nav.index[0].date()} ~ {nav.index[-1].date()}  初始 {INIT:,.0f} 元  "
          f"池 {len(res['pool'])} 只  缓冲 {res['margin']:.0%}")
    print(f"期末净值 {m['final']:,.0f} 元 | 累计 {m['final'] / INIT - 1:+.1%} | "
          f"年化 {m['cagr']:+.2%} | 最大回撤 {m['mdd']:.1%}")
    print(f"交易事件 {res['events']} 次 | 总佣金 {res['fees']:,.0f} 元 | "
          f"空仓 {res['empty_days']} 天 / 共 {len(nav)} 天 | 缓冲拦截 {res['blocked']} 次\n")

    print("== 逐年收益 ==")
    prev = INIT
    for t, v in m['yearly'].items():
        print(f"  {t.year}: {v / prev - 1:+.1%}   (年末净值 {v:,.0f})")
        prev = v

    print(f"\n== 收益来源（持有期间盈亏，费用另计；账户总盈亏 {total_pnl:+,.0f} 元）==")
    print(f"{'标的':<6}{'名称':<10}{'持仓天数':>8}{'天数占比':>9}{'盈亏贡献(元)':>14}{'占总盈亏':>9}")
    for c in sorted(res['pool'], key=lambda c: pnl_by[c], reverse=True):
        days = hold_days[c]
        if days == 0 and abs(pnl_by[c]) < 1:
            continue
        share = pnl_by[c] / total_pnl if total_pnl else float('nan')
        print(f"{c:<6}{NAMES[c]:<10}{days:>8}{days / len(nav):>9.1%}{pnl_by[c]:>14,.0f}{share:>9.1%}")
    print(f"{'':<6}{'佣金':<10}{'':>8}{'':>9}{-res['fees']:>14,.0f}{-res['fees'] / total_pnl:>9.1%}")

    wins = [s for s in seg_pnl if s[2] > 0]
    print(f"\n== 持有段（共 {len(seg_pnl)} 段）==")
    print(f"  盈利段 {len(wins)} | 胜率 {len(wins) / len(seg_pnl):.1%} | "
          f"盈利段平均 {np.mean([s[2] for s in wins]):+.2%} | "
          f"亏损段平均 {np.mean([s[2] for s in seg_pnl if s[2] <= 0]):+.2%}")
    top5 = sorted(seg_pnl, key=lambda s: s[2], reverse=True)[:5]
    print("  盈利最大的5段:", [(NAMES[s[0]], s[1], f"{s[2]:+.1%}") for s in top5])

    print("\n== 能源两只 逐年盈亏 ==")
    for c in ('501018', '159981'):
        if c not in res['pool']:
            continue
        print(f"  {NAMES[c]}({c}):")
        for y in sorted(pnl_by_year):
            v = pnl_by_year[y][c]
            if abs(v) > 1:
                print(f"    {y}: {v:+,.0f} 元")

    for y in (2025, 2026):
        if y not in pnl_by_year:
            continue
        print(f"\n== {y}年各标的盈亏 ==")
        for c, v in sorted(pnl_by_year[y].items(), key=lambda kv: kv[1], reverse=True):
            if abs(v) > 1:
                print(f"  {NAMES[c]:<8} {v:>+12,.0f} 元")


def compare():
    no_nengyuan = [c for c in POOL if c != '159981']
    variants = [
        ('A 17只基线', POOL, 'raw', 25),
        ('B 剔能化', no_nengyuan, 'raw', 25),
        ('F 17只+波动调整25', POOL, 'voladj', 25),
        ('G 剔能化+波动调整25', no_nengyuan, 'voladj', 25),
        ('H 剔能化+波动调整20', no_nengyuan, 'voladj', 20),
    ]
    results = []
    for label, pool, mode, vd in variants:
        res = run_backtest(pool, mode=mode, vol_days=vd)
        m = metrics(res)
        results.append((label, res, m))
        print(f"[done] {label}", flush=True)

    print(f"\n{'变体':<22}{'期末净值':>14}{'年化':>9}{'最大回撤':>9}{'交易事件':>8}{'佣金(万)':>9}{'段数':>6}")
    for label, res, m in results:
        print(f"{label:<22}{m['final']:>14,.0f}{m['cagr']:>9.2%}{m['mdd']:>9.1%}"
              f"{res['events']:>8}{res['fees'] / 1e4:>9.1f}{len(res['segments']):>6}")

    print(f"\n{'变体':<22}{'2024':>8}{'2025':>8}{'2026':>8}{'能化盈亏(万)':>12}{'原油盈亏(万)':>12}{'创业板(万)':>11}{'纳指(万)':>10}{'黄金(万)':>10}")
    for label, res, m in results:
        yearly = m['yearly']
        rets = {}
        prev = INIT
        for t, v in yearly.items():
            rets[t.year] = v / prev - 1
            prev = v
        pnl = res['pnl_by']
        print(f"{label:<22}{rets.get(2024, np.nan):>8.1%}{rets.get(2025, np.nan):>8.1%}{rets.get(2026, np.nan):>8.1%}"
              f"{pnl.get('159981', 0) / 1e4:>12.1f}{pnl.get('501018', 0) / 1e4:>12.1f}"
              f"{pnl.get('159915', 0) / 1e4:>11.1f}{pnl.get('513100', 0) / 1e4:>10.1f}{pnl.get('518880', 0) / 1e4:>10.1f}")

    print(f"\n{'变体':<22}{'黄金天数':>9}{'原油天数':>9}{'能化天数':>9}{'国债天数':>9}{'创业板天数':>10}{'红利天数':>9}")
    for label, res, m in results:
        hd = res['hold_days']
        bond = hd.get('511090', 0) + hd.get('511010', 0)
        print(f"{label:<22}{hd.get('518880', 0):>9}{hd.get('501018', 0):>9}{hd.get('159981', 0):>9}"
              f"{bond:>9}{hd.get('159915', 0):>10}{hd.get('515180', 0):>9}")


def mscan():
    """动量窗口稳定性扫描：绩效、逐年、收益来源结构是否随 m_days 平滑变化。"""
    pool = [c for c in POOL if c != '159981']
    days_list = [10, 15, 20, 25, 30, 40, 60]
    results = []
    for md in days_list:
        res = run_backtest(pool, m_days=md)
        m = metrics(res)
        results.append((md, res, m))
        print(f"[done] m={md}", flush=True)

    print(f"\n{'m_days':>6}{'年化':>9}{'最大回撤':>9}{'期末净值':>13}{'交易事件':>8}{'佣金(万)':>9}{'段数':>6}{'空仓':>5}")
    for md, res, m in results:
        print(f"{md:>6}{m['cagr']:>9.2%}{m['mdd']:>9.1%}{m['final']:>13,.0f}"
              f"{res['events']:>8}{res['fees'] / 1e4:>9.1f}{len(res['segments']):>6}{res['empty_days']:>5}")

    years = [2018, 2020, 2022, 2024, 2025, 2026]
    print(f"\n{'m_days':>6}" + ''.join(f"{y:>8}" for y in years))
    for md, res, m in results:
        rets, prev = {}, INIT
        for t, v in m['yearly'].items():
            rets[t.year] = v / prev - 1
            prev = v
        print(f"{md:>6}" + ''.join(f"{rets.get(y, np.nan):>8.1%}" for y in years))

    legs = ['159915', '513100', '501018', '518880']
    print(f"\n{'m_days':>6}" + ''.join(f"{NAMES[c] + '盈亏(万)':>14}" for c in legs)
          + f"{'四腿合计占比':>12}")
    for md, res, m in results:
        total = m['final'] - INIT
        s = sum(res['pnl_by'][c] for c in legs)
        print(f"{md:>6}" + ''.join(f"{res['pnl_by'][c] / 1e4:>14.1f}" for c in legs)
              + f"{s / total:>12.1%}")


if __name__ == '__main__':
    if '--compare' in sys.argv:
        compare()
    elif '--mscan' in sys.argv:
        mscan()
    else:
        report(run_backtest(POOL))
