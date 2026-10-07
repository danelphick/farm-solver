// The settings popover: every parameter except the solver page's Goal, shared
// by both pages (see templates/solverapp/_settings.html, which both include).
//
// Deliberately importable: unlike app.js this module registers nothing at
// module scope, so the mastery-costs page can pull the same panels, the same
// read/apply code and the same popover in without dragging the solver page's
// wiring along with it.
//
// Cadence -- the reason this is a popover and not a pane: edits inside it are
// STAGED. Opening snapshots the current values; Save closes and calls the
// page's `onSave` (which persists and re-solves); Cancel/Escape/the x/an
// outside click re-apply the snapshot. Nothing in here re-solves on change, so
// pasting a save or retyping a price costs one solve, not one per keystroke.
//
// The panel owns the whole state EXCEPT the goal: the pasted mastery/inventory
// text, the frontier limits, and every preference pane. `GOAL_PARAM_KEYS` names
// what it does not own, and `mergeSettingsIntoState` is how a page folds a
// panel save back into its saved state without touching those.

import {
  backupFilename, buildBackup, byteLength, downloadJson, formatBytes, parseBackup,
  readFileText,
} from "./backup.js";
import { createHelpIcon } from "./help.js";
import {
  getSnapshots,
  parseMasteryText,
  putSnapshot,
  putSnapshots,
  splitBlocks,
} from "./history.js";

// Solver state ({mastery_text, inventory_text, params}, mirroring POST
// /api/solve's body) and, under its own key, UI-only preferences (which panes
// are collapsed, the mastery table's sort) -- both shared by the two pages.
export const STORAGE_KEY = "farm-solver.v1";
export const UI_STORAGE_KEY = "farm-solver.ui.v1";

// The params the panel does NOT own: the solver page's Goal pane, the goals its
// results table has switched off ("ignore_goals") and the frontier row it has
// selected -- the last two live in app.js's module state, not in any control.
// Anything else in a saved state is the panel's to replace -- see
// mergeSettingsIntoState.
export const GOAL_PARAM_KEYS = [
  "target", "mastery", "items", "ignore_goals", "plan_select",
];

function $(selector) {
  return document.querySelector(selector);
}

// --- UI preferences ---------------------------------------------------------

export function readUi() {
  try {
    return JSON.parse(localStorage.getItem(UI_STORAGE_KEY) || "{}") || {};
  } catch (err) {
    return {}; // corrupt/unavailable storage: fall back to the markup defaults
  }
}

export function writeUi(patch) {
  try {
    localStorage.setItem(UI_STORAGE_KEY, JSON.stringify({ ...readUi(), ...patch }));
  } catch (err) {
    // Private browsing / quota exceeded -- persistence is a convenience.
  }
}

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
//                checkbox left at its default sends nothing. For definite
//                settings (perks the game state can't reveal).
//   int/float additionally take optional `min`/`max` bounds (the browser's own
//                validation), for a field with a fixed range like a perk level.
//   autobool  -> Auto-detect / On / Off <select>, for perks the solver
//                auto-detects from mastery/inventory (Scythe of Dewstar and the
//                Mechanical Heart from tower progress; Dewstar's Journal, Fungus
//                Processing and the Swamp Puzzle from inventory). "Auto-detect"
//                (the default) sends nothing, leaving the auto-detection in
//                place; On/Off force a JSON boolean.

const EXPLORING_ZONES = [
  "Forest", "Small Cave", "Small Spring", "Highland Hills", "Cane Pole Ridge",
  "Misty Forest", "Black Rock Canyon", "Mount Banon", "Ember Lagoon",
  "Whispering Creek", "Jundland Desert", "Gary's Crushroom", "Sinking Swamp",
];

// The Pets pane's pets: every numeric (level) key of the injected [pets]
// defaults, which the server completes from pets.csv (webapi.config_defaults)
// -- so a pet added to the game data needs no change here. Its boolean keys
// (use_crunchy_omelette) are flags, not pets.
function petNames() {
  const section = configDefault("pets") || {};
  return Object.keys(section).filter((key) => typeof section[key] === "number");
}

// Flat field lists (one control each), keyed by the container's data-config
// attribute in the template.
const CONFIG_FIELDS = {
  effectiveness: EXPLORING_ZONES.map((zone) => ({
    key: `exploring.effectiveness.${zone}`, label: zone, type: "int",
  })),
  farming: [
    { key: "farming.rows", label: "Rows", type: "int",
      help: "How many crop rows you farm. More rows means more plots harvested per crop cycle." },
    { key: "farming.double_crop_chance", label: "Double crop chance (%)", type: "int",
      help: "Percent chance a harvested seed drops a second crop." },
    { key: "farming.speed_reduction", label: "Speed reduction (%)", type: "int",
      help: "Percent faster every crop grows — your total grow-speed reduction from perks." },
    { key: "farming.corn_speed_reduction", label: "Corn speed reduction (%)", type: "int",
      help: "Extra percent grow-speed reduction that applies to Corn on top of the general one." },
    { key: "farming.grape_juices_per_day", label: "Grape Juices / day", type: "int",
      help: "How many Grape Juices you can press per day; bounds GJ-boosted growing." },
    { key: "farming.cookies_grow_time_threshold", label: "Cookie grow threshold", type: "text",
      help: "Minimum base grow time a crop needs before a Cookie set is worth spending on it (e.g. 18m)." },
    { key: "farming.have_scythe_of_dewstar", label: "Scythe of Dewstar", type: "autobool",
      help: "Scythe of Dewstar perk: +4 bonus crops per row each harvest. Auto-detect infers it from your tower progress (level 250)." },
    { key: "farming.have_dewstars_journal", label: "Dewstar's Journal", type: "autobool",
      help: "Dewstar's Journal: extends the Cookie boost to the Scythe's bonus crops. Auto-detect reads it from your inventory." },
  ],
  // Perks/unlocks/meals for exploring: how much one Arnold Palmer yields, how
  // far an Apple Cider's stamina goes, and what the zones drop (or whether they
  // can be entered at all). Per-zone Exploring Effectiveness is its own pane.
  exploring: [
    { key: "exploring.have_lemon_squeezer", label: "Lemon Squeezer", type: "bool",
      help: "Lemon Squeezer perk: one Arnold Palmer yields 500 items instead of 200." },
    { key: "exploring.use_quandary_chowder", label: "Quandary Chowder", type: "bool",
      help: "Quandary Chowder meal: multiplies what an Arnold Palmer yields by 1.1 (so 500 becomes 550)." },
    { key: "exploring.have_cinnamon_sticks", label: "Cinnamon Sticks", type: "bool",
      help: "Cinnamon Sticks perk: one Apple Cider does more explores per point of Exploring Effectiveness (12.5 over a base of 1250, instead of 10 over 1000)." },
    { key: "exploring.wanderer_level", label: "Wanderer level", type: "int", min: 0, max: 4,
      help: "Wanderer perk level (0–4), cutting the stamina a cider explore costs: 1 = 4%, 2 = 11%, 3 = 20%, 4 = 33%." },
    { key: "exploring.use_neigh", label: "Neigh (meal)", type: "bool",
      help: "Neigh meal: a cider explore costs 20% less stamina, multiplicatively with Wanderer." },
    { key: "exploring.have_fungus_processing", label: "Fungus Processing and You", type: "autobool",
      help: "Fungus Processing and You: Gary's Crushroom drops Mushroom Paste (plus rarer byproducts) instead of Mushroom. Auto-detect reads the artifact from your inventory." },
    { key: "exploring.have_swamp_puzzle", label: "Swamp Puzzle", type: "autobool",
      help: "Swamp Puzzle: unlocks the Sinking Swamp for both exploring AND fishing. Without it the zone is dropped, so nothing it alone drops (Scrap Cloth, and so the Basic Pillow tower level 301 needs) can be sourced. Auto-detect reads the item from your inventory." },
  ],
  // Perks/meals changing how many fish one Large Net catches. (The Sinking Swamp
  // fishing zone is gated on the Swamp Puzzle, which lives in the Exploring pane
  // since it unlocks the exploring location too.)
  fishing: [
    { key: "fishing.have_reinforced_netting", label: "Reinforced Netting", type: "bool",
      help: "Reinforced Netting perk: one Large Net catches 400 fish instead of 250." },
    { key: "fishing.have_fishing_trawl", label: "Fishing Trawl", type: "bool",
      help: "Fishing Trawl perk: +100 fish on top of the base catch." },
    { key: "fishing.use_sea_pincher", label: "Sea Pincher", type: "bool",
      help: "Sea Pincher meal: multiplies the whole catch by 1.1, after the flat bonuses." },
  ],
  // Resource Saver is what a craft yields, and the Mechanical Heart's monthly
  // grants are free consumable supply -- both resource *supply*, so they sit in
  // Resources; the Heart last, beside the Calendar mode that times its grants.
  resources: [
    { key: "workshop.resource_saver", label: "Resource Saver level", type: "int", min: 1, max: 3,
      help: "Resource Saver perk level (1–3): how many items an average craft yields — 1 = 1.10, 2 = 1.25, 3 = 1.45. Level 3 is the tower level 90 reward, so below level 90 a configured 3 is capped back to 2." },
    { key: "exploring.have_mechanical_heart", label: "Mechanical Heart", type: "autobool",
      help: "Mechanical Heart (the tower level 300 artifact): grants 3000 Apple Cider on the 7th and 2000 Arnold Palmer on the 14th of each month, added to passive production. Auto-detect infers it from your tower progress (level 300)." },
  ],
  // The two [trading] values with no dedicated price param: grapes is priced in
  // AP (not gold) like the material-trading pane, and price_stamina is a switch,
  // not a price -- both ride the generic config channel instead.
  trading: [
    { key: "trading.grapes", label: "Grapes /1k (AP)", type: "float",
      help: "Trade price of grapes, in Arnold Palmers per 1,000 (not gold — grapes trade for AP). Grape Juice is never bought directly; its value is derived from this, since pressing 200 grapes makes one." },
    { key: "trading.price_stamina", label: "Charge for free stamina", type: "bool",
      help: "Charge stamina an Apple Cider explore burns its Orange-Juice-equivalent cost (Orange Juice price ÷ 100 per stamina), so a plan draining banked or Farmhouse stamina is not scored as free. Stamina from drinking Orange Juice is exempt (already priced as the juice)." },
  ],
};

// The Buildings pane groups its fields into <h4> sub-sections; the mills share
// one section (few parameters each) per the spec.
const BUILDING_SECTIONS = [
  { title: "Storehouse", fields: [
    { key: "storehouse.inventory", label: "Inventory", type: "int",
      help: "Storehouse capacity on the 'Updated on' date; it caps how much passive production banks at once." },
    { key: "storehouse.growth", label: "Growth / day", type: "int",
      help: "How much larger the storehouse gets each day since the 'Updated on' date." },
    { key: "storehouse.updated_on", label: "Updated on", type: "date",
      help: "The date the Inventory figure was accurate; capacity is projected forward from here." },
  ] },
  { title: "Farmhouse", fields: [
    { key: "farmhouse.stamina_cap", label: "Stamina cap", type: "int",
      help: "Your natural maximum stamina on the 'Registered on' date (the Farmhouse raises it over time)." },
    { key: "farmhouse.registered_on", label: "Registered on", type: "date",
      help: "The date the Stamina cap figure was accurate; the cap grows daily from here." },
    { key: "farmhouse.have_mattress_pad", label: "Mattress Pad", type: "bool",
      help: "Mattress Pad perk: the stamina cap grows +2/day instead of +1." },
    { key: "farmhouse.have_apple_pie_enthusiast", label: "Apple Pie Enthusiast", type: "bool",
      help: "Apple Pie Enthusiast perk: daily stamina gain is capped at 1M instead of 500k." },
  ] },
  { title: "Sawmill", fields: [
    { key: "sawmill.wood", label: "Wood / hr", type: "int",
      help: "Wood the sawmill produces per hour." },
    { key: "sawmill.board", label: "Board / hr", type: "int",
      help: "Board the sawmill produces per hour." },
    { key: "sawmill.oak", label: "Oak / hr", type: "int",
      help: "Oak the sawmill produces per hour — 0 if you have no Sturdy Saw. Use the post-perk number. (The Hickory Omelette boost is set under Resources.)" },
    { key: "sawmill.have_pine_boost", label: "Pine Boost", type: "bool",
      help: "Pine Boost: the sawmill makes 2 pine board per minute instead of 1." },
  ] },
  { title: "Chicken coop", fields: [
    { key: "chicken_coop.produces", label: "Produces / day", type: "int",
      help: "Eggs the coop produces per day; it yields the same number of feathers alongside them." },
    { key: "chicken_coop.completed_starmap", label: "Completed starmap", type: "bool",
      help: "Completed Starmap: 5% as many Gold Feathers as the coop's daily feathers, on top of them. The eggs and feathers themselves are unaffected." },
  ] },
  { title: "Cow pasture", fields: [
    { key: "cow_pasture.milk", label: "Milk / day", type: "int",
      help: "Milk the pasture produces per day." },
    { key: "cow_pasture.has_mariyas_wheelbarrow", label: "Mariya's Wheelbarrow", type: "autobool",
      help: "Mariya's Wheelbarrow: the herd sheds Horns alongside the milk, 3 per milk, in the same daily deposit. Auto-detect reads the item from your inventory." },
  ] },
  { title: "Worm habitat", fields: [
    { key: "worm_habitat.worms", label: "Worms / hr", type: "int",
      help: "Worms the habitat produces per hour." },
    { key: "worm_habitat.gummy_worms", label: "Gummy Worms / hr", type: "int",
      help: "Gummy Worms produced per hour." },
    { key: "worm_habitat.mealworms", label: "Mealworms / hr", type: "int",
      help: "Mealworms produced per hour." },
  ] },
  { title: "Orchard", fields: [
    { key: "orchard.apple_trees", label: "Apple trees", type: "int",
      help: "Number of apple trees; each yields apples daily." },
    { key: "orchard.orange_trees", label: "Orange trees", type: "int",
      help: "Number of orange trees; each yields oranges daily." },
    { key: "orchard.lemon_trees", label: "Lemon trees", type: "int",
      help: "Number of lemon trees; each yields lemons daily." },
    { key: "orchard.bonus", label: "Bonus multiplier", type: "float",
      help: "Orchard yield multiplier from perks (e.g. 1.3 = +30% fruit)." },
    { key: "orchard.have_tree_shaker", label: "Tree Shaker", type: "bool",
      help: "Tree Shaker perk: an extra daily harvest with its own storehouse-cap window." },
    { key: "orchard.oddvocado_trees", label: "Oddvocado trees", type: "int",
      help: "Number of Oddvocado Trees; each bears one random fruit at noon daily (Pear, Avocado, Cherries, Lemon, Raspberries, Orange or Blueberries; Avocado most often, Blueberries least)." },
  ] },
  { title: "Raptor pen", fields: [
    { key: "raptor_pen.antlers", label: "Antlers / day", type: "int",
      help: "Antlers the raptor pen produces per day." },
    { key: "raptor_pen.have_antler_snare", label: "Antler Snare", type: "bool",
      help: "Antler Snare: a second daily antler deposit with its own cap window." },
  ] },
  { title: "Trout bait farm", fields: [
    { key: "trout_bait_farm.trout", label: "Trout / day", type: "int",
      help: "Trout produced per day." },
    { key: "trout_bait_farm.grubs", label: "Grubs / hr", type: "int",
      help: "Grubs produced per hour." },
    { key: "trout_bait_farm.minnows", label: "Minnows / hr", type: "int",
      help: "Minnows produced per hour." },
  ] },
  { title: "Vineyard", fields: [
    { key: "vineyard.grapes", label: "Grapes / day", type: "int",
      help: "Grapes the vineyard produces per day (pressed into Grape Juice)." },
  ] },
  { title: "Steelworks", fields: [
    { key: "steelworks.steel", label: "Steel / hr", type: "int",
      help: "Steel produced per hour; also yields steel wire at a third of this rate." },
  ] },
  { title: "Hayfield", fields: [
    { key: "hay_field.hay", label: "Straw / 10 min", type: "int",
      help: "Straw produced every ten minutes." },
  ] },
  { title: "Quarry", fields: [
    { key: "quarry.stone", label: "Stone / 10 min", type: "int",
      help: "Stone produced every ten minutes." },
    { key: "quarry.coal", label: "Coal / hr", type: "int",
      help: "Coal produced per hour." },
  ] },
  { title: "Mills", fields: [
    { key: "flour_mill.have_flour_power", label: "Flour Power", type: "bool",
      help: "Flour Power: the flour mill makes 2 flour/min instead of 1." },
    { key: "feed_mill.have_feed_boost", label: "Feed Boost", type: "bool",
      help: "Feed Boost: the feed mill makes 2 feed/min instead of 1." },
    { key: "sugar_cane_mill.have_sugar_boost_i", label: "Sugar Boost I", type: "bool",
      help: "Sugar Boost I: doubles the sugar cane mill's output." },
    { key: "sugar_cane_mill.have_sugar_boost_ii", label: "Sugar Boost II", type: "bool",
      help: "Sugar Boost II: doubles the sugar cane mill's output again (needs Sugar Boost I)." },
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

// The initial value of an autobool <select>: "" (Auto-detect) unless the config
// pins the perk on/off, in which case it starts at "true"/"false".
function autoboolDefaultValue(key) {
  const def = configDefault(key);
  return def == null ? "" : String(!!def);
}

// One config control: a `.field-row` (or `.field-compact` in a grid) whose
// input/select carries data-config-key/-type for readConfigParams/applyConfigParams.
function createConfigField(field) {
  const row = document.createElement("div");
  row.className = field.compact ? "field-compact" : "field-row";
  const label = document.createElement("label");
  label.textContent = field.label;
  if (field.help) label.appendChild(createHelpIcon(field.help));
  row.appendChild(label);

  const def = configDefault(field.key);
  let input;
  if (field.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!def;  // start at the config default; only a toggle is sent
  } else if (field.type === "autobool") {
    input = document.createElement("select");
    input.add(new Option("Auto-detect", ""));
    input.add(new Option("On", "true"));
    input.add(new Option("Off", "false"));
    input.classList.add("narrow");
    input.value = autoboolDefaultValue(field.key);  // "" (auto) unless config forces it
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
    if (field.min != null) input.min = String(field.min);
    if (field.max != null) input.max = String(field.max);
    if (def !== undefined && def !== null) input.placeholder = String(def);
    input.classList.add("narrow");
  }
  input.dataset.configKey = field.key;
  input.dataset.configType = field.type;
  row.appendChild(input);
  return row;
}

// Fill the pane content divs (data-config="...") from the schemas above. Called
// from initSettings() before anything reads or applies params, so every
// generated control exists by the time a saved state is restored into it.
function buildConfigPanels() {
  ["effectiveness", "farming", "exploring", "fishing", "resources", "trading"].forEach((name) => {
    const container = document.querySelector(`.config-fields[data-config="${name}"]`);
    if (container) CONFIG_FIELDS[name].forEach((f) => container.appendChild(createConfigField(f)));
  });

  const pets = document.querySelector('.config-fields[data-config="pets"]');
  if (pets) {
    pets.appendChild(createConfigField({
      key: "pets.use_crunchy_omelette", label: "Crunchy Omelette (1.5x)", type: "bool",
      help: "Crunchy Omelette: a 1.5x multiplier on every pet's item retrieval.",
    }));
    const grid = document.createElement("div");
    grid.className = "pet-grid";
    petNames().forEach((name) => grid.appendChild(createConfigField({
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

// Every control that mirrors a config key carries `data-config-key` (plus a
// `data-config-type`), generated or hand-written, so an external tool can find a
// field by what it means rather than by CSS id -- see _settings.html's comment.
// Only the generated ones ride the generic `config` param, though: a
// hand-written control tagged `data-config-own-param` travels under its own
// request param (a price, the calendar mode, ...), so the two readers below
// skip it and it is never sent twice.
const GENERIC_CONFIG_CONTROLS = "[data-config-key]:not([data-config-own-param])";

// Fill in the placeholder of any own-param control whose config key has a
// bundled default, so those hand-written fields show the same "blank means
// this" hint the generated ones do, from the same source, instead of a number
// hard-coded in the markup that quietly drifts from the config. Controls whose
// key the injected config doesn't carry (solver.* is stripped from it) keep the
// markup's own placeholder.
function applyOwnParamPlaceholders() {
  document.querySelectorAll("[data-config-key][data-config-own-param]").forEach((el) => {
    if (el.tagName !== "INPUT" || el.type === "checkbox" || el.type === "radio") return;
    const def = configDefault(el.dataset.configKey);
    if (def !== undefined && def !== null) el.placeholder = String(def);
  });
}

// Read every generated config control into a {dotted key: value} object, sent
// as the `config` param. A blank input / "Default" select is omitted, so the
// server uses config.toml.
function readConfigParams() {
  const config = {};
  document.querySelectorAll(GENERIC_CONFIG_CONTROLS).forEach((el) => {
    const key = el.dataset.configKey;
    const type = el.dataset.configType;
    if (type === "bool") {
      // Only a checkbox toggled away from its default is an override.
      if (el.checked !== !!configDefault(key)) config[key] = el.checked;
      return;
    }
    const raw = el.value;
    if (raw === "" || raw == null) return;  // "" autobool = Auto-detect, no override
    if (type === "autobool") config[key] = raw === "true";
    else if (type === "int") config[key] = parseInt(raw, 10);
    else if (type === "float") config[key] = parseFloat(raw);
    else config[key] = raw; // text / date -> string
  });
  return config;
}

function applyConfigParams(config) {
  config = config || {};
  document.querySelectorAll(GENERIC_CONFIG_CONTROLS).forEach((el) => {
    const key = el.dataset.configKey;
    const type = el.dataset.configType;
    if (type === "bool") {
      el.checked = key in config ? !!config[key] : !!configDefault(key);
    } else if (type === "autobool") {
      el.value = key in config ? String(!!config[key]) : autoboolDefaultValue(key);
    } else {
      el.value = key in config ? config[key] : "";
    }
  });
}

// --- Material trading pane --------------------------------------------------
//
// Which raw materials the plan may trade for Arnold Palmers, and at what price.
// Everything comes from the injected config's `[trading.ap]` section: `prices`
// (item -> default apk) is the item universe, `price_bounds` (item -> [low,high])
// is the greyed guide range, and `enabled`/`allow`/`deny` give the default
// checkbox state. The pane always emits an ALLOW list (never a deny list -- see
// webapi._ap_trading_override_lines), sent as the `ap_trading` param only when it
// deviates from those defaults, so an untouched pane adds nothing to the request.

function apTradingConfig() {
  return (CONFIG_DEFAULTS.trading && CONFIG_DEFAULTS.trading.ap) || {};
}

// Every tradeable item, sorted -- the keys of [trading.ap.prices].
function apTradingItems() {
  return Object.keys(apTradingConfig().prices || {}).sort();
}

function apDefaultPrice(item) {
  return (apTradingConfig().prices || {})[item];
}

// [low, high] guide bounds for an item, or undefined.
function apPriceBounds(item) {
  return (apTradingConfig().price_bounds || {})[item];
}

// Normalise a config allow/deny entry the way config._normalise_item does, so a
// TOML key written with underscores matches the spaced item name.
function normaliseItem(name) {
  return String(name).replace(/_/g, " ").trim().toLowerCase();
}

// The set of items tradeable by default, given a `[trading.ap]` config table and
// the item universe -- what "Restore defaults" resets to (mirrors
// config.arnold_palmer_prices). Pure, so it can be unit-tested.
export function computeDefaultAllowed(ap, items) {
  ap = ap || {};
  if (ap.enabled === false) return new Set();
  if (Array.isArray(ap.allow)) {
    const keep = new Set(ap.allow.map(normaliseItem));
    return new Set(items.filter((i) => keep.has(normaliseItem(i))));
  }
  if (Array.isArray(ap.deny)) {
    const drop = new Set(ap.deny.map(normaliseItem));
    return new Set(items.filter((i) => !drop.has(normaliseItem(i))));
  }
  return new Set(items); // enabled, no filter -> everything
}

function defaultAllowedItems() {
  return computeDefaultAllowed(apTradingConfig(), apTradingItems());
}

// Core of readMaterialTrading, pure for testing: given the checked items, the
// map of items whose price was edited, the default allow set, and a
// default-price lookup, return the `ap_trading` param -- or null when nothing
// deviates from the config default. `prices` is included only when a price was
// edited, and then carries every allowed item so it can replace the whole table.
export function computeApTradingParam(allowed, edited, defaultAllowed, defaultPriceOf) {
  const sameAllow =
    allowed.length === defaultAllowed.size && allowed.every((i) => defaultAllowed.has(i));
  const anyEdited = Object.keys(edited).length > 0;
  if (sameAllow && !anyEdited) return null;

  const result = { allow: allowed };
  if (anyEdited) {
    const prices = {};
    allowed.forEach((item) => {
      prices[item] = item in edited ? edited[item] : defaultPriceOf(item);
    });
    result.prices = prices;
  }
  return result;
}

function titleCaseItem(name) {
  return name.replace(/\b\w/g, (c) => c.toUpperCase());
}

// Build one checkbox + price + bounds row per tradeable item, checked to the
// config default. Called from initSettings(), like buildConfigPanels, before
// any saved `ap_trading` is applied back onto the rows.
function buildMaterialTradingPane() {
  const list = document.getElementById("material-trading-list");
  if (!list) return;
  const defaults = defaultAllowedItems();
  apTradingItems().forEach((item) => {
    const row = document.createElement("div");
    row.className = "material-row";
    row.dataset.item = item;

    const label = document.createElement("label");
    label.className = "material-check";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.dataset.apItem = item;
    check.checked = defaults.has(item);
    const name = document.createElement("span");
    name.className = "material-name";
    name.textContent = titleCaseItem(item);
    label.append(check, name);

    const price = document.createElement("input");
    price.type = "number";
    price.min = "0";
    price.step = "any";
    price.className = "material-price narrow";
    price.dataset.apPrice = item;
    const def = apDefaultPrice(item);
    if (def !== undefined) price.placeholder = String(def);

    const bounds = document.createElement("span");
    bounds.className = "material-bounds";
    const range = apPriceBounds(item);
    if (Array.isArray(range)) {
      bounds.textContent = range[0] === range[1] ? String(range[0]) : `${range[0]}–${range[1]}`;
    }

    row.append(label, price, bounds);
    list.appendChild(row);
  });
}

// Read the pane into an `ap_trading` param, or null when it matches the config
// default (so an untouched pane sends nothing). `prices` is included only when a
// price was actually edited, and then carries EVERY allowed item (edited value or
// default) because the override replaces the whole [trading.ap.prices] table.
function readMaterialTrading() {
  const rows = document.querySelectorAll("#material-trading-list .material-row");
  if (!rows.length) return null;
  const allowed = [];
  const edited = {};
  rows.forEach((row) => {
    const item = row.dataset.item;
    if (!row.querySelector("[data-ap-item]").checked) return;
    allowed.push(item);
    const raw = row.querySelector("[data-ap-price]").value;
    if (raw !== "") edited[item] = Number(raw);
  });
  return computeApTradingParam(allowed, edited, defaultAllowedItems(), apDefaultPrice);
}

// Restore the pane from a saved `ap_trading` param (or the config default when
// absent/null). A custom price fills its input; a price equal to the default is
// left as an empty placeholder, so the round-trip doesn't turn every default into
// a phantom "edit".
function applyMaterialTrading(apTrading) {
  const rows = document.querySelectorAll("#material-trading-list .material-row");
  if (!rows.length) return;
  const hasOverride = apTrading && Array.isArray(apTrading.allow);
  const allowedSet = hasOverride ? new Set(apTrading.allow) : defaultAllowedItems();
  const prices = (hasOverride && apTrading.prices) || {};
  rows.forEach((row) => {
    const item = row.dataset.item;
    row.querySelector("[data-ap-item]").checked = allowedSet.has(item);
    const priceInput = row.querySelector("[data-ap-price]");
    priceInput.value =
      item in prices && Number(prices[item]) !== Number(apDefaultPrice(item))
        ? prices[item]
        : "";
  });
}

// Both of these are staged edits like any other control in the panel: they
// change the rows and stop there, taking effect on Save (and revertible by
// Cancel, which re-applies the snapshot over them).
function setAllMaterials(on) {
  document
    .querySelectorAll("#material-trading-list [data-ap-item]")
    .forEach((cb) => { cb.checked = on; });
}

function restoreMaterialDefaults() {
  applyMaterialTrading(null);
}

// Filter the visible rows by name substring (UI-only, not part of the state).
function filterMaterialRows() {
  const q = ($("#material-trading-filter").value || "").trim().toLowerCase();
  document.querySelectorAll("#material-trading-list .material-row").forEach((row) => {
    row.hidden = q !== "" && !row.dataset.item.includes(q);
  });
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

// The lo/hi range only means anything for an axis actually sampling a range
// (pumpkin-juice/time/gold); "passive" enumerates its own breakpoints and the
// default "" (no spread) has no range either, so the row stays hidden for both.
function updateSpreadRangeVisibility() {
  const axis = $("#spread-axis").value;
  $("#spread-range-row").hidden = axis === "" || axis === "passive";
}

// --- Reading/writing the panel <-> a params object ---------------------------
//
// The two halves of the state round-trip, minus the Goal pane (app.js owns
// that): readSettingsParams collects every control the panel holds into the
// same params shape POST /api/solve takes, applySettingsParams puts one back.
// They are exact inverses -- which is what makes Cancel a one-liner (re-apply
// the snapshot) as well as making a page reload restore the panel.

export function readSettingsParams() {
  const params = {};

  // Blank means "use the config's [farmhouse] current_stamina" (like Cookie
  // sets' "from inventory"), so an explicit 0 -- "I have none banked right now"
  // -- has to be sent, and only an empty box is left out.
  if ($("#current-stamina").value !== "") {
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
  // Only sent when unchecked (off) -- the default (count juice in net gold) is
  // left to the bundled config, so this stays a toggle like the other flags.
  if (!$("#count-pumpkin-juice-gold").checked) {
    params.count_pumpkin_juice_gold = false;
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

  const spreadAxis = $("#spread-axis").value;
  if (spreadAxis) {
    // AXIS[=LO:HI] -- the same grammar --spread/webapi's "spread" param parses
    // (main._parse_spread); a blank lo/hi leaves that axis's own default range.
    const lo = $("#spread-lo").value.trim();
    const hi = $("#spread-hi").value.trim();
    params.spread = lo !== "" && hi !== "" ? `${spreadAxis}=${lo}:${hi}` : spreadAxis;
  }
  const graphMetric = $("#graph-metric").value;
  if (graphMetric) params.graph = graphMetric;

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

  const apTrading = readMaterialTrading();
  if (apTrading) params.ap_trading = apTrading;

  return params;
}

export function applySettingsParams(params) {
  params = params || {};
  $("#current-stamina").value = params.current_stamina ?? "";
  $("#cookie-sets").value = params.cookie_sets ?? "";
  $("#hickory-omelette").value = params.hickory_omelette ?? "none";

  const hasPj = params.use_pumpkin_juice !== undefined && params.use_pumpkin_juice !== null;
  $("#use-pumpkin-juice").checked = hasPj;
  $("#pumpkin-juice-cap").disabled = !hasPj;
  $("#pumpkin-juice-cap").value = hasPj && params.use_pumpkin_juice >= 0 ? params.use_pumpkin_juice : "";
  // Absent means the default (count juice in net gold); only an explicit false unchecks.
  $("#count-pumpkin-juice-gold").checked = params.count_pumpkin_juice_gold !== false;

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

  const spreadSpec = params.spread || "";
  const [spreadAxisValue, spreadRange] = spreadSpec.split("=");
  $("#spread-axis").value = spreadAxisValue || "";
  const [spreadLo, spreadHi] = spreadRange ? spreadRange.split(":") : ["", ""];
  $("#spread-lo").value = spreadLo || "";
  $("#spread-hi").value = spreadHi || "";
  updateSpreadRangeVisibility();
  $("#graph-metric").value = params.graph || "";

  for (const [key, sel] of Object.entries(PRICE_FIELDS)) {
    $(sel).value = params[key] ?? "";
  }
  $("#instant-grow-time").value = params.instant_grow_time || "";
  $("#overrides").value = (params.overrides || []).join("\n");
  applyConfigParams(params.config);
  applyMaterialTrading(params.ap_trading);
}

// How many of `item` a pasted inventory holds (0 when it has none), matched
// case-insensitively. The JS mirror of parsing.parse_inventory's reading of a
// block: the name is its first line, the quantity its last -- after dropping a
// trailing "<Category> chevron_down" header that belongs to the next items. A
// block it cannot read is skipped rather than thrown on: this only labels a
// field, and the solver reports a bad paste itself.
export function inventoryQuantity(text, item) {
  const wanted = item.toLowerCase();
  for (const block of splitBlocks(text)) {
    if (block[block.length - 1].endsWith(" chevron_down")) block.pop();
    if (!block.length || block[0].toLowerCase() !== wanted) continue;
    const quantity = block[block.length - 1];
    if (/^[\d,]+$/.test(quantity)) return Number(quantity.replace(/,/g, ""));
  }
  return 0;
}

// "You have 22 in inventory." appended to the Pumpkin Juice cap's help tooltip,
// so the cap can be set to the juice actually on hand. Only the base text until
// an inventory is pasted. help.js reads data-help on each hover, so rewriting it
// here is enough.
let pumpkinJuiceHelp = null;
function showPumpkinJuiceHeld() {
  const icon = $("#pumpkin-juice-help");
  if (pumpkinJuiceHelp === null) pumpkinJuiceHelp = icon.dataset.help;
  const text = $("#inventory-text").value;
  icon.dataset.help = text.trim()
    ? `${pumpkinJuiceHelp} You have ${inventoryQuantity(text, "Pumpkin Juice").toLocaleString()} in inventory.`
    : pumpkinJuiceHelp;
}

// The pasted game state lives in the panel too, so a snapshot (and a save) is
// the whole state bar the goal.
export function readSettingsState() {
  return {
    mastery_text: $("#mastery-text").value,
    inventory_text: $("#inventory-text").value,
    params: readSettingsParams(),
  };
}

export function applySettingsState(state) {
  state = state || {};
  $("#mastery-text").value = state.mastery_text || "";
  $("#inventory-text").value = state.inventory_text || "";
  showPumpkinJuiceHeld();
  applySettingsParams(state.params);
}

// Fold a panel save into a saved state: keep the goal (which the panel does not
// show, so it must not touch) and let the panel replace everything else. The
// non-goal keys are DROPPED rather than merged, so a field the user blanked --
// which readSettingsParams omits entirely -- really goes away instead of
// lingering from the old state. Pure, so settings.test.js can pin that.
export function mergeSettingsIntoState(saved, settingsState) {
  const savedParams = (saved && saved.params) || {};
  const goal = {};
  GOAL_PARAM_KEYS.forEach((key) => {
    if (savedParams[key] !== undefined) goal[key] = savedParams[key];
  });
  return {
    mastery_text: settingsState.mastery_text,
    inventory_text: settingsState.inventory_text,
    params: { ...goal, ...settingsState.params },
  };
}

// --- Pane open/closed states -------------------------------------------------

// Collapsing a pane is a UI preference, not solver state: persist each pane's
// state under the UI key (never the solver state) and never re-solve on toggle.
// Each pane's default comes from the markup's `open` attribute. Document-wide,
// so it covers the panel's panes plus each page's own (the solver page's
// Required crafts, the mastery page's two "cannot" lists).
function restorePaneStates() {
  const ui = readUi();
  const panes = ui.panes || {};
  if (ui.paste_collapsed !== undefined) {
    // Migrate the pre-panes shape.
    panes["paste-details"] = !!ui.paste_collapsed;
    writeUi({ panes, paste_collapsed: undefined });
  }
  document.querySelectorAll("details.pane").forEach((pane) => {
    if (panes[pane.id] !== undefined) pane.open = !panes[pane.id];
    pane.addEventListener("toggle", () => {
      writeUi({ panes: { ...(readUi().panes || {}), [pane.id]: !pane.open } });
    });
  });
}

// --- The popover -------------------------------------------------------------

// What the panel held when it was opened, re-applied by Cancel; null while
// closed. The button and panel are held by reference rather than re-queried, so
// the document-level Escape/outside-click handlers below act on the panel this
// module wired -- never on a later one that replaced it.
let snapshot = null;
let onSaveCallback = null;
let popoverEl = null;
let buttonEl = null;

function settingsOpen() {
  return !!popoverEl && popoverEl.isConnected && !popoverEl.hidden;
}

function openSettings() {
  snapshot = readSettingsState();
  // Refreshed here, not only in applySettingsState: the solver page restores the
  // paste with its own restoreState, which never goes through this module.
  showPumpkinJuiceHeld();
  popoverEl.hidden = false;
  buttonEl.setAttribute("aria-expanded", "true");
  popoverEl.focus();
}

// `revert` distinguishes Cancel (and its Escape/x/outside-click equivalents)
// from Save, which has already handed the staged values to the page.
function closeSettings({ revert }) {
  if (revert && snapshot) applySettingsState(snapshot);
  snapshot = null;
  popoverEl.hidden = true;
  buttonEl.setAttribute("aria-expanded", "false");
  buttonEl.focus();
}

function saveSettings() {
  closeSettings({ revert: false });
  if (onSaveCallback) onSaveCallback();
}

// --- Mastery snapshots -------------------------------------------------------
//
// "Save dated snapshot" is the one control in this panel that is NOT staged: it
// writes the mastery box into this browser's IndexedDB history the moment it is
// clicked, and so survives a Cancel. That is deliberate -- a snapshot is
// history, not a setting, and staging it behind Save would mean the panel's
// revert quietly threw away a recorded day. The help text says as much.
//
// Everything here degrades to a no-op when there is no IndexedDB (jsdom in the
// page tests, a locked-down private mode): openHistoryDb resolves null and
// putSnapshot returns null, the same way readUi() shrugs off unusable
// localStorage.

function setSnapshotStatus(text, kind) {
  const el = $("#snapshot-status");
  if (!el) return;
  el.textContent = text;
  el.className = kind ? "status-" + kind : "";
}

// "Last snapshot: 2026-07-24 21:03 (712 items) · 19 kept" -- shown on init and
// restated after each save, so a second save the same day visibly adds a
// snapshot instead of looking like it did nothing.
async function showSnapshotSummary() {
  try {
    const snapshots = await getSnapshots();
    if (!snapshots.length) {
      setSnapshotStatus("No snapshots saved yet.");
      return;
    }
    const latest = snapshots[snapshots.length - 1];
    const when = new Date(latest.taken_at);
    const items = Object.keys(latest.items || {}).length;
    setSnapshotStatus(
      `Last snapshot: ${latest.date} ${String(when.getHours()).padStart(2, "0")}:` +
        `${String(when.getMinutes()).padStart(2, "0")} ` +
        `(${items.toLocaleString()} items) · ${snapshots.length} kept`
    );
  } catch (err) {
    setSnapshotStatus(""); // history unavailable: say nothing rather than alarm
  }
}

async function saveSnapshot() {
  const text = $("#mastery-text").value;
  if (!text.trim()) {
    setSnapshotStatus("Paste your masteries first.", "error");
    return;
  }
  let items;
  try {
    // Throws on a block it cannot read, exactly as parsing.parse_masteries
    // does server-side -- the same paste would 422 a solve.
    items = parseMasteryText(text);
  } catch (err) {
    setSnapshotStatus("Could not read the paste: " + err.message, "error");
    return;
  }
  if (!Object.keys(items).length) {
    setSnapshotStatus("No masteries found in the paste.", "error");
    return;
  }
  try {
    const record = await putSnapshot(items);
    if (!record) {
      setSnapshotStatus("This browser cannot store history.", "error");
      return;
    }
  } catch (err) {
    setSnapshotStatus("Could not save: " + err.message, "error");
    return;
  }
  await showSnapshotSummary();
}

// --- Backup / restore --------------------------------------------------------
//
// Nothing here is stored on a server, so a backup file is the only way to move
// a save between browsers -- or to survive clearing site data. The pair at the
// bottom of the panel writes/reads one JSON file holding the solver state, the
// UI preferences and the whole mastery history (see backup.js for the format).
//
// Like the snapshot button these are NOT staged: Back up captures what is on
// screen right now, and Restore rewrites this browser's storage immediately.
// Restore is destructive -- it replaces the saved state and preferences
// wholesale -- so it asks first, validates the entire file before writing
// anything, and reloads afterwards rather than trying to graft a foreign state
// onto a live page (the goal pane, the results and the timeline all read their
// own slices of it at init).

function setBackupStatus(text, kind) {
  const el = $("#backup-status");
  if (!el) return;
  el.textContent = text;
  el.className = kind ? "status-" + kind : "";
}

// The saved solver state. Each page has its own copy of this reader (it owns
// the goal half of the state); this one exists so the panel can back up and
// restore the whole thing without importing a page module.
function readSavedState() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {};
  } catch (err) {
    return {};
  }
}

function plural(count, noun) {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}

// What the file gets: the panel's LIVE values (so a paste made seconds ago is
// in the backup even though Save hasn't been pressed) folded over the saved
// state's goal, which the panel does not show.
async function backupToFile() {
  try {
    const state = mergeSettingsIntoState(readSavedState(), readSettingsState());
    const snapshots = await getSnapshots();
    const text = JSON.stringify(buildBackup({ state, ui: readUi(), snapshots }));
    downloadJson(backupFilename(), text);
    setBackupStatus(
      `Backed up ${plural(snapshots.length, "snapshot")} and every setting ` +
        `(${formatBytes(byteLength(text))}).`
    );
  } catch (err) {
    setBackupStatus("Could not back up: " + err.message, "error");
  }
}

async function restoreFromFile(file) {
  let backup;
  try {
    backup = parseBackup(await readFileText(file));
  } catch (err) {
    setBackupStatus("Could not restore: " + err.message, "error");
    return;
  }
  const confirmed = window.confirm(
    `Restore ${plural(backup.snapshots.length, "snapshot")} and the saved settings ` +
      "from this file?\n\nThe game state and options in this browser will be " +
      "replaced. History is merged, not replaced."
  );
  if (!confirmed) {
    setBackupStatus("Restore cancelled.");
    return;
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(backup.state));
    localStorage.setItem(UI_STORAGE_KEY, JSON.stringify(backup.ui));
    if (backup.snapshots.length) await putSnapshots(backup.snapshots);
  } catch (err) {
    setBackupStatus("Could not restore: " + err.message, "error");
    return;
  }
  setBackupStatus("Restored. Reloading…");
  window.location.reload();
}

// The controls whose only job is to update the panel itself: enabling a
// dependent input, or showing a row. None of them save or solve -- that is
// Save's job -- so unlike the goal controls in app.js they get no onAnyChange.
function wireSettingsControls() {
  $("#save-snapshot").addEventListener("click", saveSnapshot);
  $("#backup-save").addEventListener("click", backupToFile);
  // The button is the affordance; the input is the file dialog behind it.
  $("#backup-restore").addEventListener("click", () => $("#backup-file").click());
  $("#backup-file").addEventListener("change", (event) => {
    const [file] = event.target.files || [];
    // Cleared so that picking the SAME file again still fires a change event
    // (a restore the user cancelled, or a file they have since edited).
    event.target.value = "";
    if (file) restoreFromFile(file);
  });
  $("#inventory-text").addEventListener("input", showPumpkinJuiceHeld);
  $("#use-pumpkin-juice").addEventListener("change", () => {
    $("#pumpkin-juice-cap").disabled = !$("#use-pumpkin-juice").checked;
  });
  document.querySelectorAll('input[name="calendar"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      $("#start-date").disabled = radio.value !== "full";
    });
  });
  $("#start-date").addEventListener("change", () => {
    setStartDateDisplay($("#start-date").value);
  });
  $("#spread-axis").addEventListener("change", updateSpreadRangeVisibility);

  $("#material-trading-enable-all").addEventListener("click", () => setAllMaterials(true));
  $("#material-trading-disable-all").addEventListener("click", () => setAllMaterials(false));
  $("#material-trading-restore").addEventListener("click", restoreMaterialDefaults);
  $("#material-trading-filter").addEventListener("input", filterMaterialRows);
}

function wirePopover() {
  popoverEl = $("#settings-popover");
  buttonEl = $("#settings-btn");
  // Focusable only programmatically (openSettings), so Escape reaches the
  // document handler below even before the user has tabbed into a control.
  popoverEl.tabIndex = -1;

  buttonEl.addEventListener("click", () => {
    if (settingsOpen()) closeSettings({ revert: true });
    else openSettings();
  });
  $("#settings-close").addEventListener("click", () => closeSettings({ revert: true }));
  $("#settings-cancel").addEventListener("click", () => closeSettings({ revert: true }));
  $("#settings-save").addEventListener("click", saveSettings);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && settingsOpen()) closeSettings({ revert: true });
  });
  // Clicking away discards, like Cancel. On pointerdown rather than click so a
  // drag that starts outside and ends inside doesn't count, and guarded on the
  // button so its own click still toggles rather than double-firing.
  document.addEventListener("pointerdown", (event) => {
    if (!settingsOpen()) return;
    if (popoverEl.contains(event.target)) return;
    if (buttonEl.contains(event.target)) return;
    closeSettings({ revert: true });
  });
}

// Build the generated panels, wire the panel, and restore the pane states --
// everything the popover needs, on either page. Call it before restoring a
// saved state, so every control the state applies to already exists.
export function initSettings({ onSave } = {}) {
  onSaveCallback = onSave || null;
  readConfigDefaults();
  buildConfigPanels();
  applyOwnParamPlaceholders();
  buildMaterialTradingPane();
  wireSettingsControls();
  wirePopover();
  restorePaneStates();
  // A brand-new session never applies a saved state, so seed the implicit
  // today and the spread-range row's initial visibility.
  setStartDateDisplay("");
  updateSpreadRangeVisibility();
  // Not awaited: the history summary is a hint beside a button, so it fills in
  // whenever IndexedDB gets round to it rather than holding up the page wiring.
  showSnapshotSummary();
}
