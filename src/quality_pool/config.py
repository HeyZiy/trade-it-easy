# -*- coding: utf-8 -*-
"""质量池策略常量 — 数字唯一来源是 strategy/roe_quality_pool.md，改动须同步该文档。"""

# ── 池与轮转（规格「策略在做什么」「二、怎么选」） ──
TOP_N = 20            # 最多持有 20 只
KEEP_RANK = 30        # 旧仓池内排名 ≤30 保留；21~30 只保护已有持仓，不为新股票开仓
ROTATE_EVERY = 20     # 每 20 个交易日检查一次
SIGNAL_CLOCK = "15:10"   # 本地模拟：T 日盘后生成名单（规格为 T 日 14:55，T 收盘涨跌不参与）
EXEC_CLOCK = "09:31"     # 下一交易日 09:31 按冻结名单先卖后买

# ── 一、股票池筛选阈值 ──
ROE_SINGLE_MIN = 3.0     # 最新单季 ROE × 4 > 12%，即单季 ROE(%) > 3
NP_YOY_MIN = 10.0        # 平台最新季度净利润同比(%) > 10
PE_MAX = 30.0            # PE(TTM) < 30，仅设上界，负值可通过
PB_MAX = 5.0             # PB < 5，仅设上界
VOL_LOOKBACK = 61        # 最近 61 根完整收盘形成 60 个收益
VOL60_MAX = 35.0         # 样本标准差(ddof=1) × √250 × 100 < 35
VOL_ANNUAL_DAYS = 250.0
BAN_WINDOW = 90          # 信号日起未来 90 自然日内任意已知解禁事件即剔除

# ── 二、强弱分数 ──
SCORE_CLOSES = 121       # 需要截止 T−1 的 121 根收盘
SCORE_SKIP_RECENT = 20   # 跳过最近 20 个交易日：R = P(T−21)/P(T−121) − 1

# ── 三、买卖与预算 ──
BUDGET_DIVISOR = TOP_N               # 单只目标市值 = 策略账户总权益 / 20
FEE_COMMISSION = 0.00025             # 比例佣金（资金检查用；台账本身不记费用）
MIN_COMMISSION = 5.0                 # 最低佣金（资金检查用）
SLIPPAGE_SPREAD = 0.002              # 价差参数，买/卖各一半（资金检查用）
LIMIT_PAD = 0.001                    # 元，涨跌停价比较容差

# ── 台账与状态 ──
ACCOUNT = "quality_pool"
# 台账是单池设计（NOMINAL_EQUITY 一本账），规格要求独立策略账户——用独立流水文件，
# 与主台账（core/satellite）互不可见。
POOL_LEDGER_PATH = "data/trade_ledger_quality_pool.jsonl"
STATE_PATH = "data/quality_pool_state.json"
REPORT_PREFIX = "quality_pool"

# 退出原因两类（规格「五、运行与记录」）
EXIT_OUT_OF_POOL = "out_of_pool"          # 附 pool_reason：首个未通过环节
EXIT_RANK_BELOW_BUFFER = "rank_below_buffer"

# 财务可见性：报告期有效且披露日不晚于截止日（T−1）
FINANCIAL_STATEMENT_TYPE_SINGLE = 2   # 合并报表(单季度)
FINANCIAL_STATEMENT_TYPE_CONSOL = 1   # 合并报表
