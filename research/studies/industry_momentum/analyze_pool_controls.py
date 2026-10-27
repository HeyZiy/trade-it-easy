"""核对聚宽六账户导出，汇总净收益、回撤、年度表现与实际决策。

从仓库根运行：python research/studies/industry_momentum/analyze_pool_controls.py
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
INITIAL = 100000.0
LABELS = [f'{size}{mode}' for size in (15, 37) for mode in ('等权', '合格持有', '排名')]


def pool_definitions():
    tree = ast.parse((HERE / 'study_pool_controls_jq.py').read_text(encoding='utf-8'))
    pools = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ('ORIGINAL15', 'INDUSTRY22'):
                pools[name] = ast.literal_eval(node.value)
    return pools['ORIGINAL15'], pools['ORIGINAL15'] + pools['INDUSTRY22']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(nav, signals, decisions, original15, mixed37):
    require(set(nav.account) == set(LABELS), '账户集合不完整')
    require(not nav.duplicated(['date', 'account']).any(), '净值日期/账户重复')
    require(not signals.duplicated(['date', 'code']).any(), '信号日期/代码重复')
    require(not decisions.duplicated(['date', 'account']).any(), '决策日期/账户重复')
    dates = sorted(nav.date.unique())
    require(set(signals.date) == set(dates), '行情信号与净值日期不一致')
    require(not set(decisions.account).difference(LABELS), '未知决策账户')
    require(not set(decisions.date).difference(dates), '决策存在净值之外的日期')
    require(set(signals.code) == set(mixed37), '37只候选不完整')
    require(signals.groupby('date').size().eq(len(mixed37)).all(), '每日候选不完整')
    errors = {}
    for label in LABELS:
        group = nav[nav.account == label].sort_values('date')
        require(group.date.tolist() == dates, f'{label}日期不完整')
        numeric = group[['nav', 'cash', 'invested', 'return_pct', 'drawdown_pct']]
        require(np.isfinite(numeric.to_numpy()).all(), f'{label}净值存在缺失或非有限数')
        require(group.nav.gt(0).all(), f'{label}净值非正')
        values = group.nav.to_numpy()
        peak = np.maximum.accumulate(np.r_[INITIAL, values])[1:]
        balance_error = float((group.cash + group.invested - group.nav).abs().max())
        return_error = float((group.return_pct - (group.nav / INITIAL - 1) * 100).abs().max())
        dd_error = float((group.drawdown_pct - (values / peak - 1) * 100).abs().max())
        require(max(balance_error, return_error, dd_error) < 1e-6, f'{label}资金或收益未对平')
        errors[label] = dict(balance_error_yuan=balance_error, return_error_pp=return_error,
                             drawdown_error_pp=dd_error)
        actual = decisions[decisions.account == label].sort_values('date')
        expected = (group.groupby(group.date.str[:7], sort=False).date.first().tolist()
                    if label.endswith('等权') else dates)
        require(actual.date.tolist() == expected, f'{label}决策日期不完整')

    # 对全区间逐日复核真实评分排序，保留原始成员顺序处理同分。
    ranks = {}
    for day, group in signals.groupby('date', sort=False):
        by_code = group.set_index('code')
        for size, pool in ((15, original15), (37, mixed37)):
            eligible = [code for code in pool if by_code.at[code, 'status'] == 'eligible']
            require(all(np.isfinite(by_code.at[code, 'score']) and
                        0 < by_code.at[code, 'score'] < 5 for code in eligible), '合格分数无效')
            ranked = sorted(eligible, key=lambda code: by_code.at[code, 'score'], reverse=True)
            expected = {code: i + 1 for i, code in enumerate(ranked)}
            for code in mixed37:
                value = by_code.at[code, f'rank{size}']
                require((code not in expected and pd.isna(value)) or
                        (code in expected and value == expected[code]), f'{day}排名不一致')
            ranks[(day, size)] = ranked

    observations = signals.set_index(['date', 'code'])
    for row in decisions.itertuples(index=False):
        if row.account.endswith('等权'):
            continue
        ranked = ranks[(row.date, int(row.account[:2]))]
        old = row.holdings_before.split('|')[0] if row.holdings_before else None
        target = old if row.account.endswith('合格持有') and old in ranked else (
            ranked[0] if ranked else '')
        require(row.target == target, f'{row.date} {row.account}目标不符合规则')
        reason = ('keep' if old else 'no_candidate') if old == (target or None) else (
            ('rank_replaced' if old in ranked else observations.at[(row.date, old), 'status'])
            if old else 'entry')
        require(row.reason == reason, f'{row.date} {row.account}退出原因不一致')
    return dates, errors


def summarize_account(group, decisions):
    group = group.sort_values('date').reset_index(drop=True)
    values = group.nav.to_numpy()
    peaks = np.maximum.accumulate(np.r_[INITIAL, values])[1:]
    drawdowns = values / peaks - 1
    trough = int(np.argmin(drawdowns))
    peak_candidates = np.flatnonzero(values[:trough + 1] == peaks[trough])
    peak_date = group.date.iloc[int(peak_candidates[-1])] if len(peak_candidates) else 'initial_capital'
    recovery = group.iloc[trough + 1:]
    recovery = recovery[recovery.nav >= peaks[trough]]
    years = []
    previous = INITIAL
    for year, rows in group.groupby(group.date.str[:4], sort=True):
        year_values = rows.nav.to_numpy()
        year_peaks = np.maximum.accumulate(np.r_[previous, year_values])[1:]
        end = float(year_values[-1])
        years.append(dict(account=group.account.iloc[0], year=int(year),
                          first_date=rows.date.iloc[0], last_date=rows.date.iloc[-1],
                          start_nav=previous, end_nav=end, profit_yuan=end-previous,
                          return_pct=(end/previous-1)*100,
                          max_drawdown_pct=float((1-year_values/year_peaks).max()*100)))
        previous = end
    underwater, longest = 0, 0
    for value, peak in zip(values, peaks):
        underwater = underwater + 1 if value < peak else 0
        longest = max(longest, underwater)
    count = len(group)
    changed = group.holdings.ne(group.holdings.shift(fill_value=''))
    summary = dict(account=group.account.iloc[0], days=count,
                   start_nav=INITIAL, ending_nav=float(values[-1]),
                   profit_yuan=float(values[-1]-INITIAL),
                   return_pct=float((values[-1]/INITIAL-1)*100),
                   annualized_250_pct=float(((values[-1]/INITIAL)**(250/count)-1)*100),
                   max_drawdown_pct=float(-drawdowns[trough]*100), peak_date=peak_date,
                   trough_date=group.date.iloc[trough],
                   recovery_date=None if recovery.empty else recovery.date.iloc[0],
                   longest_underwater_trading_days=longest,
                   average_exposure_pct=float((group.invested/group.nav).mean()*100),
                   negative_cash_days=int(group.cash.lt(0).sum()),
                   multiple_holding_days=int(group.holdings.str.contains('|', regex=False).sum()),
                   actual_holding_set_changes=int(changed.sum()),
                   decision_reason_counts={str(k): int(v) for k, v in decisions.reason.value_counts().items()})
    return summary, years


def analyze(directory):
    nav = pd.read_csv(directory / 'pool_control_nav.csv', keep_default_na=False)
    signals = pd.read_csv(directory / 'pool_control_signals.csv')
    decisions = pd.read_csv(directory / 'pool_control_decisions.csv', keep_default_na=False)
    original15, mixed37 = pool_definitions()
    dates, errors = verify(nav, signals, decisions, original15, mixed37)
    accounts, annual = [], []
    for label in LABELS:
        summary, years = summarize_account(nav[nav.account == label], decisions[decisions.account == label])
        accounts.append(summary)
        annual.extend(years)
    yearly = pd.DataFrame(annual)
    account_map = {row['account']: row for row in accounts}
    comparisons = {}
    for size in (15, 37):
        rank = yearly[yearly.account == f'{size}排名'].set_index('year')
        hold = yearly[yearly.account == f'{size}合格持有'].set_index('year')
        equal = yearly[yearly.account == f'{size}等权'].set_index('year')
        comparisons[str(size)] = dict(
            rank_beats_hold_years=int((rank.return_pct > hold.return_pct).sum()),
            rank_beats_equal_years=int((rank.return_pct > equal.return_pct).sum()),
            years=len(rank),
            rank_vs_hold_terminal_ratio=account_map[f'{size}排名']['ending_nav']/account_map[f'{size}合格持有']['ending_nav'])
    old_file = directory / 'results/mixed37_comparison/account_comparison.json'
    replication = {}
    if old_file.exists():
        old = json.loads(old_file.read_text(encoding='utf-8'))
        for label, key in (('15排名', 'original15'), ('37排名', 'mixed37')):
            delta = account_map[label]['ending_nav']-old[key]['ending_nav']
            replication[label] = dict(ending_nav_difference_yuan=delta, within_one_cent=abs(delta)<.01)
            require(abs(delta) < .01, f'{label}未复现既有期末净值，需先检查执行差异')
    return dict(first_date=dates[0], last_date=dates[-1], trading_days=len(dates),
                nav_rows=len(nav), signal_rows=len(signals), decision_rows=len(decisions),
                accounts=accounts, comparisons=comparisons, validation=errors, replication=replication,
                method=dict(initial_per_account=INITIAL, annualization='250 / observed trading days',
                            drawdown='Daily account NAV including initial capital and all account fees',
                            annual_return='Previous year last NAV anchors the next year, including its first day',
                            limits=['Same historical sample; no out-of-sample conclusion',
                                    'Equal-weight and single-holding accounts have different risk and concentration',
                                    'Decision reasons describe attempted changes, not verified fills',
                                    'Cash and invested values reconcile NAV; no order-level fee audit',
                                    'Annual 2026 covers only January through September'])), yearly


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', type=Path, default=HERE)
    args = parser.parse_args()
    summary, annual = analyze(args.directory)
    output = args.directory / 'results/pool_controls'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    pd.DataFrame(summary['accounts']).drop(columns='decision_reason_counts').to_csv(output / 'accounts.csv', index=False)
    annual.to_csv(output / 'annual.csv', index=False)
    print(pd.DataFrame(summary['accounts'])[['account', 'return_pct', 'annualized_250_pct', 'max_drawdown_pct']].to_string(index=False))
    print(f'PASS: {summary["trading_days"]} days, six accounts; cash, NAV, signals and decisions verified')


if __name__ == '__main__':
    main()
