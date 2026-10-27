"""Original-15 account control: only held positions may remain above score 5.

Uses existing JoinQuant signals/prices; no fresh prices or score reconstruction.
Adjusted-price replay is a diagnostic; the paired platform account is authoritative.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
INITIAL = 100000.


def target_for(day, holding, hold_through_cap=False):
    eligible = day.loc[day['eligible'].eq(1)].sort_values('rank')
    normal = eligible.index[0] if len(eligible) else None
    if hold_through_cap and holding in day.index:
        row = day.loc[holding]
        if (math.isfinite(row['score']) and row['score'] >= 5
                and row['paused'] == 0 and row['decision_price'] > 0):
            return holding
    return normal


def replay(signals, market, *, hold_through_cap=False):
    cash, shares, holding = INITIAL, 0., None
    rows, trades, cap_days = [], 0, 0
    for date, day in signals.groupby('date', sort=True):
        day = day.set_index('code')
        target = target_for(day, holding, hold_through_cap)
        if hold_through_cap and holding is not None and target == holding and day.at[holding, 'score'] >= 5:
            cap_days += 1
        def price(code):
            bar = market.loc[(date, code)]
            return day.at[code, 'decision_price'] * bar['close_post'] / bar['close_raw']
        if holding is not None and holding != target:
            if day.at[holding, 'paused'] == 0 and day.at[holding, 'decision_price'] > 0:
                value = shares * price(holding)
                cash += value - max(5., value * .0002)
                shares, holding = 0., None
                trades += 1
        if holding is None and target is not None:
            if day.at[target, 'paused'] == 0 and day.at[target, 'decision_price'] > 0:
                px = price(target)
                lots = int(cash / px / 100)
                while lots and lots * 100 * px + max(5., lots * 100 * px * .0002) > cash:
                    lots -= 1
                if lots:
                    shares = lots * 100
                    value = shares * px
                    cash -= value + max(5., value * .0002)
                    holding = target
                    trades += 1
        close = market.loc[(date, holding), 'close_post'] if holding is not None else 0.
        value = cash + shares * close
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'Incomplete account mark: {date}, {holding}')
        rows.append(dict(date=date, nav=value, holding=holding, target=target))
    return pd.DataFrame(rows).set_index('date'), dict(trades=trades, cap_hold_days=cap_days)


def metrics(frame):
    nav = frame['nav']
    years = {}
    previous = INITIAL
    for year, group in nav.groupby(nav.index.astype(str).str[:4]):
        years[year] = (group.iloc[-1] / previous - 1) * 100
        previous = group.iloc[-1]
    return dict(final=float(nav.iloc[-1]),
                annualized_pct=float(((nav.iloc[-1] / INITIAL) ** (250 / len(nav)) - 1) * 100),
                max_drawdown_pct=float((nav / nav.cummax() - 1).min() * 100), yearly=years)


def main():
    signals = pd.read_csv(HERE / 'fit_signals.csv', dtype={'code': str})
    market = pd.read_csv(HERE / 'fit_market.csv', dtype={'code': str}).set_index(['date', 'code'])
    if signals.duplicated(['date', 'code']).any() or market.index.has_duplicates:
        raise ValueError('Duplicate source records')
    meta = json.loads((HERE / 'fit_metadata.json').read_text(encoding='utf-8'))
    if not meta.get('export_complete') or meta.get('errors'):
        raise ValueError('Incomplete JoinQuant export')
    paths, summary = {}, {}
    for name, enabled in [('baseline', False), ('hold_cap', True)]:
        path, counts = replay(signals, market, hold_through_cap=enabled)
        paths[name] = path
        summary[name] = dict(**metrics(path), **counts)
    # Compare the unchanged selector with actual platform closing holdings.
    import sys
    if not __package__:
        sys.path.insert(0, str(HERE.parents[2]))
    from research.studies.etf_rotation.analyze_positions import read_positions
    actual = {day['date']: (day['positions'][0]['code'] if day['positions'] else None)
              for day in read_positions(HERE / 'position.csv')}
    base = paths['baseline']
    matches = sum((base.at[d, 'holding'] if pd.notna(base.at[d, 'holding']) else None)
                  == actual[d] for d in base.index)
    if matches != len(base):
        raise ValueError('Unchanged baseline holdings do not match the platform; do not interpret the control')
    summary['calibration'] = dict(days=len(base), holding_matches=matches,
                                  platform_final=2723171.3110342,
                                  replay_final_relative_error=base['nav'].iloc[-1] / 2723171.3110342 - 1)
    summary['different_holding_days'] = int((base['holding'].fillna('cash') != paths['hold_cap']['holding'].fillna('cash')).sum())
    summary['scope'] = ('Same original15, period, signals and fees. Post-adjusted prices imply dividend reinvestment; '
                        '100-share lots in adjusted space differ from raw fund units. No volume limits. '
                        'Diagnostic only; confirm with study_score_cap_jq.py before changing production.')
    out = HERE / 'results/score_cap'
    out.mkdir(parents=True, exist_ok=True)
    for name, frame in paths.items():
        frame.to_csv(out / f'{name}_nav.csv', encoding='utf-8-sig')
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
