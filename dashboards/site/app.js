"use strict";

/* Gas Storage Monitor. Renders data/dashboard.json, written daily by dashboards/export.py.
   One small TimeChart class draws every chart as SVG; there is no chart library. */

const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
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
const DAY = 864e5;
const parseDate = (str) => new Date(`${str}T00:00:00Z`);
const T = (str) => parseDate(str).getTime();
const fmt = (d, opts) => d.toLocaleDateString("en-US", { timeZone: "UTC", ...opts });
const fmtDay = (d) => fmt(d, { month: "short", day: "numeric", year: "numeric" });
const fmtShort = (d) => fmt(d, { month: "short", day: "numeric" });
const fmtMonth = (d) => fmt(d, { month: "short", year: "numeric" });
const num = (v, digits = 0) =>
  v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const signed = (v, digits = 0) => {
  const r = Number(v.toFixed(digits));
  return (r > 0 ? "+" : r < 0 ? "−" : "") + num(Math.abs(r), digits);
};
const mono = (text) => h("span", { class: "num" }, text);
const MODEL_NAMES = {
  xgboost: "XGBoost",
  ridge: "Ridge",
  baseline_5yr: "Five-year average",
  ridge_logistic: "Ridge logistic",
  baseline_persistence: "Last week's direction",
  baseline_majority: "Most common direction",
};

/* ---------- chart engine ---------- */
function niceStep(span, target) {
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  return (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
}

function linearTicks(lo, hi, target) {
  const step = niceStep(hi - lo, target);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

const LOG_TICKS = [1, 1.5, 2, 3, 5, 7, 10, 15, 20, 30, 50];

function timeTicks(t0, t1, narrow) {
  const years = (t1 - t0) / (365.25 * DAY);
  const months = years > 6 ? 24 : years > 2.5 ? 12 : years > 1.2 ? 6 : narrow ? 3 : 2;
  const ticks = [];
  const d = new Date(t0);
  let cursor = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
  while (cursor <= t1) {
    const c = new Date(cursor);
    const month = c.getUTCMonth();
    const keep = months >= 12 ? month === 0 && (months === 12 || c.getUTCFullYear() % 2 === 0)
                              : month % months === 0;
    if (keep) {
      ticks.push({ t: cursor, label: month === 0 ? String(c.getUTCFullYear()) : fmt(c, { month: "short" }) });
    }
    cursor = Date.UTC(c.getUTCFullYear(), month + 1, 1);
  }
  return ticks;
}

class TimeChart {
  /* series: [{ label, type: "line"|"band"|"markers"|"ring", color, width, points, format, endLabel }]
     points: { t, y } for lines and markers, { t, lo, hi } for bands. */
  constructor(block, cfg) {
    this.block = block;
    this.cfg = { height: 300, yFormat: num, ...cfg };
    this.host = $("[data-chart]", block);
    this.start = cfg.start ?? null;
    this.renderLegend();
    this.renderTable();
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
    const items = this.cfg.series.filter((sr) => !sr.hideLegend);
    if (items.length < 2) return;
    $("[data-legend]", this.block).replaceChildren(...items.map((sr) => {
      const swatch = {
        band: h("span", { class: "swatch-band", style: { background: "var(--band)" } }),
        line: h("span", { class: "swatch-line", style: { background: sr.color } }),
        markers: h("span", { class: "swatch-dot", style: { background: sr.color } }),
        ring: h("span", { class: "swatch-ring" }),
      }[sr.type];
      return h("span", { class: "key" }, swatch, sr.label);
    }));
  }

  render() {
    const width = this.host.clientWidth;
    if (!width) return;
    const cfg = this.cfg;
    const narrow = width < 560;
    const height = narrow ? Math.round(cfg.height * 0.78) : cfg.height;
    const m = { top: 8, right: cfg.endLabels ? (narrow ? 50 : 60) : 8, bottom: 26, left: cfg.left ?? 48 };
    const iw = width - m.left - m.right;
    const ih = height - m.top - m.bottom;

    const series = cfg.series.map((sr) => ({ ...sr, pts: this.visible(sr.points) }));
    const all = series.flatMap((sr) => sr.pts);
    if (!all.length) {
      this.host.replaceChildren(h("p", { class: "empty" }, "No data for this period."));
      return;
    }
    const t0 = this.start ?? Math.min(...all.map((p) => p.t));
    const t1 = Math.max(...all.map((p) => p.t));
    const values = all.flatMap((p) => ("lo" in p ? [p.lo, p.hi] : [p.y])).filter((v) => v != null);
    let y0 = Math.min(...values);
    let y1 = Math.max(...values);
    if (cfg.zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }

    const log = cfg.log;
    let yTicks;
    if (log) {
      yTicks = LOG_TICKS.filter((v) => v >= y0 * 0.9 && v <= y1 * 1.1);
      y0 = Math.min(y0, yTicks[0]) * 0.95;
      y1 = Math.max(y1, yTicks[yTicks.length - 1]) * 1.05;
    } else {
      // Extend to whole ticks so the data never touches the plot edge.
      yTicks = linearTicks(y0, y1, narrow ? 4 : 5);
      const step = yTicks[1] - yTicks[0];
      if (yTicks[0] > y0) yTicks.unshift(yTicks[0] - step);
      if (yTicks[yTicks.length - 1] < y1) yTicks.push(yTicks[yTicks.length - 1] + step);
      y0 = yTicks[0];
      y1 = yTicks[yTicks.length - 1];
    }
    const tx = (v) => (log ? Math.log(v) : v);
    const x = (t) => m.left + ((t - t0) / (t1 - t0 || 1)) * iw;
    const y = (v) => m.top + ih - ((tx(v) - tx(y0)) / (tx(y1) - tx(y0) || 1)) * ih;

    const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img", "aria-label": cfg.ariaLabel });
    for (const v of yTicks) {
      const isZero = !log && v === 0;
      svg.append(s("line", { x1: m.left, x2: m.left + iw, y1: y(v), y2: y(v) },
        { stroke: isZero ? "var(--rule-strong)" : "var(--rule)", strokeWidth: 1 }));
      svg.append(Object.assign(s("text", { x: m.left - 8, y: y(v) + 4, "text-anchor": "end" }),
        { textContent: cfg.yFormat(v) }));
    }
    for (const tick of timeTicks(t0, t1, narrow)) {
      if (x(tick.t) < m.left + 16 || x(tick.t) > m.left + iw - 16) continue;
      svg.append(s("line", { x1: x(tick.t), x2: x(tick.t), y1: m.top + ih, y2: m.top + ih + 4 },
        { stroke: "var(--rule-strong)" }));
      svg.append(Object.assign(s("text", { x: x(tick.t), y: height - 6, "text-anchor": "middle" }),
        { textContent: tick.label }));
    }

    for (const sr of series) {
      if (sr.type === "band") {
        const pts = sr.pts.filter((p) => p.lo != null);
        if (!pts.length) continue;
        const top = pts.map((p) => `${x(p.t).toFixed(1)},${y(p.hi).toFixed(1)}`);
        const bottom = pts.slice().reverse().map((p) => `${x(p.t).toFixed(1)},${y(p.lo).toFixed(1)}`);
        svg.append(s("path", { d: `M${top.join("L")}L${bottom.join("L")}Z` }, { fill: "var(--band)" }));
      } else if (sr.type === "line") {
        let d = "";
        let pen = false;
        for (const p of sr.pts) {
          if (p.y == null) { pen = false; continue; }
          d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`;
          pen = true;
        }
        svg.append(s("path", { d }, { fill: "none", stroke: sr.color, strokeWidth: sr.width ?? 2,
                                      strokeLinejoin: "round", strokeLinecap: "round" }));
      } else {
        for (const p of sr.pts) {
          if (p.y == null) continue;
          const ring = sr.type === "ring";
          svg.append(s("circle", { cx: x(p.t), cy: y(p.y), r: ring ? 5 : 3.5 }, {
            fill: ring ? "var(--bg)" : sr.color, stroke: ring ? sr.color : "var(--bg)", strokeWidth: 2 }));
        }
      }
    }

    // Selective end labels; a label that would collide with one already placed is dropped.
    const placed = [];
    for (const sr of series.filter((item) => item.endLabel)) {
      const last = [...sr.pts].reverse().find((p) => p.y != null);
      if (!last) continue;
      const ly = y(last.y);
      if (placed.some((py) => Math.abs(py - ly) < 14)) continue;
      placed.push(ly);
      svg.append(Object.assign(s("text", { x: x(last.t) + 9, y: ly + 4, class: "end-label" }),
        { textContent: sr.endLabel(last.y) }));
    }

    this.attachHover(svg, series, { x, y, m, iw, ih });
    this.host.replaceChildren(svg);
  }

  attachHover(svg, series, g) {
    const lookup = series.map((sr) => ({ sr, map: new Map(sr.pts.map((p) => [p.t, p])) }));
    const times = [...new Set(series.flatMap((sr) => sr.pts.map((p) => p.t)))].sort((a, b) => a - b);
    const cross = s("line", { y1: g.m.top, y2: g.m.top + g.ih },
      { stroke: "var(--ink-3)", strokeWidth: 1, visibility: "hidden" });
    const focus = s("g", {}, { visibility: "hidden" });
    const overlay = s("rect", { x: g.m.left, y: g.m.top, width: g.iw, height: g.ih, class: "overlay",
      tabindex: 0, "aria-label": `${this.cfg.ariaLabel}. Use the arrow keys to read values.` });
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
          if (p.lo != null) {
            rows.push([sr.label, "var(--band)", `${this.cfg.yFormat(p.lo)} to ${this.cfg.yFormat(p.hi)}`]);
          }
          continue;
        }
        if (p.y == null || sr.tooltip === false) continue;
        focus.append(s("circle", { cx, cy: g.y(p.y), r: 4 },
          { fill: sr.color, stroke: "var(--bg)", strokeWidth: 2 }));
        rows.push([sr.tipLabel ?? sr.label, sr.color, (sr.format ?? this.cfg.yFormat)(p.y, p)]);
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
      tooltip.style.left = `${px + 16 + tw > innerWidth ? Math.max(8, px - tw - 16) : px + 16}px`;
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
    const { columns, rows, allRows, extra } = this.cfg.table;
    const data = (allRows ? rows : this.visible(rows)).slice().reverse();
    $("[data-table]", this.block).replaceChildren(table(columns, data), extra ?? "");
  }
}

function table(columns, rows, { full = false } = {}) {
  return h("div", { class: full ? "table-wrap full" : "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: cellClass(c), scope: "col" }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", { class: r.emph ? "emph" : null },
      columns.map((c) => h("td", { class: cellClass(c) }, c.get(r))))))));
}

function cellClass(c) {
  const classes = [c.n ? "n" : c.wrap ? "wrap-text" : "", c.narrowHide ? "narrow-hide" : ""].filter(Boolean);
  return classes.length ? classes.join(" ") : null;
}

/* ---------- lead ---------- */
function renderMeta(data) {
  const { dq, data_through: through } = data.health;
  const total = dq.pass + dq.warn + dq.fail;
  const run = data.latest?.run_date ? fmtDay(parseDate(data.latest.run_date)) : data.generated_at;
  $("#meta").textContent =
    `Forecast updated ${run} · ${dq.pass} of ${total} data checks passed · ` +
    `EIA storage data through ${fmtShort(parseDate(through.storage))}`;

  const issues = [];
  if (dq.fail) {
    issues.push(`${dq.fail} data check${dq.fail > 1 ? "s" : ""} failed on the last run, so some figures may be out of date.`);
  }
  const ageDays = (Date.now() - T(through.storage)) / DAY;
  if (ageDays > 14) {
    issues.push(`The latest EIA storage week is ${Math.floor(ageDays)} days old, so a weekly report may be missing.`);
  }
  if (!data.latest?.storage) issues.push("No current forecast is available.");
  if (issues.length) {
    const notice = $("#notice");
    notice.textContent = issues.join(" ");
    notice.hidden = false;
  }
}

function describeDeviation(forecast, deviation) {
  const kind = forecast >= 0 ? "injection" : "withdrawal";
  const size = Math.round(Math.abs(deviation));
  if (size === 0) return [`Forecast ${kind}, in line with the five-year average.`];
  const tighter = deviation < 0;
  return [`Forecast ${kind}, `, h("strong", {}, `${size} Bcf ${tighter ? "below" : "above"}`),
          ` the five-year average: a ${tighter ? "tighter" : "looser"} week than normal.`];
}

function renderForecast(data) {
  const f = data.latest?.storage;
  const host = $("#forecast");
  if (!f) {
    host.replaceChildren(h("p", { class: "figure-note" }, "No current forecast."));
    return;
  }
  const last = data.storage[data.storage.length - 1];
  const published = data.backtest.storage.find((r) => r.model === f.model);
  const base = data.backtest.storage.find((r) => r.model === "baseline_5yr");
  const alerting = Math.abs(f.deviation) > f.threshold;
  const release = parseDate(f.release_date);

  host.replaceChildren(
    h("p", { class: "label" }, "Next EIA report · ",
      h("strong", {}, `week ending ${fmtShort(parseDate(f.week_ending))}`)),
    h("p", { class: "figure" }, signed(f.forecast), h("span", { class: "unit" }, "Bcf")),
    h("p", { class: "figure-note" }, ...describeDeviation(f.forecast, f.deviation),
      ` EIA publishes the figure ${fmt(release, { weekday: "short", month: "short", day: "numeric" })} at 10:30 ET.`),
    h("dl", { class: "facts" },
      h("div", {}, h("dt", {}, "Five-year average"), h("dd", {}, mono(`${signed(f.baseline)} Bcf`))),
      h("div", {}, h("dt", {}, "Average miss since 2020"),
        h("dd", {}, mono(`${num(published.mae, 1)} Bcf`),
          h("small", {}, `five-year average: ${num(base.mae, 1)}`))),
      h("div", {}, h("dt", {}, "Gas in storage"),
        h("dd", {}, mono(`${num(last.level)} Bcf`),
          h("small", {}, `${signed(last.level - last.avg5)} vs five-year average, ${fmtShort(parseDate(last.week_ending))}`)))),
    h("p", { class: alerting ? "status alerting" : "status" }, alerting
      ? `Alert sent: the forecast is more than ${f.threshold} Bcf from the five-year average.`
      : `No alert. Alerts go out when a forecast is more than ${f.threshold} Bcf from the five-year average.`),
  );
}

/* ---------- charts ---------- */
const bcf = (v) => `${num(v)} Bcf`;
const sBcf = (v) => `${signed(v)} Bcf`;

function recentChart(data) {
  const fc = data.forecasts;
  const latest = Math.max(...fc.map((r) => T(r.week_ending)));
  const pending = fc.filter((r) => r.actual == null && r.source === "live");
  return new TimeChart($("#recent-chart"), {
    ariaLabel: "Weekly storage change over the past year: actual, forecast and five-year average",
    start: latest - 371 * DAY,
    zero: true,
    endLabels: true,
    yFormat: (v) => signed(v),
    dateLabel: (d) => `Week ending ${fmtDay(d)}`,
    series: [
      { label: "Five-year average", type: "line", color: "var(--reference)", width: 1.5, format: sBcf,
        points: fc.map((r) => ({ t: T(r.week_ending), y: r.baseline_5yr })) },
      { label: "Actual", type: "line", color: "var(--ink)", format: sBcf,
        points: fc.map((r) => ({ t: T(r.week_ending), y: r.actual })) },
      { label: "Forecast", type: "line", color: "var(--accent)", format: sBcf,
        points: fc.map((r) => ({ t: T(r.week_ending), y: r.xgboost })) },
      { label: "Next report", type: "ring", color: "var(--accent)", tooltip: false,
        endLabel: (v) => signed(v),
        points: pending.map((r) => ({ t: T(r.week_ending), y: r.xgboost })) },
    ],
    table: {
      allRows: true,
      extra: h("p", { class: "note" }, "All weeks since 2020. ",
        h("a", { href: "data/storage_forecasts.csv" }, "Download CSV"), ", which also includes the ridge model."),
      rows: fc.map((r) => ({ ...r, t: T(r.week_ending) })),
      columns: [
        { label: "Week ending", get: (r) => fmtDay(parseDate(r.week_ending)) },
        { label: "Actual", n: true, get: (r) => (r.actual == null ? "pending" : signed(r.actual)) },
        { label: "Forecast", n: true, get: (r) => signed(r.xgboost) },
        { label: "Five-year avg", n: true, get: (r) => signed(r.baseline_5yr) },
        { label: "Miss", n: true, get: (r) => (r.actual == null ? "" : signed(r.xgboost - r.actual)) },
      ],
    },
  });
}

function storageChart(data) {
  const st = data.storage;
  return new TimeChart($("#storage-chart"), {
    ariaLabel: "Lower 48 working gas in storage against its five-year range",
    endLabels: true,
    dateLabel: (d) => `Week ending ${fmtDay(d)}`,
    series: [
      { label: "Five-year range", type: "band",
        points: st.map((r) => ({ t: T(r.week_ending), lo: r.min5, hi: r.max5 })) },
      { label: "Five-year average", type: "line", color: "var(--reference)", width: 1.5, format: bcf,
        points: st.map((r) => ({ t: T(r.week_ending), y: r.avg5 })) },
      { label: "Working gas", type: "line", color: "var(--ink)", format: bcf, endLabel: (v) => num(v),
        points: st.map((r) => ({ t: T(r.week_ending), y: r.level })) },
    ],
    table: {
      rows: st.map((r) => ({ ...r, t: T(r.week_ending) })),
      extra: h("p", { class: "note" }, h("a", { href: "data/storage_weekly.csv" }, "Download CSV"), " (all weeks since 2016)."),
      columns: [
        { label: "Week ending", get: (r) => fmtDay(parseDate(r.week_ending)) },
        { label: "Storage", n: true, get: (r) => num(r.level) },
        { label: "Change", n: true, get: (r) => (r.change == null ? "" : signed(r.change)) },
        { label: "Five-year avg", n: true, get: (r) => (r.avg5 == null ? "" : num(r.avg5)) },
        { label: "Difference", n: true, get: (r) => (r.avg5 == null ? "" : signed(r.level - r.avg5)) },
      ],
    },
  });
}

function errorChart(data) {
  const mae = data.rolling_mae;
  const miss = (v) => `${num(v, 1)} Bcf`;
  return new TimeChart($("#error-chart"), {
    ariaLabel: "Average forecast miss over the previous 52 weeks, by model",
    height: 260,
    zero: true,
    endLabels: true,
    dateLabel: (d) => `52 weeks to ${fmtDay(d)}`,
    series: [
      { label: "Five-year average", type: "line", color: "var(--reference)", width: 1.5, format: miss,
        endLabel: (v) => num(v, 1), points: mae.map((r) => ({ t: T(r.week_ending), y: r.baseline_5yr })) },
      { label: "Ridge", type: "line", color: "var(--accent-2)", width: 1.5, format: miss,
        points: mae.map((r) => ({ t: T(r.week_ending), y: r.ridge })) },
      { label: "XGBoost (published)", tipLabel: "XGBoost", type: "line", color: "var(--accent)", format: miss,
        endLabel: (v) => num(v, 1), points: mae.map((r) => ({ t: T(r.week_ending), y: r.xgboost })) },
    ],
    table: {
      rows: mae.map((r) => ({ ...r, t: T(r.week_ending) })),
      columns: [
        { label: "52 weeks to", get: (r) => fmtDay(parseDate(r.week_ending)) },
        { label: "XGBoost", n: true, get: (r) => num(r.xgboost, 1) },
        { label: "Ridge", n: true, get: (r) => num(r.ridge, 1) },
        { label: "Five-year avg", n: true, get: (r) => num(r.baseline_5yr, 1) },
      ],
    },
  });
}

function priceChart(data) {
  const th = data.thresholds.henry_hub_dod_pct;
  const crit = th * data.thresholds.critical_multiple;
  const px = data.prices;
  const big = px.filter((r) => r.dod_pct != null && Math.abs(r.dod_pct) > th);
  const move = (v, p) => `${signed(p.pct, 1)}% on the day`;
  const marks = (keep) => big.filter(keep).map((r) => ({ t: T(r.price_date), y: r.price, pct: r.dod_pct }));
  $("#price-caption").textContent =
    `$/MMBtu, log scale. Dots mark days that moved more than ${th}% from the previous trading day, ` +
    "the alert threshold. EIA publishes these prices about a week late.";
  return new TimeChart($("#price-chart"), {
    ariaLabel: "Henry Hub spot price since 2020 with large daily moves marked",
    log: true,
    left: 44,
    yFormat: (v) => `$${num(v, v < 10 && v % 1 ? 1 : 0)}`,
    series: [
      { label: "Henry Hub spot", type: "line", color: "var(--ink)", width: 1.25, format: (v) => `$${num(v, 2)}`,
        points: px.map((r) => ({ t: T(r.price_date), y: r.price })) },
      { label: `Move over ${th}%`, type: "markers", color: "var(--warning)", format: move,
        points: marks((r) => Math.abs(r.dod_pct) < crit) },
      { label: `Move over ${crit}%`, type: "markers", color: "var(--critical)", format: move,
        points: marks((r) => Math.abs(r.dod_pct) >= crit) },
    ],
    table: {
      rows: px.map((r) => ({ ...r, t: T(r.price_date) })),
      extra: h("p", { class: "note" }, h("a", { href: "data/henry_hub_daily.csv" }, "Download CSV"), " (daily since 2016)."),
      columns: [
        { label: "Date", get: (r) => fmtDay(parseDate(r.price_date)) },
        { label: "$/MMBtu", n: true, get: (r) => num(r.price, 2) },
        { label: "Change", n: true, get: (r) => (r.dod_pct == null ? "" : `${signed(r.dod_pct, 1)}%`) },
      ],
    },
  });
}

/* ---------- secondary content ---------- */
function backtestPeriod(data) {
  const bt = data.forecasts.filter((r) => r.source === "backtest");
  return [parseDate(bt[0].week_ending), parseDate(bt[bt.length - 1].week_ending), bt.length];
}

function renderAccuracy(data) {
  const rows = ["xgboost", "ridge", "baseline_5yr"]
    .map((m) => data.backtest.storage.find((r) => r.model === m)).filter(Boolean);
  const [first, last, n] = backtestPeriod(data);
  const p = Math.max(...rows.filter((r) => r.dm_p_value != null).map((r) => r.dm_p_value));
  $("#accuracy-table").replaceChildren(h("div", { class: "block" },
    h("h3", {}, "Backtest results"),
    table([
      { label: "Model", get: (r) => (r.model === "xgboost" ? "XGBoost (published)" : MODEL_NAMES[r.model]) },
      { label: "Average miss", n: true, get: (r) => `${num(r.mae, 1)} Bcf` },
      { label: "RMSE", n: true, narrowHide: true, get: (r) => `${num(r.rmse, 1)} Bcf` },
      { label: "vs five-year avg", n: true,
        get: (r) => (r.model === "baseline_5yr" ? "" : `${signed(r.mae_vs_baseline_pct)}%`) },
    ], rows.map((r) => ({ ...r, emph: r.model === "xgboost" })), { full: true }),
    h("p", { class: "note" },
      `${num(n)} weekly reports, ${fmtMonth(first)} to ${fmtMonth(last)}. Each forecast was made the day before ` +
      "release with models retrained on data published by then. Average miss is the mean absolute error. " +
      "Both models' improvement over the five-year average is statistically significant " +
      `(Diebold–Mariano p\u00a0${p < 0.001 ? "<\u00a00.001" : `=\u00a0${num(p, 3)}`}).`)));
}

function renderDirection(data) {
  const model = data.latest?.direction?.model;
  const prob = data.latest?.direction?.prob_up;
  const results = data.backtest.direction;
  const candidates = results.filter((r) => !r.model.startsWith("baseline"));
  const best = candidates.reduce((a, b) => (b.accuracy > a.accuracy ? b : a));
  const skill = candidates.some((r) => r.accuracy > 0.5 && r.p_vs_coin < 0.05);
  const current = prob == null ? "" : `The current model output (${MODEL_NAMES[model] ?? model}) is ` +
    `${num(100 * prob)}% for a higher average price over the next seven days. `;
  $("#direction").replaceChildren(h("div", { class: "direction" },
    h("h3", {}, skill ? "Price direction" : "Price direction: no reliable signal"),
    h("p", {}, current, skill
      ? "At least one direction model beat a coin flip in backtests."
      : `In backtests no model beat a coin flip (best ${num(100 * best.accuracy, 1)}% correct over ${best.n} ` +
        `weeks, p = ${num(best.p_vs_coin, 2)}), so it is not used for alerts.`),
    h("details", { class: "data-view" }, h("summary", {}, "Show backtest"),
      table([
        { label: "Model", get: (r) => MODEL_NAMES[r.model] ?? r.model },
        { label: "Correct", n: true, get: (r) => `${num(100 * r.accuracy, 1)}%` },
        { label: "95% interval", n: true, get: (r) => `${num(100 * r.ci_low)}–${num(100 * r.ci_high)}%` },
        { label: "p vs 50%", n: true, get: (r) => num(r.p_vs_coin, 2) },
      ], results, { full: true }))));
}

function renderAlerts(data) {
  const t = data.thresholds;
  $("#alert-rules").textContent =
    `An email goes out when the storage forecast is more than ${t.storage_vs_5yr_bcf} Bcf from the five-year ` +
    `average, or when Henry Hub moves more than ${t.henry_hub_dod_pct}% in a day. Alerts are marked critical at ` +
    `${t.critical_multiple === 2 ? "twice" : `${t.critical_multiple} times`} those thresholds, and each one is sent once.`;
  const columns = [
    { label: "Date", get: (r) => fmtDay(parseDate(r.subject_date)) },
    { label: "Severity",
      get: (r) => h("span", { class: `sev ${r.severity}` }, r.severity === "critical" ? "Critical" : "Warning") },
    { label: "Alert", wrap: true, get: (r) => r.message },
  ];
  $("#alert-log").replaceChildren(data.alerts.length
    ? table([...columns, { label: "Email", get: (r) => r.delivery_status }], data.alerts)
    : h("p", { class: "empty" }, data.live_since
      ? `No alerts since daily runs began on ${fmtDay(parseDate(data.live_since))}.`
      : "No alerts yet."));
  $("#alert-replay").replaceChildren(
    h("summary", {}, `What these rules would have flagged since 2020 (${num(data.alert_replay.length)})`),
    h("p", { class: "note", style: { marginTop: 0, marginBottom: "12px" } },
      "Simulated from backtest forecasts and price history, and used to set the thresholds. None of these were sent."),
    table(columns, data.alert_replay),
  );
}

function renderMethod(data) {
  const through = data.health.data_through;
  const link = (href, text) => h("a", { href }, text);
  $("#sources").replaceChildren(table([
    { label: "Source", get: (r) => r.source },
    { label: "Series", wrap: true, get: (r) => r.series },
    { label: "Updates", wrap: true, get: (r) => r.updates },
    { label: "Data through", get: (r) => r.through },
  ], [
    { source: link("https://www.eia.gov/naturalgas/storage/", "EIA"),
      series: "Lower 48 working gas in storage (NW2_EPG0_SWO_R48_BCF)",
      updates: "Thursdays, 10:30 ET", through: fmtDay(parseDate(through.storage)) },
    { source: link("https://www.eia.gov/dnav/ng/hist/rngwhhdd.htm", "EIA"),
      series: "Henry Hub spot price (RNGWHHD)",
      updates: "Weekly batches, about a week behind", through: fmtDay(parseDate(through.price)) },
    { source: link("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/cdus/degree_days/", "NOAA CPC"),
      series: "US heating degree days weighted by gas-heated homes; cooling degree days weighted by population",
      updates: "Daily, about two days behind", through: fmtDay(parseDate(through.weather)) },
  ], { full: true }));

  const [first, last, n] = backtestPeriod(data);
  $("#backtest-text").textContent =
    `${num(n)} weekly forecasts for reports from ${fmtMonth(first)} to ${fmtMonth(last)}, each made the day ` +
    "before release with models retrained on data published by then. Storage forecasts are compared with the " +
    "five-year average using the Diebold–Mariano test; price-direction forecasts are compared with a coin flip.";
}

/* ---------- controls ---------- */
function initTabs() {
  const tabs = [...document.querySelectorAll("[role=tab]")];
  const select = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (e) => {
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (next == null) return;
      e.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], true);
    });
  });
}

function initRange(chart, data) {
  const latest = new Date(Math.max(...data.storage.map((r) => T(r.week_ending))));
  const buttons = [...document.querySelectorAll("#range button")];
  const apply = (value) => {
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === value)));
    chart.setStart(value === "all" ? null
      : Date.UTC(latest.getUTCFullYear() - Number(value), latest.getUTCMonth(), latest.getUTCDate()));
  };
  buttons.forEach((b) => b.addEventListener("click", () => apply(b.dataset.range)));
  apply("3");
}

async function main() {
  let data;
  try {
    const resp = await fetch("data/dashboard.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    const notice = $("#notice");
    notice.textContent = `The data file could not be loaded (${err.message}). Try again shortly.`;
    notice.hidden = false;
    return;
  }
  renderMeta(data);
  renderForecast(data);
  recentChart(data);
  initRange(storageChart(data), data);
  errorChart(data);
  priceChart(data);
  renderAccuracy(data);
  renderDirection(data);
  renderAlerts(data);
  renderMethod(data);
  initTabs();
}

main();
