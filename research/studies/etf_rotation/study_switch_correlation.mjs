// Diagnose actual single-position switches; this does not simulate a filtered account.
// node research/studies/etf_rotation/study_switch_correlation.mjs
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { snapshots, reconstructMixed } from './study_entry_volatility.mjs';
import { readCsv } from './study_etf_fit_forward.mjs';
import { quantile } from './study_etf_fit.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const sum = xs => xs.reduce((a, b) => a + b, 0);
const mean = xs => xs.length ? sum(xs) / xs.length : null;
const num = x => x !== '' && x != null && Number.isFinite(Number(x)) ? Number(x) : null;
const horizons = [5, 10, 20], lookbacks = [120, 60];
const group = (code, originals) => originals.has(code) ? 'original15' : 'industry22';
const band = rho => rho >= .8 ? 'at_least_0.8' : rho >= .5 ? '0.5_to_0.8' : 'below_0.5';

function valid(row) {
  return row && num(row.close) > 0 && num(row.event_close) > 0 && num(row.volume) > 0
    && (row.paused === '' || num(row.paused) === 0) && (!row.quality || row.quality === 'ok');
}

export function pearson(a, b) {
  if (a.length !== b.length || a.length < 3 || [...a, ...b].some(x => !Number.isFinite(x))) return null;
  const ma = mean(a), mb = mean(b);
  const va = sum(a.map(x => (x - ma) ** 2)), vb = sum(b.map(x => (x - mb) ** 2));
  if (va < 1e-20 || vb < 1e-20) return null;
  return Math.max(-1, Math.min(1, sum(a.map((x, i) => (x - ma) * (b[i] - mb))) / Math.sqrt(va * vb)));
}

export function correlationBefore(calendar, market, oldCode, newCode, entry, lookback) {
  const at = calendar.indexOf(entry);
  if (at < lookback + 1) return { correlation_status: 'insufficient_history', rho: null };
  const dates = calendar.slice(at - lookback - 1, at);
  const old = dates.map(date => market.get(`${date}|${oldCode}`));
  const next = dates.map(date => market.get(`${date}|${newCode}`));
  if (![...old, ...next].every(valid)) return { correlation_status: 'missing_or_inactive_history', rho: null };
  const returns = bars => bars.slice(1).map((row, i) => Math.log(Number(row.event_close) / Number(bars[i].event_close)));
  const rho = pearson(returns(old), returns(next));
  return { correlation_status: rho === null ? 'constant_or_invalid_returns' : 'complete', rho,
    correlation_band: rho === null ? null : band(rho),
    history_start: dates[0], history_end: dates.at(-1) };
}

export function forwardPair(calendar, market, oldCode, newCode, entry, horizon, commission = .0002) {
  const at = calendar.indexOf(entry);
  const empty = status => ({ forward_status: status, old_return: null, new_return: null,
    gross_advantage: null, after_switch_cost_advantage: null, relative_mae: null });
  if (at < 0 || at + horizon >= calendar.length) return empty('right_censored');
  const dates = calendar.slice(at, at + horizon + 1);
  const old = dates.map(date => market.get(`${date}|${oldCode}`));
  const next = dates.map(date => market.get(`${date}|${newCode}`));
  if (![...old, ...next].every(valid)) return empty('missing_or_inactive_path');
  const a = old.map(x => Number(x.event_close) / Number(old[0].event_close));
  const b = next.map(x => Number(x.event_close) / Number(next[0].event_close));
  // Start with one unit of incumbent value: sell, then fund purchase plus buy fee.
  const investable = (1 - commission) / (1 + commission);
  return { forward_status: 'complete', old_return: a.at(-1) - 1, new_return: b.at(-1) - 1,
    gross_advantage: b.at(-1) - a.at(-1),
    after_switch_cost_advantage: investable * b.at(-1) - a.at(-1),
    relative_mae: Math.min(0, ...b.map((value, i) => investable * value / a[i] - 1)) };
}

export function reentryWithin(days, at, oldCode, horizon) {
  if (at + horizon >= days.length) return null;
  return days.slice(at + 1, at + horizon + 1).some(day => day.positions.some(p => p.code === oldCode));
}

function csv(rows) {
  if (!rows.length) return '';
  const keys = [...new Set(rows.flatMap(Object.keys))];
  const escape = x => x == null ? '' : /[",\r\n]/.test(String(x)) ? `"${String(x).replaceAll('"', '""')}"` : String(x);
  return '\uFEFF' + [keys.join(','), ...rows.map(row => keys.map(key => escape(row[key])).join(','))].join('\n') + '\n';
}

function summarize(events, forwards) {
  const returned = events.filter(x => x.reentry5 !== null), returned10 = events.filter(x => x.reentry10 !== null);
  const actual = events.filter(x => x.new_segment_net_return !== null);
  const result = { switches: events.length, median_correlation: quantile(events.map(x => x.rho), .5),
    reentry5_n: returned.length, reentry5_rate: mean(returned.map(x => +x.reentry5)),
    reentry10_n: returned10.length, reentry10_rate: mean(returned10.map(x => +x.reentry10)),
    actual_complete_segments: actual.length, actual_mean_new_segment_return: mean(actual.map(x => x.new_segment_net_return)),
    actual_quick_loss_3d_rate: mean(actual.map(x => +(x.new_holding_days <= 3 && x.new_segment_net_return < 0))) };
  for (const h of horizons) {
    const rows = forwards.filter(x => x.horizon === h && x.forward_status === 'complete');
    result[`n${h}`] = rows.length;
    result[`gross_advantage${h}`] = mean(rows.map(x => x.gross_advantage));
    result[`advantage${h}`] = mean(rows.map(x => x.after_switch_cost_advantage));
    result[`median_advantage${h}`] = quantile(rows.map(x => x.after_switch_cost_advantage), .5);
    result[`outperform_rate${h}`] = mean(rows.map(x => +(x.after_switch_cost_advantage > 0)));
    result[`relative_mae${h}`] = mean(rows.map(x => x.relative_mae));
  }
  return result;
}

function grouped(events, forwards, keys) {
  const groups = new Map();
  for (const event of events) {
    const key = keys.map(k => event[k]).join('|');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(event);
  }
  return [...groups.values()].map(items => {
    const ids = new Set(items.map(x => x.id));
    return { ...Object.fromEntries(keys.map(k => [k, items[0][k]])),
      ...summarize(items, forwards.filter(x => ids.has(x.id))) };
  });
}

function contrast(rows, horizon) {
  const xs = rows.filter(x => x.horizon === horizon && x.forward_status === 'complete');
  const high = xs.filter(x => x.rho >= .8), rest = xs.filter(x => x.rho < .8);
  return { high_n: high.length, other_n: rest.length,
    high_advantage: mean(high.map(x => x.after_switch_cost_advantage)),
    other_advantage: mean(rest.map(x => x.after_switch_cost_advantage)),
    high_minus_other: high.length && rest.length ? mean(high.map(x => x.after_switch_cost_advantage))
      - mean(rest.map(x => x.after_switch_cost_advantage)) : null };
}

function robustness(rows, horizon) {
  const strata = key => [...new Set(rows.map(x => x[key]))].sort().map(value => ({ [key]: value,
    ...contrast(rows.filter(x => x[key] === value), horizon) }));
  const byYear = strata('year'), byType = strata('switch_type');
  const supportedYears = byYear.filter(x => x.high_n >= 5 && x.other_n >= 5);
  const leaveOut = key => [...new Set(rows.map(x => x[key]))].sort().map(value => ({ excluded: value,
    ...contrast(rows.filter(x => x[key] !== value), horizon) }));
  return { horizon, ...contrast(rows, horizon), by_year: byYear, by_switch_type: byType,
    supported_years: supportedYears.length,
    high_better_years: supportedYears.filter(x => x.high_minus_other > 0).length,
    equal_weight_year_difference: mean(supportedYears.map(x => x.high_minus_other)),
    leave_one_year_out: leaveOut('year'), leave_one_pair_out: leaveOut('pair') };
}

function partialExitReason(signal) {
  if (!signal) return 'unknown_industry_or_missing_signal';
  if (num(signal.eligible) === 1) return 'still_eligible_rank_switch';
  const score = num(signal.score);
  if (score === null) return 'invalid_score';
  if (score >= 5) return 'score_at_least_5';
  if (score <= 0) return 'score_nonpositive_or_drop_filter';
  return 'other_ineligible';
}

function poolSnapshots(calendar, market, codes, originals) {
  const quarters = new Map();
  for (const date of calendar.filter(x => x >= '2016-01-01')) {
    const key = `${date.slice(0, 4)}Q${Math.ceil(Number(date.slice(5, 7)) / 3)}`;
    quarters.set(key, date);
  }
  const rows = [];
  const domestic = new Set(['510300', '159915', '515180', '512100', '588000']);
  for (const [quarter, date] of quarters) {
    const pairs = [];
    for (let i = 0; i < codes.length; i++) for (let j = i + 1; j < codes.length; j++) {
      const a = codes[i], b = codes[j];
      const x = correlationBefore(calendar, market, a, b, date, 120);
      if (x.correlation_status !== 'complete') continue;
      const g = originals.has(a) === originals.has(b) ? (originals.has(a) ? 'original15' : 'industry22') : 'cross_pool';
      pairs.push({ pool: g, a, b, rho: x.rho });
    }
    for (const pool of ['original15', 'industry22', 'cross_pool', 'original_domestic5']) {
      const selected = pool === 'original_domestic5'
        ? pairs.filter(x => domestic.has(x.a) && domestic.has(x.b)) : pairs.filter(x => x.pool === pool);
      rows.push({ quarter, as_of: date, pool, valid_pairs: selected.length,
        median_correlation: quantile(selected.map(x => x.rho), .5),
        high_correlation_fraction: mean(selected.map(x => +(x.rho >= .8))) });
    }
  }
  return rows;
}

export function run(output = path.join(here, 'results/switch_correlation')) {
  const cache = path.join(here, '_cache/pool37_event_adjusted_20260930');
  const manifestFile = path.join(cache, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
  if (manifest.usable_for_returns !== true || manifest.verified_codes.length !== 37
      || Object.keys(manifest.failures).length) throw new Error('Unverified adjusted data');
  const marketFile = path.join(cache, 'market.csv'), marketRows = readCsv(marketFile);
  const calendar = [...new Set(marketRows.map(x => x.date))].sort(), market = new Map();
  for (const row of marketRows) {
    const key = `${row.date}|${row.code.slice(0, 6)}`;
    if (market.has(key)) throw new Error('Duplicate market observation');
    market.set(key, row);
  }
  const metadata = JSON.parse(fs.readFileSync(path.join(here, 'fit_metadata.json'), 'utf8'));
  if (metadata.export_complete !== true || metadata.errors?.length) throw new Error('Incomplete original signal export');
  const originals = new Set(metadata.codes.map(x => x.slice(0, 6)));
  if (originals.size !== 15) throw new Error('Original reference universe changed');
  const signalFile = path.join(here, 'fit_signals.csv');
  const signals = new Map();
  for (const row of readCsv(signalFile)) {
    const key = `${row.date}|${row.code.slice(0, 6)}`;
    if (signals.has(key)) throw new Error('Duplicate original signal');
    signals.set(key, row);
  }
  const allEvents = [], forwards = [], boundaryCounts = {}, reconciliations = {};
  for (const [account, filename] of [['mixed37', 'position_mixed37.csv'], ['original15', 'position.csv']]) {
    const days = snapshots(path.join(here, filename)), rebuilt = reconstructMixed(days);
    if (days.some(day => !calendar.includes(day.date))) throw new Error('Account date missing from market calendar');
    reconciliations[account] = rebuilt.checks;
    const segments = new Map(rebuilt.rows.map(x => [`${x.entry}|${x.code}`, x]));
    let multi = 0, cashEntries = 0, cashExits = 0, switches = 0;
    for (let at = 1; at < days.length; at++) {
      const prior = days[at - 1].positions, current = days[at].positions;
      if (prior.length > 1 || current.length > 1) { multi++; continue; }
      if (!prior.length && current.length) { cashEntries++; continue; }
      if (prior.length && !current.length) { cashExits++; continue; }
      if (!prior.length || !current.length || prior[0].code === current[0].code) continue;
      switches++;
      const old = prior[0], next = current[0], date = days[at].date;
      const oldSignal = signals.get(`${date}|${old.code}`);
      const segment = segments.get(`${date}|${next.code}`);
      if (!segment) throw new Error('Missing incoming actual segment');
      const oldGroup = group(old.code, originals), newGroup = group(next.code, originals);
      for (const lookback of lookbacks) {
        const id = `${account}|${date}|${lookback}`;
        const event = { id, account, date, year: date.slice(0, 4), lookback,
          old_code: old.code, old_name: old.name, new_code: next.code, new_name: next.name,
          switch_type: `${oldGroup}->${newGroup}`, pair: [old.code, next.code].sort().join('|'),
          reentry5: reentryWithin(days, at, old.code, 5), reentry10: reentryWithin(days, at, old.code, 10),
          partial_exit_reason: partialExitReason(oldSignal),
          old_score_export: num(oldSignal?.score), old_eligible_export: num(oldSignal?.eligible),
          new_segment_net_return: segment.closed && segment.complete_pnl ? segment.net_pnl / segment.entry_notional : null,
          new_holding_days: segment.holding_days,
          ...correlationBefore(calendar, market, old.code, next.code, date, lookback) };
        allEvents.push(event);
        if (event.correlation_status !== 'complete') continue;
        for (const horizon of horizons) forwards.push({ ...event, horizon,
          ...forwardPair(calendar, market, old.code, next.code, date, horizon) });
      }
    }
    boundaryCounts[account] = { single_to_single_switches: switches, excluded_multi_boundary_days: multi,
      cash_entries_after_first_day: cashEntries, cash_exits: cashExits };
  }
  const events = allEvents.filter(x => x.correlation_status === 'complete');
  const summary = grouped(events, forwards, ['account', 'lookback', 'correlation_band']);
  const byType = grouped(events, forwards, ['account', 'lookback', 'switch_type', 'correlation_band']);
  const byYear = grouped(events, forwards, ['account', 'lookback', 'year', 'correlation_band']);
  const byPair = grouped(events, forwards, ['account', 'lookback', 'pair', 'correlation_band']);
  const exitReasons = grouped(events, forwards, ['account', 'lookback', 'partial_exit_reason', 'correlation_band']);
  const robust = {};
  for (const account of ['mixed37', 'original15']) for (const lookback of lookbacks) {
    const rows = forwards.filter(x => x.account === account && x.lookback === lookback);
    robust[`${account}|${lookback}`] = horizons.map(h => robustness(rows, h));
  }
  const structures = poolSnapshots(calendar, market, [...new Set(marketRows.map(x => x.code.slice(0, 6)))].sort(), originals);
  const sources = [marketFile, manifestFile, signalFile, path.join(here, 'fit_metadata.json'),
    path.join(here, 'position.csv'), path.join(here, 'position_mixed37.csv')];
  const result = { method: { primary_lookback: 120, sensitivity_lookback: 60, horizons,
    correlation: 'Pearson of aligned adjusted daily log returns ending strictly before switch; exact 121/61 exchange closes, no fill or skipping',
    correlation_bands: { below_0_5: 'rho < .5', middle: '.5 <= rho < .8', high: 'rho >= .8' },
    return_reference: 'Same switch-day close to fixed future close, ignoring actual exits; compares new vs old, not original-pool rank1',
    cost: 'Sell and buy at 0.02% each; new terminal value scaled by .9998/1.0002; no end liquidation, no spread/slippage; price-path reference, not actual fills',
    reentry: 'Old ETF appears in any actual closing holding within next 5/10 account trading days, potentially via other assets/cash; right censoring is unknown',
    partial_exit_reasons: 'Exact exported signals only for original15; industry exits unknown; zero score combines flat/nonpositive momentum and drop filter',
    sources: sources.map(file => ({ file: path.relative(here, file), sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') })),
    limits: ['Selected historical switches only; high vs low correlations have different assets, dates and scores',
      'Overlapping windows and repeated pairs; no independence/significance or causal ETF-filter effect',
      'Closing snapshots miss same-day round trips and cannot verify exact 10:40 exits or prices',
      'Cash boundaries and multi-position boundaries excluded from switch comparison',
      'Added22 adjusted history verified against SDK only in its permitted year; earlier events from public fund records',
      '60 and 120 days are preset diagnostics, not optimized parameters; no pool deletion or trading changes'] },
    boundary_counts: boundaryCounts, account_reconciliations: reconciliations,
    all_switch_exit_reasons: countsBy(allEvents.filter(x => x.lookback === 120), ['account', 'partial_exit_reason']),
    correlation_coverage: countsBy(allEvents, ['account', 'lookback', 'correlation_status']),
    forward_coverage: countsBy(forwards, ['account', 'lookback', 'horizon', 'forward_status']),
    summary, by_switch_type: byType, partial_exit_reasons: exitReasons, robustness: robust,
    high_group_concentration: concentration(forwards) };
  fs.mkdirSync(output, { recursive: true });
  for (const [file, rows] of Object.entries({ 'switch_events.csv': allEvents, 'forward_pairs.csv': forwards,
    'summary.csv': summary, 'by_switch_type.csv': byType, 'by_year.csv': byYear, 'by_pair.csv': byPair,
    'pool_structure.csv': structures })) fs.writeFileSync(path.join(output, file), csv(rows));
  fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify(result, null, 2) + '\n');
  return result;
}

function countsBy(rows, keys) {
  const counts = new Map();
  for (const row of rows) {
    const key = keys.map(k => row[k]).join('|');
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return Object.fromEntries(counts);
}

function concentration(forwards) {
  const results = [];
  for (const account of ['mixed37', 'original15']) for (const lookback of lookbacks) for (const horizon of horizons) {
    const rows = forwards.filter(x => x.account === account && x.lookback === lookback
      && x.horizon === horizon && x.rho >= .8 && x.forward_status === 'complete');
    const largest = [...rows].sort((a, b) => Math.abs(b.after_switch_cost_advantage) - Math.abs(a.after_switch_cost_advantage)).slice(0, 5);
    results.push({ account, lookback, horizon, n: rows.length,
      mean_advantage: mean(rows.map(x => x.after_switch_cost_advantage)),
      largest_absolute_cases: largest.map(x => ({ date: x.date, old_code: x.old_code, new_code: x.new_code,
        rho: x.rho, old_score_export: x.old_score_export, partial_exit_reason: x.partial_exit_reason,
        old_return: x.old_return, new_return: x.new_return, after_switch_cost_advantage: x.after_switch_cost_advantage,
        mean_without_this_event: mean(rows.filter(y => y.id !== x.id).map(y => y.after_switch_cost_advantage)) })) });
  }
  return results;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const r = run(process.argv[2]);
  console.log(JSON.stringify({ boundaries: r.boundary_counts, coverage: r.correlation_coverage,
    summary: r.summary, by_switch_type_120: r.by_switch_type.filter(x => x.account === 'mixed37' && x.lookback === 120) }, null, 2));
}
