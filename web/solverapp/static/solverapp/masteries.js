// Farm Solver mastery-costs page: the browser face of `--mastery-costs`.
//
// Deliberately self-contained rather than an app.js import: app.js registers a
// DOMContentLoaded -> init() listener at module scope, so importing it here
// would run the solver page's whole wiring against markup that isn't on this
// page. Only the help tooltips are shared (help.js), being big enough to be
// worth a module; the handful of one-line helpers below are duplicated instead.
//
// It has no state of its own to solve from: the pasted masteries/inventory and
// every parameter (prices, preference panels, AP trading, ...) come from the
// same localStorage the solver page writes, so the two pages always agree on
// what "your save" means. The only thing this page adds is `self_sourced`.
//
// One request per (state, self-sourced) pair -- the server caches the sweep
// under its own key (webapi.mastery_costs_fingerprint) -- and the sort is
// applied client-side over the rows already loaded, so toggling it is instant.

import { initHelpTooltips } from "./help.js";
import { tierLabel } from "./tiers.js";

// The solver page's state, read (never written) here.
const STORAGE_KEY = "farm-solver.v1";
// UI-only preferences, shared with the solver page's pane open/closed states.
const UI_STORAGE_KEY = "farm-solver.ui.v1";

function $(selector) {
  return document.querySelector(selector);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// --- Pure helpers (exported for app.test.js's sibling, masteries.test.js) ----

// Rank rows by the requested axis with the other as tie-breaker -- the JS
// mirror of main._sorted_mastery_rows, so the page and `--mastery-costs --sort`
// order the same table the same way. Returns a new array; the input is the
// server's response order, which is left untouched so re-sorting is stable.
export function sortRows(rows, key) {
  const by =
    key === "gold"
      ? (r) => [r.gold, r.time_seconds]
      : (r) => [r.time_seconds, r.gold];
  return rows
    .map((row, i) => ({ row, i }))
    .sort((a, b) => {
      const [a1, a2] = by(a.row);
      const [b1, b2] = by(b.row);
      // Ties beyond both axes keep the server's order (item-name order from
      // main.solve_mastery_costs), matching Python's stable sort.
      return a1 - b1 || a2 - b2 || a.i - b.i;
    })
    .map((entry) => entry.row);
}

// The solver page seeded with this row's goal. The tier token (M/GM/MM) is the
// amount verbatim: parsing.parse_amount reads it as the absolute 10k/100k/1M
// threshold, which is exactly the cumulative target this row was solved for.
export function masteryHref(row) {
  return "/?mastery=" + encodeURIComponent(`${row.item}:${row.tier}`);
}

// --- UI preferences ---------------------------------------------------------

function readUi() {
  try {
    return JSON.parse(localStorage.getItem(UI_STORAGE_KEY) || "{}") || {};
  } catch (err) {
    return {}; // corrupt/unavailable storage: fall back to the markup defaults
  }
}

function writeUi(patch) {
  try {
    localStorage.setItem(UI_STORAGE_KEY, JSON.stringify({ ...readUi(), ...patch }));
  } catch (err) {
    // Private browsing / quota exceeded -- persistence is a convenience.
  }
}

// --- Rendering --------------------------------------------------------------

function setStatus(text, kind) {
  const el = $("#status");
  el.textContent = text;
  el.className = "status" + (kind ? " status-" + kind : "");
}

let loadedRows = [];

function renderTable() {
  const rows = sortRows(loadedRows, $("#sort-key").value);
  // Built in JS, not markdown->HTML like the solver page's report sections:
  // each <tr> has to carry its own item/tier to become a link, which reading
  // them back out of rendered table cells would make fragile.
  $("#mastery-costs-body").innerHTML = rows
    .map(
      (row) => `<tr class="clickable" data-href="${escapeHtml(masteryHref(row))}"` +
        ` title="Plan for ${escapeHtml(row.item)} → ${escapeHtml(tierLabel(row.tier))}">` +
        `<td>${escapeHtml(row.item)}</td>` +
        `<td>${escapeHtml(row.tier)}</td>` +
        `<td class="num">${escapeHtml(row.time)}</td>` +
        `<td class="num">${Math.round(row.gold).toLocaleString()} g</td>` +
        `</tr>`
    )
    .join("");
  $("#mastery-costs-section").hidden = rows.length === 0;
}

// One of the two collapsed "cannot" sections: items with no source at all
// (unmasterable), and items whose sources cannot supply a tier's quantity
// (limited). Both are plain formatted lines from the server.
function renderChainList(prefix, lines) {
  $(`#${prefix}-details`).hidden = lines.length === 0;
  if (!lines.length) return;
  $(`#${prefix}-summary`).textContent = ` (${lines.length})`;
  $(`#${prefix}-list`).innerHTML = lines
    .map((line) => `<li>${escapeHtml(line)}</li>`)
    .join("");
}

function renderResult(data) {
  loadedRows = data.rows || [];
  renderTable();
  renderChainList("unmasterable", data.unmasterable || []);
  renderChainList("limited", data.limited || []);
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

// --- Loading ----------------------------------------------------------------

// Bumped per request so a slow response that a newer one has since superseded
// (a fast self-sourced double-toggle) is dropped rather than shown.
let sequence = 0;

async function load() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch (err) {
    saved = null;
  }
  const masteryText = (saved && saved.mastery_text) || "";
  const inventoryText = (saved && saved.inventory_text) || "";
  if (!masteryText.trim() && !inventoryText.trim()) {
    $("#empty-state").hidden = false;
    $("#mastery-controls").hidden = true;
    setStatus("Nothing pasted yet.");
    return;
  }
  $("#empty-state").hidden = true;
  $("#mastery-controls").hidden = false;

  const body = JSON.stringify({
    mastery_text: masteryText,
    inventory_text: inventoryText,
    // The solver page's own params, plus this page's one extra knob. Its
    // goal/frontier fields (target, mastery, items, plans, ...) are carried
    // along harmlessly: main.solve_mastery_costs solves its own per-item goals
    // and never reads them, and they are not part of this endpoint's cache key.
    params: {
      ...((saved && saved.params) || {}),
      self_sourced: $("#self-sourced").checked,
    },
  });

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

function init() {
  initHelpTooltips();

  const ui = readUi();
  if (ui.mastery_sort) $("#sort-key").value = ui.mastery_sort;
  if (ui.mastery_self_sourced) $("#self-sourced").checked = true;

  // Sorting is pure display: reorder what is already loaded, no request.
  $("#sort-key").addEventListener("change", () => {
    writeUi({ mastery_sort: $("#sort-key").value });
    renderTable();
  });
  // Self-sourcing changes what gets solved, so it re-requests.
  $("#self-sourced").addEventListener("change", () => {
    writeUi({ mastery_self_sourced: $("#self-sourced").checked });
    load();
  });

  // Delegated on the (stable) tbody, which renderTable replaces wholesale.
  // A plain navigation, so the browser's Back button returns to this list.
  $("#mastery-costs-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-href]");
    if (row) window.location.href = row.dataset.href;
  });

  load();
}

document.addEventListener("DOMContentLoaded", init);
