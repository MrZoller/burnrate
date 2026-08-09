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

// Refresh bounds. The cadence follows the backend's configured interval -- reported by
// /api/now -- because a fixed 60s meant a sub-second poll interval filled the store
// with readings the page would not show for another minute. Clamped at both ends: the
// floor stops a 0.5s interval turning into two fetches a second per open tab, and the
// ceiling keeps the staleness line and the banner moving even on an hourly cadence.
const REFRESH_MS_DEFAULT = 60_000;
const REFRESH_MS_MIN = 5_000;
const REFRESH_MS_MAX = 60_000;
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
  historyBody: document.getElementById("history-body"),
  historyLabel: document.getElementById("history-label"),
  range: document.getElementById("range"),
  tooltip: document.getElementById("tooltip"),
  footerMeta: document.getElementById("footer-meta"),
};

const state = {
  hours: 168,
  buckets: [],
  now: null,
  // The last history payload, kept so the charts can be redrawn without a fetch
  // when /api/now fails and their labels need to stop claiming to be current.
  history: null,
  // The moment the newest reading was taken, derived from the age the backend
  // reported rather than from our own clock, so an outage reports the data's real
  // age instead of the time since we last managed to fetch it.
  readingAt: null,
  // How far this browser's clock sits from the server's. The dashboard is meant to be
  // read from other machines on the tailnet, and every timestamp in the payloads is the
  // server's -- so comparing them against a local Date.now() shifted the chart window
  // by the skew: points past the right edge on a slow clock, a false empty tail on a
  // fast one. Measured from `generated_at` and re-measured on every successful fetch.
  clockSkewMs: 0,
};

/** Now, on the server's clock. Advances in real time, unlike `generated_at` alone. */
function serverNow() {
  return Date.now() - state.clockSkewMs;
}

/* ---------------------------------------------------------------- utilities */

const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));
const pct = (value) => `${Math.round(value)}%`;

/* Pace, not level. The verdict and its colour both come from the backend's
 * per-bucket pace math (burn% against elapsed%), so a gauge is never a green
 * "Healthy" beside a hero warning of an imminent cap. Colours reuse the existing
 * validated status tokens; the two neutral verdicts carry no colour at all, since
 * "too early" and "unknown" are explicitly not judgements. */
const PACE = {
  on_pace: { color: "var(--good)" },
  ahead_of_pace: { color: "var(--warning)" },
  on_pace_to_cap: { color: "var(--critical)" },
  too_early: { color: "var(--ink-muted)" },
  unknown: { color: "var(--ink-muted)" },
};

/** {label, color} for a bucket's pace verdict; label text is the backend's. */
function paceFor(bucket) {
  const color = (PACE[bucket.pace_status] || PACE.unknown).color;
  return { label: bucket.pace_label || "Unknown", color };
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

function renderGauge(bucket, stale) {
  const utilization = clamp(Number(bucket.utilization) || 0, 0, 100);
  const pace = paceFor(bucket);
  // A stale reading is genuinely the last known number, so keep the percentage and
  // the arc's magnitude -- but strip the present-tense judgement. A pace verdict is
  // an assertion about a value we know to be old, so on stale the word becomes a
  // neutral "Stale" and the colour drops to muted: the dataviz rule forbids the
  // colour carrying a meaning the label no longer does, so both go together. The
  // banner and freshness line already say how old the reading is.
  const stateColor = stale ? "var(--ink-muted)" : pace.color;
  const stateLabel = stale ? "Stale" : pace.label;
  const cx = 80;
  const cy = 80;
  const r = 62;
  const endAngle = GAUGE_START + (GAUGE_SWEEP * utilization) / 100;

  const svg = el("svg", {
    viewBox: "0 0 160 136",
    role: "img",
    "aria-label": `${bucket.label}: ${pct(utilization)} used, ${stateLabel}`,
  });

  svg.appendChild(
    el("path", {
      d: arcPath(cx, cy, r, GAUGE_START, GAUGE_START + GAUGE_SWEEP),
      fill: "none",
      stroke: stateColor,
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
        stroke: stateColor,
        "stroke-width": "12",
        "stroke-linecap": "round",
      }),
    );
  }

  const readout = el("div", { class: "gauge__readout" }, [
    el("div", { class: "gauge__pct", text: pct(utilization) }),
    el("div", { class: "gauge__state", style: `color:${stateColor}`, text: stateLabel }),
  ]);

  // Item 1: the window line is now primary -- "Fri 11:01 AM → Sat 10:59 AM" -- with
  // the live countdown demoted to a secondary line. Three cases, and the middle one
  // is the schema-drift path this project designs for: an unrecognized bucket can
  // still carry a `resets_at` (the parser keeps it) but has no derivable window
  // start, so it shows only the countdown -- printing "No reset reported" above a
  // live "Resets in 6h" would be a false line. "No reset reported" is reserved for a
  // bucket that genuinely has no reset at all.
  let windowLine = null;
  if (bucket.window_opened_at && bucket.resets_at) {
    windowLine = el("div", {
      class: "gauge__window",
      text: `${formatClock(bucket.window_opened_at)} → ${formatClock(bucket.resets_at)}`,
    });
  } else if (!bucket.resets_at) {
    windowLine = el("div", { class: "gauge__window", text: "No reset reported" });
  }
  const countdown = bucket.resets_at
    ? el("div", { class: "gauge__countdown", "data-resets-at": bucket.resets_at, text: "" })
    : null;

  const card = el(
    "div",
    { class: `gauge${bucket.known ? "" : " gauge--unknown"}` },
    [
      el("div", { class: "gauge__label", text: bucket.label }),
      el("div", { class: "gauge__dial" }, [svg, readout]),
      renderElapsedBar(bucket, stale),
      windowLine,
      countdown,
      bucket.known
        ? null
        : el("div", { class: "gauge__note", text: `Unrecognized bucket "${bucket.key}"` }),
    ],
  );
  return card;
}

/* Item 2: a thin time-elapsed bar under the dial -- window start at the left, reset
 * at the right, a marker at the reading. It sets percent-of-time-elapsed directly
 * against the arc's percent-of-budget-burned, so pace is legible per card without the
 * hero. Deliberately uncoloured by pace (the gauge already carries the verdict's
 * colour) and anchored to the reading time, not the wall clock: `elapsed_fraction`
 * comes from the backend measured at the reading, so on a stale reading the marker
 * freezes where the data left it instead of sliding on over hours nobody sampled. */
function renderElapsedBar(bucket, stale) {
  // Bail BEFORE coercion: the backend sends `elapsed_fraction: null` for buckets
  // with no derivable window (unknown buckets, and any bucket missing a reset), and
  // `Number(null)` is a finite 0 that would slip past the check below -- rendering a
  // confident "0% elapsed" bar on the schema-drift path, the same false line the
  // window line above already omits. Loose `==` catches null and undefined together.
  if (bucket.elapsed_fraction == null) return null;
  const fraction = Number(bucket.elapsed_fraction);
  if (!Number.isFinite(fraction)) return null;
  const left = `${clamp(fraction * 100, 0, 100)}%`;
  return el(
    "div",
    {
      class: `gauge__bar${stale ? " gauge__bar--stale" : ""}`,
      role: "img",
      "aria-label": `${pct(fraction * 100)} of the window elapsed at this reading`,
    },
    [
      el("div", { class: "gauge__bar-elapsed", style: `width:${left}` }),
      el("div", { class: "gauge__bar-marker", style: `left:${left}` }),
    ],
  );
}

function renderGauges(buckets, stale) {
  els.gauges.replaceChildren(...buckets.map((b) => renderGauge(b, stale)));
  tickCountdowns();
}

function tickCountdowns() {
  // Server clock: resets_at comes from the response, so a skewed local clock would
  // shift every countdown on the page by that skew.
  const now = serverNow();
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

/** Summary statistics for one series, shared by its aria-label and the table. */
function seriesStats(points) {
  if (!points.length) return null;
  const values = points.map((p) => p.v);
  return {
    first: points[0],
    last: points[points.length - 1],
    min: Math.min(...values),
    max: Math.max(...values),
    count: points.length,
  };
}

/**
 * What the chart says to a screen reader.
 *
 * It used to say "values are listed in the table view", which was not true --
 * the table carried one current number per bucket and none of the history the
 * chart draws. Rather than pointing somewhere else, the description now carries
 * the numbers itself: for a trend line the informative content is where it
 * starts, where it ends, and its range, and reading out several hundred
 * downsampled points would be worse than useless.
 */
function describeSeries(label, points, isCurrent) {
  const stats = seriesStats(points);
  if (!stats) return `${label}: no samples in the selected window.`;
  const latest = isCurrent ? "now" : "last recorded";
  return (
    `${label}: ${latest} ${pct(stats.last.v)}, ranging ${pct(stats.min)} to ${pct(stats.max)} ` +
    `across ${stats.count} samples from ${formatClock(new Date(stats.first.t).toISOString())} ` +
    `to ${formatClock(new Date(stats.last.t).toISOString())}. ` +
    `The table view lists these figures per bucket.`
  );
}

function renderChart(series, windowStart, windowEnd, currency = {}) {
  const { isCurrent = false, note = "" } = currency;
  const points = (series.points || [])
    .map((p) => ({ t: new Date(p.ts).getTime(), v: Number(p.utilization) }))
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
    .sort((a, b) => a.t - b.t);

  // History keeps a bucket for as long as it has samples in the window, so a
  // bucket the API has stopped reporting still draws a series here after it has
  // correctly disappeared from the gauges. Labelling its final reading "now"
  // presented a value hours or days old as the live one -- the same failure the
  // gauge path guards against, arriving through the other door. Only a bucket
  // present in the current snapshot gets to say "now".
  const newest = points.length ? points[points.length - 1] : null;
  let nowText = "";
  if (newest) {
    nowText = isCurrent
      ? `now ${pct(newest.v)}`
      : `last ${pct(newest.v)} · ${formatAge((serverNow() - newest.t) / 1000)}`;
  }

  const head = el("div", { class: "chart__head" }, [
    el("div", { class: "chart__title", text: series.label || series.key }),
    el("div", {
      class: isCurrent ? "chart__now" : "chart__now chart__now--past",
      text: nowText,
      ...(newest && !isCurrent && note ? { title: note } : {}),
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
    "aria-label": describeSeries(series.label, points, isCurrent),
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

    // Built as nodes, not markup: the label comes from the response body
    // (scope.model.display_name, or a raw JSON key) and is not ours to trust.
    els.tooltip.replaceChildren(
      el("b", { text: pct(nearest.v) }),
      document.createTextNode(" "),
      el("span", { text: `· ${ctx.series.label}` }),
      el("br"),
      el("span", {
        text: new Date(nearest.t).toLocaleString(undefined, {
          weekday: "short",
          hour: "numeric",
          minute: "2-digit",
        }),
      }),
    );
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
    ...buckets.map((bucket) =>
      el("tr", {}, [
        el("td", { text: bucket.label }),
        el("td", { text: pct(bucket.utilization) }),
        // Pace, matching the gauges -- a level word here would contradict them.
        el("td", { text: bucket.pace_label || "Unknown" }),
        el("td", { text: bucket.resets_at ? formatClock(bucket.resets_at) : "—" }),
        el("td", { text: bucket.source || "—" }),
      ]),
    ),
  );
}

/* --------------------------------------------------------------------- hero */

function renderHero(projection, weekly) {
  const status = projection?.status ?? "unavailable";
  els.hero.setAttribute("data-status", status);

  if (status === "projected" || status === "clears_reset") {
    // Item 3: the headline reads as the forecast it is. "Cap at Tue 6:01 AM"
    // asserted a fact and left the conditional buried in the fine print; the
    // "On pace to…" phrasing puts the if back where a reader sees it first. The
    // fine print still carries the evidence: rate, window age, and current %.
    const rate = projection.rate_per_hour;
    els.heroValue.textContent =
      status === "clears_reset"
        ? "On pace to clear the reset"
        : `On pace to hit the cap ${formatClock(projection.hits_cap_at)}`;
    els.heroDetail.textContent =
      `Burning ${rate.toFixed(1)}%/hour since the window opened ` +
      `${formatDuration(projection.elapsed_hours * 3600)} ago; now at ${pct(projection.utilization)}.`;
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

  // One banner for "the numbers may be behind", whether the poller is actively
  // failing or a reading has simply aged past the freshness window. It always names
  // both the cause and how old the reading is, in a single line, so it can never sit
  // as a bare "may be stale" beside a confident "Updated 8s ago".
  //
  // Diagnosis behind the merge: `data.stale` flips true the instant one poll fails
  // (consecutive_failures > 0), even seconds after a success, so the reading is often
  // NOT genuinely old -- the two independent branches let a fresh stamp and a stale
  // banner disagree with no explanation. The fix is to spell the age and cause out
  // together, and (in refresh()) to stop the stamp claiming "Updated" while stale.
  if (status.last_error || data.stale) {
    const failing = Boolean(status.last_error);
    const severity = failing && status.consecutive_failures > 2 ? "error" : "warn";
    const cause = failing
      ? status.last_error
      : `no new reading within the ${Math.round(data.stale_after_seconds)}s freshness window`;
    const title = failing
      ? `Usage fetch failing (${status.consecutive_failures}×)`
      : "Data may be stale";
    // On a fresh install whose very first poll fails there is no reading to age --
    // `formatAge(null)` is "never", so "Last reading never" reads wrong. Name the
    // no-reading-yet state directly, still carrying the cause.
    const lead = age == null ? "No successful reading yet" : `Last reading ${formatAge(age)}`;
    show(severity, title, `${lead} — ${cause}.`);
    els.dot.dataset.state = severity === "error" ? "error" : "stale";
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

/* refresh() is driven by setInterval and does not await the previous call, so once
 * the cadence follows the configured poll interval (as low as 5s) a slow /api/now
 * request can be overtaken by a newer one -- and the older response, landing last,
 * would otherwise overwrite state.now/buckets/readingAt/clockSkewMs and visibly
 * regress the dashboard to older utilization, staleness and projection values.
 *
 * nowRequest is the newest request ISSUED; appliedNow is the newest whose outcome has
 * been rendered. The apply guard tests appliedNow, not nowRequest, and that is the
 * point: a plain `token !== nowRequest` check drops a response merely because a newer
 * request has STARTED, so when every fetch outlasts the cadence -- each response
 * arriving after the next tick has already bumped nowRequest -- every outcome is
 * discarded and the dashboard freezes. Gating on appliedNow applies any response newer
 * than the last one shown while still dropping a genuinely out-of-order (older) one.
 * Both the success and failure paths advance the watermark, so the newest OUTCOME
 * wins: an older success cannot overwrite a newer failure's outage banner, nor the
 * reverse. */
let nowRequest = 0;
let appliedNow = 0;

async function refresh({ history = true } = {}) {
  const token = ++nowRequest;
  els.page.dataset.refreshing = "true";
  try {
    const data = await loadNow();
    if (token <= appliedNow) return;
    appliedNow = token;
    state.now = data;
    state.buckets = data.buckets || [];
    // Re-measured every fetch rather than once, so a clock that gets corrected while
    // the page is open corrects with it. Ignored if unparseable -- a zero skew is the
    // old behaviour, which is right when the clocks agree and no worse when they do not.
    const generated = Date.parse(data.generated_at);
    if (Number.isFinite(generated)) state.clockSkewMs = Date.now() - generated;
    applyRefreshCadence(data.poll_interval_seconds);
    // When the READING was taken, not when we fetched it. A successful fetch of
    // an already-stale snapshot used to stamp this with the browser's clock, so
    // if the backend then went away the outage message read "Stale since 60s ago"
    // over data that was days old -- the fetch succeeding was mistaken for the
    // data being fresh. Null staleness means no reading exists at all, and there
    // is no good moment to record.
    state.readingAt =
      data.staleness_seconds == null ? null : Date.now() - data.staleness_seconds * 1000;

    renderBanner(data, null);
    renderGauges(state.buckets, data.stale === true);
    renderTable(state.buckets);
    renderHero(data.projection, state.buckets.find((b) => b.key === "seven_day"));

    // Never a confident "Updated" while the banner says stale -- that contradiction
    // (a fresh stamp beside a stale banner) is the bug item 4 diagnoses. When stale,
    // the stamp reports the reading's age the same way the outage path does.
    els.freshness.textContent =
      data.staleness_seconds == null
        ? "No reading yet"
        : data.stale
          ? `Stale — last read ${formatAge(data.staleness_seconds)}`
          : `Updated ${formatAge(data.staleness_seconds)}`;
    els.footerMeta.textContent = [
      `Credential source: ${data.status?.credential_source ?? "unknown"}`,
      `poll every ${Math.round(data.poll_interval_seconds)}s`,
      ...(data.status?.notices?.length ? [`${data.status.notices.length} unrecognized bucket(s)`] : []),
    ].join(" · ");

    if (history) await refreshHistory();
  } catch (error) {
    // Strict `<`, not `<=`: tokens are unique per invocation, so token === appliedNow
    // here can only mean this same invocation already applied its success and then a
    // render threw -- fall through and surface the failure loudly rather than swallow
    // it. A genuinely out-of-order (older) failure is token < appliedNow and still drops.
    if (token < appliedNow) return;
    appliedNow = token;
    renderBanner(null, String(error.message || error));
    els.freshness.textContent = state.readingAt
      ? `Stale — last read ${formatAge((Date.now() - state.readingAt) / 1000)}`
      : "Unavailable";
    // The snapshot we are holding is now of unknown age, so say so in the data
    // rather than only in the banner. Marking it here is what stops the chart
    // headings claiming "now" for the whole length of an outage, and it also
    // makes a later range click render honestly from the same cached series.
    // Redrawn from cache deliberately -- refetching is pointless when the
    // backend is what just failed, and the labels recompute their age from the
    // clock, so they keep ageing while the outage lasts.
    if (state.now && !state.now.stale) state.now = { ...state.now, stale: true };
    // Re-render the gauges muted for the same reason the hero is redrawn below: the
    // backend just failed, so the last buckets are now of unknown age and must not keep
    // asserting a live HEALTHY/Watch/Critical judgement over them.
    renderGauges(state.buckets, true);
    if (state.history) renderCharts(state.history);
    // The hero too, and it is the most important of the three: a cap time is the
    // one thing on this page a reader would act on, and the backend deliberately
    // withholds projections from stale readings -- so leaving the last one up
    // during an outage contradicts the server's own judgement and does it in the
    // most consequential place. Marking state.now stale was not enough on its
    // own, because nothing re-rendered here.
    renderHero(
      {
        status: "unavailable",
        message: "The dashboard cannot reach its backend, so the current pace is unknown.",
      },
      state.buckets.find((b) => b.key === "seven_day"),
    );
  } finally {
    // Only the newest request owns the spinner; a stale response clearing it while a
    // newer fetch is still in flight would flicker the indicator off prematurely.
    if (token === nowRequest) delete els.page.dataset.refreshing;
  }
}

/* Identifies the newest history request in flight. Two can overlap -- click 24h
 * then 3d, or click a range while the 60s refresh is already fetching -- and the
 * slower earlier one could land last, so its data became state.history while
 * renderCharts scaled it against the range now selected: a 3-day axis over 24
 * hours of points, with statistics to match. Only the newest response is applied,
 * and it carries its own range so nothing has to infer one. */
let historyRequest = 0;

async function refreshHistory() {
  const token = ++historyRequest;
  const hours = state.hours;
  try {
    const data = await loadHistory(hours);
    if (token !== historyRequest) return;
    state.history = data;
    renderCharts(state.history);
  } catch (error) {
    if (token !== historyRequest) return;
    // The cached payload goes too. Leaving it meant the table kept listing the
    // previous window's figures while the chart beside it said history was
    // unavailable, and the outage path would later redraw those same cached points
    // -- so a failed fetch after a range change left real numbers on screen for a
    // window nobody had asked for.
    state.history = null;
    els.historyBody.replaceChildren(
      el("tr", {}, [
        el("td", { colspan: "6", class: "table__empty", text: "History unavailable." }),
      ]),
    );
    els.charts.replaceChildren(
      el("div", { class: "chart" }, [
        el("div", { class: "chart__empty", text: `History unavailable: ${error.message}` }),
      ]),
    );
  }
}

/* Separated from the fetch so a failed /api/now can redraw the labels from the
 * series already in hand. Without that an outage froze the headings at "now 61%"
 * for as long as it lasted: refresh()'s catch rewrote only the banner, so
 * state.now.stale stayed false and nothing re-evaluated the labels while the data
 * aged. Third route into the same defect, after the gauges and the stale
 * snapshot. */
/**
 * The accessible counterpart to the charts, carrying the same figures their
 * descriptions quote. Summary statistics rather than every point: the history is
 * up to 720 samples per bucket per refresh, and a table of that is not a fallback
 * anyone can read.
 */
function renderHistoryTable(series, isCurrentFor) {
  const rows = [];
  for (const s of series) {
    const points = (s.points || [])
      .map((p) => ({ t: new Date(p.ts).getTime(), v: Number(p.utilization) }))
      .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
      .sort((a, b) => a.t - b.t);
    const stats = seriesStats(points);
    if (!stats) continue;
    const latest = isCurrentFor(s.key)
      ? pct(stats.last.v)
      : `${pct(stats.last.v)} (${formatAge((serverNow() - stats.last.t) / 1000)})`;
    rows.push(
      el("tr", {}, [
        el("td", { text: s.label || s.key }),
        el("td", {
          text: `${formatClock(new Date(stats.first.t).toISOString())} – ${formatClock(
            new Date(stats.last.t).toISOString(),
          )}`,
        }),
        el("td", { text: latest }),
        el("td", { text: pct(stats.min) }),
        el("td", { text: pct(stats.max) }),
        el("td", { text: String(stats.count) }),
      ]),
    );
  }
  els.historyBody.replaceChildren(...rows);
}

function renderCharts(data) {
  // Anchored on the server's clock, since every point below carries a server
  // timestamp. Against a local Date.now() a browser running behind pushed the newest
  // points past the right edge, and one running ahead drew a tail of empty time.
  const end = serverNow();
  // The window comes from the payload, not from state.hours. The backend already
  // reports the range it answered for, and taking it from there means a redraw from
  // cache -- what the outage path does -- cannot scale old points against a range
  // selected since.
  const hours = Number(data.hours) > 0 ? Number(data.hours) : state.hours;
  const start = end - hours * 3600 * 1000;
  const order = new Map(state.buckets.map((b, i) => [b.key, i]));
  // Keys the API is reporting right now. Empty when /api/now has never
  // succeeded, which correctly makes every series read as historical rather
  // than asserting currency we have no basis for.
  const current = new Set(state.buckets.map((b) => b.key));
  // Membership alone is not enough. A stale snapshot still reports its last
  // known buckets -- that is deliberate, so a restart shows real numbers -- so
  // every one of those keys would pass the membership test while the readings
  // behind them are hours or days old. Restore an old database and the whole
  // dashboard would have said "now" over three-day-old values. The banner
  // warning about it is not a licence for the series labels to disagree.
  const snapshotIsCurrent = Boolean(state.now) && !state.now.stale;
  const series = (data.series || []).sort(
    (a, b) => (order.get(a.key) ?? 99) - (order.get(b.key) ?? 99),
  );

  els.charts.replaceChildren(
    ...(series.length
      ? series.map((s) =>
          renderChart(s, start, end, {
            isCurrent: snapshotIsCurrent && current.has(s.key),
            // Two different reasons a label cannot say "now", and a reader
            // needs to know which: the bucket is gone, or the whole snapshot
            // is behind.
            note: current.has(s.key)
              ? "The latest reading is stale, so this is the last value recorded rather than a current one."
              : "No longer reported by the API; this is its last recorded value.",
          }),
        )
      : [
          el("div", { class: "chart" }, [
            el("div", {
              class: "chart__empty",
              text: "No history yet. Samples appear within a minute of the first successful poll.",
            }),
          ]),
        ]),
  );

  renderHistoryTable(series, (key) => snapshotIsCurrent && current.has(key));
}

/** Names the window the charts below actually cover. */
function historyHeading(hours) {
  if (hours % 24 === 0) {
    const days = hours / 24;
    return days === 1 ? "Last 24 hours" : `Last ${days} days`;
  }
  return `Last ${hours} hours`;
}

els.range.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-hours]");
  if (!button) return;
  state.hours = Number(button.dataset.hours);
  for (const other of els.range.querySelectorAll("button")) {
    other.setAttribute("aria-pressed", String(other === button));
  }
  // The heading was written into the markup as "Last 7 days" and never touched
  // again, so selecting 24h left it contradicting the charts and the table beside
  // it. It is also this section's accessible name, so the wrong text was the label
  // a screen reader announced for the whole region.
  els.historyLabel.textContent = historyHeading(state.hours);
  refreshHistory();
});

/* The refresh timer, rescheduled when the backend reports a different cadence than the
 * one currently in effect. A fixed minute meant a faster poll interval -- the config
 * validator accepts sub-second values -- filled the store with readings the page would
 * not show for up to another minute. Rescheduled only on a real change, so the ordinary
 * case sets one interval on the first response and never touches it again. */
let refreshMs = REFRESH_MS_DEFAULT;
let refreshTimer = null;

function applyRefreshCadence(pollIntervalSeconds) {
  const seconds = Number(pollIntervalSeconds);
  const wanted = Number.isFinite(seconds) && seconds > 0
    ? clamp(seconds * 1000, REFRESH_MS_MIN, REFRESH_MS_MAX)
    : REFRESH_MS_DEFAULT;
  if (refreshTimer !== null && wanted === refreshMs) return;
  refreshMs = wanted;
  if (refreshTimer !== null) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refresh(), refreshMs);
}

setInterval(tickCountdowns, 1000);
applyRefreshCadence(null);
refresh();
