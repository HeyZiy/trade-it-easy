# Repository Guidelines

## Project Structure

- Root Python scripts are the operational entry points. `dashboard.py` serves the investment dashboard, `book.py` records simulated fills, and `etf_rotation.py` (跨资产动量) and `quality_pool.py` (质量动量) run strategies. The 趋势回踩 (pullback) strategy is retired — its entry script no longer exists.
- `src/` contains strategy, account, ledger, and reporting logic (`src/etf/` holds ETF rotation/momentum/rebalancer internals). `data_provider/` handles shared market data access and symbol normalization. Dashboard HTML, CSS, and JavaScript live in `dashboard/`.
- `tests/` holds regressions. `strategy/` documents current strategy rules, `research/studies/` holds experiments, and `research/tools/` holds reusable research code. See `CONTEXT.md` for account and strategy terminology.

## Strategy Research

Read [Research Guidelines](research/README.md) before writing or running a platform backtest, choosing research tools, or interpreting strategy results. Read [strategy/overview.md](strategy/overview.md) for each strategy's positioning, assumptions, and non-mixable boundaries before touching strategy code.

## Local Development

Run commands from the repository root in a Python 3.14 environment. On Windows, use `.\.conda\python.exe` in place of `python` if the project environment is already present.

- `python -m pip install -r requirements.txt pytest` installs runtime dependencies and the test runner.
- `python dashboard.py --port 8765` serves the dashboard on `127.0.0.1`. Set `TRADE_DASHBOARD_PASSWORD` in your shell before launching it.
- `python portfolio_report.py --dry-run` previews the closing account report without saving it.
- `python -m pytest tests/ -ra` runs the full suite and summarizes skips and failures.

## Runtime Configuration

Keep credentials, account ledgers, and active configuration out of Git. Sensitive settings live in `.env` (data source tokens, email, push keys). Use `config/etf_config.example.json` as the configuration reference (`data/etf_config.json` is the live, git-ignored copy) and `deploy/dashboard.md` plus `deploy/crontab.server` for server deployment and scheduling.
