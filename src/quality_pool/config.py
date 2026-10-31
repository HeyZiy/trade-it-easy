# -*- coding: utf-8 -*-
"""质量池策略常量 — 数字唯一来源是 strategy/roe_quality_pool.md，改动须同步该文档。"""

from src.account_sources import AccountSource

# ── 池与轮转（规格「策略在做什么」「二、怎么选」） ──
STRATEGY_VERSION = 2
TOP_N = 6             # 现行质量池 + 12-2 强弱；旧仓只按质量池留存
INDUSTRY_CAP = 2      # 申万一级行业，新仓最多补到同业2只
ROTATE_EVERY = 20     # 每 20 个交易日检查一次
RUN_CLOCK = "09:30"      # 单次任务启动；取昨收信号后按计算完成时的实时行情执行

# ── 一、股票池筛选阈值 ──
ROE_TTM_MIN = 12.0       # TTM归母净利润 / 最新可见期末归母权益 × 100
NP_YOY_MIN = 10.0        # 平台最新季度净利润同比(%) > 10
PE_MAX = 30.0            # 0 < PE(TTM) < 30
PB_MAX = 5.0             # 0 < PB < 5
VOL_LOOKBACK = 61        # 最近 61 根完整收盘形成 60 个收益
VOL60_MAX = 35.0         # 样本标准差(ddof=1) × √250 × 100 < 35
VOL_ANNUAL_DAYS = 250.0

# ── 二、强弱分数 ──
SCORE_CLOSES = 253       # 上一交易日及之前253根收盘
SCORE_SKIP_RECENT = 21   # 跳过最近21个交易日，计分窗口跨231个交易日

# ── 三、买卖与预算 ──
BUDGET_DIVISOR = TOP_N               # 单只目标市值 = 策略账户总权益 / 6
FEE_COMMISSION = 0.00025             # 比例佣金（资金检查用；台账本身不记费用）
MIN_COMMISSION = 5.0                 # 最低佣金（资金检查用）
SLIPPAGE_SPREAD = 0.002              # 价差参数，买/卖各一半（资金检查用）
LIMIT_PAD = 0.001                    # 元，涨跌停价比较容差

# ── 台账与状态 ──
ACCOUNT = "quality_pool"  # 独立策略流水隔离资金池
# 台账是单池设计（NOMINAL_EQUITY 一本账），规格要求独立策略账户——用独立流水文件，
# 与主台账（core/satellite）互不可见。
POOL_LEDGER_PATH = "data/trade_ledger_quality_pool_v2.jsonl"
STATE_PATH = "data/quality_pool_v2_state.json"
REPORT_PREFIX = "quality_pool"

# 新退出只有质量出池；旧名次退出不再产生。
EXIT_OUT_OF_POOL = "out_of_pool"          # 附 pool_reason：全部已确认失败条件

# 财务可见性：报告期有效且披露日不晚于截止日（T−1）
FINANCIAL_STATEMENT_TYPE_SINGLE = 2   # 合并报表(单季度)
FINANCIAL_STATEMENT_TYPE_CONSOL = 1   # 合并报表

# ── 模拟账户描述符（收盘成绩等消费方经 src/account_sources 枚举）──

ACCOUNT_SOURCE = AccountSource(
    ledger_path=POOL_LEDGER_PATH, label="质量池轮动",
    benchmark={"name": "中证800价格指数", "index": "000906"})
