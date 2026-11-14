# -*- coding: utf-8 -*-
"""ETF 中期动量 + 相关性限制（medium_correlation）聚宽回测版。

研究来源：research/studies/etf/study_local_trend.py 的 Rules("medium_correlation")，
对照口径为本地 results/local_trend_verified/medium_correlation_next_open_*.csv
（2016-01-04 ~ 2026-09-30，年化 +4.45%、最大回撤 -49.46%，成交偏移 ±10bp）。
round_4 的 next_open_20bp（+3.28%）是稳健性对照，不追求复现。

相对基线 etf_v1_baseline（v1.py）的两处规则改动：
- 评分换成中期动量：(60 日复权涨幅 + 120 日复权涨幅) / 2，替代 25 日回归趋势分。
- 买入加相关性限制：候选与现有持仓及本批已选标的的近 60 个 log 日收益相关
  系数须 < 0.80（至少 45 个重叠观测；无法估计时保留现金）。

与 v1.py 基线的两处刻意偏离（均为对齐本地研究，非遗漏）：
- 不剔除货币/债券 ETF：本地 v1_local.rebuild_pool 无名称过滤，ledger 首日即
  买入 511010/511880，现金类已实现盈亏把全期亏损拉回约 14%，是 2021-2023
  段的重要垫底收益；剔除会使对照失真。
- 有效性检查粒度：中期动量分只依赖 3 个端点日（T、T-60、T-120）有效，
  窗口中间的停牌/缺失日不影响本地得分（pct_change 只取端点）——故端点用
  close+paused 校验，相关性窗口逐日收益掩码；仅成熟判断（249 根）和流动性
  （20 根）要求整窗完整，与本地 rolling/notna 一致。

与本地口径对齐（history 不含当日，调用时点为 T+1 开盘）：
- 评分取 121 根收盘 = T-120..T，端点 T-120/T-60/T，等于本地含 T 收盘的
  pct_change(60/120)；持仓成员同样按 121 根评分，缺端点即 complete=False，
  不制造虚假掉队。
- 成熟判断取 250 根丢最后 1 根 = 截至 T-1 的 249 根全部有效，等于本地
  rolling(249).shift(1)；流动性取 21 根 money 丢最后 1 根 = 截至 T-1 的
  20 根完整成交额，等于本地 amount.iloc[loc-20:loc]。
- 成交价：T+1 开盘价撮合，PriceRelatedSlippage(0.002) 提供买卖各 10bp 偏移；
  佣金万一单边、无最低佣金；任一卖出未完成（仍持仓）则当日不买，对齐本地
  fill() 的对倒防护。
- 复权：use_real_price=True 下 history 返回基于当天的前复权价，同一窗口内
  自洽，对应本地"复权价打分、原始价成交"。
- 数据获取：全池分批 history()（每批 BATCH 只），非逐只 attribute_history，
  否则日频全池评分在聚宽会超时；停牌日用 paused 掩码，不吃填充价。
- 年化对比：本地 ANN=244，聚宽默认 250，年化数字有口径差；对齐验证以
  累计收益、最大回撤、成交笔数（本地 378 笔）为准。
"""

import builtins as _b

import numpy as np
import pandas as pd

from jqdata import *

STRATEGY_VERSION = 'etf_medium_correlation_v1'
TOPN = 3
EXIT_RANK_MULT = 2                   # 旧仓留存带：前 TOPN×2
SCORE_EXIT_CONFIRM = 3               # 连续有效评估不合格次数；分数/排名共用计数
MIN_BARS = 250
REBUILD_MONEY_BARS = 21              # 21 根丢最后 1 根 = 截至 T-1 的 20 根
LIQ_AMT20_MIN = 30_000_000.0
REBUILD_EVERY = 20
MEDIUM_SHORT = 60                    # 中期动量短窗
MEDIUM_LONG = 120                    # 中期动量长窗
CORR_DAYS = 60                       # 相关系数窗口（60 个日收益 = 61 根收盘）
CORR_MIN_OVERLAP = 45                # 最小重叠观测
CORR_MAX = 0.80                      # 相关系数上限
COMMISSION_RATE = 0.0001
BATCH = 400                          # history 单批标的数，防接口压力


def initialize(context):
    set_option("use_real_price", True)
    set_benchmark("000300.XSHG")
    set_slippage(PriceRelatedSlippage(0.002))    # 买卖各 10bp，对应本地 ±10bp
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=COMMISSION_RATE,
                             close_commission=COMMISSION_RATE, min_commission=0),
                   type="fund")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.pool = []
    g.pool_valid = False              # 区分正常空池和重建失败，失败次日重试
    g.day = 0
    g.mature = set()                  # 重建日确认成熟的候选（截至 T-1 的 249 根）
    g.frame = None                    # 评分日批量收盘矩阵，相关性检查复用
    g.frame_paused = None             # 与 frame 对齐的停牌掩码
    g.trades = []                     # 卖出台账：{date, code, name, pnl}
    g.neg_run = {}                    # 掉队或 score<=0 连续计数，卖出时清除
    run_daily(run_rotation, time='open')
    log.info('策略 %s 参数 %s' % (STRATEGY_VERSION, {
        'topn': TOPN, 'exit_rank': TOPN * EXIT_RANK_MULT,
        'confirm': SCORE_EXIT_CONFIRM,
        'min_bars': MIN_BARS, 'liquidity': LIQ_AMT20_MIN,
        'rebuild_every': REBUILD_EVERY,
        'medium': [MEDIUM_SHORT, MEDIUM_LONG],
        'corr': [CORR_DAYS, CORR_MIN_OVERLAP, CORR_MAX],
        'cash_fund_filter': '无（对齐本地研究，与 v1.py 基线不同）',
        'commission': COMMISSION_RATE, 'slippage': '10bp', 'time': 'open'}))


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def _batch_history(codes, count, field):
    """全池批量取 history（不含当日 → 截至信号日收盘），分批防接口压力。"""
    frames = []
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        try:
            df = history(count, '1d', field, security_list=chunk)
        except Exception:
            df = None
        if df is not None and len(df):
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1) if len(frames) > 1 else frames[0]


def valid_daily(close, paused):
    """每根有效 = 收盘有限为正且未停牌；缺失或停牌都算无效。

    聚宽停牌日用停牌前价填充，不吃填充价必须看 paused；本地 quality!=ok
    等价于此处的停牌或非有限价。
    """
    return close.notna() & close.gt(0) & paused.fillna(1.0).eq(0)


def medium_scores(close):
    """(60日涨幅 + 120日涨幅) / 2；close 为截至信号日的 121 根复权价矩阵。

    与本地 study_local_trend.py 的 medium_scores 同构：pct_change 只取端点，
    窗口中间的无效日不影响得分（有效性由调用方按端点校验）。
    """
    if len(close) < MEDIUM_LONG + 1:
        return pd.Series(dtype=float)
    last = close.iloc[-1]
    return (last / close.iloc[-MEDIUM_SHORT - 1] - 1 + last / close.iloc[0] - 1) / 2


def rebuild_pool(dt, held):
    """全 ETF 历史名单 → 20 日流动性 → 成熟过滤；持仓直入。

    与本地 v1_local.rebuild_pool 一致：不剔除货币/债券 ETF。
    """
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    if not len(secs):
        raise ValueError('ETF历史名单为空，无法确认候选范围')
    cands = list(secs.index)
    money = _batch_history(cands, REBUILD_MONEY_BARS, 'money')
    if money.empty or len(money) < REBUILD_MONEY_BARS:
        raise ValueError('建池成交额数据不完整')
    window = money.iloc[:REBUILD_MONEY_BARS - 1]     # 丢信号日 = 截至 T-1 的 20 根
    avg = window.mean()
    ok = window.notna().all(axis=0) & window.ge(0).all(axis=0)
    liquid = {c for c in window.columns if ok.get(c, False)
              and np.isfinite(avg.get(c, np.nan)) and avg[c] >= LIQ_AMT20_MIN}
    if not liquid:
        raise ValueError('流动性过滤后候选为空')
    fresh = sorted(liquid - held)
    close = _batch_history(fresh, MIN_BARS, 'close')
    paused = _batch_history(fresh, MIN_BARS, 'paused')
    g.mature = set(held)
    if not close.empty and len(close) == MIN_BARS:
        mature_ok = valid_daily(close, paused).iloc[:MIN_BARS - 1].all(axis=0)
        g.mature |= {c for c in close.columns if mature_ok.get(c, False)}
    pool = set(held)
    for code in fresh:
        if code in g.mature:
            pool.add(code)
    log.info('池重建 全表 %d | 流动 %d | 池 %d | 持仓直入 %s'
             % (len(cands), len(liquid), len(pool), sorted(held)))
    return sorted(pool)


def score_pool(held):
    """批量评分全池；返回 (rows, complete)。

    候选须成熟且 3 个端点日（T、T-60、T-120）有效；持仓成员只需端点有效
    （本地 rank() 对持仓不查 mature，分数有限即可），端点缺失即
    complete=False 且不进排名，不制造虚假掉队。
    """
    close = _batch_history(list(g.pool), MEDIUM_LONG + 1, 'close')
    paused = _batch_history(list(g.pool), MEDIUM_LONG + 1, 'paused')
    g.frame, g.frame_paused = close, paused
    rows = []
    complete = True
    if close.empty or len(close) < MEDIUM_LONG + 1:
        return rows, False
    ok = valid_daily(close, paused)
    score_ok = ok.iloc[-1] & ok.iloc[-MEDIUM_SHORT - 1] & ok.iloc[0]
    scores = medium_scores(close)
    for code in g.pool:
        if code not in close.columns:
            complete = False
            continue
        score = scores.get(code)
        if (not score_ok.get(code, False) or score is None
                or not np.isfinite(score)
                or (code not in held and code not in g.mature)):
            complete = False
            continue
        rows.append({'code': code, 'name': get_security_name(code), 'score': float(score)})
    rows.sort(key=lambda r: (-r['score'], r['code']))
    return rows, complete


def correlated(code, reserved):
    """候选 code 与 reserved 任一品种近 60 个 log 日收益相关 >= 0.80 时为 True。

    数据来自 score_pool 缓存的 g.frame（121 根收盘 → 60 个日收益）；日收益
    仅在其两端都有效时计入（停牌填充价不制造虚假零收益）；重叠观测不足或
    相关性无法估计时保守返回 True（保留现金），与本地一致。
    """
    frame, paused = g.frame, g.frame_paused
    if frame is None or code not in frame.columns or len(frame) < CORR_DAYS + 1:
        return True
    close_w = frame.tail(CORR_DAYS + 1)
    ok = valid_daily(close_w, paused.tail(CORR_DAYS + 1))
    rets = np.log(close_w.astype(float)).diff().iloc[1:]
    if code not in rets.columns:
        return True
    ret_ok = ok.iloc[1:].values & ok.iloc[:-1].values   # 每列独立的日收益掩码（2D 按列取用）
    cols = list(rets.columns)
    a = rets[code].values
    a_ok = ret_ok[:, cols.index(code)]
    for other in sorted(reserved):
        if other not in rets.columns:
            return True
        b = rets[other].values
        b_ok = ret_ok[:, cols.index(other)]
        valid = np.isfinite(a) & np.isfinite(b) & a_ok & b_ok
        if valid.sum() < CORR_MIN_OVERLAP:
            return True
        if np.std(a[valid]) == 0 or np.std(b[valid]) == 0:
            return True
        if np.corrcoef(a[valid], b[valid])[0, 1] >= CORR_MAX:
            return True
    return False


def evaluate_exits(context, rows, rank_valid):
    """排名未知时仅暂停排名判断，自身分数出口独立执行。"""
    rank_of = {r['code']: i + 1 for i, r in enumerate(rows)}
    row_of = {r['code']: r for r in rows}
    sold_today = set()
    open_codes = {o.security for o in get_open_orders().values()}
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0 or sec in open_codes:
            continue
        score_now = row_of.get(sec, {}).get('score')
        rank = rank_of.get(sec)
        out_of_rank = rank_valid and rank is not None and rank > TOPN * EXIT_RANK_MULT
        nonpositive = score_now is not None and score_now <= 0
        previous_neg = g.neg_run.get(sec, 0)
        neg = previous_neg
        if nonpositive or out_of_rank:
            neg += 1
        elif rank_valid and rank is not None and score_now is not None:
            neg = 0
        # 两个条件都未知时冻结；已经由有效数据确认的不合格才累计。
        g.neg_run[sec] = neg
        should_sell = (nonpositive or out_of_rank) and neg >= SCORE_EXIT_CONFIRM
        if neg == previous_neg and not should_sell:
            continue
        detail = 'score=%s rank=%s 排名有效=%s 本次非正分=%s 本次排名掉队=%s' % (
            '未知' if score_now is None else '%+.6g' % score_now,
            rank, rank_valid, nonpositive, out_of_rank)
        log.info('持仓评估 %s 计数%d->%d %s %s' % (
            sec, previous_neg, neg,
            '触发卖出' if should_sell else '继续持有', detail))
        if not should_sell:
            continue
        _submit_sell(context, sec, '连续%d次评估不合格' % neg, detail)
        sold_today.add(sec)
    return sold_today


def held_codes(context):
    return {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}


def execute_buys(context, rows, sold_today):
    """按排名补空槽；持仓与有效买单共同占用名额。相关性挡下时留现金。

    对齐本地 fill()：任一卖出未完成（仍持仓）则当日不买，避免同日对倒；
    被相关性挡下的候选继续尝试 rows[:TOPN] 内的下一个，名额不被排名
    之外的候选回填。
    """
    stuck = sorted(code for code in sold_today if code in held_codes(context))
    if stuck:
        log.info('存在未完成的卖出，当日暂停买入 %s' % stuck)
        return
    open_orders = get_open_orders()
    open_codes = {o.security for o in open_orders.values()}
    reserved = held_codes(context) | {o.security for o in open_orders.values() if o.is_buy}
    slots = TOPN - len(reserved)
    if slots <= 0:
        return
    cash_left = float(context.portfolio.available_cash)
    per = _b.min(cash_left / slots, context.portfolio.total_value / TOPN)
    for rank, row in enumerate(rows[:TOPN], 1):
        if len(reserved) >= TOPN:
            break
        code = row['code']
        if code in reserved or code in open_codes or code in sold_today or row['score'] <= 0:
            continue
        if correlated(code, reserved):
            log.info('相关性限制跳过 %s rank=%d score=%+.6g' % (
                code, rank, row['score']))
            continue
        info = get_current_data()[code]
        px = float(info.last_price)
        if info.paused or not np.isfinite(px) or px <= 0:
            log.info('买入跳过 %s 停牌或最新价无效' % code)
            continue
        budget = _b.min(per, cash_left, context.portfolio.available_cash)
        amount = int(budget / (px * (1 + COMMISSION_RATE)) // 100) * 100
        if amount < 100:
            continue
        result = order(code, amount)
        if result is None:
            log.info('买入委托失败 %s rank=%d' % (code, rank))
            continue
        log.info('提交买入委托 %s %d份 参考价%.3f score=%+.6g rank=%d' % (
            code, amount, px, row['score'], rank))
        # 平台挂单表判断有效委托，成交量判断实际持仓，均不以尝试次数替代。
        live = get_open_orders()
        pending = {o.security for o in live.values() if o.is_buy}
        if int(result.filled) > 0 or code in pending:
            reserved.add(code)
            committed = amount * px * (1 + COMMISSION_RATE) if code in pending else (
                int(result.filled) * float(result.price) * (1 + COMMISSION_RATE))
            cash_left = _b.max(0., cash_left - committed)
        else:
            log.info('买入委托结束且零成交 %s，未占用名额' % code)
        reserved |= held_codes(context) | pending


def run_rotation(context):
    """编排建池、排名、持仓退出与新仓执行；正常空池仍管理持仓。"""
    g.day += 1
    _sync_sell_orders(context)
    held = held_codes(context)
    if not g.pool_valid or (g.day - 1) % REBUILD_EVERY == 0:
        try:
            g.pool = rebuild_pool(context.current_dt, held)
            g.pool_valid = True
        except Exception as exc:
            g.pool_valid = False
            log.info('池重建失败，暂停新买与排名判断，下次重试: %s' % exc)
    if g.pool_valid:
        rows, complete = score_pool(held)
    else:
        rows, complete = [], False
    rank_valid = g.pool_valid and complete
    sold_today = evaluate_exits(context, rows, rank_valid)
    if rank_valid:
        execute_buys(context, rows, sold_today)


# BEGIN SHARED PLATFORM LEDGER（与 v1.py 相同，仅迁就此处无 cd 参数）


def _ensure_sell_ledger():
    for name, initial in (('sell_orders', {}), ('sell_partial', {}), ('sell_fills', [])):
        if not hasattr(g, name):
            setattr(g, name, initial)


def _record_sell_order(context, order, meta):
    """Reconcile cumulative fills; never use requested shares as executed shares."""
    filled = int(order.filled)
    if filled <= 0:
        return 0
    notional = float(order.price) * filled
    fee = getattr(order, 'commission', None)
    fee = float(fee) if fee is not None else notional * COMMISSION_RATE
    delta = filled - meta['filled']
    pnl = notional - meta['notional'] - meta['cost'] * delta - (fee - meta['fee'])
    if delta == 0 and abs(pnl) < 1e-9:
        return 0
    meta.update(filled=filled, notional=notional, fee=fee)
    code = meta['code']
    today = context.current_dt.strftime('%Y-%m-%d')
    g.sell_fills.append({'date': today, 'code': code, 'name': meta['name'],
                         'amount': delta, 'pnl': pnl})
    if meta['closed_index'] is not None:
        g.trades[meta['closed_index']]['pnl'] += pnl
        return delta
    g.sell_partial[code] = g.sell_partial.get(code, 0.0) + pnl
    pos = context.portfolio.positions.get(code)
    remaining = int(pos.total_amount) if pos is not None else 0
    log.info('卖出成交 %s %s 触发[%s] %s | 本次%d份 累计%d份 剩余%d份 '
             '成交均价%.4f 本次成交盈亏 %+.2f'
             % (code, meta['name'], meta['reason'], meta['detail'], delta, filled,
                remaining, float(order.price), pnl))
    if remaining <= 0:
        total = g.sell_partial.pop(code)
        closed_index = len(g.trades)
        for pending in g.sell_orders.values():
            if pending['code'] == code and pending['closed_index'] is None:
                pending['closed_index'] = closed_index
        g.trades.append({'date': today, 'code': code, 'name': meta['name'], 'pnl': total})
        g.neg_run.pop(code, None)
        log.info('完成平仓 %s %s 合并成交盈亏 %+.2f'
                 % (code, meta['name'], total))
    return delta


def _submit_sell(context, code, reason, detail=''):
    """Snapshot cost before order_target mutates the platform position object."""
    _ensure_sell_ledger()
    cost = float(context.portfolio.positions[code].avg_cost)
    order = order_target(code, 0)
    if order is None:
        log.info('卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态' % (code, reason))
        return 0
    meta = {'code': code, 'name': get_security_name(code), 'cost': cost,
            'reason': reason, 'detail': detail, 'filled': 0, 'notional': 0.0,
            'fee': 0.0, 'closed_index': None}
    g.sell_orders[str(order.order_id)] = meta
    delta = _record_sell_order(context, order, meta)
    if delta == 0:
        log.info('卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态' % (code, reason))
    return delta


def _sync_sell_orders(context):
    _ensure_sell_ledger()
    for order in get_orders().values():
        meta = g.sell_orders.get(str(order.order_id))
        if meta is not None:
            _record_sell_order(context, order, meta)


def after_trading_end(context):
    """Capture subsequent fills and fees without counting the same fill twice."""
    _sync_sell_orders(context)


def on_strategy_end(context):
    """Report confirmed closed trades, partial realized P&L and reconciliation."""
    import builtins as python_builtins

    _sync_sell_orders(context)
    trades = g.trades
    closed = float(python_builtins.sum(t['pnl'] for t in trades))
    partial = float(python_builtins.sum(g.sell_partial.values()))
    realized = closed + partial
    wins = len([t for t in trades if t['pnl'] > 0])
    losses = len([t for t in trades if t['pnl'] < 0])
    log.info('【台账】完成平仓 %d 笔（盈 %d 亏 %d 平 %d）| 合并盈亏 %+.2f'
             % (len(trades), wins, losses, len(trades)-wins-losses, closed))
    log.info('【台账】未清仓已实现 %+.2f | 全部成交已实现 %+.2f' % (partial, realized))
    ranked = sorted(trades, key=lambda t: -t['pnl'])
    for i, t in enumerate(ranked[:5]):
        log.info('【台账】TOP%d %s %s %s pnl=%+.2f'
                 % (i+1, t['date'], t['code'], t['name'], t['pnl']))
    top3 = float(python_builtins.sum(t['pnl'] for t in ranked[:3]))
    ratio = top3 / closed * 100 if closed > 0 else float('nan')
    log.info('【台账】TOP3=%+.2f 占完成平仓净利 %.1f%% | 其余 %d 笔合计 %+.2f'
             % (top3, ratio, python_builtins.max(0, len(trades)-3), closed-top3))
    years = {}
    for fill in g.sell_fills:
        year = fill['date'][:4]
        years[year] = years.get(year, 0.0) + fill['pnl']
    for year in sorted(years):
        log.info('【台账】分年成交已实现 %s: %+.2f' % (year, years[year]))
    unrealized = 0.0
    for code, pos in context.portfolio.positions.items():
        if pos.total_amount > 0:
            value = pos.value - pos.total_amount * pos.avg_cost
            unrealized += value
            log.info('【台账】期末持仓 %s %s 浮盈 %+.2f'
                     % (code, get_security_name(code), value))
    equity = context.portfolio.total_value
    initial = context.portfolio.starting_cash
    log.info('【台账】期末未实现 %+.2f | 期末权益 %.2f（总收益 %.2f%%）'
             % (unrealized, equity, (equity/initial-1)*100))
    difference = equity - initial - realized - unrealized
    if abs(difference) < 0.005:
        difference = 0.0
    log.info('【台账核对】权益增量 %+.2f = 成交已实现 %+.2f + 未实现 %+.2f '
             '+ 待核对账户差额 %+.2f（含现金分红等未按成交归因的变动）'
             % (equity-initial, realized, unrealized, difference))
# END SHARED PLATFORM LEDGER
