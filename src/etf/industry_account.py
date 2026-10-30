"""行业动量的独立模拟账户，使用 ETF 配置参考组合。"""

from src.etf.config import SIM_ACCOUNT as CORE_ACCOUNT
from src.sim_accounts import SimAccount

LEDGER_PATH = "data/trade_ledger_industry_momentum.jsonl"
SIM_ACCOUNT = SimAccount(
    ledger_path=LEDGER_PATH,
    label="行业动量轮动（独立模拟）",
    benchmark=CORE_ACCOUNT.benchmark,
)
