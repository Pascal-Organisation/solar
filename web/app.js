"use strict";

// ------------------------------------------------------------------ helpers

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const LOCALE = "de-DE";
const TZ = "Europe/Berlin";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function svg(tag, attrs = {}, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

const num = (v, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "–"
    : Number(v).toLocaleString(LOCALE, { minimumFractionDigits: 0, maximumFractionDigits: digits });

function kw(watts, digits = 2) {
  if (watts === null || watts === undefined) return "–";
  return num(watts / 1000, Math.abs(watts) >= 10000 ? 1 : digits);
}

function power(watts) {
  if (watts === null || watts === undefined) return "–";
  return Math.abs(watts) >= 1000 ? `${kw(watts)} kW` : `${num(watts, 0)} W`;
}

function kwh(v) {
  if (v === null || v === undefined) return "–";
  return num(v, Math.abs(v) >= 100 ? 0 : 1);
}

const eurFmt = new Intl.NumberFormat(LOCALE, { style: "currency", currency: "EUR" });
const eur = (v) => (v === null || v === undefined ? "–" : eurFmt.format(v));
const hasTariff = () => tariff.import_eur_kwh > 0 || tariff.export_eur_kwh > 0;
let tariff = { import_eur_kwh: 0, export_eur_kwh: 0 };

const pct = (ratio) => (ratio === null || ratio === undefined ? "–" : num(ratio * 100, 0));

const dateFmt = new Intl.DateTimeFormat(LOCALE, { timeZone: TZ, weekday: "short", day: "numeric", month: "short", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat(LOCALE, { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const dayKeyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

const todayKey = () => dayKeyFmt.format(new Date());
function shiftDay(key, days) {
  const d = new Date(`${key}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" }, credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, v) { try { localStorage.setItem(key, v); } catch { /* private mode */ } }

// --------------------------------------------------------------- series

const SERIES = {
  load: { label: "Verbrauch", cls: "load" },
  pv: { label: "PV", cls: "pv" },
  bat: { label: "Batterie", cls: "bat" },
  grid: { label: "Netz", cls: "grid" },
};

// ------------------------------------------------------------------ theme

function initTheme() {
  const saved = storageGet("theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  $("theme").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storageSet("theme", next);
  });
}

// ---------------------------------------------------------------- tooltip

const tooltip = {
  node: null,
  show(x, y, head, rows) {
    const t = this.node || (this.node = $("tooltip"));
    t.replaceChildren(el("div", { class: "tt-head" }, head));
    for (const r of rows) {
      const key = el("span", { class: r.cls ? `key-line bg-${r.cls}` : "" });
      t.append(el("div", { class: "tt-row" }, key, el("span", {}, r.label), el("strong", {}, r.value)));
    }
    t.hidden = false;
    const pad = 14;
    const w = t.offsetWidth, h = t.offsetHeight;
    let left = x + pad, top = y - h - pad;
    if (left + w > innerWidth - 8) left = x - w - pad;
    if (top < 8) top = y + pad;
    t.style.left = `${Math.max(8, left)}px`;
    t.style.top = `${top}px`;
  },
  hide() { if (this.node) this.node.hidden = true; },
};

// ------------------------------------------------------------- scales

function niceTicks(min, max, count = 5) {
  if (min === max) max = min + 1;
  const span = max - min;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return { lo, hi, ticks };
}

// ----------------------------------------------------------- line chart

/**
 * opts: { rows: [{ts, ...}], x0, x1, series: [{key, cls, label, area}], hidden: Set,
 *         format: v => string, fixed: [min, max] | null, xTicks: [ts], xLabel: ts => string,
 *         gap: seconds that break the line }
 */
function lineChart(container, opts) {
  const render = () => drawLineChart(container, opts);
  render();
  if (!container._ro) {
    container._ro = new ResizeObserver(() => container._render && container._render());
    container._ro.observe(container);
  }
  container._render = render;
}

function drawLineChart(container, o) {
  const W = container.clientWidth, H = container.clientHeight;
  if (!W || !H) return;
  const m = { l: 40, r: 10, t: 8, b: 22 };
  const visible = o.series.filter((s) => !o.hidden?.has(s.key));
  const rows = o.rows;

  let lo = Infinity, hi = -Infinity;
  for (const r of rows) for (const s of visible) {
    const v = r[s.key];
    if (v !== null && v !== undefined) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  }
  if (o.fixed) [lo, hi] = o.fixed;
  if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
  lo = Math.min(0, lo);
  const { lo: ylo, hi: yhi, ticks } = niceTicks(lo, o.fixed ? hi : hi * 1.04, H < 160 ? 2 : 4);

  const x = (ts) => m.l + ((ts - o.x0) / (o.x1 - o.x0)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - (v - ylo) / (yhi - ylo)) * (H - m.t - m.b);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.ariaLabel || "Diagramm" });
  const axis = svg("g", { class: "axis" }, root);
  for (const t of ticks) {
    svg("line", { class: t === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }, axis);
    const label = svg("text", { x: m.l - 6, y: y(t) + 3.5, "text-anchor": "end" }, axis);
    label.textContent = o.format(t);
  }
  const step = W < 480 ? 2 : 1;
  o.xTicks.forEach((ts, i) => {
    if (i % step) return;
    const label = svg("text", { x: x(ts), y: H - 5, "text-anchor": "middle" }, axis);
    label.textContent = o.xLabel(ts);
  });

  // Paths broken at data gaps so missing hours are not drawn as straight lines.
  for (const s of visible) {
    let d = "", area = "", open = false, prevTs = null, segStart = null, lastX = null;
    const closeArea = () => { if (open && s.area) area += `L${lastX},${y(0)}L${segStart},${y(0)}Z`; };
    for (const r of rows) {
      const v = r[s.key];
      if (v === null || v === undefined || (prevTs !== null && r.ts - prevTs > o.gap)) { closeArea(); open = false; }
      if (v === null || v === undefined) { prevTs = r.ts; continue; }
      const px = x(r.ts).toFixed(1), py = y(v).toFixed(1);
      if (!open) { d += `M${px},${py}`; if (s.area) area += `M${px},${y(0)}L${px},${py}`; segStart = px; open = true; }
      else { d += `L${px},${py}`; if (s.area) area += `L${px},${py}`; }
      lastX = px; prevTs = r.ts;
    }
    closeArea();
    if (s.area && area) svg("path", { class: `series-area f-${s.cls}`, d: area }, root);
    svg("path", { class: `series-line s-${s.cls}`, d }, root);
  }

  // Hover layer: crosshair snaps to the nearest sample; one tooltip lists every series.
  const hover = svg("g", { visibility: "hidden" }, root);
  const cross = svg("line", { class: "crosshair", y1: m.t, y2: H - m.b }, hover);
  const dots = visible.map((s) => svg("circle", { class: `hover-dot f-${s.cls}`, r: 4 }, hover));
  const overlay = svg("rect", {
    x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent", tabindex: rows.length ? 0 : -1,
    "aria-label": "Werte mit Pfeiltasten durchgehen",
  }, root);
  let index = -1;
  const showAt = (i, clientX, clientY) => {
    if (i < 0 || i >= rows.length) return;
    index = i;
    const r = rows[i];
    const px = x(r.ts);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px);
    visible.forEach((s, k) => {
      const v = r[s.key];
      dots[k].setAttribute("visibility", v === null || v === undefined ? "hidden" : "visible");
      if (v !== null && v !== undefined) { dots[k].setAttribute("cx", px); dots[k].setAttribute("cy", y(v)); }
    });
    hover.setAttribute("visibility", "visible");
    const box = container.getBoundingClientRect();
    tooltip.show(clientX ?? box.left + px, clientY ?? box.top + y(hi) + 20, o.tipHead(r.ts),
      visible.map((s) => ({ cls: s.cls, label: s.label, value: o.tipFormat(r[s.key]) })));
  };
  const nearest = (clientX) => {
    const box = root.getBoundingClientRect();
    const ts = o.x0 + ((clientX - box.left - m.l) / (W - m.l - m.r)) * (o.x1 - o.x0);
    let lo2 = 0, hi2 = rows.length - 1;
    while (lo2 < hi2) { const mid = (lo2 + hi2) >> 1; if (rows[mid].ts < ts) lo2 = mid + 1; else hi2 = mid; }
    if (lo2 > 0 && Math.abs(rows[lo2 - 1].ts - ts) < Math.abs(rows[lo2].ts - ts)) lo2 -= 1;
    return lo2;
  };
  overlay.addEventListener("pointermove", (e) => rows.length && showAt(nearest(e.clientX), e.clientX, e.clientY));
  overlay.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tooltip.hide(); });
  overlay.addEventListener("blur", () => { hover.setAttribute("visibility", "hidden"); tooltip.hide(); });
  overlay.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    showAt(Math.max(0, Math.min(rows.length - 1, (index < 0 ? rows.length - 1 : index) + (e.key === "ArrowRight" ? 1 : -1))));
  });

  container.replaceChildren(root);
  if (!rows.length) container.append(el("div", { class: "empty" }, o.emptyText || "Keine Daten für diesen Tag"));
}

// ------------------------------------------------------------ bar chart

/** Grouped columns. opts: { cats: [{key, label, row}], series: [{key, cls, label}], tipHead, tipRows } */
function barChart(container, opts) {
  const render = () => drawBarChart(container, opts);
  render();
  if (!container._ro) {
    container._ro = new ResizeObserver(() => container._render && container._render());
    container._ro.observe(container);
  }
  container._render = render;
}

function roundedTop(x, y, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function drawBarChart(container, o) {
  const W = container.clientWidth, H = container.clientHeight;
  if (!W || !H) return;
  const m = { l: 44, r: 8, t: 8, b: 22 };
  let hi = 0;
  for (const c of o.cats) for (const s of o.series) hi = Math.max(hi, c.row[s.key] || 0);
  const { hi: yhi, ticks } = niceTicks(0, hi * 1.04 || 1, 4);
  const y = (v) => m.t + (1 - v / yhi) * (H - m.t - m.b);
  const band = (W - m.l - m.r) / Math.max(1, o.cats.length);
  const gap = 2;
  const barW = Math.max(2, Math.min(24, (band * 0.8 - gap * (o.series.length - 1)) / o.series.length));
  const groupW = barW * o.series.length + gap * (o.series.length - 1);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.ariaLabel || "Diagramm" });
  const axis = svg("g", { class: "axis" }, root);
  for (const t of ticks) {
    svg("line", { class: t === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }, axis);
    const label = svg("text", { x: m.l - 6, y: y(t) + 3.5, "text-anchor": "end" }, axis);
    label.textContent = num(t, 0);
  }
  const labelEvery = Math.max(1, Math.ceil(o.cats.length / Math.floor((W - m.l) / 44)));
  o.cats.forEach((c, i) => {
    const gx = m.l + i * band + (band - groupW) / 2;
    const g = svg("g", {}, root);
    o.series.forEach((s, k) => {
      const v = c.row[s.key] || 0;
      svg("path", { class: `bar f-${s.cls}`, d: roundedTop(gx + k * (barW + gap), y(v), barW, y(0) - y(v), 4) }, g);
    });
    if (i % labelEvery === 0) {
      const label = svg("text", { x: m.l + i * band + band / 2, y: H - 5, "text-anchor": "middle" }, axis);
      label.textContent = c.label;
    }
    // The whole band is the hit target, not just the painted bars.
    const hit = svg("rect", { x: m.l + i * band, y: m.t, width: band, height: H - m.t - m.b, fill: "transparent", tabindex: 0 }, g);
    const show = (cx, cy) => {
      g.classList.add("hover");
      for (const b of g.querySelectorAll(".bar")) b.classList.add("hover");
      tooltip.show(cx, cy, o.tipHead(c), o.tipRows(c.row));
    };
    const hide = () => { for (const b of g.querySelectorAll(".bar")) b.classList.remove("hover"); tooltip.hide(); };
    hit.addEventListener("pointermove", (e) => show(e.clientX, e.clientY));
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => { const b = hit.getBoundingClientRect(); show(b.left + b.width / 2, b.top + 20); });
    hit.addEventListener("blur", hide);
    if (o.onSelect) {
      hit.classList.add("selectable");
      hit.addEventListener("click", () => o.onSelect(c));
      hit.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); o.onSelect(c); }
      });
    }
  });
  container.replaceChildren(root);
  if (!o.cats.length) container.append(el("div", { class: "empty" }, "Noch keine Verlaufsdaten"));
}

function legend(container, series, hidden, onToggle, shape = "line") {
  container.replaceChildren(...series.map((s) => {
    const b = el("button", { type: "button", "aria-pressed": String(!hidden?.has(s.key)) },
      el("span", { class: `${shape === "line" ? "key-line" : "key-rect"} bg-${s.cls}` }), s.label);
    if (onToggle) b.addEventListener("click", () => onToggle(s.key));
    else b.disabled = true;
    return b;
  }));
}

function table(container, headers, rows) {
  const t = el("table", {}, el("thead", {}, el("tr", {}, ...headers.map((h) => el("th", {}, h)))));
  const body = el("tbody");
  for (const r of rows) body.append(el("tr", {}, ...r.map((v) => el("td", {}, v))));
  t.append(body);
  container.replaceChildren(el("div", { class: "table-wrap" }, t));
}

// ------------------------------------------------------------ energy flow

const ICONS = {
  pv: "M-11 4h22l-4-12h-14z M-7.5-8 -8.7 4 M0-8v12 M7.5-8 8.7 4 M-9.5-2h19",
  load: "M-11 0l11-10 11 10 M-8-2v11h16v-11 M-2.5 9v-6h5v6",
  bat: "M-11-6h20v12h-20z M9-2.5h3v5h-3 M-6-1.5v3 M-1.5-1.5v3 M3-1.5v3",
  grid: "M0-12-7 11 M0-12 7 11 M-4.5-4h9 M-6-.5h12 M-8.5 6h17 M-3 4 4.5-4",
};

function buildFlow() {
  const root = $("flow");
  const hub = { x: 200, y: 150 };
  const nodes = {
    pv: { x: 200, y: 46, label: "PV", tx: 246, ty: 42, anchor: "start" },
    load: { x: 344, y: 150, label: "Haus", tx: 344, ty: 205 },
    bat: { x: 200, y: 254, label: "Batterie", tx: 246, ty: 250, anchor: "start" },
    grid: { x: 56, y: 150, label: "Netz", tx: 56, ty: 205 },
  };
  const parts = {};
  for (const [key, n] of Object.entries(nodes)) {
    const d = `M${n.x},${n.y}L${hub.x},${hub.y}`;
    svg("path", { class: "flow-line", d }, root);
    parts[key] = { dash: svg("path", { class: `flow-dash k-${key}`, d }, root) };
  }
  svg("circle", { class: "hub", cx: hub.x, cy: hub.y, r: 7 }, root);
  for (const [key, n] of Object.entries(nodes)) {
    const g = svg("g", { transform: `translate(${n.x},${n.y})` }, root);
    svg("circle", { class: `node-ring k-${key}`, r: 28 }, g);
    svg("path", { class: `node-icon k-${key}`, d: ICONS[key] }, g);
    const value = svg("text", { class: "node-value", x: n.tx, y: n.ty, "text-anchor": n.anchor || "middle" }, root);
    const label = svg("text", { class: "node-label", x: n.tx, y: n.ty + 16, "text-anchor": n.anchor || "middle" }, root);
    label.textContent = n.label;
    parts[key].value = value;
    parts[key].label = label;
  }
  return parts;
}

let flowParts = null;
function updateFlow(v) {
  flowParts ||= buildFlow();
  const set = (key, watts, toHub, text, label) => {
    const p = flowParts[key];
    const active = Math.abs(watts || 0) >= 20;
    p.dash.classList.toggle("on", active);
    p.dash.classList.toggle("rev", active && !toHub);
    p.value.textContent = text;
    if (label) p.label.textContent = label;
  };
  set("pv", v.pv_w, true, power(v.pv_w));
  set("load", v.load_w, false, power(v.load_w));
  const bat = v.battery_w || 0;
  set("bat", bat, bat > 0, `${power(Math.abs(bat))} · ${num(v.soc, 0)} %`,
    Math.abs(bat) < 20 ? "Batterie" : bat > 0 ? "Batterie entlädt" : "Batterie lädt");
  const grid = v.grid_w || 0;
  set("grid", grid, grid < 0, power(Math.abs(grid)),
    Math.abs(grid) < 20 ? "Netz" : grid > 0 ? "Einspeisung" : "Netzbezug");
  $("flow-title").textContent =
    `Energiefluss: PV ${power(v.pv_w)}, Haus ${power(v.load_w)}, Batterie ${power(bat)}, Netz ${power(grid)}`;
}

// ------------------------------------------------------------------ live

let lastLive = null;
let ratedPower = 8000;

function tile(label, value, unit, detail, extra) {
  const children = [el("p", { class: "label" }, label),
    el("p", { class: "value" }, value, el("span", { class: "unit" }, unit))];
  if (detail) children.push(el("p", { class: "detail" }, detail));
  if (extra) children.push(extra);
  return el("div", { class: "card tile" }, ...children);
}

function savingsTile(label, t) {
  const v = t?.savings_eur;
  return tile(label, v === null || v === undefined ? "–" : num(v, 2), "€",
    t ? `eingespart ${eur(t.avoided_eur)} · Einspeisung ${eur(t.feed_in_eur)}` : null);
}

function meter(ratio, cls = "") {
  const fill = el("span");
  fill.style.width = `${Math.max(0, Math.min(1, ratio || 0)) * 100}%`;
  return el("div", { class: `meter ${cls}`, role: "presentation" }, fill);
}

function kv(container, pairs) {
  const nodes = [];
  for (const [k, v, cls] of pairs) {
    nodes.push(el("dt", {}, k));
    nodes.push(el("dd", cls ? { class: cls } : {}, v ?? "–"));
  }
  container.replaceChildren(...nodes);
}

function renderLive(p) {
  lastLive = p;
  const v = p.values;
  $("hero-pv").textContent = kw(v.pv_w);
  $("hero-sub").textContent = `Haus ${power(v.load_w)} · Batterie ${num(v.soc, 0)} %`;
  const perString = ratedPower / 2;
  $("strings").replaceChildren(...[["PV1", v.pv1_w, v.pv1_v], ["PV2", v.pv2_w, v.pv2_v]].map(([n, w, volt]) =>
    el("div", { class: "string-row" }, el("span", { class: "name" }, n), meter((w || 0) / perString),
      el("span", { class: "val" }, w ? `${power(w)} · ${num(volt, 0)} V` : "aus"))));
  updateFlow(v);

  const t = p.today || {};
  $("tiles").replaceChildren(
    tile("PV-Ertrag", kwh(t.pv_kwh), "kWh", t.pv_peak_w ? `Spitze ${power(t.pv_peak_w)}` : null),
    tile("Verbrauch", kwh(t.load_kwh), "kWh"),
    tile("Autarkie", pct(t.self_sufficiency), "%", "Anteil ohne Netzbezug"),
    tile("Eigenverbrauch", pct(t.self_consumption), "%", "Anteil PV selbst genutzt"),
    tile("Einspeisung", kwh(t.export_kwh), "kWh", hasTariff() ? `+${eur(t.feed_in_eur)} Vergütung` : null),
    tile("Netzbezug", kwh(t.import_kwh), "kWh", hasTariff() ? `${eur(t.import_kwh * tariff.import_eur_kwh)} Kosten` : null),
    tile("Batterie", num(v.soc, 0), "%", `geladen ${kwh(t.charge_kwh)} · entladen ${kwh(t.discharge_kwh)} kWh`,
      meter((v.soc || 0) / 100, "bat")),
    ...(hasTariff() ? [savingsTile("Ersparnis heute", t)] : []),
  );
  renderAllSensors(p);

  const s = p.status || {};
  kv($("sys-pv"), [
    ["PV1", v.pv1_w ? `${power(v.pv1_w)} · ${num(v.pv1_v, 0)} V · ${num(v.pv1_a, 1)} A` : (s.pv1_mode_label || "aus"),
      v.pv1_w ? null : "status-warn"],
    ["PV2", v.pv2_w ? `${power(v.pv2_w)} · ${num(v.pv2_v, 0)} V · ${num(v.pv2_a, 1)} A` : (s.pv2_mode_label || "aus"),
      v.pv2_w ? null : "status-warn"],
  ]);
  kv($("sys-grid"), [
    ["Leistung", power(v.grid_w), null],
    ["L1 / L2 / L3", `${power(v.grid_l1_w)} / ${power(v.grid_l2_w)} / ${power(v.grid_l3_w)}`],
    ["Spannung", `${num(v.grid_l1_v, 0)} / ${num(v.grid_l2_v, 0)} / ${num(v.grid_l3_v, 0)} V`],
    ["Frequenz", `${num(v.grid_hz, 2)} Hz`],
    ["Status", s.grid_mode_label],
  ]);
  kv($("sys-bat"), [
    ["Ladestand", `${num(v.soc, 0)} %`],
    ["Gesundheit (SoH)", `${num(v.soh, 0)} %`],
    ["Leistung", power(v.battery_w)],
    ["Spannung", `${num(v.battery_v, 1)} V`],
    ["Temperatur", `${num(v.battery_temp, 1)} °C`],
    ["Module", s.battery_modules],
    ["Modus", s.battery_mode_label],
  ]);
  const err = s.errors && String(s.errors).trim();
  kv($("sys-inv"), [
    ["Betriebsart", s.work_mode_label],
    ["Leistung AC", power(v.inverter_w)],
    ["Temperatur", `${num(v.inverter_temp, 1)} °C`],
    ["Backup-Last", power(v.backup_w)],
    ["Fehler", err || "keine", err ? "status-bad" : "status-ok"],
    ["WLAN-Signal", s.rssi !== undefined ? `${s.rssi} %` : "–"],
    ["Diagnose", s.diagnose_result_label || "–"],
  ]);
  updateLiveIndicator();
}

function updateLiveIndicator() {
  const live = $("live");
  if (!lastLive) return;
  const age = Math.max(0, Math.round(Date.now() / 1000 - lastLive.ts));
  const stale = age > 180;
  live.dataset.state = stale ? "stale" : "ok";
  $("live-text").textContent = stale
    ? `keine Daten seit ${age >= 3600 ? `${Math.round(age / 3600)} h` : `${Math.round(age / 60)} min`}`
    : `live · vor ${age} s`;
  live.title = `Letzte Messung ${timeFmt.format(new Date(lastLive.ts * 1000))}`;
}

function connectLive() {
  let fallback = null;
  const poll = async () => {
    try { renderLive(await api("/api/live")); } catch { $("live").dataset.state = "error"; $("live-text").textContent = "offline"; }
  };
  if (!("EventSource" in window)) { poll(); setInterval(poll, 10000); return; }
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => { try { renderLive(JSON.parse(e.data)); } catch { /* ignore malformed frame */ } };
  es.onerror = () => {
    $("live").dataset.state = "error";
    $("live-text").textContent = "verbinde neu …";
    if (!fallback) { poll(); fallback = setTimeout(() => { fallback = null; }, 10000); }
  };
  setInterval(updateLiveIndicator, 1000);
}

// ------------------------------------------------------------- day view

const dayState = { date: todayKey(), hidden: new Set(["bat", "grid"]), data: null };
const DAY_SERIES = [
  { key: "load", cls: "load", label: "Verbrauch" },
  { key: "pv", cls: "pv", label: "PV", area: true },
  { key: "bat", cls: "bat", label: "Batterie (+ entladen)" },
  { key: "grid", cls: "grid", label: "Netz (+ Einspeisung)" },
];

async function loadDay() {
  const chart = $("power-chart");
  chart.classList.add("loading");
  $("day-date").value = dayState.date;
  $("day-next").disabled = dayState.date >= todayKey();
  try {
    dayState.data = await api(`/api/day?date=${dayState.date}`);
  } catch {
    dayState.data = { series: [], start: 0, end: 1, totals: null };
  }
  chart.classList.remove("loading");
  renderDay();
}

function renderDay() {
  const d = dayState.data;
  const rows = d.series.map((r) => ({
    ts: r.ts, pv: r.pv_w, load: r.load_w, bat: r.battery_w, grid: r.grid_w, soc: r.soc, source: r.source,
  }));
  const xTicks = [];
  for (let h = 0; h <= 24; h += 3) xTicks.push(d.start + h * 3600);
  const common = {
    rows, x0: d.start, x1: d.end, xTicks, gap: 900,
    xLabel: (ts) => (ts >= d.end ? "24" : timeFmt.format(new Date(ts * 1000)).slice(0, 2)),
    tipHead: (ts) => `${timeFmt.format(new Date(ts * 1000))} Uhr`,
  };
  const toggle = (key) => {
    dayState.hidden.has(key) ? dayState.hidden.delete(key) : dayState.hidden.add(key);
    legend($("power-legend"), DAY_SERIES, dayState.hidden, toggle);
    renderDay();
  };
  legend($("power-legend"), DAY_SERIES, dayState.hidden, toggle);
  lineChart($("power-chart"), {
    ...common, series: DAY_SERIES, hidden: dayState.hidden, ariaLabel: "Leistungsverlauf des Tages",
    format: (v) => num(v / 1000, 1), tipFormat: (v) => power(v),
    emptyText: d.totals ? "Keine Leistungskurve für diesen Tag – Tageswerte siehe unten" : "Keine Daten für diesen Tag",
  });
  lineChart($("soc-chart"), {
    ...common, series: [{ key: "soc", cls: "bat", label: "Ladestand" }], fixed: [0, 100],
    ariaLabel: "Batterie-Ladestand", format: (v) => num(v, 0), tipFormat: (v) => `${num(v, 0)} %`,
    emptyText: " ",
  });
  const sems = rows.some((r) => r.source === "sems");
  const noCurve = !rows.length && d.totals;
  $("day-note").textContent = noCurve
    ? "Für diesen Tag gibt es nur Tageswerte (unten). GoodWe speichert Leistungskurven nur begrenzt – "
      + `verfügbar ab ${curvesFrom ? dateFmt.format(new Date(`${curvesFrom}T12:00:00Z`)) : "–"}.`
    : sems ? "Historische Daten aus dem SEMS-Portal importiert." : "";
  table($("power-table"), ["Zeit", "PV (W)", "Verbrauch (W)", "Batterie (W)", "Netz (W)", "SoC (%)"],
    rows.map((r) => [timeFmt.format(new Date(r.ts * 1000)), num(r.pv, 0), num(r.load, 0), num(r.bat, 0),
      num(r.grid, 0), num(r.soc, 0)]));

  const t = d.totals || {};
  $("day-totals").replaceChildren(
    tile("PV-Ertrag", kwh(t.pv_kwh), "kWh"),
    tile("Verbrauch", kwh(t.load_kwh), "kWh"),
    tile("Einspeisung", kwh(t.export_kwh), "kWh"),
    tile("Netzbezug", kwh(t.import_kwh), "kWh"),
    tile("Autarkie", pct(t.self_sufficiency), "%"),
    ...(hasTariff() ? [savingsTile("Ersparnis", d.totals)] : []),
  );
}

function initDayNav() {
  const go = (key) => { dayState.date = key > todayKey() ? todayKey() : key; loadDay(); };
  $("day-prev").addEventListener("click", () => go(shiftDay(dayState.date, -1)));
  $("day-next").addEventListener("click", () => go(shiftDay(dayState.date, 1)));
  $("day-today").addEventListener("click", () => go(todayKey()));
  $("day-date").max = todayKey();
  $("day-date").addEventListener("change", (e) => e.target.value && go(e.target.value));
  // Keep "today" fresh without a reload.
  setInterval(() => { if (dayState.date === todayKey()) loadDay(); }, 60000);
}

// ------------------------------------------------------------- history

const HIST_SERIES = [
  { key: "load_kwh", cls: "load", label: "Verbrauch" },
  { key: "pv_kwh", cls: "pv", label: "PV-Ertrag" },
];
const monthFmt = new Intl.DateTimeFormat(LOCALE, { month: "short", year: "2-digit", timeZone: "UTC" });
const monthShortFmt = new Intl.DateTimeFormat(LOCALE, { month: "short", timeZone: "UTC" });
const monthLongFmt = new Intl.DateTimeFormat(LOCALE, { month: "long", year: "numeric", timeZone: "UTC" });
// range: "days" | "month" | "year"; scope narrows it after a drill-down ("2025" or "2025-06").
const hist = { range: "days", scope: null };

function monthEnd(ym) {
  const d = new Date(`${ym}-01T12:00:00Z`);
  d.setUTCMonth(d.getUTCMonth() + 1, 0);
  return d.toISOString().slice(0, 10);
}

async function loadHistory() {
  const chart = $("hist-chart");
  chart.classList.add("loading");
  let cats = [];
  try {
    if (hist.range === "days") {
      const to = hist.scope ? monthEnd(hist.scope) : todayKey();
      const from = hist.scope ? `${hist.scope}-01` : shiftDay(to, -29);
      const { days } = await api(`/api/days?from=${from}&to=${to}`);
      const byDay = Object.fromEntries(days.map((d) => [d.day, d]));
      for (let k = from; k <= to && k <= todayKey(); k = shiftDay(k, 1)) {
        cats.push({ key: k, kind: "day", label: `${Number(k.slice(8))}.`,
          long: dateFmt.format(new Date(`${k}T12:00:00Z`)), row: byDay[k] || {} });
      }
    } else {
      const { periods } = await api(`/api/periods?by=${hist.range}`);
      let list = periods;
      if (hist.range === "month") {
        list = hist.scope ? periods.filter((p) => p.period.startsWith(hist.scope)) : periods.slice(-24);
      }
      cats = list.map((p) => {
        const d = new Date(`${p.period}${hist.range === "month" ? "-15" : "-07-01"}T00:00:00Z`);
        const label = hist.range === "year" ? p.period : hist.scope ? monthShortFmt.format(d) : monthFmt.format(d);
        return { key: p.period, kind: hist.range, row: p, label,
          long: hist.range === "month" ? monthLongFmt.format(d) : p.period };
      });
    }
  } catch { cats = []; }
  chart.classList.remove("loading");
  renderHistCrumb();
  legend($("hist-legend"), HIST_SERIES, null, null, "rect");
  barChart(chart, {
    cats, series: HIST_SERIES, ariaLabel: "Energie im Verlauf",
    tipHead: (c) => `${c.long} · ${c.kind === "day" ? "klicken: Tagesverlauf" : "klicken: Details"}`,
    tipRows: (r) => [
      { cls: "pv", label: "PV-Ertrag", value: `${kwh(r.pv_kwh)} kWh` },
      { cls: "load", label: "Verbrauch", value: `${kwh(r.load_kwh)} kWh` },
      { cls: "grid", label: "Einspeisung / Bezug", value: `${kwh(r.export_kwh)} / ${kwh(r.import_kwh)} kWh` },
      { cls: null, label: "Autarkie", value: `${pct(r.self_sufficiency)} %` },
      ...(hasTariff() ? [{ cls: null, label: "Ersparnis", value: eur(r.savings_eur) }] : []),
    ],
    onSelect: drillDown,
  });
  table($("hist-table"),
    ["Zeitraum", "PV (kWh)", "Verbrauch (kWh)", "Einspeisung (kWh)", "Bezug (kWh)", "Autarkie (%)",
      ...(hasTariff() ? ["Ersparnis (€)"] : [])],
    cats.map((c) => [c.long, kwh(c.row.pv_kwh), kwh(c.row.load_kwh), kwh(c.row.export_kwh), kwh(c.row.import_kwh),
      pct(c.row.self_sufficiency), ...(hasTariff() ? [num(c.row.savings_eur, 2)] : [])]));
}

// Year -> its months -> a month's days -> that day's curve.
function drillDown(c) {
  tooltip.hide();
  if (c.kind === "year") setHist("month", c.key);
  else if (c.kind === "month") setHist("days", c.key);
  else {
    dayState.date = c.key;
    loadDay();
    $("day-h").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function setHist(range, scope = null) {
  hist.range = range;
  hist.scope = scope;
  for (const o of document.querySelectorAll(".segmented button")) {
    o.setAttribute("aria-selected", String(o.dataset.range === range));
  }
  loadHistory();
}

function renderHistCrumb() {
  const crumb = $("hist-crumb");
  if (!hist.scope) { crumb.replaceChildren(); return; }
  const label = hist.scope.length === 4 ? hist.scope : monthLongFmt.format(new Date(`${hist.scope}-15T00:00:00Z`));
  const up = el("button", { type: "button", class: "chip", title: "Eine Ebene zurück" }, "× ", label);
  up.addEventListener("click", () => (hist.scope.length === 7 ? setHist("month", hist.scope.slice(0, 4)) : setHist(hist.range)));
  crumb.replaceChildren(up);
}

function initHistoryTabs() {
  for (const b of document.querySelectorAll(".segmented button")) {
    b.addEventListener("click", () => setHist(b.dataset.range));
  }
}

// -------------------------------------------------------------- summary

let curvesFrom = null;

async function loadSummary() {
  try {
    const s = await api("/api/summary");
    if (s.inverter?.model) $("inverter").textContent = `GoodWe ${s.inverter.model}`;
    const rated = Number(s.inverter?.rated_power);
    if (rated > 0) ratedPower = rated;
    if (s.tariff) tariff = s.tariff;
    curvesFrom = s.curves_from;
    renderTotals(s);
    const r = s.records || {};
    const day = (d) => (d ? dateFmt.format(new Date(`${d}T12:00:00Z`)) : "–");
    kv($("sys-rec"), [
      ["Bester Tag", r.best_day ? `${kwh(r.best_day.pv_kwh)} kWh` : "–"],
      ["am", day(r.best_day?.day)],
      ["Höchste Leistung", r.peak_power ? power(r.peak_power.pv_peak_w) : "–"],
      ["am", day(r.peak_power?.day)],
      ["Tageswerte seit", day(s.first_day)],
      ["Kurven seit", day(s.curves_from)],
    ]);
  } catch { /* summary is optional */ }
}

// All-time balance. The inverter's own lifetime counters are authoritative (they
// include everything since commissioning); the stored days are the fallback.
async function renderTotals(s) {
  const l = s.lifetime?.pv_kwh ? s.lifetime : s.totals;
  if (!l) return;
  const since = s.first_day ? dateFmt.format(new Date(`${s.first_day}T12:00:00Z`)) : null;
  $("total-sub").textContent = s.lifetime?.pv_kwh
    ? "seit Inbetriebnahme, laut Zählern des Wechselrichters"
    : (since ? `seit ${since}` : "");
  const priced = hasTariff();
  $("total-tiles").replaceChildren(
    tile("Erzeugt", kwh(l.pv_kwh), "kWh", "PV-Ertrag gesamt"),
    tile("Verbraucht", kwh(l.load_kwh), "kWh", "Hausverbrauch gesamt"),
    tile("Eigenverbrauch", kwh(l.self_consumed_kwh), "kWh", `${pct(l.self_consumption)} % des PV-Stroms selbst genutzt`),
    tile("Autarkie", pct(l.self_sufficiency), "%", "Verbrauch ohne Netzbezug"),
    tile("Einspeisung", kwh(l.export_kwh), "kWh", priced ? `${eur(l.feed_in_eur)} Vergütung` : null),
    tile("Netzbezug", kwh(l.import_kwh), "kWh", priced ? `${eur(l.import_kwh * tariff.import_eur_kwh)} bezahlt` : null),
    ...(priced ? [
      tile("Gespart durch Eigenverbrauch", num(l.avoided_eur, 0), "€", "nicht gekaufter Netzstrom"),
      tile("Einspeisevergütung", num(l.feed_in_eur, 0), "€", "für eingespeisten Strom"),
    ] : []),
  );
  const hero = $("total-hero");
  hero.hidden = !priced;
  if (!priced) return;
  $("total-value").textContent = num(l.savings_eur, 0);
  $("total-split").textContent = `${eur(l.avoided_eur)} Eigenverbrauch + ${eur(l.feed_in_eur)} Einspeisung`;
  try {
    const [months, years] = await Promise.all([api("/api/periods?by=month"), api("/api/periods?by=year")]);
    const now = todayKey();
    const month = months.periods.find((p) => p.period === now.slice(0, 7));
    const year = years.periods.find((p) => p.period === now.slice(0, 4));
    const full = years.periods.filter((p) => p.days >= 360);
    const perYear = full.length ? full.reduce((a, p) => a + (p.savings_eur || 0), 0) / full.length : null;
    kv($("total-periods"), [
      ["Dieses Jahr", eur(year?.savings_eur)],
      ["Dieser Monat", eur(month?.savings_eur)],
      ...(perYear ? [["Ø pro volles Jahr", eur(perYear)]] : []),
      ["Tarif", `${num(tariff.import_eur_kwh * 100, 1)} ct Bezug · ${num(tariff.export_eur_kwh * 100, 1)} ct Einspeisung`],
    ]);
  } catch { /* optional */ }
}

// Labels and units for every raw sensor, fetched once; values then stream in live.
let sensorMeta = null;
function renderAllSensors(p) {
  const details = document.querySelector(".all-sensors");
  if (!details.open || !p?.all) return;
  const meta = sensorMeta || {};
  table($("all-sensors"), ["Messwert", "Wert", "Einheit", "Kennung"],
    Object.entries(p.all).map(([k, v]) => {
      const [name, unit] = meta[k] || [k, ""];
      return [name || k, typeof v === "number" ? num(v, 3) : String(v ?? ""), unit || "", k];
    }));
  $("all-sensors-ts").textContent = `Stand ${timeFmt.format(new Date(p.ts * 1000))} · live`;
}

function initAllSensors() {
  const details = document.querySelector(".all-sensors");
  details.addEventListener("toggle", async () => {
    if (!details.open) return;
    try {
      const { ts, data, sensors } = await api("/api/details");
      sensorMeta = sensors || {};
      renderAllSensors(lastLive?.all ? lastLive : { ts, all: data });
    } catch {
      $("all-sensors").textContent = "Nicht verfügbar.";
    }
  });
}

// ------------------------------------------------------------------ boot

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initDayNav();
  initHistoryTabs();
  initAllSensors();
  loadSummary().then(connectLive);
  loadDay();
  loadHistory();
  setInterval(loadSummary, 300000);
});
