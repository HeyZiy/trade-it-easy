"use strict";

const page = "/research/timing";
const api = "/api" + page;
const code = location.pathname.split("/")[3] || "";
const byId = id => document.getElementById(id);
const fullColumns = ["族", "规则", "年化收益%", "最大回撤%", "最长水下交易日", "平均仓位%", "交易次数",
  "较长拿年化差pp", "较长拿回撤改善pp", "较长拿水下缩短交易日"];
const rollingColumns = ["规则", "窗口数", "超额年化中位数pp", "回撤改善中位数pp",
  "水下缩短交易日中位数", "跑赢长拿比例%", "三项均不差比例%"];
const colors = ["#54665d", "#1d7664", "#a95b12", "#395ead", "#a43f5d", "#7751a6", "#14818d",
  "#88623b", "#2f8f4e", "#b03a2e", "#5d6d7e", "#8e44ad", "#16a085", "#c0392b"];
const chartTitles = ["净值（倍）", "回撤（%）", "仓位（%）"];
const chartDefaults = /^(长拿|半仓长拿|网格·每月重定|MA20\+ATR×1\.5|唐奇安20\/10|MOM252)$/;
const formatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const sections = {
  asset: byId("asset"), comparison: byId("comparison"), group: byId("report-group"),
  status: byId("timing-status"), error: byId("timing-error"), generate: byId("generate"),
  charts: byId("timing-charts"), range: byId("chart-range"), cursor: byId("timing-cursor"),
};
let overview, group, timer, loadingGroup = 0;
let selected = new Set(), seriesColors = {}, plotCursors = [], indexStart = 0, activeIndex = -1;
let sortState = { column: null, dir: 1 }, familyOf = {};

function element(tag, label) {
  const node = document.createElement(tag);
  if (label !== undefined) node.textContent = label;
  return node;
}

function svg(tag, attributes, label) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (label !== undefined) node.textContent = label;
  return node;
}

function number(value) {
  return Number.isFinite(value) ? formatter.format(value) : "—";
}

function showError(message) {
  sections.error.hidden = !message;
  sections.error.textContent = message || "";
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "暂时无法读取报告，请稍后重试。");
  return data;
}

function table(rows, columns) {
  const result = element("table");
  const heading = result.createTHead().insertRow();
  for (const title of columns) {
    const cell = element("th", title);
    cell.scope = "col";
    heading.append(cell);
  }
  const body = result.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const title of columns) {
      line.insertCell().textContent = title === "规则" ? row.strategy : number(row[title]);
    }
  }
  return result;
}

function metricTable(rows) {
  const result = element("table");
  const heading = result.createTHead().insertRow();
  for (const title of fullColumns) {
    const cell = element("th", title);
    cell.scope = "col";
    cell.className = "sortable" + (title === "族" || title === "规则" ? " text-left" : "");
    cell.setAttribute("aria-sort", sortState.column === title
      ? (sortState.dir > 0 ? "ascending" : "descending") : "none");
    cell.addEventListener("click", () => {
      sortState = { column: title, dir: sortState.column === title ? -sortState.dir : 1 };
      renderMetrics();
    });
    heading.append(cell);
  }
  const sorted = [...rows];
  if (sortState.column) {
    const key = sortState.column === "规则" ? "strategy" : sortState.column;
    sorted.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (typeof av === "string" || typeof bv === "string") {
        return sortState.dir * String(av ?? "").localeCompare(String(bv ?? ""), "zh");
      }
      return sortState.dir * ((av ?? -Infinity) - (bv ?? -Infinity));
    });
  }
  const body = result.createTBody();
  for (const row of sorted) {
    const line = body.insertRow();
    for (const title of fullColumns) {
      const cell = line.insertCell();
      if (title === "族" || title === "规则") cell.className = "text-left";
      cell.textContent = title === "规则" ? row.strategy
        : title === "族" ? row.族 : number(row[title]);
    }
  }
  return result;
}

function renderMetrics() {
  if (!group) return;
  const family = byId("family-filter").value;
  const rows = group.metrics.filter(row => !family || row.族 === family);
  byId("timing-metrics").replaceChildren(metricTable(rows));
}

function syncRuleChecks() {
  for (const check of byId("rule-list").querySelectorAll("input")) {
    check.checked = selected.has(check.dataset.name);
  }
}

async function loadCatalog() {
  const list = byId("catalog-list");
  try {
    const { reports } = await request(api);
    byId("catalog-count").textContent = reports.length + " 只";
    list.replaceChildren();
    for (const item of reports) {
      const link = element("a", item.name);
      link.href = page + "/" + item.code;
      if (item.code === code) link.setAttribute("aria-current", "page");
      link.append(element("small", item.code + " · 截至 " + item.end));
      list.append(link);
    }
    if (!reports.length) list.append(element("p", "暂无已保存的报告。可输入代码生成。"));
  } catch {
    list.replaceChildren(element("p", "暂时无法读取已有报告列表。"));
  }
}

function renderRolling() {
  const container = byId("timing-rolling-results");
  if (!group) return;
  const isCurrentVersion = sections.group.value === "current" && overview.version === group.version;
  const years = Number(byId("hold-years").value);
  const rows = (isCurrentVersion ? overview.rolling : group.rolling)
    .filter(row => row.years === years);
  if (rows.length) {
    const wrap = element("div");
    wrap.className = "table-wrap";
    wrap.append(table(rows, rollingColumns));
    container.replaceChildren(wrap);
    return;
  }
  let message = "暂无该期限的结果，可能没有完整持有窗口。";
  if (isCurrentVersion) {
    if (overview.rolling_state === "ready") message = "没有完整的 " + years + " 年持有窗口。";
    else if (overview.phase) message = "正在计算多起点结果…";
    else message = "多起点结果未完成，可重新生成。";
  }
  const note = element("p", message);
  note.className = "subtle";
  container.replaceChildren(note);
}

function updateCursor(index) {
  if (index === activeIndex) return;
  activeIndex = index;
  sections.cursor.value = index;
  sections.cursor.setAttribute("aria-valuetext", group.dates[index]);
  for (const { line, x } of plotCursors) {
    line.setAttribute("x1", x(index));
    line.setAttribute("x2", x(index));
  }
  const values = byId("chart-values");
  values.replaceChildren(element("strong", group.dates[index]));
  for (const [name, data] of Object.entries(group.series)) {
    if (selected.has(name)) {
      values.append(element("span", name + " · " + number(data[0][index]) + " 倍 / "
        + number(data[1][index]) + "% / " + number(data[2][index]) + "% 仓位"));
    }
  }
}

function drawCharts() {
  if (!group || sections.comparison.hidden) return;
  const { dates, series } = group;
  const last = dates.length - 1;
  const years = Number(sections.range.value);
  indexStart = 0;
  if (years) {
    const start = new Date(dates[last] + "T00:00:00Z");
    start.setUTCFullYear(start.getUTCFullYear() - years);
    indexStart = dates.findIndex(date => date >= start.toISOString().slice(0, 10));
  }
  const width = Math.max(240, sections.charts.clientWidth);
  const height = 126, left = 46, right = width - 10, top = 9, bottom = height - 16;
  const x = index => left + (right - left) * (index - indexStart) / Math.max(1, last - indexStart);
  const names = Object.keys(series).filter(name => selected.has(name));
  sections.charts.replaceChildren();
  plotCursors = [];
  chartTitles.forEach((title, metric) => {
    const figure = element("figure");
    figure.className = "timing-plot";
    const graph = svg("svg", { viewBox: "0 0 " + width + " " + height, role: "img", "aria-label": title });
    const isNav = metric === 0;
    const isExposure = metric === 2;
    let min = isNav ? Infinity : 0;
    let max = isNav ? -Infinity : 0;
    if (isExposure) {
      max = 100;
    } else {
      for (const name of names) {
        const values = series[name][metric];
        for (let i = indexStart; i <= last; i++) {
          min = Math.min(min, values[i]);
          max = Math.max(max, values[i]);
        }
      }
    }
    if (max === min) {
      min -= 0.1;
      if (isNav) max += 0.1;
    }
    const y = value => bottom - (bottom - top) * (value - min) / (max - min);
    for (const tick of [min, (min + max) / 2, max]) {
      graph.append(svg("line", { x1: left, x2: right, y1: y(tick), y2: y(tick), class: "timing-gridline" }),
        svg("text", { x: left - 5, y: y(tick) + 4, "text-anchor": "end" }, number(tick)));
    }
    for (const name of names) {
      const points = series[name][metric].slice(indexStart).map((value, i) =>
        x(i + indexStart).toFixed(1) + " " + y(value).toFixed(1));
      const path = svg("path", { d: "M" + points.join("L"), class: name === "长拿" ? "benchmark" : "" });
      path.style.setProperty("--series", seriesColors[name]);
      graph.append(path);
    }
    const line = svg("line", { class: "cursor", y1: top, y2: bottom });
    plotCursors.push({ line, x });
    graph.append(line);
    if (isExposure) {
      graph.append(svg("text", { x: left, y: height - 2 }, dates[indexStart]),
        svg("text", { x: right, y: height - 2, "text-anchor": "end" }, dates[last]));
    }
    graph.addEventListener("pointermove", event => {
      const offset = event.clientX - graph.getBoundingClientRect().left;
      const fraction = Math.max(0, Math.min(1, (offset - left) / (right - left)));
      updateCursor(indexStart + Math.round(fraction * (last - indexStart)));
    });
    figure.append(element("figcaption", title), graph);
    sections.charts.append(figure);
  });
  sections.cursor.min = indexStart;
  sections.cursor.max = last;
  const previous = activeIndex;
  activeIndex = -1;
  updateCursor(previous < indexStart || previous > last ? last : previous);
}

function renderGroup(result) {
  group = result;
  const names = Object.keys(result.series);
  familyOf = Object.fromEntries(result.metrics.map(row => [row.strategy, row.族]));
  selected = new Set(names.filter(name => chartDefaults.test(name)));
  if (selected.size <= 1) selected = new Set(names.slice(0, Math.min(4, names.length)));
  if (names.includes("长拿")) selected.add("长拿");
  seriesColors = Object.fromEntries(names.map((name, i) => [name, colors[i % colors.length]]));
  byId("timing-period").textContent = "回测窗口 " + result.start + " 至 " + result.end
    + " · " + result.source + " · 单边模拟成本 " + number(result.cost_pct) + "%";
  const families = [...new Set(result.metrics.map(row => row.族).filter(Boolean))];
  const familyFilter = byId("family-filter");
  const keepFamily = familyFilter.value;
  familyFilter.replaceChildren(element("option", "全部"),
    ...families.map(family => { const option = element("option", family); option.value = family; return option; }));
  familyFilter.value = families.includes(keepFamily) ? keepFamily : "";
  const curveFamily = byId("curve-family");
  curveFamily.replaceChildren(element("option", "自定义"),
    ...families.map(family => { const option = element("option", family); option.value = family; return option; }));
  renderMetrics();
  const rules = byId("rule-list");
  rules.replaceChildren();
  for (const name of names) {
    const label = element("label");
    const check = element("input");
    check.type = "checkbox";
    check.dataset.name = name;
    check.checked = selected.has(name);
    check.disabled = name === "长拿";
    const swatch = element("span");
    swatch.className = check.disabled ? "timing-swatch benchmark" : "timing-swatch";
    swatch.style.setProperty("--series", seriesColors[name]);
    swatch.setAttribute("aria-hidden", "true");
    check.addEventListener("change", () => {
      if (check.checked) selected.add(name);
      else selected.delete(name);
      curveFamily.value = "";
      drawCharts();
    });
    label.append(check, swatch, document.createTextNode(name));
    rules.append(label);
  }
  sections.range.value = "0";
  activeIndex = -1;
  sections.comparison.hidden = false;
  drawCharts();
  renderRolling();
}

async function loadGroup() {
  const key = sections.group.value;
  const ticket = ++loadingGroup;
  group = null;
  sections.comparison.hidden = true;
  try {
    const result = await request(api + "/" + code + "/" + key);
    if (ticket === loadingGroup) renderGroup(result);
  } catch (error) {
    if (ticket === loadingGroup) showError(error.message);
  }
}

async function refresh() {
  const before = overview;
  try {
    overview = await request(api + "/" + code);
    byId("asset-name").textContent = overview.name;
    if (overview.index_code) byId("index-code").value = overview.index_code;
    const keys = Object.keys(overview.groups);
    const active = Boolean(overview.phase);
    sections.generate.hidden = active;
    sections.generate.disabled = false;
    sections.generate.textContent = keys.length ? "重新生成" : "生成报告";
    byId("timing-empty").hidden = active || keys.length;
    sections.status.textContent = ({
      queued: "已加入生成队列…",
      history: "正在准备行情并计算全期结果…",
      rolling: "全期结果已保存，正在计算多起点…",
    })[overview.phase] || "";
    showError(overview.error);
    if (!before || Boolean(before.version) !== Boolean(overview.version)) {
      sections.group.replaceChildren(...keys.map(key => {
        const option = element("option", overview.groups[key]);
        option.value = key;
        return option;
      }));
      byId("group-controls").hidden = keys.length <= 1;
      if (keys.length) loadGroup();
    } else if (sections.group.value === "current" && before.version !== overview.version) {
      loadGroup();
    } else {
      renderRolling();
    }
    if (overview.case_study) byId("timing-case").hidden = false;
    if (active) timer = setTimeout(refresh, 1500);
    if (overview.version && before && before.version !== overview.version) loadCatalog();
  } catch (error) {
    showError(error.message);
    if (before?.phase) timer = setTimeout(refresh, 3000);
  }
}

byId("lookup").addEventListener("submit", event => {
  event.preventDefault();
  location.assign(page + "/" + byId("etf-code").value.trim());
});
sections.generate.addEventListener("click", async () => {
  sections.generate.disabled = true;
  showError("");
  clearTimeout(timer);
  try {
    await request(api, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Trade-Request": "dashboard" },
      body: JSON.stringify({ code, refresh: Boolean(Object.keys(overview.groups).length),
        index_code: byId("index-code").value.trim() || undefined }),
    });
    await refresh();
  } catch (error) {
    showError(error.message);
  } finally {
    sections.generate.disabled = false;
  }
});
sections.group.addEventListener("change", loadGroup);
byId("family-filter").addEventListener("change", renderMetrics);
byId("curve-family").addEventListener("change", () => {
  const family = byId("curve-family").value;
  if (!family || !group) return;
  selected = new Set(Object.keys(group.series).filter(name => familyOf[name] === family));
  if (group.series["长拿"]) selected.add("长拿");
  syncRuleChecks();
  drawCharts();
});
byId("hold-years").addEventListener("change", renderRolling);
sections.range.addEventListener("change", drawCharts);
sections.cursor.addEventListener("input", () => updateCursor(Number(sections.cursor.value)));
window.addEventListener("resize", drawCharts);
byId("timing-case").addEventListener("toggle", async event => {
  const body = byId("timing-case-body");
  if (!event.target.open || body.dataset.loaded) return;
  try {
    const { html } = await request(api + "/" + code + "/case");
    body.innerHTML = html; // The server strips all attributes and active tags.
    body.dataset.loaded = "true";
  } catch (error) {
    body.textContent = error.message;
  }
});

byId("catalog").open = !code;
loadCatalog();
if (code) {
  byId("etf-code").value = code;
  byId("asset-code").textContent = code;
  byId("asset-name").textContent = "ETF " + code;
  sections.status.textContent = "正在查找报告…";
  sections.asset.hidden = false;
  byId("index-controls").hidden = false;
  refresh();
}
