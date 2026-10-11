"""只读4.1实际排名退出，补充卖后路径风险；不是取消退出的新规则回测。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from analyze_stop_loss import CACHE, Prices, names, read_version, require, sha

STUDY = HERE.parent
OUT = STUDY / 'reports' / 'rank_exit_risk_v4_1_2026-10-10'
HORIZONS = (20, 40, 60)


def window_metrics(values):
    """同一完整收盘路径同时统计终点、相对起点最深跌幅、峰谷回撤。"""
    values = pd.Series(values, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all() or not values.gt(0).all():
        return None
    relative = values / values.iloc[0]
    return dict(return_=float(relative.iloc[-1] - 1),
                mae=float(min(0, relative.min() - 1)),
                max_drawdown=float((values / values.cummax() - 1).min()))


def main():
    frames, hashes = read_version('v4_1', ' (1)')
    nav = frames['nav']
    calendar = pd.DatetimeIndex(nav.date)
    account = pd.Series(nav.equity.to_numpy(), index=calendar)
    prices = Prices(calendar)
    label = names()
    exits = frames['holding_periods'].query("exit_reason == 'rank_below_buffer'").copy()
    require(not exits.empty, '没有实际排名退出')
    rows = []
    for item in exits.itertuples():
        end = calendar.get_loc(item.exit_date)
        pair = prices.get(item.code)
        adjusted = pair[1].reindex(calendar) if pair is not None else None
        for horizon in HORIZONS:
            row = dict(code=item.code, name=label.get(item.code, item.code),
                       entry_date=item.entry_date, exit_date=item.exit_date,
                       horizon=horizon)
            if end + horizon >= len(calendar):
                row['status'] = 'end_of_backtest'
            elif adjusted is None:
                row['status'] = 'missing_cache'
            else:
                stock = window_metrics(adjusted.iloc[end:end + horizon + 1])
                portfolio = window_metrics(account.iloc[end:end + horizon + 1])
                require(portfolio is not None, '账户净值窗口无效')
                if stock is None:
                    row['status'] = 'missing_close'
                else:
                    row.update(status='ok', end_date=calendar[end + horizon],
                               **{f'stock_{k}': v for k, v in stock.items()},
                               **{f'portfolio_{k}': v for k, v in portfolio.items()})
            rows.append(row)
    cases = pd.DataFrame(rows)
    summaries = []
    for horizon, frame in cases.groupby('horizon', sort=True):
        valid = frame[frame.status.eq('ok')]
        require(not valid.empty, '无可观察卖后窗口')
        summaries.append(dict(horizon=int(horizon), total=len(frame), observed=len(valid),
            missing_or_censored=len(frame) - len(valid),
            median_return=float(valid.stock_return_.median()),
            positive_count=int(valid.stock_return_.gt(0).sum()),
            median_mae=float(valid.stock_mae.median()),
            mae15_count=int(valid.stock_mae.le(-.15).sum()),
            recovered_mae15_count=int((valid.stock_mae.le(-.15) & valid.stock_return_.gt(0)).sum()),
            median_max_drawdown=float(valid.stock_max_drawdown.median()),
            drawdown15_count=int(valid.stock_max_drawdown.le(-.15).sum()),
            median_portfolio_max_drawdown=float(valid.portfolio_max_drawdown.median()),
            deeper_than_portfolio_count=int(valid.stock_max_drawdown.lt(valid.portfolio_max_drawdown).sum()),
            beat_portfolio_count=int(valid.stock_return_.gt(valid.portfolio_return_).sum()),
            up10_count=int(valid.stock_return_.ge(.1).sum())))
    summary = pd.DataFrame(summaries)
    OUT.mkdir(parents=True, exist_ok=True)
    cases.to_csv(OUT / 'cases.csv', index=False, encoding='utf-8-sig')
    summary.to_csv(OUT / 'summary.csv', index=False, encoding='utf-8-sig')
    # 核对全部输入文件保持原样；不访问网络、不填充价格。
    for filename, digest in hashes.items():
        require(sha(STUDY / filename) == digest, f'源CSV发生变化：{filename}')
    for filename, digest in prices.hashes.items():
        require(sha(CACHE / filename) == digest, f'缓存发生变化：{filename}')
    metadata = dict(actual_rank_exits=len(exits), end=str(calendar[-1].date()),
                    source_sha256=hashes, cache_sha256=prices.hashes,
                    methods='卖出日收盘为起点；20/40/60交易日完整HFQ收盘路径；不跨回测末日；'
                    '缺失价格留空；最深跌幅相对卖出日，最大回撤相对窗口内历史峰值；'
                    '账户净值仅为辅助对照，不能当成卖出资金的实际替补收益；'
                    '观察窗口可含原策略再入场，不模拟删除退出规则')
    (OUT / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 4.1排名退出：卖后收益与路径风险', '',
        f'实际跌出9名留存线清仓{len(exits)}次。只分析该组，不混入质量池退出或等权减仓。', '',
        '| 卖后期限 | 有效/总数 | 终点收益中位数 | 相对卖出日最深跌幅中位数 | 曾跌15%次数 | 峰谷回撤中位数 | 峰谷回撤15%次数 | 最后上涨次数 |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for x in summary.itertuples():
        lines.append(f'| {x.horizon}日 | {x.observed}/{x.total} | {x.median_return:.2%} | '
                     f'{x.median_mae:.2%} | {x.mae15_count} | {x.median_max_drawdown:.2%} | '
                     f'{x.drawdown15_count} | {x.positive_count} |')
    lines += ['', '相对卖出日最深跌幅回答“继续拿，相对卖出日最多会跌多少”；峰谷回撤还包含先涨后跌。'
        '卖出日收盘起算，不包含当日成交到收盘的价格变化，也不代表盘中极值或含现金分红的完整持仓收益。', '',
        '## 与原账户的辅助对照', '',
        '| 期限 | 卖出股票回撤更深次数 | 股票回撤中位数 | 账户回撤中位数 | 股票终点胜过账户次数 | 曾跌15%但最终上涨次数 |',
        '| --- | --- | --- | --- | --- | --- |']
    for x in summary.itertuples():
        lines.append(f'| {x.horizon}日 | {x.deeper_than_portfolio_count}/{x.observed} | '
                     f'{x.median_max_drawdown:.2%} | {x.median_portfolio_max_drawdown:.2%} | '
                     f'{x.beat_portfolio_count}/{x.observed} | {x.recovered_mae15_count} |')
    lines += ['', '个股与分散账户风险不同，这一比较不能证明排名退出降低账户回撤；'
        '账户收益含其余持仓与现金，不能当成卖出资金的实际替补收益。窗口内重复事件、同股票与市场冲击相关，未做独立样本显著性检验。', '',
        '## 能回答与仍不能回答', '',
        '可以回答：排名退出后，旧股票是否继续下跌、最深跌幅与峰谷回撤，以及错过上涨的频率。', '',
        '账户层面的结论仍需同口径聚宽对照：保留20日周期、质量池退出及其余规则，'
        '只取消排名退出，旧仓占用名额且继续受质量池规则约束；比较账户净值回撤与收益。'
        '这项规则变化可使旧仓长期占位，应完整披露实际组合，并且不能把本报告的固定窗口当成新策略绩效。', '',
        '源CSV及行情缓存未改；逐例见cases.csv，缺失与截尾状态保留。']
    (OUT / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(summary.to_string(index=False))
    print('Report:', OUT / 'report.md')


if __name__ == '__main__':
    main()
