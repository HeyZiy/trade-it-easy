const number = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:0}) : '—';
const price = value => finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:4}) : '—';
const money = value => finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
const signed = value => { const rounded=Number(value.toFixed(2)); return `${rounded>0?'+':''}${rounded.toFixed(2)}`; };
const percent = nav => finite(nav) && nav > 0 ? `${signed((nav - 1) * 100)}%` : '—';
const excess = (nav, base) => finite(nav) && finite(base) ? `${signed((nav - base) * 100)} 个百分点` : '—';
const ratio = (part, whole) => finite(part) && finite(whole) && whole > 0 ? `${(part / whole * 100).toFixed(1)}%` : '—';
const accountName = account => account?.name || '账户';
const reportDate = value => /^\d{8}$/.test(value) ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6)}` : value;
const timestamp = day => Date.parse(`${day}T00:00:00Z`);
const isoDay = value => /^\d{4}\/\d{1,2}\/\d{1,2}/.test(value) ? value.replace(/(\d{4})\/(\d{1,2})\/(\d{1,2}).*/, (_,y,m,d) => `${y}-${m.padStart(2,'0')}-${d.padStart(2,'0')}`) : value;
const SIDES = {buy:'买入', sell:'卖出', dividend:'分红', deposit:'入金', withdraw:'出金', assign:'持仓划转', cash_assign:'现金分配', cash_adjust:'现金校准', cash_transfer:'现金划转', fund_buy:'场外申购', fund_redeem:'场外赎回', fund_pending:'场外申购', fund_confirm:'场外申购确认', fund_void:'申购作废'};
SIDES.custody_transfer = '账户间划转';
function ownerViews() {
  const area = data.accounts[0]?.holding_area;
  return area ? [...data.strategies, area] : data.strategies;
}
const strategyOptions = (strategies = ownerViews()) => strategies.map(({id, name}) =>
  `<option value="${esc(id)}">${esc(name)}</option>`).join('');
const strategyName = id => ownerViews().find(row=>row.id===id)?.name;
const isAllocation = strategy => strategy.kind === 'allocation';
const acceptsFunds = strategy => strategy.accepts_funds;
const HOLDING_NAMES = {etf:'ETF', stock:'股票', other:'其他持仓', fund:'场外基金'};
const ASSET_NAMES = {equity:'权益', bond:'债券', commodity:'商品', cash:'现金类', mixed:'混合', unknown:'待分类'};
const isFundTrade = side => typeof side === 'string' && side.startsWith('fund_');
Object.assign(SIDES, {fund_redeem_pending:'场外赎回预登记', fund_redeem_void:'赎回作废'});
const fundShares = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:6}) : '—';
const REPORT_PREFIX = {rotation:'etf_rotation_advice_', quality:'quality_pool_advice_'};
const ACCOUNT_LOAD_FAILED = '账户数据读取失败，请检查服务器日志。';
const ALLOCATION_TOLERANCE = 0.01;
const allocationToleranceNote = `${ALLOCATION_TOLERANCE*100} 个百分点内视为配平`;
const isAllocationBalanced = deviation => Math.abs(deviation) <= ALLOCATION_TOLERANCE + Number.EPSILON;
let data, selected = 0, chartRange = 'all', chartObserver;
let coreSort = {key: null, dir: 1};
let selectedReport, reportRequest;
let overviewRequest = 0, appliedOverviewRequest = 0;
const recordDisplays = new Map();
let entryReceipt = '';
let fundVoidReceipt;
let holdingKind = '';
const reportCache = new Map();
const requestedFundNames = new Set();
const requestedSecurityNames = new Set();
let quoteRequestPending = false, quotePollTimer, quotePolling = false, quoteError = '';
const fundDisplayName = code => data.accounts[0].fund_names?.[code];
const shanghaiToday = () => new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const quotePrice = row => finite(row.close) ? price(row.close) : '<small class="price-pending">待估值</small>';
const refreshInProgress = state => state?.running || state?.funds?.running;
const valuationLabel = valuation => valuation?.date ? `收盘估值 · ${valuation.date}` : '估值日待确认';
const openEntry = createEntryDialog({
  ownerViews, strategyOptions, strategyName, acceptsFunds,
  number, money, fundShares, fundDisplayName, shanghaiToday,
  currentAccount: () => data.accounts[0], reloadAccount, showEntryReceipt,
});

function updateQuoteControls() {
  const account = data.accounts[0];
  const active = selected===0 ? account : ownerViews()[selected-1];
  document.querySelector('#quote-panel').hidden = !account.can_record || (selected!==0 && active?.account_id!=='etf');
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
  status.textContent = selected === 0 && !running ? '' : note;
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
  if (refreshInProgress(data.accounts[0]?.quote_refresh)) quotePollTimer = setTimeout(pollQuotes, 1500);
}

function clearQuoteRefreshPending() {
  const state = data.accounts[0]?.quote_refresh;
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

document.querySelector('#quote-refresh').onclick = async () => {
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
};

function warnings(account) {
  const messages = [...(account?.error ? [account.error] : []), ...(account?.warnings || [])];
  const labels = {
    '投资账户尚未初始化':'尚未设置期初资产。',
    '最新成绩待重新估值，曲线保留最近有效数据。':'收盘收益待更新，暂显示最近有效记录。'
  };
  return [...new Set(messages.map(message=>labels[message] || message))].map(message => `<div class="warning">${esc(message)}</div>`).join('');
}

function chartSection(account) {
  const history = chartPoints(account?.history || [], 'all', false);
  const hasHistory = history.length > 0;
  const hasBenchmark = !!account?.benchmark_name && history.some(row=>finite(row.benchmark_nav) && row.benchmark_nav>0);
  const start = history[0]?.date || account?.start_date;
  let basis = '';
  const lastClose = history.findLast(row => finite(row.nav) && row.nav > 0);
  if (account?.benchmark_name && hasHistory) {
    basis = `基准：${esc(account.benchmark_name)}${finite(lastClose.benchmark_nav) ? '' : ' · 待更新'}`;
  }
  else if (start) basis = `起始日期：${esc(start)}`;
  return `<section class="section chart-section" aria-labelledby="chart-title">
    <div class="section-heading"><div><h2 id="chart-title">${esc(accountName(account))} · 收盘收益</h2></div>
      <div class="range-buttons" role="group" aria-label="收益曲线时间范围" ${!hasHistory ? 'hidden' : ''}>${[['30','近30天'],['90','近90天'],['all','全部']].map(([value,label]) => `<button type="button" data-range="${value}" class="${chartRange === value ? 'active' : ''}" aria-pressed="${chartRange === value}">${label}</button>`).join('')}</div></div>
    <div class="chart-summary" aria-live="polite">
      <div class="chart-metric primary"><small><i class="legend-line"></i><span id="chart-return-label">累计收益率</span></small><b id="chart-nav">—</b></div>
      <div class="chart-metric benchmark" ${!hasBenchmark ? 'hidden' : ''}><small><i class="legend-line benchmark"></i>同期基准</small><b id="chart-base">—</b></div>
      <div class="chart-metric" ${!hasBenchmark ? 'hidden' : ''}><small>超额 / 百分点</small><b id="chart-excess">—</b></div>
    </div><div id="chart-date" class="chart-date"></div>
    <div id="chart-stage" class="chart-stage"></div>
    <input id="chart-cursor" class="chart-slider" type="range" aria-label="选择日期查看收益" hidden>
    <div class="chart-foot" ${!basis && !hasHistory ? 'hidden' : ''}><span>${basis}</span><span ${!hasHistory || history.length<2 ? 'hidden' : ''}>拖动查看每日收益</span></div>
  </section>`;
}

function chartPoints(history, range, interval) {
  const valid = row => finite(row.nav) && row.nav > 0;
  // 策略启用前也有账户收盘记录，横轴从策略首次有效净值开始。
  const first = history.findIndex(valid);
  if (first < 0) return [];
  if (range === 'all') return history.slice(first);
  const end = interval ? history.findLastIndex(valid) : history.length - 1;
  const cutoff = timestamp(history[end].date) - Number(range) * 86400000;
  if (!interval) return history.slice(first).filter(row => timestamp(row.date) >= cutoff);
  const start = history.findLastIndex(row => timestamp(row.date) <= cutoff && valid(row));
  return history.slice(start < 0 ? first : start, end + 1);
}

function drawChart(account) {
  const stage = document.querySelector('#chart-stage');
  const slider = document.querySelector('#chart-cursor');
  if (!stage || !slider) return;
  const interval = account?.id === 'etf' && chartRange !== 'all';
  const points = chartPoints(account?.history || [], chartRange, interval);
  const nav = (row, key) => {
    const baseline = interval ? points[0][key] : 1;
    return row[key] > 0 && baseline > 0 ? row[key] / baseline : null;
  };
  const value = (row, key) => {
    const adjusted = nav(row, key);
    return adjusted === null ? null : (adjusted - 1) * 100;
  };
  const returnLabel = interval ? '区间收益率' : '累计收益率';
  document.querySelector('#chart-return-label').textContent = returnLabel;
  const values = points.flatMap(row => [value(row,'nav'),value(row,'benchmark_nav')]).filter(finite);
  const empty = !values.length || (interval && points.length < 2);
  stage.classList.toggle('is-empty', empty);
  if (empty) {
    stage.innerHTML = `<div class="empty">${interval ? '区间内有效收盘记录不足' : '暂无收益记录'}</div>`;
    slider.hidden = true;
    document.querySelector('#chart-date').textContent = '';
    ['#chart-nav','#chart-base','#chart-excess'].forEach(selector => { document.querySelector(selector).textContent='—'; });
    return;
  }
  const width = Math.max(280, stage.clientWidth);
  const height = stage.clientHeight || 268;
  const left = 48, right = width - 12, top = 18, bottom = height - 32;
  const low = Math.min(0,...values), high = Math.max(0,...values);
  const padding = Math.max((high-low)*0.15,0.25);
  const min = low-padding, max = high+padding;
  const first = timestamp(points[0].date), last = timestamp(points[points.length-1].date);
  const x = index => last === first ? (left+right)/2 : left+(timestamp(points[index].date)-first)/(last-first)*(right-left);
  const y = v => bottom-(v-min)/(max-min)*(bottom-top);
  function pathFor(key) {
    let path = '', connected = false;
    points.forEach((row,index) => {
      const v = value(row,key);
      if (v === null) { connected=false; return; }
      path += `${connected ? 'L' : 'M'}${x(index).toFixed(2)},${y(v).toFixed(2)} `;
      connected = true;
    });
    return path;
  }
  let grid = '';
  for (let i=0;i<5;i++) {
    const v = min+(max-min)*i/4;
    grid += `<line class="chart-grid" x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}"/><text class="chart-axis" x="${left-10}" y="${y(v)+4}" text-anchor="end">${v.toFixed(1)}%</text>`;
  }
  const ticks = [...new Set([0,Math.floor((points.length-1)/2),points.length-1])];
  const labels = ticks.map(index => `<text class="chart-axis" x="${x(index)}" y="${height-6}" text-anchor="${index===0 && points.length>1 ? 'start' : index===points.length-1 && points.length>1 ? 'end' : 'middle'}">${esc(points[index].date.slice(5))}</text>`).join('');
  stage.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${returnLabel}与同期基准对比曲线"><title>${returnLabel}与同期基准</title>${grid}<line class="chart-zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/><path class="chart-benchmark" d="${pathFor('benchmark_nav')}"/><path class="chart-account" d="${pathFor('nav')}"/>${labels}<g id="chart-marker"></g></svg>`;
  const svg = stage.querySelector('svg');
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const gradientId = 'chart-account-fill';
  defs.innerHTML = `<linearGradient id="${gradientId}" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#2d7b61" stop-opacity=".18"/>
    <stop offset="100%" stop-color="#2d7b61" stop-opacity="0"/>
  </linearGradient>`;
  svg.prepend(defs);
  const navPath = pathFor('nav');
  if (navPath) {
    const area = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    area.setAttribute('class', 'chart-account-area');
    const lastX = [...navPath.matchAll(/([\d.]+),[\d.]+/g)].map(m => Number(m[1]));
    const zeroY = y(0).toFixed(2);
    const firstX = lastX.length ? lastX[0].toFixed(2) : left;
    const endX = lastX.length ? lastX.at(-1).toFixed(2) : left;
    area.setAttribute('d', `${navPath}L${endX},${zeroY} L${firstX},${zeroY} Z`);
    area.setAttribute('fill', `url(#${gradientId})`);
    svg.querySelector('.chart-benchmark').before(area);
  }
  slider.hidden = points.length < 2;
  slider.min = 0; slider.max = points.length-1; slider.step = 1;
  let latest = points.length-1;
  while (latest>0 && value(points[latest],'nav') === null) latest--;
  function focus(index) {
    const row = points[index];
    const accountNav = nav(row, 'nav'), benchmarkNav = nav(row, 'benchmark_nav');
    document.querySelector('#chart-nav').textContent = percent(accountNav);
    document.querySelector('#chart-base').textContent = percent(benchmarkNav);
    document.querySelector('#chart-excess').textContent = finite(accountNav) && finite(benchmarkNav) ? signed((accountNav-benchmarkNav)*100) : '—';
    document.querySelector('#chart-date').textContent = `${interval ? `${points[0].date} 至 ` : ''}${row.date}${accountNav === null ? ' · 当日数据缺失' : ''}`;
    slider.value = index;
    slider.setAttribute('aria-valuetext',`${row.date}，账户收益 ${percent(accountNav)}，基准收益 ${percent(benchmarkNav)}`);
    const dots = ['nav','benchmark_nav'].map(key => {
      const v = value(row,key);
      return v === null ? '' : `<circle class="chart-point ${key==='benchmark_nav' ? 'benchmark' : ''}" cx="${x(index)}" cy="${y(v)}" r="4"/>`;
    }).join('');
    svg.querySelector('#chart-marker').innerHTML = `<line class="chart-focus" x1="${x(index)}" x2="${x(index)}" y1="${top}" y2="${bottom}"/>${dots}`;
  }
  function pointer(event) {
    const bounds = svg.getBoundingClientRect();
    const target = (event.clientX-bounds.left)/bounds.width*width;
    let nearest = 0;
    points.forEach((_,index) => { if (Math.abs(x(index)-target)<Math.abs(x(nearest)-target)) nearest=index; });
    focus(nearest);
  }
  const summary = document.querySelector('.chart-summary');
  svg.onpointermove = event => { summary?.classList.add('is-scanning'); pointer(event); };
  svg.onpointerdown = event => { summary?.classList.add('is-scanning'); pointer(event); };
  svg.onpointerleave = () => { summary?.classList.remove('is-scanning'); focus(latest); };
  slider.oninput = () => focus(Number(slider.value));
  focus(latest);
}

function summaryPage() {
  const account = data.accounts[0];
  return `${warnings(account)}${account.can_record ? accountDetails(account) : chartSection(account)}`;
}

function entryActions(view) {
  const expanded = recordDisplays.get(view.id)?.expanded || false;
  // 现金类四操作收进「现金」菜单，每个动作带一句适用场景，划转类收进「划转」菜单。
  const cashMenu = [
    view.id === 'etf' || view.holding_only ? '' : {action:'data-cash-rebalance', label:'现金再平衡', hint:'把各策略现金调到接近目标预算'},
    {action:'data-open-cash', label:'分配现金', hint:'把账户现金分派给各策略'},
    {action:'id:cash-transfer', label:data.accounts[0].trading_accounts?'策略间划转现金':'划转现金', hint:'策略之间挪现金，不动持仓'},
  ].filter(Boolean);
  const menu = items => `<div class="action-menu"><button type="button" class="text-button action-menu-toggle" aria-haspopup="true" aria-expanded="false">现金 ▾</button><div class="action-menu-list" hidden>${items.map(item =>
    `<button type="button" class="action-menu-item" ${item.action.startsWith('id:') ? `id="${item.action.slice(3)}"` : item.action}><span>${esc(item.label)}</span><small>${esc(item.hint)}</small></button>`).join('')}</div></div>`;
  return `<div class="account-entry-bar"><div class="allocation-actions">
    <button type="button" id="manual-entry" class="primary-button">＋ 记一笔</button>
    <button type="button" id="account-records-link" class="secondary-button" aria-controls="account-records" aria-expanded="${expanded}">${expanded ? '收起流水' : '查看流水'}</button>
    ${view.id === 'etf' ? '<button type="button" class="secondary-button" data-holding-filter="" aria-controls="account-holdings">查看持仓</button>' : ''}
    ${view.id === 'etf' ? '<button type="button" id="dca-settings" class="text-button">定投计划</button>' : ''}
    ${cashMenu.length ? menu(cashMenu) : ''}
    <button type="button" id="position-transfer" class="text-button">持仓划转</button>
  </div><p id="entry-receipt" class="subtle" role="status">${entryReceipt ? esc(entryReceipt) : ''}</p></div>`;
}

function bindActionMenus(root=document) {
  for (const toggle of root.querySelectorAll('.action-menu-toggle:not([data-menu-bound])')) {
    toggle.dataset.menuBound = '1';
    const list = toggle.nextElementSibling;
    const close = () => { list.hidden = true; toggle.setAttribute('aria-expanded','false'); };
    toggle.onclick = event => {
      event.stopPropagation();
      const open = list.hidden;
      for (const other of document.querySelectorAll('.action-menu-list:not([hidden])')) other.hidden = true;
      list.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
    };
    for (const item of list.querySelectorAll('.action-menu-item')) {
      item.onclick = () => { close(); item.dispatchEvent(new CustomEvent('action-menu-pick', {bubbles:true})); };
    }
  }
  root.addEventListener?.('click', event => {
    if (!event.target.closest('.action-menu')) {
      for (const list of root.querySelectorAll('.action-menu-list:not([hidden])')) {
        list.hidden = true;
        list.parentElement.querySelector('.action-menu-toggle')?.setAttribute('aria-expanded','false');
      }
    }
  }, {once:false});
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
    ${custodySection(account)}${recordSection(account)}`;
}

function pendingFundSection(account, recordedToday=account.dca?.recorded_today || []) {
  return pendingSubscriptionSection(account, recordedToday) + pendingRedemptionSection(account);
}

function fundVoidStatus(view, side) {
  const message = fundVoidReceipt?.view === view.id && fundVoidReceipt.side === side ? fundVoidReceipt.message : '';
  return message ? `<p id="fund-void-receipt" class="entry-success" role="status" tabindex="-1">${esc(message)}</p>` : '';
}

function pendingRedemptionSection(view) {
  const account = data.accounts[0];
  const orders = Object.values(account.fund_redeem_pending || {}).filter(order=>view.id==='etf' || order.strategy===view.id);
  const receipt = fundVoidStatus(view, 'fund_redeem_void');
  if (!orders.length && !receipt) return '';
  const rows = orders.map(order=>`<tr><td>${esc(fundDisplayName(order.code) || order.code)}<small>${esc([order.code, strategyName(order.strategy), account.trading_accounts && custodyName(account,order.trading_account)].filter(Boolean).join(' · '))}</small><small class="pending-fund-mobile">${esc(order.date)} · ${fundShares(order.qty)} 份</small></td><td class="num">${esc(order.date)}</td><td class="num">${fundShares(order.qty)}</td><td><div class="pending-fund-actions"><button type="button" class="text-button" data-redeem-receipt="${esc(order.id)}">确认到账</button><button type="button" class="text-button fund-void-action" data-redeem-void="${esc(order.id)}">作废</button></div></td></tr>`).join('');
  return `<section class="section"><div class="section-heading"><h2>待到账赎回</h2></div>${receipt}${rows ? `<div class="table-wrap"><table class="pending-fund-table"><thead><tr><th>基金</th><th class="num">申请日</th><th class="num">申请份额</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : ''}</section>`;
}

function pendingSubscriptionSection(account, recordedToday) {
  const pending = Object.values(account.fund_pending || {});
  const autoNote = recordedToday.length
    ? `今日已自动记账 ${recordedToday.length} 笔（净值日 ${[...new Set(recordedToday)].join('、')}）`
    : '';
  const receipt = fundVoidStatus(account, 'fund_void');
  if (!pending.length && !autoNote && !receipt) return '';
  const rows = pending.map(order => {
    const name = fundDisplayName(order.code);
    return `<tr><td>${esc(name || order.code)}<small>${esc([name && order.code, strategyName(order.strategy), account.trading_accounts && custodyName(account,order.trading_account)].filter(Boolean).join(' · '))}</small><small class="pending-fund-mobile">${esc(order.nav_date)} · ${money(order.amount)} 元</small></td><td class="num">${esc(order.nav_date)}</td><td class="num">${money(order.amount)}</td><td><div class="pending-fund-actions"><button type="button" class="text-button" data-edit-event="${esc(order.id)}">修改</button><button type="button" class="text-button fund-void-action" data-void-event="${esc(order.id)}">作废</button></div></td></tr>`;
  }).join('');
  const note = autoNote ? `<p class="subtle">${esc(autoNote)}</p>` : '';
  return `<section id="fund-pending" class="section"><div class="section-heading"><h2>待确认申购</h2>${rows ? `<span class="subtle">在途 ${money(account.pending_fund_value)} 元</span>` : ''}</div>${receipt}${note}${rows ? `<div class="table-wrap"><table class="pending-fund-table"><thead><tr><th>基金</th><th class="num">买入日</th><th class="num">实际扣款 / 元</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : ''}</section>`;
}

function recordRows(view) {
  return view.id === 'etf' ? view.trades : [...(view.trades || [])].reverse();
}

function recordSection(view) {
  const rows = recordRows(view);
  const display = recordDisplays.get(view.id);
  const filters = display?.filters || {};
  const account = data.accounts[0];
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
  if (row.side === 'fund_pending') return data.accounts[0].fund_pending?.[row.id] ? '场外申购 · 待确认' : '场外申购扣款';
  if (row.side === 'fund_redeem' && row.pending_id) return '场外赎回到账';
  return SIDES[row.side] || row.side;
}

function recordAmount(row) {
  return row.kind === 'fund' ? row.transfer_price : row.price ?? row.amount;
}

const recordCashAllocations = row => Object.entries(row.cash_allocations || {}).map(([key,amount]) =>
  `${strategyName(key) || key} ${price(amount)} 元`).join(' · ');

function recordCsv(rows) {
  const account = data.accounts[0];
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
    const name = fund ? fundDisplayName(row.code) || '场外基金' : row.code ? data.accounts[0].positions.find(item=>item.code===row.code)?.name || row.name || row.code : '';
    let detail = [name, row.code && name!==row.code ? row.code : '', strategyName(row.strategy || (isFundTrade(row.side) ? 'core' : ''))].filter(Boolean).join(' · ') || '账户资金';
    const account=data.accounts[0];
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
    const waiting = !!data.accounts[0].fund_pending?.[row.id];
    const label = recordLabel(row);
    const edit = row.kind!=='pending' && !immutable && (!pending || waiting) ? `<button type="button" class="text-button" data-edit-event="${esc(row.id)}">修改</button>` : '';
    const recordPrice = recordAmount(row);
    return `<tr><td>${esc(row.date)}</td><td>${esc(label)}<small>${esc(detail)}</small>${edit}</td><td class="num">${fund ? fundShares(row.qty) : number(row.qty)}</td><td class="num">${price(recordPrice)}</td><td class="num">${price(row.fee)}</td></tr>`;
    }).join('');
    return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>类型 / 明细</th><th class="num">份额</th><th class="num">价格或金额 / 元</th><th class="num">手续费 / 元</th></tr></thead><tbody>${body}</tbody></table></div>`;
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

  const canRecord = data.accounts[0].can_record;
  const status = strategy.status && strategy.status!=='已分配' ? `<p class="subtle">${esc(strategy.status)}</p>` : '';
  const allocation = isAllocation(strategy);
  const fundBuy = acceptsFunds(strategy) ? `<button type="button" data-fund-side="fund_buy" data-code="" data-strategy="${esc(strategy.id)}" class="text-button">场外申购</button>` : '';
  const settings = allocation ? `<a href="/settings/${esc(strategy.id)}" class="text-button">编辑组合配比</a>`
    : strategy.id==='rotation' ? '<a href="/settings/rotation" class="text-button">编辑标的池</a>' : '';
  const actions = canRecord && (fundBuy || settings) ? `<div class="core-actions">${fundBuy}${settings}</div>` : '';
  const pending = acceptsFunds(strategy) ? pendingFundSection(strategy, data.accounts[0].dca?.recorded_today_by_strategy?.[strategy.id]) : '';
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

function render() {
  chartObserver?.disconnect();
  updateQuoteControls();
  const statusAccount = data.accounts[0];
  document.querySelector('#account-settings').hidden=!statusAccount.can_record;
  const bar = document.querySelector('#status-bar');
  const barHtml = selected === 0 ? '' : statusBar(statusAccount);
  bar.innerHTML = barHtml;
  bar.hidden = !barHtml;
  const tabs = [{id:'etf',name:'账户总览'},...ownerViews()];
  document.querySelector('#tabs').innerHTML = tabs.map((tab,index) => `<button type="button" role="tab" id="tab-${esc(tab.id)}" class="${index===selected ? 'active' : ''}" aria-selected="${index===selected}" aria-controls="account" tabindex="${index===selected ? 0 : -1}" data-index="${index}">${esc(accountName(tab))}</button>`).join('');
  function activateTab(index) {
    if (selected !== index) entryReceipt = '';
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
  document.querySelector('#account').innerHTML = selected===0 ? summaryPage() : fundedStrategyPage(account);
  drawChart(account);
  bindAccountActions(account);
  bindCoreSort(account);
  document.querySelectorAll('[data-range]').forEach(button => button.onclick=() => {
    chartRange=button.dataset.range;
    document.querySelectorAll('[data-range]').forEach(item => { item.classList.toggle('active',item.dataset.range===chartRange); item.setAttribute('aria-pressed',item.dataset.range===chartRange); });
    drawChart(account);
  });
  chartObserver = new ResizeObserver(() => { drawChart(account); measureReport(); });
  const chartStage = document.querySelector('#chart-stage');
  if (chartStage) chartObserver.observe(chartStage);
  const prefix = REPORT_PREFIX[account?.id];
  const section = document.querySelector('.reports-section');
  section.hidden = !prefix;
  reportRequest?.abort();
  selectedReport = null;
  const report = prefix && data.reports.find(row => row.file.startsWith(prefix));
  document.querySelector('#report-panel').hidden = !report;
  document.querySelector('#report-empty').hidden = !!report;
  if (report) selectReport(report.file);
}

function measureReport() {
  const reader = document.querySelector('#report-reader');
  const content = document.querySelector('#report-content');
  if (!reader || !content) return;
  const overflowing = content.scrollHeight>430;
  reader.classList.toggle('overflowing',overflowing);
  document.querySelector('#report-actions').hidden=!overflowing;
}

function showReport(report) {
  const content = document.querySelector('#report-content');
  content.innerHTML = report.html;
  content.setAttribute('aria-busy','false');
  content.querySelectorAll('table').forEach(table => {
    const wrap=document.createElement('div'); wrap.className='report-table';
    table.replaceWith(wrap); wrap.append(table);
  });
  measureReport();
}

async function selectReport(file) {
  reportRequest?.abort();
  selectedReport=file;
  const report=data.reports.find(row => row.file===file);
  document.querySelector('#report-kind').textContent='策略日报';
  document.querySelector('#report-name').textContent=report.name;
  document.querySelector('#report-date').textContent=reportDate(report.date);
  document.querySelector('#report-reader').classList.remove('expanded','overflowing');
  document.querySelector('#report-expand').textContent='展开全文 ↓';
  document.querySelector('#report-expand').setAttribute('aria-expanded','false');
  document.querySelector('#report-actions').hidden=true;
  const content=document.querySelector('#report-content');
  content.setAttribute('aria-busy','true');
  if (reportCache.has(file)) { showReport(reportCache.get(file)); return; }
  content.innerHTML='<div class="empty">正在读取报告…</div>';
  const controller=new AbortController(); reportRequest=controller;
  try {
    const response=await fetch(`/api/report/${encodeURIComponent(file)}`,{cache:'no-store',signal:controller.signal});
    if (!response.ok) throw Error('Report unavailable');
    const result=await response.json();
    if (selectedReport!==file || controller.signal.aborted) return;
    reportCache.set(file,result); showReport(result);
  } catch (error) {
    if (controller.signal.aborted) return;
    content.setAttribute('aria-busy','false');
    content.innerHTML='<div class="empty">报告暂时无法读取。<br><button type="button" id="report-retry" class="text-button">重新读取</button></div>';
    document.querySelector('#report-retry').onclick=() => selectReport(file);
  }
}

document.querySelector('#report-expand').onclick=() => {
  const expanded=document.querySelector('#report-reader').classList.toggle('expanded');
  document.querySelector('#report-expand').textContent=expanded ? '收起报告 ↑' : '展开全文 ↓';
  document.querySelector('#report-expand').setAttribute('aria-expanded',expanded);
};

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

document.querySelector('#overview-retry').onclick = loadOverview;
loadOverview();


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

async function withGuard(button, request) {
  button.disabled = true;
  try {
    await request();
    await reloadAccount();
  } catch (error) {
    document.querySelector('#status').textContent = error.message;
    button.disabled = false;
  }
}

async function fillFundName(dialog, session, input, hint, state) {
  const code = input.value;
  if (code === state.code) return;
  state.code = code;
  hint.textContent = fundDisplayName(code) || '';
  hint.hidden = !hint.textContent;
  if (hint.textContent || !/^\d{6}$/.test(code)) return;
  try {
    const {names} = await accountRequest('/api/etf/fund/names', {codes:[code]});
    if (dialog.open && dialog.dataset.session === session && input.value === code) {
      hint.textContent = names[code] || '';
      hint.hidden = !hint.textContent;
    }
  } catch {
    // 简称查询失败不影响表单操作。
  }
}

function bindAccountActions(view) {
  const account = data.accounts[0];
  if (!account?.can_record || (view.id!=='etf' && view.account_id!=='etf')) return;
  if (view.id === 'etf') bindReconciliation(account);
  bindActionMenus();
  const strategy = view.account_id==='etf' ? view.id : 'holding_area';
  document.querySelector('#manual-entry').onclick = () => openEntry(account, null, strategy);
  document.querySelector('#position-transfer').onclick = () => openEntry(account, {side:'assign'}, strategy);
  document.querySelector('#cash-transfer').onclick = () => openEntry(account, {side:'cash_transfer'}, strategy);
  document.querySelectorAll('[data-custody-account]').forEach(button=>button.onclick=()=>{
    openCustodyMove(account,button.dataset.custodyAccount,account.custody_positions[Number(button.dataset.custodyRow)]);
  });
  document.querySelectorAll('[data-cash-adjust]').forEach(button=>button.onclick=()=>{
    openCashAdjust(account,button.dataset.cashAdjust);
  });
  async function editRecordEvent(button) {
    button.disabled=true;
    try {
      const context=await accountRequest('/api/etf/event/context',{revision:account.revision,event_id:button.dataset.editEvent});
      const before={...account,...context};
      if(context.event.side==='cash_assign') openCashAllocation(before,{edit:context.event});
      else if(context.event.side==='cash_adjust') openCashAdjust(before,context.event.trading_account || 'unlocated',context.event);
      else if(context.event.side==='custody_transfer') {
        const e=context.event;
        const row=before.custody_positions.find(r=>r.account===e.trading_account && r.kind===e.kind && r.strategy===e.strategy && r.code===e.code);
        openCustodyMove(before,e.trading_account,row,e);
      } else if(isFundTrade(context.event.side)) openFundTrade(before,context.event.side,context.event.code,context.event.strategy || 'core',context.event);
      else openEntry(before,context.event,context.event.strategy,{edit:context.event});
    } catch(error) {document.querySelector('#status').textContent=error.message;}
    finally {button.disabled=false;}
  }
  document.querySelectorAll('[data-edit-event]').forEach(button=>button.onclick=()=>editRecordEvent(button));
  bindRecords(view, editRecordEvent);
  document.querySelectorAll('[data-position-side]').forEach(button => button.onclick = () => openEntry(account, {side:button.dataset.positionSide,code:button.dataset.code}, button.dataset.strategy));
  for (const button of document.querySelectorAll('[data-holding-transfer]')) {
    const {code, kind, strategy} = button.dataset;
    button.onclick = () => openEntry(account, {side:'assign', code, kind}, strategy);
  }
  for (const button of document.querySelectorAll('[data-classify-asset]')) {
    button.onclick = () => openAssetClassification(account, button.dataset);
  }
  for (const button of document.querySelectorAll('[data-strategy-tab]')) {
    button.onclick = () => document.getElementById(`tab-${button.dataset.strategyTab}`).click();
  }
  const dcaButton = document.querySelector('#dca-settings');
  if (dcaButton) dcaButton.onclick = () => openDcaPlans(account);
  document.querySelectorAll('[data-fund-dca]').forEach(button=>button.onclick=()=>
    openDcaPlans(account, button.dataset.strategy, button.dataset.fundDca));
  document.querySelectorAll('[data-void-event]').forEach(button=>button.onclick=()=>
    openFundVoid(account, view, view.fund_pending[button.dataset.voidEvent], 'fund_void'));
  document.querySelectorAll('[data-redeem-receipt]').forEach(button=>button.onclick=()=> {
    const order = account.fund_redeem_pending[button.dataset.redeemReceipt];
    openFundTrade(account, 'fund_redeem', order.code, order.strategy, null, order);
  });
  document.querySelectorAll('[data-redeem-void]').forEach(button=>button.onclick=()=>
    openFundVoid(account, view, account.fund_redeem_pending[button.dataset.redeemVoid], 'fund_redeem_void'));
  for (const button of document.querySelectorAll('[data-fund-side]')) {
    button.onclick = () => openFundTrade(account, button.dataset.fundSide, button.dataset.code, button.dataset.strategy);
  }
  document.querySelectorAll('[data-open-cash]').forEach(button=>button.onclick=()=>openCashAllocation(account));
  document.querySelectorAll('[data-cash-rebalance]').forEach(button=>button.onclick=()=>openCashAllocation(account,{rebalance:true}));
  document.querySelectorAll('[data-fill]').forEach(button => button.onclick = () => openEntry(account, view.suggestions[Number(button.dataset.fill)], strategy));
  document.querySelectorAll('[data-dismiss]').forEach(button => button.onclick = () => {
    const target = view.suggestions[Number(button.dataset.dismiss)];
    if (button.dataset.armed) {
      delete button.dataset.armed;
      withGuard(button, () => accountRequest('/api/etf/dismiss', {revision:account.revision, id:target.id}));
    } else {
      // 两步确认：第一次点击进入待确认态，5 秒不点即自动复原。
      button.dataset.armed = '1';
      button.textContent = '确认忽略？';
      button.setAttribute('aria-label', `忽略${target.name || target.code}的${target.side==='sell' ? '减持' : '增持'}建议，再次点击确认`);
      clearTimeout(button._armedTimer);
      button._armedTimer = setTimeout(() => {
        if (!button.isConnected) return;
        delete button.dataset.armed;
        button.textContent = '本轮忽略';
        button.removeAttribute('aria-label');
      }, 5000);
    }
  });
}

function openFundVoid(account, view, order, side) {
  const dialog = document.querySelector('#fund-void-dialog');
  const form = document.querySelector('#fund-void-form');
  const error = document.querySelector('#fund-void-error');
  const save = document.querySelector('#fund-void-save');
  const cancel = document.querySelector('#fund-void-cancel');
  const subscription = side === 'fund_void';
  const name = fundDisplayName(order.code);
  const product = name ? `${name}（${order.code}）` : order.code;
  const strategy = strategyName(order.strategy);
  const date = subscription ? `买入日 ${order.nav_date}` : `申请日 ${order.date}`;
  const type = subscription ? '申购' : '赎回预登记';
  const amount = subscription ? `${money(order.amount)} 元` : `${fundShares(order.qty)} 份`;
  const location = account.trading_accounts && custodyName(account, order.trading_account);
  const impact = subscription
    ? `作废工作台记录后，${amount}将退回「${strategy}」的策略现金。`
    : `作废工作台记录后，将释放预登记占用的 ${amount}，策略现金不变。`;
  document.querySelector('#fund-void-title').textContent = `作废${type}`;
  document.querySelector('#fund-void-order').textContent = [product, strategy, location, date, amount].filter(Boolean).join(' · ');
  document.querySelector('#fund-void-impact').textContent = impact;
  error.hidden = true;
  save.textContent = '确认作废';
  save.disabled = false;
  cancel.textContent = '取消';
  cancel.disabled = false;

  const payload = {request_id:crypto.randomUUID(), revision:account.revision,
    side, voided_pending_id:order.id, date:shanghaiToday()};
  let saving = false, saved = false;
  cancel.onclick = () => dialog.close();
  dialog.oncancel = event => { if (saving) event.preventDefault(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    save.disabled = true;
    cancel.disabled = true;
    error.hidden = true;
    try {
      if (!saved) {
        await accountRequest('/api/etf/event', payload);
        saved = true;
        const detail = subscription ? `${amount}已退回「${strategy}」的策略现金。` : `已释放 ${amount}。`;
        fundVoidReceipt = {view:view.id, side, message:`${product} · ${type}已作废。${detail}`};
      }
      await reloadAccount();
      dialog.close();
      document.querySelector('#fund-void-receipt')?.focus();
    } catch (failure) {
      error.textContent = saved ? '已作废，账户刷新失败。请点击「刷新账户」核对。' : failure.message;
      error.hidden = false;
      if (saved) {
        save.textContent = '刷新账户';
        cancel.textContent = '关闭';
      }
    } finally {
      saving = false;
      save.disabled = false;
      cancel.disabled = false;
    }
  };
  dialog.showModal();
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
  const account = data.accounts[0];
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

function openAssetClassification(account, {code, kind, category}) {
  const dialog = document.querySelector('#asset-class-dialog');
  const form = document.querySelector('#asset-class-form');
  const field = form.elements.namedItem('asset_class');
  const save = document.querySelector('#asset-class-save');
  const error = document.querySelector('#asset-class-error');
  const name = kind === 'fund' ? fundDisplayName(code) : account.positions.find(row => row.code === code)?.name;
  document.querySelector('#asset-class-product').textContent = [name, code].filter(Boolean).join(' · ');
  field.innerHTML = '<option value="">请选择</option>' + Object.entries(ASSET_NAMES)
    .filter(([value]) => value !== 'unknown').map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
  field.value = category === 'unknown' ? '' : category;
  field.disabled = false;
  save.disabled = false;
  save.textContent = '保存分类';
  error.hidden = true;
  let saving = false, saved = false;
  document.querySelector('#asset-class-cancel').onclick = () => { if (!saving) dialog.close(); };
  dialog.oncancel = event => { if (saving) event.preventDefault(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (saving || (!saved && !form.reportValidity())) return;
    saving = true; save.disabled = true; field.disabled = true; error.hidden = true;
    try {
      if (!saved) {
        await accountRequest('/api/etf/asset/classify', {revision:account.config_revision, kind, code, asset_class:field.value});
        saved = true;
      }
      await reloadAccount();
      dialog.close();
      document.querySelector('#status').textContent = `${code} 已归入${ASSET_NAMES[field.value]}`;
    } catch (failure) {
      error.textContent = saved ? '分类已保存，页面更新失败。请点击刷新账户。' : failure.message;
      error.hidden = false;
    } finally {
      saving = false; save.disabled = false; field.disabled = saved;
      save.textContent = saved ? '刷新账户' : '保存分类';
    }
  };
  dialog.showModal();
  field.focus();
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

function refreshCoreTable(strategy) {
  const wrap = document.querySelector('.core-allocation-table')?.closest('.table-wrap');
  if (!wrap) return;
  const tmp = document.createElement('div');
  tmp.innerHTML = strategyPositionTable(strategy);
  wrap.replaceWith(tmp.firstElementChild);
  bindAccountActions(strategy);
  bindCoreSort(strategy);
}

function bindCoreSort(strategy) {
  document.querySelectorAll('.core-allocation-table th[data-sort-key]').forEach(th => {
    const activate = () => {
      const key = th.dataset.sortKey;
      coreSort = coreSort.key === key ? {key, dir: -coreSort.dir} : {key, dir: key === 'deviation' ? -1 : 1};
      refreshCoreTable(strategy);
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

function openFundTrade(account, side, code, strategy, edit=null, receipt=null) {
  const dialog = document.querySelector('#fund-dialog');
  const form = document.querySelector('#fund-form');
  const fields = document.querySelector('#fund-fields');
  const save = document.querySelector('#fund-save');
  const error = document.querySelector('#fund-error');
  const preview = document.querySelector('#fund-preview');
  const field = name => form.elements.namedItem(name);
  const buy = side !== 'fund_redeem';
  const registerRedemption = !buy && !edit && !receipt;
  const pending = side === 'fund_pending' || (side === 'fund_buy' && !edit);
  const confirming = side === 'fund_confirm';
  const accountFee = pending && !edit ? account.fund_subscription_fee : null;
  const options = document.querySelector('#fund-buy-tools');
  const summary = document.querySelector('#fund-quick-summary');
  const fundName = document.querySelector('#fund-name');
  const fetchNav = document.querySelector('#fund-fetch-nav');
  const recalcQty = document.querySelector('#fund-recalc-qty');
  let manualQty = !!edit, fetchedNavKey, feeCode;
  let saving = false, saved = false, requestId = crypto.randomUUID();
  const today = shanghaiToday();
  const session = crypto.randomUUID();
  dialog.dataset.session = session;
  form.reset();
  delete field('trading_account').dataset.context;
  const owner = field('strategy');
  const fundOwners = ownerViews().filter(acceptsFunds);
  const holders = fundOwners.filter(row=>account.strategy_fund_positions?.[row.id]?.[code]?.qty > 0);
  const choices = strategy ? fundOwners.filter(row=>row.id===strategy) : buy ? fundOwners : holders;
  owner.innerHTML = `<option value="">请选择策略</option>${strategyOptions(choices)}`;
  owner.value = strategy || (holders.length===1 ? holders[0].id : buy && !holders.length ? 'core' : '');
  owner.disabled = !!strategy;
  fields.disabled = false;
  save.disabled = false;
  fetchNav.disabled = false;
  const saveLabel = edit ? '保存修改' : receipt ? '确认到账' : registerRedemption ? '保存预登记' : '保存记账';
  save.textContent = saveLabel;
  error.hidden = true;
  document.querySelector('#fund-title').textContent = edit ? `修改${SIDES[side]}` : receipt ? '确认赎回到账' : registerRedemption ? SIDES.fund_redeem_pending : SIDES[side];
  document.querySelector('#fund-help').textContent = pending
    ? '扣款计入在途资金。每日更新或刷新估值时查到买入日净值，就会自动确认份额。'
    : confirming ? '按交易确认单更正份额。' : registerRedemption ? '登记申请份额，实际到账后再录入到账金额。' : '请按交易确认单和实际资金变动记账。';
  document.querySelector('#fund-amount-label').textContent = buy ? '实际扣款 / 元' : '实际到账 / 元';
  field('amount').parentElement.hidden = registerRedemption;
  field('amount').disabled = registerRedemption;
  document.querySelector('#fund-qty-label').textContent = buy ? '份额（自动计算，可修正）' : edit ? '确认赎回份额' : '申请赎回份额';
  options.hidden = !buy;
  options.open = buy && !!edit && !confirming;
  document.querySelector('#fund-buy-tools-label').textContent = accountFee ? '买入日' : '买入日与费用';
  document.querySelector('#fund-fee-kind-field').hidden = !!accountFee;
  summary.hidden = !pending && !confirming;
  document.querySelector('#fund-code-field').hidden = pending && !!code && !edit;
  document.querySelector('#fund-record-day-field').hidden = pending && !edit;
  document.querySelector('#fund-record-day-label').textContent = receipt ? '到账日期' : registerRedemption ? '申请日期' : '记账日期';
  document.querySelector('#fund-qty-field').hidden = pending;
  document.querySelector('#fund-nav-field').hidden = pending;
  document.querySelector('#fund-nav-day-label').textContent = pending ? '买入日' : '申购净值日期';
  fetchNav.hidden = pending || confirming;
  field('qty').disabled = pending;
  field('fee_kind').disabled = !buy || confirming || !!accountFee;
  field('nav_date').disabled = !buy || confirming;
  field('nav').disabled = !buy || pending || confirming;
  field('code').value = code || '';
  field('code').readOnly = confirming || (!!code && !edit);
  for (const name of ['amount','date']) field(name).readOnly = confirming;
  field('amount').value = edit?.amount ?? '';
  field('qty').readOnly = !!receipt;
  field('qty').value = (receipt || edit)?.qty ?? '';
  field('date').min = account.min_date || account.trades[0]?.date || receipt?.date || account.start_date;
  field('date').max = account.max_date || today;
  field('date').value = edit?.date || today;
  field('nav').value = edit?.nav ?? '';
  field('nav_date').value = edit?.nav_date || (pending ? today : '');
  field('nav_date').max = field('date').value;
  if (edit?.subscription_fee) {
    field('fee_kind').value = edit.subscription_fee.kind;
    field('fee').value = edit.subscription_fee.value;
  }
  function loadFeeForCode() {
    const code = field('code').value;
    if (!pending || accountFee || feeCode === code) return;
    feeCode = code;
    const fee = account.fund_subscription_fees?.[code];
    field('fee_kind').value = fee?.kind || '';
    field('fee').value = fee?.value ?? '';
    options.open = !fee && /^\d{6}$/.test(code);
  }
  const nameState = {};
  if (edit) feeCode = code;
  loadFeeForCode();
  const showError = message => { error.textContent = message; error.hidden = false; };
  const navKey = () => `${field('code').value}:${field('nav_date').value}`;
  function selectedFee() {
    if (accountFee) return accountFee;
    const kind = field('fee_kind').value, value = Number(field('fee').value);
    if (kind === 'none') return {kind,value:0};
    if (kind && field('fee').value !== '' && finite(value) && value >= 0) return {kind,value};
    return null;
  }
  function estimateQty(amount, fee, nav) {
    if (!(amount > 0 && nav > 0) || !fee) return 0;
    const net = fee.kind === 'rate' ? amount / (1 + fee.value / 100) : fee.kind === 'fixed' ? amount - fee.value : amount;
    return net > 0 ? Number((Math.round(net * 100) / 100 / nav).toFixed(2)) : 0;
  }
  function update() {
    const code = field('code').value;
    const location=custodySelect(account,field('trading_account'),{side,kind:'fund',code,strategy:owner.value,edit:receipt || edit});
    if (receipt) field('trading_account').disabled = true;
    const held = location ? location.qty : account.strategy_fund_positions?.[owner.value]?.[code]?.qty || 0;
    const reserved = !buy && !receipt ? Object.values(account.fund_redeem_pending || {})
      .filter(order=>order.code===code && order.strategy===owner.value && order.trading_account===(location?.id || 'unlocated'))
      .reduce((total,order)=>total+order.qty,0) : 0;
    const available = Math.max(0, Number((held-reserved).toFixed(6)));
    const cash = account.cash_by_strategy[owner.value];
    const amount = Number(field('amount').value);
    const kind = field('fee_kind').value;
    const usesFee = buy && !accountFee && (kind === 'rate' || kind === 'fixed');
    document.querySelector('#fund-fee-field').hidden = !usesFee;
    document.querySelector('#fund-fee-label').textContent = kind === 'rate' ? '实际费率 / %' : '申购费 / 元';
    field('fee').disabled = !usesFee || confirming;
    field('nav_date').max = field('date').value;
    field('qty').max = buy ? '1000000000' : String(available);
    if (buy && !pending && !confirming) {
      const estimate = estimateQty(amount, selectedFee(), Number(field('nav').value));
      if (!manualQty) field('qty').value = estimate || '';
      recalcQty.hidden = !manualQty || !estimate;
    } else {
      recalcQty.hidden = true;
    }
    const fee = selectedFee();
    const feeLabel = fee?.kind === 'none' ? '免申购费'
      : fee?.kind === 'rate' ? `费率 ${fee.value}%`
      : fee?.kind === 'fixed' ? `申购费 ${money(fee.value)} 元` : '待设置费用';
    summary.textContent = [code && `基金 ${code}`, `买入日 ${field('nav_date').value || '待选择'}`, accountFee ? `账户申购费：${feeLabel}` : feeLabel].filter(Boolean).join(' · ');
    preview.hidden = confirming;
    preview.textContent = owner.value
      ? `${strategyName(owner.value)}现金 ${money(cash)} 元 · ${registerRedemption ? '可申请赎回' : '持有'} ${fundShares(registerRedemption ? available : held)} 份${amount > 0 ? ` · 记账后现金 ${money(cash + (buy ? -amount : amount))} 元` : ''}`
      : '请选择所属策略。';
    if(location?.id) preview.textContent+=' · 账户现金 '+money(location.cash)+' 元';
    field('amount').setCustomValidity(buy && !confirming && amount > cash ? '所属策略现金不足，请先分配现金。' :
      buy && !confirming && location?.id && amount>location.cash ? '所选交易账户现金不足。' : '');
  }
  form.oninput = event => {
    if (event?.target === field('code')) {
      loadFeeForCode();
      fillFundName(dialog, session, field('code'), fundName, nameState);
    }
    if (event?.target === field('qty')) manualQty = !!field('qty').value;
    if (event?.target === field('nav')) fetchedNavKey = undefined;
    if (fetchedNavKey && fetchedNavKey !== navKey()) {
      field('nav').value = '';
      fetchedNavKey = undefined;
    }
    requestId = crypto.randomUUID();
    error.hidden = true;
    update();
  };
  update();

  fetchNav.onclick = async () => {
    const code = field('code').value, date = field('nav_date').value;
    if (!/^\d{6}$/.test(code) || !date) return showError('请先填写六位基金代码和申购净值日期。');
    error.hidden = true;
    fetchNav.disabled = true;
    try {
      const result = await accountRequest('/api/etf/fund/nav', {code,date});
      if (!saving && !saved && dialog.open && dialog.dataset.session === session && field('code').value === code && field('nav_date').value === date) {
        field('nav').value = result.nav;
        fetchedNavKey = navKey();
        form.oninput();
      }
    } catch (failure) {
      if (dialog.open && dialog.dataset.session === session) showError(failure.message);
    } finally {
      if (dialog.dataset.session === session) fetchNav.disabled = false;
    }
  };
  recalcQty.onclick = () => {
    manualQty = false;
    form.oninput();
  };
  document.querySelector('#fund-cancel').onclick = () => { if (!saving) dialog.close(); };
  dialog.oncancel = event => { if (saving) event.preventDefault(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (saving) return;
    if (!saved && !form.reportValidity()) return;
    if (!saved && pending && (!field('nav_date').value || !selectedFee())) {
      options.open = true;
      return showError('请先确认买入日和申购费用。');
    }
    if (!saved && buy && field('nav').value && !field('nav_date').value) return showError('请填写单位净值对应的日期。');
    saving = true;
    save.disabled = true;
    fields.disabled = true;
    error.hidden = true;
    try {
      if (!saved) {
        const payload = {request_id:requestId, revision:account.revision, side, strategy:owner.value, code:field('code').value,
          date:field('date').value, qty:Number(field('qty').value), amount:Number(field('amount').value)};
        if (account.trading_accounts) payload.trading_account=field('trading_account').value;
        if (edit) payload.event_id = edit.id;
        if (registerRedemption) {
          payload.side = 'fund_redeem_pending';
          delete payload.amount;
        }
        if (receipt) payload.pending_id = receipt.id;
        if (pending) {
          payload.side = 'fund_pending';
          payload.nav_date = field('nav_date').value;
          if (!accountFee) payload.subscription_fee = selectedFee();
          delete payload.qty;
        } else if (buy) {
          if (field('nav').value) {
            payload.nav = Number(field('nav').value);
            payload.nav_date = field('nav_date').value;
          }
          const fee = selectedFee();
          if (edit?.code === payload.code && fee) payload.subscription_fee = fee;
        }
        await accountRequest(edit ? '/api/etf/event/edit' : '/api/etf/event', payload);
        saved = true;
      }
      await reloadAccount();
      dialog.close();
      showEntryReceipt(edit?.id || requestId, !!edit);
    } catch (failure) {
      showError(saved ? '本笔已保存，可点击刷新账户核对。' : failure.message);
    } finally {
      saving = false;
      fields.disabled = saved;
      save.disabled = false;
      save.textContent = saved ? '刷新账户' : saveLabel;
    }
  };
  dialog.showModal();
  fillFundName(dialog, session, field('code'), fundName, nameState);
}

function openCashAllocation(account, options={}) {
  const dialog=document.querySelector('#cash-dialog'),form=document.querySelector('#cash-form');
  const error=document.querySelector('#cash-error');
  const fields=document.querySelector('#cash-fields');
  const button=form.querySelector('[type=submit]');
  let saving=false, saved=false, planning=false;
  let requestId=crypto.randomUUID();
  form.reset();error.hidden=true;fields.disabled=false;button.disabled=!!options.rebalance;button.textContent='确认分配';
  document.querySelector('#cash-title').textContent=options.edit?'修改历史现金分配':options.rebalance?'现金再平衡':'分配现金';
  const preview=document.querySelector('#cash-rebalance-preview');
  preview.hidden=true;
  const dialogRequest=crypto.randomUUID();dialog.dataset.request=dialogRequest;
  document.querySelector('#cash-date-label').hidden=!options.edit;
  const dateField=form.elements.namedItem('date');
  dateField.value=options.edit?.date || '';
  dateField.min=account.min_date || account.start_date;
  dateField.max=account.max_date || '';
  dateField.required=!!options.edit;
  document.querySelector('#cash-total').textContent=money(account.cash);
  const cashFields=document.querySelector('#cash-strategies');
  cashFields.hidden=!!options.rebalance;
  cashFields.innerHTML=data.strategies.map(({id,name}) =>
    `<label>${esc(name)} / 元<input name="${esc(id)}" type="number" min="0" step="0.01" required></label>`).join('');
  const inputs = data.strategies.map(({id}) => form.elements.namedItem(id));
  for (const input of inputs) input.value=((options.edit?.cash_allocations || account.cash_by_strategy)[input.name] || 0).toFixed(2);
  function update() {
    requestId=crypto.randomUUID();
    preview.hidden=true;
    const allocated=inputs.reduce((sum,input)=>sum+Number(input.value),0);
    const remaining=Math.round((account.cash-allocated)*100)/100;
    const message=remaining<0 ? `超出账户现金 ${money(-remaining)} 元` : `待定现金 ${money(remaining)} 元`;
    const remainder=document.querySelector('#cash-remaining');
    remainder.textContent=message;
    remainder.hidden=!!options.rebalance && remaining===0;
    remainder.classList.toggle('invalid',remaining<0);
    inputs[0].setCustomValidity(remaining<0 ? message : '');
  }
  form.oninput=update;
  document.querySelector('#cash-cancel').onclick=()=>{if(!saving)dialog.close();};
  dialog.oncancel=event=>{if(saving)event.preventDefault();};
  const rebalanceButton=document.querySelector('#cash-fill-target');
  rebalanceButton.hidden=!!options.edit;
  rebalanceButton.disabled=!finite(account.equity);
  rebalanceButton.onclick=async()=>{
    if(planning || saved)return;
    planning=true;rebalanceButton.disabled=true;button.disabled=true;error.hidden=true;
    try {
      const plan=await accountRequest('/api/etf/cash/rebalance',{revision:account.revision});
      if(!dialog.open || dialog.dataset.request!==dialogRequest)return;
      for(const input of inputs) input.value=plan.cash_allocations[input.name].toFixed(2);
      update();
      const area=plan.rows.find(row=>row.id==='holding_area');
      const rows=plan.rows.filter(row=>row.deviation!==null || row===area).map(row=>{
        const gap=row===area ? Math.round((row.invested+row.after_cash-row.residual_budget)*100)/100 : row.deviation;
        const label=row===area ? (gap>0?'高于余量':gap<0?'低于余量':'符合余量') : (gap>0?'超配':gap<0?'低配':'已对齐');
        return `<tr><td>${esc(row.name)}</td><td>${row.change>0?'+':''}${money(row.change)}</td><td>${money(row.after_cash)}</td><td>${label}${gap?` ${money(Math.abs(gap))}`:''}</td></tr>`;
      }).join('');
      const overweight=plan.rows.filter(row=>row.holding_overweight).map(row=>row.name);
      const note=plan.shortfall>0?`按当前持仓，达到策略预算共需现金 ${money(plan.required_cash)} 元。可调配 ${money(plan.available_cash)} 元，按各策略所需现金的比例分配后，仍低于预算 ${money(plan.shortfall)} 元。`:overweight.length?'已补足低配策略的现金。':'本次调整后，各策略达到已设预算。';
      const retained=plan.rows.filter(row=>row.deviation===null && row!==area && row.after_cash>0)
        .map(row=>`${esc(row.name)}保留现金 ${money(row.after_cash)} 元。`).join('');
      const basis = valuationLabel(plan.valuation) + (Object.keys(account.fund_positions || {}).length ? ' · 场外按各自净值日' : '');
      preview.innerHTML=`<p class="entry-preview">${note}${overweight.length?` ${esc(overweight.join('、'))}的持仓市值已超过预算。`:''}${retained}</p><p class="subtle">${esc(basis)}</p><div class="table-wrap"><table class="cash-plan-table"><thead><tr><th>策略</th><th>现金调整 / 元</th><th>调整后现金 / 元</th><th>调整后偏离预算 / 元</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      preview.hidden=false;
    } catch(failure) {
      if(dialog.dataset.request===dialogRequest) {preview.hidden=true;error.textContent=failure.message;error.hidden=false;}
    } finally {
      planning=false;
      if(dialog.dataset.request===dialogRequest) {rebalanceButton.disabled=!finite(account.equity);button.disabled=!!options.rebalance && preview.hidden;}
    }
  };
  form.onsubmit=async event=>{
    event.preventDefault();if(saving || planning || (options.rebalance && preview.hidden))return;
    if(!form.reportValidity())return;
    saving=true;button.disabled=true;error.hidden=true;fields.disabled=true;
    const day=shanghaiToday();
    const cash_allocations=Object.fromEntries(inputs.map(input=>[input.name,Number(input.value)]));
    const payload={request_id:requestId,revision:account.revision,date:day,side:'cash_assign',cash_allocations};
    if(options.edit) {payload.date=dateField.value;payload.event_id=options.edit.id;}
    try {
      if(!saved) {await accountRequest(options.edit?'/api/etf/event/edit':'/api/etf/event',payload);saved=true;}
      await reloadAccount();dialog.close();
      showEntryReceipt(options.edit?.id || requestId, !!options.edit);
    } catch (failure) {
      error.textContent=saved ? '现金分配已保存，页面更新失败。请刷新账户查看。' : failure.message;
      error.hidden=false;
    } finally {saving=false;fields.disabled=saved;button.disabled=false;button.textContent=saved?'刷新账户':'确认分配';}
  };
  update();dialog.showModal();
  if(options.rebalance) {
    if(finite(account.equity)) rebalanceButton.click();
    else {error.textContent='部分持仓待估值，请先补齐估值数据再进行现金再平衡。';error.hidden=false;}
  }
}

function openDcaPlans(account, strategy='', code='') {
  const dialog=document.querySelector('#dca-dialog');
  const form=document.querySelector('#dca-form');
  const fields=document.querySelector('#dca-fields');
  const save=document.querySelector('#dca-save');
  const error=document.querySelector('#dca-error');
  const plansBox=document.querySelector('#dca-plans');
  const formTitle=document.querySelector('#dca-form-title');
  const fundName=code && fundDisplayName(code);
  document.querySelector('#dca-title').textContent=code
    ? `${strategyName(strategy)} · ${fundName || code}定投计划`
    : '定投计划';
  const nameHint=document.querySelector('#dca-fund-name');
  const frequencySelect=form.elements.namedItem('frequency');
  const anchorSelect=form.elements.namedItem('anchor');
  const anchorLabel=document.querySelector('#dca-anchor-label');
  const codeInput=form.elements.namedItem('code');
  const planOwner=form.elements.namedItem('strategy');
  const plans=(account.dca?.plans || []).filter(plan=>
    !code || (plan.code===code && (plan.strategy || 'core')===strategy));
  const FREQUENCY_ANCHORS={monthly:Array.from({length:31},(_,i)=>[i+1,`${i+1} 日`]),
    weekly:[[1,'周一'],[2,'周二'],[3,'周三'],[4,'周四'],[5,'周五']]};
  let editing=null, saving=false;
  const nameState={};
  const session=crypto.randomUUID();
  dialog.dataset.session=session;
  form.reset();error.hidden=true;fields.disabled=false;save.disabled=false;
  fields.hidden=save.hidden=Boolean(code && plans.length>1);
  codeInput.readOnly=Boolean(code);
  codeInput.value=code;
  planOwner.innerHTML=`<option value="">请选择所属组合</option>${strategyOptions(ownerViews().filter(acceptsFunds))}`;
  planOwner.value=strategy;
  planOwner.disabled=Boolean(code);
  nameHint.textContent=fundName || '';
  nameHint.hidden=!fundName;
  save.textContent='保存计划';formTitle.textContent='添加计划';
  const planAccount=form.elements.namedItem('trading_account');
  planAccount.parentElement.hidden=planAccount.disabled=!account.trading_accounts;
  planAccount.replaceChildren(new Option('跟随产品或渠道默认账户',''),...Object.entries(account.trading_accounts?.accounts || {})
    .filter(([,r])=>r.channel==='fund').map(([id,r])=>new Option(r.name,id)));
  function fillAnchors() {
    const current=Number(anchorSelect.value);
    const anchors=FREQUENCY_ANCHORS[frequencySelect.value] || [];
    anchorLabel.hidden=anchorSelect.disabled=anchors.length===0;
    anchorSelect.innerHTML=anchors.map(([value,label])=>`<option value="${value}">${label}</option>`).join('');
    if ([...anchorSelect.options].some(option=>Number(option.value)===current)) anchorSelect.value=current;
  }
  function editPlan(plan) {
    editing=plan;
    fields.hidden=save.hidden=false;
    planOwner.value=plan.strategy || 'core';
    planAccount.value=plan.trading_account || '';
    formTitle.textContent='修改计划';
    save.textContent='保存修改';
    codeInput.value=plan.code;
    form.elements.namedItem('amount').value=plan.amount;
    frequencySelect.value=plan.frequency;
    fillAnchors();
    if (!anchorSelect.disabled) anchorSelect.value=plan.anchor;
    form.elements.namedItem('note').value=plan.note || '';
    nameState.code=plan.code;
    nameHint.textContent=fundDisplayName(plan.code) || '';
    nameHint.hidden=!nameHint.textContent;
    error.hidden=true;
  }
  function renderPlans() {
    plansBox.innerHTML=plans.length ? `<table><thead><tr><th>基金</th><th>计划</th><th>状态</th><th>下次扣款日</th><th>最近一期补记</th><th></th></tr></thead><tbody>${plans.map(plan=>{
      const name=fundDisplayName(plan.code);
      const owner=strategyName(plan.strategy || 'core');
      let cadence='每个交易日';
      if (plan.frequency==='monthly') cadence=`每月 ${plan.anchor} 日`;
      if (plan.frequency==='weekly') cadence=`每周${FREQUENCY_ANCHORS.weekly[plan.anchor-1][1].slice(1)}`;
      const result=plan.latest_result;
      let latest='暂无记录';
      if (result) {
        latest=`${result.status==='failed'?'补记失败':'已补记'}<small>扣款日 ${esc(result.due_date)}</small>`;
        if (result.reason) latest+=`<small>${esc(result.reason)}</small>`;
      }
      const detail=[name && plan.code, !code && owner].filter(Boolean).join(' · ');
      return `<tr><td>${esc(name || plan.code)}${detail?`<small>${esc(detail)}</small>`:''}</td><td>${cadence} · ${money(plan.amount)} 元${account.trading_accounts?`<small>${plan.trading_account?esc(custodyName(account,plan.trading_account)):'跟随默认账户'}</small>`:''}${plan.note?`<small>${esc(plan.note)}</small>`:''}</td><td>${plan.enabled?'启用中':'已停用'}</td><td>${plan.next_due?esc(plan.next_due):plan.enabled?'暂无日期':'—'}</td><td>${latest}</td><td><button type="button" class="text-button" data-dca-edit="${esc(plan.id)}">修改</button> <button type="button" class="text-button" data-dca-toggle="${esc(plan.id)}">${plan.enabled?'停用':'启用'}</button> <button type="button" class="text-button" data-dca-delete="${esc(plan.id)}">删除</button></td></tr>`;
    }).join('')}</tbody></table>` : '<div class="empty">暂无定投计划。请在基金 App 设定自动扣款后，把计划录到这里。</div>';
    plansBox.querySelectorAll('[data-dca-edit]').forEach(button=>button.onclick=()=>{
      const plan=plans.find(row=>row.id===button.dataset.dcaEdit);
      if (plan) editPlan(plan);
    });
    plansBox.querySelectorAll('[data-dca-toggle]').forEach(button=>button.onclick=()=>{
      const plan=plans.find(row=>row.id===button.dataset.dcaToggle);
      if (!plan) return;
      changePlan('/api/etf/dca/save', {...plan, enabled:!plan.enabled},
        plan.enabled?'定投计划已停用':'定投计划已启用', {keepOpen:true});
    });
    plansBox.querySelectorAll('[data-dca-delete]').forEach(button=>button.onclick=()=>
      changePlan('/api/etf/dca/delete', {id:button.dataset.dcaDelete}, '定投计划已删除', {keepOpen:true}));
  }
  frequencySelect.onchange=fillAnchors;
  form.elements.namedItem('code').oninput=event=>fillFundName(dialog, session, event.target, nameHint, nameState);
  document.querySelector('#dca-cancel').onclick=()=>{if(!saving)dialog.close();};
  dialog.oncancel=event=>{if(saving)event.preventDefault();};
  async function changePlan(path, payload, message, {keepOpen=false}={}) {
    if (saving) return;
    saving=true;save.disabled=true;fields.disabled=true;error.hidden=true;
    try {
      await accountRequest(path, payload);
    } catch (failure) {
      error.textContent=failure.message;error.hidden=false;
      saving=false;fields.disabled=false;save.disabled=false;
      return;
    }
    if (!keepOpen) dialog.close();
    try {
      await reloadAccount();
      if (keepOpen) openDcaPlans(data.accounts[0], strategy, code);
      document.querySelector('#status').textContent=message;
    } catch {
      dialog.close();
      document.querySelector('#status').textContent=`${message}，请刷新页面核对。`;
    }
  }
  form.onsubmit=event=>{
    event.preventDefault();
    if (saving || save.hidden || !form.reportValidity()) return;
    const payload={code:codeInput.value.trim(),
      strategy:planOwner.value,
      amount:Number(form.elements.namedItem('amount').value),
      frequency:frequencySelect.value,
      note:form.elements.namedItem('note').value.trim(),
      enabled:editing?.enabled ?? true};
    if (!anchorSelect.disabled) payload.anchor=Number(anchorSelect.value);
    if (editing) payload.id=editing.id;
    if(account.trading_accounts) payload.trading_account=planAccount.value;
    changePlan('/api/etf/dca/save', payload, editing?'定投计划已保存':'定投计划已添加');
  };
  fillAnchors();
  renderPlans();
  if (code && plans.length===1) editPlan(plans[0]);
  if (!dialog.open) dialog.showModal();
}
