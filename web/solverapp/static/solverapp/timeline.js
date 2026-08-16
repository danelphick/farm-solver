// Farm Solver mastery-timeline page: how one mastery has moved over time.
//
// The only page that never talks to the solver. Everything it draws comes from
// the dated snapshots history.js keeps in this browser's IndexedDB, written by
// the settings popover's "Save dated snapshot" button -- so there is no request,
// no cache key and no solve here, and the chart is built client-side rather than
// server-side like the solver page's impact graph.
//
// Self-contained rather than an app.js import, for the reason masteries.js
// spells out: app.js registers a DOMContentLoaded -> init() at module scope. It
// shares the modules that register nothing -- the settings popover
// (settings.js), the help tooltips (help.js), the tier labels (tiers.js), the
// item catalogue (catalogue.js) and the history store itself (history.js) --
// and duplicates the handful of one-line helpers.

import { MASTERY_CATALOGUE, loadCatalogue } from "./catalogue.js";
import { initHelpTooltips } from "./help.js";
import {
  TIER_THRESHOLDS,
  changeSince,
  getSnapshots,
  seriesByItem,
  seriesFor,
} from "./history.js";
import {
  STORAGE_KEY,
  applySettingsState,
  initSettings,
  mergeSettingsIntoState,
  readSettingsState,
  readUi,
  writeUi,
} from "./settings.js";
import { tierForProgress, tierLabel } from "./tiers.js";

const DEBOUNCE_MS = 400;

// Below this gap a "per day" figure is meaningless: two snapshots ten minutes
// apart would extrapolate a handful of items into thousands a day.
const MIN_RATE_GAP_MS = 60 * 60 * 1000;

function $(selector) {
  return document.querySelector(selector);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// --- Formatting --------------------------------------------------------------

// Axis labels only: the table always shows the exact figure.
export function compactNumber(value) {
  const n = Math.round(value);
  if (Math.abs(n) >= 1_000_000) {
    const millions = n / 1_000_000;
    return `${Math.abs(millions) >= 10 ? Math.round(millions) : millions.toFixed(1)}M`;
  }
  if (Math.abs(n) >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(n);
}

// A Y-axis tick. Rounded labels ("341k") only while the axis covers enough
// ground for them to differ -- a zoomed range (see chartRange) can be a couple
// of thousand wide, where every tick would round to the same figure.
export function axisLabel(value, span) {
  return span < 10_000 ? Math.round(value).toLocaleString() : compactNumber(value);
}

// A gain in a table cell: "+8,100", "-40", "0" -- or "—" for a figure that
// does not exist rather than one that is zero (no earlier reading to measure
// from, a rate over too short a gap, a window with nothing recorded in it).
function signed(value) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${Math.round(value).toLocaleString()}`;
}

function clockTime(takenAt) {
  const when = new Date(takenAt);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

function shortDate(takenAt) {
  return new Date(takenAt).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  });
}

// --- The chart ---------------------------------------------------------------
//
// A self-contained, theme-aware inline SVG, built the way report._svg_plot
// builds the solver page's impact graph: colours as CSS custom properties
// scoped to the root <svg> and re-themed by one `prefers-color-scheme: dark`
// block. The one thing it needs the DOM for is the point tooltip -- see
// "Point tooltips" below.

const CHART_WIDTH = 640;
const CHART_HEIGHT = 300;
const MARGIN_LEFT = 62;
const MARGIN_RIGHT = 34; // room for the M/GM/MM labels at the line ends
const MARGIN_TOP = 16;
const MARGIN_BOTTOM = 40;

const DAY_MS = 24 * 60 * 60 * 1000;

// Below this fraction of the full 0 -> next-tier range, a series' movement is
// invisible on that scale (a few thousand progress inside a million), so the
// chart zooms to the data instead. A mastery moves by ~1% of a tier a week --
// drawn from zero, the whole page would be one flat line.
const ZOOM_BELOW = 0.2;

// A zoomed window widened out to round bounds: the data's own low and high
// pushed down and up to a whole step, so the axis reads 210k / 280k / 350k
// rather than "data ± 15%" (194k / 273k / 352k, where no figure means anything
// and the eye has to work to see 333,631 in "352k").
//
// The step is the span's SECOND significant place -- 10k across a 121k window,
// 100 across a 3.4k one -- which is what puts a 0 in the bounds' third figure
// while still hugging the data. Deriving it from the span rather than from the
// values is what keeps a small movement high up the scale honest: a series
// running 995,000 → 1,005,000 steps in 1,000, where rounding the values
// themselves would blow the window out to 990k → 1.1M and flatten the line.
//
// The bounds also come out an EVEN number of steps apart, because chartSvg
// labels the midpoint as well as the ends -- otherwise the middle tick lands on
// a half step (a 13-step window would read 210k / 275k / 340k).
export function niceRange(least, best) {
  // Never below 1: progress is a whole number, so a sub-unit step would round
  // to bounds no tidier than the data.
  const step = Math.max(10 ** (Math.floor(Math.log10(best - least)) - 1), 1);
  const lo = Math.floor(least / step) * step;
  let hi = Math.ceil(best / step) * step;
  if (Math.round((hi - lo) / step) % 2) hi += step;
  return { lo: Math.max(lo, 0), hi };
}

// Which tier thresholds the chart draws a reference line for, given the range
// it is plotting: those inside it, minus Master when Grandmaster and Megamaster
// are both there too (see chartSvg).
function visibleTiers(lo, hi) {
  const inside = TIER_THRESHOLDS.filter(
    (tier) => tier.progress >= lo && tier.progress <= hi
  );
  const labels = new Set(inside.map((tier) => tier.tier));
  return labels.has("GM") && labels.has("MM")
    ? inside.filter((tier) => tier.tier !== "M")
    : inside;
}

// The Y range a series is drawn in.
//
// The default is 0 up to the next tier threshold above the item's best
// progress, so the chart reads as "progress towards the next tier" with the
// M/GM/MM lines placed meaningfully (past Megamaster there is no next
// threshold, so it takes a little headroom instead). But that only works while
// the series actually spans a useful part of it: when the movement is small,
// the range zooms to the data (rounded out by niceRange), and only the tier
// lines that fall inside it are drawn. Either way the axis is labelled with
// real figures, and the table beside it always has the exact numbers.
//
// One point is never "movement", so a lone snapshot keeps the full range --
// there is no shape to show, and the tier context is the useful part.
export function chartRange(series) {
  const values = series.map((point) => point.progress);
  const best = Math.max(...values);
  const least = Math.min(...values);
  const next = TIER_THRESHOLDS.find((tier) => tier.progress > best);
  const full = { lo: 0, hi: next ? next.progress : Math.max(best * 1.05, 1) };

  const span = best - least;
  if (series.length < 2 || span >= (full.hi - full.lo) * ZOOM_BELOW) return full;

  // A flat series has no span to round out, so it gets a window of its own to
  // sit in the middle of first.
  if (!span) {
    const pad = Math.max(best * 0.0005, 1);
    return niceRange(least - pad, best + pad);
  }
  return niceRange(least, best);
}

// --- Point tooltips ----------------------------------------------------------
//
// Hovering a point says which snapshot it is: when it was taken and the mastery
// it recorded. A native <title> per circle (what the chart shipped with) is the
// cheap version of this, but it waits on the OS tooltip delay, cannot be themed,
// and a 4px circle is a fiddly thing to hover in the first place -- so the box
// is drawn in the SVG instead, over a hit target several times the size of the
// dot.
//
// The split: `chartSvg` emits the (hidden) tooltip group and a data-carrying
// hit circle per point, `tooltipLines`/`tooltipBox` decide what it says and
// where it goes -- all pure, so the tests pin them as markup and numbers -- and
// `wireChartTooltip` does the only part that needs a live DOM, filling the box
// in and moving it on hover. Each point still carries the same text as an
// `aria-label`, which is what a screen reader gets in place of the hover.

const HIT_RADIUS = 12;
const TIP_LINE_HEIGHT = 14;
const TIP_PAD_X = 8;
const TIP_PAD_Y = 6;
const TIP_GAP = 12; // between the point and the box
const TIP_MARGIN = 2; // between the box and the edge of the viewBox
const TIP_LINES = 2; // one per tooltipLines line

// The lines travel from the markup to the hover as one attribute, so they need
// a separator that survives being one -- a newline does not: XML normalises it
// to a space (which is how the SVG is parsed when a test reads chartSvg back
// through DOMParser, even though innerHTML in the page keeps it). No tooltip
// line can contain a pipe: they are a date and a formatted number.
const TIP_SEPARATOR = "|";

// Nothing here can measure text -- chartSvg is pure markup and the page tests
// run in jsdom, which has no getBBox -- so the box is sized off the longest
// line at roughly the average character width of the chart's 11px font.
const TIP_CHAR_PX = 6.2;

// What one point's tooltip says: when the snapshot was taken and what it
// recorded. Not the tier -- the chart already draws the M/GM/MM lines, and the
// table below spells it out per row.
export function tooltipLines(point) {
  return [
    `${point.date} ${clockTime(point.taken_at)}`,
    `${point.progress.toLocaleString()} mastery`,
  ];
}

// Where the box sits for a point at `(x, y)`: above it and centred on it by
// preference, nudged back inside the viewBox when that would clip it (a point
// at either end of the series), and flipped below the point when there is no
// room above (a point near the top of the range, which is most of them once a
// series is climbing).
export function tooltipBox(lines, x, y) {
  const longest = Math.max(...lines.map((line) => line.length));
  const width = Math.round(2 * TIP_PAD_X + longest * TIP_CHAR_PX);
  const height = 2 * TIP_PAD_Y + lines.length * TIP_LINE_HEIGHT;
  const left = Math.min(
    Math.max(x - width / 2, TIP_MARGIN),
    CHART_WIDTH - width - TIP_MARGIN
  );
  const above = y - TIP_GAP - height;
  return {
    x: Math.round(left),
    y: Math.round(above >= TIP_MARGIN ? above : y + TIP_GAP),
    width,
    height,
  };
}

// The hidden box itself, drawn last so it is over every point rather than under
// the ones plotted after it. Its text lines are filled in on hover.
function tooltipMarkup() {
  const lines = Array.from(
    { length: TIP_LINES },
    (_, i) =>
      `<text class="tip-line" x="${TIP_PAD_X}" ` +
      `y="${TIP_PAD_Y + 10 + i * TIP_LINE_HEIGHT}"></text>`
  ).join("");
  return `<g class="tip" opacity="0" aria-hidden="true">` +
    `<rect class="tip-box" x="0" y="0" width="0" height="0" rx="4" ry="4" />` +
    `${lines}</g>`;
}

// `series` is history.seriesFor's output (oldest first, at least one point).
// Pure -- returns markup, touches no DOM -- so timeline.test.js can pin the
// geometry directly.
export function chartSvg(series, item) {
  const { lo, hi } = chartRange(series);
  const left = MARGIN_LEFT;
  const right = CHART_WIDTH - MARGIN_RIGHT;
  const top = MARGIN_TOP;
  const bottom = CHART_HEIGHT - MARGIN_BOTTOM;

  const times = series.map((point) => new Date(point.taken_at).getTime());
  let tLo = Math.min(...times);
  let tHi = Math.max(...times);
  if (tHi === tLo) {
    // One snapshot, or several at the same instant: a zero-wide range has no
    // scale, so give it a day either side and let the point sit in the middle.
    tLo -= DAY_MS;
    tHi += DAY_MS;
  }

  // Time-proportional, from the timestamps rather than the day index: two
  // snapshots an hour apart must sit next to each other, and a fortnight's gap
  // must read as a fortnight.
  const px = (t) => left + ((t - tLo) / (tHi - tLo)) * (right - left);
  const py = (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top);

  // Each point is the visible dot plus an invisible, much larger hit circle
  // carrying its tooltip text -- the dot alone is a 4px target, and the hover
  // has to be forgiving enough to be worth having.
  const points = series
    .map((point, i) => {
      const x = px(times[i]).toFixed(1);
      const y = py(point.progress).toFixed(1);
      const lines = tooltipLines(point);
      return `<g class="pt-group" role="img" ` +
        `aria-label="${escapeHtml(lines.join(", "))}">` +
        `<circle class="pt" cx="${x}" cy="${y}" r="4" />` +
        `<circle class="pt-hit" cx="${x}" cy="${y}" r="${HIT_RADIUS}" ` +
        `data-x="${x}" data-y="${y}" data-tip="${escapeHtml(lines.join(TIP_SEPARATOR))}" />` +
        `</g>`;
    })
    .join("");

  const path = series
    .map((point, i) => `${px(times[i]).toFixed(1)},${py(point.progress).toFixed(1)}`)
    .join(" ");

  // Dotted reference lines at Master / Grandmaster / Megamaster, drawn only
  // where they actually fall inside the range being plotted -- and Master is
  // dropped once both of the others are up: on a scale that reaches Megamaster
  // it sits at 1% of the height, a line crushed against the axis that says
  // nothing the Grandmaster line above it does not already say.
  const tierLines = visibleTiers(lo, hi)
    .map((tier) => {
      const y = py(tier.progress).toFixed(1);
      return `<line class="tier" x1="${left}" y1="${y}" x2="${right}" y2="${y}" />` +
        `<text class="tier-label" x="${right + 4}" y="${y}" dy="0.32em">${tier.tier}</text>`;
    })
    .join("");

  const xTicks = [tLo, (tLo + tHi) / 2, tHi]
    .map(
      (t) =>
        `<text class="muted" x="${px(t).toFixed(1)}" y="${bottom + 16}" ` +
        `text-anchor="middle">${escapeHtml(shortDate(t))}</text>`
    )
    .join("");
  const yTicks = [lo, (lo + hi) / 2, hi]
    .map(
      (v) =>
        `<text class="muted" x="${left - 8}" y="${(py(v) + 4).toFixed(1)}" ` +
        `text-anchor="end">${escapeHtml(axisLabel(v, hi - lo))}</text>`
    )
    .join("");

  // No `height` attribute: it is a presentation attribute wanting a length, so
  // "auto" logs an SVG parse error in the console. The aspect ratio comes from
  // the viewBox and style.css's `.timeline-chart svg { height: auto }` anyway.
  return `<svg class="mastery-graph" viewBox="0 0 ${CHART_WIDTH} ${CHART_HEIGHT}" \
width="100%" role="img" \
aria-label="${escapeHtml(item)} mastery progress over time" \
xmlns="http://www.w3.org/2000/svg">
<style>
.mastery-graph {
  --mg-surface: #fcfcfb;
  --mg-ink: #0b0b0b;
  --mg-muted: #898781;
  --mg-axis: #c3c2b7;
  --mg-accent: #2a78d6;
  font: 11px system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  .mastery-graph {
    --mg-surface: #1a1a19;
    --mg-ink: #ffffff;
    --mg-axis: #383835;
    --mg-accent: #3987e5;
  }
}
.mastery-graph .axis { stroke: var(--mg-axis); stroke-width: 1; }
.mastery-graph .tier {
  stroke: var(--mg-axis);
  stroke-width: 1;
  stroke-dasharray: 3 3;
}
.mastery-graph text { fill: var(--mg-ink); }
.mastery-graph .muted, .mastery-graph .tier-label { fill: var(--mg-muted); }
.mastery-graph .line {
  fill: none;
  stroke: var(--mg-accent);
  stroke-width: 2;
  stroke-linejoin: round;
}
.mastery-graph .pt {
  fill: var(--mg-accent);
  stroke: var(--mg-surface);
  stroke-width: 2;
  transition: r 120ms ease;
}
/* The hit circle sits over the dot, so the grow-on-hover has to hang off the
   group rather than off .pt itself, which never sees the pointer. */
.mastery-graph .pt-hit { fill: transparent; stroke: none; }
.mastery-graph .pt-group:hover .pt { r: 7; }
.mastery-graph .tip {
  pointer-events: none;
  transition: opacity 120ms ease;
}
.mastery-graph .tip-box {
  fill: var(--mg-surface);
  stroke: var(--mg-axis);
  stroke-width: 1;
}
.mastery-graph .tip-line { fill: var(--mg-ink); }
</style>
<line class="axis" x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}" />
<line class="axis" x1="${left}" y1="${top}" x2="${left}" y2="${bottom}" />
${tierLines}
${xTicks}
${yTicks}
<polyline class="line" points="${path}" />
${points}
${tooltipMarkup()}
</svg>`;
}

// --- The delta table ---------------------------------------------------------

// One table row per snapshot, newest first: what the item stood at, what it
// gained since the previous snapshot that mentioned it, and what that works out
// at per day. Pure, given the series.
export function deltaRows(series) {
  // How many snapshots share each day, so the Date cell only spells out the
  // time when a day holds more than one.
  const perDay = new Map();
  series.forEach((point) => perDay.set(point.date, (perDay.get(point.date) || 0) + 1));

  return series
    .map((point, i) => {
      const previous = i > 0 ? series[i - 1] : null;
      const gain = previous ? point.progress - previous.progress : null;
      const elapsed = previous
        ? new Date(point.taken_at) - new Date(previous.taken_at)
        : 0;
      return {
        taken_at: point.taken_at,
        date: point.date,
        showTime: perDay.get(point.date) > 1,
        progress: point.progress,
        gain,
        // Only where the gap is long enough to divide by honestly.
        perDay:
          gain !== null && elapsed >= MIN_RATE_GAP_MS
            ? (gain * DAY_MS) / elapsed
            : null,
        tier: tierForProgress(point.progress),
      };
    })
    .reverse();
}

// --- Sorting -----------------------------------------------------------------
//
// Both tables sort per column, the way the mastery-costs table does
// (masteries.sortRows, duplicated here rather than imported for the reason at
// the top of this file): clicking a column sorts on it, clicking it again
// reverses, and the choice is remembered in the shared UI key.
//
// Each column names the values a row is ranked on, primary first and the rest
// as tie-breakers. Tier ranks on the *progress* it was derived from, not on the
// M/GM/MM label: the tier is that figure coarsened (tiers.tierForProgress), so
// ranking on the label would leave the rows inside a tier in an arbitrary
// order. Sorting by Tier therefore reads exactly like sorting by the mastery
// itself, with the tier column showing where the boundaries fall.
const INDEX_SORTS = {
  item: (row) => [row.item.toLowerCase()],
  latest: (row) => [row.latest, row.item.toLowerCase()],
  change7: (row) => [row.change7, row.item.toLowerCase()],
  change1: (row) => [row.change1, row.item.toLowerCase()],
  tier: (row) => [row.latest, row.item.toLowerCase()],
};

const SERIES_SORTS = {
  date: (row) => [row.taken_at],
  progress: (row) => [row.progress],
  gain: (row) => [row.gain, row.taken_at],
  perday: (row) => [row.perDay, row.taken_at],
  tier: (row) => [row.progress],
};

export const INDEX_SORT_COLUMNS = Object.keys(INDEX_SORTS);
export const SERIES_SORT_COLUMNS = Object.keys(SERIES_SORTS);

function compareValues(a, b) {
  if (typeof a === "string" || typeof b === "string") {
    return String(a).localeCompare(String(b));
  }
  return a - b;
}

// `sign` reverses real values only: a missing figure (the oldest row's gain, a
// rate over too short a gap -- both "—" in the table) is not a small number, it
// is no number, so it stays at the bottom whichever way the column points.
function compareAxes(left, right, sign) {
  for (let axis = 0; axis < left.length; axis += 1) {
    const a = left[axis];
    const b = right[axis];
    if (a === null || b === null) {
      if (a === b) continue;
      return a === null ? 1 : -1;
    }
    const order = compareValues(a, b);
    if (order) return sign * order;
  }
  return 0;
}

// Returns a new array, leaving the caller's order alone. Rows equal on every
// axis keep that order whichever direction is asked for -- like
// masteries.sortRows, so a re-sort never shuffles ties.
function sortRows(rows, axesOf, direction) {
  const sign = direction === "desc" ? -1 : 1;
  return rows
    .map((row, i) => ({ row, i }))
    .sort((a, b) => compareAxes(axesOf(a.row), axesOf(b.row), sign) || a.i - b.i)
    .map((entry) => entry.row);
}

export function sortIndexRows(rows, key, direction = "asc") {
  return sortRows(rows, INDEX_SORTS[key] || INDEX_SORTS.item, direction);
}

export function sortSeriesRows(rows, key, direction = "asc") {
  return sortRows(rows, SERIES_SORTS[key] || SERIES_SORTS.date, direction);
}

// The active sort of each table: display state over data that is already
// loaded, restored from the UI key in init(). The defaults are the orders the
// page had before the columns sorted -- items A-Z, snapshots newest first.
const indexSort = { key: "item", direction: "asc" };
const seriesSort = { key: "date", direction: "desc" };

const SORT_ARROWS = { asc: "▲", desc: "▼", none: "↕" };

// Point each header button's arrow at the current sort (and tell a screen
// reader the same thing through the th's aria-sort).
function renderSortIndicators(head, sort) {
  document.querySelectorAll(`${head} th`).forEach((th) => {
    const button = th.querySelector(".sort-btn");
    if (!button) return;
    const active = button.dataset.sort === sort.key;
    th.setAttribute(
      "aria-sort",
      active ? (sort.direction === "desc" ? "descending" : "ascending") : "none"
    );
    button.classList.toggle("active", active);
    button.querySelector(".sort-arrow").textContent = active
      ? SORT_ARROWS[sort.direction]
      : SORT_ARROWS.none;
  });
}

// The live half of the point tooltip: one box, filled in and moved to whichever
// point the pointer is on. One shared box rather than one per point, so nothing
// has to be re-stacked to keep it above the dots plotted after it.
//
// Listeners are delegated on the <svg>, which renderChart replaces wholesale, so
// they go with it -- there is nothing to unbind when the chart is redrawn.
export function wireChartTooltip(svg) {
  const tip = svg.querySelector(".tip");
  const box = tip.querySelector(".tip-box");
  const texts = [...tip.querySelectorAll(".tip-line")];

  const show = (hit) => {
    const lines = (hit.getAttribute("data-tip") || "").split(TIP_SEPARATOR);
    texts.forEach((text, i) => {
      text.textContent = lines[i] || "";
    });
    const at = tooltipBox(
      lines,
      Number(hit.getAttribute("data-x")),
      Number(hit.getAttribute("data-y"))
    );
    box.setAttribute("width", String(at.width));
    box.setAttribute("height", String(at.height));
    tip.setAttribute("transform", `translate(${at.x}, ${at.y})`);
    tip.setAttribute("opacity", "1");
  };

  svg.addEventListener("mouseover", (event) => {
    const hit = event.target.closest(".pt-hit");
    if (hit) show(hit);
  });
  // On the way out of a point, and again on the way out of the chart: a pointer
  // leaving fast enough can skip the point's own mouseout.
  svg.addEventListener("mouseout", (event) => {
    if (event.target.closest(".pt-hit")) tip.setAttribute("opacity", "0");
  });
  svg.addEventListener("mouseleave", () => tip.setAttribute("opacity", "0"));
}

function renderChart(series, item) {
  const holder = $("#timeline-chart");
  holder.innerHTML = chartSvg(series, item);
  wireChartTooltip(holder.querySelector("svg"));
  holder.hidden = false;
}

function renderTable(series) {
  renderSortIndicators("#timeline-head", seriesSort);
  const rows = sortSeriesRows(deltaRows(series), seriesSort.key, seriesSort.direction);
  $("#timeline-body").innerHTML = rows
    .map((row) => {
      const when = row.showTime
        ? `${row.date} ${clockTime(row.taken_at)}`
        : row.date;
      const tier = row.tier
        ? `${escapeHtml(row.tier)} <span class="muted-inline">(${escapeHtml(tierLabel(row.tier))})</span>`
        : "—";
      return `<tr><td>${escapeHtml(when)}</td>` +
        `<td class="num">${row.progress.toLocaleString()}</td>` +
        `<td class="num">${signed(row.gain)}</td>` +
        `<td class="num">${signed(row.perDay)}</td>` +
        `<td>${tier}</td></tr>`;
    })
    .join("");
  $("#timeline-section").hidden = rows.length === 0;
}

// The no-item view: everything with any history, each row linking to its own
// timeline. Same shape as the mastery-costs table (clickable rows carrying
// their own href, one delegated listener on the stable tbody).
export function timelineHref(item) {
  return "/timeline?item=" + encodeURIComponent(item);
}

// --- The next goal for an item ------------------------------------------------
//
// Each index row links to the solver page seeded with the obvious next thing to
// do for that item: the tier above where it stands now.

const NEXT_TIER = { "": "M", M: "GM", GM: "MM" };

// Past Megamaster there is no next tier -- every threshold is behind you -- so
// the goal becomes a plain "make this many more", which is the only thing left
// that still means anything.
export const MEGAMASTERED_GOAL = 10_000;

// The solver page, seeded (see app.js's parseSeededGoal): a mastery goal while
// there is a tier left to reach, otherwise an item count. Both are the same
// "Item:Amount" spec the goal rows use, and a tier token survives as free text
// -- parsing.parse_amount reads M/GM/MM as the absolute 10k/100k/1M threshold.
export function nextGoalHref(item, tier) {
  const next = NEXT_TIER[tier];
  return next
    ? "/?mastery=" + encodeURIComponent(`${item}:${next}`)
    : "/?items=" + encodeURIComponent(`${item}:${MEGAMASTERED_GOAL}`);
}

// Short enough to sit beside every item name without crowding it. Both forms
// read as "towards this": an item goal is a target to HOLD that many, which
// the solver meets out of inventory first (its Goal table spells out have /
// target / still needed), so "+10,000" would overstate what it plans.
export function nextGoalLabel(tier) {
  const next = NEXT_TIER[tier];
  return next ? `→ ${next}` : `→ ${MEGAMASTERED_GOAL.toLocaleString()}`;
}

export function nextGoalTitle(item, tier) {
  const next = NEXT_TIER[tier];
  return next
    ? `Plan ${item} to ${tierLabel(next)}`
    : `Plan ${MEGAMASTERED_GOAL.toLocaleString()} ${item}`;
}

// The index table's rows: one per item with history, carrying every figure the
// table shows, so the sort ranks on values rather than re-deriving them per
// comparison.
//
// The two Change columns are **windowed**, not "since your first ever
// snapshot": what an item gained over the last week and the last day
// (history.changeSince, which returns null -- "—" in the table -- for a window
// it cannot answer). Windows are measured from `now`, so a history that stopped
// a fortnight ago reads as "—" rather than claiming a week of no progress.
export const CHANGE_WINDOWS = [
  { key: "change7", days: 7 },
  { key: "change1", days: 1 },
];

export function indexRows(snapshots, now = new Date()) {
  const cutoffs = CHANGE_WINDOWS.map((window) => ({
    key: window.key,
    at: new Date(now.getTime() - window.days * DAY_MS).toISOString(),
  }));
  return seriesByItem(snapshots).map(({ item, points }) => {
    const latest = points[points.length - 1].progress;
    const row = { item, latest, tier: tierForProgress(latest) };
    cutoffs.forEach((cutoff) => {
      row[cutoff.key] = changeSince(points, cutoff.at);
    });
    return row;
  });
}

// --- Filters -----------------------------------------------------------------
//
// Two of them, both over the index only (a chart of one item has nothing to
// filter): which tiers to list, and -- the interesting one -- "made from".
//
// The catalogue's `uses` maps a material to the items whose recipe consumes it
// DIRECTLY. Walking it transitively is what makes the filter useful: a pile of
// Straw is not just Twine and Brooms, it is Rope (via Twine), and Fishing Nets
// (via Rope), and so on down the chain. Several materials union, so a zone's
// whole drop list answers "what could a trip there turn into?".

// Every item reachable from `seeds` by following `uses`, as a Set of
// lower-cased names. Breadth-first with a seen set, so a recipe graph with a
// cycle in it (there is none today, but nothing here guarantees that) still
// terminates. The seeds themselves are only included if something makes them
// out of another seed.
export function transitiveConsumers(uses, seeds) {
  const reachable = new Set();
  const queue = [...seeds];
  while (queue.length) {
    const material = queue.shift();
    for (const consumer of uses[material] || []) {
      if (reachable.has(consumer)) continue;
      reachable.add(consumer);
      queue.push(consumer);
    }
  }
  return reachable;
}

// `tiers` is the set of tier tokens to keep ("" being not-yet-Master);
// `allowed` is null when no material filter is on, else the reachable set.
export function filterIndexRows(rows, { tiers, allowed }) {
  return rows.filter(
    (row) =>
      tiers.has(row.tier) &&
      (allowed === null || allowed.has(row.item.toLowerCase()))
  );
}

// The recipe graph and zone drops (GET /api/catalogue), or an empty catalogue
// while it is still loading -- or for good, if it fails. The tier filter and
// everything else on the page work without it; only "made from" needs it.
let catalogue = { uses: {}, names: {}, zones: [] };

// The materials the filter is seeded with, lower-cased, in the order added.
let materials = [];

const TIER_BOXES = [
  { selector: "#tier-none", tier: "" },
  { selector: "#tier-m", tier: "M" },
  { selector: "#tier-gm", tier: "GM" },
  { selector: "#tier-mm", tier: "MM" },
];

function tierFilter() {
  return new Set(
    TIER_BOXES.filter((box) => $(box.selector).checked).map((box) => box.tier)
  );
}

function displayName(key) {
  return catalogue.names[key] || key;
}

// One removable chip per material, plus the Clear all button the markup keeps
// inside the same box (so the row disappears with them).
function renderChips() {
  const holder = $("#material-chips");
  holder.querySelectorAll(".chip").forEach((chip) => chip.remove());
  const clear = $("#material-clear");
  materials.forEach((key) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = displayName(key);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn-delete";
    remove.dataset.material = key;
    remove.title = `Remove ${displayName(key)}`;
    remove.setAttribute("aria-label", `Remove ${displayName(key)}`);
    remove.textContent = "×";
    chip.appendChild(remove);
    holder.insertBefore(chip, clear);
  });
  holder.hidden = materials.length === 0;
}

// What the pane says about itself while collapsed.
function renderFilterSummary(shown, total) {
  const parts = [];
  if (shown !== total) parts.push(`${shown.toLocaleString()} of ${total.toLocaleString()}`);
  if (materials.length) {
    parts.push(
      materials.length === 1
        ? `from ${displayName(materials[0])}`
        : `from ${materials.length} materials`
    );
  }
  $("#filter-summary").textContent = parts.length ? ` (${parts.join(", ")})` : "";
}

function setMaterials(next) {
  materials = next;
  renderChips();
  writeUi({ timeline_materials: materials });
  render();
}

function addMaterials(keys) {
  const known = new Set(materials);
  // Only materials the recipe graph knows: anything else could never bring a
  // row in, so a chip for it would be a filter that silently does nothing.
  // (Which is most of a zone's drop list -- chest items, fish, gemstones.)
  const additions = keys
    .map((key) => key.trim().toLowerCase())
    .filter((key) => key && catalogue.uses[key] && !known.has(key));
  if (!additions.length) return false;
  setMaterials([...materials, ...new Set(additions)]);
  return true;
}

function renderIndex(snapshots) {
  renderSortIndicators("#timeline-index-head", indexSort);
  const all = indexRows(snapshots);
  const entries = sortIndexRows(
    filterIndexRows(all, {
      tiers: tierFilter(),
      allowed: materials.length
        ? transitiveConsumers(catalogue.uses, materials)
        : null,
    }),
    indexSort.key,
    indexSort.direction
  );
  renderFilterSummary(entries.length, all.length);
  $("#timeline-index-body").innerHTML = entries
    .map(
      (entry) => `<tr class="clickable" data-href="${escapeHtml(timelineHref(entry.item))}"` +
        ` title="Timeline for ${escapeHtml(entry.item)}">` +
        `<td>${escapeHtml(entry.item)}` +
        ` <a class="solver-link" href="${escapeHtml(nextGoalHref(entry.item, entry.tier))}"` +
        ` title="${escapeHtml(nextGoalTitle(entry.item, entry.tier))}">` +
        `${escapeHtml(nextGoalLabel(entry.tier))}</a></td>` +
        `<td>${escapeHtml(entry.tier || "—")}</td>` +
        `<td class="num">${entry.latest.toLocaleString()}</td>` +
        CHANGE_WINDOWS.map(
          (window) => `<td class="num">${signed(entry[window.key])}</td>`
        ).join("") +
        `</tr>`
    )
    .join("");
  $("#timeline-index").hidden = entries.length === 0;
  return { shown: entries.length, total: all.length };
}

// --- Page state --------------------------------------------------------------

// Unlike the other two pages, this box only speaks up when something is off --
// an item no snapshot mentions, filters that match nothing, a history store
// that will not read. Called with no text for the ordinary cases (a chart, a
// listed index), where the page already shows what it has and a box repeating
// the count in the corner is just noise.
function setStatus(text, kind) {
  const el = $("#status");
  el.textContent = text || "";
  el.className = "status" + (kind ? " status-" + kind : "");
  el.hidden = !text;
}

// Every snapshot, loaded once per page load (and again after a settings save,
// which may have recorded a new one).
let snapshots = [];

// The series the delta table is currently showing, kept so a sort click can
// redraw that table alone -- re-running render() would rebuild the chart too,
// and the chart is the one thing on this page a sort does not touch.
let shownSeries = [];

function hideAll() {
  $("#timeline-chart").hidden = true;
  $("#timeline-section").hidden = true;
  $("#timeline-index").hidden = true;
  $("#filter-details").hidden = true;
}

function render() {
  const item = $("#timeline-item").value.trim();
  hideAll();

  if (!snapshots.length) {
    // #timeline-empty says this at length, and says what to do about it.
    $("#timeline-empty").hidden = false;
    setStatus("");
    return;
  }
  $("#timeline-empty").hidden = true;

  if (!item) {
    // The filters belong to the index, so they appear with it -- and stay put
    // even when they have filtered every row away, or there would be no way to
    // relax them again.
    $("#filter-details").hidden = false;
    const { shown, total } = renderIndex(snapshots);
    setStatus(
      shown
        ? ""
        : `Nothing matches these filters · ${total.toLocaleString()} items with history`,
      "error"
    );
    return;
  }

  const series = seriesFor(snapshots, item);
  if (!series.length) {
    setStatus(`No snapshot mentions “${item}”.`, "error");
    return;
  }
  shownSeries = series;
  renderChart(series, item);
  renderTable(series);
  setStatus("");
}

// Selecting an item rewrites the query string in place, so the URL stays the
// shareable/bookmarkable "/timeline?item=X" the mastery-costs page links to --
// without a reload (there is nothing to fetch; the snapshots are already here).
function selectItem(item) {
  const url = new URL(window.location.href);
  if (item) url.searchParams.set("item", item);
  else url.searchParams.delete("item");
  window.history.replaceState(null, "", url);
  render();
}

async function load() {
  try {
    snapshots = await getSnapshots();
  } catch (err) {
    snapshots = [];
    setStatus("Could not read this browser's history: " + err.message, "error");
    return;
  }
  render();
}

// --- Saved state -------------------------------------------------------------
//
// This page has no solver state of its own, but it carries the shared settings
// popover, so a save here must write the same localStorage the other two pages
// read (keeping the solver page's goal, which the popover does not show).

function readSavedState() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch (err) {
    return null; // corrupt/unavailable storage: as good as nothing pasted
  }
}

function saveSettings() {
  const state = mergeSettingsIntoState(readSavedState(), readSettingsState());
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (err) {
    // Private browsing / quota exceeded: the panel's values are still live in
    // the DOM, they just won't outlast this page.
  }
  // The popover holds the snapshot button, so a visit to it may have added a
  // snapshot -- re-read rather than assuming the history is unchanged.
  load();
}

// --- Wiring ------------------------------------------------------------------

// Fill the "made from" picker and the zone list from the fetched catalogue.
// Zones are grouped by kind and identified by their INDEX, not their name: one
// name can be both an exploring location and a fishing zone with different
// drops (Sinking Swamp), so a name alone does not identify one.
function renderCatalogueControls() {
  const datalist = $("#material-catalogue");
  const fragment = document.createDocumentFragment();
  Object.keys(catalogue.uses)
    .map(displayName)
    .sort((a, b) => a.localeCompare(b))
    .forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      fragment.appendChild(option);
    });
  datalist.appendChild(fragment);

  const select = $("#zone-select");
  const groups = new Map();
  catalogue.zones.forEach((zone, index) => {
    if (!groups.has(zone.kind)) {
      const group = document.createElement("optgroup");
      group.label = zone.kind === "fishing" ? "Fishing" : "Exploring";
      groups.set(zone.kind, group);
      select.appendChild(group);
    }
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = zone.name;
    groups.get(zone.kind).appendChild(option);
  });
}

async function loadCraftingCatalogue() {
  try {
    const response = await fetch("/api/catalogue");
    if (!response.ok) return;
    const payload = await response.json();
    // Keep the empty catalogue unless the response really is one: a page whose
    // filter is a nicety must not break on a truncated or unexpected body.
    if (!payload || typeof payload.uses !== "object") return;
    catalogue = {
      uses: payload.uses || {},
      names: payload.names || {},
      zones: payload.zones || [],
    };
  } catch (err) {
    return; // best-effort: the tier filter and the rest of the page still work
  }
  renderCatalogueControls();
  // Materials restored from a previous visit could not be checked against the
  // graph (or shown by their display name) until now.
  const restored = materials;
  materials = [];
  addMaterials(restored.map(displayName));
  renderChips();
}

function wireFilters() {
  const saved = readUi();
  TIER_BOXES.forEach((box) => {
    if (typeof saved[`timeline_tier_${box.tier || "none"}`] === "boolean") {
      $(box.selector).checked = saved[`timeline_tier_${box.tier || "none"}`];
    }
    $(box.selector).addEventListener("change", () => {
      writeUi({ [`timeline_tier_${box.tier || "none"}`]: $(box.selector).checked });
      render();
    });
  });
  if (Array.isArray(saved.timeline_materials)) materials = saved.timeline_materials;

  const addTyped = () => {
    if (addMaterials([$("#material-input").value])) $("#material-input").value = "";
  };
  $("#material-add").addEventListener("click", addTyped);
  // Enter in the box adds, rather than doing nothing (there is no form here).
  $("#material-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addTyped();
    }
  });

  $("#zone-add").addEventListener("click", () => {
    const zone = catalogue.zones[Number($("#zone-select").value)];
    if (zone) addMaterials(zone.items);
  });

  $("#material-clear").addEventListener("click", () => setMaterials([]));
  // Delegated on the (stable) chip box, which renderChips refills.
  $("#material-chips").addEventListener("click", (event) => {
    const remove = event.target.closest("button[data-material]");
    if (remove) setMaterials(materials.filter((key) => key !== remove.dataset.material));
  });
}

// Sorting is pure display: reorder rows already drawn from history already
// loaded, so a click redraws that one table and nothing else. Delegated on the
// header row, so every column's button is one listener.
function wireSort(head, sort, columns, uiKeys, redraw) {
  const saved = readUi();
  if (columns.includes(saved[uiKeys.key])) sort.key = saved[uiKeys.key];
  if (saved[uiKeys.dir] === "asc" || saved[uiKeys.dir] === "desc") {
    sort.direction = saved[uiKeys.dir];
  }

  $(head).addEventListener("click", (event) => {
    const button = event.target.closest("button[data-sort]");
    if (!button) return;
    // Clicking the column already sorted on flips it; any other column starts
    // ascending (A-Z, smallest first, lowest tier first).
    sort.direction =
      button.dataset.sort === sort.key && sort.direction === "asc" ? "desc" : "asc";
    sort.key = button.dataset.sort;
    writeUi({ [uiKeys.key]: sort.key, [uiKeys.dir]: sort.direction });
    redraw();
  });
}

function init() {
  initSettings({ onSave: saveSettings });
  applySettingsState(readSavedState());
  initHelpTooltips();
  loadCatalogue(MASTERY_CATALOGUE, "mastery-catalogue");

  $("#timeline-item").value =
    new URLSearchParams(window.location.search).get("item") || "";

  const onItemChange = debounce(() => selectItem($("#timeline-item").value.trim()), DEBOUNCE_MS);
  $("#timeline-item").addEventListener("input", onItemChange);

  // Wired before the catalogue arrives (it restores the saved materials, which
  // loadCraftingCatalogue then re-checks against the graph); the fetch is not
  // awaited, so history still renders immediately if the request is slow.
  wireFilters();
  loadCraftingCatalogue().then(render);

  wireSort(
    "#timeline-index-head",
    indexSort,
    INDEX_SORT_COLUMNS,
    { key: "timeline_index_sort", dir: "timeline_index_sort_dir" },
    () => renderIndex(snapshots)
  );
  wireSort(
    "#timeline-head",
    seriesSort,
    SERIES_SORT_COLUMNS,
    { key: "timeline_sort", dir: "timeline_sort_dir" },
    () => renderTable(shownSeries)
  );

  // Delegated on the (stable) tbody, which renderIndex replaces wholesale. A
  // plain navigation, so the browser's Back button returns to the index.
  $("#timeline-index-body").addEventListener("click", (event) => {
    // The Item cell carries its own next-goal link; let the anchor navigate
    // rather than hijacking the click for the row's own destination.
    if (event.target.closest("a")) return;
    const row = event.target.closest("tr[data-href]");
    if (row) window.location.href = row.dataset.href;
  });

  load();
}

// `once`: see masteries.js -- the page tests re-import this module against a
// rebuilt DOM and dispatch DOMContentLoaded again.
document.addEventListener("DOMContentLoaded", init, { once: true });
