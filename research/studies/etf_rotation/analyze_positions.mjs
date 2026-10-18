// Read-only analysis of JoinQuant's position.csv. Run: node analyze_positions.mjs [csv]
// Buy fees use the current strategy's commission settings. Exit-day net P/L
// is reconstructed from account changes after removing the incoming position.
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const input = process.argv[2] || fileURLToPath(new URL('./position.csv', import.meta.url));
const commission = 0.0002;
const minimumCommission = 5;
const startingCash = 100000;
const raw = new TextDecoder('gb18030', { fatal: true }).decode(fs.readFileSync(input));

function parseCsv(text) {
  const rows = [];
  let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++; }
      else quoted = !quoted;
    } else if (c === ',' && !quoted) { row.push(cell); cell = ''; }
    else if ((c === '\r' || c === '\n') && !quoted) {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell);
      if (row.some(value => value !== '')) rows.push(row);
      row = []; cell = '';
    } else cell += c;
  }
  if (quoted) throw new Error('Unclosed quoted CSV field');
  if (row.length || cell) { row.push(cell); rows.push(row); }
  return rows;
}

function numeric(value, label) {
  if (value === '' || value == null || !Number.isFinite(Number(value))) {
    throw new Error(`Invalid numeric field: ${label}=${value}`);
  }
  return Number(value);
}

const rows = parseCsv(raw);
if (rows[0][0] !== '日期' || rows[0][12] !== '当日盈亏') throw new Error('Unexpected CSV header');
const byDate = new Map();
for (const r of rows.slice(1)) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(r[0])) throw new Error(`Invalid date: ${r[0]}`);
  if (!byDate.has(r[0])) byDate.set(r[0], { date: r[0], positions: [], cash: null });
  const day = byDate.get(r[0]);
  if (r[2] === 'Cash') {
    if (day.cash !== null) throw new Error(`Duplicate cash row: ${r[0]}`);
    day.cash = numeric(r[7], 'cash');
  } else {
    // Export defect: security rows have an unlabelled account-value field at
    // index 15, before position weight; header/cash rows have only 16 fields.
    if (r.length !== 17) throw new Error(`Unexpected security row width: ${r[0]}`);
    const match = r[2].match(/^(.*)\((\d{6}\.(?:XSHG|XSHE))\)$/);
    if (!match) throw new Error(`Invalid security identifier: ${r[2]}`);
    day.positions.push({ name: match[1], code: match[2],
      qty: numeric(r[4].replace(/股$/, ''), 'quantity'),
      value: numeric(r[7], 'position value'),
      cost: numeric(r[9], 'entry average'),
      reportedPnl: numeric(r[12], 'reported daily P/L'),
      nav: numeric(r[15], 'account value') });
  }
}

const days = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
const assets = new Map(), episodes = [];
function asset(position) {
  if (!assets.has(position.code)) assets.set(position.code, { code: position.code,
    name: position.name, holdingDays: 0, netPnl: 0, logContribution: 0,
    drawdownPnl: 0, drawdownPeakPoints: 0, closedTrades: 0, closedWinners: 0 });
  return assets.get(position.code);
}

let previousNav = startingCash, previousCash = startingCash, previous = null, episode = null;
let reportedPnl = 0, cashReconciliationMax = 0, continuousPnlErrorMax = 0;
let cashEntryFeeErrorMax = 0, negativeCashDays = 0, negativeCashRatioMax = 0;
let cashEntryCount = 0, episodeCashflowErrorMax = 0;
let peak = startingCash, peakDate = null, worst = { drawdown: 0 };
for (const [dayIndex, day] of days.entries()) {
  if (day.cash === null || day.positions.length > 1) throw new Error(`Unsupported holdings: ${day.date}`);
  const current = day.positions[0] || null;
  day.nav = current ? current.nav : day.cash;
  if (day.nav <= 0) throw new Error(`Nonpositive NAV: ${day.date}`);
  const reconciliation = Math.abs(day.nav - day.cash - (current?.value || 0));
  cashReconciliationMax = Math.max(cashReconciliationMax, reconciliation);
  if (reconciliation > 0.011) throw new Error(`NAV/cash mismatch: ${day.date}`);
  const delta = day.nav - previousNav;
  const allocations = [];
  const changed = current?.code !== previous?.code;
  if (changed) {
    // New position's intraday mark-to-market is reported; the position sold
    // today is missing from the closing snapshot, so recover its net P/L.
    const buyFee = current ? Math.round(Math.max(minimumCommission,
      current.qty * current.cost * commission) * 100) / 100 : 0;
    const incomingNet = current ? current.reportedPnl - buyFee : 0;
    const outgoingNet = delta - incomingNet;
    if (previous) {
      allocations.push({ code: previous.code, pnl: outgoingNet, episodeIndex: episode.index });
      episode.netPnl += outgoingNet;
      const soldNetCash = day.cash - previousCash + (current ? current.qty * current.cost : 0) + buyFee;
      episode.sellFeeEstimate = Math.round(Math.max(minimumCommission,
        soldNetCash / (1 - commission) * commission) * 100) / 100;
      episode.beforeFeePnlEstimate = episode.netPnl + episode.buyFeeEstimate + episode.sellFeeEstimate;
      const cashflowPnl = soldNetCash - episode.entryNotional - episode.buyFeeEstimate
        + previousCash - episode.entryCashAfter;
      episodeCashflowErrorMax = Math.max(episodeCashflowErrorMax, Math.abs(episode.netPnl - cashflowPnl));
      if (Math.abs(episode.netPnl - cashflowPnl) > 0.011) throw new Error(`Episode cashflow mismatch: ${day.date}`);
      episode.exit = day.date;
      episode.exitDayIndex = dayIndex;
      episode.closed = true;
      asset(previous).closedTrades++;
      if (episode.netPnl > 0) asset(previous).closedWinners++;
      episode = null;
    } else {
      cashEntryCount++;
      cashEntryFeeErrorMax = Math.max(cashEntryFeeErrorMax, Math.abs(outgoingNet));
      if (Math.abs(outgoingNet) > 0.011) throw new Error(`Unexplained cash-entry change: ${day.date}`);
    }
    if (current) {
      episode = { index: episodes.length, code: current.code, name: current.name, entry: day.date,
        entryDayIndex: dayIndex, closeHoldingDays: 0, path: [],
        entryNotional: current.qty * current.cost, buyFeeEstimate: buyFee,
        entryCashAfter: day.cash,
        netPnl: incomingNet, logContribution: 0, closed: false };
      episodes.push(episode);
      allocations.push({ code: current.code, pnl: incomingNet, episodeIndex: episode.index });
    }
  } else if (current) {
    continuousPnlErrorMax = Math.max(continuousPnlErrorMax, Math.abs(delta - current.reportedPnl));
    if (Math.abs(delta - current.reportedPnl) > 0.011) throw new Error(`Unexplained holding-day change: ${day.date}`);
    allocations.push({ code: current.code, pnl: delta, episodeIndex: episode.index });
    episode.netPnl += delta;
  } else if (Math.abs(delta) > 0.011) throw new Error(`Unexplained cash-only change: ${day.date}`);
  if (current) { asset(current).holdingDays++; episode.closeHoldingDays++; }
  const dailyReturn = delta / previousNav;
  const link = dailyReturn === 0 ? 1 : Math.log1p(dailyReturn) / dailyReturn;
  for (const item of allocations) {
    const entry = assets.get(item.code) || asset(current);
    entry.netPnl += item.pnl;
    entry.logContribution += item.pnl / previousNav * link;
    episodes[item.episodeIndex].logContribution += item.pnl / previousNav * link;
    const holding = episodes[item.episodeIndex];
    holding.path.push({ date: day.date, dayIndex,
      returnOnEntryNotional: holding.netPnl / holding.entryNotional });
  }
  day.allocations = allocations;
  day.previousNav = previousNav;
  reportedPnl += current?.reportedPnl || 0;
  if (day.cash < 0) {
    negativeCashDays++;
    negativeCashRatioMax = Math.max(negativeCashRatioMax, -day.cash / day.nav);
  }
  if (day.nav > peak) { peak = day.nav; peakDate = day.date; }
  const drawdown = day.nav / peak - 1;
  if (drawdown < worst.drawdown) worst = { drawdown, peakDate, troughDate: day.date, peak, trough: day.nav };
  previousNav = day.nav;
  previousCash = day.cash;
  previous = current;
}

let recoveryDate = null;
for (const day of days) {
  if (day.date > worst.troughDate && day.nav >= worst.peak && recoveryDate === null) recoveryDate = day.date;
  if ((worst.peakDate === null || day.date > worst.peakDate) && day.date <= worst.troughDate) {
    for (const item of day.allocations) {
      const entry = assets.get(item.code);
      entry.drawdownPnl += item.pnl;
      entry.drawdownPeakPoints += item.pnl / worst.peak * 100;
    }
  }
}
const netProfit = previousNav - startingCash;
const all = [...assets.values()];
const sumNet = all.reduce((sum, entry) => sum + entry.netPnl, 0);
const sumLog = all.reduce((sum, entry) => sum + entry.logContribution, 0);
if (Math.abs(sumNet - netProfit) > Math.max(1, cashEntryCount) * 0.011) throw new Error('Attribution does not reconcile');
if (Math.abs(sumLog - Math.log(previousNav / startingCash)) > 0.00001) throw new Error('Log contributions do not reconcile');
const closed = episodes.filter(entry => entry.closed);
const sortedClosed = [...closed].sort((a, b) => b.netPnl - a.netPnl);
const sortedGrowth = [...closed].sort((a, b) => b.logContribution - a.logContribution);
console.log(JSON.stringify({ source: input, method: 'Single-position close snapshots; incoming fees estimated at 0.02%/min5; exit net P/L recovered from account change; no order-level exit prices supplied',
  dates: [days[0].date, days.at(-1).date], tradingDays: days.length,
  endingNav: previousNav, netProfit, reportedPnl, reportedPnlExcess: reportedPnl - netProfit,
  checks: { cashReconciliationMax, continuousPnlErrorMax, cashEntryFeeErrorMax, episodeCashflowErrorMax,
    netAttributionError: sumNet - netProfit, logAttributionError: sumLog - Math.log(previousNav / startingCash) },
  negativeCashDays, negativeCashRatioMax, worst: { ...worst, recoveryDate },
  closedTrades: closed.length, winners: closed.filter(entry => entry.netPnl > 0).length,
  losers: closed.filter(entry => entry.netPnl < 0).length,
  beforeFeeWinnersEstimate: closed.filter(entry => entry.beforeFeePnlEstimate > 0).length,
  beforeFeeLosersEstimate: closed.filter(entry => entry.beforeFeePnlEstimate < 0).length,
  top5ClosedPnlShare: sortedClosed.slice(0, 5).reduce((s, entry) => s + entry.netPnl, 0) / netProfit,
  top10ClosedPnlShare: sortedClosed.slice(0, 10).reduce((s, entry) => s + entry.netPnl, 0) / netProfit,
  top5ClosedLogGrowthShare: sortedGrowth.slice(0, 5).reduce((s, entry) => s + entry.logContribution, 0) / sumLog,
  top10ClosedLogGrowthShare: sortedGrowth.slice(0, 10).reduce((s, entry) => s + entry.logContribution, 0) / sumLog,
  assets: all.sort((a, b) => b.netPnl - a.netPnl).map(entry => ({ ...entry,
    netProfitShare: entry.netPnl / netProfit, logGrowthShare: entry.logContribution / sumLog })),
  topTrades: sortedClosed.slice(0, 10), topGrowthTrades: sortedGrowth.slice(0, 10),
  openPositions: episodes.filter(entry => !entry.closed),
  ...(process.argv.includes('--episodes') ? { episodes, calendar: days.map(day => day.date) } : {}),
}, null, 2));
