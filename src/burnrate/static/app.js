/* burnrate dashboard.
 *
 * No build step and no dependencies: SVG is generated directly. Two fetches
 * drive everything -- /api/now for the gauges, hero, and banner, /api/history
 * for the small-multiple area charts.
 *
 * The guiding rule is that this page must never present a stale reading as a
 * current one. Every render path knows how old its data is, and a failed fetch
 * escalates to a banner instead of quietly leaving the last good numbers up.
 */

const REFRESH_MS = 60_000;
const GAUGE_SWEEP = 270; // degrees, centered on 12 o'clock
const GAUGE_START = -135;

const els = {
  page: document.querySelector(".page"),
  banner: document.getElementById("banner"),
  bannerIcon: document.getElementById("banner-icon"),
  bannerTitle: document.getElementById("banner-title"),
  bannerDetail: document.getElementById("banner-detail"),
  dot: document.getElementById("health-dot"),
  freshness: document.getElementById("freshness"),
  hero: document.querySelector(".hero"),
  heroValue: document.getElementById("hero-value"),
  heroDetail: document.getElementById("hero-detail"),
  gauges: document.getElementById("gauges"),
  charts: document.getElementById("charts"),
  tableBody: document.getElementById("table-body"),
  range: document.getElementById("range"),
  tooltip: document.getElementById("tooltip"),
  footerMeta: document.getElementById("footer-meta"),
};

const state = {
  hours: 168,
  buckets: [],
  now: null,
  lastGoodAt: null,
};

/* ---------------------------------------------------------------- utilities */

const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));
const pct = (value) => `${Math.round(value)}%`;

function stateFor(utilization) {
  if (utilization >= 90) return { key: "critical", label: "Critical", color: "var(--critical)" };
  if (utilization >= 70) return { key: "warning", label: "Watch", color: "var(--warning)" };
  return { key: "good", label: "Healthy", color: "var(--good)" };
}

/** "5h 42m" / "3d 4h" / "48s" — coarse by design, this is a countdown not a clock. */
function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function formatAge(seconds) {
  if (seconds == null) return "never";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  return `${formatDuration(seconds)} ago`;
}

function formatClock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function el(tag, attrs = {}, children = []) {
  const node =
    tag === "svg" || SVG_TAGS.has(tag)
      ? document.createElementNS("http://www.w3.org/2000/svg", tag)
      : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(child);
  }
  return node;
}

const SVG_TAGS = new Set([
  "svg",
  "g",
  "path",
  "circle",
  "line",
  "rect",
  "text",
  "defs",
  "linearGradient",
  "stop",
]);

/* ------------------------------------------------------------------- gauges */

function polar(cx, cy, r, angleDeg) {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function arcPath(cx, cy, r, startAngle, endAngle) {
  const [x0, y0] = polar(cx, cy, r, startAngle);
  const [x1, y1] = polar(cx, cy, r, endAngle);
  const largeArc = Math.abs(endAngle - startAngle) > 180 ? 1 : 0;
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

function renderGauge(bucket) {
  const utilization = clamp(Number(bucket.utilization) || 0, 0, 100);
  const status = stateFor(utilization);
  const cx = 80;
  const cy = 80;
  const r = 62;
  const endAngle = GAUGE_START + (GAUGE_SWEEP * utilization) / 100;

  const svg = el("svg", {
    viewBox: "0 0 160 136",
    role: "img",
    "aria-label": `${bucket.label}: ${pct(utilization)} used, ${status.label}`,
  });

  svg.appendChild(
    el("path", {
      d: arcPath(cx, cy, r, GAUGE_START, GAUGE_START + GAUGE_SWEEP),
      fill: "none",
      stroke: status.color,
      "stroke-opacity": "0.16",
      "stroke-width": "12",
      "stroke-linecap": "round",
    }),
  );

  // A zero-length arc renders as a stray dot under round caps, so skip it and
  // let the numeric readout carry "0%".
  if (utilization > 0.5) {
    svg.appendChild(
      el("path", {
        d: arcPath(cx, cy, r, GAUGE_START, endAngle),
        fill: "none",
        stroke: status.color,
        "stroke-width": "12",
        "stroke-linecap": "round",
      }),
    );
  }

  const readout = el("div", { class: "gauge__readout" }, [
    el("div", { class: "gauge__pct", text: pct(utilization) }),
    el("div", { class: "gauge__state", style: `color:${status.color}`, text: status.label }),
  ]);

  const countdown = el("div", {
    class: "gauge__countdown",
    "data-resets-at": bucket.resets_at || "",
    text: bucket.resets_at ? "" : "No reset reported",
  });

  const card = el(
    "div",
    { class: `gauge${bucket.known ? "" : " gauge--unknown"}` },
    [
      el("div", { class: "gauge__label", text: bucket.label }),
      el("div", { class: "gauge__dial" }, [svg, readout]),
      countdown,
      bucket.known
        ? null
        : el("div", { class: "gauge__note", text: `Unrecognized bucket "${bucket.key}"` }),
    ],
  );
  return card;
}

function renderGauges(buckets) {
  els.gauges.replaceChildren(...buckets.map(renderGauge));
  tickCountdowns();
}

function tickCountdowns() {
  const now = Date.now();
  for (const node of els.gauges.querySelectorAll(".gauge__countdown")) {
    const iso = node.getAttribute("data-resets-at");
    if (!iso) continue;
    const target = new Date(iso).getTime();
    if (Number.isNaN(target)) {
      node.textContent = "Reset time unreadable";
      continue;
    }
    node.textContent =
      target <= now ? "Resetting…" : `Resets in ${formatDuration((target - now) / 1000)}`;
  }
}

/* ------------------------------------------------------------------- charts */

const CHART = { w: 760, h: 168, left: 36, right: 14, top: 12, bottom: 26 };

function renderChart(series, windowStart, windowEnd) {
  const points = (series.points || [])
    .map((p) => ({ t: new Date(p.ts).getTime(), v: Number(p.utilization) }))
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
    .sort((a, b) => a.t - b.t);

  const head = el("div", { class: "chart__head" }, [
    el("div", { class: "chart__title", text: series.label || series.key }),
    el("div", {
      class: "chart__now",
      text: points.length ? `now ${pct(points[points.length - 1].v)}` : "",
    }),
  ]);

  if (points.length < 2) {
    return el("div", { class: "chart" }, [
      head,
      el("div", {
        class: "chart__empty",
        text: "Not enough samples yet — the chart fills in as polling continues.",
      }),
    ]);
  }

  const plotW = CHART.w - CHART.left - CHART.right;
  const plotH = CHART.h - CHART.top - CHART.bottom;
  const span = Math.max(1, windowEnd - windowStart);
  const x = (t) => CHART.left + (plotW * (t - windowStart)) / span;
  const y = (v) => CHART.top + plotH * (1 - clamp(v, 0, 100) / 100);

  const gradientId = `grad-${series.key.replace(/[^a-z0-9]/gi, "")}`;
  const svg = el("svg", {
    viewBox: `0 0 ${CHART.w} ${CHART.h}`,
    role: "img",
    "aria-label": `${series.label}: utilization over the selected window. Values are listed in the table view.`,
  });

  svg.appendChild(
    el("defs", {}, [
      el("linearGradient", { id: gradientId, x1: "0", y1: "0", x2: "0", y2: "1" }, [
        el("stop", { offset: "0%", "stop-color": "var(--series)", "stop-opacity": "0.30" }),
        el("stop", { offset: "100%", "stop-color": "var(--series)", "stop-opacity": "0.02" }),
      ]),
    ]),
  );

  // Recessive hairline grid; solid, never dashed.
  for (const value of [0, 50, 100]) {
    const gy = y(value);
    svg.appendChild(
      el("line", {
        x1: CHART.left,
        y1: gy,
        x2: CHART.w - CHART.right,
        y2: gy,
        stroke: value === 0 ? "var(--axis)" : "var(--grid)",
        "stroke-width": "1",
      }),
    );
    svg.appendChild(
      el("text", {
        x: CHART.left - 8,
        y: gy + 4,
        "text-anchor": "end",
        fill: "var(--ink-muted)",
        "font-size": "10",
        "font-variant-numeric": "tabular-nums",
        text: `${value}%`,
      }),
    );
  }

  const line = points.map((p, i) => `${i ? "L" : "M"} ${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  const area = `${line} L ${x(points[points.length - 1].t).toFixed(1)} ${y(0).toFixed(1)} L ${x(points[0].t).toFixed(1)} ${y(0).toFixed(1)} Z`;

  svg.appendChild(el("path", { d: area, fill: `url(#${gradientId})`, stroke: "none" }));
  svg.appendChild(
    el("path", {
      d: line,
      fill: "none",
      stroke: "var(--series)",
      "stroke-width": "2",
      "stroke-linejoin": "round",
      "stroke-linecap": "round",
    }),
  );

  // Selective direct label: the endpoint only.
  const last = points[points.length - 1];
  svg.appendChild(
    el("circle", {
      cx: x(last.t),
      cy: y(last.v),
      r: "3.5",
      fill: "var(--series)",
      stroke: "var(--surface)",
      "stroke-width": "2",
    }),
  );

  for (const tick of timeTicks(windowStart, windowEnd)) {
    svg.appendChild(
      el("text", {
        x: x(tick.t),
        y: CHART.h - 8,
        "text-anchor": "middle",
        fill: "var(--ink-muted)",
        "font-size": "10",
        text: tick.label,
      }),
    );
  }

  const crosshair = el("line", {
    y1: CHART.top,
    y2: CHART.top + plotH,
    stroke: "var(--ink-muted)",
    "stroke-width": "1",
    opacity: "0",
  });
  const marker = el("circle", {
    r: "4",
    fill: "var(--series)",
    stroke: "var(--surface)",
    "stroke-width": "2",
    opacity: "0",
  });
  svg.appendChild(crosshair);
  svg.appendChild(marker);

  attachHover(svg, { points, x, y, series, crosshair, marker });

  return el("div", { class: "chart" }, [head, svg]);
}

function timeTicks(start, end) {
  const ticks = [];
  const span = end - start;
  const count = 6;
  for (let i = 0; i <= count; i += 1) {
    const t = start + (span * i) / count;
    const date = new Date(t);
    ticks.push({
      t,
      label:
        span > 36 * 3600 * 1000
          ? date.toLocaleDateString(undefined, { weekday: "short" })
          : date.toLocaleTimeString(undefined, { hour: "numeric" }),
    });
  }
  return ticks;
}

/** Crosshair + tooltip. Hit area is the whole plot, not the 2px line. */
function attachHover(svg, ctx) {
  const hide = () => {
    ctx.crosshair.setAttribute("opacity", "0");
    ctx.marker.setAttribute("opacity", "0");
    els.tooltip.hidden = true;
  };

  const move = (event) => {
    const rect = svg.getBoundingClientRect();
    const scale = CHART.w / rect.width;
    const localX = (event.clientX - rect.left) * scale;

    let nearest = ctx.points[0];
    let best = Infinity;
    for (const point of ctx.points) {
      const distance = Math.abs(ctx.x(point.t) - localX);
      if (distance < best) {
        best = distance;
        nearest = point;
      }
    }

    const px = ctx.x(nearest.t);
    const py = ctx.y(nearest.v);
    ctx.crosshair.setAttribute("x1", px);
    ctx.crosshair.setAttribute("x2", px);
    ctx.crosshair.setAttribute("opacity", "0.5");
    ctx.marker.setAttribute("cx", px);
    ctx.marker.setAttribute("cy", py);
    ctx.marker.setAttribute("opacity", "1");

    els.tooltip.innerHTML = `<b>${pct(nearest.v)}</b> <span>· ${ctx.series.label}</span><br><span>${new Date(
      nearest.t,
    ).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}</span>`;
    els.tooltip.hidden = false;
    const tip = els.tooltip.getBoundingClientRect();
    const left = clamp(event.clientX + 14, 8, window.innerWidth - tip.width - 8);
    els.tooltip.style.left = `${left}px`;
    els.tooltip.style.top = `${Math.max(8, event.clientY - tip.height - 12)}px`;
  };

  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("pointercancel", hide);
}

/* -------------------------------------------------------------------- table */

function renderTable(buckets) {
  els.tableBody.replaceChildren(
    ...buckets.map((bucket) => {
      const status = stateFor(bucket.utilization);
      return el("tr", {}, [
        el("td", { text: bucket.label }),
        el("td", { text: pct(bucket.utilization) }),
        el("td", { text: status.label }),
        el("td", { text: bucket.resets_at ? formatClock(bucket.resets_at) : "—" }),
        el("td", { text: bucket.source || "—" }),
      ]);
    }),
  );
}

/* --------------------------------------------------------------------- hero */

function renderHero(projection, weekly) {
  const status = projection?.status ?? "unavailable";
  els.hero.setAttribute("data-status", status);

  if (status === "projected" || status === "clears_reset") {
    const rate = projection.rate_per_hour;
    const detail =
      `${projection.message} Burning ${rate.toFixed(1)}%/hour since the period opened ` +
      `${formatDuration(projection.elapsed_hours * 3600)} ago; now at ${pct(projection.utilization)}.`;
    els.heroValue.textContent =
      status === "clears_reset" ? "Clears the reset" : `Cap at ${formatClock(projection.hits_cap_at)}`;
    els.heroDetail.textContent = detail;
    return;
  }

  const fallbacks = {
    at_cap: "Weekly cap reached",
    idle: "No usage yet",
    insufficient_data: "Too early to project",
    unavailable: "Projection unavailable",
  };
  els.heroValue.textContent = fallbacks[status] ?? "Projection unavailable";
  els.heroDetail.textContent =
    projection?.message ??
    (weekly ? "Waiting for enough samples." : "No weekly bucket in the latest reading.");
}

/* ------------------------------------------------------------------- banner */

function renderBanner(data, fetchError) {
  if (fetchError) {
    show("error", "Dashboard cannot reach its backend", fetchError);
    els.dot.dataset.state = "error";
    return;
  }

  const status = data.status || {};
  const age = data.staleness_seconds;

  if (status.last_error) {
    show(
      status.consecutive_failures > 2 ? "error" : "warn",
      `Usage fetch failing (${status.consecutive_failures}×)`,
      `${status.last_error} — showing the last good reading from ${formatAge(age)}.`,
    );
    els.dot.dataset.state = status.consecutive_failures > 2 ? "error" : "stale";
    return;
  }

  if (data.stale) {
    show(
      "warn",
      "Data may be stale",
      `Last successful reading ${formatAge(age)}, past the ${Math.round(
        data.stale_after_seconds,
      )}s freshness window.`,
    );
    els.dot.dataset.state = "stale";
    return;
  }

  // Only genuine anomalies reach the banner. Routine drift -- an unrecognized
  // bucket -- arrives as a notice and is already visible on its own dashed
  // card, so it stays out of the banner and off the footer's critical path.
  if (status.warnings?.length) {
    show(
      "warn",
      "Response shape changed",
      `${status.warnings.join("; ")} — buckets shown may be incomplete.`,
    );
    els.dot.dataset.state = "live";
    return;
  }

  els.banner.hidden = true;
  els.dot.dataset.state = "live";

  function show(severity, title, detail) {
    els.banner.hidden = false;
    els.banner.dataset.severity = severity === "error" ? "error" : "warn";
    els.bannerIcon.textContent = severity === "error" ? "×" : "!";
    els.bannerTitle.textContent = title;
    els.bannerDetail.textContent = detail;
  }
}

/* --------------------------------------------------------------------- load */

async function loadNow() {
  const response = await fetch("./api/now", { cache: "no-store" });
  if (!response.ok) throw new Error(`/api/now returned HTTP ${response.status}`);
  return response.json();
}

async function loadHistory(hours) {
  const response = await fetch(`./api/history?hours=${hours}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`/api/history returned HTTP ${response.status}`);
  return response.json();
}

async function refresh({ history = true } = {}) {
  els.page.dataset.refreshing = "true";
  try {
    const data = await loadNow();
    state.now = data;
    state.buckets = data.buckets || [];
    state.lastGoodAt = Date.now();

    renderBanner(data, null);
    renderGauges(state.buckets);
    renderTable(state.buckets);
    renderHero(data.projection, state.buckets.find((b) => b.key === "seven_day"));

    els.freshness.textContent = `Updated ${formatAge(data.staleness_seconds)}`;
    els.footerMeta.textContent = [
      `Credential source: ${data.status?.credential_source ?? "unknown"}`,
      `poll every ${Math.round(data.poll_interval_seconds)}s`,
      ...(data.status?.notices?.length ? [`${data.status.notices.length} unrecognized bucket(s)`] : []),
    ].join(" · ");

    if (history) await refreshHistory();
  } catch (error) {
    renderBanner(null, String(error.message || error));
    els.freshness.textContent = state.lastGoodAt
      ? `Stale since ${formatAge((Date.now() - state.lastGoodAt) / 1000)}`
      : "Unavailable";
  } finally {
    delete els.page.dataset.refreshing;
  }
}

async function refreshHistory() {
  try {
    const data = await loadHistory(state.hours);
    const end = Date.now();
    const start = end - state.hours * 3600 * 1000;
    const order = new Map(state.buckets.map((b, i) => [b.key, i]));
    const series = (data.series || []).sort(
      (a, b) => (order.get(a.key) ?? 99) - (order.get(b.key) ?? 99),
    );

    els.charts.replaceChildren(
      ...(series.length
        ? series.map((s) => renderChart(s, start, end))
        : [
            el("div", { class: "chart" }, [
              el("div", {
                class: "chart__empty",
                text: "No history yet. Samples appear within a minute of the first successful poll.",
              }),
            ]),
          ]),
    );
  } catch (error) {
    els.charts.replaceChildren(
      el("div", { class: "chart" }, [
        el("div", { class: "chart__empty", text: `History unavailable: ${error.message}` }),
      ]),
    );
  }
}

els.range.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-hours]");
  if (!button) return;
  state.hours = Number(button.dataset.hours);
  for (const other of els.range.querySelectorAll("button")) {
    other.setAttribute("aria-pressed", String(other === button));
  }
  refreshHistory();
});

setInterval(tickCountdowns, 1000);
setInterval(() => refresh(), REFRESH_MS);
refresh();
