// Farm Solver mastery-costs page: the browser face of `--mastery-costs`.
//
// Deliberately self-contained rather than an app.js import: app.js registers a
// DOMContentLoaded -> init() listener at module scope, so importing it here
// would run the solver page's whole wiring against markup that isn't on this
// page. What *is* shared lives in modules that register nothing: the settings
// popover (settings.js), the help tooltips (help.js) and the tier labels
// (tiers.js). The handful of one-line helpers below are duplicated instead.
//
// It has no goal of its own to solve for: the pasted masteries/inventory and
// every parameter (prices, preference panels, AP trading, ...) live in the
// settings popover this page shares with the solver page (settings.js, wired
// over the same localStorage), so the two always agree on what "your save"
// means. Saving the popover here writes that shared state -- keeping the solver
// page's goal, which the popover does not show (mergeSettingsIntoState) -- and
// re-requests. The only thing this page adds is `self_sourced`.
//
// One request per (state, self-sourced) pair -- the server caches the sweep
// under its own key (webapi.mastery_costs_fingerprint) -- and the sort and the
// filters are applied client-side over the rows already loaded, so changing
// either is instant.

import { initHelpTooltips } from "./help.js";
import {
  STORAGE_KEY,
  initSettings,
  applySettingsState,
  mergeSettingsIntoState,
  readSettingsState,
  readUi,
  writeUi,
} from "./settings.js";
import { tierLabel, tierRank } from "./tiers.js";

function $(selector) {
  return document.querySelector(selector);
}

// Gold rounded to the nearest tenth, the way report.py's _gold_text prints it
// ("1,234.5").
function formatGold(value) {
  return value.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// --- Pure helpers (exported for app.test.js's sibling, masteries.test.js) ----

// Every column sorts, keyed by the header button's data-sort. `ranked` names the
// values a row is ranked on, primary first and the rest as tie-breakers: the two
// numeric axes still break on each other (the JS mirror of
// main._sorted_mastery_rows, so the page and `--mastery-costs --sort` order the
// table the same way), and Item breaks on the target so a same-item run stays
// readable. Targets rank M < GM < MM, never alphabetically -- see tiers.tierRank.
//
// `fixed` axes (only Target has any) are tie-breakers that ignore the sort
// direction: within one target the rows always read cheapest-first, so flipping
// Target to MM/GM/M reorders the groups without shuffling the rows inside them.
const SORT_KEYS = {
  item: { ranked: (r) => [r.item.toLowerCase(), tierRank(r.tier)] },
  tier: {
    ranked: (r) => [tierRank(r.tier)],
    fixed: (r) => [r.time_seconds, r.gold],
  },
  time: { ranked: (r) => [r.time_seconds, r.gold] },
  gold: { ranked: (r) => [r.gold, r.time_seconds] },
};

export const SORT_COLUMNS = Object.keys(SORT_KEYS);

function compare(a, b) {
  if (typeof a === "string" || typeof b === "string") {
    return String(a).localeCompare(String(b));
  }
  return a - b;
}

function compareAxes(left, right) {
  for (let axis = 0; axis < left.length; axis += 1) {
    const order = compare(left[axis], right[axis]);
    if (order) return order;
  }
  return 0;
}

// Returns a new array; the input is the server's response order, which is left
// untouched so re-sorting is stable. `direction` is "asc" (the default) or
// "desc", which reverses the `ranked` axes -- but never the `fixed` ones or the
// final index tie-break, so rows equal on every axis keep the server's order
// (item-name order from main.solve_mastery_costs) either way, matching Python's
// stable sort.
export function sortRows(rows, key, direction = "asc") {
  const spec = SORT_KEYS[key] || SORT_KEYS.time;
  const sign = direction === "desc" ? -1 : 1;
  return rows
    .map((row, i) => ({ row, i }))
    .sort((a, b) => {
      const order = compareAxes(spec.ranked(a.row), spec.ranked(b.row));
      if (order) return sign * order;
      if (spec.fixed) {
        const tie = compareAxes(spec.fixed(a.row), spec.fixed(b.row));
        if (tie) return tie;
      }
      return a.i - b.i;
    })
    .map((entry) => entry.row);
}

// The solver page seeded with this row's goal. The tier token (M/GM/MM) is the
// amount verbatim: parsing.parse_amount reads it as the absolute 10k/100k/1M
// threshold, which is exactly the cumulative target this row was solved for.
export function masteryHref(row) {
  return "/?mastery=" + encodeURIComponent(`${row.item}:${row.tier}`);
}

// This item's mastery timeline (the /timeline page's own link format). Rendered
// as a small link inside the Item cell rather than as the row's destination:
// the row itself still goes to the plan, which is what this table is about.
export function timelineHref(item) {
  return "/timeline?item=" + encodeURIComponent(item);
}

// The filters pane, as data: `name` is matched case-insensitively anywhere in
// the item's name; `showEvents` false drops event items; `categories` and
// `statuses` are the ticked type / obtainability boxes. An item is kept when it
// has ANY ticked type (an item can have several -- see main.item_categories).
export function itemMatches(item, info, filter) {
  const needle = filter.name.trim().toLowerCase();
  if (needle && !item.toLowerCase().includes(needle)) return false;
  // An item the server sent no facts for (it always should) is never hidden by
  // the facts it lacks -- only by its name.
  if (!info) return true;
  if (info.event && !filter.showEvents) return false;
  if (!info.categories.some((category) => filter.categories.has(category))) return false;
  return filter.statuses.has(info.status);
}

// `entries` are anything with an `item` (table rows, "cannot" notes); `items`
// the response's {name: {categories, event, status}} map.
export function filterEntries(entries, items, filter) {
  return entries.filter((entry) => itemMatches(entry.item, items[entry.item], filter));
}

// --- Rendering --------------------------------------------------------------

function setStatus(text, kind) {
  const el = $("#status");
  el.textContent = text;
  el.className = "status" + (kind ? " status-" + kind : "");
}

let loadedRows = [];
let loadedItems = {};
let loadedUnmasterable = [];
let loadedLimited = [];
// Whether any response has arrived yet: before one, an empty table is "not
// loaded", not "filtered to nothing".
let loaded = false;

// The table solves only each item's next tier; an item with more (its
// `more_tiers`) gets an expand toggle that asks /api/mastery-costs/tiers for the
// rest. item -> {open, status: "pending" | "done" | "error", rows, limited,
// error}. Kept until the next table load, so re-expanding shows them again
// without another request.
let expansions = new Map();

// Event items are shown until the button hides them (restored in init()).
let showEvents = true;

function checkedValues(selector) {
  return new Set(
    [...document.querySelectorAll(`${selector} input[type="checkbox"]`)]
      .filter((box) => box.checked)
      .map((box) => box.value)
  );
}

function readFilter() {
  return {
    name: $("#filter-name").value,
    showEvents,
    categories: checkedValues("#filter-categories"),
    statuses: checkedValues("#filter-statuses"),
  };
}

// The active sort, restored from the UI key in init() and flipped by the
// header buttons. Purely display state: the rows are already loaded.
let sortKey = "time";
let sortDirection = "asc";

const SORT_ARROWS = { asc: "▲", desc: "▼", none: "↕" };

// Point each header button's arrow at the current sort (and tell a screen
// reader the same thing through the th's aria-sort).
function renderSortIndicators() {
  document.querySelectorAll("#mastery-costs-head th").forEach((th) => {
    const button = th.querySelector(".sort-btn");
    if (!button) return;
    const active = button.dataset.sort === sortKey;
    th.setAttribute(
      "aria-sort",
      active ? (sortDirection === "desc" ? "descending" : "ascending") : "none"
    );
    button.classList.toggle("active", active);
    button.querySelector(".sort-arrow").textContent = active
      ? SORT_ARROWS[sortDirection]
      : SORT_ARROWS.none;
  });
}

// The expand toggle before an item's name, when it has tiers left unsolved.
function tierToggleHtml(item) {
  const info = loadedItems[item];
  const more = (info && info.more_tiers) || [];
  // An empty slot the toggle's width, so every row's name lines up.
  if (!more.length) return `<span class="tier-toggle-slot"></span>`;
  const open = Boolean(expansions.get(item)?.open);
  const tiers = more.map(tierLabel).join(", ");
  return `<button type="button" class="tier-toggle" data-item="${escapeHtml(item)}"` +
    ` aria-expanded="${open}" title="${open ? "Hide" : "Show"} ${escapeHtml(tiers)}">` +
    `${open ? "▾" : "▸"}</button>`;
}

function costRowHtml(row, extra) {
  const name = extra
    ? `<span class="tier-toggle-slot"></span><span class="tier-extra-item">${escapeHtml(row.item)}</span>`
    : tierToggleHtml(row.item) + escapeHtml(row.item) +
      ` <a class="timeline-link" href="${escapeHtml(timelineHref(row.item))}"` +
      ` title="Mastery timeline for ${escapeHtml(row.item)}">timeline</a>`;
  return `<tr class="clickable${extra ? " tier-extra" : ""}"` +
    ` data-href="${escapeHtml(masteryHref(row))}"` +
    ` title="Plan for ${escapeHtml(row.item)} → ${escapeHtml(tierLabel(row.tier))}">` +
    `<td>${name}</td>` +
    `<td>${escapeHtml(row.tier)}</td>` +
    `<td class="num">${escapeHtml(row.time)}</td>` +
    `<td class="num">${formatGold(row.gold)} g</td>` +
    `</tr>`;
}

function noteRowHtml(text, kind) {
  return `<tr class="tier-extra tier-${kind}"><td colspan="4">${escapeHtml(text)}</td></tr>`;
}

// An expanded item's further tiers, directly under its row whatever the sort:
// they are part of that item's story, not rows to rank against the rest.
function expansionHtml(item) {
  const exp = expansions.get(item);
  if (!exp || !exp.open) return "";
  if (exp.status === "pending") return noteRowHtml("Calculating further tiers…", "pending");
  if (exp.status === "error") return noteRowHtml(exp.error, "error");
  return (
    [...exp.rows]
      .sort((a, b) => tierRank(a.tier) - tierRank(b.tier))
      .map((row) => costRowHtml(row, true))
      .join("") + exp.limited.map((note) => noteRowHtml(note.text, "limited")).join("")
  );
}

function renderTable(filter) {
  renderSortIndicators();
  const rows = sortRows(filterEntries(loadedRows, loadedItems, filter), sortKey, sortDirection);
  // Built in JS, not markdown->HTML like the solver page's report sections:
  // each <tr> has to carry its own item/tier to become a link, which reading
  // them back out of rendered table cells would make fragile.
  $("#mastery-costs-body").innerHTML = rows
    .map((row) => costRowHtml(row, false) + expansionHtml(row.item))
    .join("");
  $("#mastery-costs-section").hidden = rows.length === 0;
  return rows.length;
}

// One of the two collapsed "cannot" sections: items with no source at all
// (unmasterable), and items whose sources cannot supply a tier's quantity
// (limited). Each entry is a formatted line from the server plus its item,
// which the filters match on.
function renderChainList(prefix, entries, filter) {
  const shown = filterEntries(entries, loadedItems, filter);
  $(`#${prefix}-details`).hidden = shown.length === 0;
  if (!shown.length) return 0;
  $(`#${prefix}-summary`).textContent =
    shown.length === entries.length
      ? ` (${entries.length})`
      : ` (${shown.length} of ${entries.length})`;
  $(`#${prefix}-list`).innerHTML = shown
    .map((entry) => `<li>${escapeHtml(entry.text)}</li>`)
    .join("");
  return shown.length;
}

// Every section, through the current filters. The pane's collapsed summary
// says how much of the table they leave; "No masteries match" covers the case
// where they leave nothing at all, in any section.
function renderFiltered() {
  if (!loaded) return;
  const filter = readFilter();
  const shownRows = renderTable(filter);
  const shownNotes =
    renderChainList("unmasterable", loadedUnmasterable, filter) +
    renderChainList("limited", loadedLimited, filter);
  const total = loadedRows.length;
  $("#mastery-filter-summary").textContent =
    shownRows === total ? "" : ` (${shownRows.toLocaleString()} of ${total.toLocaleString()} rows)`;
  const anything = total + loadedUnmasterable.length + loadedLimited.length > 0;
  $("#filter-empty").hidden = !anything || shownRows + shownNotes > 0;
}

function renderEventsButton() {
  $("#filter-events").textContent = showEvents ? "Hide event items" : "Show event items";
  $("#filter-events").setAttribute("aria-pressed", String(!showEvents));
}

function renderResult(data) {
  expansions = new Map();
  loadedRows = data.rows || [];
  loadedItems = data.items || {};
  loadedUnmasterable = data.unmasterable || [];
  loadedLimited = data.limited || [];
  loaded = true;
  $("#mastery-filter-details").hidden = false;
  renderFiltered();
  const count = loadedRows.length;
  const timing = data.cached
    ? `cached (${data.solved_ms} ms)`
    : `solved in ${data.solved_ms} ms`;
  setStatus(
    count ? `${count.toLocaleString()} masteries left · ${timing}` : timing,
    "ok"
  );
  if (!count) {
    $("#mastery-costs-section").hidden = true;
    setStatus("Nothing left to master.", "ok");
  }
}

// --- Saved state ------------------------------------------------------------

// The state the settings popover and the solver page share. Read on every load
// (it is the source of truth for what to solve) and written back on a popover
// save.
function readSavedState() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch (err) {
    return null; // corrupt/unavailable storage: as good as nothing pasted
  }
}

// A popover save: fold the panel's values into the saved state, keeping the
// goal the solver page owns (mergeSettingsIntoState), then re-request off the
// stored state like any other load.
function saveSettings() {
  const state = mergeSettingsIntoState(readSavedState(), readSettingsState());
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (err) {
    // Private browsing / quota exceeded: the panel's values are still live in
    // the DOM, so this load still reflects them -- they just won't outlast it.
  }
  load();
}

// --- Loading ----------------------------------------------------------------

// Bumped per request so a slow response that a newer one has since superseded
// (a fast self-sourced double-toggle) is dropped rather than shown.
let sequence = 0;

// The request body both endpoints take: the saved pastes and settings, plus
// this page's self-sourced knob (and, for the tiers endpoint, the `item`).
function requestBody(extra = {}) {
  const saved = readSavedState();
  return JSON.stringify({
    mastery_text: (saved && saved.mastery_text) || "",
    inventory_text: (saved && saved.inventory_text) || "",
    // The shared settings, plus this page's one extra knob. The goal/frontier
    // fields (target, mastery, items, plans, ...) are carried along harmlessly:
    // main.solve_mastery_costs solves its own per-item goals and never reads
    // them, and they are not part of this endpoint's cache key.
    params: {
      ...((saved && saved.params) || {}),
      self_sourced: $("#self-sourced").checked,
    },
    ...extra,
  });
}

// Fetch an expanded item's further tiers into `expansions`, then redraw. A
// response for a table that has since been reloaded (`sequence` moved on) is
// dropped: it was solved against the old state.
async function fetchTiers(item) {
  const mySequence = sequence;
  const exp = expansions.get(item);
  let data;
  let failure = null;
  try {
    const response = await fetch("/api/mastery-costs/tiers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: requestBody({ item }),
    });
    data = await response.json();
    if (!response.ok) failure = (data && data.error) || `Error ${response.status}`;
  } catch (err) {
    failure = "Could not calculate further tiers: " + err.message;
  }
  if (mySequence !== sequence || expansions.get(item) !== exp) return;
  if (failure) {
    Object.assign(exp, { status: "error", error: failure });
  } else {
    Object.assign(exp, { status: "done", rows: data.rows || [], limited: data.limited || [] });
  }
  renderFiltered();
}

// The expand toggle: the first open fetches; after that it only shows/hides
// what is already here (an error is retried on the next open).
function toggleTiers(item) {
  const exp = expansions.get(item);
  if (exp && !(exp.status === "error" && !exp.open)) {
    exp.open = !exp.open;
    renderFiltered();
    return;
  }
  expansions.set(item, { open: true, status: "pending", rows: [], limited: [] });
  renderFiltered();
  fetchTiers(item);
}

async function load() {
  const saved = readSavedState();
  const masteryText = (saved && saved.mastery_text) || "";
  const inventoryText = (saved && saved.inventory_text) || "";
  if (!masteryText.trim() && !inventoryText.trim()) {
    $("#empty-state").hidden = false;
    $("#mastery-controls").hidden = true;
    $("#mastery-filter-details").hidden = true;
    setStatus("Nothing pasted yet.");
    return;
  }
  $("#empty-state").hidden = true;
  $("#mastery-controls").hidden = false;

  const body = requestBody();

  const mySequence = ++sequence;
  setStatus("Solving every remaining mastery…", "pending");

  let response;
  try {
    response = await fetch("/api/mastery-costs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (err) {
    if (mySequence !== sequence) return;
    setStatus("Network error: " + err.message, "error");
    return;
  }
  if (mySequence !== sequence) return;

  let data;
  try {
    data = await response.json();
  } catch (err) {
    if (mySequence !== sequence) return;
    setStatus("Unexpected response from the server", "error");
    return;
  }
  if (mySequence !== sequence) return;

  if (!response.ok) {
    setStatus((data && data.error) || `Error ${response.status}`, "error");
    return;
  }
  renderResult(data);
}

// --- Wiring -----------------------------------------------------------------

// The filters persist in the UI key like the sort. The boxes are stored as the
// values left UNticked, so a type added later starts ticked rather than hidden.
function wireFilters() {
  const ui = readUi();
  if (typeof ui.mastery_filter_name === "string") $("#filter-name").value = ui.mastery_filter_name;
  if (ui.mastery_hide_events === true) showEvents = false;
  renderEventsButton();

  $("#filter-name").addEventListener("input", () => {
    writeUi({ mastery_filter_name: $("#filter-name").value });
    renderFiltered();
  });
  $("#filter-events").addEventListener("click", () => {
    showEvents = !showEvents;
    writeUi({ mastery_hide_events: !showEvents });
    renderEventsButton();
    renderFiltered();
  });

  [
    ["#filter-categories", "mastery_hidden_categories"],
    ["#filter-statuses", "mastery_hidden_statuses"],
  ].forEach(([selector, key]) => {
    const boxes = [...document.querySelectorAll(`${selector} input[type="checkbox"]`)];
    const hidden = Array.isArray(ui[key]) ? ui[key] : [];
    boxes.forEach((box) => {
      box.checked = !hidden.includes(box.value);
      box.addEventListener("change", () => {
        writeUi({ [key]: boxes.filter((b) => !b.checked).map((b) => b.value) });
        renderFiltered();
      });
    });
  });
}

function init() {
  // The popover builds its generated panels here, so applySettingsState below
  // has controls to restore the saved settings into -- without that, opening it
  // would show bundled defaults and saving would wipe the real settings.
  initSettings({ onSave: saveSettings });
  applySettingsState(readSavedState());
  initHelpTooltips();

  const ui = readUi();
  if (SORT_COLUMNS.includes(ui.mastery_sort)) sortKey = ui.mastery_sort;
  if (ui.mastery_sort_dir === "desc") sortDirection = "desc";
  if (ui.mastery_self_sourced) $("#self-sourced").checked = true;
  renderSortIndicators();

  // Sorting is pure display: reorder what is already loaded, no request.
  // Delegated on the header row, so every column's button is one listener.
  $("#mastery-costs-head").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-sort]");
    if (!button) return;
    // Clicking the column already sorted on flips it; any other column starts
    // ascending (A-Z, M before MM, fastest/cheapest first).
    sortDirection =
      button.dataset.sort === sortKey && sortDirection === "asc"
        ? "desc"
        : "asc";
    sortKey = button.dataset.sort;
    writeUi({ mastery_sort: sortKey, mastery_sort_dir: sortDirection });
    renderFiltered();
  });
  wireFilters();
  // Self-sourcing changes what gets solved, so it re-requests.
  $("#self-sourced").addEventListener("change", () => {
    writeUi({ mastery_self_sourced: $("#self-sourced").checked });
    load();
  });

  // Delegated on the (stable) tbody, which renderTable replaces wholesale.
  // A plain navigation, so the browser's Back button returns to this list.
  $("#mastery-costs-body").addEventListener("click", (event) => {
    // The expand toggle shows/hides the item's further tiers, not its plan.
    const toggle = event.target.closest("button.tier-toggle");
    if (toggle) {
      toggleTiers(toggle.dataset.item);
      return;
    }
    // The Item cell carries its own "timeline" link; let the anchor navigate
    // rather than hijacking the click for the row's plan destination.
    if (event.target.closest("a")) return;
    const row = event.target.closest("tr[data-href]");
    if (row) window.location.href = row.dataset.href;
  });

  load();
}

// `once`: init() wires the page exactly one time. DOMContentLoaded only fires
// once in a browser anyway, but the page tests re-import this module against a
// rebuilt DOM and dispatch the event again -- without this, every earlier
// import's init() would run again too, wiring the new page a second time.
document.addEventListener("DOMContentLoaded", init, { once: true });
