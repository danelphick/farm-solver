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

// Trading-value price params (each maps to a config override server-side) and
// the input they read from. Sent only when non-empty; blank means "use the
// bundled default". See webapi._CONFIG_PARAM_FLAGS.
const PRICE_FIELDS = {
  price_arnold_palmer: "#price-arnold-palmer",
  price_apple_cider: "#price-apple-cider",
  price_orange_juice: "#price-orange-juice",
  price_large_net: "#price-large-net",
  price_cookie_set: "#price-cookie-set",
  price_pumpkin_juice: "#price-pumpkin-juice",
  price_hickory_omelette: "#price-hickory-omelette",
};

// --- Config preference panels -----------------------------------------------
//
// The Exploring effectiveness / Farming / Pets / Buildings panes are generated
// from these declarative schemas rather than hand-written markup: each field
// maps to one config `section.flag` override, and they all funnel through the
// single `config` request param (a {dotted key: value} object -- see
// webapi.params_to_args / _config_override_line), the structured sibling of the
// free-text overrides box. A blank/"Default" control sends nothing, so the
// server falls back to config.toml; the field's placeholder shows that default
// (window-injected via the #config-defaults <script>, read by configDefault).
//
// `type` drives both the control and how its value is (de)serialised:
//   int/float -> <input type=number>, sent as a JSON number
//   text/date -> <input type=text|date>, sent as a JSON string
//   bool      -> <input type=checkbox>, initialised to the config default and
//                sent (as a JSON boolean) only when toggled away from it -- so a
//                checkbox left at its default sends nothing, and an auto-detected
//                perk (Scythe of Dewstar, Dewstar's Journal -- absent from the
//                config, so unchecked) stays auto-detected until you tick it.

const EXPLORING_ZONES = [
  "Forest", "Small Cave", "Small Spring", "Highland Hills", "Cane Pole Ridge",
  "Misty Forest", "Black Rock Canyon", "Mount Banon", "Ember Lagoon",
  "Whispering Creek", "Jundland Desert", "Gary's Crushroom", "Sinking Swamp",
];

const PET_NAMES = [
  "Cat", "Wolf", "Owl", "Dog", "Squirrel", "Swine", "Snake", "Lemur", "Baboon",
  "Frog", "Hedgehog", "Fox", "Armadillo", "Spider", "Pet Rock", "Bird",
  "Penguin", "Green Dragon", "Red Dragon", "Blue Dragon", "Bear", "Capybara",
  "Strange Onion", "Seal", "Skunk", "Polar Bear", "Hummingbird", "Shark",
];

// Flat field lists (one control each), keyed by the container's data-config
// attribute in the template.
const CONFIG_FIELDS = {
  effectiveness: EXPLORING_ZONES.map((zone) => ({
    key: `exploring.effectiveness.${zone}`, label: zone, type: "int",
  })),
  farming: [
    { key: "farming.rows", label: "Rows", type: "int" },
    { key: "farming.double_crop_chance", label: "Double crop chance (%)", type: "int" },
    { key: "farming.speed_reduction", label: "Speed reduction (%)", type: "int" },
    { key: "farming.corn_speed_reduction", label: "Corn speed reduction (%)", type: "int" },
    { key: "farming.grape_juices_per_day", label: "Grape Juices / day", type: "int" },
    { key: "farming.cookies_grow_time_threshold", label: "Cookie grow threshold", type: "text" },
    { key: "farming.have_scythe_of_dewstar", label: "Scythe of Dewstar", type: "bool" },
    { key: "farming.have_dewstars_journal", label: "Dewstar's Journal", type: "bool" },
  ],
};

// The Buildings pane groups its fields into <h4> sub-sections; the mills share
// one section (few parameters each) per the spec.
const BUILDING_SECTIONS = [
  { title: "Storehouse", fields: [
    { key: "storehouse.inventory", label: "Inventory", type: "int" },
    { key: "storehouse.growth", label: "Growth / day", type: "int" },
    { key: "storehouse.updated_on", label: "Updated on", type: "date" },
  ] },
  { title: "Farmhouse", fields: [
    { key: "farmhouse.stamina_cap", label: "Stamina cap", type: "int" },
    { key: "farmhouse.registered_on", label: "Registered on", type: "date" },
    { key: "farmhouse.have_mattress_pad", label: "Mattress Pad", type: "bool" },
    { key: "farmhouse.have_apple_pie_enthusiast", label: "Apple Pie Enthusiast", type: "bool" },
  ] },
  { title: "Chicken coop", fields: [
    { key: "chicken_coop.produces", label: "Produces / day", type: "int" },
    { key: "chicken_coop.completed_starmap", label: "Completed starmap", type: "bool" },
  ] },
  { title: "Cow pasture", fields: [
    { key: "cow_pasture.milk", label: "Milk / day", type: "int" },
  ] },
  { title: "Worm habitat", fields: [
    { key: "worm_habitat.worms", label: "Worms / hr", type: "int" },
    { key: "worm_habitat.gummy_worms", label: "Gummy Worms / hr", type: "int" },
    { key: "worm_habitat.mealworms", label: "Mealworms / hr", type: "int" },
  ] },
  { title: "Orchard", fields: [
    { key: "orchard.apple_trees", label: "Apple trees", type: "int" },
    { key: "orchard.orange_trees", label: "Orange trees", type: "int" },
    { key: "orchard.lemon_trees", label: "Lemon trees", type: "int" },
    { key: "orchard.bonus", label: "Bonus multiplier", type: "float" },
    { key: "orchard.have_tree_shaker", label: "Tree Shaker", type: "bool" },
  ] },
  { title: "Raptor pen", fields: [
    { key: "raptor_pen.antlers", label: "Antlers / day", type: "int" },
    { key: "raptor_pen.have_antler_snare", label: "Antler Snare", type: "bool" },
  ] },
  { title: "Trout bait farm", fields: [
    { key: "trout_bait_farm.trout", label: "Trout / day", type: "int" },
    { key: "trout_bait_farm.grubs", label: "Grubs / hr", type: "int" },
    { key: "trout_bait_farm.minnows", label: "Minnows / hr", type: "int" },
  ] },
  { title: "Vineyard", fields: [
    { key: "vineyard.grapes", label: "Grapes / day", type: "int" },
  ] },
  { title: "Steelworks", fields: [
    { key: "steelworks.steel", label: "Steel / hr", type: "int" },
  ] },
  { title: "Hayfield", fields: [
    { key: "hay_field.hay", label: "Straw / 10 min", type: "int" },
  ] },
  { title: "Quarry", fields: [
    { key: "quarry.stone", label: "Stone / 10 min", type: "int" },
    { key: "quarry.coal", label: "Coal / hr", type: "int" },
  ] },
  { title: "Mills", fields: [
    { key: "flour_mill.have_flour_power", label: "Flour Power", type: "bool" },
    { key: "feed_mill.have_feed_boost", label: "Feed Boost", type: "bool" },
    { key: "sugar_cane_mill.have_sugar_boost_i", label: "Sugar Boost I", type: "bool" },
    { key: "sugar_cane_mill.have_sugar_boost_ii", label: "Sugar Boost II", type: "bool" },
  ] },
];

// Bundled config.toml values injected by the index view (see views.index).
let CONFIG_DEFAULTS = {};

function readConfigDefaults() {
  const el = document.getElementById("config-defaults");
  if (!el) return;
  try {
    CONFIG_DEFAULTS = JSON.parse(el.textContent) || {};
  } catch (err) {
    CONFIG_DEFAULTS = {};
  }
}

// Walk a dotted config key ("exploring.effectiveness.Forest") into the injected
// defaults; undefined if the section/flag isn't in the bundled config.
function configDefault(key) {
  return key.split(".").reduce(
    (node, part) => (node == null ? undefined : node[part]), CONFIG_DEFAULTS
  );
}

// One config control: a `.field-row` (or `.field-compact` in a grid) whose
// input/select carries data-config-key/-type for readConfigParams/applyConfigParams.
function createConfigField(field) {
  const row = document.createElement("div");
  row.className = field.compact ? "field-compact" : "field-row";
  const label = document.createElement("label");
  label.textContent = field.label;
  row.appendChild(label);

  const def = configDefault(field.key);
  let input;
  if (field.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!def;  // start at the config default; only a toggle is sent
  } else if (field.type === "date") {
    input = document.createElement("input");
    input.type = "date";
    // Not `.narrow`: a date field needs room for dd/mm/yyyy + the picker button.
    input.classList.add("date-field");
  } else {
    input = document.createElement("input");
    input.type = field.type === "text" ? "text" : "number";
    if (field.type === "float") input.step = "any";
    if (field.type === "int") input.min = "0";
    if (def !== undefined && def !== null) input.placeholder = String(def);
    input.classList.add("narrow");
  }
  input.dataset.configKey = field.key;
  input.dataset.configType = field.type;
  row.appendChild(input);
  return row;
}

// Fill the pane content divs (data-config="...") from the schemas above. Called
// from init() before the blanket input wiring, so each generated control gets
// its onAnyChange listener like a hand-written one.
function buildConfigPanels() {
  ["effectiveness", "farming"].forEach((name) => {
    const container = document.querySelector(`.config-fields[data-config="${name}"]`);
    if (container) CONFIG_FIELDS[name].forEach((f) => container.appendChild(createConfigField(f)));
  });

  const pets = document.querySelector('.config-fields[data-config="pets"]');
  if (pets) {
    pets.appendChild(createConfigField({
      key: "pets.use_crunchy_omelette", label: "Crunchy Omelette (1.5x)", type: "bool",
    }));
    const grid = document.createElement("div");
    grid.className = "pet-grid";
    PET_NAMES.forEach((name) => grid.appendChild(createConfigField({
      key: `pets.${name}`, label: name, type: "int", compact: true,
    })));
    pets.appendChild(grid);
  }

  const buildings = document.querySelector('.config-fields[data-config="buildings"]');
  if (buildings) {
    BUILDING_SECTIONS.forEach((section) => {
      const wrap = document.createElement("div");
      wrap.className = "config-section";
      const heading = document.createElement("h4");
      heading.textContent = section.title;
      wrap.appendChild(heading);
      section.fields.forEach((f) => wrap.appendChild(createConfigField(f)));
      buildings.appendChild(wrap);
    });
  }
}

// Read every generated config control into a {dotted key: value} object, sent
// as the `config` param. A blank input / "Default" select is omitted, so the
// server uses config.toml.
function readConfigParams() {
  const config = {};
  document.querySelectorAll("[data-config-key]").forEach((el) => {
    const key = el.dataset.configKey;
    const type = el.dataset.configType;
    if (type === "bool") {
      // Only a checkbox toggled away from its default is an override.
      if (el.checked !== !!configDefault(key)) config[key] = el.checked;
      return;
    }
    const raw = el.value;
    if (raw === "" || raw == null) return;
    if (type === "int") config[key] = parseInt(raw, 10);
    else if (type === "float") config[key] = parseFloat(raw);
    else config[key] = raw; // text / date -> string
  });
  return config;
}

function applyConfigParams(config) {
  config = config || {};
  document.querySelectorAll("[data-config-key]").forEach((el) => {
    const key = el.dataset.configKey;
    if (el.dataset.configType === "bool") {
      el.checked = key in config ? !!config[key] : !!configDefault(key);
    } else {
      el.value = key in config ? config[key] : "";
    }
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
  "mastery-catalogue": "/static/solverapp/mastery_items.txt",
  "item-catalogue": "/static/solverapp/items.txt",
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

function todayISO() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
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

async function loadCatalogue(url, datalistId) {
  try {
    const response = await fetch(url);
    if (!response.ok) return;
    const text = await response.text();
    const datalist = $(`#${datalistId}`);
    const fragment = document.createDocumentFragment();
    text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        fragment.appendChild(option);
      });
    datalist.appendChild(fragment);
  } catch (err) {
    // No network / no static file yet -- free-typing still works, just
    // without suggestions, same as any other best-effort enhancement here.
  }
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

// --- Reading/writing the form <-> a {mastery_text, inventory_text, params} state ---

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

  if ($("#current-stamina").value) {
    params.current_stamina = Number($("#current-stamina").value);
  }
  if ($("#cookie-sets").value !== "") {
    params.cookie_sets = Number($("#cookie-sets").value);
  }
  // Always sent (a config override, so the UI select is authoritative over the
  // bundled config.toml default); "none" means no sawmill boost.
  params.hickory_omelette = $("#hickory-omelette").value;
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
  if ($("#max-ac").value !== "") params.max_ac = Number($("#max-ac").value);
  const passiveLimit = $("#passive-limit").value.trim();
  if (passiveLimit) params.passive_limit = passiveLimit;
  if ($("#plans").value) params.plans = Number($("#plans").value);
  // The craft breakdown renders into its own collapsible pane, so it is
  // always requested (a refilter field -- asking costs no extra solve).
  params.breakdown = true;

  for (const [key, sel] of Object.entries(PRICE_FIELDS)) {
    if ($(sel).value !== "") params[key] = Number($(sel).value);
  }
  const instantGrow = $("#instant-grow-time").value.trim();
  if (instantGrow) params.instant_grow_time = instantGrow;

  const overrides = $("#overrides").value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (overrides.length) params.overrides = overrides;

  const config = readConfigParams();
  if (Object.keys(config).length) params.config = config;

  if (planSelect) params.plan_select = planSelect;

  return params;
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
  $("#current-stamina").value = params.current_stamina ?? 0;
  $("#cookie-sets").value = params.cookie_sets ?? "";
  $("#hickory-omelette").value = params.hickory_omelette ?? "none";

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
  $("#start-date").disabled = calendarValue !== "full";

  $("#max-gold").value = params.max_gold || "";
  $("#max-ap").value = params.max_ap ?? "";
  $("#max-ln").value = params.max_ln ?? "";
  $("#max-ac").value = params.max_ac ?? "";
  $("#passive-limit").value = params.passive_limit || "";
  $("#plans").value = params.plans || 5;
  for (const [key, sel] of Object.entries(PRICE_FIELDS)) {
    $(sel).value = params[key] ?? "";
  }
  $("#instant-grow-time").value = params.instant_grow_time || "";
  $("#overrides").value = (params.overrides || []).join("\n");
  applyConfigParams(params.config);
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

// What the server said "highest reachable" resolves to (response
// target_level); labels the dropdown option and the Goal pane's summary.
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

  if (data.target_level) {
    towerTargetLevel = data.target_level;
    updateTowerOptionLabel();
    updateGoalSummary();
  }

  $("#solution-section").innerHTML = data.solution_html;
  markUnreachableGoalRows();
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
  const container = $("#solution-section");
  if (!container) return;
  const heading = Array.from(container.querySelectorAll(":scope > h2")).find(
    (h) => h.textContent.trim() === "Goal"
  );
  if (!heading) return;

  const body = [];
  for (let node = heading.nextElementSibling; node && node.tagName !== "H2"; ) {
    const next = node.nextElementSibling;
    body.push(node);
    node = next;
  }

  // Only the goal table sits in `body`; the "Materials with no known source"
  // table is a later <h2> section, outside it.
  const table = body.find((el) => el.tagName === "TABLE");
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
    const details = document.createElement("details");
    details.className = "plan-group";
    details.dataset.groupLabel = label;
    details.open = planGroupOpenState[label] || false;
    // Real clicks toggle synchronously enough for this to stay in sync, but
    // setAllPlanGroupsOpen below writes planGroupOpenState directly rather
    // than relying on this for its batch of programmatic changes -- browsers
    // (and jsdom) don't guarantee "toggle" fires synchronously for those.
    details.addEventListener("toggle", () => {
      planGroupOpenState[label] = details.open;
    });
    const summary = document.createElement("summary");
    summary.textContent = label;
    if (statText) {
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
  document.querySelectorAll("#plan-section > details.plan-group").forEach((details) => {
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
  // Build the generated preference panels (Exploring effectiveness / Farming /
  // Pets / Buildings) first, so their controls exist before the blanket input
  // wiring below attaches onAnyChange and before restoreState/applyParams runs.
  readConfigDefaults();
  buildConfigPanels();

  $("#expand-all-groups").addEventListener("click", () => setAllPlanGroupsOpen(true));
  $("#collapse-all-groups").addEventListener("click", () => setAllPlanGroupsOpen(false));
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
      $("#start-date").disabled = radio.value !== "full";
      onAnyChange();
    });
  });
  $("#start-date").addEventListener("change", () => {
    setStartDateDisplay($("#start-date").value);
    onAnyChange();
  });

  document.querySelectorAll("input, textarea, select").forEach((el) => {
    if (el.id === "target" || el.id === "use-pumpkin-juice") return; // wired above
    if (el.name === "calendar" || el.id === "start-date") return; // wired above
    const changeEvent =
      el.tagName === "SELECT" || el.type === "checkbox" || el.type === "radio" || el.type === "date";
    el.addEventListener(changeEvent ? "change" : "input", onAnyChange);
  });

  // Goal lists (Mastery/Item goals): rows are built dynamically, so they're
  // wired via event delegation on their (stable) container -- see
  // wireGoalList -- rather than the blanket per-input wiring above. Render
  // the default single blank row first; restoreState() below replaces it
  // with saved entries, if any.
  Object.entries(GOAL_LISTS).forEach(([containerId, datalistId]) => {
    renderGoalRows(containerId, [], datalistId);
    wireGoalList(containerId);
  });
  document.querySelectorAll(".btn-add").forEach((btn) => {
    btn.addEventListener("click", () => {
      const containerId = btn.dataset.goal;
      appendGoalRow(containerId, GOAL_LISTS[containerId]);
      onAnyChange();
    });
  });
  Object.entries(GOAL_CATALOGUES).forEach(([datalistId, url]) => {
    loadCatalogue(url, datalistId);
  });

  // Collapsing a pane is a UI preference, not solver state: persist each
  // pane's state separately and never trigger a re-solve on toggle. Each
  // pane's default open/closed state comes from the markup's `open`
  // attribute (limits & crafts start collapsed).
  let ui = {};
  try {
    ui = JSON.parse(localStorage.getItem(UI_STORAGE_KEY) || "{}");
  } catch (err) {
    // Corrupt/unavailable storage: keep the markup defaults.
  }
  if (!ui.panes) ui.panes = {};
  if (ui.paste_collapsed !== undefined) {
    // Migrate the pre-panes shape.
    ui.panes["paste-details"] = !!ui.paste_collapsed;
    delete ui.paste_collapsed;
  }
  document.querySelectorAll("details.pane").forEach((pane) => {
    if (ui.panes[pane.id] !== undefined) pane.open = !ui.panes[pane.id];
    pane.addEventListener("toggle", () => {
      ui.panes[pane.id] = !pane.open;
      try {
        localStorage.setItem(UI_STORAGE_KEY, JSON.stringify(ui));
      } catch (err) {
        // Persistence is a convenience; ignore storage failures.
      }
    });
  });

  // A brand-new session never runs applyParams, so seed the implicit today
  // and the Goal pane's summary.
  setStartDateDisplay("");
  updateGoalSummary();

  if (restoreState()) {
    solve();
  }
}

document.addEventListener("DOMContentLoaded", init);
