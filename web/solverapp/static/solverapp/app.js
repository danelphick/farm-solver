// Farm Solver web client: vanilla JS, no build step, no framework.
//
// State model: everything the user can set lives in one `state` shape
// ({mastery_text, inventory_text, params}) that mirrors POST /api/solve's
// body. It is saved to localStorage and restored (+ auto-solved) on load, so a
// reload picks up where you left off -- nothing is stored server-side (see
// ARCHITECTURE.md's web section).
//
// This module owns only the **Goal** pane and the results; every other
// parameter (the pasted game state, the limits, the preference panes) lives in
// the settings popover, which settings.js owns and the mastery-costs page
// shares. readParams/applyParams stitch the two halves back together.
//
// Three request cadences: a Goal edit debounces 400ms then re-solves (a
// monotonic sequence number drops a response that arrives after a newer
// request has since been sent -- typing fast never shows a stale result);
// saving the settings popover re-solves once, on Save (settings.js stages the
// edits until then); clicking a frontier row re-solves immediately with that
// plan selected (a refilter, so it hits the cache and returns fast).

import { ITEM_CATALOGUE, MASTERY_CATALOGUE, loadCatalogue } from "./catalogue.js";
import { initHelpTooltips } from "./help.js";
import {
  STORAGE_KEY,
  applySettingsParams,
  initSettings,
  readSettingsParams,
} from "./settings.js";
import { tierLabel } from "./tiers.js";

const DEBOUNCE_MS = 400;


// --- Debug: equivalent CLI command ------------------------------------------
//
// DEBUG builds render a `[</>]` button just left of the status box; it toggles a
// popup with the `python main.py ...` command equivalent to the current web
// config (built server-side in webapi.args_to_command, returned as
// `debug_command` on each solve). Absent entirely in production -- the template
// only emits the button/popup when Django's DEBUG is on.

let latestDebugCommand = "";

function setDebugCommand(command) {
  latestDebugCommand = command || "";
  const box = document.getElementById("debug-command-text");
  if (box) box.value = latestDebugCommand;
}

function initDebugCommand() {
  const button = document.getElementById("debug-command-btn");
  const popup = document.getElementById("debug-popup");
  if (!button || !popup) return; // not a DEBUG build

  button.addEventListener("click", () => {
    popup.hidden = !popup.hidden;
    if (!popup.hidden) {
      const box = document.getElementById("debug-command-text");
      box.value = latestDebugCommand;
      box.focus();
      box.select();
    }
  });
  document.getElementById("debug-popup-close").addEventListener("click", () => {
    popup.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !popup.hidden) popup.hidden = true;
  });
  document.getElementById("debug-command-copy").addEventListener("click", () => {
    const box = document.getElementById("debug-command-text");
    box.select();
    if (navigator.clipboard) navigator.clipboard.writeText(box.value).catch(() => {});
    else document.execCommand("copy");
  });
}

// The two goal lists: each maps its row-container id to the <datalist> id
// backing its item picker. Mastery goals only ever suggest from the user's
// own masterable-item catalogue (mastery_items.txt); item goals suggest from
// the broad catalogue (items.txt, every known item across all data files).
// Both still accept a typed value absent from the list -- see GOAL_CATALOGUES.
const GOAL_LISTS = {
  "mastery-rows": "mastery-catalogue",
  "items-rows": "item-catalogue",
};
const GOAL_CATALOGUES = {
  "mastery-catalogue": MASTERY_CATALOGUE,
  "item-catalogue": ITEM_CATALOGUE,
};

function $(selector) {
  return document.querySelector(selector);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// --- Goal lists: "Item:Amount,Item:Amount" <-> row entries -----------------
//
// These two are pure (no DOM), so they're unit-testable directly (see
// app.test.js) -- everything else in this section is DOM wiring around them.
// The wire format matches parsing.parse_item_amounts exactly (see
// parsing.py:247-265); amounts stay free text (not <input type="number">) so
// FarmRPG shorthands like "1M"/"100k"/"GM" survive to the server unparsed.

// Parses a "item:amount,item:amount" spec into `{item, amount}` entries. A
// chunk with no ":" (a partial/malformed entry, e.g. mid-edit) becomes an
// entry with an empty amount rather than being dropped, so restoring it into
// rows never silently loses what the user typed.
export function parseGoalSpec(spec) {
  if (!spec) return [];
  return spec
    .split(",")
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk) => {
      const colon = chunk.lastIndexOf(":");
      if (colon === -1) return { item: chunk, amount: "" };
      return {
        item: chunk.slice(0, colon).trim(),
        amount: chunk.slice(colon + 1).trim(),
      };
    });
}

// The inverse: entries -> a spec string, dropping any row missing an item or
// an amount (an in-progress row the user hasn't finished filling in).
export function serializeGoalEntries(entries) {
  return entries
    .filter((e) => e.item.trim() && e.amount.trim())
    .map((e) => `${e.item.trim()}:${e.amount.trim()}`)
    .join(",");
}

// The mastery-costs page links each row here as "/?mastery=Item:TIER" (see
// masteries.js's masteryHref): read that goal off a query string, or null when
// the page was opened normally. Reuses parseGoalSpec's grammar so a
// colon-bearing item name ("Time: A Gift") still splits on the right colon, and
// keeps the tier token (M/GM/MM) as free text the way every other amount is --
// parsing.parse_amount turns it into the absolute threshold server-side.
// Two kinds, because the two things a link can want are different goals:
// `?mastery=Item:TIER` is "get this mastery to that tier" (the mastery-costs
// table, and the timeline's next-tier links), while `?items=Item:N` is "make N
// more of it" -- what the timeline offers for an item that is already
// Megamastered, where every mastery threshold is long since passed and only a
// plain item count still means anything.
export function parseSeededGoal(search) {
  const params = new URLSearchParams(search || "");
  for (const kind of ["mastery", "items"]) {
    const raw = params.get(kind);
    if (!raw) continue;
    const [goal] = parseGoalSpec(raw);
    if (goal && goal.item && goal.amount) return { kind, ...goal };
  }
  return null;
}

// Where a seeded link came from, for the banner's way back. The Goal table's
// own "Focus" links (focusGoalHref below) add `from=goal`, so the banner can
// point at the goal the user actually had; every other seeded link (the
// mastery-costs table, the timeline's next-tier links) predates the param and
// keeps the original mastery-costs wording.
export function seededOrigin(search) {
  const from = new URLSearchParams(search || "").get("from");
  return from === "goal" ? "goal" : "masteries";
}

// The seeded-goal URL for one row of the rendered Goal table (see
// report.markdown_solution's Purpose/Item/Have/Target/Still needed columns):
// "solve for this item alone". Purpose says which kind of goal reproduces the
// row -- "Inventory" is a plain -i count, a tower level ("T240") or "Mastery"
// is an -m goal -- and Target is the absolute figure that need was solved for,
// which parsing.parse_amount reads as-is. Returns null for a row whose target
// isn't a plain number (nothing sensible to seed), so its cell keeps no button.
export function focusGoalHref(purpose, item, target) {
  const name = String(item || "").trim();
  const amount = String(target || "").replace(/,/g, "").trim();
  if (!name || !/^\d+(\.\d+)?$/.test(amount)) return null;
  const kind = String(purpose || "").trim() === "Inventory" ? "items" : "mastery";
  return `/?${kind}=${encodeURIComponent(`${name}:${amount}`)}&from=goal`;
}

// --- Goal lists: DOM (row render/read/wire) ---------------------------------

function createGoalRow(entry, datalistId) {
  const row = document.createElement("div");
  row.className = "goal-row";

  const itemInput = document.createElement("input");
  itemInput.type = "text";
  itemInput.className = "goal-item";
  itemInput.placeholder = "Item name";
  itemInput.setAttribute("list", datalistId);
  itemInput.value = entry.item;

  const amountInput = document.createElement("input");
  amountInput.type = "text";
  amountInput.className = "goal-amount narrow";
  amountInput.placeholder = "Amount";
  amountInput.value = entry.amount;

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "btn-delete";
  deleteBtn.setAttribute("aria-label", "Remove goal");
  deleteBtn.title = "Remove";
  deleteBtn.textContent = "×";

  row.append(itemInput, amountInput, deleteBtn);
  return row;
}

// Rebuilds a goal list's rows from `entries`; an empty list still shows one
// blank row so there's always something visible to type into besides the
// "+ Add" button.
function renderGoalRows(containerId, entries, datalistId) {
  const container = $(`#${containerId}`);
  container.innerHTML = "";
  const rows = entries.length ? entries : [{ item: "", amount: "" }];
  rows.forEach((entry) => container.appendChild(createGoalRow(entry, datalistId)));
}

function readGoalRows(containerId) {
  const rows = document.querySelectorAll(`#${containerId} .goal-row`);
  return Array.from(rows, (row) => ({
    item: row.querySelector(".goal-item").value,
    amount: row.querySelector(".goal-amount").value,
  }));
}

function appendGoalRow(containerId, datalistId) {
  $(`#${containerId}`).appendChild(createGoalRow({ item: "", amount: "" }, datalistId));
}

// Event delegation on the (stable) container -- rows come and go, but the
// container itself never gets recreated, so wiring it once in init() covers
// every row it ever holds.
function wireGoalList(containerId) {
  const container = $(`#${containerId}`);
  container.addEventListener("input", onAnyChange);
  container.addEventListener("click", (event) => {
    const btn = event.target.closest(".btn-delete");
    if (!btn) return;
    btn.closest(".goal-row").remove();
    if (!container.querySelector(".goal-row")) {
      appendGoalRow(containerId, GOAL_LISTS[containerId]);
    }
    onAnyChange();
  });
}

// --- Reading/writing the form <-> a {mastery_text, inventory_text, params} state ---

// The Goal pane's params (this module's half) plus the settings popover's
// (settings.js's). Split exactly the way the state is: everything here is a
// goal or a per-request display choice, everything there describes the farm.
function readParams() {
  const params = {};

  // Target dropdown -> --target (see main.parse_target):
  //  - "No Tower" ("none")        -> --target none: solve only the -m/-i goals.
  //  - "Specific tower level"     -> --target <level>.
  //  - "Tower (highest reachable)" (and a half-typed "level") -> --target tower,
  //    which keeps the tower as a goal even alongside -m/-i.
  const targetSel = $("#target").value;
  if (targetSel === "none") {
    params.target = "none";
  } else if (targetSel === "level" && $("#level").value) {
    params.target = $("#level").value;
  } else {
    params.target = "tower";
  }
  const mastery = serializeGoalEntries(readGoalRows("mastery-rows"));
  if (mastery) params.mastery = mastery;
  const items = serializeGoalEntries(readGoalRows("items-rows"));
  if (items) params.items = items;

  // The craft breakdown renders into its own collapsible pane, so it is
  // always requested (a refilter field -- asking costs no extra solve).
  params.breakdown = true;

  if (planSelect) params.plan_select = planSelect;

  return { ...params, ...readSettingsParams() };
}

function applyParams(params) {
  // Map --target back onto the dropdown. Legacy states omitted `target` for
  // BOTH "tower, no goals" and "goals-only": disambiguate by whether goals are
  // set, which reproduces the old server behaviour exactly ("none" == no tower).
  let target = params.target;
  if (target === undefined || target === null) {
    target = params.mastery || params.items ? "none" : "tower";
  }
  if (target === "none") {
    $("#target").value = "none";
    $("#level").hidden = true;
    $("#level").value = "";
  } else if (target === "tower") {
    $("#target").value = "";
    $("#level").hidden = true;
    $("#level").value = "";
  } else {
    $("#target").value = "level";
    $("#level").hidden = false;
    $("#level").value = target;
  }
  renderGoalRows("mastery-rows", parseGoalSpec(params.mastery || ""), GOAL_LISTS["mastery-rows"]);
  renderGoalRows("items-rows", parseGoalSpec(params.items || ""), GOAL_LISTS["items-rows"]);
  applySettingsParams(params);
  updateGoalSummary();
}

// --- The Goal pane's collapsed-state summary --------------------------------

// One "Item:Amount" goal spec entry, readable: "1,000,000 Grand Piano".
function formatGoalEntry(entry) {
  const colon = entry.lastIndexOf(":");
  if (colon === -1) return entry.trim();
  const item = entry.slice(0, colon).trim();
  const amount = entry.slice(colon + 1).trim();
  const pretty = /^\d+$/.test(amount) ? Number(amount).toLocaleString() : amount;
  return `${pretty} ${item}`;
}

// A -m/-i goal spec for the summary: spelled out when it is one short goal
// ("1,000,000 Grand Piano Mastery"), else a generic "custom mastery".
function goalSpecSummary(spec, kindLabel) {
  const entries = spec.split(",").filter((e) => e.trim());
  if (entries.length === 1) {
    const text = `${formatGoalEntry(entries[0])} ${kindLabel}`;
    if (text.length <= 40) return text;
  }
  return `custom ${kindLabel.toLowerCase()}`;
}

// The tower's top level (response tower_max_level) -- what "highest reachable"
// means; labels the dropdown option and the Goal pane's summary. Constant, so a
// specific-level solve never changes it.
let towerTargetLevel = null;

function updateGoalSummary() {
  const parts = [];
  const mastery = serializeGoalEntries(readGoalRows("mastery-rows"));
  const items = serializeGoalEntries(readGoalRows("items-rows"));
  const targetSel = $("#target").value;
  const level = targetSel === "level" ? $("#level").value : "";
  // Mirrors the server: the tower is a goal unless "No Tower" is picked; a
  // specific level or the highest-reachable tower both stand alongside any
  // -m/-i (farm_solver.solve_target). "No Tower" (--target none) leaves only
  // the -m/-i goals.
  if (level) {
    parts.push(`Masteries for Tower Level ${level}`);
  } else if (targetSel !== "none") {
    parts.push(
      towerTargetLevel
        ? `Masteries for the Tower (T${towerTargetLevel})`
        : "Masteries for the Tower"
    );
  }
  if (mastery) parts.push(goalSpecSummary(mastery, "Mastery"));
  if (items) parts.push(goalSpecSummary(items, "Inventory"));
  if (!parts.length) parts.push("No goal set");
  $("#goal-summary").textContent = " — " + parts.join(" + ");
}

function updateTowerOptionLabel() {
  $('#target option[value=""]').textContent = towerTargetLevel
    ? `Tower (highest reachable — T${towerTargetLevel})`
    : "Tower (highest reachable)";
}

function collectState() {
  return {
    mastery_text: $("#mastery-text").value,
    inventory_text: $("#inventory-text").value,
    params: readParams(),
  };
}

function saveState() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(collectState()));
  } catch (err) {
    // Private browsing / quota exceeded -- persistence is a convenience,
    // not a requirement, so this is silently non-fatal.
  }
}

function restoreState() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch (err) {
    saved = null;
  }
  if (!saved) return false;
  $("#mastery-text").value = saved.mastery_text || "";
  $("#inventory-text").value = saved.inventory_text || "";
  applyParams(saved.params || {});
  return true;
}

// --- Status badge ------------------------------------------------------------

function setStatus(text, kind) {
  const el = $("#status");
  el.textContent = text;
  el.className = "status" + (kind ? " status-" + kind : "");
}

function hideResults() {
  $("#results").hidden = true;
}

// --- Solving -------------------------------------------------------------

let sequence = 0;
// The frontier row explicitly clicked, or null for the default (fastest)
// plan. Reset whenever anything else changes -- a stale plan number could
// point past the end of a differently-filtered frontier.
let planSelect = null;

async function solve() {
  const masteryText = $("#mastery-text").value;
  const inventoryText = $("#inventory-text").value;
  if (!masteryText.trim() && !inventoryText.trim()) {
    setStatus("Paste your data under ⚙ Settings to begin.");
    hideResults();
    return;
  }

  const body = JSON.stringify({
    mastery_text: masteryText,
    inventory_text: inventoryText,
    params: readParams(),
  });

  const mySequence = ++sequence;
  setStatus("Solving…", "pending");

  let response;
  try {
    response = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (err) {
    if (mySequence !== sequence) return; // a newer request superseded this one
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
    hideResults();
    return;
  }

  renderResult(data);
}

function renderResult(data) {
  setStatus(
    data.cached ? `cached (${data.solved_ms} ms)` : `solved in ${data.solved_ms} ms`,
    "ok"
  );

  // DEBUG builds return the equivalent CLI command for the debug popup.
  if (data.debug_command != null) setDebugCommand(data.debug_command);

  if (data.tower_max_level) {
    towerTargetLevel = data.tower_max_level;
    updateTowerOptionLabel();
    updateGoalSummary();
  }

  $("#solution-section").innerHTML = data.solution_html;
  markUnreachableGoalRows();
  addGoalFocusLinks();
  collapseGoalSection();

  const noteBlocks = [];
  (data.notes || []).forEach((note) => {
    noteBlocks.push(`<p class="note">${escapeHtml(note)}</p>`);
  });
  (data.warnings || []).forEach((warning) => {
    noteBlocks.push(`<p class="warning">${escapeHtml(warning)}</p>`);
  });
  $("#notes-section").innerHTML = noteBlocks.join("");

  $("#frontier-section").innerHTML = data.frontier.html;
  wireFrontierRows(data.frontier.plans || []);

  $("#graph-section").innerHTML = (data.graph && data.graph.html) || "";
  wireGraphHover(data.frontier.plans || []);

  $("#plan-section").innerHTML = data.plan.html;
  collapsePlanGroups();

  const craftsHtml = (data.crafts && data.crafts.html) || "";
  $("#crafts-section").innerHTML = craftsHtml;
  $("#crafts-details").hidden = !craftsHtml;

  $("#results").hidden = false;
}

// Tint the "[fastest]"/"[cheapest]" tags in a frontier row's Notes cell to
// match the left-edge bars. The tags live inside the leading "[...]" bracket
// (see report.markdown_frontier); the reason text after it never contains these
// words, so a word-boundary wrap is safe.
function colourNoteTags(row) {
  const cell = row.querySelector("td:last-child");
  if (!cell) return;
  cell.innerHTML = cell.innerHTML
    .replace(/\bfastest\b/g, '<span class="tag-fastest">fastest</span>')
    .replace(/\bcheapest\b/g, '<span class="tag-cheapest">cheapest</span>');
}

// report.UNREACHABLE_GOAL_MARK ("✗") flags a Goal row whose item has no known
// source at all (report.markdown_solution appends it to "Still needed"); tint
// that row so it stands out from the rows the plan can actually cover.
function markUnreachableGoalRows() {
  const rows = document.querySelectorAll("#solution-section table tbody tr");
  rows.forEach((row) => {
    if (row.textContent.includes("✗")) {
      row.classList.add("unreachable-goal");
    }
  });
}

// report.markdown_solution's Goal section: its <h2> heading, the nodes up to
// the next <h2> (the goal table plus any note paragraphs), and that table.
// Null when the solve produced no Goal section at all. Shared by the two things
// that rework it after render -- the Focus buttons and the collapse -- so both
// agree on which table is the goal's: only the goal table sits in `body`, the
// "Materials with no known source" one is a later <h2> section, outside it.
function findGoalSection() {
  const container = $("#solution-section");
  if (!container) return null;
  const heading = Array.from(container.querySelectorAll(":scope > h2")).find(
    (h) => h.textContent.trim() === "Goal"
  );
  if (!heading) return null;

  const body = [];
  for (let node = heading.nextElementSibling; node && node.tagName !== "H2"; ) {
    const next = node.nextElementSibling;
    body.push(node);
    node = next;
  }
  return { heading, body, table: body.find((el) => el.tagName === "TABLE") || null };
}

// A drill-down per goal row: "Focus" re-opens this page seeded with that one
// item (focusGoalHref -> "/?mastery=Item:N&from=goal", the same seeded-goal
// entry point the mastery-costs table uses), so a tower run's long list of
// required masteries can be costed one item at a time. A plain <a>, not a
// button, so it is an ordinary navigation: Back returns to the original goals,
// which are still what localStorage holds (the seeded page deliberately does
// not save -- see applySeededGoal / the banner's "Make Sole Goal").
//
// Only when the goal has more than one row: a sole goal is already focused.
function addGoalFocusLinks() {
  const section = findGoalSection();
  if (!section || !section.table) return;
  const rows = Array.from(section.table.querySelectorAll("tbody tr"));
  if (rows.length < 2) return;
  rows.forEach((row) => {
    const cells = row.querySelectorAll("td");
    if (cells.length < 4) return;
    const item = cells[1].textContent.trim();
    const href = focusGoalHref(cells[0].textContent, item, cells[3].textContent);
    if (!href) return;
    const link = document.createElement("a");
    link.className = "focus-link";
    link.href = href;
    link.textContent = "Focus";
    link.title = `Solve for ${item} on its own`;
    cells[1].append(" ", link);
  });
}

// Persists the Goal section's open/closed state across re-renders within this
// page load (#solution-section is rebuilt every solve, so the <details> itself
// doesn't survive). Session-only, like planGroupOpenState; defaults open.
let goalSectionOpen = true;

// report.markdown_solution renders the goal needs as an <h2>Goal</h2> heading
// followed by a table (and any note paragraphs), then optionally a separate
// "Materials with no known source" <h2> + table. Wrap just the Goal heading and
// its table into a collapsible <details> so a long goal list (a full tower run
// expands to dozens of per-level requirement rows) can be folded away; the
// Materials section, being a warning, is deliberately left outside so it never
// hides. Collapsed, the summary reads at a glance: the number of goal rows plus
// the highest tower level named (the Purpose column's "T<level>" cells), echoed
// into a `.summary-detail` span that existing CSS hides once expanded.
function collapseGoalSection() {
  const section = findGoalSection();
  if (!section) return;
  const { heading, body, table } = section;
  const rows = table ? table.querySelectorAll("tbody tr") : [];
  const count = rows.length;
  let maxLevel = 0;
  rows.forEach((row) => {
    const purpose = row.querySelector("td");
    const match = purpose && purpose.textContent.trim().match(/^T(\d+)$/);
    if (match) maxLevel = Math.max(maxLevel, parseInt(match[1], 10));
  });

  const parts = [`${count} goal${count === 1 ? "" : "s"}`];
  if (maxLevel) parts.push(`Tower Level ${maxLevel}`);
  const statText = parts.join(", ");

  const details = document.createElement("details");
  details.className = "goal-group";
  details.open = goalSectionOpen;
  details.addEventListener("toggle", () => {
    goalSectionOpen = details.open;
  });
  const summary = document.createElement("summary");
  summary.textContent = "Goal";
  const stat = document.createElement("span");
  stat.className = "summary-detail";
  stat.textContent = ` — ${statText}`;
  summary.appendChild(stat);
  details.appendChild(summary);
  body.forEach((el) => details.appendChild(el));
  heading.replaceWith(details);
}

// Parses a table cell/text's leading number the way report.py formats them
// ("47,427", "-44,371,440", "2,009 ✗") -- comma thousands, optional sign,
// trailing junk ignored.
function parseReportNumber(text) {
  const match = text.replace(/,/g, "").match(/-?\d+(\.\d+)?/);
  return match ? parseFloat(match[0]) : 0;
}

// Sums a group's table's first column (each of Fishing/Exploring/Exploring
// (Apple Cider)/Opening (Locksmith)'s first column is exactly the one count
// that section is "about" -- Large Nets, Arnold Palmers, Apple Cider, opens).
function sumFirstColumn(body, unitLabel) {
  const table = body.find((el) => el.tagName === "TABLE");
  if (!table) return null;
  let total = 0;
  table.querySelectorAll("tbody tr").forEach((row) => {
    const cell = row.querySelector("td");
    if (cell) total += parseReportNumber(cell.textContent);
  });
  if (total <= 0) return null;
  return `${Math.round(total).toLocaleString()} ${unitLabel}`;
}

// report.markdown_plan's Stamina section (see the doc.heading("Stamina") call)
// is a bullet list: one "N spent on Apple Cider" line (the only stamina sink
// modelled) plus one "Drink/Eat ... -> N stamina" line per topup source --
// sum those for gained, since the banked-at-start and Farmhouse-rate lines
// aren't sums of a plan action the way the topups are.
function staminaStat(body) {
  const list = body.find((el) => el.tagName === "UL");
  if (!list) return null;
  let used = 0;
  let gained = 0;
  list.querySelectorAll("li").forEach((li) => {
    const text = li.textContent;
    const spent = text.match(/^([\d,]+) spent on Apple Cider/);
    if (spent) used += parseReportNumber(spent[1]);
    const topup = text.match(/-> ([\d,]+) stamina$/);
    if (topup) gained += parseReportNumber(topup[1]);
  });
  if (!used && !gained) return null;
  return (
    `${Math.round(gained).toLocaleString()} gained, ` +
    `${Math.round(used).toLocaleString()} used`
  );
}

// Per-group summary-stat extractors, keyed by the group's exact (static)
// heading text -- Sawmill boost isn't here because its heading text is
// static but has no single number to sum. Groups with neither an extractor
// nor a match in DYNAMIC_HEADING_RE (Passive production, From inventory)
// fall through to GROUP_TOTAL_LINE_RE below, or show no stat at all when
// collapsed -- per spec, only "if one exists".
const GROUP_STAT_EXTRACTORS = {
  "Fishing": (body) => sumFirstColumn(body, "Large Nets"),
  "Exploring (Arnold Palmer)": (body) => sumFirstColumn(body, "Arnold Palmers"),
  "Exploring (Apple Cider)": (body) => sumFirstColumn(body, "Apple Cider"),
  "Opening (Locksmith)": (body) => sumFirstColumn(body, "opened"),
  "Stamina": staminaStat,
};

// Buy/Pumpkin Juice/Surplus each end in their own "Total: ..."/"Nominal
// value: ..." paragraph (see markdown_plan/markdown_surplus_value) -- no
// extractor needed, just echo that line.
const GROUP_TOTAL_LINE_RE = /^(Total|Nominal value):/i;

// Farming and Critical path bake their one number straight into the heading
// text (`f"Farming ({duration}, ...)"` / `f"Critical path (sets the
// {duration} total)"` in report.markdown_plan) instead of a static label --
// unlike Exploring (Apple Cider)/Opening (Locksmith), where the parenthesised
// text is part of the group's name, not a figure. Split those two off here so
// they read the same as every other group: a plain bold label plus the stat
// in a muted `.summary-detail`, not baked into the always-visible heading.
const DYNAMIC_HEADINGS = [
  { label: "Farming", re: /^Farming \((.+)\)$/ },
  { label: "Critical path", re: /^Critical path \((.+)\)$/ },
];

// Persists each group's open/closed state across re-renders (switching
// plans, changing a param, ...) within this page load, keyed by the group's
// label -- #plan-section's whole DOM is torn down and rebuilt on every
// render, so the <details> elements themselves don't survive to remember it.
// A live-session convenience only, not saved to localStorage. A group not
// yet seen this session (or never toggled) defaults to closed.
const planGroupOpenState = {};

// report.markdown_plan renders the selected plan as a flat run of <h3>
// section headings (Fishing, Exploring (Arnold Palmer), Farming, Surplus,
// ...) each followed by that section's table/list/paragraphs. Turn every one
// into its own collapsible <details> so a long plan can be skimmed section by
// section -- closed by default the first time a group is seen, but remembers
// each one's open/closed state after that (planGroupOpenState) so re-solving
// (a plan switch, any param edit) doesn't reset what the user had open. The
// "Expand all"/"Collapse all" buttons next to the "Selected plan" heading
// (wired in init()) open/close every currently-shown one at once.
//
// Collapsed, a section still needs to read at a glance, so each gets a
// summary stat where one is available (a DYNAMIC_HEADINGS split,
// GROUP_STAT_EXTRACTORS, or a trailing total paragraph) echoed into the
// <summary> as a `.summary-detail` span -- the same class the Goal pane's
// collapsed-state summary uses; existing CSS already hides `.summary-detail`
// while its <details> is open, so it only appears once collapsed, and the
// section's own content (table, total paragraph, ...) is left in place for
// the expanded view.
function collapsePlanGroups() {
  const container = $("#plan-section");
  if (!container) return;
  const headings = Array.from(container.querySelectorAll(":scope > h3"));
  headings.forEach((heading) => {
    const body = [];
    for (let node = heading.nextElementSibling; node && node.tagName !== "H3"; ) {
      const next = node.nextElementSibling;
      body.push(node);
      node = next;
    }
    const rawText = heading.textContent.trim();
    let label = rawText;
    let statText = null;
    for (const { label: dynLabel, re } of DYNAMIC_HEADINGS) {
      const match = rawText.match(re);
      if (match) {
        label = dynLabel;
        statText = match[1];
        break;
      }
    }
    if (!statText) {
      const extractor = GROUP_STAT_EXTRACTORS[label];
      statText = extractor ? extractor(body) : null;
    }
    if (!statText) {
      const totalLine = body.find(
        (el) => el.tagName === "P" && GROUP_TOTAL_LINE_RE.test(el.textContent)
      );
      if (totalLine) statText = totalLine.textContent;
    }
    // The critical path is the plan's headline, so it stays pinned open as a
    // static panel: no disclosure marker, no collapsed-state stat, and left out
    // of the collapse/expand-all wiring below (.plan-group-static).
    const isStatic = label === "Critical path";
    const details = document.createElement("details");
    details.className = isStatic ? "plan-group plan-group-static" : "plan-group";
    details.dataset.groupLabel = label;
    if (isStatic) {
      details.open = true;
    } else {
      details.open = planGroupOpenState[label] || false;
      // Real clicks toggle synchronously enough for this to stay in sync, but
      // setAllPlanGroupsOpen below writes planGroupOpenState directly rather
      // than relying on this for its batch of programmatic changes -- browsers
      // (and jsdom) don't guarantee "toggle" fires synchronously for those.
      details.addEventListener("toggle", () => {
        planGroupOpenState[label] = details.open;
      });
    }
    const summary = document.createElement("summary");
    summary.textContent = label;
    if (statText && !isStatic) {
      const stat = document.createElement("span");
      stat.className = "summary-detail";
      stat.textContent = ` — ${statText}`;
      summary.appendChild(stat);
    }
    details.appendChild(summary);
    body.forEach((el) => details.appendChild(el));
    heading.replaceWith(details);
  });
}

// Wired to the "Expand all"/"Collapse all" buttons next to "Selected plan".
function setAllPlanGroupsOpen(open) {
  document
    .querySelectorAll("#plan-section > details.plan-group:not(.plan-group-static)")
    .forEach((details) => {
      details.open = open;
      planGroupOpenState[details.dataset.groupLabel] = open;
    });
}

function wireFrontierRows(plans) {
  const rows = document.querySelectorAll("#frontier-section table tbody tr");
  rows.forEach((row, i) => {
    const info = plans[i];
    if (!info) return;
    row.classList.toggle("selected", !!info.selected);
    row.classList.toggle("cheapest", !!info.cheapest);
    row.classList.toggle("fastest", !!info.fastest);
    colourNoteTags(row);
    row.classList.add("clickable");
    row.addEventListener("click", () => {
      // The API's plan_select index is 1-based (matches --plan N on the CLI);
      // info.index is the 0-based position in this response's frontier.
      planSelect = { kind: "index", index: info.index + 1 };
      saveState();
      solve();
    });
  });
}

// --- Impact graph hover tooltip ---------------------------------------------
//
// The graph SVG (report._svg_plot) already gives each `<circle class="pt">` a
// CSS :hover state (enlarge + highlight) and a <title> tooltip -- a no-JS
// baseline that works even here. This adds a richer floating overlay on top,
// mirroring the help-icon tooltip's own singleton-div/positioning pattern
// (ensureHelpTooltip/positionHelpTooltip above), but keyed to the pointer
// instead of a fixed icon and reading each point's numbers straight from
// data.frontier.plans (axis_value/secondary/time/net_gold/pumpkin_juices --
// see webapi.render's frontier_rows) rather than parsing the SVG's own title.

let graphTooltipEl = null;

function ensureGraphTooltip() {
  if (!graphTooltipEl) {
    graphTooltipEl = document.createElement("div");
    graphTooltipEl.className = "graph-tooltip";
    graphTooltipEl.setAttribute("role", "tooltip");
    graphTooltipEl.hidden = true;
    document.body.appendChild(graphTooltipEl);
  }
  return graphTooltipEl;
}

function positionGraphTooltip(tip, event) {
  const margin = 8;
  const offset = 14; // clear of the cursor, so the tooltip doesn't eat the hover
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = event.clientX + offset;
  let top = event.clientY + offset;
  left = Math.max(margin, Math.min(left, window.innerWidth - tw - margin));
  top = Math.max(margin, Math.min(top, window.innerHeight - th - margin));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

// Gold rounded to the nearest tenth, the way report.py's _gold_text prints it
// ("1,234.5").
function formatGold(value) {
  return value.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function hideGraphTooltip() {
  if (graphTooltipEl) graphTooltipEl.hidden = true;
}

function graphTooltipHtml(info) {
  const gold = formatGold(info.net_gold);
  // info.index is the 0-based frontier position (webapi.render); +1 is the plan
  // number shown in the frontier table and what clicking the point selects.
  const rows = [
    `<div class="graph-tooltip-plan">Plan ${info.index + 1}</div>`,
    `<div class="graph-tooltip-time">${escapeHtml(info.time)}</div>`,
  ];
  rows.push(`<div>Net gold: ${gold}</div>`);
  if (info.pumpkin_juices) {
    rows.push(`<div>Pumpkin Juice: ${Math.round(info.pumpkin_juices).toLocaleString()}</div>`);
  }
  return rows.join("");
}

// `plans` is the same data.frontier.plans array wireFrontierRows zips with the
// frontier table, fastest-first -- the graph's points are plotted from the
// same shown-plans list (see optimizer.spread_graph_series/webapi's
// frontier_rows), so index i's <circle class="pt"> is plans[i].
function wireGraphHover(plans) {
  const svg = document.querySelector("#graph-section svg");
  if (!svg) return;
  const tip = ensureGraphTooltip();
  svg.querySelectorAll(".pt").forEach((point, i) => {
    const info = plans[i];
    if (!info) return;
    point.classList.toggle("selected", !!info.selected);
    point.addEventListener("mouseover", (event) => {
      tip.innerHTML = graphTooltipHtml(info);
      tip.hidden = false;
      positionGraphTooltip(tip, event);
    });
    point.addEventListener("mousemove", (event) => positionGraphTooltip(tip, event));
    point.addEventListener("mouseout", hideGraphTooltip);
    // Clicking a point selects that plan, exactly like clicking its frontier row
    // (see wireFrontierRows): the API's plan_select index is 1-based, info.index
    // is the 0-based frontier position.
    point.addEventListener("click", () => {
      hideGraphTooltip();
      planSelect = { kind: "index", index: info.index + 1 };
      saveState();
      solve();
    });
  });
}

function initGraphTooltip() {
  // Viewport-positioned like the help tooltip -- a scroll or Escape strands
  // it, so hide it the same way (see initHelpTooltips).
  window.addEventListener("scroll", hideGraphTooltip, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideGraphTooltip();
  });
}

// --- Wiring ----------------------------------------------------------------

const debouncedSave = debounce(saveState, DEBOUNCE_MS);
const debouncedSolve = debounce(solve, DEBOUNCE_MS);

function onAnyChange() {
  planSelect = null;
  updateGoalSummary();
  debouncedSave();
  debouncedSolve();
}

function init() {
  // The settings popover first: it builds the generated preference panels and
  // the material-trading rows, so every control a saved state applies to exists
  // before restoreState() below runs. Its Save is this page's second request
  // cadence -- the staged edits land in one solve, not one per keystroke.
  initSettings({
    onSave: () => {
      planSelect = null;
      saveState();
      solve();
    },
  });
  initHelpTooltips();
  initDebugCommand();

  $("#expand-all-groups").addEventListener("click", () => setAllPlanGroupsOpen(true));
  $("#collapse-all-groups").addEventListener("click", () => setAllPlanGroupsOpen(false));

  // The Goal pane is the only live-solving part of the page (everything else is
  // staged behind the popover's Save), so its handful of controls are wired
  // one by one rather than by a blanket sweep: the target dropdown, the level
  // it reveals, and the two goal lists (delegated, see wireGoalList).
  $("#target").addEventListener("change", () => {
    $("#level").hidden = $("#target").value !== "level";
    onAnyChange();
  });
  $("#level").addEventListener("input", onAnyChange);

  // Goal lists (Mastery/Item goals): rows are built dynamically, so they're
  // wired via event delegation on their (stable) container -- see
  // wireGoalList. Render the default single blank row first; restoreState()
  // below replaces it with saved entries, if any.
  Object.entries(GOAL_LISTS).forEach(([containerId, datalistId]) => {
    renderGoalRows(containerId, [], datalistId);
    wireGoalList(containerId);
  });
  // Only the goal-list "+ Add" buttons carry data-goal; other .btn-add buttons
  // (the popover's Save/Cancel and material buttons, Expand/Collapse, debug
  // Copy) have their own handlers and must not get the append-row one.
  document.querySelectorAll(".btn-add[data-goal]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const containerId = btn.dataset.goal;
      appendGoalRow(containerId, GOAL_LISTS[containerId]);
      onAnyChange();
    });
  });
  Object.entries(GOAL_CATALOGUES).forEach(([datalistId, url]) => {
    loadCatalogue(url, datalistId);
  });

  // A brand-new session never runs applyParams, so seed the Goal pane's summary.
  updateGoalSummary();
  initGraphTooltip();

  const restored = restoreState();
  // Arriving from the mastery-costs table ("/?mastery=Item:TIER"): keep every
  // restored setting (prices, limits, panels) but replace the goal with that
  // one mastery. Deliberately no saveState() -- merely following a link must
  // not overwrite the goal the user had saved; their first edit persists this
  // one as usual, via onAnyChange.
  const search = window.location.search;
  const seeded = applySeededGoal(parseSeededGoal(search), seededOrigin(search));
  $("#seeded-adopt").addEventListener("click", adoptSeededGoal);
  if (restored || seeded) {
    solve();
  }
}

// A seeded item count for the banner: grouped ("10,000") when it is a plain
// number, otherwise left as typed -- an amount is free text all the way to the
// server (parsing.parse_amount reads "1M"/"100k" too).
function seededAmount(amount) {
  const number = Number(amount);
  return Number.isFinite(number) ? number.toLocaleString() : amount;
}

// The goal currently seeded from the URL, or null once it has been adopted (or
// when the page was opened normally). Only "Make Sole Goal" reads it.
let seededGoal = null;

// The banner's label for a mastery goal: a tier token spells out ("Compass →
// Grandmaster"), while a plain figure -- what a Focus link off the Goal table
// carries, since that table's Target column is the absolute threshold -- reads
// as the amount it is ("Compass → 100,000 mastery").
function masteryGoalLabel(item, amount) {
  const label = tierLabel(amount);
  const target = label === amount ? `${seededAmount(amount)} mastery` : label;
  return `${item} → ${target}`;
}

// Point the form at a single goal and nothing else, and say so in the header
// banner. Returns whether anything was seeded, so init() knows to solve even
// when there was no saved state to restore.
function applySeededGoal(goal, origin) {
  if (!goal) return false;
  const mastery = goal.kind === "mastery";
  $("#target").value = "none";  // just this goal, no tower goal
  $("#level").hidden = true;
  renderGoalRows("mastery-rows", mastery ? [goal] : [], GOAL_LISTS["mastery-rows"]);
  renderGoalRows("items-rows", mastery ? [] : [goal], GOAL_LISTS["items-rows"]);
  $("#seeded-goal").textContent = mastery
    ? masteryGoalLabel(goal.item, goal.amount)
    : `${seededAmount(goal.amount)} × ${goal.item}`;
  // A Focus drill-down came from this page's own goals, so point back at them
  // ("/" restores the saved state) rather than at the mastery-costs table the
  // banner's older callers came from. Plain links either way -- the browser's
  // Back button gets there too, since every seeded page is a real navigation.
  if (origin === "goal") {
    $("#seeded-back").textContent = "← back to your goals";
    $("#seeded-back").href = "/";
  }
  $("#seeded-banner").hidden = false;
  seededGoal = goal;
  updateGoalSummary();
  return true;
}

// "Make Sole Goal": substitute the seeded goal into the saved parameters. The
// form already holds it and nothing else (applySeededGoal pointed every goal
// control at it), so saving the form IS the substitution -- every other
// setting, restored from localStorage as usual, is untouched. No re-solve: the
// results on screen are already this goal's. The ?mastery=/?items= query is
// dropped with replaceState so a reload doesn't re-seed a goal that is now the
// saved one; replacing rather than pushing leaves the Back button alone.
function adoptSeededGoal() {
  if (!seededGoal) return;
  seededGoal = null;
  saveState();
  $("#seeded-banner").hidden = true;
  window.history.replaceState({}, "", window.location.pathname);
  setStatus("Saved as your only goal.", "ok");
}

// `once`: init() wires the page exactly one time. DOMContentLoaded only fires
// once in a browser anyway, but the page tests re-import this module against a
// rebuilt DOM and dispatch the event again -- without this, every earlier
// import's init() would run again too, wiring the new page a second time.
document.addEventListener("DOMContentLoaded", init, { once: true });
