import { esc, finite } from './common.js';

import { number, price, money, reportDate, SIDES, isFundTrade, fundShares, shanghaiToday } from './dashboard-format.js';

import { custodyName } from './trading-accounts.js';

export function createRecords({currentAccount, strategyName, fundDisplayName}) {
  const recordDisplays = new Map();
  let entryReceipt = '';

  function recordRows(view) {
    return view.id === 'etf' ? view.trades : [...(view.trades || [])].reverse();
  }

  function recordSection(view) {
    const rows = recordRows(view);
    const display = recordDisplays.get(view.id);
    const filters = display?.filters || {};
    const account = currentAccount();
    const options = (values, selected, label) => [...new Set(values)].sort().map(value =>
      `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(label(value))}</option>`).join('');
    return `<section id="account-records" class="section" aria-labelledby="account-records-title" tabindex="-1" ${display?.expanded ? '' : 'hidden'}>
      <div class="section-heading"><h2 id="account-records-title">${view.id === 'etf' ? '账户流水' : '策略流水'}</h2>
        <div class="allocation-actions">
          <button type="button" id="export-records" class="text-button">导出 CSV</button>
          <button type="button" id="close-records" class="text-button">收起流水</button>
        </div>
      </div>
      <div id="record-filters" class="allocation-actions">
        <label>开始日期 <input type="date" name="start" value="${esc(filters.start || '')}"></label>
        <label>结束日期 <input type="date" name="end" value="${esc(filters.end || '')}"></label>
        <label>代码 <input type="search" name="code" size="8" value="${esc(filters.code || '')}"></label>
        <label>交易账户 <select name="account"><option value="">全部</option>${options(rows.flatMap(recordAccounts), filters.account, id => custodyName(account,id))}</select></label>
        <label>类型 <select name="type"><option value="">全部</option>${options(rows.map(recordType), filters.type, type => type === 'pending_assign' ? '在途申购归属划转' : SIDES[type] || type)}</select></label>
        <button type="button" id="reset-record-filters" class="text-button">清除筛选</button>
      </div>
      <div class="section-heading">
        <span id="record-count" class="subtle" aria-live="polite"></span>
        <button type="button" id="all-records" class="text-button" hidden></button>
      </div>
      <div id="record-results"></div></section>`;
  }

  const recordType = row => row.kind === 'pending' ? 'pending_assign' : row.side;

  function recordAccounts(row) {
    if (!row.trading_account && ['cash_assign','cash_transfer'].includes(row.side)) return [];
    return [row.trading_account || 'unlocated', row.to_account].filter(Boolean);
  }

  function filterRecords(rows, {start, end, code, account, type}) {
    const query = (code || '').trim().toLowerCase();
    return rows.filter(row => {
      const day = reportDate(row.date);
      return (!start || day >= start) && (!end || day <= end)
        && (!query || String(row.code || '').toLowerCase().includes(query))
        && (!account || recordAccounts(row).includes(account))
        && (!type || recordType(row) === type);
    });
  }

  function recordLabel(row) {
    if (row.kind === 'pending') return '在途申购归属划转';
    if (row.side === 'fund_pending') return currentAccount().fund_pending?.[row.id] ? '场外申购 · 待确认' : '场外申购扣款';
    if (row.side === 'fund_redeem' && row.pending_id) return '场外赎回到账';
    return SIDES[row.side] || row.side;
  }

  function recordAmount(row) {
    return row.kind === 'fund' ? row.transfer_price : row.price ?? row.amount;
  }

  const recordCashAllocations = row => Object.entries(row.cash_allocations || {}).map(([key,amount]) =>
    `${strategyName(key) || key} ${price(amount)} 元`).join(' · ');

  function recordCsv(rows) {
    const account = currentAccount();
    const cells = [['日期','类型','代码','交易账户','转入账户','来源策略','所属策略','份额','价格或金额 / 元','手续费 / 元','现金分配明细']];
    for (const row of rows) {
      const [source, destination] = recordAccounts(row);
      cells.push([row.date, recordLabel(row), row.code ? `'${row.code}` : '',
        source ? custodyName(account,source) : '', destination ? custodyName(account,destination) : '',
        strategyName(row.from_strategy) || row.from_strategy,
        strategyName(row.strategy) || row.strategy, row.qty, recordAmount(row), row.fee, recordCashAllocations(row)]);
    }
    const lines = cells.map(row => row.map(value => {
      const text = String(value ?? '');
      // Excel can evaluate formula-like text even when the CSV field is quoted.
      const safe = typeof value !== 'number' && /^(?:[\s\x00-\x1f]*[=+@-]|[\x09-\x0d])/u.test(text) ? `'${text}` : text;
      return `"${safe.replaceAll('"','""')}"`;
    }).join(','));
    return `\uFEFF${lines.join('\r\n')}\r\n`;
  }

  function recordTable(rows) {
    if (!rows.length) return '<div class="empty">暂无流水</div>';
    const body = rows.map(row => {
      const fund = isFundTrade(row.side) || ['fund','pending'].includes(row.kind);
      const name = fund ? fundDisplayName(row.code) || '场外基金' : row.code ? currentAccount().positions.find(item=>item.code===row.code)?.name || row.name || row.code : '';
      let detail = [name, row.code && name!==row.code ? row.code : '', strategyName(row.strategy || (isFundTrade(row.side) ? 'core' : ''))].filter(Boolean).join(' · ') || '账户资金';
      const account=currentAccount();
      if (row.side === 'assign') detail = [fund && name, row.code, `${strategyName(row.from_strategy)} → ${strategyName(row.strategy)}`].filter(Boolean).join(' · ');
      if (row.side === 'cash_transfer') detail = `${strategyName(row.from_strategy)} → ${strategyName(row.strategy)}`;
      if (row.side === 'cash_adjust') detail = `平台 ${custodyName(account,row.trading_account)} 对账差额${Object.entries(row.cash_allocations || {}).map(([key,amount])=>` · ${strategyName(key)||key} ${price(amount)} 元`).join('')}`;
      if (row.side === 'cash_assign') detail = recordCashAllocations(row);
      if (account.trading_accounts && (row.trading_account || !['cash_assign','cash_transfer'].includes(row.side))) {
        detail+=' · '+custodyName(account,row.trading_account);
        if (row.to_account) detail+=' → '+custodyName(account,row.to_account);
      }
      const pending = row.side === 'fund_pending';
      const immutable = ['fund_void','fund_redeem_pending','fund_redeem_void'].includes(row.side) || (row.side === 'fund_redeem' && row.pending_id);
      const waiting = !!currentAccount().fund_pending?.[row.id];
      const label = recordLabel(row);
      const edit = row.kind!=='pending' && !immutable && (!pending || waiting) ? `<button type="button" class="text-button" data-edit-event="${esc(row.id)}">修改</button>` : '';
      const recordPrice = recordAmount(row);
      return `<tr><td>${esc(row.date)}</td><td>${esc(label)}<small>${esc(detail)}</small>${edit}</td><td class="num">${fund ? fundShares(row.qty) : number(row.qty)}</td><td class="num">${price(recordPrice)}</td><td class="num">${price(row.fee)}</td></tr>`;
      }).join('');
      return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>类型 / 明细</th><th class="num">份额</th><th class="num">价格或金额 / 元</th><th class="num">手续费 / 元</th></tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function bindRecords(view, editRecordEvent) {
    const viewId = view.id;
    if (!recordDisplays.has(viewId)) recordDisplays.set(viewId, {expanded:false, all:false});
    const display = recordDisplays.get(viewId);
    const trigger = document.querySelector('#account-records-link');
    const section = document.querySelector('#account-records');
    function setExpanded(expanded) {
      display.expanded = expanded;
      section.hidden = !expanded;
      trigger.textContent = expanded ? '收起流水' : '查看流水';
      trigger.setAttribute('aria-expanded', String(expanded));
      if (expanded) {
        section.focus({preventScroll:true});
        section.scrollIntoView();
      } else {
        trigger.focus();
      }
    }
    trigger.onclick = () => setExpanded(section.hidden);
    document.querySelector('#close-records').onclick = () => setExpanded(false);
    const controls = section.querySelector('#record-filters');
    const fields = [...controls.querySelectorAll('input, select')];
    const allRecords = section.querySelector('#all-records');
    const exportButton = section.querySelector('#export-records');
    const results = section.querySelector('#record-results');
    const rows = recordRows(view);
    let filtered;
    function updateResults() {
      display.filters = Object.fromEntries(fields.map(field => [field.name, field.value]));
      filtered = filterRecords(rows, display.filters);
      allRecords.hidden = filtered.length <= 20;
      allRecords.textContent = display.all ? '只看最近 20 笔' : `查看全部 ${filtered.length} 笔`;
      exportButton.disabled = !filtered.length;
      section.querySelector('#record-count').textContent = `${filtered.length} 笔流水`;
      results.innerHTML = recordTable(display.all ? filtered : filtered.slice(0,20));
    }
    controls.oninput = updateResults;
    section.querySelector('#reset-record-filters').onclick = () => {
      for (const field of fields) field.value = '';
      updateResults();
    };
    allRecords.onclick = () => {
      display.all = !display.all;
      updateResults();
    };
    results.onclick = event => {
      const button = event.target.closest('[data-edit-event]');
      if (button) editRecordEvent(button);
    };
    exportButton.onclick = () => {
      const url = URL.createObjectURL(new Blob([recordCsv(filtered)], {type:'text/csv;charset=utf-8'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = `${viewId}-ledger-${shanghaiToday()}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    updateResults();
  }

  function showEntryReceipt(eventId, editing) {
    const account = currentAccount();
    const row = account.trades.find(trade => trade.id === eventId);
    const confirmation = editing ? '已修改' : '已保存';
    const parts = [confirmation];
    if (row) {
      parts.push(row.side === 'fund_redeem' && row.pending_id ? recordLabel(row) : SIDES[row.side], row.code);
      if (finite(row.qty)) {
        const shares = row.kind === 'fund' || isFundTrade(row.side) ? fundShares(row.qty) : number(row.qty);
        parts.push(`${shares} 份`);
      } else if (finite(row.amount)) {
        parts.push(`${money(row.amount)} 元`);
      }
      if (row.side === 'fund_redeem' && row.pending_id) parts.push(`到账 ${money(row.amount)} 元`);
      const owner = strategyName(row.strategy);
      parts.push(row.from_strategy ? `${strategyName(row.from_strategy)} → ${owner}` : owner);
      if (account.fund_pending?.[eventId]) parts.push('待确认');
      if (account.fund_redeem_pending?.[eventId]) parts.push('待到账');
    }
    entryReceipt = parts.filter(Boolean).join(' · ');
    const message = document.querySelector('#entry-receipt') || document.querySelector('#status');
    message.textContent = entryReceipt;
    return entryReceipt;
  }

  function recordsExpanded(view) {
    return recordDisplays.get(view.id)?.expanded || false;
  }

  function receiptText() {
    return entryReceipt;
  }

  function clearReceipt() {
    entryReceipt = '';
  }

  return { recordSection, bindRecords, recordsExpanded, receiptText, clearReceipt, showEntryReceipt };
}
