// Diagnose observed entries; do not change the pool, score, or trading rules.
// node research/studies/etf_rotation/study_entry_volatility.mjs
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readCsv } from './study_etf_fit_forward.mjs';
import { quantile } from './study_etf_fit.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const sum = xs => xs.reduce((a, b) => a + b, 0);
const mean = xs => xs.length ? sum(xs) / xs.length : null;
const num = x => x !== '' && x != null && Number.isFinite(Number(x)) ? Number(x) : null;
const truth = x => String(x).toLowerCase() === 'true';
const bands = ['below_1pct', '1_to_2pct', 'at_least_2pct'];
export const volatilityBand = sigma => sigma < .01 ? bands[0] : sigma < .02 ? bands[1] : bands[2];

function csvFields(text) {
  const rows = []; let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted;
    } else if (c === ',' && !quoted) { row.push(cell); cell = ''; }
    else if ((c === '\n' || c === '\r') && !quoted) {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); if (row.some(x => x !== '')) rows.push(row);
      row = []; cell = '';
    } else cell += c;
  }
  if (quoted) throw new Error('Unclosed snapshot CSV field');
  if (row.length || cell) { row.push(cell); rows.push(row); }
  return rows;
}

export function snapshots(file) {
  const records = csvFields(new TextDecoder('gb18030', { fatal: true }).decode(fs.readFileSync(file)));
  if (records[0][0] !== '日期' || records[0][12] !== '当日盈亏') throw new Error('Unexpected snapshot header');
  const byDate = new Map();
  for (const r of records.slice(1)) {
    if (!byDate.has(r[0])) byDate.set(r[0], { date: r[0], cash: null, positions: [] });
    const day = byDate.get(r[0]);
    if (r[2] === 'Cash') {
      if (day.cash !== null) throw new Error('Duplicate cash row');
      day.cash = num(r[7]);
    } else {
      const match = r[2].match(/^(.*)\((\d{6})\.(?:XSHG|XSHE)\)$/);
      if (r.length !== 17 || !match) throw new Error('Malformed position row');
      const position = { name: match[1], code: match[2], quantity: num(r[4].replace(/股$/, '')),
        value: num(r[7]), cost: num(r[9]), pnl: num(r[12]), nav: num(r[15]) };
      if (Object.values(position).some(x => x === null)) throw new Error('Missing snapshot value');
      if (day.positions.some(x => x.code === position.code)) throw new Error('Duplicate position');
      day.positions.push(position);
    }
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

export function reconstructMixed(days) {
  let previous = [], previousNav = 100000, unallocated = 0, maxNavError = 0;
  const active = new Map(), rows = [], skipped = [];
  for (const day of days) {
    const current = day.positions;
    const nav = current[0]?.nav ?? day.cash;
    const navError = Math.abs(nav - day.cash - sum(current.map(x => x.value)));
    maxNavError = Math.max(maxNavError, navError);
    if (day.cash === null || navError > .011 || current.some(x => Math.abs(x.nav - nav) > .011)) {
      throw new Error(`Snapshot cash/NAV mismatch: ${day.date}`);
    }
    for (const p of current) {
      if (!previous.some(x => x.code === p.code)) {
        const item = { code: p.code, name: p.name, entry: day.date, exit: '', closed: false,
          complete_pnl: true, holding_days: 0, entry_notional: p.cost * p.quantity,
          net_pnl: 0, path: [] };
        active.set(p.code, item); rows.push(item);
      }
      active.get(p.code).holding_days++;
    }
    const change = nav - previousNav;
    if (previous.length > 1 || current.length > 1) {
      skipped.push(day.date); unallocated += change;
      for (const item of active.values()) item.complete_pnl = false;
    } else {
      const a = previous[0], b = current[0];
      if (a?.code !== b?.code) {
        const fee = b ? Math.round(Math.max(5, b.cost * b.quantity * .0002) * 100) / 100 : 0;
        const incoming = b ? b.pnl - fee : 0;
        if (a) active.get(a.code).net_pnl += change - incoming;
        else if (Math.abs(change - incoming) > .011) throw new Error('Unexplained cash entry P/L');
        if (b) active.get(b.code).net_pnl += incoming;
      } else if (b) active.get(b.code).net_pnl += change;
      else if (Math.abs(change) > .011) throw new Error('Unexplained cash-only P/L');
      for (const item of active.values()) item.path.push(item.net_pnl / item.entry_notional);
    }
    for (const p of previous) {
      if (!current.some(x => x.code === p.code)) {
        const item = active.get(p.code);
        item.exit = day.date; item.closed = true; active.delete(p.code);
      }
    }
    previous = current; previousNav = nav;
  }
  const reconciliation = sum(rows.map(x => x.net_pnl)) + unallocated - (previousNav - 100000);
  if (Math.abs(reconciliation) > .02) throw new Error('Mixed segment P/L reconciliation failed');
  return { rows, checks: { max_nav_error: maxNavError, net_pnl_error: reconciliation,
    skipped_dates: skipped, unallocated_pnl: unallocated } };
}

function pathMetrics(values) {
  let peak = 1, drawdown = 0;
  for (const r of values) {
    peak = Math.max(peak, 1 + r);
    drawdown = Math.max(drawdown, 1 - (1 + r) / peak);
  }
  return { observed_mae: Math.min(0, ...values), observed_mfe: Math.max(0, ...values),
    observed_max_drawdown: drawdown };
}

function usable(row) {
  return row && num(row.close) > 0 && num(row.event_close) > 0
    && num(row.volume) > 0 && (row.paused === '' || num(row.paused) === 0)
    && (!row.quality || row.quality === 'ok');
}

export function featuresBeforeEntry(calendar, market, code, entry) {
  const at = calendar.indexOf(entry);
  if (at < 25) return { feature_status: 'insufficient_calendar' };
  // Exact exchange dates: missing/paused prices are not skipped or filled.
  const dates = calendar.slice(at - 25, at);
  const rows = dates.map(date => market.get(`${date}|${code}`));
  if (!rows.every(usable)) return { feature_status: 'missing_or_inactive_history' };
  const prices = rows.map(row => Number(row.event_close));
  const logs = prices.slice(1).map((p, i) => Math.log(p / prices[i]));
  const recent = logs.slice(-20), average = mean(recent);
  const sigma = Math.sqrt(sum(recent.map(r => (r - average) ** 2)) / 19);
  const positive = logs.filter(r => r > 0).sort((a, b) => b - a);
  const positiveSum = sum(positive);
  let peak = prices[0], drawdown = 0;
  for (const price of prices) {
    peak = Math.max(peak, price);
    drawdown = Math.max(drawdown, 1 - price / peak);
  }
  return { feature_status: 'complete', feature_start: dates[0], feature_end: dates.at(-1),
    vol20: sigma, volatility_band: volatilityBand(sigma),
    return24: prices.at(-1) / prices[0] - 1, historical_drawdown24: drawdown,
    top3_positive_share24: positiveSum > 0 ? sum(positive.slice(0, 3)) / positiveSum : null,
    trend_efficiency24: sum(logs.map(Math.abs)) > 0 ? sum(logs) / sum(logs.map(Math.abs)) : 0 };
}

export function summarize(rows) {
  const r = rows.map(x => x.net_return), winners = rows.filter(x => x.net_return > 0);
  const losers = rows.filter(x => x.net_return < 0);
  const grossWins = sum(winners.map(x => x.net_return));
  const grossLosses = -sum(losers.map(x => x.net_return));
  const sortedWins = winners.map(x => x.net_return).sort((a, b) => b - a);
  const longWins = winners.filter(x => x.holding_days >= 10);
  const topCodeCounts = [...new Set(rows.map(x => x.code))].map(code => ({ code,
    count: rows.filter(x => x.code === code).length })).sort((a, b) => b.count - a.count);
  return { n: rows.length, codes: topCodeCounts.length, entry_years: new Set(rows.map(x => x.year)).size,
    mean_net_return: mean(r), median_net_return: quantile(r, .5),
    win_rate: rows.length ? winners.length / rows.length : null,
    quick_loss_3d_rate: rows.length ? rows.filter(x => x.net_return < 0 && x.holding_days <= 3).length / rows.length : null,
    median_holding_days: quantile(rows.map(x => x.holding_days), .5),
    normalized_gross_wins: grossWins, normalized_gross_losses: grossLosses,
    return_profit_factor: grossLosses > 0 ? grossWins / grossLosses : null,
    top5_winners_share_of_gross_wins: grossWins > 0 ? sum(sortedWins.slice(0, 5)) / grossWins : null,
    mean_return_excluding_top5_winners: rows.length > Math.min(5, winners.length)
      ? (sum(r) - sum(sortedWins.slice(0, 5))) / (rows.length - Math.min(5, winners.length)) : null,
    profitable_10plus_day_segments: longWins.length,
    long_winner_loss_coverage: grossLosses > 0 ? sum(longWins.map(x => x.net_return)) / grossLosses : null,
    net_pnl_yuan: sum(rows.map(x => x.net_pnl)), median_vol20: quantile(rows.map(x => x.vol20), .5),
    mean_observed_mae: mean(rows.map(x => x.observed_mae)),
    median_observed_max_drawdown: quantile(rows.map(x => x.observed_max_drawdown), .5),
    median_top3_positive_share24: quantile(rows.map(x => x.top3_positive_share24), .5),
    median_historical_drawdown24: quantile(rows.map(x => x.historical_drawdown24), .5),
    most_frequent_code: topCodeCounts[0]?.code || null,
    most_frequent_code_share: rows.length ? (topCodeCounts[0]?.count || 0) / rows.length : null };
}

function encodeCsv(rows) {
  if (!rows.length) return '';
  const keys = Object.keys(rows[0]);
  const quote = value => value == null ? '' : /[",\r\n]/.test(String(value))
    ? `"${String(value).replaceAll('"', '""')}"` : String(value);
  return '\uFEFF' + [keys.join(','), ...rows.map(row => keys.map(k => quote(row[k])).join(','))].join('\n') + '\n';
}

function grouped(rows, keys) {
  const groups = new Map();
  for (const row of rows) {
    const key = keys.map(k => row[k]).join('|');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return [...groups.values()].map(items => ({ ...Object.fromEntries(keys.map(k => [k, items[0][k]])),
    ...summarize(items) }));
}

function differences(rows) {
  const mid = rows.filter(x => x.volatility_band === bands[1]);
  const high = rows.filter(x => x.volatility_band === bands[2]);
  return { mid_n: mid.length, high_n: high.length,
    high_minus_mid_mean: mid.length && high.length
      ? mean(high.map(x => x.net_return)) - mean(mid.map(x => x.net_return)) : null,
    high_minus_mid_quick_loss_rate: mid.length && high.length
      ? mean(high.map(x => +(x.holding_days <= 3 && x.net_return < 0)))
        - mean(mid.map(x => +(x.holding_days <= 3 && x.net_return < 0))) : null };
}

function robustness(rows) {
  const byYear = [...new Set(rows.map(x => x.year))].sort().map(year => ({ year,
    ...differences(rows.filter(x => x.year === year)) }));
  const leaveOut = key => [...new Set(rows.map(x => x[key]))].sort().map(value => ({ excluded: value,
    ...differences(rows.filter(x => x[key] !== value)) }));
  const yearStrata = byYear.filter(x => x.mid_n >= 5 && x.high_n >= 5);
  const byCode = [...new Set(rows.map(x => x.code))].sort().map(code => ({ code,
    ...differences(rows.filter(x => x.code === code)) }));
  const codeStrata = byCode.filter(x => x.mid_n >= 5 && x.high_n >= 5);
  return { pooled: differences(rows), by_year: byYear, by_code: byCode,
    equal_weight_year_difference: mean(yearStrata.map(x => x.high_minus_mid_mean)),
    qualifying_years: yearStrata.length,
    high_better_years: yearStrata.filter(x => x.high_minus_mid_mean > 0).length,
    equal_weight_code_difference: mean(codeStrata.map(x => x.high_minus_mid_mean)),
    qualifying_codes: codeStrata.length,
    leave_one_year_out: leaveOut('year'), leave_one_code_out: leaveOut('code') };
}

function segmentInputs() {
  const mixedFile = path.join(here, 'results/mixed37_comparison/mixed_holding_segments.csv');
  const reference = readCsv(mixedFile);
  const rebuilt = reconstructMixed(snapshots(path.join(here, 'position_mixed37.csv')));
  if (reference.length !== rebuilt.rows.length) throw new Error('Mixed segment count changed');
  const mixed = rebuilt.rows.map(({ path: holdingPath, ...x }) => {
    const old = reference.find(y => y.entry === x.entry && y.code.slice(0, 6) === x.code);
    if (!old || truth(old.complete_pnl) !== x.complete_pnl || truth(old.closed) !== x.closed
        || old.exit !== x.exit || Number(old.observed_days) !== x.holding_days
        || Math.abs(Number(old.entry_notional) - x.entry_notional) > .011
        || (x.complete_pnl && Math.abs(Number(old.net_pnl) - x.net_pnl) > .011)) {
      throw new Error(`Mixed segment differs from previous independent study: ${x.entry} ${x.code}`);
    }
    return { ...x, account: 'mixed37', group: old.group,
      net_return: x.net_pnl / x.entry_notional, ...pathMetrics(holdingPath) };
  });
  const original = JSON.parse(execFileSync(process.execPath,
    [path.join(here, 'analyze_positions.mjs'), path.join(here, 'position.csv'), '--episodes'],
    { encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 }));
  const originals = original.episodes.map(x => ({ account: 'original15', group: 'original15',
    code: x.code.slice(0, 6), name: x.name, entry: x.entry, exit: x.exit || '',
    closed: x.closed, complete_pnl: true, holding_days: x.closeHoldingDays,
    entry_notional: x.entryNotional, net_pnl: x.netPnl, net_return: x.netPnl / x.entryNotional,
    ...pathMetrics(x.path.map(p => p.returnOnEntryNotional)) }));
  return { rows: [...mixed, ...originals], originalReconciliation: original.checks,
    mixedReconciliation: rebuilt.checks, mixedFile };
}

export function run(output = path.join(here, 'results/entry_volatility')) {
  const cache = path.join(here, '_cache/pool37_event_adjusted_20260930');
  const manifest = JSON.parse(fs.readFileSync(path.join(cache, 'manifest.json'), 'utf8'));
  if (manifest.usable_for_returns !== true || manifest.verified_codes.length !== 37
      || Object.keys(manifest.failures).length) throw new Error('Adjusted snapshot is not verified');
  const marketFile = path.join(cache, 'market.csv');
  const marketRows = readCsv(marketFile), market = new Map();
  for (const row of marketRows) {
    const key = `${row.date}|${row.code.slice(0, 6)}`;
    if (market.has(key)) throw new Error(`Duplicate market observation ${key}`);
    market.set(key, row);
  }
  const calendar = [...new Set(marketRows.map(x => x.date))].sort();
  const inputs = segmentInputs();
  const all = inputs.rows.map(x => ({ ...x, year: x.entry.slice(0, 4),
    ...featuresBeforeEntry(calendar, market, x.code, x.entry) }));
  for (const row of all) {
    if (row.closed && row.complete_pnl && (!Number.isFinite(row.net_return)
        || Math.abs(row.net_return - row.net_pnl / row.entry_notional) > 1e-10)) {
      throw new Error(`Segment return does not reconcile: ${row.entry} ${row.code}`);
    }
    if (row.feature_status === 'complete' && !(row.feature_end < row.entry)) throw new Error('Lookahead');
    row.included = row.closed && row.complete_pnl && row.feature_status === 'complete';
    row.exclusion = !row.closed ? 'open_position' : !row.complete_pnl ? 'incomplete_pnl'
      : row.feature_status !== 'complete' ? row.feature_status : '';
    row.jump_concentration = row.top3_positive_share24 == null ? 'no_positive_days'
      : row.top3_positive_share24 >= .5 ? 'top3_at_least_half' : 'top3_below_half';
  }
  const included = all.filter(x => x.included);
  const summary = grouped(included, ['account', 'group', 'volatility_band']);
  const overall = grouped(included, ['account', 'group']);
  const diagnostics = {};
  for (const row of overall) {
    diagnostics[`${row.account}|${row.group}`] = robustness(
      included.filter(x => x.account === row.account && x.group === row.group));
  }
  const byYear = grouped(included, ['account', 'group', 'volatility_band', 'year']);
  const byCode = grouped(included, ['account', 'group', 'volatility_band', 'code']);
  const jumpGroups = grouped(included, ['account', 'group', 'volatility_band', 'jump_concentration']);
  const excluded = all.filter(x => !x.included);
  const crossGroup = bands.map(band => {
    const rows = included.filter(x => x.account === 'mixed37' && x.volatility_band === band);
    const orig = rows.filter(x => x.group === 'original15'), ind = rows.filter(x => x.group === 'added_industry');
    const years = [...new Set(rows.map(x => x.year))].sort().map(year => {
      const a = orig.filter(x => x.year === year), b = ind.filter(x => x.year === year);
      return { year, original_n: a.length, industry_n: b.length,
        industry_minus_original_mean: a.length && b.length ? mean(b.map(x => x.net_return)) - mean(a.map(x => x.net_return)) : null };
    });
    return { volatility_band: band, original_n: orig.length, industry_n: ind.length,
      industry_minus_original_mean: orig.length && ind.length ? mean(ind.map(x => x.net_return)) - mean(orig.map(x => x.net_return)) : null,
      equal_weight_year_gap: mean(years.filter(x => x.original_n >= 3 && x.industry_n >= 3).map(x => x.industry_minus_original_mean)),
      by_year: years };
  });
  const sourceFiles = [marketFile, inputs.mixedFile, path.join(here, 'position.csv'),
    path.join(here, 'position_mixed37.csv'), path.join(cache, 'manifest.json')];
  const result = { method: { volatility: 'Sample std of 20 adjusted log returns from 21 closes ending BEFORE entry; 25 exact exchange closes required',
    common_bands_daily: { below_1pct: '[0, .01)', '1_to_2pct': '[.01, .02)', at_least_2pct: '[.02, infinity)' },
    pnl: 'Actual closed holding segments, net P/L including original account costs; mixed multi-holding-affected segments excluded',
    profit_factor: 'Sum positive segment returns / abs(sum negative segment returns), each normalized by its own entry notional',
    long_winner: 'Closed segment held at least 10 observed closing days, net return > 0; contribution divided by all losses',
    observed_risk: 'Cumulative actual net P/L / entry notional at held daily closes plus reconstructed exit mark; excludes intraday moves',
    quick_loss: 'Closed net-losing segment with at most 3 observed closing days',
    jump: 'Top 3 positive adjusted log returns / sum of all positive log returns in 24 pre-entry returns; concentration >= 50%',
    robustness: 'High minus medium volatility; year/code strata each need >=5 entries per band; omit-one diagnostics are descriptive',
    sources: sourceFiles.map(file => ({ file: path.relative(here, file), sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') })),
    limits: ['Observed selected entries only; no causal filter effect or alternative account return',
      'Segment returns are not additive or compound account returns; calendar-year risk is not entry-year mean return',
      'Group dates/scores/exposures differ; volatility conditioning does not eliminate selection bias',
      'Historical closes omit intraday adverse moves; SDK validation covers its authorized year only',
      'No historical AUM/spread/industry turnover series; no size effect claim',
      'No entry-day close is used in pre-entry features; no optimized thresholds or trading changes'] },
    counts: { input_segments: all.length, included: included.length, excluded: excluded.length },
    exclusions: groupedExclusions(excluded), original_account_reconciliation: inputs.originalReconciliation,
    mixed_account_reconciliation: inputs.mixedReconciliation,
    overall, summary, cross_group_same_volatility: crossGroup, robustness: diagnostics };
  fs.mkdirSync(output, { recursive: true });
  for (const [file, rows] of Object.entries({ 'episodes.csv': all, 'summary.csv': summary,
    'by_year.csv': byYear, 'by_code.csv': byCode, 'jump_concentration.csv': jumpGroups })) {
    fs.writeFileSync(path.join(output, file), encodeCsv(rows));
  }
  fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify(result, null, 2) + '\n');
  return result;
}

function groupedExclusions(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = `${row.account}|${row.group}|${row.exclusion}`;
    groups.set(key, (groups.get(key) || 0) + 1);
  }
  return Object.fromEntries(groups);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = run(process.argv[2]);
  console.log(JSON.stringify({ counts: result.counts, exclusions: result.exclusions,
    summary: result.summary, cross_group_same_volatility: result.cross_group_same_volatility.map(({ by_year, ...x }) => x),
    robustness: Object.fromEntries(Object.entries(result.robustness).map(([key, value]) => [key, {
      pooled: value.pooled, equal_weight_year_difference: value.equal_weight_year_difference,
      qualifying_years: value.qualifying_years, high_better_years: value.high_better_years,
      equal_weight_code_difference: value.equal_weight_code_difference, qualifying_codes: value.qualifying_codes }])) }, null, 2));
}
