"use strict";

// The graph: numbers found in the received lines (settings: "name | prefix | suffix" or "name | /regex/"),
// kept as points over time and drawn as lines over the output.

const GRAPH_COLOURS = ["#5b9dff", "#e5a83a", "#3fbf7f", "#c48bff", "#ef7ba8", "#4dd0e1", "#ff8a65", "#a5d6a7"];
const GRAPH_MAX_POINTS = 20000;
const GRAPH_MARGIN = { left: 56, right: 10, top: 8, bottom: 20 };

const newGraph = (profile) => ({ specs: parseSeries(profile.graphSeries), series: new Map(), partial: "", decoder: new TextDecoder(), windowSec: 60, each: false });
const updateGraphSpecs = (tab) => { tab.graph.specs = parseSeries(tab.profile.graphSeries); };

function addPoint(graph, name, t, value) {
  if (!graph.series.has(name)) graph.series.set(name, []);
  const points = graph.series.get(name);
  points.push({ t, v: value });
  if (points.length > GRAPH_MAX_POINTS) points.splice(0, points.length - GRAPH_MAX_POINTS);
}

// Received data is read for numbers as it arrives, whether the graph is shown or not.
function feedGraph(tab, ev) {
  const graph = tab.graph;
  if (!graph.specs.length) return;
  const lines = (graph.partial + graph.decoder.decode(ev.text, { stream: true })).split(/\r\n|\r|\n/);
  graph.partial = lines.pop().slice(-4096);
  for (const line of lines) {
    for (const { name, value } of extractValues(line, graph.specs)) addPoint(graph, name, ev.t, value);
  }
  if (lines.length) scheduleGraph(tab);
}

const graphOpen = (pane) => !pane.graphbar.hidden;

function scheduleGraph(tab) {
  if (tab.graphFrame || tab !== tab.pane.active || !graphOpen(tab.pane)) return;
  tab.graphFrame = requestAnimationFrame(() => { tab.graphFrame = 0; drawGraph(tab); });
}

function toggleGraph(pane) {
  pane.graphbar.hidden = !pane.graphbar.hidden;
  syncGraph(pane);
  fitAll();  // the lines get the room that is left
}

// Show the graph of the pane's tab (or hide it), and its settings.
function syncGraph(pane) {
  const tab = pane.active;
  pane.viewbar.querySelector("[data-graph]").setAttribute("aria-pressed", String(graphOpen(pane)));
  if (!tab || !graphOpen(pane)) return;
  pane.graphbar.querySelector("[data-graph-window]").value = String(tab.graph.windowSec);
  pane.graphbar.querySelector("[data-graph-each]").checked = tab.graph.each;
  drawGraph(tab);
}

const graphValue = (v) => (Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.01) ? v.toExponential(2) : String(Math.round(v * 1000) / 1000));

// The points of a series inside the time window, and the window itself.
function graphView(graph, now) {
  const win = graph.windowSec ? graph.windowSec * 1000 : 0;
  const shown = [...graph.series].map(([name, points]) => ({ name, points: win ? points.filter((p) => p.t >= now - win) : points }));
  const all = shown.flatMap((s) => s.points);
  const from = win ? now - win : Math.min(...all.map((p) => p.t), now - 1000);
  const to = win ? now : Math.max(...all.map((p) => p.t), from + 1000);
  return { shown, all, from, to };
}

function drawGraph(tab) {
  const bar = tab.pane.graphbar;
  const canvas = bar.querySelector("canvas");
  if (!graphOpen(tab.pane) || tab !== tab.pane.active) return;
  drawLegend(bar, tab.graph);
  const ctx = canvas.getContext?.("2d");
  if (!ctx) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const css = getComputedStyle(document.documentElement);
  const colour = (name) => css.getPropertyValue(name).trim() || "#888";
  ctx.clearRect(0, 0, width, height);
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = colour("--muted");
  const graph = tab.graph;
  if (!graph.specs.length) {
    ctx.fillText("No series. Add lines such as  Temp | Temperature: | degC  under Advanced > Graph in the settings of the session.", 12, 22);
    return;
  }
  const { shown, all, from, to } = graphView(graph, Date.now());
  if (!all.length) {
    ctx.fillText("Waiting for lines with numbers…", 12, 22);
    return;
  }
  const plot = { x: GRAPH_MARGIN.left, y: GRAPH_MARGIN.top, w: width - GRAPH_MARGIN.left - GRAPH_MARGIN.right, h: height - GRAPH_MARGIN.top - GRAPH_MARGIN.bottom };
  const range = (points) => {  // the values a scale covers, with some room
    let low = Math.min(...points.map((p) => p.v));
    let high = Math.max(...points.map((p) => p.v));
    if (high === low) { low -= 1; high += 1; }
    const pad = (high - low) * 0.05;
    return { low: low - pad, high: high + pad };
  };
  const shared = range(all);
  const px = (t) => plot.x + ((t - from) / Math.max(to - from, 1)) * plot.w;
  const py = (v, r = shared) => plot.y + plot.h - ((v - r.low) / (r.high - r.low)) * plot.h;
  ctx.strokeStyle = colour("--border");
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {  // horizontal lines with their values (or the share of each series' own range)
    const v = shared.low + ((shared.high - shared.low) * i) / 4;
    ctx.beginPath(); ctx.moveTo(plot.x, py(v)); ctx.lineTo(plot.x + plot.w, py(v)); ctx.stroke();
    ctx.fillText(graph.each ? `${i * 25}%` : graphValue(v), plot.x - 6, py(v) + 4);
  }
  ctx.textAlign = "left"; ctx.fillText(`-${Math.round((to - from) / 1000)} s`, plot.x, height - 5);
  ctx.textAlign = "right"; ctx.fillText("now", plot.x + plot.w, height - 5);
  ctx.lineWidth = 1.6;
  shown.forEach((series) => {
    const own = graph.each && series.points.length ? range(series.points) : shared;
    ctx.strokeStyle = GRAPH_COLOURS[[...graph.series.keys()].indexOf(series.name) % GRAPH_COLOURS.length];
    ctx.beginPath();
    series.points.forEach((p, n) => (n ? ctx.lineTo(px(p.t), py(p.v, own)) : ctx.moveTo(px(p.t), py(p.v, own))));
    ctx.stroke();
  });
}

// What each line is, with its last value.
function drawLegend(bar, graph) {
  const names = graph.specs.map((s) => s.name);
  bar.querySelector(".graph-legend").replaceChildren(...names.map((name) => {
    const points = graph.series.get(name) || [];
    const chip = el("span", "chip");
    const swatch = el("i");
    swatch.style.background = GRAPH_COLOURS[names.indexOf(name) % GRAPH_COLOURS.length];
    chip.append(swatch, `${name}${points.length ? `: ${graphValue(points[points.length - 1].v)}` : ""}`);
    return chip;
  }));
}

// The points as CSV: time, series, value.
function graphCsv(tab) {
  const rows = [...tab.graph.series].flatMap(([name, points]) => points.map((p) => `${new Date(p.t).toISOString()},${name.replace(/[",\n]/g, "_")},${p.v}`));
  rows.sort();
  return `time,series,value\n${rows.join("\n")}\n`;
}

function wireGraph(pane) {
  const bar = pane.graphbar;
  pane.viewbar.querySelector("[data-graph]").addEventListener("click", () => toggleGraph(pane));
  bar.querySelector("[data-graph-window]").addEventListener("change", (e) => {
    if (pane.active) { pane.active.graph.windowSec = Number(e.target.value); drawGraph(pane.active); }
  });
  bar.querySelector("[data-graph-each]").addEventListener("change", (e) => {
    if (pane.active) { pane.active.graph.each = e.target.checked; drawGraph(pane.active); }
  });
  bar.querySelector("[data-graph-clear]").addEventListener("click", () => {
    if (pane.active) { pane.active.graph.series.clear(); drawGraph(pane.active); }
  });
  bar.querySelector("[data-graph-csv]").addEventListener("click", () => {
    if (pane.active) download(fileName(pane.active, "csv"), new Blob([graphCsv(pane.active)], { type: "text/csv" }));
  });
}

setInterval(() => { for (const pane of panes) if (pane.active && graphOpen(pane)) drawGraph(pane.active); }, 500);  // the time axis moves on
