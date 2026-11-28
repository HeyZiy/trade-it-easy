import { esc, finite } from './common.js';

import { number, price, money, signed, percent, ratio, isAllocation, acceptsFunds, HOLDING_NAMES, ASSET_NAMES, fundShares, quotePrice, valuationLabel } from './dashboard-format.js';

import { chartSection } from './performance.js';

import { custodyProduct, custodySection } from './trading-accounts.js';

const ALLOCATION_TOLERANCE = 0.01;
const allocationToleranceNote = `${ALLOCATION_TOLERANCE*100} 个百分点内视为配平`;
const isAllocationBalanced = deviation => Math.abs(deviation) <= ALLOCATION_TOLERANCE + Number.EPSILON;

export function createPortfolioView({
  currentAccount, ownerViews, strategyName, fundDisplayName,
  recordSection, recordsExpanded, receiptText, pendingFundSection,
}) {
  let holdingKind = '';
  let coreSort = {key: null, dir: 1};

  function warnings(account) {
    const messages = [...(account?.error ? [account.error] : []), ...(account?.warnings || [])];
    const labels = {
      '投资账户尚未初始化':'尚未设置期初资产。',
      '最新成绩待重新估值，曲线保留最近有效数据。':'收盘收益待更新，暂显示最近有效记录。'
    };
    return [...new Set(messages.map(message=>labels[message] || message))].map(message => `<div class="warning">${esc(message)}</div>`).join('');
  }

  function summaryPage() {
    const account = currentAccount();
    return `${warnings(account)}${account.can_record ? accountDetails(account) : chartSection(account)}`;
  }

  function entryActions(view) {
    const expanded = recordsExpanded(view);
    const cashMenu = [
      view.id === 'etf' || view.holding_only ? '' : {action:'data-cash-rebalance', label:'现金再平衡', hint:'把各策略现金调到接近目标预算'},
      {action:'data-open-cash', label:'分配现金', hint:'把账户现金分派给各策略'},
      {action:'id:cash-transfer', label:currentAccount().trading_accounts?'策略间划转现金':'划转现金', hint:'策略之间挪现金，不动持仓'},
    ].filter(Boolean);
    const menu = items => `<div class="action-menu"><button type="button" class="text-button action-menu-toggle" aria-haspopup="true" aria-expanded="false">现金 ▾</button><div class="action-menu-list" hidden>${items.map(item =>
      `<button type="button" class="action-menu-item" ${item.action.startsWith('id:') ? `id="${item.action.slice(3)}"` : item.action}><span>${esc(item.label)}</span><small>${esc(item.hint)}</small></button>`).join('')}</div></div>`;
    return `<div class="account-entry-bar"><div class="allocation-actions">
      <button type="button" id="manual-entry" class="primary-button">＋ 记一笔</button>
      <button type="button" id="account-records-link" class="secondary-button" aria-controls="account-records" aria-expanded="${expanded}">${expanded ? '收起流水' : '查看流水'}</button>
      ${view.id === 'etf' ? '<button type="button" class="secondary-button" data-holding-filter="" aria-controls="account-holdings">查看持仓</button>' : ''}
      <button type="button" class="secondary-button" data-open-fund-order="">基金申购</button>
      ${view.id === 'etf' ? '<button type="button" id="dca-settings" class="text-button">定投计划</button>' : ''}
      ${cashMenu.length ? menu(cashMenu) : ''}
      <button type="button" id="position-transfer" class="text-button">持仓划转</button>
    </div><p id="entry-receipt" class="subtle" role="status">${esc(receiptText())}</p></div>`;
  }

  function assetComposition(account) {
    const assets = Object.entries(account.asset_allocation || {})
      .filter(([, amount]) => amount === null || amount > 0);
    const sized = account.equity > 0 && assets.every(([, amount]) => finite(amount));
    const pending = account.pending_fund_value || 0;
    const bar = sized ? `<div class="asset-strip" aria-hidden="true">${assets.map(([kind, amount]) =>
      `<i class="asset-${kind}" style="flex-grow:${amount / account.equity}"></i>`).join('')}${pending ? `<i class="asset-pending" style="flex-grow:${pending / account.equity}"></i>` : ''}</div>` : '';
    const labels = assets.map(([kind, amount]) => {
      const value = finite(amount) ? `${money(amount)} 元` : '待估值';
      const detail = kind === 'cash' && finite(amount) ? `账面现金 ${money(account.cash)} 元 · 货币基金 ${money(amount - account.cash)} 元` : value;
      const label = `<i class="asset-${kind}"></i>${ASSET_NAMES[kind]} <b>${sized ? ratio(amount, account.equity) : value}</b>`;
      return `<button type="button" data-holding-filter="class:${kind}" aria-controls="account-holdings" title="${detail}">${label}</button>`;
    }).join('');
    return `<div class="asset-composition" role="group" aria-label="资产构成">${bar}<div class="asset-labels">${labels}
      ${pending ? `<a href="#fund-pending"><i class="asset-pending"></i>申购在途 <b>${money(pending)} 元</b></a>` : ''}
    </div></div>`;
  }

  function accountSummary(account) {
    const latest = (account.history || []).findLast(row => finite(row.nav) && row.nav > 0);
    const pnl = finite(account.pnl) ? `${account.pnl > 0 ? '+' : ''}${money(account.pnl)}` : '待估值';
    const cash = ownerViews().map(({id, name}) =>
      `<div><dt>${esc(name)}</dt><dd>${money(account.cash_by_strategy[id])}</dd></div>`).join('');
    return `<section class="reconciliation" aria-label="账户摘要">
      <div class="reconciliation-top">
        <div><span>总资产 / 元</span><strong>${finite(account.equity) ? money(account.equity) : '待估值'}</strong><small>${esc(valuationLabel(account.valuation))}</small></div>
        <div><span>累计盈亏 / 元</span><b class="${account.pnl > 0 ? 'is-gain' : account.pnl < 0 ? 'is-loss' : ''}">${pnl}</b></div>
        <div><span>累计收益率</span><b>${percent(latest?.nav)}</b>${latest ? `<small>收盘 · ${esc(latest.date)}</small>` : ''}</div>
        <div><span>账面现金 / 元</span><button type="button" id="account-cash-toggle" aria-label="查看各策略现金明细" aria-controls="account-cash-detail" aria-expanded="false"><b>${money(account.cash)}</b></button></div>
      </div>
      <dl id="account-cash-detail" class="reconciliation-cash" aria-label="各策略现金（元）" hidden>${cash}</dl>
      ${assetComposition(account)}
      <details class="calculation-note"><summary>盈亏对账</summary><p>期初资产 ${money(account.initial_equity)} 元 · 净入金 ${money(account.net_contributed - account.initial_equity)} 元 · 累计投入 ${money(account.net_contributed)} 元</p><p>累计盈亏等于总资产减累计投入。收益率按有效收盘净值计算，扣除入出金影响。</p></details>
    </section>`;
  }

  function positionActions(code, strategy, held=true, suggestion=null) {
    const button = side => `<button type="button" class="text-button" ${suggestion?.side===side ? `data-fill="${suggestion.index}"` : `data-position-side="${side}" data-code="${esc(code)}" data-strategy="${esc(strategy)}"`}>${side==='buy' ? '买入' : '卖出'}</button>`;
    return `<div class="position-actions">${button('buy')}${held ? button('sell') : ''}</div>`;
  }

  function fundActions(code, held, strategy='') {
    if (strategy && !ownerViews().find(row => row.id === strategy)?.accepts_funds) return '';
    const action = (side, label) => `<button type="button" class="text-button" data-fund-side="${side}" data-code="${esc(code)}" data-strategy="${esc(strategy)}">${label}</button>`;
    const dca = strategy ? `<button type="button" class="text-button" data-fund-dca="${esc(code)}" data-strategy="${esc(strategy)}">定投</button>` : '';
    return `<div class="position-actions">${action('fund_buy','申购')}${held ? action('fund_redeem','赎回') : ''}${dca}</div>`;
  }

  function fundMark(row) {
    return finite(row.nav) ? `${row.nav.toLocaleString('zh-CN',{maximumFractionDigits:8})}<small>净值日 ${esc(row.date)}</small>` : '<small class="price-pending">待估值</small>';
  }

  function holdingOwnership(code, owners, kind) {
    if (!owners.length) return '';
    const ownerRows = owners.map(({strategy, shares}) => {
      const transfer = `<button type="button" class="text-button" data-holding-transfer data-code="${esc(code)}" data-kind="${kind}" data-strategy="${esc(strategy)}" aria-label="从${esc(strategyName(strategy))}划转 ${esc(code)}">划转</button>`;
      return `<div class="holding-owner"><span>${esc(strategyName(strategy))}${owners.length > 1 ? ` · ${shares} 份` : ''}</span>${transfer}</div>`;
    }).join('');
    return owners.length === 1 ? ownerRows : `<details class="holding-owners"><summary>${owners.length} 个策略</summary>${ownerRows}</details>`;
  }

  function assetClassButton(row, kind) {
    return `<button type="button" class="text-button" data-classify-asset data-code="${esc(row.code)}" data-kind="${kind}" data-category="${row.asset_class || 'unknown'}" aria-label="设置 ${esc(row.code)} 的资产类别">${row.asset_class && row.asset_class !== 'unknown' ? ASSET_NAMES[row.asset_class] : '设置分类'}</button>`;
  }

  function accountDetails(account) {
    const assetFilters = Object.keys(account.asset_allocation || {}).map(kind => `<option value="class:${kind}">${ASSET_NAMES[kind]}</option>`).join('');
    const filters = Object.keys(account.holdings_by_kind).map(kind => `<option value="${kind}">${HOLDING_NAMES[kind]}</option>`).join('');
    const rows = account.positions.map(row => {
      const owners = account.strategy_positions.filter(item=>item.code===row.code && item.count>0);
      const ownership = holdingOwnership(row.code, owners.map(owner => ({strategy:owner.strategy, shares:number(owner.count)})), 'listed') + custodyProduct(account,'listed',row.code) + assetClassButton(row, 'listed');
      return `<tr data-holding-kind="${row.kind}" data-asset-class="${row.asset_class || 'unknown'}"><td>${esc(row.name)}<small>${esc(row.code)}<span class="holding-mobile-count"> · ${number(row.count)} 份</span></small>${ownership}</td><td class="num">${number(row.count)}</td><td class="num">${quotePrice(row)}</td><td class="num">${finite(row.market_value) ? money(row.market_value) : '待估值'}</td><td class="num">${ratio(row.market_value,account.equity)}</td><td>${positionActions(row.code, owners.length===1 ? owners[0].strategy : '')}</td></tr>`;
    }).join('');
    const fundRows = Object.values(account.fund_positions || {}).map(row => {
      const owners = Object.entries(account.strategy_fund_positions || {})
        .filter(([, positions]) => positions[row.code]?.qty > 0)
        .map(([strategy, positions]) => ({strategy, shares:fundShares(positions[row.code].qty)}));
      const ownership = holdingOwnership(row.code, owners, 'fund') + custodyProduct(account,'fund',row.code) + assetClassButton(row, 'fund');
      return `<tr data-holding-kind="fund" data-asset-class="${row.asset_class || 'unknown'}"><td>${esc(fundDisplayName(row.code) || '场外基金')}<small>${esc(row.code)}<span class="holding-mobile-count"> · ${fundShares(row.qty)} 份</span></small>${ownership}</td><td class="num">${fundShares(row.qty)}</td><td class="num">${fundMark(row)}</td><td class="num">${finite(row.market_value) ? money(row.market_value) : '待估值'}</td><td class="num">${ratio(row.market_value,account.equity)}</td><td>${fundActions(row.code,true)}</td></tr>`;
    }).join('');
    return `${accountSummary(account)}${entryActions(account)}
      ${strategyAllocation(account)}${chartSection(account)}${pendingFundSection(account)}
      <section id="account-holdings" class="section" aria-labelledby="account-holdings-title" tabindex="-1"><div class="section-heading"><h2 id="account-holdings-title">账户持仓</h2><select id="holding-filter" aria-label="持仓类别"><option value="">全部持仓</option><optgroup label="资产类别">${assetFilters}</optgroup><optgroup label="产品类型">${filters}</optgroup></select></div>
        <p id="cash-class-detail" class="subtle" hidden>账面现金 ${money(account.cash)} 元 · 货币基金 ${finite(account.asset_allocation?.cash) ? `${money(account.asset_allocation.cash - account.cash)} 元` : '待估值'}</p>
        <div class="table-wrap"><table class="account-holdings-table"><thead><tr><th scope="col">标的</th><th scope="col" class="num">份额</th><th scope="col" class="num">价格 / 元</th><th scope="col" class="num">市值 / 元</th><th scope="col" class="num">资产占比</th><th scope="col">记账</th></tr></thead><tbody>${rows}${fundRows}<tr id="holdings-empty" hidden><td colspan="6" class="empty">暂无持仓</td></tr></tbody></table></div></section>
      ${custodySection(account, {strategyName, fundDisplayName})}${recordSection(account)}`;
  }

  function holdingAreaPage(area) {
    const products = [...area.positions.map(row=>({...row,kind:'listed'})),
      ...Object.values(area.fund_positions || {}).map(row=>({...row,kind:'fund'}))];
    const rows = products.map(row=>{
      const fund=row.kind==='fund';
      const name=fund ? fundDisplayName(row.code) : row.name;
      return `<tr><td>${esc(name || row.code)}<small>${esc(row.code)}</small></td><td>${fund ? fundShares(row.qty) : number(row.count)}</td><td>${money(row.market_value)}</td><td>${fund ? fundActions(row.code,true,area.id) : positionActions(row.code,area.id)} ${holdingOwnership(row.code,[{strategy:area.id,shares:fund ? fundShares(row.qty) : number(row.count)}],row.kind)}</td></tr>`;
    }).join('');
    return `<div class="stats">${[['资产',area.assets],['持仓与在途',area.invested],['现金',area.cash],['累计盈亏',area.pnl]].map(([label,value])=>`<div class="stat"><span>${label} / 元</span><b>${money(value)}</b></div>`).join('')}</div>
      ${entryActions(area)}<section class="section"><div class="section-heading"><h2>待定持仓</h2></div>
      ${rows ? `<div class="table-wrap"><table><thead><tr><th>标的</th><th>份额</th><th>市值 / 元</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="empty">暂无待定持仓</p>'}</section>
      ${pendingFundSection(area,[])}${recordSection(area)}`;
  }

  function fundedStrategyPage(strategy) {
    if (strategy.holding_only) return holdingAreaPage(strategy);
    const stats = [[finite(strategy.budget) ? money(strategy.budget) : finite(strategy.budget_pct)?'待估值':'待设置','目标资产 / 元'],[money(strategy.invested),strategy.pending_fund_value ? '持仓与在途 / 元' : '持仓市值 / 元'],[`${strategy.pnl>0?'+':''}${money(strategy.pnl)}`,'累计盈亏 / 元'],[money(strategy.cash),'策略现金 / 元']];

    const canRecord = currentAccount().can_record;
    const status = strategy.status && strategy.status!=='已分配' ? `<p class="subtle">${esc(strategy.status)}</p>` : '';
    const allocation = isAllocation(strategy);
    const fundBuy = acceptsFunds(strategy) ? `<button type="button" data-fund-side="fund_buy" data-code="" data-strategy="${esc(strategy.id)}" class="text-button">场外申购</button>` : '';
    const settings = allocation ? `<a href="/settings/${esc(strategy.id)}" class="text-button">编辑组合配比</a>`
      : strategy.id==='rotation' ? '<a href="/settings/rotation" class="text-button">编辑标的池</a>' : '';
    const actions = canRecord && (fundBuy || settings) ? `<div class="core-actions">${fundBuy}${settings}</div>` : '';
    const pending = acceptsFunds(strategy) ? pendingFundSection(strategy, currentAccount().dca?.recorded_today_by_strategy?.[strategy.id]) : '';
    return `<div class="stats">${stats.map(([value,label])=>`<div class="stat"><span>${label}</span><b>${esc(value)}</b></div>`).join('')}</div>
      ${canRecord ? entryActions(strategy) : ''}
      <section class="section"><div class="section-heading"><h2>${allocation ? '持仓与目标配比' : '持仓与调仓建议'}</h2>${actions}</div>${status}${strategyPositionTable(strategy)}</section>
      ${pending}${chartSection(strategy)}${canRecord ? recordSection(strategy) : ''}`;
  }

  function rotationPositionTable(strategy) {
    const suggestions = strategy.suggestions || [];
    const codes = [...new Set([...strategy.positions.map(row=>row.code),...suggestions.map(row=>row.code)])];
    const status = `<p class="subtle">${esc(strategy.advice_status || '等待交易日更新')}${strategy.signal_date ? ` · 信号日期 ${esc(strategy.signal_date)}` : ''}</p>`;
    if (!codes.length) return `${status}<div class="empty">暂无持仓与调仓信号</div>`;
    const rows = codes.map(code=>{
      const position = strategy.positions.find(row=>row.code===code);
      const index = suggestions.findIndex(row=>row.code===code);
      const item = suggestions[index];
      const signal = item ? `<strong class="signal-label ${item.side}">${item.side==='sell' ? '减持' : '增持'}约 ${number(item.qty)} 份</strong><small>${money(item.amount)} 元 · ${esc(item.reason)}</small>${item.cash_note ? `<small class="cash-note">${esc(item.cash_note)}</small>` : ''}` : '<span class="subtle">—</span>';
      return `<tr${item ? ' class="allocation-attention"' : ''}><td>${esc(position?.name || item?.name || code)}<small>${esc(code)}</small><span class="allocation-mobile-values">持有 ${number(position?.count || 0)} 份<br>${money(position?.market_value || 0)} 元</span></td><td>${number(position?.count || 0)}</td><td>${position ? quotePrice(position) : price(item?.price)}</td><td>${money(position?.market_value || 0)}</td><td>${signal}</td><td>${positionActions(code,strategy.id,(position?.count || 0)>0,item ? {...item,index} : null)}${item ? `<button type="button" class="text-button signal-dismiss" data-dismiss="${index}">本轮忽略</button>` : ''}</td></tr>`;
    }).join('');
    return `${status}<div class="table-wrap"><table class="allocation-table rotation-allocation-table"><thead><tr><th>标的</th><th>持有份额</th><th>参考价 / 元</th><th>持仓市值 / 元</th><th>本轮信号</th><th>记账</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function statusBar(account) {
    if (!account?.can_record) return '';
    const value = amount => finite(amount) ? money(amount) : '待估值';
    const latest = (account.history || []).filter(row => finite(row.nav) && row.nav > 0).at(-1);
    const gain = latest ? `${signed((latest.nav - 1) * 100)}%` : '—';
    const pnl = finite(account.pnl) ? `${account.pnl > 0 ? '+' : ''}${value(account.pnl)}` : '—';
    return `<div class="status-bar-item"><span>总资产</span><b>${value(account.equity)}</b></div>
      <div class="status-bar-item"><span>累计盈亏</span><b class="${account.pnl > 0 ? 'is-gain' : account.pnl < 0 ? 'is-loss' : ''}">${pnl}</b></div>
      <div class="status-bar-item"><span>累计收益率</span><b>${gain}</b></div>
      <div class="status-bar-item"><span>现金</span><b>${value(account.cash)}</b></div>`;
  }

  function bindReconciliation(account) {
    const filter = document.querySelector('#holding-filter');
    const rows = [...document.querySelectorAll('[data-holding-kind]')];
    function showHoldings(kind) {
      const assetClass = kind.startsWith('class:') ? kind.slice(6) : '';
      holdingKind = Object.hasOwn(account.holdings_by_kind, kind) || Object.hasOwn(account.asset_allocation || {}, assetClass) ? kind : '';
      filter.value = holdingKind;
      for (const row of rows) row.hidden = !!holdingKind && (assetClass ? row.dataset.assetClass !== assetClass : row.dataset.holdingKind !== holdingKind);
      document.querySelector('#cash-class-detail').hidden = holdingKind !== 'class:cash';
      document.querySelector('#holdings-empty').hidden = rows.some(row => !row.hidden);
    }
    function jumpTo(id) {
      const section = document.getElementById(id);
      section.focus({preventScroll:true});
      section.scrollIntoView();
    }
    showHoldings(holdingKind);
    filter.onchange = () => showHoldings(filter.value);
    for (const button of document.querySelectorAll('[data-holding-filter]')) {
      button.onclick = () => { showHoldings(button.dataset.holdingFilter); jumpTo('account-holdings'); };
    }
    const cash = document.querySelector('#account-cash-detail');
    document.querySelector('#account-cash-toggle').onclick = event => {
      cash.hidden = !cash.hidden;
      event.currentTarget.setAttribute('aria-expanded', String(!cash.hidden));
    };
  }

  function strategyAllocation(account) {
    const rows = ownerViews().map(row => ({...row, target:row.holding_only ? row.residual_pct : row.budget_pct}));
    const bar = row => {
      const actual = finite(row.actual_pct) ? row.actual_pct * 100 : null;
      const target = finite(row.target) ? row.target * 100 : null;
      const gap = row.deviation;
      const deviation = !finite(gap) ? '' : gap === 0 ? '已配平' : `${gap > 0 ? '超配' : '低配'} ${money(Math.abs(gap))} 元`;
      const emphasized = actual !== null && target !== null && !isAllocationBalanced(row.actual_pct - row.target);
      const name = `<button type="button" class="allocation-strategy-link" data-strategy-tab="${esc(row.id)}">${esc(row.name)}</button>`;
      const pending = account.strategy_pending_fund_value?.[row.id] || 0;
      const positions = finite(row.invested) ? `${money(row.invested - pending)} 元` : '待估值';
      return `<div class="allocation-row">
        <div class="allocation-row-top">${name}<b>${finite(row.assets) ? `${money(row.assets)} 元` : '待估值'}</b></div>
        <div class="allocation-row-labels"><span>实际 ${actual === null ? (finite(account.equity) ? '—' : '待估值') : `${actual.toFixed(1)}%`}</span><span>${target === null ? '目标未设置' : `${row.holding_only ? '预算余量' : '目标'} ${target.toFixed(1)}%`}</span></div>
        <div class="allocation-track" aria-hidden="true">${actual === null ? '' : `<div class="allocation-fill allocation-${esc(row.id)}" style="width:${Math.max(0, Math.min(100, actual))}%"></div>`}${target === null ? '' : `<i class="allocation-target" style="left:${target}%"></i>`}</div>
        <div class="allocation-row-foot"><span>持仓 ${positions} · 现金 ${money(row.cash)} 元${pending ? ` · 在途 ${money(pending)} 元` : ''}</span><strong class="${emphasized ? 'allocation-gap' : ''}">${deviation}</strong></div>
      </div>`;
    };
    const layer = (title, hint, groupRows) => `<div class="allocation-layer"><div class="allocation-layer-head"><h3>${title}</h3><span>${hint}</span></div>${groupRows.map(bar).join('')}</div>`;
    const allocation = rows.filter(row => isAllocation(row));
    const satellites = rows.filter(row => !row.holding_only && !isAllocation(row));
    const bars = [layer('配置层', '按配置目标低频复核', allocation),
      satellites.length ? layer('量化卫星', '小预算试验，错了砍预算', satellites) : '',
      rows.filter(row=>row.holding_only).map(bar).join('')].join('');
    return `<section class="section strategy-panel" aria-labelledby="strategy-allocation-title">
      <div class="section-heading"><h2 id="strategy-allocation-title">策略配比</h2>
        <div class="allocation-actions"><button type="button" data-cash-rebalance class="text-button">现金再平衡</button>${account.can_record ? '<a href="/settings/budgets" class="text-button">编辑目标占比</a>' : ''}<a class="text-button" href="/api/etf/export">导出配置与持仓</a><a class="text-button" href="https://github.com/HeyZiy/trade-it-easy/blob/main/docs/ALLOCATION.md" rel="noopener" target="_blank">口径说明 ↗</a></div>
      </div><div class="strategy-allocation-rows">${bars}</div></section>`;
  }

  function allocationProductRow(row, strategyId) {
    const fund = row.kind === 'fund';
    const held = fund ? !!row.qty : row.count > 0;
    const name = fund ? fundDisplayName(row.code) || '场外基金' : row.name;
    const shares = fund ? fundShares(row.qty) : number(row.count);
    const nav = row.nav?.toLocaleString('zh-CN', {maximumFractionDigits:8});
    const note = [row.code, fund && (row.pending_amount ? `待确认 ${money(row.pending_amount)} 元` : !held && '暂无确认持仓')].filter(Boolean).join(' · ');
    const mark = fund ? (held ? fundMark(row) : '—') : quotePrice(row);
    const value = held ? (finite(row.market_value) ? money(row.market_value) : '待估值') : '—';
    const action = fund
      ? fundActions(row.code, held, strategyId)
      : positionActions(row.code, strategyId, held);
    const mobile = held ? `<small class="allocation-product-mobile">${shares} 份${fund ? (nav ? ` · 净值 ${nav}<br>${esc(row.date)}` : ' · 待估值') : ''}</small>` : '';
    return `<tr class="allocation-product-row">
      <td>${esc(name)}${note ? `<small>${esc(note)}</small>` : ''}${mobile}</td>
      <td>${shares ?? '—'}</td><td>${fund || held ? mark : '—'}</td><td>${value}</td>
      <td></td><td><span class="allocation-product-mobile">${held ? `${value}${finite(row.market_value) ? ' 元' : ''}` : '—'}</span></td><td>${action}</td>
    </tr>`;
  }

  function sortAllocationGroups(groups, key, dir) {
    const cash = groups.filter(group => group.id === 'CASH');
    const rest = groups.filter(group => group.id !== 'CASH');
    if (key) {
      const value = group => key === 'name' ? (fundDisplayName(group.name_fund_code) || group.name || '')
        : key === 'weight' ? group.weight
        : key === 'actual' ? group.market_value
        : group.absolute_deviation;
      const missing = v => v === null || v === undefined || (typeof v === 'number' && !finite(v));
      rest.sort((a, b) => {
        const va = value(a), vb = value(b), ma = missing(va), mb = missing(vb);
        if (ma !== mb) return ma ? 1 : -1;
        if (ma && mb) return 0;
        return (key === 'name' ? String(va).localeCompare(String(vb), 'zh') : va - vb) * dir;
      });
    }
    return [...rest, ...cash];
  }

  function coreSortTh(label, key) {
    const active = coreSort.key === key;
    const arrow = active ? (coreSort.dir > 0 ? ' ▲' : ' ▼') : '';
    const sortValue = active ? (coreSort.dir > 0 ? 'ascending' : 'descending') : 'none';
    return `<th class="sortable${active ? ' sorted' : ''}" data-sort-key="${key}" tabindex="0" role="columnheader" aria-sort="${sortValue}" title="点击或按回车排序" aria-label="${label}，点击或按回车切换排序">${label}${arrow}</th>`;
  }

  function refreshCoreTable(strategy, bindAccountActions) {
    const wrap = document.querySelector('.core-allocation-table')?.closest('.table-wrap');
    if (!wrap) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = strategyPositionTable(strategy);
    wrap.replaceWith(tmp.firstElementChild);
    bindAccountActions(strategy);
    bindCoreSort(strategy, bindAccountActions);
  }

  function bindCoreSort(strategy, bindAccountActions) {
    document.querySelectorAll('.core-allocation-table th[data-sort-key]').forEach(th => {
      const activate = () => {
        const key = th.dataset.sortKey;
        coreSort = coreSort.key === key ? {key, dir: -coreSort.dir} : {key, dir: key === 'deviation' ? -1 : 1};
        refreshCoreTable(strategy, bindAccountActions);
      };
      th.onclick = activate;
      th.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); }
      };
    });
  }

  function strategyPositionTable(strategy) {
    if (!isAllocation(strategy)) return rotationPositionTable(strategy);
    if (!strategy.allocation_groups.length) return '<div class="empty">请在「编辑组合配比」中添加配置项。</div>';
    const budget = strategy.allocation_budget;
    const assets = strategy.allocation_assets;
    const pct=value=>finite(value)?`${(value*100).toFixed(2)}%`:'—';
    const thresholds = strategy.rebalance_thresholds;
    const suggestions = strategy.suggestions || [];
    const thresholdNote = thresholds ? `偏离达到 ${(thresholds.single*100).toFixed(0)} 个百分点或该项目标权重的 ${(thresholds.relative*100).toFixed(0)}%（先达先触发）时重点高亮；破带且缺口≥${thresholds.trade_amount.toFixed(0)} 元、可换算整手时，提供参考份额。` : '偏离提醒阈值暂不可用。';
    let basisNote = '请先在账户设置中填写策略目标占比。';
    if (budget > 0) {
      basisNote = '目标金额 = 配置目标资产 × 配置项权重。配比偏离按配置目标资产计算。';
    } else if (finite(strategy.budget_pct)) {
      basisNote = '账户估值待补齐后计算目标金额。';
    }
    const rows=sortAllocationGroups(strategy.allocation_groups, coreSort.key, coreSort.dir).map(group=>{
      const {id:code, market_value:actual, weight, products,
        target_value:target, gap_amount:gap, absolute_deviation:deviation,
        relative_deviation:relative, absolute_review:absReview, relative_review:relReview,
        trade_eligible:tradeEligible} = group;
      const name = fundDisplayName(group.name_fund_code) || group.name;
      const position = products.length === 1 ? products[0] : null;
      const fund = position?.kind === 'fund';
      const held = fund ? position.qty > 0 : position?.count > 0;
      const shares = fund ? fundShares(position.qty) : number(position?.count);
      const mark = held ? (fund ? fundMark(position) : quotePrice(position)) : '—';
      const review = absReview || relReview;
      const level = review ? 'strong' : tradeEligible ? 'attention' : '';
      const index = suggestions.findIndex(item=>item.code===code);
      const item = suggestions[index];
      let difference;
      if (!finite(gap)) {
        const status = finite(strategy.budget_pct) ? '待估值' : '待设置目标';
        difference = `<span class="subtle">${status}</span>`;
      } else if (!review && isAllocationBalanced(deviation)) {
        difference = '<span class="subtle">已配平</span>';
      } else {
        const deviationText = finite(relative) ? `相对偏离 ${pct(relative)}` : `偏离 ${(deviation*100).toFixed(2)} 个百分点`;
        difference = `<strong class="allocation-gap">${gap>0 ? '超配' : '低配'} ${money(Math.abs(gap))} 元</strong><small>${deviationText}</small>`;
      }
      const reference = item
        ? `<small>参考${item.side==='sell' ? '减持' : '增持'}约 ${number(item.qty)} 份</small>${item.cash_note ? `<small class="cash-note">${esc(item.cash_note)}</small>` : ''}`
        : tradeEligible && group.reference_price_missing ? '<small class="price-pending">参考份额待估值</small>' : '';
      let actions = '', details = '', productRows = '';
      if (code === 'CASH') {
        actions = '<button type="button" class="text-button" data-open-cash>分配现金</button>';
      } else if (position) {
        actions = fund ? fundActions(position.code, held, strategy.id)
          : positionActions(position.code, strategy.id, held, item ? {...item,index} : null);
        const productName = fund ? fundDisplayName(position.code) : position.name;
        const notes = [productName !== name && productName, position.code,
          position.pending_amount && `待确认 ${money(position.pending_amount)} 元`].filter(Boolean);
        details = `<small>${esc(notes.join(' · '))}</small>`;
        if (held) details += `<div class="allocation-product-mobile"><small>${shares} 份${fund ? ` · ${mark}` : ''}</small></div>`;
      } else {
        details = '<small>组内合计</small>';
        productRows = products.map(product => allocationProductRow(product, strategy.id)).join('');
      }
      return `<tr data-allocation-code="${esc(code)}"${level ? ` class="allocation-${level}"` : ''}><td>${esc(name)}${group.outside ? '<small>目标外持仓</small>' : ''}${details}</td><td>${shares}</td><td>${mark}</td><td>${money(actual)}<small>配置内 ${pct(assets>0 && finite(actual)?actual/assets:null)}</small></td><td>${money(target)}<small>目标权重 ${pct(weight)}</small></td><td>${difference}<span class="allocation-mobile-values">现 ${money(actual)}<br>目标 ${money(target)}</span>${reference}</td><td>${actions}</td></tr>${productRows}`;
    }).join('');
    return `<div class="table-wrap"><table class="allocation-table core-allocation-table"><thead><tr>${coreSortTh('产品与配置','name')}<th>份额</th><th>价格 / 元</th>${coreSortTh('当前金额 / 元','actual')}${coreSortTh('目标金额 / 元','weight')}${coreSortTh('配比偏离','deviation')}<th>记账</th></tr></thead><tbody>${rows}</tbody></table></div><details class="calculation-note"><summary>配比说明</summary><p>${basisNote}未达到重点高亮阈值时，${allocationToleranceNote}。${thresholdNote} 多产品配置按组内合计金额判断偏离。</p></details>`;
  }

  return { summaryPage, fundedStrategyPage, statusBar, bindCoreSort, bindReconciliation };
}
