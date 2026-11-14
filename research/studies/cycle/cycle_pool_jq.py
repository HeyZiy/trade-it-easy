# -*- coding: utf-8 -*-
"""Cycle 阶段机 × 全市场 ETF 动态池——聚宽天级回测版（整文件粘贴）。

实现 strategy/cycle_strategy.md 的 8 阶段判定、动态建池、组合仓位和 T+1 执行。

三层结构：
  池子   指数映射归并（FUND_INVEST_TARGET：官方代码 → 名称内嵌代码 → 名称兜底），每指数保留 20 日均额最大一只；
         均额门槛 3000 万。
         每 60 个交易日重建，失败沿用旧池。
  信号   阶段机逐日推进（指标单一实现 + 前置状态与量能硬门槛为闸门 + 自由判据按阶段门槛 HIT_MIN + 01→08 取先命中者），信号空间 = 聚宽后复权价（fq='post'，以上市日为基准、跨日恒定）；
         v8.1 性能重构：指标按 numpy 列一次算好、逐行经只读视图判定（不再构造
         结果 DataFrame），每段先过前置与量能闸门再算判据、首中即断——与全量
         计算同序同结果，仅省未触达分支；
         缓冲直接跨日追加，止损存后复权绝对价即可跨日比较，不需要锚点校验与重拉重放。
         执行仍用真实价（当日开盘、不复权昨收）。
  组合   §4：持仓 ≤4 只、单标的 ≤30%、单笔风险 0.5%，份额 = 权益×0.5% ÷
         (开盘价−止损价)，整手向下取整后受 30% 上限与可用资金约束；
         同日多标的抢名额按 命中条数降序 → 流动均额降序（2026-11-09 用户
         指定；§3 的 20 日相对强度排序留作后续对照）。

阶段（记法 门槛/自由判据数；前置闸门与量能硬门槛不计入命中数，按 01→08
取先命中者，全不命中归 08。阶段是持续状态，被另一阶段取代才变）：
  01 恐慌底部  3/5  无前置 → 不买，仅作 02 的前置
     LH+LL、dev≤−6%、放量长下影或阴线、反转K（长下影/放量阳线）、
     收盘在 250 日低点上方 5% 内
  02 突破 WedgePop  1/3  硬门槛 量≥1.5× → 买入，止损=信号日平台低点，高开>3%不追
     前置：60 个已判定阶段内有 01，或近 10 日曾在均线下且当前非多头排列
     ATR 收缩+10 日振幅收窄、收盘破前 10 日高、同时站上 10 与 20EMA
  03 上行回踩  1/3  硬门槛 量≤0.7× → 买入，止损=动态 20EMA，不限高开
     前置：20 个已判定阶段内有 02
     最低触碰 10 或 20EMA（较低者×1.01 内）、收盘不破 20EMA 且长下影或非阴线、
     10 与 20EMA 均不低于 3 日前
  04 平台突破  3/5  无前置，硬门槛 量≥1.5× → 买入，止损=信号日平台低点，高开>3%不追
     价在 10EMA 上且 10EMA 在 20EMA 上、高点抬高、10 日振幅收窄、
     收盘距 20EMA 在 3% 内、收盘破前 10 日高
     持仓中再出现 04 只记录、不加仓
  05 衰竭延伸  3/3  无前置 → 第 2 次延伸卖 50%，第 3 次卖 75%，同段只执行一次
     dev≥+6%、滞涨K（长上影/阴线）、自底部第 2 次延伸（计数≥2）
     （v8.1 删票：ATR扩张、阴包阳、周线偏离；门槛 3/5 → 3/3，有效严格度
       提高，回测过难可下调为 2 做对照）
  06 跌破均线 WedgeDrop  2/4 → 清仓
     前置：20 个已判定阶段内有 05，或近 10 日出现过 dev≥6%
     反弹高点降低、放量跌破 20EMA、收盘破前 10 日低、当日刚跌破 20EMA（前一日在上方）
  07 下行反抽  2/3 → 清仓
     前置：20 个已判定阶段内有 06，且收盘在 20EMA 下、20EMA 低于 5 日前
     最高反抽到≥20EMA×0.99 且缩量、长上影或阴线、高点降低
  08 下行破位  —   无判据，禁买；也是全不命中时的兜底标签
     （v8.1：原 3 条判据对兜底结果零影响，删除；EXIT_REASON 不得加 '08'）

执行（§6 T+1）：T−1 收盘判定 → T 日 09:31 开盘市价执行；突破高开 >3% 不追
（当日放弃，次日阶段机自然重估）；停牌/无法成交顺延不撤单、中间不重判
（挂单队列）；当日有卖出则不开新仓（沿用 reversal_v1 纪律）。

回测口径：
- 05 分批止盈按仓位跟踪延伸段，同一标的同段内重新建仓可再次分批卖出。
- 成本按 §7：万一双边、零滑点、无最低佣金。
- 新入池标的只回放 140 日热身阶段历史，seen 前置条件在热身期偏保守
  （少触发不误触发），约 60 个交易日后与全历史重放一致。
- 数据为聚宽后复权日线，量能口径用 volume；停牌行（volume=0）剔除以
  对齐 akshare 缺行口径。
- 宽基和行业统一 ±6% 偏离阈值，不做差异化。
- 入场诊断：每次成交把 入场日/阶段/阶段起始日/阶段第N日/开盘/信号收/高开
  幅度/份额/止损 追加写入环境文件 cycle_pool_jq_entries.csv，并覆盖写
  cycle_pool_jq_stage_age.csv（按时效分桶），回测结束后取文件核对信号时效。

从聚宽回测运行：频率选"每天"，整文件粘贴，基准 000300.XSHG，需 finance
数据权限（建池查跟踪指数映射）。
"""
from collections import deque
import re

import numpy as np
import pandas as pd
from jqdata import *
# 必须放在星号导入之后：jqdata 会遮蔽这几个内建名，显式还原
from builtins import any, min, max, sum

# 逐行性能分析：只在"运行回测"的结果页可见，本身拖慢回测，长区间跑前注释掉
enable_profile()

# ---- 池子（§1 + etf_pool 口径）----
LIQ_MIN = 30_000_000.0
LIQUIDITY_DAYS = 20
POOL_REFRESH_DAYS = 60
BATCH_SIZE = 50
PRICE_BATCH_SIZE = 100
KEEP_UNMAPPED = False
SUFFIX_RE = re.compile(r'\.(?:XSHG|XSHE|SH|SZ|CSI)$')
NAME_CODE_RE = re.compile(r'[（(](\d{6}(?:\.(?:XSHG|XSHE|SH|SZ|CSI))?)[）)]')

# ---- cycle 内核（§2/§3）----
EMA_FAST, EMA_SLOW = 10, 20
ATR_FAST, ATR_SLOW = 5, 20
DEV_TH = 0.06   # 偏离 10EMA 的统一阈值，上下共用（§2 ±6%）
VOL_SPIKE, VOL_DRY = 1.5, 0.7
PIVOT_WIN = 5
PLAT_WIN = 10
STRUCT_WIN = 20
LOW250_TOL = 0.05
NEAR_EMA = 0.03
CHASE_LIMIT = 0.03
# 自由判据门槛：前置状态与量能硬门槛已移出计数、改为闸门，故各阶段分母不同。
# 取值对齐重构前的有效严格度；02/03 的 1/3 是现行口径，是否收紧留给回测对照。
# 05 原 5 票取 3，v8.1 删至 3 票（吞没/ATR扩张/周线），3 = 全中，有效严格度
# 提高；回测显示 05 过难可下调为 2 做对照。08 是无判据兜底，不设门槛。
HIT_MIN = {"01": 3, "02": 1, "03": 1, "04": 3, "05": 3, "06": 2, "07": 2}
STAGES = ("01", "02", "03", "04", "05", "06", "07", "08")
VOL_GATE = {"02": "vol_spike", "03": "vol_dry", "04": "vol_spike"}

# ---- 组合层（§4）----
MAX_NAMES = 4
MAX_SINGLE = 0.30
RISK = 0.005
COMMISSION = 0.0001
EXIT_REASON = {'06': 'wedge-drop', '07': 'down-crossback'}   # §6 阶段清仓

# ---- 数据缓冲 ----
BUF_BARS = 420
INC_BARS = 3
BOOTSTRAP_REPLAY = 140
TAIL_KEEP = 70
MIN_BUFFER = 130

# ---- 诊断导出（聚宽环境文件，回测结束后在根目录取）----
ENTRY_FILE = 'cycle_pool_jq_entries.csv'
AGE_FILE = 'cycle_pool_jq_stage_age.csv'


# ======================================================================
# cycle 内核：指标与阶段判定
# ======================================================================

def plain_code(code):
    return SUFFIX_RE.sub('', str(code).strip().upper())


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def atrs(df):
    """TrueRange 只算一次，快慢两条 ATR 共享（旧 atr() 两次调用重复算 tr）。"""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift()
    # fmax 而非 maximum：对齐 DataFrame.max 的 skipna，首行 prev_c 为 NaN 时仍取 h-l
    tr = np.fmax(np.fmax(h - l, (h - prev_c).abs()), (l - prev_c).abs())
    return (tr.rolling(ATR_FAST).mean().values,
            tr.rolling(ATR_SLOW).mean().values)


def _shift(a, k):
    """Series.shift(k) 的 numpy 等价（float 数组右移，移出补 NaN）。"""
    if k <= 0:
        return a
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k]
    return out


def _shift_ffill(a, k):
    """Series.shift(k).ffill() 的 numpy 等价：右移后向前填充最近非 NaN。"""
    if not len(a):
        return a
    if k:
        a = np.concatenate([np.full(k, np.nan), a[:-k]])
    idx = np.where(np.isnan(a), -1, np.arange(len(a)))
    np.maximum.accumulate(idx, out=idx)
    return np.where(idx >= 0, a[np.maximum(idx, 0)], np.nan)


def _grouped_cumsum(x, g):
    """x.groupby(g).cumsum() 的 numpy 等价，仅要求 g 非降（组连续）。

    组号可能不从 0 开始（如 bottom.cumsum() 首行即为 True 时从 1 起），
    故按 run 序号而非组号取基值，避免 off-by-one。
    """
    seg_cum = np.cumsum(x)
    new_group = np.r_[True, g[1:] != g[:-1]]
    run_id = np.cumsum(new_group) - 1                # 每行所属 run 的序号
    base = (seg_cum - x)[np.flatnonzero(new_group)]  # 各 run 开始前的累计
    return seg_cum - base[run_id]


def pivots(df, win=PIVOT_WIN):
    """bool 序列：high[i] 是 [i-win, i+win] 内最高 → 高点枢轴。"""
    width = 2 * win + 1
    h, l = df["high"], df["low"]
    ph = h == h.rolling(width, center=True, min_periods=width).max()
    pl = l == l.rolling(width, center=True, min_periods=width).min()
    return ph, pl


def B(x):
    """判据结果转 bool。指标列已归一为 numpy 数组，NaN 参与比较在建列时
    即得 False（与 pandas 比较语义一致），不再需要 pd.notna 检查。"""
    return bool(x)


class RowView(object):
    """指标列字典的单行只读视图：r[k] 等价旧版 ind[k].iloc[p]，免逐行拷贝。"""

    __slots__ = ("cols", "p")

    def __init__(self, cols, p):
        self.cols = cols
        self.p = p

    def __getitem__(self, key):
        return self.cols[key][self.p]

    def window(self, key, k):
        """含当前行在内最近 k 行的切片；不足 k 行时长度 < k。"""
        p = self.p
        return self.cols[key][max(0, p - k + 1):p + 1]


def build_indicators(df):
    """§2 指标单一实现。每一行只用到截至当日的数据。

    返回 {指标名: np.ndarray}，逐行经 RowView 取标量判定，不构造结果
    DataFrame（旧版构造触发 BlockManager 整理复制，resample/groupby 占
    大头开销，均在此去除）。列语义与旧 DataFrame 版本一致：NaN 参与比较
    得 False。相对旧版删除的列（v8.1 规格删减或无消费方）：
    - 周线族 wk_*（05 的"周线偏离"票，规格删减）
    - bull/bear_engulf（01/05 的吞没票，规格删减）
    - atr_expand（05 票，规格删减）、below_consol（仅服务 08 兜底死列）
    - open/dev/a5/a20/vol_ratio/swing_high/swing_low/e10（无消费方的中间量）
    prior_down/prior_exh 不再建列，由 stage_prereq 按行取窗口惰性计算。
    """
    c = df["close"].values
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    v = df["volume"].values

    e10 = ema(df["close"], EMA_FAST).values
    e20 = ema(df["close"], EMA_SLOW).values
    dev = (c - e10) / e10
    a5, a20 = atrs(df)
    vol_ratio = v / df["volume"].rolling(20).mean().values

    ph, pl = pivots(df)
    swing_high = _shift_ffill(np.where(ph.values, h, np.nan), PIVOT_WIN)
    swing_low = _shift_ffill(np.where(pl.values, l, np.nan), PIVOT_WIN)

    rng = np.where(h - l == 0, np.nan, h - l)        # (h-l).replace(0, nan)
    long_lower = (np.minimum(c, o) - l) / rng >= 0.4
    long_upper = (h - np.maximum(c, o)) / rng >= 0.4
    bullish, bearish = c > o, c < o

    plat_hi = df["high"].rolling(PLAT_WIN).max().values
    plat_lo = df["low"].rolling(PLAT_WIN).min().values
    plat_high, plat_low = _shift(plat_hi, 1), _shift(plat_lo, 1)
    rng10 = (plat_hi - plat_lo) / c
    narrow = rng10 < _shift(rng10, PLAT_WIN)

    hh = swing_high > _shift(swing_high, STRUCT_WIN)
    lh = swing_high < _shift(swing_high, STRUCT_WIN)
    ll = swing_low < _shift(swing_low, STRUCT_WIN)

    low250 = df["low"].rolling(250, min_periods=250).min().values
    near250 = c <= low250 * (1 + LOW250_TOL)

    above = (c > e10) & (e10 > e20)
    below = (c < e10) & (e10 < e20)
    dev_hi = dev >= DEV_TH
    dev_lo = dev <= -DEV_TH

    ext_on = dev > DEV_TH
    if ext_on.size:
        prev_ext = np.concatenate(([False], ext_on[:-1]))
        seg_start = ext_on & ~prev_ext
        seg_id = np.cumsum(seg_start)
        bottom = dev_lo | (c <= low250 * 1.02)
        ext_count = _grouped_cumsum(seg_start.astype(np.int64), np.cumsum(bottom))
    else:
        seg_id = ext_count = np.zeros(0, dtype=np.int64)

    return {
        "close": c,
        "e20": e20,
        "above": above, "below": below,
        "ma_flat_up": (e10 >= _shift(e10, 3)) & (e20 >= _shift(e20, 3)),
        "ma_down": e20 < _shift(e20, 5),
        "dev_hi": dev_hi, "dev_lo": dev_lo,
        "atr_contract": a5 < a20,
        "vol_spike": vol_ratio >= VOL_SPIKE, "vol_dry": vol_ratio <= VOL_DRY,
        "hh": hh, "lh": lh, "ll": ll,
        "narrow": narrow, "near250": near250,
        "long_lower": long_lower, "long_upper": long_upper,
        "bullish": bullish, "bearish": bearish,
        "break_high": c > plat_high, "break_low": c < plat_low,
        "touch_ma": l <= np.minimum(e10, e20) * 1.01,
        "close_above_ma": (c > e10) & (c > e20),
        "close_below_ma": c < e20,
        "near_e20": np.abs(c - e20) / e20 < NEAR_EMA,
        "fresh_below": (c < e20) & (_shift(c, 1) >= _shift(e20, 1)),
        "bounce_to_ma": h >= e20 * 0.99,
        "plat_low": plat_low,
        "ext_on": ext_on, "ext_seg": seg_id, "ext_count": ext_count,
    }


def seen(hist, stage, lookback):
    """§3 前置条件"已发生阶段 X"：查最近 lookback 个已判定阶段。"""
    return any(s == stage for s in list(hist)[-lookback:])


def stage_prereq(code, r, hist):
    """§3 前置状态闸门（按阶段惰性求值）：不成立则该阶段本轮不参与判定，
    不计入命中条数。

    prior_down/prior_exh 原为整列 rolling(10).max().fillna(0)，现仅在
    seen() 未命中时取含当前行的 10 行窗口等价计算（窗口不足 10 行为
    False，与 min_periods=10 一致）；prior_down 原式 below|(c<e20) 中
    below 蕴含 c<e20，并入后只判 c<e20。
    """
    if code == "02":
        if seen(hist, "01", 60):
            return True
        wc, we = r.window("close", 10), r.window("e20", 10)
        return (len(wc) == 10 and bool((wc < we).any())
                and not B(r["above"]))
    if code == "03":
        return seen(hist, "02", 20)
    if code == "06":
        if seen(hist, "05", 20):
            return True
        w = r.window("dev_hi", 10)
        return len(w) == 10 and bool(w.any())
    if code == "07":
        return seen(hist, "06", 20) and B(r["below"]) and B(r["ma_down"])
    return True


def stage_checks(code, r):
    """§3 自由判据清单（按阶段惰性构建），r 为单日指标行视图；
    前置与量能硬门槛见 stage_prereq / VOL_GATE。

    08 是兜底阶段、无判据（旧版对其判据的计算对 winner 选择零影响）。
    v8.1 规格删减：01"反转K线"与 05"滞涨K"去掉吞没形态票，05 去掉
    ATR 扩张与周线偏离票（05 判据 5→3，HIT_MIN 仍 3，有效严格度提高）。
    """
    if code == "01":
        rev_k = B(r["vol_spike"] and (r["long_lower"] or r["bearish"]))
        rev_bar = B(r["long_lower"] or (r["bullish"] and r["vol_spike"]))
        return [("LH+LL", B(r["lh"] and r["ll"])),
                ("dev<=-阈值", B(r["dev_lo"])),
                ("放量长下影/阴线", rev_k),
                ("反转K线", rev_bar),
                ("250日低点附近", B(r["near250"]))]
    if code == "02":
        return [("ATR收缩+平台", B(r["atr_contract"] and r["narrow"])),
                ("突破结构高点", B(r["break_high"])),
                ("站上10/20EMA", B(r["close_above_ma"]))]
    if code == "03":
        return [("触碰10/20EMA", B(r["touch_ma"])),
                ("企稳K/收盘不破20EMA",
                 B((r["close"] >= r["e20"]) and (r["long_lower"] or not r["bearish"]))),
                ("均线走平/向上", B(r["ma_flat_up"]))]
    if code == "04":
        return [("价在10/20EMA上方", B(r["above"])),
                ("高点抬高", B(r["hh"])),
                ("平台收窄", B(r["narrow"])),
                ("EMA贴近价格", B(r["near_e20"])),
                ("突破平台高点", B(r["break_high"]))]
    if code == "05":
        return [("dev>=阈值", B(r["dev_hi"])),
                ("滞涨K", B(r["long_upper"] or r["bearish"])),
                ("自底部第2次延伸", B(r["ext_count"] >= 2))]
    if code == "06":
        return [("反弹高点降低", B(r["lh"])),
                ("放量跌破10/20EMA", B(r["close_below_ma"] and r["vol_spike"])),
                ("跌破平台低点", B(r["break_low"])),
                ("收盘有效站回均线下方", B(r["fresh_below"]))]
    if code == "07":
        return [("反抽近均线+缩量", B(r["bounce_to_ma"] and r["vol_dry"])),
                ("均线处滞涨/反转K", B(r["long_upper"] or r["bearish"])),
                ("更低高点", B(r["lh"]))]
    return []


def advance_stage(r, hist):
    """单日判定：按 01→07 逐段过 前置闸门 → 量能闸门 → 判据计数，
    首个达标阶段即胜出；无达标者落 08 兜底（无判据，hits=0）。

    旧版先全量构建 8 阶段判据再取首个达标阶段，选择结果一致；此处短路
    求值，只计算被触达的阶段。EXIT_REASON 不得包含 '08'（兜底标签，
    加进清仓会让所有判据不达标的标的被清仓）。
    """
    for code in STAGES:
        if code == "08":
            break                           # 兜底阶段无判据，不参与命中
        if not stage_prereq(code, r, hist):
            continue
        gate = VOL_GATE.get(code)
        if gate is not None and not B(r[gate]):
            continue
        hits = sum(1 for _, ok in stage_checks(code, r) if ok)
        if hits >= HIT_MIN[code]:
            return code, hits
    return "08", 0


# ======================================================================
# 建池：移植 etf_pool.py（聚宽平台函数）
# ======================================================================

def index_identity(row):
    """跟踪指数归并键：官方代码 → 名称内嵌代码 → 名称本身；无信息返回 None。"""
    raw = row.get('traced_index_code')
    if pd.notna(raw) and str(raw).strip():
        return plain_code(raw)
    name = row.get('traced_index_name')
    if not (pd.notna(name) and str(name).strip()):
        return None
    keys = {plain_code(token) for token in NAME_CODE_RE.findall(str(name).upper())}
    if len(keys) == 1:
        return keys.pop()
    return 'NAME:' + re.sub(r'\s+', '', str(name)).upper()


def classify_etfs(asof, batch_size=BATCH_SIZE):
    """点时全量分类快照：全市场 ETF 带 pool_key。"""
    date = pd.Timestamp(asof).normalize()
    date_str = str(date.date())
    securities = get_all_securities(['fund'], date=date_str)
    if securities is None or securities.empty:
        raise ValueError('历史基金名单为空')
    etfs = securities[securities['type'].astype(str).str.lower() == 'etf'].copy()
    codes = sorted(etfs.index)
    if not codes:
        raise ValueError('历史ETF名单为空')

    cache = g.index_key_cache
    need = [c for c in codes if str(c) not in cache]
    records = []
    for offset in range(0, len(need), batch_size):
        batch = need[offset:offset + batch_size]
        result = finance.run_query(query(finance.FUND_INVEST_TARGET).filter(
            finance.FUND_INVEST_TARGET.code.in_(batch),
            finance.FUND_INVEST_TARGET.pub_date <= date_str))
        if result is not None and not result.empty:
            records.append(result)

    indexed = None
    if records:
        mapping = pd.concat(records, ignore_index=True)
        need_cols = ['code', 'pub_date', 'start_date', 'end_date']
        missing = [c for c in need_cols if c not in mapping.columns]
        if missing:
            raise ValueError('FUND_INVEST_TARGET 缺少列 %s，实际列 %s'
                             % (missing, list(mapping.columns)))
        for field in ['pub_date', 'start_date', 'end_date']:
            mapping[field] = pd.to_datetime(mapping[field])
        mapping = mapping[mapping.pub_date <= date]
        mapping = mapping[(mapping.start_date <= date)
                          & (mapping.end_date.isna() | (mapping.end_date > date))]
        mapping = mapping.sort_values('pub_date').drop_duplicates('code', keep='last')
        keep = [c for c in ('traced_index_name', 'traced_index_code')
                if c in mapping.columns]
        if keep:
            indexed = mapping.set_index('code')[keep]
    if indexed is None:
        etfs['traced_index_name'] = None
        etfs['traced_index_code'] = None
    else:
        etfs = etfs.join(indexed)

    keys = []
    for code, row in etfs.iterrows():
        key = cache.get(str(code))
        if key is None:
            key = index_identity(row)
            if key is not None:
                cache[str(code)] = key   # 只缓存成功映射，未映射的下次重查
        keys.append(key)
    etfs['pool_key'] = keys
    return etfs


def to_long_frame(data, batch, fields):
    """统一 get_price(panel=False) 的返回形态为含 time/code 的长表。

    聚宽在标的数与字段数不同时返回结构不同：多字段一般是长表，单字段
    或多标的单标的情形会退化成宽表（列是代码或字段）。这里统一归一，
    调用方只面对一种形状。
    """
    if data is None or data.empty:
        return None
    frame = data.copy()
    if 'code' not in frame.columns:
        if 'time' not in frame.columns:
            frame.index.name = 'time'
            frame = frame.reset_index()
        if 'code' not in frame.columns:
            if set(fields).issubset([str(c) for c in frame.columns]):
                frame['code'] = str(batch[0])       # 单标的宽表（列=字段）
            else:
                frame = frame.melt(id_vars='time', var_name='code',
                                   value_name=fields[0])  # 宽表（列=代码）
                frame['code'] = frame['code'].astype(str)
    if 'time' in frame.columns:
        frame['time'] = pd.DatetimeIndex(frame['time']).normalize()
    return frame


def average_money(codes, date_str, days=LIQUIDITY_DAYS,
                  batch_size=PRICE_BATCH_SIZE):
    """过去 days 个交易日均成交额；缺行按实际返回行数平均，无数据记 0。"""
    records = []
    for offset in range(0, len(codes), batch_size):
        batch = codes[offset:offset + batch_size]
        data = get_price(batch, end_date=date_str,
                         count=days, frequency='daily', fields=['money'],
                         skip_paused=True, panel=False)
        frame = to_long_frame(data, batch, ['money'])
        if frame is not None and {'code', 'money'}.issubset(frame.columns):
            records.append(frame[['code', 'money']])
    if not records:
        return pd.Series(0.0, index=codes)
    flat = pd.concat(records, ignore_index=True)
    flat['money'] = pd.to_numeric(flat['money'], errors='coerce')
    means = flat.dropna(subset=['money']).groupby('code')['money'].mean()
    return means.reindex([str(c) for c in codes]).fillna(0.0).astype(float)


def build_pool(asof):
    """点时快照建池：归类 → 均额门槛 → 每指数取均额最大一只。"""
    date_str = str(pd.Timestamp(asof).normalize().date())
    etfs = classify_etfs(asof)
    total = len(etfs)

    rest = etfs['pool_key'].isna()
    if KEEP_UNMAPPED:
        etfs.loc[rest, 'pool_key'] = [plain_code(code) for code in etfs.index[rest]]
    else:
        etfs = etfs[~rest].copy()
    classifiable = len(etfs)

    etfs['avg_money'] = average_money(etfs.index.tolist(), date_str)
    etfs = etfs[etfs['avg_money'] >= LIQ_MIN].copy()
    liquid = len(etfs)

    etfs = etfs.assign(_code=etfs.index.astype(str))
    etfs = etfs.sort_values(['avg_money', '_code'],
                            ascending=[False, True])
    selected = etfs.drop_duplicates(subset=['pool_key'], keep='first')
    log.info('建池 %s:ETF=%d 可归类=%d 均额≥%.0f万=%d 代表=%d'
             % (date_str, total, classifiable, LIQ_MIN / 1e4, liquid,
                len(selected)))
    return [dict(code=str(code), pool_key=str(row['pool_key']),
                 avg_money=float(row['avg_money']))
            for code, row in selected.iterrows()]


# ======================================================================
# 数据缓冲与阶段推进
# ======================================================================

def fetch_frames(codes, end_date, count):
    """分批取日线（后复权，含停牌行），返回 {code: OHLCV 帧}。

    后复权以上市日为基准、跨日恒定，缓冲因此能跨日复用，不必校验复权漂移。
    """
    out = {}
    fields = ['open', 'high', 'low', 'close', 'volume']
    for offset in range(0, len(codes), PRICE_BATCH_SIZE):
        batch = codes[offset:offset + PRICE_BATCH_SIZE]
        data = get_price(batch, end_date=end_date, count=count,
                         frequency='daily', fields=fields,
                         skip_paused=False, fq='post', panel=False)
        frame = to_long_frame(data, batch, fields)
        if frame is None or not (set(fields) | {'code', 'time'}).issubset(frame.columns):
            continue
        for code, grp in frame.groupby('code'):
            out[str(code)] = (grp.sort_values('time')
                              .set_index('time')[fields])
    return out


def active_frame(frame):
    """剔除停牌行（volume=0）以对齐 akshare 缺行口径；无有效行返回 None。"""
    if frame is None or frame.empty:
        return None
    frame = frame[frame['volume'] > 0]
    return None if frame.empty else frame


def advance_code(code):
    """把缓冲里尚未判定的行逐日推进阶段机，刷新 g.sig / g.stage_info。"""
    df = g.buf[code]
    hist = g.hist_tail[code]
    bar_date = g.bar_date[code]
    info = g.stage_info[code]
    if bar_date is None:
        positions = range(max(0, len(df) - BOOTSTRAP_REPLAY), len(df))
    elif df.index[-1] > bar_date:
        # 首个 > bar_date 的位置，等价旧版全表枚举 d > bar_date
        positions = range(df.index.searchsorted(bar_date, "right"), len(df))
    else:
        return                          # 无新行，sig 已是最新，跳过重算
    ind = build_indicators(df)
    stage = info['stage'] if info else None
    since = g.stage_since.get(code)
    for p in positions:
        winner, hits = advance_stage(RowView(ind, p), hist)
        hist.append(winner)
        if winner != stage:
            stage = winner
            since = str(df.index[p].date())
        info = {'stage': winner, 'hits': hits}
    g.stage_since[code] = since
    g.bar_date[code] = df.index[-1]
    row = RowView(ind, len(df) - 1)
    plat_low = row['plat_low']
    g.sig[code] = {'close': float(row['close']),
                   'e20': float(row['e20']),
                   'plat_low': float(plat_low) if pd.notna(plat_low) else None,
                   'ext_on': bool(row['ext_on']),
                   'ext_seg': int(row['ext_seg']),
                   'ext_count': int(row['ext_count'])}
    g.stage_info[code] = info


def advance_all():
    for code in list(g.buf):
        try:
            advance_code(code)
        except Exception as exc:
            log.info('阶段推进失败 %s %r' % (code, exc))


def fetch_and_advance(prev):
    """每日增量拉行追加进缓冲；后复权价跨日恒定，重叠行无需校验。"""
    codes = list(g.buf)
    if not codes:
        return
    inc = fetch_frames(codes, prev, INC_BARS)
    for code in codes:
        frame = active_frame(inc.get(code))
        if frame is None:
            continue
        buf = g.buf[code]
        newer = frame[frame.index > buf.index[-1]]
        if not len(newer):
            continue
        merged = pd.concat([buf, newer])
        # 缓冲截断：超出 BUF_BARS+热身余量时丢弃最旧的热身段，重放窗口
        # 恰好覆盖余量段，均摊每天约 1 行重放；截断后 seen 历史重置，
        # 语义与新标的 bootstrap 一致（偏保守）。
        if len(merged) > BUF_BARS + BOOTSTRAP_REPLAY:
            merged = merged.iloc[-BUF_BARS:]
            g.bar_date[code] = None
            g.hist_tail[code] = deque(maxlen=TAIL_KEEP)
            # 保留 stage_info/stage_since：重放只用于补 hist，阶段未变则
            # 起始日沿用旧值，避免每次截断把阶段时效重置成重放窗口长度
        g.buf[code] = merged
    advance_all()


def refresh_pool(context, prev):
    """重建池子；新入池标的拉 BUF_BARS 根热身，不再跟踪的清理状态。"""
    rows = build_pool(prev)
    g.pool_rows = rows
    g.pool_date = prev
    tracked = {row['code'] for row in rows} | set(held_positions(context))
    for code in list(g.buf):
        if code not in tracked:
            for store in (g.buf, g.hist_tail, g.bar_date, g.sig,
                          g.stage_info, g.stage_since):
                store.pop(code, None)
    new_codes = [c for c in sorted(tracked) if c not in g.buf]
    frames = fetch_frames(new_codes, prev, BUF_BARS)
    for code in new_codes:
        df = frames.get(code)
        if df is None or df.empty:
            log.info('无历史数据，暂不跟踪 %s' % code)
            continue
        df = df[df['volume'] > 0]
        if df.empty or len(df) < MIN_BUFFER:
            log.info('历史不足%d根，暂不跟踪 %s（%d根）' % (MIN_BUFFER, code, len(df)))
            continue
        g.buf[code] = df
        g.hist_tail[code] = deque(maxlen=TAIL_KEEP)
        g.bar_date[code] = None
        g.sig[code] = None
        g.stage_info[code] = None
        g.stage_since[code] = None
    advance_all()


def pool_stale(date_key):
    try:
        days = get_trade_days(start_date=g.pool_date, end_date=date_key)
        return len(days) > POOL_REFRESH_DAYS
    except Exception:
        return True


# ======================================================================
# 账户与订单
# ======================================================================

def held_positions(context):
    return {c: p for c, p in context.portfolio.positions.items() if p.total_amount > 0}


def sync_book(context):
    today = str(context.current_dt.date())
    held = held_positions(context)
    live = get_open_orders()
    pending_buys = {o.security for o in live.values() if o.is_buy}
    for code, meta in list(g.book.items()):
        if code in held:
            if meta['entry_date'] is None:
                meta['entry_date'] = today   # 首次观察真实持仓，不以接受委托冒充成交
        elif code not in pending_buys:
            del g.book[code]


def do_sell(context, code, frac, reason):
    """提交卖单，返回是否成交（拒单/跌停返回 False，调用方顺延重试）。"""
    pos = context.portfolio.positions.get(code)
    if pos is None or pos.total_amount <= 0:
        return True
    if frac >= 1.0:
        result = order_target(code, 0)
        log.info('清仓 %s 委托=%s %s' % (code, result is not None, reason))
        return result is not None
    n = min(int(pos.total_amount * frac), pos.closeable_amount) // 100 * 100
    if n < 100:
        log.info('减仓不足一手跳过 %s %s' % (code, reason))
        return True
    result = order(code, -n)
    log.info('减仓 %s %d份(%d%%) 委托=%s %s'
             % (code, n, int(frac * 100), result is not None, reason))
    return result is not None


def submit_sell(context, code, frac, reason):
    """卖出判定已定：可执行即执行，停牌/T+1 不可卖则挂单顺延。"""
    pos = context.portfolio.positions.get(code)
    if pos is None or pos.total_amount <= 0:
        return
    g.sold_day = str(context.current_dt.date())
    od = {'code': code, 'side': 'SELL', 'frac': frac, 'reason': reason}
    cd = get_current_data()[code]
    if cd.paused or pos.closeable_amount <= 0:
        g.retry_orders.append(od)
        log.info('卖出顺延 %s %s %s' % (code, frac, reason))
        return
    if not do_sell(context, code, frac, reason):
        # 委托被拒（跌停/流动性不足）：次日开盘重试，中间不重判（§6）
        g.retry_orders.append(od)


def execute_pending(context):
    """挂单队列开盘重试：成交或作废则出队，停牌/拒单留队次日再试（§6）。"""
    today = str(context.current_dt.date())
    still = []
    for od in g.retry_orders:
        code = od['code']
        if od['side'] == 'BUY' and code not in g.buf:
            log.info('挂单作废(出池) %s' % code)
            continue                    # 已出池/无数据，买单作废
        cd = get_current_data()[code]
        if od['side'] == 'SELL':
            pos = context.portfolio.positions.get(code)
            if pos is None or pos.total_amount <= 0:
                continue                    # 已平仓，挂单作废
            if cd.paused or pos.closeable_amount <= 0:
                still.append(od)
                continue
            g.sold_day = today
            if not do_sell(context, code, od['frac'], od['reason']):
                still.append(od)        # 仍被拒（连续跌停），明日再试
        elif cd.paused:
            log.info('挂单顺延(停牌) %s' % code)
            still.append(od)
        elif execute_buy(context, od) == 'defer':
            still.append(od)
    g.retry_orders = still


def execute_buy(context, od):
    """执行买单：返回 'done' / 'defer'（调用方决定是否入挂单队列）。"""
    code = od['code']
    cd = get_current_data()[code]
    if cd.paused:
        return 'defer'
    px = float(cd.day_open)
    if not np.isfinite(px) or px <= 0:
        return 'defer'
    if od['no_chase'] and px > od['sig_close_real'] * (1 + CHASE_LIMIT):
        g.chase_skips += 1
        log.info('高开>3%%不追 %s 开盘=%.3f 信号收=%.3f'
                 % (code, px, od['sig_close_real']))
        return 'done'
    equity = float(context.portfolio.total_value)
    # 止损绝对位（真实价空间）= 比率 × 信号日真实收盘；风险距离按今开计，
    # 跳空高开时距离变大、份额自动收缩，保持单笔 0.5% 风险口径
    distance = px - od['stop_ratio'] * od['sig_close_real']
    if not np.isfinite(equity) or equity <= 0 or not np.isfinite(distance) or distance <= 0:
        log.info('份额计算无效，放弃 %s' % code)
        return 'done'
    cash = float(context.portfolio.available_cash)
    risk_lots = int(equity * RISK / distance // 100) * 100
    single_cap = int(equity * MAX_SINGLE / px // 100) * 100
    cash_cap = int(cash / (px * (1 + COMMISSION)) // 100) * 100
    n = min(risk_lots, single_cap, cash_cap)
    if n < 100:
        log.info('预算不足跳过 %s %s' % (code, od['reason']))
        return 'done'
    g.book[code] = {'entry_date': None, 'stage': od['stage'],
                    'stop_kind': od['stop_kind'], 'stop_ratio': od['stop_ratio'],
                    'stop_price': od['stop_price'],
                    'ext_seg_seen': None, 'ext_ordinal': 0, 'ext_sold': False}
    result = order(code, n)
    live = any(o.security == code and o.is_buy for o in get_open_orders().values())
    if result is not None and (int(result.filled) > 0 or live):
        g.bought_today.add(code)
        age = od.get('stage_age')
        log.info('买入 %s %d份 %s 止损=%s(比率%.3f) 阶段第%s日'
                 % (code, n, od['reason'], od['stop_kind'], od['stop_ratio'],
                    '-' if age is None else age))
        record_entry(context, od, px, n)
        return 'done'
    g.book.pop(code, None)
    log.info('买入拒单/零成交 %s' % code)
    return 'done'


def real_prev_closes(codes, prev):
    """真实价（不复权）昨收，用于高开不追判断——与当日开盘同一价格空间。

    故意取两个字段：多字段在聚宽返回含 time/code 的长表（与 fetch_frames
    同形）；单字段请求在部分平台版本会退化成宽表，避开这一形态差异。
    """
    out = {}
    for offset in range(0, len(codes), PRICE_BATCH_SIZE):
        batch = codes[offset:offset + PRICE_BATCH_SIZE]
        data = get_price(batch, end_date=prev, count=1, frequency='daily',
                         fields=['close', 'volume'], skip_paused=False,
                         fq=None, panel=False)
        frame = to_long_frame(data, batch, ['close', 'volume'])
        if frame is None or not {'code', 'close'}.issubset(frame.columns):
            log.info('昨收取价解析失败 shape=%r columns=%r'
                     % (None if data is None else getattr(data, 'shape', None),
                        None if data is None else list(data.columns)[:8]))
            continue
        for _, rec in frame.iterrows():
            v = float(rec['close'])
            if np.isfinite(v) and v > 0:
                out[str(rec['code'])] = v
    return out


def manage_held(context):
    """T−1 收盘数据判定持仓动作（§6 T+1：今日开盘执行）。"""
    today = str(context.current_dt.date())
    for code in held_positions(context):
        meta = g.book.get(code)
        if meta is None:
            log.info('未知持仓元数据，禁止新买并人工核查: %s' % code)
            continue
        if meta['entry_date'] is None or meta['entry_date'] >= today:
            continue
        if any(od['code'] == code for od in g.retry_orders):
            continue                        # 顺延中不重判
        info = g.stage_info.get(code)
        sig = g.sig.get(code)
        if not info or not sig:
            continue
        if sig['ext_on'] and sig['ext_seg'] != meta['ext_seg_seen']:
            meta['ext_seg_seen'] = sig['ext_seg']
            meta['ext_ordinal'] = sig['ext_count']
            meta['ext_sold'] = False
        st = info['stage']
        # 20EMA 动态跟踪取当日值；平台低点固定于信号日，后复权绝对价跨日可比
        cur_stop = sig['e20'] if meta['stop_kind'] == '20EMA' else meta['stop_price']
        if st in EXIT_REASON:
            submit_sell(context, code, 1.0, EXIT_REASON[st])
        elif np.isfinite(cur_stop) and sig['close'] < cur_stop:
            submit_sell(context, code, 1.0, 'stop-%s' % meta['stop_kind'])
        elif st == '05' and not meta['ext_sold'] and meta['ext_ordinal'] in (2, 3):
            ordinal = meta['ext_ordinal']
            meta['ext_sold'] = True
            submit_sell(context, code, 0.5 if ordinal == 2 else 0.75,
                        'exhaustion-%d' % ordinal)


def stage_age_of(code, prev):
    """信号日（prev）距当前阶段起始日的交易日数；当日首次进入阶段 = 1。"""
    since = g.stage_since.get(code)
    if not since:
        return None
    try:
        return len(get_trade_days(start_date=since, end_date=prev))
    except Exception as exc:
        log.info('阶段天数计算失败 %s since=%s %r' % (code, since, exc))
        return None


def _csv_num(v, nd=4):
    if v is None:
        return ''
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ''
    if not np.isfinite(f):
        return ''
    return ('%.' + str(nd) + 'f') % f


def export_entries():
    """入场明细 + 阶段时效分桶写入聚宽环境（每次覆盖，结束即最终版）。"""
    lines = ['entry_date,code,stage,stage_since,stage_age,open_price,'
             'signal_close,gap_pct,qty,amount,stop_kind,stop_ratio']
    for e in g.entry_stats:
        lines.append('%s,%s,%s,%s,%s,%s,%s,%s,%d,%.2f,%s,%s'
                     % (e['date'], e['code'], e['stage'], e['since'] or '',
                        '' if e['age'] is None else e['age'],
                        _csv_num(e['open'], 3), _csv_num(e['signal_close'], 3),
                        _csv_num(e['gap'], 4), e['qty'], e['amount'],
                        e['stop_kind'], _csv_num(e['stop_ratio'], 4)))
    write_file(ENTRY_FILE, '\n'.join(lines) + '\n')

    buckets = [('1', lambda a: a == 1), ('2-3', lambda a: 2 <= a <= 3),
               ('4-10', lambda a: 4 <= a <= 10), ('>10', lambda a: a > 10),
               ('unknown', lambda a: a is None)]
    out = ['bucket,n,avg_age,min_age,max_age']
    for name, pred in buckets:
        ages = [e['age'] for e in g.entry_stats if pred(e['age'])]
        if not ages:
            out.append('%s,0,,,' % name)
            continue
        out.append('%s,%d,%.1f,%d,%d'
                   % (name, len(ages), float(sum(ages)) / len(ages),
                      min(ages), max(ages)))
    write_file(AGE_FILE, '\n'.join(out) + '\n')


def record_entry(context, od, px, qty):
    sig = od['sig_close_real']
    g.entry_stats.append({
        'date': str(context.current_dt.date()), 'code': od['code'],
        'stage': od['stage'], 'since': od.get('stage_since'),
        'age': od.get('stage_age'), 'open': px, 'signal_close': sig,
        'gap': (px / sig - 1.0) if sig else None,
        'qty': qty, 'amount': px * qty,
        'stop_kind': od['stop_kind'], 'stop_ratio': od['stop_ratio']})
    try:
        export_entries()
    except Exception as exc:
        log.info('诊断文件写入失败 %r' % (exc,))


def scan_entries(context):
    held = held_positions(context)
    unknown = [c for c in held if c not in g.book]
    if unknown:
        log.info('未知持仓暂停新仓: %s' % unknown)
        return
    if len(held) >= MAX_NAMES:
        return
    prev = str(context.previous_date)
    cands = []
    for row in g.pool_rows:
        code = row['code']
        if code in held or code in g.bought_today:
            continue
        info = g.stage_info.get(code)
        sig = g.sig.get(code)
        if not info or not sig or info['stage'] not in ('02', '03', '04'):
            continue
        if len(g.buf.get(code, pd.DataFrame())) < MIN_BUFFER:
            continue
        stage = info['stage']
        if stage == '03':
            if not np.isfinite(sig['e20']) or sig['e20'] <= 0:
                continue
            stop_kind, stop_price = '20EMA', sig['e20']
        else:
            if sig['plat_low'] is None:
                continue
            stop_kind, stop_price = '平台低点', sig['plat_low']
        stop_ratio = stop_price / sig['close']
        if not 0 < stop_ratio < 1:
            continue
        cands.append({'code': code, 'side': 'BUY', 'stage': stage,
                      'hits': info['hits'], 'avg_money': float(row['avg_money']),
                      'reason': '%s(%d条)' % (stage, info['hits']),
                      'no_chase': stage in ('02', '04'),
                      'stop_kind': stop_kind, 'stop_ratio': float(stop_ratio),
                      'stop_price': float(stop_price),
                      'stage_since': g.stage_since.get(code),
                      'stage_age': stage_age_of(code, prev)})
    if not cands:
        return
    log.info('扫描诊断：挂单队列 %d 单%s'
             % (len(g.retry_orders),
                '' if not g.retry_orders else '，首单=%r' % (g.retry_orders[0],)))
    closes = real_prev_closes([o['code'] for o in cands], prev)
    if len(closes) < len(cands):
        log.info('昨收取价覆盖 %d/%d 只候选' % (len(closes), len(cands)))
    cands.sort(key=lambda o: (-o['hits'], -o['avg_money'], o['code']))
    opened = 0
    attempted = 0
    skipped_pending = 0
    skipped_close = 0
    for od in cands:
        code = od['code']
        if len(held_positions(context)) >= MAX_NAMES:
            break
        if any(o['code'] == code for o in g.retry_orders):
            if not skipped_pending:
                log.info('在途命中 %s：队列前3=%r' % (code, g.retry_orders[:3]))
            skipped_pending += 1
            continue
        sig_close = closes.get(code)
        if not sig_close:
            skipped_close += 1
            continue
        attempted += 1
        od['sig_close_real'] = float(sig_close)
        if execute_buy(context, od) == 'defer':
            g.retry_orders.append(od)
            log.info('买入顺延(停牌) %s %s' % (code, od['reason']))
        else:
            opened += 1
    log.info('扫描池%d只 候选%d只 新开%d只 持仓%d只 高开累计%d'
             % (len(g.pool_rows), len(cands), opened,
                len(held_positions(context)), g.chase_skips))
    if attempted < len(cands):
        log.info('候选跳过 %d 只：在途%d 缺昨收%d（尝试下单%d）'
                 % (len(cands) - attempted, skipped_pending,
                    skipped_close, attempted))


def run_cycle(context):
    today = str(context.current_dt.date())
    if g.day != today:
        g.day = today
        g.bought_today = set()
    prev = str(context.previous_date)
    if not g.ready:
        try:
            refresh_pool(context, prev)
            g.ready = True
        except Exception as exc:
            log.info('首次建池失败，跳过 %r' % (exc,))
            return
    elif pool_stale(prev):
        try:
            refresh_pool(context, prev)
        except Exception as exc:
            log.info('建池失败，沿用旧池 %r' % (exc,))
    try:
        fetch_and_advance(prev)
    except Exception as exc:
        log.info('行情更新失败，仅处理挂单与持仓 %r' % (exc,))
    sync_book(context)
    execute_pending(context)
    sync_book(context)
    manage_held(context)
    sync_book(context)
    if g.sold_day == today:
        return                          # 当日有卖出不开新仓（reversal_v1 纪律）
    scan_entries(context)


def initialize(context):
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000300.XSHG')
    set_slippage(FixedSlippage(0.0))    # §7 零滑点
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=COMMISSION,
                             close_commission=COMMISSION, min_commission=0),
                   type='fund')
    g.book = {}
    g.buf = {}
    g.hist_tail = {}
    g.bar_date = {}
    g.sig = {}
    g.stage_info = {}
    g.stage_since = {}
    g.entry_stats = []
    g.pool_rows = []
    g.pool_date = None
    g.index_key_cache = {}          # ETF 代码 -> 跟踪指数键，仅存成功映射
    g.retry_orders = []
    g.bought_today = set()
    g.sold_day = None
    g.chase_skips = 0
    g.day = None
    g.ready = False
    try:
        export_entries()               # 清空上次运行的诊断文件（仅表头）
    except Exception as exc:
        log.info('诊断文件初始化失败 %r' % (exc,))
    run_daily(run_cycle, time='9:31')
    log.info('cycle_pool_jq v8.1 初始化完成（性能重构：numpy列+闸门短路+行视图；'
             '规格删减票：吞没×2/ATR扩张/周线偏离，05判据3/3；'
             '02/04放量、03缩量仍为硬门槛），回测频率须选"每天"；'
             '入场诊断导出 %s / %s'
             % (ENTRY_FILE, AGE_FILE))


def after_trading_end(context):
    sync_book(context)
