const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = value => typeof value === 'number' && Number.isFinite(value);
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
const SIDES = {buy:'买入', sell:'卖出', dividend:'分红', deposit:'入金', withdraw:'出金', assign:'持仓划转', cash_assign:'现金分配', cash_transfer:'现金划转'};
const STRATEGY_NAMES = {core:'多资产配置', rotation:'跨资产动量', quality:'质量动量', unassigned:'未分配'};
const HOLDING_NAMES = {etf:'ETF', stock:'股票', other:'其他持仓'};
const REPORT_PREFIX = {core:'etf_weekly_', satellite:'industry_momentum_', rotation:'etf_rotation_', quality:'quality_pool_'};
const ALLOCATION_TOLERANCE = 0.01;
const allocationToleranceNote = `${ALLOCATION_TOLERANCE*100} 个百分点内视为配平`;
const isAllocationBalanced = deviation => Math.abs(deviation) <= ALLOCATION_TOLERANCE + Number.EPSILON;
let data, selected = 0, chartRange = 'all', chartObserver;
let selectedReport, reportRequest, configRequest;
let showAllRecords = false;
let holdingKind = '';
const reportCache = new Map();
let quoteRequestPending = false, quotePollTimer, quotePolling = false, quoteError = '';
const quoteTime = value => new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false});
const quotePrice = row => `${price(row.close)}${!finite(row.close) ? '<small class="price-pending">待估值</small>' : row.quote_missing && !data.accounts[0]?.quote_refresh?.running ? '<small class="price-pending">保留上一组估值</small>' : ''}${row.quote_fetched_at ? `<small>获取于 ${esc(quoteTime(row.quote_fetched_at))}</small>` : ''}`;

function valuationLabel(valuation) {
  if (valuation.mode === 'live') {
    return `行情估算 · ${valuation.fetched_at ? `获取于 ${quoteTime(valuation.fetched_at)}` : valuation.date}`;
  }
  return `${valuation.mode === 'initial' ? '期初估值' : '收盘估值'} · ${valuation.date}`;
}

function updateQuoteControls() {
  const account = data.accounts[0];
  const active = selected===0 ? account : [...data.strategies,...data.observations][selected-1];
  document.querySelector('#quote-panel').hidden = !account.can_record || (selected!==0 && active?.account_id!=='etf');
  const state = account.quote_refresh;
  const running = quoteRequestPending || state?.running;
  const button = document.querySelector('#quote-refresh');
  button.disabled = !!running;
  button.setAttribute('aria-busy',String(!!running));
  button.textContent = running ? '刷新中…' : '刷新行情';
  const valuation=account.valuation;
  let note = valuation ? valuationLabel(valuation) : '';
  let message = '';
  if (running) note = `正在刷新行情…${state?.estimated ? ' · 暂显示上次估值' : ''}`;
  else if (state && !state.applicable) message = '估值已变化，请刷新行情。';
  else if (state?.applicable && state.missing?.length) {
    const names = state.missing.map(code=>account.positions.find(row=>row.code===code)?.name || code);
    message = `${names.join('、')}未能更新，本轮不切换，整组保留上一份完整估值。`;
  } else if (state?.error) message = '行情暂时无法更新，请稍后重试。';
  if (quoteError) message = quoteError;
  if(valuation?.missing?.length) message += `${message?' ':''}${valuation.missing.join('、')}缺少同日价格，总资产与相关盈亏待估值；可刷新行情。`;
  const status = document.querySelector('#quote-status');
  status.textContent = selected === 0 && !running ? '' : note;
  const feedback = document.querySelector('#quote-message');
  feedback.textContent = message; feedback.hidden = !message;
}

function scheduleQuotePoll() {
  clearTimeout(quotePollTimer);
  if (data.accounts[0]?.quote_refresh?.running) quotePollTimer = setTimeout(pollQuotes, 1500);
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
    if (data.accounts[0]?.quote_refresh) data.accounts[0].quote_refresh.running = false;
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
    quoteError = error.message || '行情刷新失败，请稍后重试。';
    if (data.accounts[0]?.quote_refresh) data.accounts[0].quote_refresh.running = false;
  }
  finally { quoteRequestPending = false; updateQuoteControls(); }
};

function positionTable(rows) {
  if (!rows.length) return '<div class="empty">暂无持仓</div>';
  const priced = rows.every(row => finite(row.close) && finite(row.count));
  const valueOf = row => row.count * (priced ? row.close : row.cost_price);
  const total = rows.reduce((sum, row) => sum + valueOf(row), 0);
  const body = rows.map(row => `<tr><td>${esc(row.name)}<small>${esc(row.code)}</small></td><td>${price(row.close)}</td><td>${price(row.cost_price)}</td><td>${ratio(valueOf(row),total)}</td></tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr><th>标的</th><th>最近收盘</th><th>成本价</th><th>${priced ? '市值占比' : '成本占比'}</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function tradeTable(rows) {
  if (!rows.length) return '<div class="empty">暂无流水</div>';
  return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>标的</th><th>类型</th><th>数量</th><th>价格</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.date)}</td><td>${esc(row.name)}<small>${esc(row.code)}</small></td><td>${esc(SIDES[row.side] || row.side)}</td><td>${number(row.qty)}</td><td>${price(row.price)}</td></tr>`).join('')}</tbody></table></div>`;
}

function warnings(account) {
  const messages = [...(account?.error ? [account.error] : []), ...(account?.warnings || [])];
  const labels = {'台账文件不存在':'尚无模拟记录。','投资账户尚未初始化':'尚未设置期初资产。','账户数据读取失败，请检查服务器日志。':'账户暂时无法加载，请稍后重试。',
    '最新成绩待重新估值，曲线保留最近有效数据。':'收盘收益待更新，暂显示最近有效记录。',
    '最近收盘成绩不完整或流水已变更，显示最近有效成绩。':'收盘收益待更新，暂显示最近有效记录。'};
  return [...new Set(messages.map(message=>labels[message] || message))].map(message => `<div class="warning">${esc(message)}</div>`).join('');
}

function chartSection(account, note = '') {
  const history = account?.history || [];
  const hasHistory = history.some(row=>finite(row.nav) && row.nav>0);
  const hasBenchmark = !!account?.benchmark_name && history.some(row=>finite(row.benchmark_nav) && row.benchmark_nav>0);
  const start = account?.start_date || account?.history?.[0]?.date;
  const basis = hasBenchmark ? `基准：${esc(account.benchmark_name)}` : start ? `起始日期：${esc(start)}` : '';
  const details = [note ? esc(note) : '',basis].filter(Boolean).join(' · ');
  return `<section class="section chart-section" aria-labelledby="chart-title">
    <div class="section-heading"><div><h2 id="chart-title">${esc(accountName(account))} · 收盘收益</h2></div>
      <div class="range-buttons" role="group" aria-label="收益曲线时间范围" ${!hasHistory ? 'hidden' : ''}>${[['30','近30天'],['90','近90天'],['all','全部']].map(([value,label]) => `<button type="button" data-range="${value}" class="${chartRange === value ? 'active' : ''}" aria-pressed="${chartRange === value}">${label}</button>`).join('')}</div></div>
    <div class="chart-summary" aria-live="polite">
      <div class="chart-metric primary"><small><i class="legend-line"></i>累计收益率</small><b id="chart-nav">—</b></div>
      <div class="chart-metric benchmark" ${!hasBenchmark ? 'hidden' : ''}><small><i class="legend-line benchmark"></i>同期基准</small><b id="chart-base">—</b></div>
      <div class="chart-metric" ${!hasBenchmark ? 'hidden' : ''}><small>超额 / 百分点</small><b id="chart-excess">—</b></div>
    </div><div id="chart-date" class="chart-date"></div>
    <div id="chart-stage" class="chart-stage"></div>
    <input id="chart-cursor" class="chart-slider" type="range" aria-label="选择日期查看收益" hidden>
    <div class="chart-foot" ${!details && !hasHistory ? 'hidden' : ''}><span>${details}</span><span ${!hasHistory || history.length<2 ? 'hidden' : ''}>拖动查看每日收益</span></div>
  </section>`;
}

function chartPoints(history, range) {
  if (!history.length || range === 'all') return history;
  const cutoff = timestamp(history[history.length - 1].date) - Number(range) * 86400000;
  return history.filter(row => timestamp(row.date) >= cutoff);
}

function drawChart(account) {
  const stage = document.querySelector('#chart-stage');
  const slider = document.querySelector('#chart-cursor');
  if (!stage || !slider) return;
  const points = chartPoints(account?.history || [], chartRange);
  const value = (row, key) => finite(row[key]) && row[key] > 0 ? (row[key] - 1) * 100 : null;
  const values = points.flatMap(row => [value(row,'nav'),value(row,'benchmark_nav')]).filter(finite);
  stage.classList.toggle('is-empty',!points.length || !values.length);
  if (!points.length || !values.length) {
    stage.innerHTML = '<div class="empty">暂无收益记录</div>';
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
  stage.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="账户累计收益与同期基准对比曲线"><title>账户累计收益与同期基准</title>${grid}<line class="chart-zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/><path class="chart-benchmark" d="${pathFor('benchmark_nav')}"/><path class="chart-account" d="${pathFor('nav')}"/>${labels}<g id="chart-marker"></g></svg>`;
  const svg = stage.querySelector('svg');
  slider.hidden = points.length < 2;
  slider.min = 0; slider.max = points.length-1; slider.step = 1;
  let latest = points.length-1;
  while (latest>0 && value(points[latest],'nav') === null) latest--;
  function focus(index) {
    const row = points[index];
    document.querySelector('#chart-nav').textContent = percent(row.nav);
    document.querySelector('#chart-base').textContent = percent(row.benchmark_nav);
    document.querySelector('#chart-excess').textContent = finite(row.nav) && finite(row.benchmark_nav) ? signed((row.nav-row.benchmark_nav)*100) : '—';
    document.querySelector('#chart-date').textContent = `${row.date}${value(row,'nav') === null ? ' · 当日数据缺失' : ''}`;
    slider.value = index;
    slider.setAttribute('aria-valuetext',`${row.date}，账户收益 ${percent(row.nav)}，基准收益 ${percent(row.benchmark_nav)}`);
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
  svg.onpointermove = pointer;
  svg.onpointerdown = pointer;
  svg.onpointerleave = () => focus(latest);
  slider.oninput = () => focus(Number(slider.value));
  focus(latest);
}

function summaryPage() {
  const account = data.accounts[0];
  return `${warnings(account)}${account.can_record ? accountDetails(account) : chartSection(account)}`;
}

function entryActions() {
  return '<div class="allocation-actions"><button type="button" id="manual-entry" class="primary-button">＋ 记一笔</button><button type="button" data-cash-rebalance class="text-button">现金再平衡</button><button type="button" id="cash-transfer" class="text-button">划转现金</button><button type="button" id="position-transfer" class="text-button">持仓划转</button><button type="button" data-open-cash class="text-button">分配现金</button></div>';
}

function profitNote() {
  return '<details class="calculation-note"><summary>盈亏怎么算</summary><p>累计盈亏从账户期初估值开始计算，包含已实现盈亏、持仓涨跌、分红与手续费，入出金及内部现金划转不计盈利。持仓划转前的盈亏留在原策略，之后归接收策略。期初估值并非历史买入成本。</p><p>全部持仓统一使用同一估值日的收盘价，或同一轮完整获取的行情。成交价不补入展示估值；缺少同日价格时，相关市值和盈亏显示待估值。现金及持仓数量使用最新账目。下方收盘收益率只使用有效收盘记录，独立模拟收益与实际账户分开计算。</p></details>';
}

function reconciliation(account) {
  const value = amount => finite(amount) ? money(amount) : '待估值';
  const groups = Object.entries(account.holdings_by_kind).map(([kind, amount]) =>
    `<button type="button" data-holding-filter="${kind}" aria-controls="account-holdings"><span>${HOLDING_NAMES[kind]}${kind === 'other' ? '' : '市值'}</span><b>${value(amount)}</b></button>`).join('');
  const cash = Object.entries(STRATEGY_NAMES).map(([id, name]) =>
    `<div><dt>${name}</dt><dd>${money(account.cash_by_strategy[id])}</dd></div>`).join('');
  return `<section class="reconciliation" aria-label="账户对账">
    <div class="reconciliation-top"><div><span>总资产 / 元</span><strong>${value(account.equity)}</strong><small>${esc(valuationLabel(account.valuation))}</small></div><div class="reconciliation-capital"><span>累计投入 / 元</span><b>${money(account.net_contributed)}</b><small>期初资产 + 净入金</small><span>累计盈亏 / 元</span><b>${account.pnl > 0 ? '+' : ''}${value(account.pnl)}</b></div></div>
    <div class="reconciliation-breakdown" role="group" aria-label="资产明细（元）">${groups}<button type="button" id="account-cash-toggle" aria-controls="account-cash-detail" aria-expanded="false"><span>现金</span><b>${money(account.cash)}</b></button></div>
    <dl id="account-cash-detail" class="reconciliation-cash" aria-label="各策略现金（元）" hidden>${cash}</dl>
  </section>`;
}

function positionActions(code, strategy, held=true, suggestion=null) {
  const button = side => `<button type="button" class="text-button" ${suggestion?.side===side ? `data-fill="${suggestion.index}"` : `data-position-side="${side}" data-code="${esc(code)}" data-strategy="${esc(strategy)}"`}>${side==='buy' ? '买入' : '卖出'}</button>`;
  return `<div class="position-actions">${button('buy')}${held ? button('sell') : ''}</div>`;
}

function accountDetails(account) {
  const filters = Object.keys(account.holdings_by_kind).map(kind => `<option value="${kind}">${HOLDING_NAMES[kind]}</option>`).join('');
  const rows = account.positions.map(row => {
    const owners = account.strategy_positions.filter(item=>item.code===row.code && item.count>0);
    return `<tr data-holding-kind="${row.kind}"><td>${esc(row.name)}<small>${esc(row.code)}<span class="holding-mobile-count"> · ${number(row.count)} 份</span></small></td><td>${number(row.count)}</td><td>${quotePrice(row)}</td><td>${finite(row.market_value) ? money(row.market_value) : '待估值'}</td><td>${ratio(row.market_value,account.equity)}</td><td>${positionActions(row.code, owners.length===1 ? owners[0].strategy : '')}</td></tr>`;
  }).join('');
  return `${reconciliation(account)}<nav class="account-shortcuts" aria-label="账户快捷入口"><button type="button" class="secondary-button" data-holding-filter="" aria-controls="account-holdings">查看持仓</button><button type="button" class="secondary-button" id="account-records-link" aria-controls="account-records">查看流水</button></nav>
    ${profitNote()}<div class="account-entry-bar">${entryActions()}</div>
    ${allocationCharts(account)}${chartSection(account)}
    <section id="account-holdings" class="section" aria-labelledby="account-holdings-title" tabindex="-1"><div class="section-heading"><h2 id="account-holdings-title">账户持仓</h2><select id="holding-filter" aria-label="持仓类别"><option value="">全部持仓</option>${filters}</select></div><div class="table-wrap"><table class="account-holdings-table"><thead><tr><th scope="col">标的</th><th scope="col">份额</th><th scope="col">价格 / 元</th><th scope="col">市值 / 元</th><th scope="col">资产占比</th><th scope="col">记账</th></tr></thead><tbody>${rows}<tr id="holdings-empty" hidden><td colspan="6" class="empty">暂无持仓</td></tr></tbody></table></div></section>
    <section id="account-records" class="section" aria-labelledby="account-records-title" tabindex="-1"><div class="section-heading"><h2 id="account-records-title">近期流水</h2><span class="subtle">${showAllRecords?'全部历史':'最近 20 笔'}</span></div>${recordTable(account.trades)}</section>`;
}

function recordTable(rows) {
  if (!rows.length) return '<div class="empty">暂无流水</div>';
  const shown = showAllRecords ? rows : rows.slice(0,20);
  const body = shown.map(row => {
    const name = row.code ? data.accounts[0].positions.find(item=>item.code===row.code)?.name || row.name || row.code : '';
    let detail = [name, row.code && name!==row.code ? row.code : '', STRATEGY_NAMES[row.strategy]].filter(Boolean).join(' · ') || '账户资金';
    if (row.side === 'assign') detail = `${row.code} · ${STRATEGY_NAMES[row.from_strategy]} → ${STRATEGY_NAMES[row.strategy]}`;
    if (row.side === 'cash_transfer') detail = `${STRATEGY_NAMES[row.from_strategy]} → ${STRATEGY_NAMES[row.strategy]}`;
    if (row.side === 'cash_assign') {
      detail = Object.entries(row.cash_allocations).map(([key,amount]) => `${STRATEGY_NAMES[key]} ${price(amount)} 元`).join(' · ');
    }
    return `<tr><td>${esc(row.date)}</td><td>${esc(SIDES[row.side])}<small>${esc(detail)}</small><button type="button" class="text-button" data-edit-event="${esc(row.id)}">修改</button></td><td>${number(row.qty)}</td><td>${price(row.price ?? row.amount)}</td><td>${price(row.fee)}</td></tr>`;
  }).join('');
  return `${rows.length>20 ? `<button type="button" id="all-records" class="text-button">${showAllRecords?'收起':'查看全部'} ${rows.length} 笔流水</button>` : ''}<div class="table-wrap"><table><thead><tr><th>日期</th><th>类型 / 明细</th><th>份额</th><th>价格或金额 / 元</th><th>手续费 / 元</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function fundedStrategyPage(strategy) {
  const scope = strategy.external_fund_value>0 ? '券商' : '';
  const stats = [[finite(strategy.budget) ? money(strategy.budget) : finite(strategy.budget_pct)?'待估值':'待设置',scope+'目标资产 / 元'],[money(strategy.invested),scope+'持仓市值 / 元'],[`${strategy.pnl>0?'+':''}${money(strategy.pnl)}`,scope+'累计盈亏 / 元'],[money(strategy.cash),'策略现金 / 元']];
  const observation = strategy.observation;
  const observationSection = observation ? `${chartSection(observation, '独立模拟，不计入投资账户')}<section class="section"><div class="section-heading"><h2>模拟持仓</h2></div>${positionTable(observation.positions)}</section>` : '';
  const trades = [...(strategy.trades || [])].reverse();
  const canRecord = data.accounts[0].can_record;
  const status = strategy.status && strategy.status!=='已分配' ? `<p class="subtle">${esc(strategy.status)}</p>` : '';
  const coreActions = strategy.id==='core' && canRecord
    ? '<div class="core-actions"><button type="button" data-edit-fund="" class="text-button">录入场外基金</button><button type="button" id="core-settings" class="text-button">编辑配置</button></div>' : '';
  return `<div class="stats">${stats.map(([value,label])=>`<div class="stat"><span>${label}</span><b>${esc(value)}</b></div>`).join('')}</div>${profitNote()}${canRecord ? `<div class="account-entry-bar">${entryActions()}</div>` : ''}<section class="section"><div class="section-heading"><h2>${strategy.id==='core' ? '持仓与目标配比' : '持仓与调仓建议'}</h2>${coreActions}</div>${status}${strategyPositionTable(strategy)}</section><section class="section"><div class="section-heading"><h2>策略流水</h2><span class="subtle">最近 20 笔</span></div>${recordTable(trades)}</section>${observationSection}`;
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

function strategyPage(account) {
  if (account.account_id === 'etf') return fundedStrategyPage(account);
  const p = account.performance;
  const priced = account.positions.filter(row => finite(row.close)).length;
  const closeNote = !account.positions.length ? '' : priced===account.positions.length ? `收盘价截至 ${esc(p?.date)}` : '收盘价不全，占比按成本计算';
  const stats = [[`${number(account.positions.length)} 只`,'持仓数量'],[ratio(p?.invested,p?.equity),'持仓占比'],[ratio(p?.cash,p?.equity),'现金占比'],[ratio(p?.receivables,p?.equity),'应收分红占比']];
  return `${warnings(account)}${chartSection(account)}<div class="stats">${stats.map(([v,label]) => `<div class="stat"><span>${label}</span><b>${v}</b></div>`).join('')}</div><section class="section"><div class="section-heading"><h2>账户持仓</h2><span class="subtle">${closeNote}</span></div>${positionTable(account.positions)}</section><section class="section"><div class="section-heading"><h2>近期流水</h2><span class="subtle">最近 20 笔</span></div>${tradeTable(account.trades)}</section>`;
}

function render() {
  chartObserver?.disconnect();
  updateQuoteControls();
  const tabs = [{id:'etf',name:'账户总览'},...data.strategies,...data.observations];
  document.querySelector('#tabs').innerHTML = tabs.map((tab,index) => `${index===0 ? '<span class="nav-label">投资账户</span>' : index===data.strategies.length+1 ? '<span class="nav-label">策略观察</span>' : ''}<button type="button" role="tab" id="tab-${esc(tab.id)}" class="${index===selected ? 'active' : ''}" aria-selected="${index===selected}" aria-controls="account" tabindex="${index===selected ? 0 : -1}" data-index="${index}">${esc(accountName(tab))}</button>`).join('');
  function activateTab(index) {
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
  const chart = account.observation || account;
  document.querySelector('#account').innerHTML = selected===0 ? summaryPage() : strategyPage(account);
  drawChart(chart);
  bindAccountActions(account);
  document.querySelectorAll('[data-range]').forEach(button => button.onclick=() => {
    chartRange=button.dataset.range;
    document.querySelectorAll('[data-range]').forEach(item => { item.classList.toggle('active',item.dataset.range===chartRange); item.setAttribute('aria-pressed',item.dataset.range===chartRange); });
    drawChart(chart);
  });
  chartObserver = new ResizeObserver(() => { drawChart(chart); measureReport(); });
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

function reportKind(file) {
  if (file.startsWith('etf_weekly_')) return '周度配置';
  return '策略日报';
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
  document.querySelector('#report-kind').textContent=reportKind(file);
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
  document.querySelector('#states').innerHTML=data.states.map(state => `<p><strong>${esc(state.name)}</strong> · ${esc(state.detail)}</p>`).join('');
  render();
  scheduleQuotePoll();
}).catch(() => { document.querySelector('#status').textContent='数据读取失败，请刷新页面重试。'; });

async function accountRequest(path, payload) {
  let response, result;
  try {
    response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Trade-Request':'dashboard'}, body:JSON.stringify(payload)});
    result = await response.json();
  } catch { throw Error('连接中断，请重试。'); }
  if (!response.ok) throw Error(result.error || (path==='/api/etf/refresh' ? '行情暂时无法更新，请稍后重试。' : '保存失败，请重试。'));
  return result;
}

async function reloadAccount({deferWhileRefreshing=false} = {}) {
  const response = await fetch('/api/overview', {cache:'no-store'});
  if (!response.ok) throw Error('页面暂时无法更新，请重试。');
  data = await response.json();
  document.querySelector('#status').textContent=`页面更新于 ${quoteTime(data.generated_at)}`;
  document.querySelector('#states').innerHTML=data.states.map(state => `<p><strong>${esc(state.name)}</strong> · ${esc(state.detail)}</p>`).join('');
  if (!deferWhileRefreshing || !data.accounts[0]?.quote_refresh?.running) render();
}

function bindAccountActions(view) {
  const account = data.accounts[0];
  if (!account?.can_record || (view.id!=='etf' && view.account_id!=='etf')) return;
  if (view.id === 'etf') bindReconciliation(account);
  const strategy = view.account_id==='etf' ? view.id : 'unassigned';
  document.querySelector('#manual-entry').onclick = () => openEntry(account, null, strategy);
  document.querySelector('#position-transfer').onclick = () => openEntry(account, {side:'assign'}, strategy);
  document.querySelector('#cash-transfer').onclick = () => openEntry(account, {side:'cash_transfer'}, strategy);
  const allRecords = document.querySelector('#all-records');
  if (allRecords) allRecords.onclick = () => {showAllRecords=!showAllRecords;render();};
  document.querySelectorAll('[data-edit-event]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try {
      const context=await accountRequest('/api/etf/event/context',{revision:account.revision,event_id:button.dataset.editEvent});
      const before={...account,...context};
      if(context.event.side==='cash_assign') openCashAllocation(before,{edit:context.event});
      else openEntry(before,context.event,context.event.strategy,{edit:context.event});
    } catch(error) {document.querySelector('#status').textContent=error.message;}
    finally {button.disabled=false;}
  });
  document.querySelectorAll('[data-position-side]').forEach(button => button.onclick = () => openEntry(account, {side:button.dataset.positionSide,code:button.dataset.code}, button.dataset.strategy));
  const budgetButton = document.querySelector('#budget-settings');
  if (budgetButton) budgetButton.onclick = () => openBudgets(account);
  const coreButton = document.querySelector('#core-settings');
  if (coreButton) coreButton.onclick = () => openConfiguration();
  const fundButtons = document.querySelectorAll('[data-edit-fund]');
  if (fundButtons.length) {
    const funds = new Map(view.allocation_groups.flatMap(group => group.products)
      .filter(row => row.kind === 'fund').map(row => [row.code, row]));
    for (const button of fundButtons) {
      button.onclick = () => openFundPosition(account, funds.get(button.dataset.editFund));
    }
  }
  const cashButton = document.querySelector('#cash-settings');
  if (cashButton) cashButton.onclick = () => openCashAllocation(account);
  document.querySelectorAll('[data-open-cash]').forEach(button=>button.onclick=()=>openCashAllocation(account));
  document.querySelectorAll('[data-cash-rebalance]').forEach(button=>button.onclick=()=>openCashAllocation(account,{rebalance:true}));
  bindOverviewCharts(account);
  document.querySelectorAll('[data-fill]').forEach(button => button.onclick = () => openEntry(account, view.suggestions[Number(button.dataset.fill)], strategy));
  document.querySelectorAll('[data-dismiss]').forEach(button => button.onclick = async () => {
    button.disabled = true;
    try {
      await accountRequest('/api/etf/dismiss', {revision:account.revision, id:view.suggestions[Number(button.dataset.dismiss)].id});
      await reloadAccount();
    } catch (error) {
      document.querySelector('#status').textContent = error.message;
      button.disabled = false;
    }
  });
}

function bindReconciliation(account) {
  const filter = document.querySelector('#holding-filter');
  const rows = [...document.querySelectorAll('[data-holding-kind]')];
  function showHoldings(kind) {
    holdingKind = Object.hasOwn(account.holdings_by_kind, kind) ? kind : '';
    filter.value = holdingKind;
    for (const row of rows) row.hidden = !!holdingKind && row.dataset.holdingKind !== holdingKind;
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
  document.querySelector('#account-records-link').onclick = () => jumpTo('account-records');
  const cash = document.querySelector('#account-cash-detail');
  document.querySelector('#account-cash-toggle').onclick = event => {
    cash.hidden = !cash.hidden;
    event.currentTarget.setAttribute('aria-expanded', String(!cash.hidden));
  };
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
  field('side').value = 'buy';
  const today = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  field('date').value = options.edit?.date || options.date || today;
  field('date').min = options.edit ? account.min_date : account.trades?.[0]?.date || account.start_date;
  field('date').max = options.edit ? account.max_date : today;
  field('strategy').value = strategy;
  if (suggestion) {
    for (const key of ['side','code','qty']) if (suggestion[key] !== undefined) field(key).value = suggestion[key];
  }
  if (field('side').value==='cash_transfer') {
    field('strategy').value=strategy==='unassigned'?'core':strategy;
    field('from_strategy').value=Object.keys(STRATEGY_NAMES).find(key=>key!==field('strategy').value && account.cash_by_strategy[key]>0) || 'unassigned';
  }
  if (field('side').value==='assign') {
    field('from_strategy').value = options.fromStrategy || strategy || 'unassigned';
    field('strategy').value = options.fromStrategy ? strategy : strategy==='core' ? 'unassigned' : 'core';
  }
  const securities = new Map(account.positions.map(row=>[row.code,row.name]));
  if(options.edit) for(const key of ['side','date','code','qty','price','fee','strategy','from_strategy','amount']) if(options.edit[key]!==undefined) field(key).value=options.edit[key];
  for (const item of data.strategies) {
    for (const code of Object.keys(item.target_weights || {})) if (code!=='CASH' && !securities.has(code)) securities.set(code, code);
  }
  document.querySelector('#entry-securities').innerHTML = [...securities].map(([code,name])=>`<option value="${esc(code)}">${esc(name)}</option>`).join('');
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
    field('qty').min = side==='buy' ? '100' : '1';
    field('qty').step = side==='buy' ? '100' : '1';
    document.querySelector('[data-cash-field]').hidden = trade || assign;
    field('amount').disabled = trade || assign;
    field('amount').parentElement.firstChild.textContent = cashTransfer ? '划转金额 / 元' : side==='withdraw' ? '转出金额 / 元' : '到账金额 / 元';
    const owner = internal ? source.value : field('strategy').value;
    const held = account.strategy_positions.find(row=>row.code===field('code').value && row.strategy===owner)?.count || 0;
    const cash = account.cash_by_strategy[owner];
    const ownerName = STRATEGY_NAMES[owner];
    const knownName = securities.get(field('code').value);
    document.querySelector('#entry-security').textContent = knownName && knownName!==field('code').value ? knownName : field('code').value.length===6 ? '请核对证券代码' : '可输入代码，或从已有持仓中选择';
    for (const option of field('strategy').options) {
      if (option.value) option.textContent = internal ? STRATEGY_NAMES[option.value] : `${STRATEGY_NAMES[option.value]} · ${money(account.cash_by_strategy[option.value])} 元现金`;
    }
    const all = document.querySelector('#entry-all');
    all.hidden = !['sell','assign'].includes(side);
    all.textContent = assign ? '全部划转' : '全部卖出';
    all.disabled = held<=0;
    const context = document.querySelector('#entry-context');
    context.textContent = !ownerName ? '请选择本笔记账所属的策略。' : assign ? field('code').value ? `${ownerName}可划转 ${number(held)} 份` : '选择证券后查看可划转份额。' : `${ownerName}现金 ${money(cash)} 元${trade && field('code').value ? ` · 持有 ${number(held)} 份` : ''}`;
    const amount = trade ? Number(field('qty').value) * Number(field('price').value) : Number(field('amount').value);
    const fee = trade ? Number(field('fee').value) : 0;
    const delta = Math.round((['buy','withdraw','cash_transfer'].includes(side) ? -amount-fee : amount-fee)*100)/100;
    document.querySelector('#event-preview').hidden = assign || !(amount>0);
    document.querySelector('#event-preview').textContent = `${trade ? `成交金额 ${money(amount)} 元 · 手续费 ${money(fee)} 元。` : ''}现金${delta>=0?'增加':'减少'} ${money(Math.abs(delta))} 元；${ownerName || '所选策略'}记账后余额 ${finite(cash) ? money(cash+delta) : '—'} 元。`;
    if(cashTransfer && amount>0) document.querySelector('#event-preview').textContent=`${ownerName}划出 ${money(amount)} 元后剩余 ${money(cash-amount)} 元；${STRATEGY_NAMES[field('strategy').value]}收到后余额 ${money(account.cash_by_strategy[field('strategy').value]+amount)} 元。账户总现金不变。`;
    for (const name of ['qty','amount','price','strategy']) field(name).setCustomValidity('');
    let problem = '';
    if (['sell','assign'].includes(side) && Number(field('qty').value)>held) {
      problem = `份额超过${ownerName || '所选策略'}持仓（${number(held)} 份）。`;
      field('qty').setCustomValidity(problem);
    } else if (side==='buy' && field('qty').value && Number(field('qty').value)%100!==0) {
      problem = '买入份额须为100份的整数倍。'; field('qty').setCustomValidity(problem);
    } else if (internal && source.value===field('strategy').value) {
      problem = '请选择不同的划转来源和目标。'; field('strategy').setCustomValidity(problem);
    } else if (!assign && finite(cash) && Math.round((cash+delta)*100)<0) {
      problem = `${ownerName}现金不足${side==='buy' ? '，可先通过“划转现金”补充' : ''}。`;
      field(trade ? 'price' : 'amount').setCustomValidity(problem);
    }
    const validation = document.querySelector('#entry-validation');
    validation.hidden = !problem; validation.textContent = problem;
  }
  form.oninput = () => { requestId=crypto.randomUUID(); error.hidden=true; update(); };
  form.onchange = form.oninput;
  document.querySelectorAll('[data-entry-mode]').forEach(button=>button.onclick=()=>{
    const mode = button.dataset.entryMode;
    const side = mode==='trade' ? 'buy' : mode==='cash' ? 'deposit' : mode;
    // 先恢复选项，避免浏览器拒绝选中上一类别中被禁用的选项。
    for (const option of field('side').options) option.disabled=false;
    field('side').value = side;
    if (['assign','cash_transfer'].includes(mode) && field('from_strategy').value===field('strategy').value) field('strategy').value = field('from_strategy').value==='core' ? 'unassigned' : 'core';
    form.oninput();
  });
  document.querySelector('#entry-all').onclick = () => {
    const owner = field('side').value==='assign' ? field('from_strategy').value : field('strategy').value;
    field('qty').value = account.strategy_positions.find(row=>row.code===field('code').value && row.strategy===owner)?.count || '';
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
    if (side==='assign') {
      Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), strategy:field('strategy').value, from_strategy:field('from_strategy').value});
    } else if (['buy','sell'].includes(side)) {
      Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), price:Number(field('price').value), fee:Number(field('fee').value), strategy:field('strategy').value});
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
      document.querySelector('#status').textContent = '记账已保存';
      if (keepOpen) {
        const receipt = `${payload.date} · ${SIDES[side]}${payload.code ? ` ${payload.code} ${number(payload.qty)} 份` : ` ${money(payload.amount)} 元`}已保存。可继续记下一笔。`;
        openEntry(data.accounts[0], {side}, payload.strategy, {date:payload.date,fromStrategy:payload.from_strategy,success:receipt});
      } else dialog.close();
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

function openBudgets() {
  return openConfiguration();
}

function allocationRows(account) {
  const rows = data.strategies.map(strategy => ({...strategy, target:strategy.budget_pct}));
  const holdings = account.strategy_positions.filter(row=>row.strategy==='unassigned').reduce((sum,row)=>sum+row.market_value,0);
  const cash = account.cash_by_strategy.unassigned;
  const assets = holdings+cash;
  const configured = rows.some(row=>finite(row.target));
  rows.push({id:'unassigned',name:'未分配',invested:holdings,cash,assets,
    actual_pct:assets/account.equity,target:configured ? Math.max(0,1-rows.reduce((sum,row)=>sum+(row.target||0),0)) : null});
  return rows;
}

function assetSlices(account) {
  return [
    {name:'多资产配置持仓',value:data.strategies[0].invested,color:'#22735b'},
    {name:'跨资产动量持仓',value:data.strategies[1].invested,color:'#7393a2'},
    {name:'质量动量持仓',value:data.strategies.find(row=>row.id==='quality')?.invested || 0,color:'#997fa4'},
    {name:'未分配持仓',value:account.strategy_positions.filter(row=>row.strategy==='unassigned').reduce((sum,row)=>sum+row.market_value,0),color:'#b1884c'},
    {name:'现金',value:account.cash,color:'#c2d3c5'}
  ];
}

function allocationCharts(account) {
  if(!finite(account.equity)) return '<p class="warning">部分持仓待估值，资产占比与目标金额暂不计算。可刷新行情。</p>';
  const slices = assetSlices(account), circumference=2*Math.PI*78;
  let offset=0;
  const rings = slices.map((slice,index)=>{
    const length = Math.max(0,slice.value/account.equity*circumference);
    const arc = `<circle class="donut-segment" data-asset-index="${index}" cx="105" cy="105" r="78" fill="none" stroke="${slice.color}" stroke-width="24" stroke-dasharray="${Math.max(0,length-2)} ${circumference-Math.max(0,length-2)}" stroke-dashoffset="${-offset}" transform="rotate(-90 105 105)"><title>${slice.name} ${ratio(slice.value,account.equity)}</title></circle>`;
    offset+=length; return length>0 ? arc : '';
  }).join('');
  const bars = allocationRows(account).map((row,index)=>{
    const actual = Math.max(0,Math.min(100,row.actual_pct*100));
    const target = finite(row.target) ? row.target*100 : null;
    const delta = finite(row.target) && finite(row.actual_pct) ? row.actual_pct-row.target : null;
    const deviation = delta===null ? '—' : isAllocationBalanced(delta) ? '已配平' : `${delta>0?'超配':'低配'} ${(Math.abs(delta)*100).toFixed(2)} 个百分点`;
    return `<div class="allocation-row"><div class="allocation-row-top"><strong>${esc(row.name)}</strong><b>${price(row.assets)} <small>元</small></b></div><div class="allocation-row-labels"><span>实际 ${actual.toFixed(1)}%</span><span>${target===null?'目标未设置':`目标 ${target.toFixed(1)}%`}</span></div><div class="allocation-track" role="img" aria-label="${esc(row.name)}实际${actual.toFixed(1)}%，${target===null?'目标未设置':`目标${target.toFixed(1)}%`}"><div class="allocation-fill allocation-${row.id}" style="width:${actual}%"></div>${target===null?'':`<i class="allocation-target" style="left:${target}%"></i>`}</div><div class="allocation-row-foot"><span>持仓 ${number(row.invested)} · 现金 ${number(row.cash)}</span><span>${deviation}</span></div></div>`;
  }).join('');
  return `<div class="allocation-grid"><section class="section asset-panel"><div class="section-heading"><h2>资产构成</h2></div><div class="asset-layout"><svg class="asset-donut" viewBox="0 0 210 210" role="img" aria-label="账户持仓与现金占比"><circle cx="105" cy="105" r="78" fill="none" stroke="#edf1eb" stroke-width="24"/>${rings}<text id="donut-label" x="105" y="91" text-anchor="middle" class="donut-label">总资产</text><text id="donut-value" x="105" y="117" text-anchor="middle" class="donut-value">${number(account.equity)}</text><text id="donut-share" x="105" y="137" text-anchor="middle" class="donut-label">元</text></svg><div class="asset-legend">${slices.map((slice,index)=>`<button type="button" class="asset-legend-item" data-asset-index="${index}"><i style="background:${slice.color}"></i><span>${slice.name}<small>${price(slice.value)} 元</small></span><b>${ratio(slice.value,account.equity)}</b></button>`).join('')}</div></div></section><section class="section strategy-panel"><div class="section-heading"><h2>策略配比</h2><div class="allocation-actions"><button type="button" id="budget-settings" class="text-button">编辑配置</button><button type="button" id="cash-settings" class="text-button">分配现金</button></div></div><div class="allocation-legend"><i></i> 实际 <i class="target-key"></i> 目标 <span>含各策略现金 · ${allocationToleranceNote}</span></div>${bars}</section></div>`;
}

function bindOverviewCharts(account) {
  const label=document.querySelector('#donut-label');
  if (!label) return;
  const slices=assetSlices(account);
  function focus(index) {
    const slice = index===null ? null : slices[index];
    label.textContent = slice ? slice.name : '总资产';
    document.querySelector('#donut-value').textContent=number(slice ? slice.value : account.equity);
    document.querySelector('#donut-share').textContent=slice ? ratio(slice.value,account.equity) : '元';
    document.querySelectorAll('.donut-segment').forEach(segment=>{segment.style.opacity=slice && Number(segment.dataset.assetIndex)!==index ? '.28' : '1';});
  }
  document.querySelectorAll('[data-asset-index]').forEach(element=>{
    element.onpointerenter=()=>focus(Number(element.dataset.assetIndex));
    element.onfocus=()=>focus(Number(element.dataset.assetIndex));
    element.onpointerleave=()=>focus(null);element.onblur=()=>focus(null);
  });
}

function allocationProductDetails(products, strategyId) {
  const items = products.map(row => {
    if (row.kind === 'fund') {
      const mark = row.qty
        ? `${row.qty.toLocaleString('zh-CN',{maximumFractionDigits:6})} 份 · ${money(row.market_value)} 元<br>净值 ${row.nav.toLocaleString('zh-CN',{maximumFractionDigits:8})} · ${esc(row.date)}`
        : '未录入份额与净值';
      return `<div>场外基金 ${esc(row.code)}<small>${mark}</small><button type="button" class="text-button" data-edit-fund="${esc(row.code)}">${row.qty ? '更新' : '录入'}</button></div>`;
    }
    const value = finite(row.market_value) ? money(row.market_value) + ' 元' : '待估值';
    return `<div>${esc(row.name)}<small>${esc(row.code)} · ${number(row.count)} 份 · ${value}</small>${positionActions(row.code,strategyId,row.count>0)}</div>`;
  }).join('');
  return `<details class="allocation-products"><summary>${products.length} 个产品</summary>${items}</details>`;
}

function strategyPositionTable(strategy) {
  if (!strategy.target_weights) return rotationPositionTable(strategy);
  const wallet = data.accounts[0];
  const budget = strategy.allocation_budget;
  const assets = strategy.allocation_assets;
  const pct=value=>finite(value)?`${(value*100).toFixed(2)}%`:'—';
  const thresholds = strategy.rebalance_thresholds;
  const suggestions = strategy.suggestions || [];
  const thresholdNote = thresholds ? `偏离达到 ${(thresholds.trade*100).toFixed(0)} 个百分点提示，超过 ${(thresholds.single*100).toFixed(0)} 个百分点重点提醒。` : '偏离提醒阈值暂不可用。';
  let basisNote = '请先在“编辑配置”中设置策略目标占比。';
  if (budget > 0) {
    basisNote = '目标金额 = 配置目标资产 × 配置项权重。配比偏离按配置目标资产计算。';
  } else if (finite(strategy.budget_pct)) {
    basisNote = '账户估值待补齐后计算目标金额。';
  }
  const priceIssue = suggestions.find(item=>item.price_issue)?.price_issue;
  const rows=strategy.allocation_groups.map(group=>{
    const {id:code, name, market_value:actual, weight, products} = group;
    const showProducts = products.length>1 || products.some(row => row.kind==='fund');
    const position = showProducts ? null : products[0];
    const target=finite(budget)?budget*weight:null;
    const gap = budget>0 && finite(target) && finite(actual) ? (Math.round(actual*100)-Math.round(target*100))/100 : null;
    const deviation = finite(gap) ? Math.abs(gap)/budget : null;
    let level = '';
    if (finite(deviation) && thresholds) {
      if (deviation > thresholds.single) level = 'strong';
      else if (deviation >= thresholds.trade) level = 'attention';
    }
    const index = suggestions.findIndex(item=>item.code===code);
    const item = wallet.quote_refresh?.estimated ? null : suggestions[index];
    let difference;
    if (!finite(gap)) {
      const status = finite(strategy.budget_pct) ? '待估值' : '待设置目标';
      difference = `<span class="subtle">${status}</span>`;
    } else if (isAllocationBalanced(deviation)) {
      difference = '<span class="subtle">已配平</span>';
    } else {
      difference = `<strong class="allocation-gap">${gap>0 ? '超配' : '低配'} ${money(Math.abs(gap))} 元</strong><small>${(deviation*100).toFixed(2)} 个百分点${level==='strong' ? ' · 偏离较大' : ''}</small>`;
    }
    const reference = item ? `<small>${item.price_issue ? esc(item.price_issue) : `参考${item.side==='sell' ? '减持' : '增持'}约 ${number(item.qty)} 份`}</small>${item.cash_note ? `<small class="cash-note">${esc(item.cash_note)}</small>` : ''}` : '';
    let actions, details = '';
    if (code === 'CASH') {
      actions = '<button type="button" class="text-button" data-open-cash>分配现金</button>';
    } else if (showProducts) {
      actions = '<small>展开产品明细</small>';
      details = allocationProductDetails(products, strategy.id);
    } else {
      actions = positionActions(code, strategy.id, position.count > 0, item ? {...item,index} : null);
      details = `<small>${esc(position.code)}</small>`;
    }
    return `<tr data-allocation-code="${esc(code)}"${level ? ` class="allocation-${level}"` : ''}><td>${esc(name)}${group.outside ? '<small>目标外持仓</small>' : ''}${details}</td><td>${position?number(position.count):'—'}</td><td>${position?.count ? quotePrice(position) : price(null)}</td><td>${money(actual)}<small>配置内 ${pct(assets>0 && finite(actual)?actual/assets:null)}</small></td><td>${money(target)}<small>目标权重 ${pct(weight)}</small></td><td>${difference}<span class="allocation-mobile-values">现 ${money(actual)}<br>目标 ${money(target)}</span>${reference}</td><td>${actions}</td></tr>`;
  }).join('');
  const external = strategy.external_fund_value>0 ? `<p class="subtle">配置目标资产 ${money(budget)} 元，由券商目标资产加场外基金市值 ${money(strategy.external_fund_value)} 元组成。场外按各产品录入的净值日期估值。</p>` : '';
  return `${external}${priceIssue && !wallet.quote_refresh?.estimated ? `<p class="warning">参考份额暂缺：${esc(priceIssue)}。</p>` : ''}<div class="table-wrap"><table class="allocation-table core-allocation-table"><thead><tr><th>配置项</th><th>份额</th><th>价格 / 元</th><th>当前金额 / 元</th><th>目标金额 / 元</th><th>配比偏离</th><th>记账</th></tr></thead><tbody>${rows}</tbody></table></div><details class="calculation-note"><summary>配比说明</summary><p>${basisNote}${allocationToleranceNote}。${thresholdNote} 多产品配置按合计市值判断偏离，组内产品由你选择。</p></details>`;
}

function openFundPosition(account, position) {
  const dialog=document.querySelector('#fund-dialog');
  const form=document.querySelector('#fund-form');
  const fields=document.querySelector('#fund-fields');
  const save=document.querySelector('#fund-save');
  const error=document.querySelector('#fund-error');
  const field=name=>form.elements.namedItem(name);
  let saving = false;
  let saved = false;
  form.reset();
  fields.disabled = false;
  save.disabled = false;
  save.textContent = '保存持仓';
  error.hidden = true;
  field('code').value=position?.code || '';
  field('code').readOnly=!!position;
  field('qty').value=position?.qty ?? '';
  field('nav').value=position?.nav ?? '';
  field('date').value=position?.date ?? '';
  field('date').max=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  function update() {
    const removing=field('qty').value!=='' && Number(field('qty').value)===0;
    field('nav').disabled = removing;
    field('date').disabled = removing;
  }
  form.oninput=update;
  update();
  document.querySelector('#fund-cancel').onclick=()=>{if(!saving)dialog.close();};
  dialog.oncancel=event=>{if(saving)event.preventDefault();};
  form.onsubmit=async event=>{
    event.preventDefault();
    if (saving) return;
    saving = true;
    save.disabled = true;
    fields.disabled = true;
    error.hidden = true;
    try {
      if (!saved) {
        const payload = {
          revision: account.fund_revision,
          code: field('code').value,
          qty: Number(field('qty').value),
          nav: Number(field('nav').value),
          date: field('date').value,
        };
        await accountRequest('/api/etf/fund', payload);
        saved = true;
      }
      await reloadAccount();
      dialog.close();
      document.querySelector('#status').textContent = '场外持仓已保存';
    } catch (failure) {
      error.textContent = saved ? '场外持仓已保存，页面更新失败。可点击刷新账户。' : failure.message;
      error.hidden = false;
    } finally {
      saving = false;
      fields.disabled = saved;
      save.disabled = false;
      save.textContent = saved ? '刷新账户' : '保存持仓';
    }
  };
  dialog.showModal();
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
  for (const id of ['core','rotation','quality']) form.elements.namedItem(id).value=((options.edit?.cash_allocations || account.cash_by_strategy)[id] || 0).toFixed(2);
  function update() {
    requestId=crypto.randomUUID();
    preview.hidden=true;
    const remaining=Math.round((account.cash-Number(form.elements.namedItem('core').value)-Number(form.elements.namedItem('rotation').value)-Number(form.elements.namedItem('quality').value))*100)/100;
    const message=remaining<0 ? `超出账户现金 ${money(-remaining)} 元` : `未分配现金 ${money(remaining)} 元`;
    document.querySelector('#cash-remaining').textContent=message;
    document.querySelector('#cash-remaining').classList.toggle('invalid',remaining<0);
    form.elements.namedItem('rotation').setCustomValidity(remaining<0 ? message : '');
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
      const basis=plan.valuation.mode==='live'?`本轮完整行情，获取于 ${quoteTime(plan.valuation.fetched_at)}`:`${plan.valuation.date} ${plan.valuation.mode==='initial'?'期初估值':'收盘价'}`;
      preview.innerHTML=`<p class="entry-preview">${note}${overweight.length?` ${esc(overweight.join('、'))}仅持仓已超配，需调整持仓才能完全对齐。`:''}</p><p class="subtle">计算依据：${esc(basis)}；未设置目标的策略保留原现金。</p><div class="table-wrap"><table class="cash-plan-table"><thead><tr><th>策略</th><th>现金调整 / 元</th><th>调整后现金 / 元</th><th>调整后偏离 / 元</th></tr></thead><tbody>${rows}</tbody></table></div><p class="subtle">仅调整策略现金，持仓与总现金不变；检查后确认保存。</p>`;
      preview.hidden=false;
    } catch(failure) {error.textContent=failure.message;error.hidden=false;}
    finally {planning=false;rebalanceButton.disabled=!finite(account.equity);button.disabled=false;}
  };
  form.onsubmit=async event=>{
    event.preventDefault();if(saving || planning)return;
    if(!form.reportValidity())return;
    saving=true;button.disabled=true;error.hidden=true;fields.disabled=true;
    const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    const payload={request_id:requestId,revision:account.revision,date:day,side:'cash_assign',cash_allocations:{core:Number(form.elements.namedItem('core').value),rotation:Number(form.elements.namedItem('rotation').value),quality:Number(form.elements.namedItem('quality').value)}};
    if(options.edit) {payload.date=dateField.value;payload.event_id=options.edit.id;}
    try {
      if(!saved) {await accountRequest(options.edit?'/api/etf/event/edit':'/api/etf/event',payload);saved=true;}
      await reloadAccount();dialog.close();
      document.querySelector('#status').textContent='现金分配已保存';
    } catch (failure) {
      error.textContent=saved ? '现金分配已保存，页面更新失败。请刷新账户查看。' : failure.message;
      error.hidden=false;
    } finally {saving=false;fields.disabled=saved;button.disabled=false;button.textContent=saved?'刷新账户':'确认分配';}
  };
  update();dialog.showModal();
  if(options.rebalance) {
    if(finite(account.equity)) rebalanceButton.click();
    else {error.textContent='部分持仓待估值，请先刷新完整行情再进行现金再平衡。';error.hidden=false;}
  }
}

async function openConfiguration() {
  configRequest?.abort();
  const controller=new AbortController();configRequest=controller;
  const dialog=document.querySelector('#config-dialog');
  const form=document.querySelector('#config-form');
  const error=document.querySelector('#config-error');
  const save=document.querySelector('#config-save');
  const fields=document.querySelector('#config-fields');
  let saving=false, saved=false;
  form.reset();error.hidden=true;save.disabled=true;save.textContent='保存配置';fields.disabled=true;
  document.querySelector('#config-history').innerHTML='';
  document.querySelector('#config-assets').innerHTML='';
  document.querySelector('#config-total').textContent='正在读取配置…';
  document.querySelector('#config-cancel').onclick=()=>{if(!saving){controller.abort();dialog.close();}};
  dialog.oncancel=event=>{if(saving)event.preventDefault();else controller.abort();};
  dialog.showModal();
  let result;
  try {
    const response=await fetch('/api/etf/config',{cache:'no-store',signal:controller.signal});
    result=await response.json();
    if(controller.signal.aborted || !dialog.open)return;
    if(!response.ok) throw Error(result.error || '配置读取失败');
  } catch(failure) {if(controller.signal.aborted)return;error.textContent=failure instanceof TypeError ? '配置暂时无法加载，请关闭后重试。' : failure.message;error.hidden=false;return;}
  const percentInput=value=>+(value*100).toFixed(8);
  const memberCodes=value=>value.trim().split(/[\s,，、]+/).filter(Boolean);
  let draft;
  function total() {
    const weights=[...document.querySelectorAll('#config-assets [data-field=weight]')];
    const sum=weights.reduce((n,input)=>n+Number(input.value),0);
    const budgets=['core','rotation','quality'].reduce((sum,key)=>sum+Number(form.elements.namedItem(key).value),0);
    const weightIssue=Math.abs(sum-100)>1e-6;
    const budgetIssue=budgets>100+1e-8;
    weights.forEach(input=>input.setCustomValidity(''));
    weights.at(-1)?.setCustomValidity(weightIssue ? '多资产配置内部权重须合计100%。' : '');
    form.elements.namedItem('core').setCustomValidity(budgetIssue ? '策略目标占比合计不能超过100%。' : '');
    document.querySelector('#config-total').textContent=`多资产配置权重 ${sum.toFixed(4).replace(/\.?0+$/,'')}% · 账户目标 ${budgets.toFixed(2)}%${weightIssue?' · 多资产配置权重须合计100%':''}${budgetIssue?' · 账户目标合计不能超过100%':''}`;
    document.querySelector('#config-total').classList.toggle('invalid',weightIssue||budgetIssue);
  }
  function addRow(asset) {
    const row=document.createElement('tr');
    row.original=structuredClone(asset);
    const options=(values,current)=>values.map(([value,label])=>`<option value="${value}" ${value===current?'selected':''}>${label}</option>`).join('');
    const cash = asset.code==='CASH';
    const code = asset.code.startsWith('FUND:') ? '' : asset.code;
    row.innerHTML = `
      <td>
        <label ${cash?'hidden':''}>配置名称<input data-field="label" value="${esc(asset.label || '')}" maxlength="40" placeholder="如创业板"></label>
        <label ${cash?'hidden':''}>主要场内代码</label>
        <input data-field="code" value="${esc(code)}" type="${cash?'hidden':'text'}" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" aria-label="主要场内代码">
        <small data-name>${esc(result.names[asset.code] || (asset.code==='CASH'?'现金':asset.code))}</small>
      </td>
      <td><input data-field="weight" type="number" value="${percentInput(asset.weight)}" min="0" max="100" step="any" required aria-label="内部权重"></td>
      <td>
        <details ${cash?'hidden':''}>
          <summary>绑定产品与设置</summary>
          <div class="config-advanced">
            <label>其他场内代码<input data-field="etf_codes" value="${esc((asset.etf_codes || []).join(', '))}" placeholder="多个用逗号隔开"></label>
            <label>场外基金代码<input data-field="fund_codes" value="${esc((asset.fund_codes || []).join(', '))}" placeholder="多个用逗号隔开"></label>
            <label>资产类别<select data-field="asset_type">${options([['equity','权益'],['bond','债券'],['gold','黄金'],['cash','现金']],asset.asset_type)}</select></label>
            <label>估值来源<select data-field="valuation_kind">${options([['none','不展示估值'],['csindex','中证指数'],['overseas','海外']],asset.valuation_kind)}</select></label>
            <label>跟踪指数<input data-field="tracked_index" value="${esc(asset.tracked_index || '')}" pattern="[0-9]{6}" maxlength="6"></label>
            <label>减仓顺序<input data-field="volatility_rank" type="number" min="0" max="100" step="1" value="${asset.volatility_rank}" required></label>
            <label>股息估值<select data-field="dividend_style"><option value="false" ${!asset.dividend_style?'selected':''}>否</option><option value="true" ${asset.dividend_style?'selected':''}>是</option></select></label>
          </div>
        </details>
      </td>
      <td><button type="button" class="text-button" data-remove ${cash?'hidden':''}>删除</button></td>
    `;
    const field=name=>row.querySelector(`[data-field=${name}]`);
    function update() {
      const code=field('code');
      const fundOnly=!code.value && memberCodes(field('fund_codes').value).length>0;
      code.setCustomValidity(!code.value && (!fundOnly || field('etf_codes').value.trim()) ? '请输入主要场内代码，或在绑定产品中填写场外基金代码。' : '');
      const index=field('tracked_index'),indexed=!fundOnly && field('valuation_kind').value==='csindex';
      index.disabled=!indexed;index.required=indexed;
      field('valuation_kind').disabled=field('dividend_style').disabled=fundOnly;
      row.querySelector('[data-name]').textContent=cash?'现金':result.names[code.value] || (fundOnly?'仅场外基金':'');
      total();
    }
    row.oninput=update;row.onchange=update;
    row.querySelector('[data-remove]').onclick=()=>{row.remove();total();};
    document.querySelector('#config-assets').append(row);update();
  }
  function show(config) {
    draft=structuredClone(config);
    for(const key of ['core','rotation','quality']) form.elements.namedItem(key).value=finite(config.budgets[key])?percentInput(config.budgets[key]):'';
    document.querySelector('#config-assets').innerHTML='';
    config.core.forEach(addRow);total();
  }
  document.querySelector('#config-add').onclick=()=>addRow({code:'',weight:0,asset_type:'equity',valuation_kind:'none',tracked_index:null,dividend_style:false,volatility_rank:0});
  const history=document.querySelector('#config-history');
  history.innerHTML=result.history.map((version,index)=>`<option value="${index}">${esc(quoteTime(version.saved_at))}${version.revision===result.revision?' · 当前配置':''}</option>`).join('');
  history.value=Math.max(0,result.history.findIndex(version=>version.revision===result.revision));
  document.querySelector('#config-load-history').onclick=()=>{const version=result.history[Number(history.value)];if(version)show(version.config);};
  form.oninput=total;
  form.onsubmit=async event=>{
    event.preventDefault();if(saving)return;
    saving=true;save.disabled=true;error.hidden=true;fields.disabled=true;
    const budgets={};
    for(const key of ['core','rotation','quality']) {
      const value=form.elements.namedItem(key).value;
      if(value!=='') budgets[key]=Number(value)===percentInput(draft.budgets[key])?draft.budgets[key]:Number(value)/100;
    }
    const core=[...document.querySelectorAll('#config-assets tr')].map(row=>{
      const get=key=>row.querySelector(`[data-field=${key}]`).value;
      const primary = get('code');
      const label = get('label').trim();
      const etfs = memberCodes(get('etf_codes'));
      const funds = memberCodes(get('fund_codes'));
      const valuation = primary ? get('valuation_kind') : 'none';
      const weight = Number(get('weight'));
      const asset = {
        code: primary || `FUND:${funds[0]}`,
        weight: weight === percentInput(row.original.weight) ? row.original.weight : weight / 100,
        asset_type: get('asset_type'),
        valuation_kind: valuation,
        tracked_index: valuation === 'csindex' ? get('tracked_index') : null,
        dividend_style: !!primary && get('dividend_style') === 'true',
        volatility_rank: Number(get('volatility_rank')),
      };
      if (label) asset.label = label;
      if (etfs.length) asset.etf_codes = etfs;
      if (funds.length) asset.fund_codes = funds;
      return asset;
    });
    try {
      if(!saved) {await accountRequest('/api/etf/config',{revision:result.revision,config:{schema_version:1,budgets,core}});saved=true;}
      await reloadAccount();dialog.close();
      document.querySelector('#status').textContent='配置已保存';
    } catch(failure) {error.textContent=saved?'配置已保存，页面更新失败。请刷新账户查看。':failure.message;error.hidden=false;}
    finally {saving=false;fields.disabled=saved;save.disabled=false;save.textContent=saved?'刷新账户':'保存配置';}
  };
  show(result.config);save.disabled=false;fields.disabled=false;
}
