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
const SIDES = {buy:'买入', sell:'卖出', dividend:'分红', deposit:'入金', withdraw:'出金', assign:'持仓划转', cash_assign:'现金分配', cash_transfer:'现金划转', fund_buy:'场外申购', fund_redeem:'场外赎回', fund_pending:'场外申购', fund_confirm:'场外申购确认', fund_void:'申购作废'};
SIDES.custody_transfer = '账户间划转';
const strategyOptions = (strategies = data.strategies) => strategies.map(({id, name}) =>
  `<option value="${esc(id)}">${esc(name)}</option>`).join('');
const strategyName = id => id==='unassigned' ? '未分配' : data.strategies.find(row=>row.id===id)?.name;
const isAllocation = strategy => !!strategy.target_weights;
const HOLDING_NAMES = {etf:'ETF', stock:'股票', other:'其他持仓', fund:'场外基金'};
const ASSET_NAMES = {equity:'权益', bond:'债券', commodity:'商品', cash:'现金类', mixed:'混合', unknown:'待分类'};
const isFundTrade = side => typeof side === 'string' && side.startsWith('fund_');
const fundShares = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:6}) : '—';
const REPORT_PREFIX = {rotation:'etf_rotation_advice_', quality:'quality_pool_advice_'};
const ALLOCATION_TOLERANCE = 0.01;
const allocationToleranceNote = `${ALLOCATION_TOLERANCE*100} 个百分点内视为配平`;
const isAllocationBalanced = deviation => Math.abs(deviation) <= ALLOCATION_TOLERANCE + Number.EPSILON;
let data, selected = 0, chartRange = 'all', chartObserver;
let coreSort = {key: null, dir: 1};
let selectedReport, reportRequest;
let overviewRequest = 0, appliedOverviewRequest = 0;
const recordDisplays = new Map();
let entryReceipt = '';
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

function updateQuoteControls() {
  const account = data.accounts[0];
  const active = selected===0 ? account : data.strategies[selected-1];
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
  if (valuation?.missing?.length) {
    const names = valuation.missing.map(code => {
      const name = account.positions.find(row => row.code === code)?.name;
      return name && name !== code ? `${name}（${code}）` : code;
    });
    message += `${message ? ' ' : ''}${names.join('、')}缺少${valuation.date ? ` ${valuation.date} ` : ''}收盘价，账户待估值。`;
  }
  if (valuation?.missing_funds?.length) {
    message += `${message ? ' ' : ''}场外基金 ${valuation.missing_funds.join('、')} 缺少单位净值，账户待估值。`;
  }
  const status = document.querySelector('#quote-status');
  status.textContent = selected === 0 && !running ? '' : note;
  const feedback = document.querySelector('#quote-message');
  feedback.textContent = message; feedback.hidden = !message;
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
    '账户数据读取失败，请检查服务器日志。':'账户暂时无法加载，请稍后重试。',
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
  return `<div class="account-entry-bar"><div class="allocation-actions">
    <button type="button" id="manual-entry" class="primary-button">＋ 记一笔</button>
    <button type="button" id="account-records-link" class="secondary-button" aria-controls="account-records" aria-expanded="${expanded}">${expanded ? '收起流水' : '查看流水'}</button>
    ${view.id === 'etf' ? '' : '<button type="button" data-cash-rebalance class="text-button">现金再平衡</button>'}
    <button type="button" id="cash-transfer" class="text-button">${data.accounts[0].trading_accounts?'策略间划转现金':'划转现金'}</button>
    <button type="button" id="position-transfer" class="text-button">持仓划转</button>
    <button type="button" data-open-cash class="text-button">分配现金</button>
  </div><p id="entry-receipt" class="subtle" role="status">${entryReceipt ? esc(entryReceipt) : ''}</p></div>`;
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
    <button type="button" class="all-holdings-link" data-holding-filter="" aria-controls="account-holdings">查看持仓</button>
  </div></div>`;
}

function accountSummary(account) {
  const latest = (account.history || []).findLast(row => finite(row.nav) && row.nav > 0);
  const pnl = finite(account.pnl) ? `${account.pnl > 0 ? '+' : ''}${money(account.pnl)}` : '待估值';
  const cash = [...data.strategies, {id:'unassigned', name:'未分配'}]
    .filter(({id}) => id !== 'unassigned' || account.cash_by_strategy[id] !== 0).map(({id, name}) =>
    `<div><dt>${esc(name)}</dt><dd>${money(account.cash_by_strategy[id])}</dd></div>`).join('');
  return `<section class="reconciliation" aria-label="账户摘要">
    <div class="reconciliation-top">
      <div><span>总资产 / 元</span><strong>${finite(account.equity) ? money(account.equity) : '待估值'}</strong><small>${esc(valuationLabel(account.valuation))}</small></div>
      <div><span>累计盈亏 / 元</span><b class="${account.pnl > 0 ? 'is-gain' : account.pnl < 0 ? 'is-loss' : ''}">${pnl}</b></div>
      <div><span>累计收益率</span><b>${percent(latest?.nav)}</b>${latest ? `<small>收盘 · ${esc(latest.date)}</small>` : ''}</div>
      <div><span>账面现金 / 元</span><button type="button" id="account-cash-toggle" aria-label="查看各策略现金明细" aria-controls="account-cash-detail" aria-expanded="false"><b>${money(account.cash)}</b></button>${account.cash_by_strategy.unassigned ? `<small>未分配 ${money(account.cash_by_strategy.unassigned)} 元</small>` : ''}</div>
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
    return `<tr data-holding-kind="${row.kind}" data-asset-class="${row.asset_class || 'unknown'}"><td>${esc(row.name)}<small>${esc(row.code)}<span class="holding-mobile-count"> · ${number(row.count)} 份</span></small>${ownership}</td><td>${number(row.count)}</td><td>${quotePrice(row)}</td><td>${finite(row.market_value) ? money(row.market_value) : '待估值'}</td><td>${ratio(row.market_value,account.equity)}</td><td>${positionActions(row.code, owners.length===1 ? owners[0].strategy : '')}</td></tr>`;
  }).join('');
  const fundRows = Object.values(account.fund_positions || {}).map(row => {
    const owners = Object.entries(account.strategy_fund_positions || {})
      .filter(([, positions]) => positions[row.code]?.qty > 0)
      .map(([strategy, positions]) => ({strategy, shares:fundShares(positions[row.code].qty)}));
    const ownership = holdingOwnership(row.code, owners, 'fund') + custodyProduct(account,'fund',row.code) + assetClassButton(row, 'fund');
    return `<tr data-holding-kind="fund" data-asset-class="${row.asset_class || 'unknown'}"><td>${esc(fundDisplayName(row.code) || '场外基金')}<small>${esc(row.code)}<span class="holding-mobile-count"> · ${fundShares(row.qty)} 份</span></small>${ownership}</td><td>${fundShares(row.qty)}</td><td>${fundMark(row)}</td><td>${finite(row.market_value) ? money(row.market_value) : '待估值'}</td><td>${ratio(row.market_value,account.equity)}</td><td>${fundActions(row.code,true)}</td></tr>`;
  }).join('');
  return `${accountSummary(account)}${entryActions(account)}
    ${strategyAllocation(account)}${chartSection(account)}${pendingFundSection(account)}
    <section id="account-holdings" class="section" aria-labelledby="account-holdings-title" tabindex="-1"><div class="section-heading"><h2 id="account-holdings-title">账户持仓</h2><select id="holding-filter" aria-label="持仓类别"><option value="">全部持仓</option><optgroup label="资产类别">${assetFilters}</optgroup><optgroup label="产品类型">${filters}</optgroup></select></div>
      <p id="cash-class-detail" class="subtle" hidden>账面现金 ${money(account.cash)} 元 · 货币基金 ${finite(account.asset_allocation?.cash) ? `${money(account.asset_allocation.cash - account.cash)} 元` : '待估值'}</p>
      <div class="table-wrap"><table class="account-holdings-table"><thead><tr><th scope="col">标的</th><th scope="col">份额</th><th scope="col">价格 / 元</th><th scope="col">市值 / 元</th><th scope="col">资产占比</th><th scope="col">记账</th></tr></thead><tbody>${rows}${fundRows}<tr id="holdings-empty" hidden><td colspan="6" class="empty">暂无持仓</td></tr></tbody></table></div></section>
    ${custodySection(account)}${recordSection(account, account.trades)}`;
}

function pendingFundSection(account, recordedToday=account.dca?.recorded_today || []) {
  const pending = Object.values(account.fund_pending || {});
  const autoNote = recordedToday.length
    ? `今日已自动记账 ${recordedToday.length} 笔（净值日 ${[...new Set(recordedToday)].join('、')}）`
    : '';
  if (!pending.length) return autoNote ? `<section id="fund-pending" class="section"><div class="section-heading"><h2>待确认申购</h2><span class="subtle">${esc(autoNote)}</span></div></section>` : '';
  const rows = pending.map(order => {
    const name = fundDisplayName(order.code);
    return `<tr><td>${esc(name || order.code)}<small>${esc([name && order.code, strategyName(order.strategy), account.trading_accounts && custodyName(account,order.trading_account)].filter(Boolean).join(' · '))}</small></td><td>${esc(order.nav_date)}</td><td>${money(order.amount)}</td><td>待确认 <button type="button" class="text-button" data-edit-event="${esc(order.id)}">修改</button> <button type="button" class="text-button" data-void-event="${esc(order.id)}">作废</button></td></tr>`;
  }).join('');
  const note = autoNote ? `<p class="subtle">${esc(autoNote)}</p>` : '';
  return `<section id="fund-pending" class="section"><div class="section-heading"><h2>待确认申购</h2><span class="subtle">在途 ${money(account.pending_fund_value)} 元</span></div>${note}<div class="table-wrap"><table><thead><tr><th>基金</th><th>买入日</th><th>实际扣款 / 元</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

function recordSection(view, rows) {
  const display = recordDisplays.get(view.id);
  const all = display?.all;
  return `<section id="account-records" class="section" aria-labelledby="account-records-title" tabindex="-1" ${display?.expanded ? '' : 'hidden'}>
    <div class="section-heading"><h2 id="account-records-title">${view.id === 'etf' ? '账户流水' : '策略流水'}</h2>
      <div class="allocation-actions">
        ${rows.length > 20 ? `<button type="button" id="all-records" class="text-button">${all ? '只看最近 20 笔' : `查看全部 ${rows.length} 笔`}</button>` : ''}
        <button type="button" id="close-records" class="text-button">收起流水</button>
      </div>
    </div>${recordTable(all ? rows : rows.slice(0,20))}</section>`;
}

function recordTable(rows) {
  if (!rows.length) return '<div class="empty">暂无流水</div>';
  const body = rows.map(row => {
    const fund = isFundTrade(row.side) || row.kind === 'fund';
    const name = fund ? fundDisplayName(row.code) || '场外基金' : row.code ? data.accounts[0].positions.find(item=>item.code===row.code)?.name || row.name || row.code : '';
    let detail = [name, row.code && name!==row.code ? row.code : '', strategyName(row.strategy || (isFundTrade(row.side) ? 'core' : ''))].filter(Boolean).join(' · ') || '账户资金';
    if (row.side === 'assign') detail = [fund && name, row.code, `${strategyName(row.from_strategy)} → ${strategyName(row.strategy)}`].filter(Boolean).join(' · ');
    if (row.side === 'cash_transfer') detail = `${strategyName(row.from_strategy)} → ${strategyName(row.strategy)}`;
    if (row.side === 'cash_assign') {
      detail = Object.entries(row.cash_allocations).map(([key,amount]) => `${strategyName(key)} ${price(amount)} 元`).join(' · ');
    }
    const account=data.accounts[0];
    if (account.trading_accounts && (row.trading_account || !['cash_assign','cash_transfer'].includes(row.side))) {
      detail+=' · '+custodyName(account,row.trading_account);
      if (row.to_account) detail+=' → '+custodyName(account,row.to_account);
    }
    const pending = row.side === 'fund_pending';
    const voided = row.side === 'fund_void';
    const waiting = !!data.accounts[0].fund_pending?.[row.id];
    const label = pending ? (waiting ? '场外申购 · 待确认' : '场外申购扣款') : SIDES[row.side];
    const edit = !voided && (!pending || waiting) ? `<button type="button" class="text-button" data-edit-event="${esc(row.id)}">修改</button>` : '';
    const recordPrice = row.kind === 'fund' ? row.transfer_price : row.price ?? row.amount;
    return `<tr><td>${esc(row.date)}</td><td>${esc(label)}<small>${esc(detail)}</small>${edit}</td><td>${fund ? fundShares(row.qty) : number(row.qty)}</td><td>${price(recordPrice)}</td><td>${price(row.fee)}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>类型 / 明细</th><th>份额</th><th>价格或金额 / 元</th><th>手续费 / 元</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function fundedStrategyPage(strategy) {
  const stats = [[finite(strategy.budget) ? money(strategy.budget) : finite(strategy.budget_pct)?'待估值':'待设置','目标资产 / 元'],[money(strategy.invested),strategy.pending_fund_value ? '持仓与在途 / 元' : '持仓市值 / 元'],[`${strategy.pnl>0?'+':''}${money(strategy.pnl)}`,'累计盈亏 / 元'],[money(strategy.cash),'策略现金 / 元']];
  const trades = [...(strategy.trades || [])].reverse();
  const canRecord = data.accounts[0].can_record;
  const status = strategy.status && strategy.status!=='已分配' ? `<p class="subtle">${esc(strategy.status)}</p>` : '';
  const allocation = isAllocation(strategy);
  const actions = allocation && canRecord
    ? `<div class="core-actions"><button type="button" data-fund-side="fund_buy" data-code="" data-strategy="${esc(strategy.id)}" class="text-button">场外申购</button><button type="button" id="dca-settings" class="text-button">定投计划</button><a href="/settings/${esc(strategy.id)}" class="text-button">编辑组合配比</a></div>`
    : canRecord && strategy.id==='rotation' ? '<a href="/settings/rotation" class="text-button">编辑标的池</a>' : '';
  const pending = allocation ? pendingFundSection(strategy, data.accounts[0].dca?.recorded_today_by_strategy?.[strategy.id]) : '';
  return `<div class="stats">${stats.map(([value,label])=>`<div class="stat"><span>${label}</span><b>${esc(value)}</b></div>`).join('')}</div>
    ${canRecord ? entryActions(strategy) : ''}${chartSection(strategy)}
    <section class="section"><div class="section-heading"><h2>${allocation ? '持仓与目标配比' : '持仓与调仓建议'}</h2>${actions}</div>${status}${strategyPositionTable(strategy)}</section>
    ${pending}${canRecord ? recordSection(strategy, trades) : ''}`;
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
  const tabs = [{id:'etf',name:'账户总览'},...data.strategies];
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

fetch('/api/overview',{cache:'no-store'}).then(response => {
  if (!response.ok) throw Error('Overview unavailable');
  return response.json();
}).then(result => {
  data=result;
  document.querySelector('#status').textContent=`页面更新于 ${quoteTime(data.generated_at)}`;
  document.querySelector('#app').hidden=false;
  render();
  loadMissingFundNames();
  loadMissingSecurityNames();
  scheduleQuotePoll();
}).catch(() => { document.querySelector('#status').textContent='数据读取失败，请刷新页面重试。'; });


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
  const response = await fetch('/api/overview', {cache:'no-store'});
  if (!response.ok) throw Error('页面暂时无法更新，请重试。');
  const result = await response.json();
  if (request < appliedOverviewRequest) return;
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

async function withGuard(button, request, succeed) {
  button.disabled = true;
  try {
    await request();
    await reloadAccount();
    if (succeed) document.querySelector('#status').textContent = succeed;
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
  const strategy = view.account_id==='etf' ? view.id : 'unassigned';
  document.querySelector('#manual-entry').onclick = () => openEntry(account, null, strategy);
  document.querySelector('#position-transfer').onclick = () => openEntry(account, {side:'assign'}, strategy);
  document.querySelector('#cash-transfer').onclick = () => openEntry(account, {side:'cash_transfer'}, strategy);
  bindRecords(view.id);
  document.querySelectorAll('[data-custody-account]').forEach(button=>button.onclick=()=>{
    openCustodyMove(account,button.dataset.custodyAccount,account.custody_positions[Number(button.dataset.custodyRow)]);
  });
  document.querySelectorAll('[data-edit-event]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try {
      const context=await accountRequest('/api/etf/event/context',{revision:account.revision,event_id:button.dataset.editEvent});
      const before={...account,...context};
      if(context.event.side==='cash_assign') openCashAllocation(before,{edit:context.event});
      else if(context.event.side==='custody_transfer') {
        const e=context.event;
        const row=before.custody_positions.find(r=>r.account===e.trading_account && r.kind===e.kind && r.strategy===e.strategy && r.code===e.code);
        openCustodyMove(before,e.trading_account,row,e);
      } else if(isFundTrade(context.event.side)) openFundTrade(before,context.event.side,context.event.code,context.event.strategy || 'core',context.event);
      else openEntry(before,context.event,context.event.strategy,{edit:context.event});
    } catch(error) {document.querySelector('#status').textContent=error.message;}
    finally {button.disabled=false;}
  });
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
  if (dcaButton) dcaButton.onclick = () => openDcaPlans(account, strategy);
  document.querySelectorAll('[data-fund-dca]').forEach(button=>button.onclick=()=>
    openDcaPlans(account, button.dataset.strategy, button.dataset.fundDca));
  document.querySelectorAll('[data-void-event]').forEach(button=>button.onclick=()=>
    withGuard(button, () => accountRequest('/api/etf/event', {request_id:crypto.randomUUID(), revision:account.revision,
      side:'fund_void', voided_pending_id:button.dataset.voidEvent, date:shanghaiToday()}),
      '申购已作废，现金已退回所属策略'));
  for (const button of document.querySelectorAll('[data-fund-side]')) {
    button.onclick = () => openFundTrade(account, button.dataset.fundSide, button.dataset.code, button.dataset.strategy);
  }
  document.querySelectorAll('[data-open-cash]').forEach(button=>button.onclick=()=>openCashAllocation(account));
  document.querySelectorAll('[data-cash-rebalance]').forEach(button=>button.onclick=()=>openCashAllocation(account,{rebalance:true}));
  document.querySelectorAll('[data-fill]').forEach(button => button.onclick = () => openEntry(account, view.suggestions[Number(button.dataset.fill)], strategy));
  document.querySelectorAll('[data-dismiss]').forEach(button => button.onclick = () =>
    withGuard(button, () => accountRequest('/api/etf/dismiss', {revision:account.revision, id:view.suggestions[Number(button.dataset.dismiss)].id})));
}

function bindRecords(viewId) {
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
  const allRecords = document.querySelector('#all-records');
  if (allRecords) {
    allRecords.onclick = () => {
      display.all = !display.all;
      render();
      document.querySelector('#all-records').focus({preventScroll:true});
    };
  }
}

function showEntryReceipt(eventId, editing) {
  const account = data.accounts[0];
  const row = account.trades.find(trade => trade.id === eventId);
  const confirmation = editing ? '已修改' : '已保存';
  const parts = [confirmation];
  if (row) {
    parts.push(SIDES[row.side], row.code);
    if (finite(row.qty)) {
      const shares = row.kind === 'fund' || isFundTrade(row.side) ? fundShares(row.qty) : number(row.qty);
      parts.push(`${shares} 份`);
    } else if (finite(row.amount)) {
      parts.push(`${money(row.amount)} 元`);
    }
    const owner = strategyName(row.strategy);
    parts.push(row.from_strategy ? `${strategyName(row.from_strategy)} → ${owner}` : owner);
    if (account.fund_pending?.[eventId]) parts.push('待确认');
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

function openEntry(account, suggestion, strategy='unassigned', options={}) {
  const dialog = document.querySelector('#event-dialog');
  const form = document.querySelector('#event-form');
  const error = document.querySelector('#event-error');
  const save = document.querySelector('#event-save');
  const next = document.querySelector('#event-continue');
  const fields = document.querySelector('#event-fields');
  const refresh = document.querySelector('#event-refresh');
  const success = document.querySelector('#event-success');
  // 重开表单前恢复各类型，防止 reset 跳到上次类别中唯一可用的选项。
  for (const option of form.elements.namedItem('side').options) option.disabled = false;
  form.reset(); error.hidden = true; save.disabled = false; next.disabled = false;
  next.hidden = !!options.edit;
  save.textContent = options.edit ? '保存修改' : '保存记账';
  fields.disabled = false; refresh.hidden = true;
  success.hidden = !options.success; success.textContent = options.success || '';
  let requestId = crypto.randomUUID();
  let saving = false;
  const field = name => form.elements.namedItem(name);
  delete field('trading_account').dataset.context;
  field('side').value = 'buy';
  const owners = `<option value="unassigned">未分配</option>${strategyOptions()}`;
  field('strategy').innerHTML = `<option value="">请选择策略</option>${owners}`;
  field('from_strategy').innerHTML = owners;
  const today = shanghaiToday();
  field('date').value = options.edit?.date || options.date || today;
  field('date').min = options.edit ? account.min_date : account.trades?.[0]?.date || account.start_date;
  field('date').max = options.edit ? account.max_date : today;
  field('strategy').value = strategy;
  if (suggestion) {
    for (const key of ['side','code','qty']) if (suggestion[key] !== undefined) field(key).value = suggestion[key];
  }
  if (field('side').value==='cash_transfer') {
    field('strategy').value=strategy==='unassigned'?'core':strategy;
    field('from_strategy').value=Object.keys(account.cash_by_strategy).find(key=>key!==field('strategy').value && account.cash_by_strategy[key]>0) || 'unassigned';
  }
  if (field('side').value==='assign') {
    field('from_strategy').value = options.fromStrategy || strategy || 'unassigned';
    field('strategy').value = options.fromStrategy ? strategy : strategy==='core' ? 'unassigned' : 'core';
    if (suggestion?.kind === 'fund') {
      field('strategy').value = data.strategies.find(row => row.id !== strategy && isAllocation(row))?.id || '';
    }
  }
  const securities = new Map(account.positions.map(row=>[row.code,row.name]));
  if(options.edit) for(const key of ['side','date','code','qty','price','fee','strategy','from_strategy','amount']) if(options.edit[key]!==undefined) field(key).value=options.edit[key];
  const kind = field('kind');
  kind.value = options.edit ? options.edit.kind || 'listed' : suggestion?.kind || '';
  const allocationIds = new Set(data.strategies.filter(isAllocation).map(row=>row.id));
  for (const item of data.strategies) {
    for (const code of Object.keys(item.target_weights || {})) if (code!=='CASH' && !securities.has(code)) securities.set(code, code);
  }
  let suggestionOwner;
  function entryPosition() {
    const assign = field('side').value === 'assign';
    const owner = field(assign ? 'from_strategy' : 'strategy').value;
    const code = field('code').value;
    const listed = account.strategy_positions.find(row=>row.strategy===owner && row.code===code);
    const fund = assign ? account.strategy_fund_positions?.[owner]?.[code] : null;
    const ambiguous = Boolean(listed && fund);
    const selectedFund = fund && (!listed || kind.value === 'fund') ? fund : null;
    const held = ambiguous && !kind.value ? 0 : selectedFund ? selectedFund.qty : listed?.count || 0;
    return {listed, fund:selectedFund, ambiguous, held};
  }
  // 建议价格只用于估算，实际成交均价必须由用户填写。
  function update() {
    const side = field('side').value;
    const trade = ['buy','sell'].includes(side);
    const assign = side === 'assign';
    const cashTransfer = side === 'cash_transfer';
    const internal = assign || cashTransfer;
    const mode = trade ? 'trade' : assign ? 'assign' : cashTransfer ? 'cash_transfer' : 'cash';
    field('side').parentElement.hidden = internal;
    document.querySelector('#event-title').textContent = options.edit ? '修改历史记账' : assign ? '持仓划转' : cashTransfer ? '现金划转' : trade ? '记录买卖成交' : '记录出入金 / 分红';
    document.querySelectorAll('[data-entry-mode]').forEach(button=>{
      button.classList.toggle('active',button.dataset.entryMode===mode);
      button.setAttribute('aria-pressed',button.dataset.entryMode===mode);
    });
    for (const option of field('side').options) {
      const group = ['buy','sell'].includes(option.value) ? 'trade' : ['assign','cash_transfer'].includes(option.value) ? option.value : 'cash';
      option.hidden = group!==mode; option.disabled = group!==mode;
    }
    document.querySelectorAll('[data-trade-field]').forEach(label => {
      const input = label.querySelector('input,select');
      const shown = trade || (assign && !['price','fee'].includes(input.name));
      label.hidden = !shown; input.disabled = !shown;
    });
    field('strategy').parentElement.hidden = false;
    field('strategy').disabled = false;
    const source = field('from_strategy');
    source.parentElement.hidden = !internal; source.disabled = !internal;
    field('strategy').parentElement.firstChild.textContent = internal ? '转入策略' : '策略';
    document.querySelector('#entry-qty-label').textContent = assign ? '划转份额' : '成交份额';
    const {listed, fund, ambiguous, held:totalHeld} = entryPosition();
    const location=custodySelect(account,field('trading_account'),{side,kind:fund?'fund':'listed',code:field('code').value,
      strategy:field(assign?'from_strategy':'strategy').value,edit:options.edit});
    const held=location ? location.qty : totalHeld;
    kind.disabled = !ambiguous;
    document.querySelector('#entry-kind-field').hidden = !ambiguous;
    const shareStep = fund ? '0.000001' : side==='buy' ? '100' : '1';
    field('qty').min = shareStep;
    field('qty').step = shareStep;
    document.querySelector('[data-cash-field]').hidden = trade || assign;
    field('amount').disabled = trade || assign;
    field('amount').parentElement.firstChild.textContent = cashTransfer ? '划转金额 / 元' : side==='withdraw' ? '转出金额 / 元' : '到账金额 / 元';
    const owner = internal ? source.value : field('strategy').value;
    const cash = account.cash_by_strategy[owner];
    const ownerName = strategyName(owner);
    const code = field('code').value;
    const nextSuggestionOwner = assign ? owner : null;
    if (nextSuggestionOwner !== suggestionOwner) {
      suggestionOwner = nextSuggestionOwner;
      const choices = assign ? new Map(account.strategy_positions.filter(row=>row.strategy===owner)
        .map(row=>[row.code,row.name])) : securities;
      if (assign) for (const fundCode of Object.keys(account.strategy_fund_positions?.[owner] || {})) {
        choices.set(fundCode, choices.has(fundCode) ? '场内证券 / 场外基金' : fundDisplayName(fundCode) || '场外基金');
      }
      document.querySelector('#entry-securities').innerHTML = [...choices]
        .map(([value,name])=>`<option value="${esc(value)}">${esc(name)}</option>`).join('');
    }
    const chooseKind = ambiguous && !kind.value;
    const knownName = fund ? fundDisplayName(code) || '场外基金' : assign ? listed?.name : securities.get(code);
    document.querySelector('#entry-code-label').textContent = fund ? '基金代码' : assign ? '标的代码' : '证券代码';
    document.querySelector('#entry-security').textContent = chooseKind ? '同一代码有两类持仓，请选择标的类型'
      : knownName && knownName!==code ? knownName
      : assign ? '可输入代码，或从转出组合的持仓中选择'
      : code.length===6 ? '请核对证券代码' : '可输入代码，或从已有持仓中选择';
    for (const option of field('strategy').options) {
      option.disabled = Boolean(fund && option.value && !allocationIds.has(option.value));
      if (option.value) option.textContent = internal ? strategyName(option.value) : `${strategyName(option.value)} · ${money(account.cash_by_strategy[option.value])} 元现金`;
    }
    const all = document.querySelector('#entry-all');
    all.hidden = !['sell','assign'].includes(side);
    all.textContent = assign ? '全部划转' : '全部卖出';
    all.disabled = held<=0;
    const context = document.querySelector('#entry-context');
    const shares = (fund ? fundShares : number)(held);
    context.textContent = !ownerName ? '请选择本笔记账所属的策略。' : assign ? code && !chooseKind ? `${ownerName}可划转 ${shares} 份` : '选择持仓后查看可划转份额。' : `${ownerName}现金 ${money(cash)} 元${trade && code ? ` · 持有 ${shares} 份` : ''}`;
    if (location?.id && !assign) context.textContent+=' · 账户现金 '+money(location.cash)+' 元';
    if (assign && held > 0) {
      context.textContent += '。划转前的盈亏归转出策略，之后归转入策略。';
    }
    const amount = trade ? Number(field('qty').value) * Number(field('price').value) : Number(field('amount').value);
    const feeInput = field('fee');
    const feeKind = Object.keys(account.trade_fee_code_prefixes || {}).find(key =>
      /^\d{6}$/.test(code) && account.trade_fee_code_prefixes[key].some(prefix => code.startsWith(prefix)));
    const rule = trade && !options.edit ? account.trade_fees?.[feeKind] : null;
    const roundMoney = value => Math.round(Number((value * 100).toPrecision(15))) / 100;
    const automaticFee = rule && feeInput.value === '';
    const commission = automaticFee && amount > 0 ? roundMoney(Math.max(rule.min_commission, amount * rule.commission_per_10000 / 10000)) : 0;
    const stampDuty = automaticFee && feeKind === 'stock' && side === 'sell'
      ? roundMoney(amount * (account.trade_fees.stock_sell_stamp_duty_per_10000 ?? 0) / 10000) : 0;
    const fee = automaticFee ? roundMoney(commission + stampDuty) : trade ? Number(feeInput.value) : 0;
    document.querySelector('#entry-fee-label').textContent = rule ? '手续费 / 元（可按交割单修正）' : '手续费 / 元';
    feeInput.placeholder = automaticFee && amount > 0 ? `${money(fee)} 元（自动）` : rule ? '按账户费率自动计算' : '0';
    const delta = Math.round((['buy','withdraw','cash_transfer'].includes(side) ? -amount-fee : amount-fee)*100)/100;
    document.querySelector('#event-preview').hidden = assign || !(amount>0);
    const feeDetails = automaticFee ? ` · 佣金 ${money(commission)} 元${kind === 'stock' && side === 'sell' ? ` · 印花税 ${money(stampDuty)} 元` : ''}` : '';
    document.querySelector('#event-preview').textContent = `${trade ? `成交金额 ${money(amount)} 元 · 手续费 ${money(fee)} 元${feeDetails}。` : ''}现金${delta>=0?'增加':'减少'} ${money(Math.abs(delta))} 元；${ownerName || '所选策略'}记账后余额 ${finite(cash) ? money(cash+delta) : '—'} 元。`;
    if(cashTransfer && amount>0) document.querySelector('#event-preview').textContent=`${ownerName}划出 ${money(amount)} 元后剩余 ${money(cash-amount)} 元；${strategyName(field('strategy').value)}收到后余额 ${money(account.cash_by_strategy[field('strategy').value]+amount)} 元。账户总现金不变。`;
    for (const name of ['code','qty','amount','price','strategy']) field(name).setCustomValidity('');
    let problem = '';
    if (assign && code.length===6 && !held && !chooseKind) {
      problem = `${ownerName || '转出组合'}暂无此代码的已确认持仓。`;
      field('code').setCustomValidity(problem);
    } else if (fund && !finite(fund.nav)) {
      problem = '场外基金缺少单位净值，请先更新估值。';
      field('code').setCustomValidity(problem);
    } else if (fund && field('strategy').value && !allocationIds.has(field('strategy').value)) {
      problem = `场外基金只能在${data.strategies.filter(row=>allocationIds.has(row.id)).map(row=>row.name).join('和')}之间划转。`;
      field('strategy').setCustomValidity(problem);
    } else if (['sell','assign'].includes(side) && Number(field('qty').value)>held) {
      problem = `份额超过${ownerName || '所选策略'}持仓（${shares} 份）。`;
      field('qty').setCustomValidity(problem);
    } else if (side==='buy' && field('qty').value && Number(field('qty').value)%100!==0) {
      problem = '买入份额须为100份的整数倍。'; field('qty').setCustomValidity(problem);
    } else if (internal && source.value===field('strategy').value) {
      problem = '请选择不同的划转来源和目标。'; field('strategy').setCustomValidity(problem);
    } else if (!assign && location?.id && Math.round((location.cash+delta)*100)<0) {
      problem='所选交易账户现金不足。';field(trade?'price':'amount').setCustomValidity(problem);
    } else if (!assign && finite(cash) && Math.round((cash+delta)*100)<0) {
      problem = `${ownerName}现金不足${side==='buy' ? '，可先通过“划转现金”补充' : ''}。`;
      if (!automaticFee) field(trade ? 'price' : 'amount').setCustomValidity(problem);
    }
    const validation = document.querySelector('#entry-validation');
    validation.hidden = !problem; validation.textContent = problem;
  }
  form.oninput = event => {
    if (event?.target === field('code') || event?.target === field('from_strategy')) kind.value = '';
    requestId=crypto.randomUUID(); error.hidden=true; update();
  };
  form.onchange = form.oninput;
  document.querySelectorAll('[data-entry-mode]').forEach(button=>button.onclick=()=>{
    const mode = button.dataset.entryMode;
    const side = mode==='trade' ? 'buy' : mode==='cash' ? 'deposit' : mode;
    // 先恢复选项，避免浏览器拒绝选中上一类别中被禁用的选项。
    for (const option of field('side').options) option.disabled=false;
    field('side').value = side;
    kind.value = '';
    if (['assign','cash_transfer'].includes(mode) && field('from_strategy').value===field('strategy').value) field('strategy').value = field('from_strategy').value==='core' ? 'unassigned' : 'core';
    form.oninput();
  });
  document.querySelector('#entry-all').onclick = () => {
    const owner=field(field('side').value==='assign'?'from_strategy':'strategy').value;
    field('qty').value = account.trading_accounts ? account.custody_positions.find(r=>r.account===field('trading_account').value && r.code===field('code').value && r.strategy===owner && r.kind===(entryPosition().fund?'fund':'listed'))?.qty || '' : entryPosition().held || '';
    form.oninput();
  };
  document.querySelector('#event-cancel').onclick = () => { if (!saving) dialog.close(); };
  dialog.oncancel = event => { if (saving) event.preventDefault(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (saving || fields.disabled) return;
    if (!form.reportValidity()) return;
    saving = true; save.disabled = true; next.disabled = true; fields.disabled = true; error.hidden = true;
    const keepOpen = event.submitter===next;
    const side = field('side').value;
    const payload = {request_id:requestId, revision:account.revision, date:field('date').value, side};
    if(options.edit) payload.event_id=options.edit.id;
    if(account.trading_accounts && side!=='cash_transfer') payload.trading_account=field('trading_account').value;
    if (side==='assign') {
      Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), strategy:field('strategy').value, from_strategy:field('from_strategy').value});
      if (entryPosition().fund) payload.kind = 'fund';
    } else if (['buy','sell'].includes(side)) {
      Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), price:Number(field('price').value), strategy:field('strategy').value});
      if (options.edit || field('fee').value !== '') payload.fee = Number(field('fee').value);
    } else {
      payload.amount=Number(field('amount').value);
      payload.strategy=field('strategy').value;
      if(side==='cash_transfer') payload.from_strategy=field('from_strategy').value;
    }
    async function finish() {
      saving = true;
      refresh.disabled = true;
      try {
        await reloadAccount();
      } catch (failure) {
        saving = false; refresh.disabled = false; refresh.hidden = false;
        error.textContent = '本笔已保存，账户刷新失败。请点击“刷新账户”核对后继续。'; error.hidden=false;
        return;
      }
      saving = false;
      if (!keepOpen) dialog.close();
      const receipt = showEntryReceipt(options.edit?.id || payload.request_id, !!options.edit);
      if (keepOpen) {
        openEntry(data.accounts[0], {side}, payload.strategy, {date:payload.date,fromStrategy:payload.from_strategy,success:`${payload.date} · ${receipt}。可继续记下一笔。`});
      }
    }
    refresh.onclick = finish;
    try {
      await accountRequest(options.edit ? '/api/etf/event/edit' : '/api/etf/event', payload);
    } catch (failure) {
      saving = false; fields.disabled = false;
      error.textContent = failure.message; error.hidden=false; save.disabled=false; next.disabled=false;
      return;
    }
    await finish();
  };
  update(); if (!dialog.open) dialog.showModal();
  field(['buy','sell','assign'].includes(field('side').value) ? suggestion?.code && strategy ? 'qty' : 'code' : 'amount').focus();
}

function allocationRows(account) {
  const rows = data.strategies.map(strategy => ({...strategy, target:strategy.budget_pct}));
  const values = account.strategy_positions.filter(row => row.strategy === 'unassigned').map(row => row.market_value);
  const holdings = values.every(finite) ? values.reduce((sum, value) => sum + value, 0) : null;
  const cash = account.cash_by_strategy.unassigned;
  const assets = finite(holdings) ? holdings + cash : null;
  const target = rows.some(row => finite(row.target)) ? Math.max(0, 1 - rows.reduce((sum, row) => sum + (row.target || 0), 0)) : null;
  const deviation = finite(assets) && finite(account.equity) && finite(target) ? Number((assets - account.equity * target).toFixed(2)) : null;
  rows.push({id:'unassigned', name:'未分配', invested:holdings, cash, assets, target, deviation,
    actual_pct:account.equity > 0 && finite(assets) ? assets / account.equity : null});
  return rows;
}

function strategyAllocation(account) {
  const rows = allocationRows(account);
  const bar = row => {
    const actual = finite(row.actual_pct) ? row.actual_pct * 100 : null;
    const target = finite(row.target) ? row.target * 100 : null;
    const gap = row.deviation;
    const deviation = !finite(gap) ? '' : gap === 0 ? '已配平' : `${gap > 0 ? '超配' : '低配'} ${money(Math.abs(gap))} 元`;
    const emphasized = actual !== null && target !== null && !isAllocationBalanced(row.actual_pct - row.target);
    const name = row.id === 'unassigned' ? `<strong>${esc(row.name)}</strong>` : `<button type="button" class="allocation-strategy-link" data-strategy-tab="${esc(row.id)}">${esc(row.name)}</button>`;
    const pending = account.strategy_pending_fund_value?.[row.id] || 0;
    const positions = finite(row.invested) ? `${money(row.invested - pending)} 元` : '待估值';
    return `<div class="allocation-row">
      <div class="allocation-row-top">${name}<b>${finite(row.assets) ? `${money(row.assets)} 元` : '待估值'}</b></div>
      <div class="allocation-row-labels"><span>实际 ${actual === null ? (finite(account.equity) ? '—' : '待估值') : `${actual.toFixed(1)}%`}</span><span>${target === null ? '目标未设置' : `目标 ${target.toFixed(1)}%`}</span></div>
      <div class="allocation-track" aria-hidden="true">${actual === null ? '' : `<div class="allocation-fill allocation-${esc(row.id)}" style="width:${Math.max(0, Math.min(100, actual))}%"></div>`}${target === null ? '' : `<i class="allocation-target" style="left:${target}%"></i>`}</div>
      <div class="allocation-row-foot"><span>持仓 ${positions} · 现金 ${money(row.cash)} 元${pending ? ` · 在途 ${money(pending)} 元` : ''}</span><strong class="${emphasized ? 'allocation-gap' : ''}">${deviation}</strong></div>
    </div>`;
  };
  const layer = (title, hint, groupRows) => `<div class="allocation-layer"><div class="allocation-layer-head"><h3>${title}</h3><span>${hint}</span></div>${groupRows.map(bar).join('')}</div>`;
  const allocation = rows.filter(row => isAllocation(row));
  const satellites = rows.filter(row => row.id !== 'unassigned' && !isAllocation(row));
  const unassigned = rows.filter(row => row.id === 'unassigned' && row.assets !== 0);
  const bars = [layer('配置层', '股债配置两腿，低频复核', allocation),
    satellites.length ? layer('量化卫星', '小预算试验，错了砍预算', satellites) : '',
    unassigned.map(bar).join('')].join('');
  return `<section class="section strategy-panel" aria-labelledby="strategy-allocation-title">
    <div class="section-heading"><h2 id="strategy-allocation-title">策略配比</h2>
      <div class="allocation-actions"><button type="button" data-cash-rebalance class="text-button">现金再平衡</button>${account.can_record ? '<a href="/settings/budgets" class="text-button">编辑目标占比</a>' : ''}<a class="text-button" href="/api/etf/export">导出配置与持仓</a></div>
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
  return `<th class="sortable${active ? ' sorted' : ''}" data-sort-key="${key}" title="点击排序">${label}${arrow}</th>`;
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
    th.onclick = () => {
      const key = th.dataset.sortKey;
      coreSort = coreSort.key === key ? {key, dir: -coreSort.dir} : {key, dir: key === 'deviation' ? -1 : 1};
      refreshCoreTable(strategy);
    };
  });
}

function strategyPositionTable(strategy) {
  if (!strategy.target_weights) return rotationPositionTable(strategy);
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

function openFundTrade(account, side, code, strategy, edit=null) {
  const dialog = document.querySelector('#fund-dialog');
  const form = document.querySelector('#fund-form');
  const fields = document.querySelector('#fund-fields');
  const save = document.querySelector('#fund-save');
  const error = document.querySelector('#fund-error');
  const preview = document.querySelector('#fund-preview');
  const field = name => form.elements.namedItem(name);
  const buy = side !== 'fund_redeem';
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
  const allocations = data.strategies.filter(isAllocation);
  const holders = allocations.filter(row=>account.strategy_fund_positions?.[row.id]?.[code]?.qty > 0);
  const choices = strategy ? allocations.filter(row=>row.id===strategy) : buy ? allocations : holders;
  owner.innerHTML = `<option value="">请选择策略</option>${strategyOptions(choices)}`;
  owner.value = strategy || (holders.length===1 ? holders[0].id : buy && !holders.length ? 'core' : '');
  owner.disabled = !!strategy;
  fields.disabled = false;
  save.disabled = false;
  fetchNav.disabled = false;
  save.textContent = edit ? '保存修改' : '保存记账';
  error.hidden = true;
  document.querySelector('#fund-title').textContent = edit ? `修改${SIDES[side]}` : SIDES[side];
  document.querySelector('#fund-help').textContent = pending
    ? '扣款计入在途资金。每日更新或刷新估值时查到买入日净值，就会自动确认份额。'
    : confirming ? '按交易确认单更正份额。' : '请按交易确认单和实际资金变动记账。';
  document.querySelector('#fund-amount-label').textContent = buy ? '实际扣款 / 元' : '实际到账 / 元';
  document.querySelector('#fund-qty-label').textContent = buy ? '份额（自动计算，可修正）' : '确认赎回份额';
  options.hidden = !buy;
  options.open = buy && !!edit && !confirming;
  document.querySelector('#fund-buy-tools-label').textContent = accountFee ? '买入日' : '买入日与费用';
  document.querySelector('#fund-fee-kind-field').hidden = !!accountFee;
  summary.hidden = !pending && !confirming;
  document.querySelector('#fund-code-field').hidden = pending && !!code && !edit;
  document.querySelector('#fund-record-day-field').hidden = pending && !edit;
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
  field('qty').value = edit?.qty ?? '';
  field('date').min = account.min_date || account.trades[0]?.date || account.start_date;
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
    const location=custodySelect(account,field('trading_account'),{side,kind:'fund',code,strategy:owner.value,edit});
    const held = location ? location.qty : account.strategy_fund_positions?.[owner.value]?.[code]?.qty || 0;
    const cash = account.cash_by_strategy[owner.value];
    const amount = Number(field('amount').value);
    const kind = field('fee_kind').value;
    const usesFee = buy && !accountFee && (kind === 'rate' || kind === 'fixed');
    document.querySelector('#fund-fee-field').hidden = !usesFee;
    document.querySelector('#fund-fee-label').textContent = kind === 'rate' ? '实际费率 / %' : '申购费 / 元';
    field('fee').disabled = !usesFee || confirming;
    field('nav_date').max = field('date').value;
    field('qty').max = buy ? '1000000000' : String(held);
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
      ? `${strategyName(owner.value)}现金 ${money(cash)} 元 · 持有 ${fundShares(held)} 份${amount > 0 ? ` · 记账后现金 ${money(cash + (buy ? -amount : amount))} 元` : ''}`
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
      save.textContent = saved ? '刷新账户' : edit ? '保存修改' : '保存记账';
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
  form.reset();error.hidden=true;fields.disabled=false;button.disabled=false;button.textContent='确认分配';
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
  document.querySelector('#cash-strategies').innerHTML=data.strategies.map(({id,name}) =>
    `<label>${esc(name)} / 元<input name="${esc(id)}" type="number" min="0" step="0.01" required></label>`).join('');
  const inputs = data.strategies.map(({id}) => form.elements.namedItem(id));
  for (const input of inputs) input.value=((options.edit?.cash_allocations || account.cash_by_strategy)[input.name] || 0).toFixed(2);
  function update() {
    requestId=crypto.randomUUID();
    preview.hidden=true;
    const allocated=inputs.reduce((sum,input)=>sum+Number(input.value),0);
    const remaining=Math.round((account.cash-allocated)*100)/100;
    const message=remaining<0 ? `超出账户现金 ${money(-remaining)} 元` : `未分配现金 ${money(remaining)} 元`;
    document.querySelector('#cash-remaining').textContent=message;
    document.querySelector('#cash-remaining').classList.toggle('invalid',remaining<0);
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
      for(const [key,value] of Object.entries(plan.cash_allocations)) form.elements.namedItem(key).value=value.toFixed(2);
      update();
      const rows=plan.rows.map(row=>`<tr><td>${esc(row.name)}</td><td>${row.change>0?'+':''}${money(row.change)}</td><td>${money(row.after_cash)}</td><td>${row.deviation===null?'目标未设':`${row.deviation>0?'超配':row.deviation<0?'低配':'已对齐'}${row.deviation===0?'':` ${money(Math.abs(row.deviation))}`}`}</td></tr>`).join('');
      const overweight=plan.rows.filter(row=>row.holding_overweight).map(row=>row.name);
      const note=plan.shortfall>0?`现金不足以补齐全部目标，按各项现金缺口比例分配，尚差 ${money(plan.shortfall)} 元。`:'可通过本次现金调整达到已设置的目标配比。';
      const basis = valuationLabel(plan.valuation) + (Object.keys(account.fund_positions || {}).length ? ' · 场外按各自净值日' : '');
      preview.innerHTML=`<p class="entry-preview">${note}${overweight.length?` ${esc(overweight.join('、'))}仅持仓已超配，需调整持仓才能完全对齐。`:''}</p><p class="subtle">计算依据：${esc(basis)}；未设置目标的策略保留原现金。</p><div class="table-wrap"><table class="cash-plan-table"><thead><tr><th>策略</th><th>现金调整 / 元</th><th>调整后现金 / 元</th><th>调整后偏离 / 元</th></tr></thead><tbody>${rows}</tbody></table></div><p class="subtle">仅调整策略现金，持仓与总现金不变；检查后确认保存。</p>`;
      preview.hidden=false;
    } catch(failure) {error.textContent=failure.message;error.hidden=false;}
    finally {planning=false;rebalanceButton.disabled=!finite(account.equity);button.disabled=false;}
  };
  form.onsubmit=async event=>{
    event.preventDefault();if(saving || planning)return;
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

function openDcaPlans(account, strategy, code='') {
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
    : `${strategyName(strategy)}定投计划`;
  const nameHint=document.querySelector('#dca-fund-name');
  const frequencySelect=form.elements.namedItem('frequency');
  const anchorSelect=form.elements.namedItem('anchor');
  const anchorLabel=document.querySelector('#dca-anchor-label');
  const codeInput=form.elements.namedItem('code');
  const plans=(account.dca?.plans || []).filter(plan=>
    (plan.strategy || 'core')===strategy && (!code || plan.code===code));
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
    plansBox.innerHTML=plans.length ? `<table><thead><tr><th>基金</th><th>计划</th><th>状态</th><th></th></tr></thead><tbody>${plans.map(plan=>{
      const name=fundDisplayName(plan.code);
      let cadence='每个交易日';
      if (plan.frequency==='monthly') cadence=`每月 ${plan.anchor} 日`;
      if (plan.frequency==='weekly') cadence=`每周${FREQUENCY_ANCHORS.weekly[plan.anchor-1][1].slice(1)}`;
      return `<tr><td>${name?`${esc(name)}<small>${esc(plan.code)}</small>`:esc(plan.code)}</td><td>${cadence} · ${money(plan.amount)} 元${account.trading_accounts?`<small>${plan.trading_account?esc(custodyName(account,plan.trading_account)):'跟随默认账户'}</small>`:''}${plan.note?`<small>${esc(plan.note)}</small>`:''}</td><td>${plan.enabled?'启用中':'已停用'}</td><td><button type="button" class="text-button" data-dca-edit="${esc(plan.id)}">${code || plan.enabled?'修改':'启用'}</button> <button type="button" class="text-button" data-dca-toggle="${esc(plan.id)}">${plan.enabled?'停用':'启用'}</button> <button type="button" class="text-button" data-dca-delete="${esc(plan.id)}">删除</button></td></tr>`;
    }).join('')}</tbody></table>` : '<div class="empty">暂无定投计划。请在基金 App 设定自动扣款后，把计划录到这里。</div>';
    plansBox.querySelectorAll('[data-dca-edit]').forEach(button=>button.onclick=()=>{
      const plan=plans.find(row=>row.id===button.dataset.dcaEdit);
      if (plan) editPlan(plan);
    });
    plansBox.querySelectorAll('[data-dca-toggle]').forEach(button=>button.onclick=()=>{
      const plan=plans.find(row=>row.id===button.dataset.dcaToggle);
      if (!plan) return;
      withGuard(button, () => accountRequest('/api/etf/dca/save', {...plan, strategy, enabled:!plan.enabled}),
        plan.enabled?'定投计划已停用':'定投计划已启用');
    });
    plansBox.querySelectorAll('[data-dca-delete]').forEach(button=>button.onclick=()=>
      withGuard(button, () => accountRequest('/api/etf/dca/delete', {id:button.dataset.dcaDelete}), '定投计划已删除'));
  }
  frequencySelect.onchange=fillAnchors;
  form.elements.namedItem('code').oninput=event=>fillFundName(dialog, session, event.target, nameHint, nameState);
  document.querySelector('#dca-cancel').onclick=()=>{if(!saving)dialog.close();};
  dialog.oncancel=event=>{if(saving)event.preventDefault();};
  form.onsubmit=async event=>{
    event.preventDefault();
    if (saving || save.hidden) return;
    if (!form.reportValidity()) return;
    saving=true;save.disabled=true;fields.disabled=true;error.hidden=true;
    const payload={code:form.elements.namedItem('code').value.trim(),
      strategy,
      amount:Number(form.elements.namedItem('amount').value),
      frequency:frequencySelect.value,
      note:form.elements.namedItem('note').value.trim(),
      enabled: code && editing ? editing.enabled : true};
    if (!anchorSelect.disabled) payload.anchor=Number(anchorSelect.value);
    if (editing) payload.id=editing.id;
    if(account.trading_accounts) payload.trading_account=planAccount.value;
    try {
      await accountRequest('/api/etf/dca/save', payload);
      await reloadAccount();
      dialog.close();
      document.querySelector('#status').textContent=editing?'定投计划已保存':'定投计划已添加';
    } catch (failure) {
      error.textContent=failure.message;error.hidden=false;
    } finally {
      saving=false;fields.disabled=false;save.disabled=false;save.textContent='保存计划';
    }
  };
  fillAnchors();
  renderPlans();
  if (code && plans.length===1) editPlan(plans[0]);
  dialog.showModal();
}
