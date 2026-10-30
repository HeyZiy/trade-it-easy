const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = value => typeof value === 'number' && Number.isFinite(value);
const number = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:0}) : '—';
const price = value => finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:4}) : '—';
const signed = value => `${value > 0 ? '+' : ''}${value.toFixed(2)}`;
const percent = nav => finite(nav) && nav > 0 ? `${signed((nav - 1) * 100)}%` : '—';
const excess = (nav, base) => finite(nav) && finite(base) ? `${signed((nav - base) * 100)} 个百分点` : '—';
const ratio = (part, whole) => finite(part) && finite(whole) && whole > 0 ? `${(part / whole * 100).toFixed(1)}%` : '—';
const reportDate = value => /^\d{8}$/.test(value) ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6)}` : value;
const timestamp = day => Date.parse(`${day}T00:00:00Z`);
const SIDES = {buy:'买入', sell:'卖出', dividend:'分红'};
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

function chartSection(account) {
  return `<section class="section chart-section" aria-labelledby="chart-title">
    <div class="section-heading"><div><div class="eyebrow">PERFORMANCE</div><h2 id="chart-title">${esc(account?.name || '账户')} · 收益走势</h2></div>
      <div class="range-buttons" role="group" aria-label="收益曲线时间范围">${[['30','近30天'],['90','近90天'],['all','全部']].map(([value,label]) => `<button type="button" data-range="${value}" class="${chartRange === value ? 'active' : ''}" aria-pressed="${chartRange === value}">${label}</button>`).join('')}</div></div>
    <div class="chart-summary" aria-live="polite">
      <div class="chart-metric primary"><small><i class="legend-line"></i>账户累计收益</small><b id="chart-nav">—</b></div>
      <div class="chart-metric benchmark"><small><i class="legend-line benchmark"></i>同期基准</small><b id="chart-base">—</b></div>
      <div class="chart-metric"><small>超额 / 百分点</small><b id="chart-excess">—</b></div>
    </div><div id="chart-date" class="chart-date"></div>
    <div id="chart-stage" class="chart-stage"></div>
    <input id="chart-cursor" class="chart-slider" type="range" aria-label="选择日期查看收益" hidden>
    <div class="chart-foot"><span>基准：${esc(account?.benchmark_name || '同期基准')}</span><span>收盘累计口径 · 拖动查看日期</span></div>
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
  const points = chartPoints(account?.history || [], chartRange);
  const value = (row, key) => finite(row[key]) && row[key] > 0 ? (row[key] - 1) * 100 : null;
  const values = points.flatMap(row => [value(row,'nav'),value(row,'benchmark_nav')]).filter(finite);
  if (!points.length || !values.length) {
    stage.innerHTML = '<div class="empty">收盘成绩正在积累，暂时没有可绘制的数据。</div>';
    slider.hidden = true;
    document.querySelector('#chart-date').textContent = '暂无有效成绩';
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
    document.querySelector('#chart-date').textContent = `${row.date}${value(row,'nav') === null ? ' · 当日成绩缺失' : ''}`;
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
  const core = data.accounts.find(account => account.id==='core');
  const rows = data.accounts.map(account => {
    const p = account.performance;
    return `<tr><td>${esc(account.name)}</td><td>${percent(p?.nav)}</td><td>${percent(p?.benchmark_nav)}</td><td>${excess(p?.nav,p?.benchmark_nav)}</td><td>${ratio(p?.invested,p?.equity)}</td><td>${esc(p?.date)}</td></tr>`;
  }).join('');
  return `${data.accounts.map(warnings).join('')}${chartSection(core)}<section class="section"><div class="section-heading"><h2>账户一览</h2><span class="subtle">分别统计，不合并收益</span></div><div class="table-wrap"><table><thead><tr><th>账户</th><th>收益</th><th>基准</th><th>超额</th><th>仓位</th><th>成绩日期</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

function strategyPage(account) {
  const p = account.performance;
  const priced = account.positions.filter(row => finite(row.close)).length;
  const closeNote = account.positions.length && priced===account.positions.length ? `收盘价截至 ${esc(p?.date)}` : '价格缺失时按成本占比展示';
  const stats = [[`${number(account.positions.length)} 只`,'持仓'],[ratio(p?.invested,p?.equity),'仓位'],[ratio(p?.cash,p?.equity),'现金'],[ratio(p?.receivables,p?.equity),'应收红利']];
  return `${warnings(account)}${chartSection(account)}<div class="stats">${stats.map(([v,label]) => `<div class="stat"><span>${label}</span><b>${v}</b></div>`).join('')}</div><section class="section"><div class="section-heading"><h2>账户持仓</h2><span class="subtle">${closeNote}</span></div>${positionTable(account.positions)}</section><section class="section"><div class="section-heading"><h2>近期流水</h2><span class="subtle">最近 20 笔</span></div>${tradeTable(account.trades)}</section>`;
}

function render() {
  chartObserver?.disconnect();
  const tabs = [{id:'summary',name:'汇总'},...data.strategies];
  document.querySelector('#tabs').innerHTML = tabs.map((tab,index) => `<button type="button" class="${index===selected ? 'active' : ''}" aria-pressed="${index===selected}" data-index="${index}">${esc(tab.name)}</button>`).join('');
  document.querySelectorAll('#tabs button').forEach(button => button.onclick=() => { selected=Number(button.dataset.index); render(); });
  const account = selected===0 ? data.accounts.find(row => row.id==='core') : data.strategies[selected-1];
  document.querySelector('#account').innerHTML = selected===0 ? summaryPage() : strategyPage(account);
  drawChart(account);
  document.querySelectorAll('[data-range]').forEach(button => button.onclick=() => {
    chartRange=button.dataset.range;
    document.querySelectorAll('[data-range]').forEach(item => { item.classList.toggle('active',item.dataset.range===chartRange); item.setAttribute('aria-pressed',item.dataset.range===chartRange); });
    drawChart(account);
  });
  chartObserver = new ResizeObserver(() => { drawChart(account); measureReport(); });
  chartObserver.observe(document.querySelector('#chart-stage'));
  const prefix = REPORT_PREFIX[account?.id];
  const report = data.reports.find(row => row.file.startsWith(prefix)) || data.reports[0];
  if (report) selectReport(report.file);
}

function reportKind(file) {
  if (file.startsWith('portfolio_close_')) return '收盘汇总';
  if (file.startsWith('etf_weekly_')) return '周度配置';
  return '策略日报';
}

function renderReportTabs() {
  document.querySelector('#report-list').innerHTML = data.reports.map((report,index) => `<button type="button" role="tab" id="report-tab-${index}" aria-controls="report-panel" aria-selected="${report.file===selectedReport}" class="report-tab ${report.file===selectedReport ? 'active' : ''}" data-file="${esc(report.file)}"><strong>${esc(report.name)}</strong><time>${esc(reportDate(report.date))}</time></button>`).join('');
  const buttons = [...document.querySelectorAll('.report-tab')];
  buttons.forEach((button,index) => {
    button.onclick = () => selectReport(button.dataset.file);
    button.onkeydown = event => {
      if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const next=(index+(['ArrowLeft','ArrowUp'].includes(event.key) ? -1 : 1)+buttons.length)%buttons.length;
      selectReport(buttons[next].dataset.file);
      document.querySelector(`#report-tab-${next}`).focus();
    };
  });
  const active = buttons.find(button => button.dataset.file===selectedReport);
  if (active) document.querySelector('#report-panel').setAttribute('aria-labelledby',active.id);
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
  renderReportTabs();
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
  if (!data.reports.length) {
    document.querySelector('.report-shell').innerHTML='<div class="empty">暂无报告，策略运行后会在这里更新。</div>';
  }
  render();
}).catch(() => { document.querySelector('#status').textContent='数据读取失败，请刷新页面重试。'; });
