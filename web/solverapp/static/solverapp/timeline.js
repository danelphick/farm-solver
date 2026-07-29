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
  getSnapshots,
  historyIndex,
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
// block, a native <title> per point for the tooltip, and no JavaScript needed
// once it is in the DOM.

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

// The Y range a series is drawn in.
//
// The default is 0 up to the next tier threshold above the item's best
// progress, so the chart reads as "progress towards the next tier" with the
// M/GM/MM lines placed meaningfully (past Megamaster there is no next
// threshold, so it takes a little headroom instead). But that only works while
// the series actually spans a useful part of it: when the movement is small,
// the range zooms to the data (padded), and only the tier lines that fall
// inside it are drawn. Either way the axis is labelled with real figures, and
// the table beside it always has the exact numbers.
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

  // A flat series still needs a window to sit in the middle of, hence the
  // floor on the padding.
  const pad = Math.max(span * 0.15, best * 0.0005, 1);
  return { lo: Math.max(least - pad, 0), hi: best + pad };
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

  const points = series
    .map((point, i) => {
      const x = px(times[i]).toFixed(1);
      const y = py(point.progress).toFixed(1);
      const title =
        `${point.date} ${clockTime(point.taken_at)}\n` +
        `${point.progress.toLocaleString()} progress`;
      return `<circle class="pt" cx="${x}" cy="${y}" r="4">` +
        `<title>${escapeHtml(title)}</title></circle>`;
    })
    .join("");

  const path = series
    .map((point, i) => `${px(times[i]).toFixed(1)},${py(point.progress).toFixed(1)}`)
    .join(" ");

  // Dotted reference lines at Master / Grandmaster / Megamaster, drawn only
  // where they actually fall inside the range being plotted.
  const tierLines = TIER_THRESHOLDS.filter(
    (tier) => tier.progress >= lo && tier.progress <= hi
  )
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

  return `<svg class="mastery-graph" viewBox="0 0 ${CHART_WIDTH} ${CHART_HEIGHT}" \
width="100%" height="auto" role="img" \
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
.mastery-graph .pt:hover { r: 7; }
</style>
<line class="axis" x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}" />
<line class="axis" x1="${left}" y1="${top}" x2="${left}" y2="${bottom}" />
${tierLines}
${xTicks}
${yTicks}
<polyline class="line" points="${path}" />
${points}
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
  change: (row) => [row.change, row.item.toLowerCase()],
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

function renderChart(series, item) {
  const holder = $("#timeline-chart");
  holder.innerHTML = chartSvg(series, item);
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
      const gain =
        row.gain === null
          ? "—"
          : `${row.gain > 0 ? "+" : ""}${row.gain.toLocaleString()}`;
      const perDay =
        row.perDay === null
          ? "—"
          : `${row.perDay > 0 ? "+" : ""}${Math.round(row.perDay).toLocaleString()}`;
      const tier = row.tier
        ? `${escapeHtml(row.tier)} <span class="muted-inline">(${escapeHtml(tierLabel(row.tier))})</span>`
        : "—";
      return `<tr><td>${escapeHtml(when)}</td>` +
        `<td class="num">${row.progress.toLocaleString()}</td>` +
        `<td class="num">${gain}</td>` +
        `<td class="num">${perDay}</td>` +
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

// history.historyIndex plus the two figures the table shows on top of it, so
// the sort has them to rank on rather than re-deriving them per comparison.
export function indexRows(snapshots) {
  return historyIndex(snapshots).map((entry) => ({
    ...entry,
    change: entry.latest - entry.first,
    tier: tierForProgress(entry.latest),
  }));
}

function renderIndex(snapshots) {
  renderSortIndicators("#timeline-index-head", indexSort);
  const entries = sortIndexRows(
    indexRows(snapshots),
    indexSort.key,
    indexSort.direction
  );
  $("#timeline-index-body").innerHTML = entries
    .map(
      (entry) => `<tr class="clickable" data-href="${escapeHtml(timelineHref(entry.item))}"` +
        ` title="Timeline for ${escapeHtml(entry.item)}">` +
        `<td>${escapeHtml(entry.item)}</td>` +
        `<td>${escapeHtml(entry.tier || "—")}</td>` +
        `<td class="num">${entry.latest.toLocaleString()}</td>` +
        `<td class="num">${entry.change > 0 ? "+" : ""}${entry.change.toLocaleString()}</td></tr>`
    )
    .join("");
  $("#timeline-index").hidden = entries.length === 0;
}

// --- Page state --------------------------------------------------------------

function setStatus(text, kind) {
  const el = $("#status");
  el.textContent = text;
  el.className = "status" + (kind ? " status-" + kind : "");
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
}

function render() {
  const item = $("#timeline-item").value.trim();
  hideAll();

  if (!snapshots.length) {
    $("#timeline-empty").hidden = false;
    setStatus("No snapshots saved yet.");
    return;
  }
  $("#timeline-empty").hidden = true;

  const span = `${snapshots[0].date} → ${snapshots[snapshots.length - 1].date}`;
  const kept = `${snapshots.length.toLocaleString()} snapshot${snapshots.length === 1 ? "" : "s"}`;

  if (!item) {
    renderIndex(snapshots);
    setStatus(`${kept} · ${span} · pick an item to chart it`, "ok");
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
  const total = series[series.length - 1].progress - series[0].progress;
  setStatus(
    `${item}: ${series.length} of ${kept} · ${series[0].date} → ` +
      `${series[series.length - 1].date} · ${total >= 0 ? "+" : ""}${total.toLocaleString()} progress`,
    "ok"
  );
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
    const row = event.target.closest("tr[data-href]");
    if (row) window.location.href = row.dataset.href;
  });

  load();
}

// `once`: see masteries.js -- the page tests re-import this module against a
// rebuilt DOM and dispatch DOMContentLoaded again.
document.addEventListener("DOMContentLoaded", init, { once: true });
