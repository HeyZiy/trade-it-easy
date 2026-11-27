import { esc, finite } from './common.js';

import { accountName, signed, percent, excess, timestamp, reportDate } from './dashboard-format.js';

const REPORT_PREFIX = {rotation:'etf_rotation_advice_', quality:'quality_pool_advice_'};

let chartRange = 'all', chartObserver;
let selectedReport, reportRequest;
const reportCache = new Map();

export function chartSection(account) {
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

async function selectReport(report) {
  const {file} = report;
  reportRequest?.abort();
  selectedReport=file;
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
    document.querySelector('#report-retry').onclick=() => selectReport(report);
  }
}

export function toggleReport() {
  const expanded=document.querySelector('#report-reader').classList.toggle('expanded');
  document.querySelector('#report-expand').textContent=expanded ? '收起报告 ↑' : '展开全文 ↓';
  document.querySelector('#report-expand').setAttribute('aria-expanded',expanded);
}

export function bindPerformance(account, reports) {
  chartObserver?.disconnect();
  drawChart(account);
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
  const report = prefix && reports.find(row => row.file.startsWith(prefix));
  document.querySelector('#report-panel').hidden = !report;
  document.querySelector('#report-empty').hidden = !!report;
  if (report) selectReport(report);
}
