# -*- coding: utf-8 -*-
"""生成两个聚宽单文件脚本：时序全套规则 × {固定22行业池, 动态池}。

温度闸无法在平台内复算（需成分股时点成交面板），以"≥0.9 交易日区间表"
离线嵌入（源 research/studies/industry_momentum/_cache/temperature_panel.pkl，
生成物 _cache/temp_hot_ranges.py）。本生成器从仓库根运行：
python research/studies/etf_timing/make_jq_scripts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / '_cache'))
from temp_hot_ranges import TEMP_HOT  # noqa: E402

TEMPLATE = '''# -*- coding: utf-8 -*-
# __TITLE__
#
# 规则 = etf_timing 线"目前证据支持的时序策略"合成（见其 README）：
#   进场：池内 score>0 且 温度分位<0.9，按 score 降序补 TOPN=3 槽；
#   出场（只看自身，不看排名）：①跳水当日清仓（当日/前两日任一日环比<0.95）；
#     ②score≤0 连续 3 日（渐变破位）；③温度≥0.9 当日清仓；
#   仓位：每只目标 = 总值/3 × min(1, 20%/σ̂20年化)，σ̂ 仓位变动>0.2 才调仓；
#   score = 25 根收盘+现价加权回归（w 1→2，w² 加权 R²），跳水清零否决同式。
# __VARIANT__
# 边界与读法：
#   - 该合成四件（score 门/跳水/温度闸/波目）各自单变量过检，作为整体这是首跑，
#     无任何历史账户读数；对照锚（本地重放口径）：纯行业22无闸 5.33%/-35.85%、
#     22 等权 6.78%、原15跨资产轮动 36.8%/-19.0%。
#   - 不含 v3_3_2 的 -8% 限亏/保本损（属 lM 线待重跑证据，加入须单变量）。
#   - 温度表由本地 TGW/腾讯面板预计算，冻结至 2026-09-30；表内成员闸生效，
# __TEMP_COVER__
#   - 不带 lM 的成交台账机器（平台回测按现价撮合）；真金启用前须补。
# 运行：聚宽 2016-01-01 ~ 2026-10-01，初始 100000，频率每天，基准 000300，
#   无滑点，万二单边无最低佣金（对齐本地研究口径）。
import math

import numpy as np
from jqdata import *

TOPN = 3
SCORE_DAYS = 25
DIVE_RATIO = 0.95
SCORE_EXIT_CONFIRM = 3
VOL_TARGET, VOL_BAND, VOL_WIN = 0.20, 0.2, 20

__POOL_DEF__

# 温度高温区间：ETF/成分股成交额比 20 日均值的自身 expanding 分位 ≥0.9 的
# 交易日（区间含端点；离线生成，源见本仓库 industry_momentum/temperature_build.py）
TEMP_HOT = __TEMP_HOT__


def is_hot(code, d):
    for s, e in TEMP_HOT.get(code, ()):
        if s <= d <= e:
            return True
    return False


def momentum_score(close_tail, price):
    # 与 src/etf/momentum.py 同式（平台单文件自包含）；w² R²，跳水清零
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(price))
        if len(prices) < SCORE_DAYS + 1 or np.any(prices <= 0) \\
                or not np.isfinite(prices).all():
            return math.nan
        logp = np.log(prices)
        x = np.arange(len(logp), dtype=float)
        w = np.linspace(1.0, 2.0, len(logp))
        slope, intercept = np.polyfit(x, logp, 1, w=w)
        ann = math.exp(slope * 250.0) - 1.0
        ew = w ** 2
        m = float(np.average(logp, weights=ew))
        ss_res = float(np.sum(ew * (logp - (slope * x + intercept)) ** 2))
        ss_tot = float(np.sum(ew * (logp - m) ** 2))
        r2 = float(np.clip(1.0 - ss_res / ss_tot, 0.0, 1.0)) if ss_tot > 0 else 0.0
        score = ann * r2
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < DIVE_RATIO:
            return 0.0
        return float(score)
    except Exception:
        return math.nan


def frac_of(closes):
    # 波目仓位：σ̂ = 最近 20 个已完成日对数收益 std × √250；不足或异常 → 1.0
    xs = closes[-(VOL_WIN + 1):]
    if len(xs) < VOL_WIN + 1:
        return 1.0
    lr = np.diff(np.log(xs))
    if not np.isfinite(lr).all():
        return 1.0
    sig = float(np.std(lr, ddof=1)) * math.sqrt(250.0)
    if sig <= 0:
        return 1.0
    return min(1.0, VOL_TARGET / sig)


def dive_today(closes, price):
    prices = closes[-3:] + [float(price)]
    if len(prices) < 4 or min(prices) <= 0:
        return False
    return min(prices[i + 1] / prices[i] for i in range(3)) < DIVE_RATIO


def _sell(context, sec, reason, extra=""):
    order_target(sec, 0)
    g.neg_run.pop(sec, None)
    g.frac.pop(sec, None)
    log.info("卖出 %s %s：%s" % (sec, reason, extra))


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    set_slippage(FixedSlippage(0))
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=0.0002,
                             close_commission=0.0002, min_commission=0),
                   type="fund")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.pool = []
    g.day = 0
    g.neg_run = {}
    g.frac = {}
    run_daily(run_strategy, time='14:55')


__REBUILD_DEF__

def run_strategy(context):
    dt = context.current_dt
    g.day += 1
    __POOL_REFRESH__
    cd = get_current_data()
    d_int = int(dt.strftime('%Y%m%d'))

    # ── 先卖：三条自身出场 + 波目调仓，不依赖池与排名 ──
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0 or cd[sec].paused:
            continue
        px = float(cd[sec].last_price)
        if px <= 0:
            continue
        h = attribute_history(sec, SCORE_DAYS + 1, '1d', ['close'],
                              skip_paused=True)
        if h is None or len(h) < SCORE_DAYS:
            continue                              # 数据不足：持有不动
        closes = list(h['close'].values)
        score = momentum_score(closes[-SCORE_DAYS:], px)
        neg = 0 if (score is not None and score > 0) else g.neg_run.get(sec, 0) + 1
        g.neg_run[sec] = neg
        if is_hot(sec, d_int):
            _sell(context, sec, "温度≥0.9", "score=%.4f" % score)
            continue
        if dive_today(closes, px):
            _sell(context, sec, "跳水清零当日", "score=%.4f px=%.3f" % (score, px))
            continue
        if neg >= SCORE_EXIT_CONFIRM:
            _sell(context, sec, "score≤0连续%d日" % SCORE_EXIT_CONFIRM,
                  "score=%.4f" % score)
            continue
        frac = frac_of(closes)
        if abs(frac - g.frac.get(sec, 1.0)) > VOL_BAND:
            order_target_value(sec, context.portfolio.total_value / TOPN * frac)
            g.frac[sec] = frac
            log.info("波目调仓 %s → frac=%.2f" % (sec, frac))

    # ── 后买：池内 score>0、非高温，按 score 降序补槽 ──
    held = {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}
    slots = TOPN - len(held)
    if slots <= 0 or not g.pool:
        return
    cands = []
    for code in g.pool:
        if code in held or cd[code].paused:
            continue
        h = attribute_history(code, SCORE_DAYS + 1, '1d', ['close'],
                              skip_paused=True)
        if h is None or len(h) < SCORE_DAYS:
            continue
        closes = list(h['close'].values)
        px = float(cd[code].last_price)
        if px <= 0:
            continue
        score = momentum_score(closes[-SCORE_DAYS:], px)
        if score is None or score <= 0:
            continue
        if is_hot(code, d_int):
            continue                              # 温度闸：表内高温不买
        cands.append((code, score, px, closes))
    cands.sort(key=lambda t: -t[1])
    cash = context.portfolio.available_cash
    sleeve = context.portfolio.total_value
    for code, score, px, closes in cands:
        if slots <= 0:
            break
        frac = frac_of(closes)
        value = min(cash / slots, sleeve / TOPN) * frac
        if value < 100 * px * 1.0002:
            continue
        order_target_value(code, value)
        g.frac[code] = frac
        g.neg_run[code] = 0
        cash -= value
        slots -= 1
        log.info("买入 %s %s @%.3f score=%.4f frac=%.2f rank%d/%d"
                 % (code, get_security_info(code).display_name, px, score, frac,
                    len(cands), len(g.pool)))
'''


def fmt_temp(table: dict) -> str:
    items = []
    for code in sorted(table):
        rngs = ', '.join('(%d,%d)' % (a, b) for a, b in table[code])
        items.append("'%s': [%s]" % (code, rngs))
    body = ',\n    '.join(items)
    return '{\n    ' + body + ',\n}'


INDUSTRY22 = ['515170.XSHG', '512660.XSHG', '512690.XSHG', '512400.XSHG',
              '159928.XSHE', '515220.XSHG', '512010.XSHG', '515210.XSHG',
              '512170.XSHG', '159870.XSHE', '512800.XSHG', '159996.XSHE',
              '512000.XSHG', '512200.XSHG', '512480.XSHG', '159611.XSHE',
              '515880.XSHG', '516110.XSHG', '512720.XSHG', '512980.XSHG',
              '515030.XSHG', '515790.XSHG']


def jq_code(six: str) -> str:
    return six + ('.XSHG' if six.startswith(('51', '56', '58')) else '.XSHE')


def table_for_platform(table: dict) -> dict:
    return {jq_code(k): v for k, v in table.items() if k in
            {c.split('.')[0] for c in INDUSTRY22}}


def build_variant(kind: str) -> str:
    txt = TEMPLATE
    if kind == 'industry22':
        txt = txt.replace('__TITLE__',
                          'jq_timing_industry22 —— 时序全套规则 × 固定22只行业池（聚客单文件）')
        txt = txt.replace('__VARIANT__',
                          '#   池 = 固定 22 只行业 ETF（名单同 industry_momentum/unified_industry22.py），'
                          '无重建；\n#   成员不足 25 根收盘时自然跳过。')
        txt = txt.replace('__TEMP_COVER__',
                          '#   本版 22 只全覆盖（温度面板即按此 22 只建）。')
        txt = txt.replace('__POOL_DEF__',
                          'INDUSTRY22 = %r' % INDUSTRY22)
        txt = txt.replace('__POOL_REFRESH__',
                          'g.pool = INDUSTRY22')
        txt = txt.replace('__REBUILD_DEF__', '')
    else:
        txt = txt.replace('__TITLE__',
                          'jq_timing_dynamic_pool —— 时序全套规则 × 动态池（聚客单文件）')
        txt = txt.replace('__VARIANT__',
                          '#   池 = lM 规则动态池（每 20 交易日重建：非跨境/名称词表剔除/上市≥365 天/\n'
                          '#   20 日日均成交额≥5000 万/250 日收益相关<0.90 去重留流动性高者），\n'
                          '#   池构建继承 lM_v3_3_2；相关性阈值取研究基线 0.90（生产 0.85，分叉未决）。')
        txt = txt.replace('__TEMP_COVER__',
                          '#   表外成员无温度数据 → 默认无闸交易（FAIL_OPEN）；把 STRICT_TEMP 改\n'
                          '#   True 可切为"表外不买"（fail-closed），两种口径读数须分开登记。')
        txt = txt.replace('__POOL_DEF__',
                          'REBUILD_EVERY = 20\n'
                          'LIQ_AMT20_MIN = 50_000_000.0\n'
                          'CORR_DEDUPE = 0.90\n'
                          'MATURE_DAYS = 365\n'
                          'MIN_BARS = 250\n'
                          'STRICT_TEMP = False\n'
                          "EXCLUDE_CODE_PREFIXES = ('513', '518')\n"
                          "EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', "
                          "'2000', '200', '中证A500', 'A500', 'A50', 'A100', '上证50', '上证180', "
                          "'上证380', '科创50', '科创100', '科创综', '创业板50', '创业板综', "
                          "'创业板指', '创业板', '双创', '北证', '深证100', '基本面50', '红利', "
                          "'股息', '国债', '政金', '信用', '债券', '转债', '货币', '现金', '黄金', "
                          "'白银', '原油', '豆粕', '商品', '纳斯达克', '纳指', '标普', '道琼', "
                          "'日经', '德国', '法国', '亚太', '东南亚', '恒生', '香港', 'H股', '港股', "
                          "'央企', '国企', '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', "
                          "'成长', '质量', '低波', '动量', '多因子', '自由现金流', 'HK', '225', "
                          "'东证', '中韩', '美国', '恒指', '油气', '债', '上海金', '短融', '日利', "
                          "'添益', '快线')")
        txt = txt.replace('__POOL_REFRESH__',
                          'if (g.day - 1) % REBUILD_EVERY == 0:\n'
                          '        g.pool = rebuild_pool(dt)')
        rebuild = '''

def rebuild_pool(dt):
    """lM 规则动态池（继承 lM_v3_3_2，全用当时数据）。"""
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    today = dt.date()
    cands = []
    for code, row in secs.iterrows():
        if code.startswith(EXCLUDE_CODE_PREFIXES):
            continue
        hits = [k for k in EXCLUDE_KW if k in row['display_name']]
        if hits:
            continue
        start = row['start_date']
        if hasattr(start, 'date'):
            start = start.date()
        if (today - start).days < MATURE_DAYS:
            continue
        cands.append(code)
    if not cands:
        return []
    amt20 = history(20, '1d', 'money', security_list=cands).mean(axis=0)
    liq = [c for c in cands if amt20.get(c, 0) == amt20.get(c, 0)
           and amt20[c] >= LIQ_AMT20_MIN]
    if len(liq) <= TOPN:
        log.info("池重建 %s: 候选 %d | 流动 %d（去重跳过）"
                 % (dt.strftime('%Y-%m-%d'), len(cands), len(liq)))
        return liq
    rets = history(250, '1d', 'close', security_list=liq).pct_change()
    corr = rets.corr(min_periods=120)
    kept = []
    for c in sorted(liq, key=lambda x: -amt20[x]):
        ok = True
        for k in kept:
            r = corr.at[c, k]
            if r == r and r >= CORR_DEDUPE:
                ok = False
                break
        if ok:
            kept.append(c)
    log.info("池重建 %s: 候选 %d | 流动 %d | 去重后 %d"
             % (dt.strftime('%Y-%m-%d'), len(cands), len(liq), len(kept)))
    return kept
'''
        txt = txt.replace('__REBUILD_DEF__', rebuild)
    txt = txt.replace('__TEMP_HOT__', fmt_temp(table_for_platform(TEMP_HOT)))
    return txt


def main():
    for kind, name in (('industry22', 'jq_timing_industry22.py'),
                       ('dynamic', 'jq_timing_dynamic_pool.py')):
        out = HERE / name
        out.write_text(build_variant(kind), encoding='utf-8')
        print('写出', out, out.stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
