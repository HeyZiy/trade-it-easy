import { esc, accountRequest } from './common.js';

import { valuationLabel } from './dashboard-format.js';

export const refreshInProgress = state => state?.running || state?.funds?.running;

export function createQuoteRefresh({currentAccount, activeView, reloadAccount}) {
  let quoteRequestPending = false, quotePollTimer, quotePolling = false, quoteError = '';

  function updateQuoteControls() {
    const account = currentAccount();
    const active = activeView();
    document.querySelector('#quote-panel').hidden = !account.can_record || (active !== account && active?.account_id!=='etf');
    const state = account.quote_refresh;
    const funds = state?.funds;
    const running = quoteRequestPending || refreshInProgress(state);
    const button = document.querySelector('#quote-refresh');
    button.disabled = !!running;
    button.setAttribute('aria-busy',String(!!running));
    button.textContent = running ? '刷新中…' : '刷新估值';
    const valuation=account.valuation;
    let note = valuationLabel(valuation);
    let message = state?.error || '';
    if (quoteRequestPending || state?.running) note = '正在补查收盘价…';
    if (funds?.running && !quoteRequestPending && !state.running) note = '正在更新场外基金净值…';
    if (funds?.updated || funds?.unchanged || funds?.failed) {
      const parts = [];
      if (funds.confirmed?.length) parts.push(`已确认 ${funds.confirmed.length} 笔申购`);
      if (funds.updated?.length) parts.push(`已更新 ${funds.updated.length} 只`);
      if (funds.unchanged?.length) parts.push(`${funds.unchanged.length} 只保留原日期`);
      if (funds.failed?.length) parts.push(`${funds.failed.join('、')} 取数失败，保留原净值`);
      if (parts.length) message += `${message?' ':''}场外净值 · ${parts.join('，')}。`;
    }
    if (quoteError) message = quoteError;
    const status = document.querySelector('#quote-status');
    status.textContent = active === account && !running ? '' : note;
    const feedback = document.querySelector('#quote-message');
    feedback.textContent = message; feedback.hidden = !message;

    const missingGroups = [
      {codes:valuation?.missing, label:'收盘价', name:code => account.positions.find(row => row.code === code)?.name},
      {codes:valuation?.missing_funds, label:'单位净值', name:code => account.fund_names?.[code]},
    ].filter(group => group.codes?.length);
    const missing = document.querySelector('#quote-missing');
    missing.hidden = !missingGroups.length;
    const summary = missingGroups.map(({codes,label}) => `${codes.length} 只缺${label}`).join('、');
    const chips = missingGroups.map(({codes,name}) => `<p class="quote-chips">${codes.map(code => {
      const label = name(code);
      return `<span class="quote-chip">${esc(label && label !== code ? `${label}（${code}）` : code)}</span>`;
    }).join('')}</p>`).join('');
    const expanded = missing.querySelector('details')?.open;
    missing.innerHTML = summary ? `<details class="quote-missing"${expanded ? ' open' : ''}><summary>${summary} · 账户待估值</summary>${chips}</details>` : '';
  }

  function scheduleQuotePoll() {
    clearTimeout(quotePollTimer);
    if (refreshInProgress(currentAccount()?.quote_refresh)) quotePollTimer = setTimeout(pollQuotes, 1500);
  }

  function clearQuoteRefreshPending() {
    const state = currentAccount()?.quote_refresh;
    if (state) state.running = false;
    if (state?.funds) state.funds.running = false;
  }

  async function pollQuotes() {
    if (quotePolling) return;
    quotePolling = true;
    try {
      await reloadAccount({deferWhileRefreshing:true});
      quoteError = '';
      updateQuoteControls();
      scheduleQuotePoll();
    } catch {
      quoteError = '连接中断，请重试。';
      // 网络断开时允许主动重试；服务器会合并正在进行的刷新。
      clearQuoteRefreshPending();
      updateQuoteControls();
    } finally { quotePolling = false; }
  }

  async function refreshQuotes() {
    quoteRequestPending = true; quoteError = ''; updateQuoteControls();
    try {
      await accountRequest('/api/etf/refresh', {});
      await reloadAccount();
      scheduleQuotePoll();
    } catch (error) {
      quoteError = error.message || '估值刷新失败，请稍后重试。';
      clearQuoteRefreshPending();
    }
    finally { quoteRequestPending = false; updateQuoteControls(); }
  }

  return { updateQuoteControls, scheduleQuotePoll, refreshQuotes };
}
