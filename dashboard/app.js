import { esc, quoteTime, accountRequest } from './common.js';

import { accountName, isoDay } from './dashboard-format.js';

import { createAccountActions } from './account-actions.js';

import { createEntryDialog } from './entry-dialog.js';

import { createFundDialogs } from './fund-dialogs.js';
import { createFundOrders } from './fund-orders.js';

import { bindPerformance, toggleReport } from './performance.js';

import { createPortfolioView } from './portfolio-view.js';

import { createQuoteRefresh, refreshInProgress } from './quotes.js';

import { createRecords } from './records.js';

import { createCustodyDialogs } from './trading-accounts.js';

const ACCOUNT_LOAD_FAILED = '账户数据读取失败，请检查服务器日志。';

let data, selected = 0;
let overviewRequest = 0, appliedOverviewRequest = 0;
const requestedFundNames = new Set();
const requestedSecurityNames = new Set();

const currentAccount = () => data.accounts[0];
const currentStrategies = () => data.strategies;
const activeView = () => selected === 0 ? currentAccount() : ownerViews()[selected - 1];
const fundDisplayName = code => currentAccount().fund_names?.[code];

function ownerViews() {
  const area = data.accounts[0]?.holding_area;
  return area ? [...data.strategies, area] : data.strategies;
}
const strategyOptions = (strategies = ownerViews()) => strategies.map(({id, name}) =>
  `<option value="${esc(id)}">${esc(name)}</option>`).join('');
const strategyName = id => ownerViews().find(row=>row.id===id)?.name;

const records = createRecords({currentAccount, strategyName, fundDisplayName});
const {showEntryReceipt} = records;
const {openCashAdjust, openCustodyMove} = createCustodyDialogs({strategyName, reloadAccount, showEntryReceipt});
const openEntry = createEntryDialog({
  currentAccount, ownerViews, strategyOptions, strategyName, fundDisplayName, reloadAccount, showEntryReceipt,
});
const {pendingFundSection, openFundTrade, openFundVoid, openDcaPlans} = createFundDialogs({
  currentAccount, ownerViews, strategyOptions, strategyName, fundDisplayName, reloadAccount, showEntryReceipt,
  openFundOrders: id => fundOrders.open(id),
});
const fundOrders = createFundOrders({currentAccount, fundDisplayName, openFundTrade, openFundVoid});
const portfolio = createPortfolioView({
  currentAccount, ownerViews, strategyName, fundDisplayName, pendingFundSection,
  recordSection: records.recordSection, recordsExpanded: records.recordsExpanded, receiptText: records.receiptText,
});
const {bindAccountActions} = createAccountActions({
  currentAccount, currentStrategies, fundDisplayName, reloadAccount, showEntryReceipt,
  openEntry, openCashAdjust, openCustodyMove, openFundTrade, openFundVoid, openDcaPlans,
  openFundOrders: fundOrders.open, bindRecords: records.bindRecords, bindReconciliation: portfolio.bindReconciliation,
});
const {updateQuoteControls, scheduleQuotePoll, refreshQuotes} = createQuoteRefresh({
  currentAccount, activeView, reloadAccount,
});

function render() {
  fundOrders.refresh();
  updateQuoteControls();
  const statusAccount = data.accounts[0];
  document.querySelector('#account-settings').hidden=!statusAccount.can_record;
  const bar = document.querySelector('#status-bar');
  const barHtml = selected === 0 ? '' : portfolio.statusBar(statusAccount);
  bar.innerHTML = barHtml;
  bar.hidden = !barHtml;
  const tabs = [{id:'etf',name:'账户总览'},...ownerViews()];
  document.querySelector('#tabs').innerHTML = tabs.map((tab,index) => `<button type="button" role="tab" id="tab-${esc(tab.id)}" class="${index===selected ? 'active' : ''}" aria-selected="${index===selected}" aria-controls="account" tabindex="${index===selected ? 0 : -1}" data-index="${index}">${esc(accountName(tab))}</button>`).join('');
  function activateTab(index) {
    if (selected !== index) records.clearReceipt();
    selected=index; render();
    document.querySelector(`#tabs [data-index="${selected}"]`).focus();
  }
  document.querySelectorAll('#tabs button').forEach(button => {
    button.onclick=() => activateTab(Number(button.dataset.index));
    button.onkeydown=event => {
      const next = {ArrowRight:(selected+1)%tabs.length,ArrowLeft:(selected+tabs.length-1)%tabs.length,Home:0,End:tabs.length-1}[event.key];
      if (next!==undefined) { event.preventDefault(); activateTab(next); }
    };
  });
  document.querySelector('#account').setAttribute('aria-labelledby',`tab-${tabs[selected].id}`);
  const account = selected===0 ? data.accounts[0] : tabs[selected];
  document.querySelector('#account').innerHTML = selected===0 ? portfolio.summaryPage() : portfolio.fundedStrategyPage(account);
  bindPerformance(account, data.reports);
  bindAccountActions(account);
  portfolio.bindCoreSort(account, bindAccountActions);
}

async function fetchOverview() {
  const response = await fetch('/api/overview', {cache:'no-store'});
  if (!response.ok) throw Error('页面暂时无法更新，请重试。');
  return response.json();
}

async function loadOverview() {
  const app = document.querySelector('#app');
  const status = document.querySelector('#status');
  const retry = document.querySelector('#overview-retry');
  const retrying = !retry.hidden;
  const wasHidden = app.hidden;
  retry.disabled = true;
  status.textContent = '正在加载…';
  try {
    data = await fetchOverview();
    app.hidden = false;
    render();
    const accountFailed = data.accounts[0]?.error === ACCOUNT_LOAD_FAILED;
    status.textContent = accountFailed ? '账户数据读取失败。' : `页面更新于 ${isoDay(quoteTime(data.generated_at))}`;
    retry.hidden = !accountFailed;
    if (accountFailed) return;
    if (retrying) document.querySelector('#tabs [aria-selected="true"]').focus();
    loadMissingFundNames();
    loadMissingSecurityNames();
    scheduleQuotePoll();
  } catch {
    app.hidden = wasHidden;
    status.textContent = wasHidden ? '账户数据读取失败。' : '账户刷新失败，仍显示已加载内容。';
    retry.hidden = false;
  } finally {
    retry.disabled = false;
    if (retrying && !retry.hidden) retry.focus();
  }
}

window.addEventListener('pageshow', event => {
  if (event.persisted && data) reloadAccount().catch(failure => {
    document.querySelector('#status').textContent=failure.message;
  });
});

async function loadMissingFundNames() {
  const codes = Object.entries(data.accounts[0].fund_names || {})
    .filter(([code,name]) => !name && !requestedFundNames.has(code))
    .slice(0,100).map(([code]) => code);
  if (!codes.length) return;
  codes.forEach(code => requestedFundNames.add(code));
  try {
    const {names} = await accountRequest('/api/etf/fund/names', {codes});
    const current = data.accounts[0].fund_names;
    const updated = Object.keys(names).filter(code => current?.[code] === null);
    if (updated.length) {
      updated.forEach(code => { current[code] = names[code]; });
      render();
    }
    loadMissingFundNames();
  } catch {
    // 未能加载简称时保留基金代码。
  }
}

async function loadMissingSecurityNames() {
  const codes = (data.accounts[0]?.missing_security_names || [])
    .filter(code => !requestedSecurityNames.has(code)).slice(0,100);
  if (!codes.length) return;
  codes.forEach(code => requestedSecurityNames.add(code));
  try {
    const {names} = await accountRequest('/api/etf/security/names', {codes});
    if (Object.keys(names).length) {
      await reloadAccount({deferWhileRefreshing:true});
    } else {
      loadMissingSecurityNames();
    }
  } catch {
    // 读不到简称时继续显示证券代码。
  }
}

async function reloadAccount({deferWhileRefreshing=false} = {}) {
  const request = ++overviewRequest;
  const result = await fetchOverview();
  if (request < appliedOverviewRequest) return;
  if (result.accounts[0]?.error === ACCOUNT_LOAD_FAILED) throw Error('账户暂时无法加载，请重试。');
  appliedOverviewRequest = request;
  const previous = data.accounts[0]?.quote_refresh;
  data = result;
  document.querySelector('#status').textContent=`页面更新于 ${quoteTime(data.generated_at)}`;
  const state = data.accounts[0]?.quote_refresh;
  const completed = (previous?.running && !state?.running) || (previous?.funds?.running && !state?.funds?.running);
  if (!deferWhileRefreshing || completed || !refreshInProgress(state)) {
    render();
    loadMissingFundNames();
    loadMissingSecurityNames();
  }
}

document.querySelector('#quote-refresh').onclick = refreshQuotes;
document.querySelector('#report-expand').onclick = toggleReport;
document.querySelector('#overview-retry').onclick = loadOverview;
loadOverview();
