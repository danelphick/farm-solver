// The settings popover's staging cadence, on the solver page, driven against
// the real template (with its `{% include %}` expanded -- see
// template-fixture.js). The mastery-costs page's side of the same popover is
// covered in masteries.page.test.js: one page module per test file, since each
// import's DOMContentLoaded listener keeps firing on later loads (see below),
// and two different pages' listeners would then run against one page's markup.
//
// The one behaviour worth pinning: an edit inside the popover changes nothing
// until Save. Everywhere else in the app a control edit debounces into a solve,
// so it would be easy to reintroduce that here by wiring a stray listener --
// these tests fail if anyone does. The mirror image matters too: Save must both
// persist and re-request, and Cancel must put the panel back.

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { IDBFactory } from "fake-indexeddb";

import { pageBody } from "./template-fixture.js";

// jsdom ships no Storage implementation here; see masteries.page.test.js.
function installLocalStorage(saved) {
  const store = new Map();
  if (saved) store.set("farm-solver.v1", JSON.stringify(saved));
  vi.stubGlobal("localStorage", {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
    clear: () => store.clear(),
  });
  return store;
}

const SAVED = {
  mastery_text: "Mastery In-Progress",
  inventory_text: "Items chevron_down",
  params: {
    target: "tower",
    mastery: "Gold Bar:5000",
    max_gold: "500",
    price_cookie_set: 20,
  },
};

const SOLVE_RESPONSE = {
  cached: false, solved_ms: 1, tower_max_level: 300,
  solution_html: "", notes: [], warnings: [],
  frontier: { html: "", plans: [] },
  graph: { html: "" },
  plan: { index: 0, html: "" },
  crafts: { html: "" },
  debug_command: null,
};

let fetchMock;

// Build the page (config defaults injected as the views do), stub the API and
// storage, then import the page module and let it initialise.
//
// Like the other page tests, every earlier import's DOMContentLoaded listener
// fires again against the current DOM, so request assertions count from the
// baseline this returns rather than from zero.
async function loadPage({ saved = SAVED } = {}) {
  document.body.innerHTML =
    pageBody("index") +
    // The server lists every pets.csv pet under [pets] (webapi.config_defaults);
    // the Pets pane is built from those keys.
    '<script id="config-defaults" type="application/json">' +
    '{"pets": {"use_crunchy_omelette": true, "Cat": 8, "Miner": 0}}</script>';
  window.history.replaceState({}, "", "/");
  const store = installLocalStorage(saved);
  fetchMock = vi.fn(async (url) =>
    url === "/api/solve"
      ? { ok: true, status: 200, json: async () => SOLVE_RESPONSE }
      : { ok: true, status: 200, text: async () => "" }  // a datalist catalogue
  );
  vi.stubGlobal("fetch", fetchMock);
  // The popover's snapshot button writes to IndexedDB, which jsdom does not
  // implement; a fresh factory per load keeps each test's history to itself.
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.resetModules();
  await import("./app.js");
  document.dispatchEvent(new Event("DOMContentLoaded"));
  await vi.waitFor(() => expect(solveCalls().length).toBeGreaterThan(0));
  return { store, baseline: solveCalls().length };
}

const solveCalls = () =>
  fetchMock.mock.calls.filter(([url]) => url === "/api/solve");

const $ = (selector) => document.querySelector(selector);
const click = (selector) => $(selector).click();

function edit(selector, value) {
  const el = $(selector);
  el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

// Long enough for app.js's 400ms debounce to have fired, had anything armed it.
const settle = () => new Promise((resolve) => setTimeout(resolve, 600));

describe("settings popover", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  test("the cog opens the popover and restores the saved settings into it", async () => {
    await loadPage();
    expect($("#settings-popover").hidden).toBe(true);

    click("#settings-btn");
    expect($("#settings-popover").hidden).toBe(false);
    expect($("#settings-btn").getAttribute("aria-expanded")).toBe("true");
    // Not the markup default -- the saved state, which both pages apply.
    expect($("#max-gold").value).toBe("500");
    expect($("#mastery-text").value).toBe("Mastery In-Progress");
  });

  test("editing inside the popover neither solves nor saves", async () => {
    const { store, baseline } = await loadPage();

    click("#settings-btn");
    edit("#price-cookie-set", "99");
    edit("#current-stamina", "1234");
    await settle();

    expect(solveCalls().length).toBe(baseline);
    expect(JSON.parse(store.get("farm-solver.v1")).params.price_cookie_set).toBe(20);
  });

  test("Save persists the staged edit and re-solves once", async () => {
    const { store, baseline } = await loadPage();

    click("#settings-btn");
    edit("#price-cookie-set", "99");
    click("#settings-save");

    expect($("#settings-popover").hidden).toBe(true);
    await vi.waitFor(() => expect(solveCalls().length).toBe(baseline + 1));
    const body = JSON.parse(solveCalls().at(-1)[1].body);
    expect(body.params.price_cookie_set).toBe(99);
    // Persisted too, and the goal the popover never showed is still there.
    const stored = JSON.parse(store.get("farm-solver.v1"));
    expect(stored.params.price_cookie_set).toBe(99);
    expect(stored.params.mastery).toBe("Gold Bar:5000");
    expect(stored.params.target).toBe("tower");
  });

  test("Cancel reverts the staged edits and requests nothing", async () => {
    const { store, baseline } = await loadPage();

    click("#settings-btn");
    edit("#price-cookie-set", "99");
    edit("#mastery-text", "pasted something else");
    click("#settings-cancel");

    expect($("#settings-popover").hidden).toBe(true);
    expect($("#price-cookie-set").value).toBe("20");
    expect($("#mastery-text").value).toBe("Mastery In-Progress");
    await settle();
    expect(solveCalls().length).toBe(baseline);
    expect(JSON.parse(store.get("farm-solver.v1")).params.price_cookie_set).toBe(20);
  });

  test("Escape closes and reverts, like Cancel", async () => {
    await loadPage();

    click("#settings-btn");
    edit("#price-cookie-set", "99");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

    expect($("#settings-popover").hidden).toBe(true);
    expect($("#price-cookie-set").value).toBe("20");
  });

  test("a click outside reverts; a click inside does not", async () => {
    await loadPage();

    click("#settings-btn");
    edit("#price-cookie-set", "99");
    $("#price-cookie-set").dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true })
    );
    expect($("#settings-popover").hidden).toBe(false);
    expect($("#price-cookie-set").value).toBe("99");

    document.body.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    expect($("#settings-popover").hidden).toBe(true);
    expect($("#price-cookie-set").value).toBe("20");
  });

  // A schema section only reaches the page if buildConfigPanels names it AND the
  // template carries its .config-fields div, so a field added to one but not the
  // other silently vanishes. These pin one control per section, and that what
  // they collect really rides the generic `config` param to the server.
  test("every config schema section is generated into the panel", async () => {
    await loadPage();
    click("#settings-btn");

    [
      "exploring.effectiveness.Forest",     // effectiveness
      "farming.rows",                       // farming
      "exploring.wanderer_level",           // exploring
      "fishing.have_reinforced_netting",    // fishing
      "workshop.resource_saver",            // resources
      "trading.price_stamina",              // trading values
      "pets.Cat",                           // pets
      "pets.Miner",                         // pets (level 0 default)
      "sawmill.wood",                       // buildings
    ].forEach((key) => {
      expect($(`[data-config-key="${key}"]`)).not.toBeNull();
    });
  });

  // The contract an external tool (a scraping extension) relies on: a control
  // is findable by the config key it mirrors, whichever channel it travels
  // under. The own-param ones must NOT also ride the generic `config` object --
  // the server would then get the same setting twice, by two routes.
  test("hand-written config-backed controls are tagged but not double-sent", async () => {
    const { baseline } = await loadPage();
    click("#settings-btn");

    expect($('[data-config-key="trading.arnold_palmer"]').id).toBe("price-arnold-palmer");
    expect($('[data-config-key="farmhouse.current_stamina"]').id).toBe("current-stamina");
    expect($('[data-config-key="farming.instant_grow_time"]').id).toBe("instant-grow-time");
    expect($('[data-config-key="sawmill.hickory_omelette"]').id).toBe("hickory-omelette");

    edit('[data-config-key="trading.arnold_palmer"]', "77");
    click("#settings-save");

    await vi.waitFor(() => expect(solveCalls().length).toBe(baseline + 1));
    const body = JSON.parse(solveCalls().at(-1)[1].body);
    expect(body.params.price_arnold_palmer).toBe(77);
    expect(body.params.config?.["trading.arnold_palmer"]).toBeUndefined();
  });

  test("a blank Current stamina defers to the config; an explicit 0 is sent", async () => {
    const { baseline } = await loadPage();
    // Nothing saved for it, so the box is blank and the param stays absent --
    // that is what lets [farmhouse] current_stamina apply.
    expect($("#current-stamina").value).toBe("");
    expect(JSON.parse(solveCalls().at(-1)[1].body).params.current_stamina).toBeUndefined();

    click("#settings-btn");
    edit("#current-stamina", "0");
    click("#settings-save");

    await vi.waitFor(() => expect(solveCalls().length).toBe(baseline + 1));
    expect(JSON.parse(solveCalls().at(-1)[1].body).params.current_stamina).toBe(0);
  });

  test("a generated config control is sent under the `config` param", async () => {
    const { baseline } = await loadPage();

    click("#settings-btn");
    edit('[data-config-key="exploring.wanderer_level"]', "2");
    // A bool starts at the injected config default (false here, the fixture
    // injects `{}`), so ticking it is the deviation that gets sent.
    $('[data-config-key="fishing.use_sea_pincher"]').click();
    click("#settings-save");

    await vi.waitFor(() => expect(solveCalls().length).toBe(baseline + 1));
    const body = JSON.parse(solveCalls().at(-1)[1].body);
    expect(body.params.config["exploring.wanderer_level"]).toBe(2);
    expect(body.params.config["fishing.use_sea_pincher"]).toBe(true);
  });
});

// The snapshot button is the one control in the panel that is NOT staged: it
// records the mastery box into this browser's history the moment it is clicked.
// These tests are the guard on that exception staying deliberate.
describe("the mastery snapshot button", () => {
  const PASTE = "Tomato\n992,332 / 1,000,000 Progress\n99.2332% Stop\n";

  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  async function storedSnapshots() {
    // The same module instance the page is wired to (loadPage resetModules'd
    // before importing app.js, so this import returns that instance).
    const history = await import("./history.js");
    return history.getSnapshots();
  }

  test("records the pasted masteries, and says what it saved", async () => {
    await loadPage();
    click("#settings-btn");
    edit("#mastery-text", PASTE);
    click("#save-snapshot");

    await vi.waitFor(async () => expect(await storedSnapshots()).toHaveLength(1));
    const [snapshot] = await storedSnapshots();
    expect(snapshot.items).toEqual({ Tomato: 992332 });
    expect($("#snapshot-status").textContent).toMatch(/Last snapshot: .*1 items.*1 kept/);
  });

  test("a snapshot survives Cancel, unlike every staged edit beside it", async () => {
    await loadPage();
    click("#settings-btn");
    edit("#mastery-text", PASTE);
    click("#save-snapshot");
    await vi.waitFor(async () => expect(await storedSnapshots()).toHaveLength(1));

    click("#settings-cancel");
    expect($("#mastery-text").value).toBe("Mastery In-Progress"); // reverted
    expect(await storedSnapshots()).toHaveLength(1); // but the history is kept
  });

  test("reports an unreadable paste instead of storing it", async () => {
    await loadPage();
    click("#settings-btn");
    edit("#mastery-text", "Tomato\nnot a progress line\n");
    click("#save-snapshot");

    await vi.waitFor(() =>
      expect($("#snapshot-status").textContent).toMatch(/Could not read the paste/)
    );
    expect(await storedSnapshots()).toEqual([]);
  });

  test("does not solve or persist the solver state", async () => {
    const { store, baseline } = await loadPage();
    click("#settings-btn");
    edit("#mastery-text", PASTE);
    click("#save-snapshot");
    await settle();

    expect(solveCalls().length).toBe(baseline);
    expect(JSON.parse(store.get("farm-solver.v1")).mastery_text).toBe(
      "Mastery In-Progress"
    );
  });
});

// Backup/restore, the other pair of unstaged controls: they move a whole save
// between browsers, so what a backup CONTAINS and what a restore refuses both
// matter more than the buttons themselves. The file format's own edge cases are
// backup.test.js's; these pin the wiring -- that a backup carries the live
// panel plus the goal it cannot see, and that a restore writes storage and
// reloads only once the user has agreed.
describe("backup and restore", () => {
  const PASTE = "Tomato\n992,332 / 1,000,000 Progress\n99.2332% Stop\n";

  let downloads;
  let reload;
  let locationDescriptor;

  beforeEach(() => {
    vi.unstubAllGlobals();
    downloads = [];
    // jsdom implements neither object URLs nor navigation: capture the blob the
    // download anchor would have fetched, and swallow its click.
    URL.createObjectURL = vi.fn((blob) => {
      const url = `blob:${downloads.length}`;
      downloads.push({ url, blob });
      return url;
    });
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    locationDescriptor = Object.getOwnPropertyDescriptor(window, "location");
    reload = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { href: "http://localhost/", pathname: "/", search: "", reload },
    });
  });

  afterEach(() => {
    delete URL.createObjectURL;
    delete URL.revokeObjectURL;
    vi.restoreAllMocks();
    if (locationDescriptor) Object.defineProperty(window, "location", locationDescriptor);
  });

  // What the (mocked) download actually received. Read with a FileReader for
  // the same reason backup.js does: jsdom's Blob has no text() promise.
  async function downloadedBackup() {
    expect(downloads).toHaveLength(1);
    const text = await new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(downloads[0].blob);
    });
    return JSON.parse(text);
  }

  // Drive the hidden file input the way the file dialog would.
  function pickFile(contents) {
    const input = $("#backup-file");
    const file = new File([contents], "backup.json", { type: "application/json" });
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  test("backs up the live panel, the goal it cannot see, and the history", async () => {
    await loadPage();
    click("#settings-btn");
    edit("#mastery-text", PASTE);
    click("#save-snapshot");
    await vi.waitFor(() => expect($("#snapshot-status").textContent).toMatch(/1 kept/));

    click("#backup-save");
    await vi.waitFor(async () => expect(downloads).toHaveLength(1));

    const backup = await downloadedBackup();
    expect(backup.format).toBe("farm-solver-backup");
    // The paste as it stands in the box, not the (older) saved copy...
    expect(backup.state.mastery_text).toBe(PASTE);
    // ...alongside the goal, which the panel does not show and must not lose.
    expect(backup.state.params.mastery).toBe("Gold Bar:5000");
    expect(backup.state.params.max_gold).toBe("500");
    expect(backup.snapshots).toHaveLength(1);
    expect(backup.snapshots[0].items).toEqual({ Tomato: 992332 });
    expect($("#backup-status").textContent).toMatch(/Backed up 1 snapshot and every setting/);
  });

  test("restoring writes the file's state and preferences, then reloads", async () => {
    const { store } = await loadPage();
    vi.stubGlobal("confirm", vi.fn(() => true));

    const file = JSON.stringify({
      format: "farm-solver-backup",
      version: 1,
      exported_at: "2026-07-31T09:00:00.000Z",
      state: { mastery_text: "Restored", inventory_text: "", params: { mastery: "Tomato:MM" } },
      ui: { panes: { "paste-details": true } },
      snapshots: [
        { taken_at: "2026-07-28T20:03:11.482Z", date: "2026-07-28", items: { Tomato: 992332 } },
      ],
    });
    click("#settings-btn");
    pickFile(file);

    await vi.waitFor(() => expect(reload).toHaveBeenCalled());
    expect(JSON.parse(store.get("farm-solver.v1"))).toEqual({
      mastery_text: "Restored",
      inventory_text: "",
      params: { mastery: "Tomato:MM" },
    });
    expect(JSON.parse(store.get("farm-solver.ui.v1"))).toEqual({
      panes: { "paste-details": true },
    });
    const history = await import("./history.js");
    expect(await history.getSnapshots()).toHaveLength(1);
  });

  test("a file that is not a backup is refused, and nothing is written", async () => {
    const { store } = await loadPage();
    vi.stubGlobal("confirm", vi.fn(() => true));

    click("#settings-btn");
    pickFile("<html>not a backup</html>");

    await vi.waitFor(() =>
      expect($("#backup-status").textContent).toMatch(/Could not restore: that file is not JSON/)
    );
    expect(window.confirm).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
    expect(JSON.parse(store.get("farm-solver.v1")).mastery_text).toBe("Mastery In-Progress");
  });

  test("declining the confirmation leaves the browser as it was", async () => {
    const { store } = await loadPage();
    vi.stubGlobal("confirm", vi.fn(() => false));

    click("#settings-btn");
    pickFile(
      JSON.stringify({
        format: "farm-solver-backup",
        version: 1,
        state: { mastery_text: "Restored" },
      })
    );

    await vi.waitFor(() =>
      expect($("#backup-status").textContent).toMatch(/Restore cancelled/)
    );
    expect(reload).not.toHaveBeenCalled();
    expect(JSON.parse(store.get("farm-solver.v1")).mastery_text).toBe("Mastery In-Progress");
  });
});
