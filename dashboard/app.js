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
const SIDES = {buy:'买入', sell:'卖出', dividend:'分红', deposit:'入金', withdraw:'出金', assign:'持仓分配', cash_assign:'现金分配'};
const STRATEGY_NAMES = {core:'核心配置', rotation:'跨资产轮动', unassigned:'未分配'};
const REPORT_PREFIX = {core:'etf_weekly_', satellite:'industry_momentum_', rotation:'etf_rotation_', quality:'quality_pool_'};
let data, selected = 0, chartRange = 'all', chartObserver;
let selectedReport, reportRequest;
const reportCache = new Map();

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
  return messages.map(message => `<div class="warning">${esc(message)}</div>`).join('');
}

function chartSection(account, note = '') {
  return `<section class="section chart-section" aria-labelledby="chart-title">
    <div class="section-heading"><div><div class="eyebrow">PERFORMANCE</div><h2 id="chart-title">${esc(accountName(account))} · 收益走势</h2></div>
      <div class="range-buttons" role="group" aria-label="收益曲线时间范围">${[['30','近30天'],['90','近90天'],['all','全部']].map(([value,label]) => `<button type="button" data-range="${value}" class="${chartRange === value ? 'active' : ''}" aria-pressed="${chartRange === value}">${label}</button>`).join('')}</div></div>
    <div class="chart-summary" aria-live="polite">
      <div class="chart-metric primary"><small><i class="legend-line"></i>账户累计收益</small><b id="chart-nav">—</b></div>
      <div class="chart-metric benchmark" ${!account?.benchmark_name ? 'hidden' : ''}><small><i class="legend-line benchmark"></i>同期基准</small><b id="chart-base">—</b></div>
      <div class="chart-metric" ${!account?.benchmark_name ? 'hidden' : ''}><small>超额 / 百分点</small><b id="chart-excess">—</b></div>
    </div><div id="chart-date" class="chart-date"></div>
    <div id="chart-stage" class="chart-stage"></div>
    <input id="chart-cursor" class="chart-slider" type="range" aria-label="选择日期查看收益" hidden>
    <div class="chart-foot"><span>${note ? `${esc(note)} · ` : ''}${account?.benchmark_name ? `基准：${esc(account.benchmark_name)}` : `建账起点：${esc(account?.start_date || '—')}`}</span><span>拖动查看每日收益</span></div>
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
  if (!points.length || !values.length) {
    stage.innerHTML = '<div class="empty">暂无收益记录</div>';
    slider.hidden = true;
    document.querySelector('#chart-date').textContent = '暂无收益记录';
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

function accountDetails(account) {
  const stats = [[money(account.equity),'总资产 / 元'],[money(account.invested),'持仓市值 / 元'],[money(account.cash),'现金 / 元'],[`${account.pnl>0?'+':''}${money(account.pnl)}`,'累计盈亏 / 元']];
  const rows = account.positions.map(row => `<tr><td>${esc(row.name)}<small>${esc(row.code)}</small></td><td>${number(row.count)}</td><td>${price(row.close)}</td><td>${money(row.market_value)}</td><td>${ratio(row.market_value,account.equity)}</td></tr>`).join('');
  return `<div class="stats">${stats.map(([value,label]) => `<div class="stat"><span>${label}</span><b>${value}</b></div>`).join('')}</div>
    ${allocationCharts(account)}${chartSection(account)}
    <section class="section"><div class="section-heading"><h2>账户持仓</h2><button type="button" id="manual-entry" class="text-button">＋ 记账</button></div><p class="subtle">价格日期 ${esc(account.valuation_date)}</p><div class="table-wrap"><table><thead><tr><th>标的</th><th>份额</th><th>价格 / 元</th><th>市值 / 元</th><th>资产占比</th></tr></thead><tbody>${rows}</tbody></table></div></section>
    <section class="section"><div class="section-heading"><h2>近期流水</h2><span class="subtle">最近 20 笔</span></div>${recordTable(account.trades)}</section>`;
}

function recordTable(rows) {
  if (!rows.length) return '<div class="empty">暂无流水</div>';
  const body = rows.map(row => {
    let detail = row.name || STRATEGY_NAMES[row.strategy] || '账户资金';
    if (row.side === 'assign') detail = `${row.code} · ${STRATEGY_NAMES[row.from_strategy]} → ${STRATEGY_NAMES[row.strategy]}`;
    if (row.side === 'cash_assign') {
      detail = Object.entries(row.cash_allocations).map(([key,amount]) => `${STRATEGY_NAMES[key]} ${price(amount)} 元`).join(' · ');
    }
    return `<tr><td>${esc(row.date)}</td><td>${esc(SIDES[row.side])}<small>${esc(detail)}</small></td><td>${number(row.qty)}</td><td>${price(row.price ?? row.amount)}</td><td>${price(row.fee)}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>类型 / 明细</th><th>份额</th><th>价格或金额 / 元</th><th>手续费 / 元</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function suggestionCards(rows) {
  const cards = rows.map((item,index) => `<article class="suggestion-card"><div class="suggestion-top"><span class="side-label ${item.side}">${item.side==='sell'?'减持':'增持'}</span><strong>${esc(item.name)}</strong><small>${esc(item.code)}</small></div><div class="suggestion-quantity">约 ${number(item.gap_amount)} <span>元</span></div><p>当前 ${money(item.current_value)} 元 · 目标 ${money(item.target_value)} 元</p><p class="suggestion-reference">${item.price_issue ? esc(item.price_issue) : `约 ${number(item.qty)} 份 · 约 ${money(item.amount)} 元`}</p><p class="subtle">${esc(item.price_date)} · ${price(item.price)} 元</p>${item.cash_note ? `<p class="cash-note">${esc(item.cash_note)}</p>` : ''}<div class="suggestion-actions"><button type="button" class="primary-button" data-fill="${index}">记录成交</button><button type="button" class="text-button" data-dismiss="${index}">本轮忽略</button></div></article>`).join('');
  return cards || '<div class="empty">暂无建议</div>';
}

function fundedStrategyPage(strategy) {
  const stats = [[finite(strategy.budget) ? money(strategy.budget) : '待设置','目标资产 / 元'],[money(strategy.invested),'持仓市值 / 元'],[`${strategy.pnl>0?'+':''}${money(strategy.pnl)}`,'累计盈亏 / 元'],[money(strategy.cash),'策略现金 / 元']];
  const observation = strategy.observation;
  const observationSection = observation ? `${chartSection(observation, '独立模拟，不计入ETF账户')}<section class="section"><div class="section-heading"><h2>模拟持仓</h2></div>${positionTable(observation.positions)}</section>` : '';
  return `<div class="stats">${stats.map(([value,label])=>`<div class="stat"><span>${label}</span><b>${esc(value)}</b></div>`).join('')}</div><section class="section"><div class="section-heading"><h2>${esc(strategy.name)} · 持仓</h2><span class="subtle">${esc(strategy.status)}</span><button type="button" id="manual-entry" class="text-button">＋ 记账</button></div>${strategyPositionTable(strategy)}</section><section class="section"><div class="section-heading"><h2>待执行建议</h2></div>${strategy.id === 'rotation' && !strategy.suggestions?.length ? `<div class="empty">${esc(strategy.advice_status || '等待交易日更新')}</div>` : suggestionCards(strategy.suggestions || [])}</section>${observationSection}`;
}

function allocatedPositionTable(rows) {
  if (!rows.length) return '<div class="empty">暂无持仓</div>';
  return `<div class="table-wrap"><table><thead><tr><th>标的</th><th>份额</th><th>价格 / 元</th><th>市值 / 元</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.name)}<small>${esc(row.code)}</small></td><td>${number(row.count)}</td><td>${price(row.close)}</td><td>${price(row.market_value)}</td></tr>`).join('')}</tbody></table></div>`;
}

function strategyPage(account) {
  if (account.account_id === 'etf') return fundedStrategyPage(account);
  const p = account.performance;
  const priced = account.positions.filter(row => finite(row.close)).length;
  const closeNote = account.positions.length && priced===account.positions.length ? `收盘价截至 ${esc(p?.date)}` : '价格缺失时按成本占比展示';
  const stats = [[`${number(account.positions.length)} 只`,'持仓'],[ratio(p?.invested,p?.equity),'仓位'],[ratio(p?.cash,p?.equity),'现金'],[ratio(p?.receivables,p?.equity),'应收红利']];
  return `${warnings(account)}${chartSection(account)}<div class="stats">${stats.map(([v,label]) => `<div class="stat"><span>${label}</span><b>${v}</b></div>`).join('')}</div><section class="section"><div class="section-heading"><h2>账户持仓</h2><span class="subtle">${closeNote}</span></div>${positionTable(account.positions)}</section><section class="section"><div class="section-heading"><h2>近期流水</h2><span class="subtle">最近 20 笔</span></div>${tradeTable(account.trades)}</section>`;
}

function render() {
  chartObserver?.disconnect();
  const tabs = [{id:'etf',name:'账户总览'},...data.strategies,...data.observations];
  document.querySelector('#tabs').innerHTML = tabs.map((tab,index) => `${index===0 ? '<span class="nav-label">ETF账户</span>' : index===3 ? '<span class="nav-label">策略观察</span>' : ''}<button type="button" class="${index===selected ? 'active' : ''}" aria-pressed="${index===selected}" data-index="${index}">${esc(accountName(tab))}</button>`).join('');
  document.querySelectorAll('#tabs button').forEach(button => button.onclick=() => { selected=Number(button.dataset.index); render(); });
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
  document.querySelector('#status').textContent=`读取于 ${new Date(data.generated_at).toLocaleString('zh-CN')}`;
  document.querySelector('#app').hidden=false;
  document.querySelector('#states').innerHTML=data.states.map(state => `<p><strong>${esc(state.name)}</strong> · ${esc(state.detail)}</p>`).join('');
  render();
}).catch(() => { document.querySelector('#status').textContent='数据读取失败，请刷新页面重试。'; });

async function accountRequest(path, payload) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Trade-Request':'dashboard'}, body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '保存失败，请刷新核对后重试。');
}

async function reloadAccount() {
  const response = await fetch('/api/overview', {cache:'no-store'});
  if (!response.ok) throw Error('记录已提交，页面刷新失败，请刷新核对。');
  data = await response.json();
  render();
}

function bindAccountActions(view) {
  const account = data.accounts[0];
  if (!account?.can_record || (view.id!=='etf' && view.account_id!=='etf')) return;
  const strategy = view.account_id==='etf' ? view.id : 'unassigned';
  document.querySelector('#manual-entry').onclick = () => openEntry(account, null, strategy);
  const budgetButton = document.querySelector('#budget-settings');
  if (budgetButton) budgetButton.onclick = () => openBudgets(account);
  const cashButton = document.querySelector('#cash-settings');
  if (cashButton) cashButton.onclick = () => openCashAllocation(account);
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

function openEntry(account, suggestion, strategy='unassigned') {
  const dialog = document.querySelector('#event-dialog');
  const form = document.querySelector('#event-form');
  const error = document.querySelector('#event-error');
  const save = document.querySelector('#event-save');
  form.reset(); error.hidden = true; save.disabled = false;
  let requestId = crypto.randomUUID();
  const field = name => form.elements.namedItem(name);
  field('date').value = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  field('date').min = account.start_date;
  field('date').max = field('date').value;
  field('strategy').value = strategy;
  if (suggestion) {
    for (const key of ['side','code','qty']) field(key).value = suggestion[key];
  }
  // 建议价格只用于估算，实际成交均价必须由用户填写。
  function update() {
    const trade = ['buy','sell'].includes(field('side').value);
    const assign = field('side').value === 'assign';
    document.querySelectorAll('[data-trade-field]').forEach(label => {
      const input = label.querySelector('input,select');
      const shown = trade || (assign && !['price','fee'].includes(input.name));
      label.hidden = !shown; input.disabled = !shown;
    });
    field('strategy').parentElement.hidden = false;
    field('strategy').disabled = false;
    const source = field('from_strategy');
    source.parentElement.hidden = !assign; source.disabled = !assign;
    field('strategy').parentElement.firstChild.textContent = assign ? '分配到' : '策略';
    field('qty').parentElement.firstChild.textContent = assign ? '分配份额' : '成交份额';
    document.querySelector('[data-cash-field]').hidden = trade || assign;
    field('amount').disabled = trade || assign;
    const amount = trade ? Number(field('qty').value) * Number(field('price').value) : Number(field('amount').value);
    const fee = trade ? Number(field('fee').value) : 0;
    const delta = ['buy','withdraw'].includes(field('side').value) ? -amount-fee : amount-fee;
    document.querySelector('#event-preview').hidden = !assign && !(amount > 0);
    document.querySelector('#event-preview').textContent = assign ? '分配不改变账户现金和总份额。' : amount>0 ? `现金${delta>=0?'增加':'减少'} ${price(Math.abs(delta))} 元 · 余额 ${price(account.cash+delta)} 元` : '';
  }
  form.oninput = () => { requestId=crypto.randomUUID(); error.hidden=true; update(); };
  form.onchange = update;
  document.querySelector('#event-cancel').onclick = () => dialog.close();
  form.onsubmit = async event => {
    event.preventDefault();
    save.disabled = true; error.hidden = true;
    const side = field('side').value;
    const payload = {request_id:requestId, revision:account.revision, date:field('date').value, side};
    if (side==='assign') {
      Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), strategy:field('strategy').value, from_strategy:field('from_strategy').value});
    } else if (['buy','sell'].includes(side)) {
      Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), price:Number(field('price').value), fee:Number(field('fee').value), strategy:field('strategy').value});
    } else {
      payload.amount=Number(field('amount').value);
      payload.strategy=field('strategy').value;
    }
    try {
      await accountRequest('/api/etf/event', payload);
      await reloadAccount();
      dialog.close();
      document.querySelector('#status').textContent = '已记账，持仓、现金和建议已更新。';
    } catch (failure) {
      error.textContent = failure.message; error.hidden=false; save.disabled=false;
    }
  };
  update(); dialog.showModal();
}

function openBudgets(account) {
  const dialog = document.querySelector('#budget-dialog');
  const form = document.querySelector('#budget-form');
  const error = document.querySelector('#budget-error');
  form.reset(); error.hidden = true;
  for (const strategy of data.strategies) {
    form.elements.namedItem(strategy.id).value = finite(strategy.budget_pct) ? +(strategy.budget_pct*100).toFixed(4) : '';
  }
  document.querySelector('#budget-cancel').onclick = () => dialog.close();
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('[type=submit]');
    button.disabled = true; error.hidden = true;
    const budgets = {};
    for (const id of ['core','rotation']) {
      const value = form.elements.namedItem(id).value;
      if (value!=='') budgets[id]=Number(value)/100;
    }
    try {
      await accountRequest('/api/etf/budgets', {revision:account.revision, budgets});
      await reloadAccount(); dialog.close();
    } catch (failure) {
      error.textContent = failure.message; error.hidden=false;
    } finally { button.disabled=false; }
  };
  dialog.showModal();
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
    {name:'核心持仓',value:data.strategies[0].invested,color:'#22735b'},
    {name:'轮动持仓',value:data.strategies[1].invested,color:'#7393a2'},
    {name:'未分配持仓',value:account.strategy_positions.filter(row=>row.strategy==='unassigned').reduce((sum,row)=>sum+row.market_value,0),color:'#b1884c'},
    {name:'现金',value:account.cash,color:'#c2d3c5'}
  ];
}

function allocationCharts(account) {
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
    const delta = finite(row.target) ? (row.actual_pct-row.target)*100 : null;
    let deviation='—';
    if (delta!==null) deviation=Math.abs(delta)<.005?'已对齐':`${delta>0?'超配':'低配'} ${Math.abs(delta).toFixed(2)} 个百分点`;
    return `<div class="allocation-row"><div class="allocation-row-top"><strong>${esc(row.name)}</strong><b>${price(row.assets)} <small>元</small></b></div><div class="allocation-row-labels"><span>实际 ${actual.toFixed(1)}%</span><span>${target===null?'目标未设置':`目标 ${target.toFixed(1)}%`}</span></div><div class="allocation-track" role="img" aria-label="${esc(row.name)}实际${actual.toFixed(1)}%，${target===null?'目标未设置':`目标${target.toFixed(1)}%`}"><div class="allocation-fill allocation-${row.id}" style="width:${actual}%"></div>${target===null?'':`<i class="allocation-target" style="left:${target}%"></i>`}</div><div class="allocation-row-foot"><span>持仓 ${number(row.invested)} · 现金 ${number(row.cash)}</span><span>${deviation}</span></div></div>`;
  }).join('');
  return `<div class="allocation-grid"><section class="section asset-panel"><div class="section-heading"><h2>资产构成</h2></div><div class="asset-layout"><svg class="asset-donut" viewBox="0 0 210 210" role="img" aria-label="账户持仓与现金占比"><circle cx="105" cy="105" r="78" fill="none" stroke="#edf1eb" stroke-width="24"/>${rings}<text id="donut-label" x="105" y="91" text-anchor="middle" class="donut-label">总资产</text><text id="donut-value" x="105" y="117" text-anchor="middle" class="donut-value">${number(account.equity)}</text><text id="donut-share" x="105" y="137" text-anchor="middle" class="donut-label">元</text></svg><div class="asset-legend">${slices.map((slice,index)=>`<button type="button" class="asset-legend-item" data-asset-index="${index}"><i style="background:${slice.color}"></i><span>${slice.name}<small>${price(slice.value)} 元</small></span><b>${ratio(slice.value,account.equity)}</b></button>`).join('')}</div></div></section><section class="section strategy-panel"><div class="section-heading"><h2>策略配比</h2><div class="allocation-actions"><button type="button" id="budget-settings" class="text-button">目标比例</button><button type="button" id="cash-settings" class="text-button">分配现金</button></div></div><div class="allocation-legend"><i></i> 实际 <i class="target-key"></i> 目标 <span>含各策略现金</span></div>${bars}</section></div>`;
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

function strategyPositionTable(strategy) {
  if (!strategy.target_weights) return allocatedPositionTable(strategy.positions);
  const wallet = data.accounts[0];
  const codes=[...new Set([...strategy.positions.map(row=>row.code),...Object.keys(strategy.target_weights).filter(code=>code!=='CASH'),'CASH'])];
  const pct=value=>finite(value)?`${(value*100).toFixed(2)}%`:'—';
  const rows=codes.map(code=>{
    const position=strategy.positions.find(row=>row.code===code);
    const name=code==='CASH'?'现金':position?.name || wallet.positions.find(row=>row.code===code)?.name || code;
    const actual=code==='CASH'?strategy.cash:position?.market_value||0;
    const weight=strategy.target_weights[code];
    const target=finite(strategy.budget)&&finite(weight)?strategy.budget*weight:null;
    return `<tr><td>${esc(name)}${code==='CASH'?'':`<small>${esc(code)}</small>`}</td><td>${code==='CASH'?'—':number(position?.count || 0)}</td><td>${price(position?.close)}</td><td>${money(actual)}</td><td>${pct(strategy.assets>0?actual/strategy.assets:null)}</td><td>${pct(weight)}</td><td>${money(target)}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th>资产</th><th>份额</th><th>价格 / 元</th><th>当前金额 / 元</th><th>实际比例</th><th>目标比例</th><th>目标金额 / 元</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function openCashAllocation(account) {
  const dialog=document.querySelector('#cash-dialog'),form=document.querySelector('#cash-form');
  const error=document.querySelector('#cash-error');
  let requestId=crypto.randomUUID();
  form.reset();error.hidden=true;
  for (const id of ['core','rotation']) form.elements.namedItem(id).value=account.cash_by_strategy[id].toFixed(2);
  function update() {
    requestId=crypto.randomUUID();
    const remaining=account.cash-Number(form.elements.namedItem('core').value)-Number(form.elements.namedItem('rotation').value);
    document.querySelector('#cash-remaining').textContent=`未分配现金 ${price(remaining)} 元`;
  }
  form.oninput=update;
  document.querySelector('#cash-cancel').onclick=()=>dialog.close();
  document.querySelector('#cash-fill-target').onclick=()=>{
    const core=data.strategies.find(strategy=>strategy.id==='core');
    if (finite(core?.budget)) form.elements.namedItem('core').value=Math.max(0,core.budget-core.invested).toFixed(2);
    update();
  };
  form.onsubmit=async event=>{
    event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;error.hidden=true;
    const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    const payload={request_id:requestId,revision:account.revision,date:day,side:'cash_assign',cash_allocations:{core:Number(form.elements.namedItem('core').value),rotation:Number(form.elements.namedItem('rotation').value)}};
    try {await accountRequest('/api/etf/event',payload);await reloadAccount();dialog.close();}
    catch (failure) {error.textContent=failure.message;error.hidden=false;}
    finally {button.disabled=false;}
  };
  update();dialog.showModal();
}
