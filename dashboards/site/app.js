"use strict";

/* Gas Storage Monitor: renders data/dashboard.json (written daily by dashboards/export.py).
   No chart library: one small TimeChart class draws every chart as SVG. */

const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function s(tag, attrs = {}, style = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  Object.assign(node.style, style);
  return node;
}

/* ---------- formatting ---------- */
const parseDate = (str) => new Date(`${str}T00:00:00Z`);
const fmtDate = (d, opts) => d.toLocaleDateString("en-US", { timeZone: "UTC", ...opts });
const fmtDay = (d) => fmtDate(d, { month: "short", day: "numeric", year: "numeric" });
const fmtShort = (d) => fmtDate(d, { month: "short", day: "numeric" });
const num = (v, digits = 0) =>
  v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const signed = (v, digits = 0) => {
  const r = Number(v.toFixed(digits));
  return (r > 0 ? "+" : r < 0 ? "−" : "") + num(Math.abs(r), digits);
};
const MODEL_NAMES = {
  baseline_5yr: "5-yr average (baseline)",
  ridge: "Ridge",
  xgboost: "XGBoost",
  baseline_persistence: "Persistence (baseline)",
  baseline_majority: "Majority class (baseline)",
  ridge_logistic: "Ridge logistic",
};

const ICONS = {
  check: "M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm3.2 5.3-3.8 4a.75.75 0 0 1-1.1 0L4.8 8.8a.75.75 0 1 1 1.1-1l1 1.1 3.2-3.4a.75.75 0 1 1 1.1 1z",
  warning: "M8.9 1.6a1 1 0 0 0-1.8 0L.6 13.4A1 1 0 0 0 1.5 15h13a1 1 0 0 0 .9-1.6zM8 5.5a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 8 5.5zm0 7.5a1 1 0 1 1 0-2 1 1 0 0 1 0 2z",
  critical: "M5.1 1h5.8L15 5.1v5.8L10.9 15H5.1L1 10.9V5.1zM8 4.5a.75.75 0 0 0-.75.75v3.5a.75.75 0 0 0 1.5 0v-3.5A.75.75 0 0 0 8 4.5zM8 12a1 1 0 1 0 0-2 1 1 0 0 0 0 2z",
};
function icon(name, color) {
  const svg = s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" });
  svg.append(s("path", { d: ICONS[name] }, color ? { fill: color } : {}));
  return svg;
}
function severity(level) {
  return h("span", { class: `sev ${level}` }, icon(level), level === "critical" ? "Critical" : "Warning");
}

/* ---------- theme ---------- */
function initTheme() {
  const root = document.documentElement;
  try {
    const saved = localStorage.getItem("theme");
    if (saved) root.dataset.theme = saved;
  } catch (_) { /* storage unavailable: follow the OS setting */ }
  $("#theme-toggle").addEventListener("click", () => {
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (_) { /* ignore */ }
  });
}

/* ---------- chart engine ---------- */
function niceStep(span, target) {
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  return (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
}

function linearTicks(lo, hi, target = 5) {
  const step = niceStep(hi - lo, target);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

const LOG_TICKS = [1, 1.5, 2, 3, 4, 5, 7, 10, 15, 20, 30, 50];

function timeTicks(t0, t1) {
  const years = (t1 - t0) / (365.25 * 864e5);
  const months = years > 6 ? 12 : years > 2.5 ? 6 : years > 1.2 ? 3 : 1;
  const ticks = [];
  const d = new Date(t0);
  let cursor = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
  while (cursor <= t1) {
    const c = new Date(cursor);
    if (c.getUTCMonth() % months === 0) ticks.push(cursor);
    cursor = Date.UTC(c.getUTCFullYear(), c.getUTCMonth() + 1, 1);
  }
  const label = (t) => {
    const c = new Date(t);
    if (months === 12 || c.getUTCMonth() === 0) return String(c.getUTCFullYear());
    return fmtDate(c, { month: "short" });
  };
  return ticks.map((t) => ({ t, label: label(t) }));
}

class TimeChart {
  /* cfg.series: [{ id, label, type: "line"|"band"|"dots"|"markers", color, width, points, format }]
     points are { t: ms, y } (lines/dots/markers) or { t, lo, hi } (band). */
  constructor(card, cfg) {
    this.card = card;
    this.cfg = { height: 300, yFormat: (v) => num(v), ...cfg };
    this.host = $("[data-chart]", card);
    this.start = null;
    this.renderLegend();
    new ResizeObserver(() => this.render()).observe(this.host);
  }

  setStart(start) {
    this.start = start;
    this.render();
    this.renderTable();
  }

  visible(points) {
    return this.start == null ? points : points.filter((p) => p.t >= this.start);
  }

  renderLegend() {
    const legend = $("[data-legend]", this.card);
    const items = this.cfg.series.filter((sr) => !sr.hideLegend);
    if (items.length < 2) return;
    legend.replaceChildren(...items.map((sr) => {
      const cls = sr.type === "band" ? "swatch-band" : sr.type === "line" ? "swatch-line" : "swatch-dot";
      const bg = sr.type === "band" ? "var(--band)" : sr.color;
      return h("span", { class: "key" }, h("span", { class: cls, style: { background: bg } }), sr.label);
    }));
  }

  render() {
    const width = this.host.clientWidth;
    if (!width) return;
    const cfg = this.cfg;
    const height = width < 520 ? Math.round(cfg.height * 0.8) : cfg.height;
    const m = { top: 10, right: cfg.endLabels ? 70 : 14, bottom: 26, left: 46 };
    const iw = width - m.left - m.right;
    const ih = height - m.top - m.bottom;

    const series = cfg.series.map((sr) => ({ ...sr, pts: this.visible(sr.points) }));
    const all = series.flatMap((sr) => sr.pts);
    if (!all.length) return;
    const t0 = this.start ?? Math.min(...all.map((p) => p.t));
    const t1 = Math.max(...all.map((p) => p.t));

    const ys = all.flatMap((p) => ("lo" in p ? [p.lo, p.hi] : [p.y])).filter((v) => v != null);
    let y0 = Math.min(...ys);
    let y1 = Math.max(...ys);
    if (cfg.zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
    const log = cfg.log;
    const tx = (v) => (log ? Math.log(v) : v);
    if (!log) {
      const pad = (y1 - y0) * 0.06 || 1;
      y0 -= cfg.zero && y0 === 0 ? 0 : pad;
      y1 += pad;
    }
    const yTicks = log ? LOG_TICKS.filter((v) => v >= y0 * 0.95 && v <= y1 * 1.05)
                       : linearTicks(y0, y1, width < 380 ? 4 : 6);
    if (!log) { y0 = Math.min(y0, yTicks[0]); y1 = Math.max(y1, yTicks[yTicks.length - 1]); }

    const x = (t) => m.left + ((t - t0) / (t1 - t0 || 1)) * iw;
    const y = (v) => m.top + ih - ((tx(v) - tx(y0)) / (tx(y1) - tx(y0) || 1)) * ih;

    const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img",
                           "aria-label": cfg.ariaLabel });
    // grid + axes
    for (const v of yTicks) {
      const isZero = !log && v === 0;
      svg.append(s("line", { x1: m.left, x2: m.left + iw, y1: y(v), y2: y(v) },
                   { stroke: isZero ? "var(--axis)" : "var(--grid)", strokeWidth: 1 }));
      svg.append(Object.assign(s("text", { x: m.left - 8, y: y(v) + 4, "text-anchor": "end" }),
                               { textContent: cfg.yFormat(v) }));
    }
    for (const tick of timeTicks(t0, t1)) {
      if (x(tick.t) < m.left + 14 || x(tick.t) > m.left + iw - 14) continue;
      svg.append(Object.assign(s("text", { x: x(tick.t), y: height - 6, "text-anchor": "middle" }),
                               { textContent: tick.label }));
    }

    // marks
    for (const sr of series) {
      if (sr.type === "band") {
        const pts = sr.pts.filter((p) => p.lo != null);
        if (!pts.length) continue;
        const top = pts.map((p) => `${x(p.t)},${y(p.hi)}`);
        const bottom = pts.slice().reverse().map((p) => `${x(p.t)},${y(p.lo)}`);
        svg.append(s("path", { d: `M${top.join("L")}L${bottom.join("L")}Z` }, { fill: "var(--band)" }));
      } else if (sr.type === "line") {
        let d = "";
        let pen = false;
        for (const p of sr.pts) {
          if (p.y == null) { pen = false; continue; }
          d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`;
          pen = true;
        }
        svg.append(s("path", { d }, {
          fill: "none", stroke: sr.color, strokeWidth: sr.width ?? 2,
          strokeLinejoin: "round", strokeLinecap: "round",
        }));
      } else {
        for (const p of sr.pts) {
          if (p.y == null) continue;
          svg.append(s("circle", { cx: x(p.t), cy: y(p.y), r: sr.r ?? 4.5 }, {
            fill: p.color ?? sr.color, stroke: "var(--surface)", strokeWidth: 2,
          }));
        }
      }
    }

    // selective end labels (dropped rather than stacked if they would collide)
    const placed = [];
    for (const sr of series.filter((item) => item.endLabel)) {
      const last = [...sr.pts].reverse().find((p) => p.y != null);
      if (!last) continue;
      const ly = y(last.y);
      if (placed.some((py) => Math.abs(py - ly) < 14)) continue;
      placed.push(ly);
      svg.append(Object.assign(
        s("text", { x: x(last.t) + 8, y: ly + 4, class: "end-label" }),
        { textContent: sr.endLabel(last.y) },
      ));
    }

    this.attachHover(svg, series, { x, y, m, iw, ih, height });
    this.host.replaceChildren(svg);
  }

  attachHover(svg, series, g) {
    const lookup = series.map((sr) => ({ sr, map: new Map(sr.pts.map((p) => [p.t, p])) }));
    const times = [...new Set(series.flatMap((sr) => sr.pts.map((p) => p.t)))].sort((a, b) => a - b);
    const cross = s("line", { y1: g.m.top, y2: g.m.top + g.ih }, {
      stroke: "var(--axis)", strokeWidth: 1, visibility: "hidden" });
    const focus = s("g", {}, { visibility: "hidden" });
    const overlay = s("rect", { x: g.m.left, y: g.m.top, width: g.iw, height: g.ih,
                                class: "overlay", tabindex: 0,
                                "aria-label": `${this.cfg.ariaLabel}. Use arrow keys to read values.` });
    svg.append(cross, focus, overlay);
    const tooltip = $("#tooltip");
    let index = times.length - 1;

    const show = (i, clientX, clientY) => {
      index = Math.max(0, Math.min(times.length - 1, i));
      const t = times[index];
      const cx = g.x(t);
      cross.setAttribute("x1", cx);
      cross.setAttribute("x2", cx);
      cross.style.visibility = "visible";
      focus.replaceChildren();
      const rows = [];
      for (const { sr, map } of lookup) {
        const p = map.get(t);
        if (!p) continue;
        if (sr.type === "band") {
          if (p.lo == null) continue;
          rows.push([sr.label, "var(--band)", `${this.cfg.yFormat(p.lo)}–${this.cfg.yFormat(p.hi)}`]);
          continue;
        }
        if (p.y == null) continue;
        focus.append(s("circle", { cx, cy: g.y(p.y), r: 4.5 }, {
          fill: p.color ?? sr.color, stroke: "var(--surface)", strokeWidth: 2 }));
        rows.push([sr.label, p.color ?? sr.color, (sr.format ?? this.cfg.yFormat)(p.y, p)]);
      }
      focus.style.visibility = "visible";
      tooltip.replaceChildren(
        h("div", { class: "t-date" }, (this.cfg.dateLabel ?? fmtDay)(new Date(t))),
        ...rows.map(([name, color, value]) => h("div", { class: "t-row" },
          h("span", { class: "t-name" }, h("span", { class: "t-key", style: { background: color } }), name),
          h("span", { class: "t-val" }, value))),
      );
      tooltip.hidden = false;
      const rect = svg.getBoundingClientRect();
      const px = clientX ?? rect.left + (cx / svg.viewBox.baseVal.width) * rect.width;
      const py = clientY ?? rect.top + g.m.top;
      const tw = tooltip.offsetWidth;
      tooltip.style.left = `${px + 16 + tw > innerWidth ? px - tw - 16 : px + 16}px`;
      tooltip.style.top = `${Math.max(8, py - 12)}px`;
    };
    const hide = () => {
      tooltip.hidden = true;
      cross.style.visibility = "hidden";
      focus.style.visibility = "hidden";
    };
    const nearest = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const vx = ((clientX - rect.left) / rect.width) * svg.viewBox.baseVal.width;
      let lo = 0;
      let hi = times.length - 1;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (g.x(times[mid]) < vx) lo = mid; else hi = mid;
      }
      return Math.abs(g.x(times[lo]) - vx) <= Math.abs(g.x(times[hi]) - vx) ? lo : hi;
    };
    overlay.addEventListener("pointermove", (e) => show(nearest(e.clientX), e.clientX, e.clientY));
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("focus", () => show(index));
    overlay.addEventListener("blur", hide);
    overlay.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -times.length, End: times.length }[e.key];
      if (step) { e.preventDefault(); show(index + step); }
      if (e.key === "Escape") hide();
    });
  }

  renderTable() {
    const { columns, rows } = this.cfg.table;
    const data = this.visible(rows).slice().reverse();
    const table = h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
      h("tbody", {}, data.map((r) => h("tr", {},
        columns.map((c) => h("td", { class: c.num ? "num" : "" }, c.get(r)))))));
    $("[data-table]", this.card).replaceChildren(h("div", { class: "table-scroll" }, table));
  }
}

/* ---------- page sections ---------- */
function renderStatus(data) {
  const { dq, dq_checked_at: checked, data_through: through } = data.health;
  const total = dq.pass + dq.warn + dq.fail;
  const state = dq.fail ? "fail" : dq.warn ? "warn" : "";
  const dqText = dq.fail ? `${dq.fail} data-quality check${dq.fail > 1 ? "s" : ""} failing`
    : dq.warn ? `${dq.pass}/${total} data-quality checks passed, ${dq.warn} warning`
    : `${dq.pass}/${total} data-quality checks passed`;
  $("#status-row").replaceChildren(
    h("span", { class: "item" }, h("span", { class: `status-dot ${state}` }), dqText),
    h("span", { class: "item" }, `Storage through ${fmtShort(parseDate(through.storage))}`),
    h("span", { class: "item" }, `Henry Hub through ${fmtShort(parseDate(through.price))}`),
    h("span", { class: "item" }, `Weather through ${fmtShort(parseDate(through.weather))}`),
    h("span", { class: "item" }, `Updated ${data.generated_at}`),
  );
  $("#generated").textContent = checked ? `Last data-quality run ${checked}` : "";
}

function renderHero(data) {
  const hero = $("#hero");
  const f = data.latest?.storage;
  if (!f) {
    hero.replaceChildren(h("p", { class: "eyebrow" }, "No live forecast yet."));
    return;
  }
  const week = parseDate(f.week_ending);
  const release = parseDate(f.release_date);
  const T = f.threshold;
  const dev = f.deviation;
  const side = dev > 0 ? "looser" : "tighter";
  const kind = f.forecast >= 0 ? "injection" : "withdrawal";
  const pos = ((Math.max(-2 * T, Math.min(2 * T, dev)) + 2 * T) / (4 * T)) * 100;
  const alerting = Math.abs(dev) > T;

  hero.replaceChildren(
    h("div", { class: "eyebrow" },
      `Next EIA storage report · week ending ${fmtShort(week)} · released ${fmtDate(release, { weekday: "short", month: "short", day: "numeric" })}, 10:30 ET`),
    h("div", { class: "hero-figure" }, signed(f.forecast), h("span", { class: "unit" }, "Bcf")),
    h("p", { class: "hero-note" },
      `Forecast ${kind} (${MODEL_NAMES[f.model]}). The 5-year average for this week is `,
      h("strong", {}, `${signed(f.baseline)} Bcf`), ", so this week looks ",
      h("strong", {}, `${num(Math.abs(dev))} Bcf ${side}`), " than normal.",
      f.ridge != null ? ` Ridge model: ${signed(f.ridge)} Bcf.` : ""),
    h("div", { class: "meter", role: "img",
               "aria-label": `Deviation from 5-year average: ${signed(dev)} Bcf; alert threshold ±${T} Bcf` },
      h("div", { class: "meter-track" },
        h("span", { class: "meter-zone alert", style: { left: "0%", width: "25%" } }),
        h("span", { class: "meter-zone calm", style: { left: "25%", width: "50%" } }),
        h("span", { class: "meter-zone alert", style: { left: "75%", width: "25%" } }),
        ...[25, 50, 75].map((p) => h("span", { class: "meter-tick", style: { left: `${p}%` } })),
        h("span", { class: "meter-dot", style: { left: `${pos}%` } })),
      h("div", { class: "meter-scale" },
        h("span", {}, `${signed(-2 * T)} tighter`), h("span", {}, signed(-T)),
        h("span", {}, "5-yr avg"), h("span", {}, signed(T)), h("span", {}, `${signed(2 * T)} looser`))),
    h("span", { class: "chip" },
      alerting ? icon("warning", "var(--warning)") : icon("check", "var(--good)"),
      alerting ? `Alert: ${num(Math.abs(dev))} Bcf ${side} than normal (threshold ±${T} Bcf)`
               : `Within ±${T} Bcf of normal, so no alert`),
  );
}

function tile(label, value, unit, delta, foot) {
  return h("article", { class: "card tile" },
    h("span", { class: "label" }, label),
    h("span", { class: "value" }, value, unit ? h("span", { class: "unit" }, unit) : null),
    h("span", { class: "delta" }, ...delta),
    foot ? h("span", { class: "foot" }, foot) : null);
}

function renderTiles(data) {
  const last = data.storage[data.storage.length - 1];
  const vs = last.level - last.avg5;
  const prices = data.prices;
  const p = prices[prices.length - 1];
  const xgb = data.backtest.storage.find((r) => r.model === "xgboost");
  const base = data.backtest.storage.find((r) => r.model === "baseline_5yr");
  const dirModel = data.latest?.direction?.model;
  const dirBt = data.backtest.direction.find((r) => r.model === dirModel);
  const probUp = data.latest?.direction?.prob_up;

  $("#tiles").replaceChildren(
    tile("Working gas in storage", num(last.level), "Bcf",
      [h("strong", {}, `${signed(vs)} Bcf`), ` (${signed((100 * vs) / last.avg5, 1)}%) vs 5-yr average`],
      `Week ending ${fmtShort(parseDate(last.week_ending))} · net change ${signed(last.change)} Bcf`),
    tile("Henry Hub spot", `$${num(p.price, 2)}`, "/MMBtu",
      [h("strong", {}, `${signed(p.dod_pct, 1)}%`), " day over day"],
      `${fmtDay(parseDate(p.price_date))} · EIA publishes with ~1 week lag`),
    tile("Storage forecast error", num(xgb.mae, 1), "Bcf MAE",
      [h("strong", {}, `${signed(xgb.mae_vs_baseline_pct)}%`), ` vs 5-yr average baseline (${num(base.mae, 1)})`],
      `Walk-forward backtest, ${xgb.n} weeks since 2020`),
    tile("Henry Hub direction, next 7 days",
      probUp == null ? "—" : `${probUp >= 0.5 ? "Up" : "Down"} · ${num(100 * Math.max(probUp, 1 - probUp))}%`, "",
      [h("span", { class: "caveat" },
        `No demonstrated skill: ${num(100 * dirBt.accuracy, 1)}% backtest accuracy (p = ${num(dirBt.p_vs_coin, 2)} vs a coin flip). Shown for transparency, never alerted on.`)],
      null),
  );
}

function renderBacktest(data) {
  const fc = data.forecasts.filter((r) => r.source === "backtest");
  $("#backtest-sub").textContent =
    `${num(fc.length)} weekly reports, weeks ending ${fmtDay(parseDate(fc[0].week_ending))} to ` +
    `${fmtDay(parseDate(fc[fc.length - 1].week_ending))}. Models retrained before every forecast on data published by then.`;
  const best = Math.min(...data.backtest.storage.map((r) => r.mae));
  const storage = h("table", {},
    h("thead", {}, h("tr", {}, ["Storage change", "Weeks", "MAE", "RMSE", "vs baseline", "DM p"]
      .map((c, i) => h("th", { class: i ? "num" : "" }, c)))),
    h("tbody", {}, data.backtest.storage.map((r) => h("tr", { class: r.mae === best ? "best" : "" },
      h("td", {}, MODEL_NAMES[r.model] ?? r.model),
      h("td", { class: "num" }, num(r.n)),
      h("td", { class: "num" }, num(r.mae, 1)),
      h("td", { class: "num" }, num(r.rmse, 1)),
      h("td", { class: "num" }, r.model === "baseline_5yr" ? "—" : `${signed(r.mae_vs_baseline_pct, 1)}%`),
      h("td", { class: "num" }, r.dm_p_value == null ? "—" : r.dm_p_value < 0.001 ? "<0.001" : num(r.dm_p_value, 3))))));
  const direction = h("table", {},
    h("thead", {}, h("tr", {}, ["Henry Hub direction", "Weeks", "Accuracy", "95% CI", "p vs 50%"]
      .map((c, i) => h("th", { class: i ? "num" : "" }, c)))),
    h("tbody", {}, data.backtest.direction.map((r) => h("tr", {},
      h("td", {}, MODEL_NAMES[r.model] ?? r.model),
      h("td", { class: "num" }, num(r.n)),
      h("td", { class: "num" }, `${num(100 * r.accuracy, 1)}%`),
      h("td", { class: "num" }, `${num(100 * r.ci_low)}–${num(100 * r.ci_high)}%`),
      h("td", { class: "num" }, num(r.p_vs_coin, 2))))));
  const anySkill = data.backtest.direction.some((r) => !r.model.startsWith("baseline") && r.p_vs_coin < 0.05 && r.accuracy > 0.5);
  $("#backtest").replaceChildren(
    h("div", { class: "table-scroll", style: { maxHeight: "none" } }, storage),
    h("p", { class: "table-note" },
      "Storage is driven by weather we can observe before the report, so it is forecastable: both models cut the baseline's error roughly in half, and the Diebold–Mariano test shows the gain is not luck."),
    h("div", { class: "table-scroll", style: { maxHeight: "none", marginTop: "14px" } }, direction),
    h("p", { class: "table-note" }, anySkill
      ? "At least one direction model beats a coin flip at the 5% level."
      : "Price direction is not forecastable from public, week-lagged data: no model beats a coin flip at the 5% level. The market prices in weather and storage information faster than this data arrives."),
  );
}

function renderAlerts(data, tab) {
  const t = data.thresholds;
  $("#alerts-sub").textContent =
    `Rules: storage forecast more than ±${t.storage_vs_5yr_bcf} Bcf from the 5-yr average, or Henry Hub spot moving more than ±${t.henry_hub_dod_pct}% in a day. Critical at ${t.critical_multiple}× the threshold. Each alert is emailed once.`;
  const rows = tab === "live" ? data.alerts : data.alert_replay.slice(0, 40);
  const note = tab === "live" ? null
    : h("p", { class: "table-note" },
        `What today's rules would have raised from backtest forecasts and price history since 2020: ${num(data.alert_replay.length)} alerts. Simulated, never sent; used to set the thresholds.`);
  if (!rows.length) {
    $("#alerts").replaceChildren(h("div", { class: "empty" },
      h("strong", {}, "No live alerts yet"),
      "Neither rule has fired since the daily pipeline went live. See the rule replay for how often they would have fired historically."));
    return;
  }
  const table = h("table", {},
    h("thead", {}, h("tr", {}, ["Date", "Severity", "Alert", tab === "live" ? "Email" : null]
      .filter(Boolean).map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {},
      h("td", { style: { whiteSpace: "nowrap" } }, fmtDay(parseDate(r.subject_date))),
      h("td", {}, severity(r.severity)),
      h("td", {}, r.message),
      tab === "live" ? h("td", {}, r.delivery_status) : null))));
  $("#alerts").replaceChildren(h("div", { class: "table-scroll" }, table), note);
}

/* ---------- charts ---------- */
function buildCharts(data) {
  const T = (str) => parseDate(str).getTime();
  const bcf = (v) => `${num(v)} Bcf`;
  const sBcf = (v) => `${signed(v)} Bcf`;
  const st = data.storage;

  const storage = new TimeChart($("#storage-card"), {
    ariaLabel: "Lower-48 working gas in storage against the 5-year range",
    yFormat: num,
    endLabels: true,
    dateLabel: (d) => `Week ending ${fmtDay(d)}`,
    series: [
      { id: "band", label: "5-yr range", type: "band", format: bcf,
        points: st.map((r) => ({ t: T(r.week_ending), lo: r.min5, hi: r.max5 })) },
      { id: "avg", label: "5-yr average", type: "line", color: "var(--reference)", width: 1.5,
        format: bcf, points: st.map((r) => ({ t: T(r.week_ending), y: r.avg5 })) },
      { id: "level", label: "Working gas", type: "line", color: "var(--series-1)", format: bcf,
        endLabel: (v) => num(v), points: st.map((r) => ({ t: T(r.week_ending), y: r.level })) },
    ],
    table: {
      rows: st.map((r) => ({ ...r, t: T(r.week_ending) })),
      columns: [
        { label: "Week ending", get: (r) => fmtDay(parseDate(r.week_ending)) },
        { label: "Storage", num: true, get: (r) => num(r.level) },
        { label: "Change", num: true, get: (r) => (r.change == null ? "—" : signed(r.change)) },
        { label: "5-yr avg", num: true, get: (r) => (r.avg5 == null ? "—" : num(r.avg5)) },
        { label: "vs 5-yr", num: true, get: (r) => (r.avg5 == null ? "—" : signed(r.level - r.avg5)) },
      ],
    },
  });

  const fc = data.forecasts;
  const live = fc.filter((r) => r.source === "live" && r.actual == null);
  const forecast = new TimeChart($("#forecast-card"), {
    ariaLabel: "Weekly storage change: actual versus forecasts",
    yFormat: (v) => signed(v),
    zero: true,
    endLabels: true,
    dateLabel: (d) => `Week ending ${fmtDay(d)}`,
    series: [
      { id: "base", label: "5-yr average", type: "line", color: "var(--reference)", width: 1.5,
        format: sBcf, points: fc.map((r) => ({ t: T(r.week_ending), y: r.baseline_5yr })) },
      { id: "actual", label: "Actual (EIA)", type: "line", color: "var(--series-1)", format: sBcf,
        points: fc.map((r) => ({ t: T(r.week_ending), y: r.actual })) },
      { id: "xgb", label: "XGBoost forecast", type: "line", color: "var(--series-2)", format: sBcf,
        points: fc.map((r) => ({ t: T(r.week_ending), y: r.xgboost })) },
      { id: "next", label: "Next report (pending)", type: "dots", color: "var(--series-2)",
        format: sBcf, endLabel: (v) => `Next ${signed(v)}`,
        points: live.map((r) => ({ t: T(r.week_ending), y: r.xgboost })) },
    ],
    table: {
      rows: fc.map((r) => ({ ...r, t: T(r.week_ending) })),
      columns: [
        { label: "Week ending", get: (r) => fmtDay(parseDate(r.week_ending)) },
        { label: "Actual", num: true, get: (r) => (r.actual == null ? "pending" : signed(r.actual)) },
        { label: "XGBoost", num: true, get: (r) => signed(r.xgboost) },
        { label: "Ridge", num: true, get: (r) => signed(r.ridge) },
        { label: "5-yr avg", num: true, get: (r) => signed(r.baseline_5yr) },
        { label: "Source", get: (r) => r.source },
      ],
    },
  });

  const mae = data.rolling_mae;
  const error = new TimeChart($("#error-card"), {
    ariaLabel: "Trailing 52-week mean absolute error by model",
    yFormat: num,
    zero: true,
    endLabels: true,
    dateLabel: (d) => `52 weeks to ${fmtDay(d)}`,
    series: [
      { id: "base", label: "5-yr average", type: "line", color: "var(--reference)", width: 1.5,
        format: (v) => `${num(v, 1)} Bcf`, endLabel: (v) => num(v, 1),
        points: mae.map((r) => ({ t: T(r.week_ending), y: r.baseline_5yr })) },
      { id: "ridge", label: "Ridge", type: "line", color: "var(--series-3)",
        format: (v) => `${num(v, 1)} Bcf`,
        points: mae.map((r) => ({ t: T(r.week_ending), y: r.ridge })) },
      { id: "xgb", label: "XGBoost", type: "line", color: "var(--series-2)",
        format: (v) => `${num(v, 1)} Bcf`, endLabel: (v) => num(v, 1),
        points: mae.map((r) => ({ t: T(r.week_ending), y: r.xgboost })) },
    ],
    table: {
      rows: mae.map((r) => ({ ...r, t: T(r.week_ending) })),
      columns: [
        { label: "52 weeks to", get: (r) => fmtDay(parseDate(r.week_ending)) },
        { label: "5-yr avg", num: true, get: (r) => num(r.baseline_5yr, 1) },
        { label: "Ridge", num: true, get: (r) => num(r.ridge, 1) },
        { label: "XGBoost", num: true, get: (r) => num(r.xgboost, 1) },
      ],
    },
  });

  const th = data.thresholds.henry_hub_dod_pct;
  const crit = th * data.thresholds.critical_multiple;
  const px = data.prices;
  const moves = px.filter((r) => r.dod_pct != null && Math.abs(r.dod_pct) > th);
  $("#price-sub").textContent =
    `$/MMBtu, daily, log scale. Markers show day-over-day moves beyond ±${th}% (the alert rule); red beyond ±${crit}%.`;
  const price = new TimeChart($("#price-card"), {
    ariaLabel: "Henry Hub spot price with alert-level moves",
    log: true,
    yFormat: (v) => `$${num(v, v < 10 && v % 1 ? 1 : 0)}`,
    series: [
      { id: "price", label: "Henry Hub spot", type: "line", color: "var(--series-1)", width: 1.5,
        format: (v) => `$${num(v, 2)}`,
        points: px.map((r) => ({ t: T(r.price_date), y: r.price })) },
      { id: "warn", label: `Move > ±${th}%`, type: "markers", color: "var(--warning)", r: 4,
        format: (v, p) => `${signed(p.pct, 1)}% d/d`,
        points: moves.filter((r) => Math.abs(r.dod_pct) < crit)
          .map((r) => ({ t: T(r.price_date), y: r.price, pct: r.dod_pct })) },
      { id: "crit", label: `Move > ±${crit}%`, type: "markers", color: "var(--critical)", r: 4,
        format: (v, p) => `${signed(p.pct, 1)}% d/d`,
        points: moves.filter((r) => Math.abs(r.dod_pct) >= crit)
          .map((r) => ({ t: T(r.price_date), y: r.price, pct: r.dod_pct })) },
    ],
    table: {
      rows: px.map((r) => ({ ...r, t: T(r.price_date) })),
      columns: [
        { label: "Date", get: (r) => fmtDay(parseDate(r.price_date)) },
        { label: "$/MMBtu", num: true, get: (r) => num(r.price, 2) },
        { label: "Day over day", num: true, get: (r) => (r.dod_pct == null ? "—" : `${signed(r.dod_pct, 1)}%`) },
      ],
    },
  });

  return [storage, forecast, error, price];
}

function initRange(charts, data) {
  const latest = Math.max(...data.storage.map((r) => parseDate(r.week_ending).getTime()));
  const buttons = [...document.querySelectorAll("#range button")];
  const apply = (value) => {
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === value)));
    const start = value === "all" ? null
      : Date.UTC(new Date(latest).getUTCFullYear() - Number(value), new Date(latest).getUTCMonth(),
                 new Date(latest).getUTCDate());
    charts.forEach((c) => c.setStart(start));
  };
  buttons.forEach((b) => b.addEventListener("click", () => apply(b.dataset.range)));
  apply("3");
}

function initAlertTabs(data) {
  const tabs = [...document.querySelectorAll("#alert-tabs button")];
  const select = (name) => {
    tabs.forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    renderAlerts(data, name);
  };
  tabs.forEach((b) => b.addEventListener("click", () => select(b.dataset.tab)));
  select("live");
}

async function main() {
  initTheme();
  let data;
  try {
    const resp = await fetch("data/dashboard.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    $("main").prepend(h("div", { class: "card empty" }, h("strong", {}, "Data unavailable"),
      `Could not load data/dashboard.json (${err.message}).`));
    return;
  }
  renderStatus(data);
  renderHero(data);
  renderTiles(data);
  renderBacktest(data);
  initAlertTabs(data);
  initRange(buildCharts(data), data);
}

main();
