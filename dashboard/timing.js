"use strict";

const page = "/research/timing";
const api = "/api" + page;
const code = location.pathname.split("/")[3] || "";
const byId = id => document.getElementById(id);
const fullColumns = ["规则", "年化收益%", "最大回撤%", "最长水下交易日", "平均仓位%", "交易次数",
  "较长拿年化差pp", "较长拿回撤改善pp", "较长拿水下缩短交易日"];
const rollingColumns = ["规则", "窗口数", "超额年化中位数pp", "回撤改善中位数pp",
  "水下缩短交易日中位数", "跑赢长拿比例%", "三项均不差比例%"];
const colors = ["#54665d", "#1d7664", "#a95b12", "#395ead", "#a43f5d", "#7751a6", "#14818d", "#88623b"];
const chartTitles = ["净值（倍）", "回撤（%）", "仓位（%）"];
const chartDefaults = /^(长拿|唐奇安20\/10|MA20_ATR20×1\.5_下轨退出|Cycle v8\.1（入场满仓）)$/;
const formatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const sections = {
  asset: byId("asset"), comparison: byId("comparison"), group: byId("report-group"),
  status: byId("timing-status"), error: byId("timing-error"), generate: byId("generate"),
  charts: byId("timing-charts"), range: byId("chart-range"), cursor: byId("timing-cursor"),
};
let overview, group, timer, loadingGroup = 0;
let selected = new Set(), seriesColors = {}, plotCursors = [], indexStart = 0, activeIndex = -1;

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
  const current = sections.group.value === "current";
  const years = Number(byId("hold-years").value);
  const rows = (current && overview.version === group.version ? overview.rolling : group.rolling)
    .filter(row => row.years === years);
  if (rows.length) {
    const wrap = element("div");
    wrap.className = "table-wrap";
    wrap.append(table(rows, rollingColumns));
    container.replaceChildren(wrap);
    return;
  }
  let message = "暂无该期限的结果，可能没有完整持有窗口。";
  if (current && overview.version === group.version) {
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
    let min = metric === 0 ? Infinity : 0;
    let max = metric === 0 ? -Infinity : metric === 2 ? 100 : 0;
    if (metric !== 2) for (const name of names) {
      for (let i = indexStart; i <= last; i++) {
        min = Math.min(min, series[name][metric][i]);
        max = Math.max(max, series[name][metric][i]);
      }
    }
    if (max === min) { min -= 0.1; if (metric === 0) max += 0.1; }
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
    if (metric === 2) {
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
  selected = new Set(names.filter(name => chartDefaults.test(name)));
  seriesColors = Object.fromEntries(names.map((name, i) => [name, colors[i % colors.length]]));
  byId("timing-period").textContent = "回测窗口 " + result.start + " 至 " + result.end
    + " · " + result.source + " · 单边模拟成本 " + number(result.cost_pct) + "%";
  byId("timing-metrics").replaceChildren(table(result.metrics, fullColumns));
  const rules = byId("rule-list");
  rules.replaceChildren();
  for (const name of names) {
    const label = element("label");
    const check = element("input");
    check.type = "checkbox";
    check.checked = selected.has(name);
    check.disabled = name === "长拿";
    const swatch = element("span");
    swatch.className = check.disabled ? "timing-swatch benchmark" : "timing-swatch";
    swatch.style.setProperty("--series", seriesColors[name]);
    swatch.setAttribute("aria-hidden", "true");
    check.addEventListener("change", () => {
      if (check.checked) selected.add(name);
      else selected.delete(name);
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
      body: JSON.stringify({ code, refresh: Boolean(Object.keys(overview.groups).length) }),
    });
    await refresh();
  } catch (error) {
    showError(error.message);
  } finally {
    sections.generate.disabled = false;
  }
});
sections.group.addEventListener("change", loadGroup);
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
  refresh();
}
