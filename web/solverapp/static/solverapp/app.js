// Farm Solver web client: vanilla JS, no build step, no framework.
//
// State model: everything the user can set lives in one `state` shape
// ({mastery_text, inventory_text, params}) that mirrors POST /api/solve's
// body. It is saved to localStorage (debounced) on every change and
// restored (+ auto-solved) on load, so a reload picks up where you left
// off -- nothing is stored server-side (see ARCHITECTURE.md's web section).
//
// Two request cadences: any control or paste-box edit debounces 400ms then
// re-solves (a monotonic sequence number drops a response that arrives after
// a newer request has since been sent -- typing fast never shows a stale
// result); clicking a frontier row re-solves immediately with that plan
// selected (a refilter, so it hits the cache and returns fast).

const STORAGE_KEY = "farm-solver.v1";
// UI-only preferences (e.g. whether the paste pane is collapsed) live under
// their own key: STORAGE_KEY's shape mirrors the POST /api/solve body.
const UI_STORAGE_KEY = "farm-solver.ui.v1";
const DEBOUNCE_MS = 400;

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

function todayISO() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

// The start date defaults to "today" without being a real setting: the input
// always shows a date, but until the user picks one it shows today muted,
// placeholder-style, and nothing is sent (the solver defaults to today
// server-side). Picking today (or clearing) returns it to that implicit state.
function setStartDateDisplay(value) {
  const el = $("#start-date");
  const implicit = !value || value === todayISO();
  el.value = implicit ? todayISO() : value;
  el.classList.toggle("implicit-date", implicit);
}

// The date input and its calendar-toggle button enable/disable as one.
function setStartDateEnabled(enabled) {
  $("#start-date").disabled = !enabled;
  $("#start-date-toggle").disabled = !enabled;
}

// --- Reading/writing the form <-> a {mastery_text, inventory_text, params} state ---

function readParams() {
  const params = {};

  if ($("#target").value === "level" && $("#level").value) {
    params.target = $("#level").value;
  }
  const mastery = $("#mastery").value.trim();
  if (mastery) params.mastery = mastery;
  const items = $("#items").value.trim();
  if (items) params.items = items;

  if ($("#current-stamina").value) {
    params.current_stamina = Number($("#current-stamina").value);
  }
  if ($("#cookie-sets").value !== "") {
    params.cookie_sets = Number($("#cookie-sets").value);
  }
  if ($("#use-pumpkin-juice").checked) {
    const cap = $("#pumpkin-juice-cap").value;
    params.use_pumpkin_juice = cap !== "" ? Number(cap) : -1;
  }
  const calendar = document.querySelector('input[name="calendar"]:checked').value;
  params.calendar = calendar;
  // The start date only means anything when the calendar is fully modelled
  // (the input is disabled otherwise, but its value lingers -- don't send it),
  // and an implicit "today" (see setStartDateDisplay) is never sent either.
  if (calendar === "full" && !$("#start-date").classList.contains("implicit-date")) {
    const startDate = $("#start-date").value;
    if (startDate) params.start_date = startDate;
  }

  const maxGold = $("#max-gold").value.trim();
  if (maxGold) params.max_gold = maxGold;
  if ($("#max-ap").value !== "") params.max_ap = Number($("#max-ap").value);
  if ($("#max-ln").value !== "") params.max_ln = Number($("#max-ln").value);
  const passiveLimit = $("#passive-limit").value.trim();
  if (passiveLimit) params.passive_limit = passiveLimit;
  if ($("#plans").value) params.plans = Number($("#plans").value);
  if ($("#breakdown").checked) params.breakdown = true;

  const overrides = $("#overrides").value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (overrides.length) params.overrides = overrides;

  if (planSelect) params.plan_select = planSelect;

  return params;
}

function applyParams(params) {
  if (params.target) {
    $("#target").value = "level";
    $("#level").hidden = false;
    $("#level").value = params.target;
  } else {
    $("#target").value = "";
    $("#level").hidden = true;
    $("#level").value = "";
  }
  $("#mastery").value = params.mastery || "";
  $("#items").value = params.items || "";
  $("#current-stamina").value = params.current_stamina ?? 0;
  $("#cookie-sets").value = params.cookie_sets ?? "";

  const hasPj = params.use_pumpkin_juice !== undefined && params.use_pumpkin_juice !== null;
  $("#use-pumpkin-juice").checked = hasPj;
  $("#pumpkin-juice-cap").disabled = !hasPj;
  $("#pumpkin-juice-cap").value = hasPj && params.use_pumpkin_juice >= 0 ? params.use_pumpkin_juice : "";

  // "model" is the pre-enum wire value for "full"; a state saved before the
  // enum existed carries no calendar at all and gets the new default (ignore).
  const calendarValue =
    params.calendar === "model" ? "full" : params.calendar || "ignore";
  document.querySelectorAll('input[name="calendar"]').forEach((radio) => {
    radio.checked = radio.value === calendarValue;
  });
  setStartDateDisplay(params.start_date);
  setStartDateEnabled(calendarValue === "full");

  $("#max-gold").value = params.max_gold || "";
  $("#max-ap").value = params.max_ap ?? "";
  $("#max-ln").value = params.max_ln ?? "";
  $("#passive-limit").value = params.passive_limit || "";
  $("#plans").value = params.plans || 5;
  $("#breakdown").checked = !!params.breakdown;
  $("#overrides").value = (params.overrides || []).join("\n");
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
    setStatus("Paste your data to begin.");
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

  $("#solution-section").innerHTML = data.solution_html;

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

  $("#plan-section").innerHTML = data.plan.html;

  $("#results").hidden = false;
}

function wireFrontierRows(plans) {
  const rows = document.querySelectorAll("#frontier-section table tbody tr");
  rows.forEach((row, i) => {
    const info = plans[i];
    if (!info) return;
    row.classList.toggle("selected", !!info.selected);
    row.classList.toggle("cheapest", !!info.cheapest);
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

// --- Wiring ----------------------------------------------------------------

const debouncedSave = debounce(saveState, DEBOUNCE_MS);
const debouncedSolve = debounce(solve, DEBOUNCE_MS);

function onAnyChange() {
  planSelect = null;
  debouncedSave();
  debouncedSolve();
}

function init() {
  $("#target").addEventListener("change", () => {
    $("#level").hidden = $("#target").value !== "level";
    onAnyChange();
  });
  $("#use-pumpkin-juice").addEventListener("change", () => {
    $("#pumpkin-juice-cap").disabled = !$("#use-pumpkin-juice").checked;
    onAnyChange();
  });
  document.querySelectorAll('input[name="calendar"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      setStartDateEnabled(radio.value === "full");
      onAnyChange();
    });
  });
  $("#start-date").addEventListener("change", () => {
    setStartDateDisplay($("#start-date").value);
    onAnyChange();
  });

  // Calendar popout toggling. The native indicator can't toggle: the browser
  // closes an open popout on any outside pointerdown (including on the
  // indicator itself) and the indicator's click then immediately reopens it.
  // Our button tracks believed-open state instead: the pointerdown that
  // closed an open popout marks it, and the click skips the reopen. The
  // state can only go stale via Escape (the popout swallows that key), in
  // which case one click re-syncs and the next toggles -- a soft failure.
  {
    const input = $("#start-date");
    const btn = $("#start-date-toggle");
    let pickerOpen = false;
    let closedByThisPress = false;
    btn.addEventListener("pointerdown", () => {
      closedByThisPress = pickerOpen;
      pickerOpen = false;
    });
    btn.addEventListener("click", () => {
      if (!closedByThisPress && !input.disabled && input.showPicker) {
        try {
          input.showPicker();
          pickerOpen = true;
        } catch (err) {
          // Not allowed (no user activation) or unsupported: nothing to do.
        }
      }
      closedByThisPress = false;
    });
    document.addEventListener("pointerdown", (event) => {
      if (event.target !== btn) pickerOpen = false; // outside click closes it
    });
    input.addEventListener("change", () => {
      pickerOpen = false; // picking a date closes it
    });
  }

  document.querySelectorAll("input, textarea, select").forEach((el) => {
    if (el.id === "target" || el.id === "use-pumpkin-juice") return; // wired above
    if (el.name === "calendar" || el.id === "start-date") return; // wired above
    const changeEvent =
      el.tagName === "SELECT" || el.type === "checkbox" || el.type === "radio" || el.type === "date";
    el.addEventListener(changeEvent ? "change" : "input", onAnyChange);
  });

  // Collapsing the paste pane is a UI preference, not solver state: persist
  // it separately and never trigger a re-solve on toggle.
  const pasteDetails = $("#paste-details");
  try {
    const ui = JSON.parse(localStorage.getItem(UI_STORAGE_KEY) || "{}");
    if (ui.paste_collapsed) pasteDetails.open = false;
  } catch (err) {
    // Corrupt/unavailable storage: keep the default (open).
  }
  pasteDetails.addEventListener("toggle", () => {
    try {
      localStorage.setItem(
        UI_STORAGE_KEY,
        JSON.stringify({ paste_collapsed: !pasteDetails.open })
      );
    } catch (err) {
      // Persistence is a convenience; ignore storage failures.
    }
  });

  // A brand-new session never runs applyParams, so seed the implicit today.
  setStartDateDisplay("");

  if (restoreState()) {
    solve();
  }
}

document.addEventListener("DOMContentLoaded", init);
