// Page-level tests for the mastery-costs client, driven against the REAL
// template markup (mastery_costs.html with its Django tags stripped) rather
// than a hand-written fixture -- so an id renamed in one file and not the other
// fails here instead of silently in a browser.
//
// masteries.js registers its DOMContentLoaded -> init() listener at module
// scope, so each test builds the DOM and its localStorage/fetch stubs first,
// then dynamically imports the module (resetModules between tests) and fires
// the event.

import { beforeEach, describe, expect, test, vi } from "vitest";

// The real template, settings include expanded -- see template-fixture.js.
import { pageBody } from "./template-fixture.js";

// The page markup plus the config the view injects (webapi.config_defaults),
// which the popover's generated preference panels and material-trading rows are
// built from; an empty object is enough for everything these tests assert.
function buildPage() {
  return (
    pageBody("mastery_costs") +
    '<script id="config-defaults" type="application/json">{}</script>'
  );
}

const ROWS = [
  { item: "Slow", tier: "MM", time: "5d", time_seconds: 432000, gold: 10 },
  { item: "Fast Dear", tier: "GM", time: "1d", time_seconds: 86400, gold: 900 },
  { item: "Fast Cheap", tier: "M", time: "1d", time_seconds: 86400, gold: 100 },
];

const PAYLOAD = {
  cached: false,
  solved_ms: 12,
  rows: ROWS,
  unmasterable: [
    { item: "Awl", text: "Awl needs Flarite (which needs Unpolished Flarite (unobtainable))" },
    { item: "Cicada", text: "Cicada (seasonal)" },
  ],
  limited: [
    {
      item: "Bouquet of Flowers",
      text: "Bouquet of Flowers cannot reach Megamaster: its sources cannot supply that many",
    },
  ],
  items: {
    Slow: { categories: ["Fishing"], event: false, status: "obtainable" },
    "Fast Dear": { categories: ["Exploring", "Crafting"], event: false, status: "obtainable" },
    "Fast Cheap": { categories: ["Farming"], event: true, status: "obtainable" },
    Awl: { categories: ["Crafting"], event: false, status: "materials-unobtainable" },
    Cicada: { categories: ["Other"], event: true, status: "unobtainable" },
    "Bouquet of Flowers": { categories: ["Crafting"], event: false, status: "obtainable" },
  },
};

let fetchMock;

// jsdom ships no Storage implementation here (vitest logs "localStorage is not
// available"), and masteries.js treats missing storage as "nothing saved" -- so
// without a stub every test would silently exercise the empty-state path.
function installLocalStorage() {
  const store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
    clear: () => store.clear(),
  });
}

function setSavedState(state) {
  localStorage.clear();
  if (state) localStorage.setItem("farm-solver.v1", JSON.stringify(state));
}

// Build the page, stub the API, import the module and let it initialise.
//
// Returns how many requests the initial load made, which the "did this action
// request again?" assertions count from. (One: masteries.js registers its
// DOMContentLoaded listener `{ once: true }`, so an earlier test's import
// cannot initialise this page a second time.)
async function loadPage({ state, payload = PAYLOAD, ui } = {}) {
  document.body.innerHTML = buildPage();
  setSavedState(state);
  // After setSavedState, which clears storage -- the UI key is a separate one.
  if (ui) localStorage.setItem("farm-solver.ui.v1", JSON.stringify(ui));
  fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => payload,
  }));
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
  await import("./masteries.js");
  document.dispatchEvent(new Event("DOMContentLoaded"));
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await vi.waitFor(() =>
    expect(document.querySelector("#mastery-costs-body").children.length)
      .toBeGreaterThan(0)
  );
  return fetchMock.mock.calls.length;
}

// A row's cells as plain text. Only each cell's own text nodes, so the Item
// cell reads as the item name alone -- the "timeline" link it also carries is
// asserted on separately, and isn't part of any of these tables' values.
const cellText = (td) =>
  [...td.childNodes]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent)
    .join("")
    .trim();

const rowText = () =>
  [...document.querySelectorAll("#mastery-costs-body tr")].map((tr) =>
    [...tr.children].map(cellText)
  );

const SAVED = {
  mastery_text: "Mastery In-Progress",
  inventory_text: "Items chevron_down",
  params: { calendar: "ignore", target: "tower", mastery: "Gold Bar:5000" },
};

describe("mastery-costs page", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    installLocalStorage();
  });

  test("posts the saved state and renders the table", async () => {
    await loadPage({ state: SAVED });

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/mastery-costs");
    const body = JSON.parse(options.body);
    expect(body.mastery_text).toBe(SAVED.mastery_text);
    expect(body.inventory_text).toBe(SAVED.inventory_text);
    // The solver page's own params ride along, plus this page's one knob.
    expect(body.params).toEqual({
      calendar: "ignore",
      target: "tower",
      mastery: "Gold Bar:5000",
      self_sourced: false,
    });

    expect(rowText()).toEqual([
      ["Fast Cheap", "M", "1d", "100.0 g"],
      ["Fast Dear", "GM", "1d", "900.0 g"],
      ["Slow", "MM", "5d", "10.0 g"],
    ]);
    expect(document.querySelector("#mastery-costs-section").hidden).toBe(false);
    expect(document.querySelector("#empty-state").hidden).toBe(true);
  });

  test("each row links to the solver page seeded with its goal", async () => {
    await loadPage({ state: SAVED });
    const hrefs = [...document.querySelectorAll("#mastery-costs-body tr")].map(
      (tr) => tr.dataset.href
    );
    expect(hrefs).toEqual([
      "/?mastery=Fast%20Cheap%3AM",
      "/?mastery=Fast%20Dear%3AGM",
      "/?mastery=Slow%3AMM",
    ]);
  });

  test("clicking a row navigates to that goal", async () => {
    await loadPage({ state: SAVED });
    const assign = vi.fn();
    // jsdom refuses a real navigation; intercept the assignment instead.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { set href(value) { assign(value); } },
    });
    document
      .querySelectorAll("#mastery-costs-body tr")[1]
      .querySelector("td")
      .dispatchEvent(new Event("click", { bubbles: true }));
    expect(assign).toHaveBeenCalledWith("/?mastery=Fast%20Dear%3AGM");
  });

  test("each row's Item cell links to that item's mastery timeline", async () => {
    await loadPage({ state: SAVED });
    const links = [...document.querySelectorAll("#mastery-costs-body .timeline-link")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/timeline?item=Fast%20Cheap",
      "/timeline?item=Fast%20Dear",
      "/timeline?item=Slow",
    ]);
  });

  // The row's own click handler must leave the anchor alone, or the timeline
  // link would navigate to the row's plan instead.
  test("clicking the timeline link does not hijack the row's navigation", async () => {
    await loadPage({ state: SAVED });
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { set href(value) { assign(value); } },
    });
    document
      .querySelectorAll("#mastery-costs-body .timeline-link")[1]
      .dispatchEvent(new Event("click", { bubbles: true }));
    expect(assign).not.toHaveBeenCalled();
  });

  const clickSort = (key) =>
    document
      .querySelector(`#mastery-costs-head button[data-sort="${key}"]`)
      .dispatchEvent(new Event("click", { bubbles: true }));

  test("a column's sort button reorders without re-requesting", async () => {
    const requests = await loadPage({ state: SAVED });
    clickSort("gold");

    expect(rowText().map((cells) => cells[0])).toEqual([
      "Slow",        // 10 g
      "Fast Cheap",  // 100 g
      "Fast Dear",   // 900 g
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(requests);  // sorting is display-only
  });

  test("clicking the active column again reverses it", async () => {
    await loadPage({ state: SAVED });
    clickSort("gold");
    clickSort("gold");

    expect(rowText().map((cells) => cells[0])).toEqual([
      "Fast Dear",
      "Fast Cheap",
      "Slow",
    ]);
    // A different column starts ascending again rather than staying reversed.
    clickSort("item");
    expect(rowText().map((cells) => cells[0])).toEqual([
      "Fast Cheap",
      "Fast Dear",
      "Slow",
    ]);
  });

  test("Target sorts M before GM before MM, not alphabetically", async () => {
    await loadPage({ state: SAVED });
    clickSort("tier");
    expect(rowText().map((cells) => cells[1])).toEqual(["M", "GM", "MM"]);
    clickSort("tier");
    expect(rowText().map((cells) => cells[1])).toEqual(["MM", "GM", "M"]);
  });

  test("the active column shows its direction and is remembered", async () => {
    await loadPage({ state: SAVED });
    clickSort("item");

    const headers = [...document.querySelectorAll("#mastery-costs-head th")];
    expect(headers.map((th) => th.getAttribute("aria-sort"))).toEqual([
      "ascending",
      "none",
      "none",
      "none",
    ]);
    expect(
      document.querySelector('button[data-sort="item"] .sort-arrow').textContent
    ).toBe("▲");

    clickSort("item");
    expect(headers[0].getAttribute("aria-sort")).toBe("descending");
    expect(
      document.querySelector('button[data-sort="item"] .sort-arrow').textContent
    ).toBe("▼");
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      mastery_sort: "item",
      mastery_sort_dir: "desc",
    });
  });

  test("a saved sort is restored on the next visit", async () => {
    await loadPage({
      state: SAVED,
      ui: { mastery_sort: "gold", mastery_sort_dir: "desc" },
    });
    expect(rowText().map((cells) => cells[0])).toEqual([
      "Fast Dear",
      "Fast Cheap",
      "Slow",
    ]);
  });

  test("the self-sourced checkbox re-requests with the flag set", async () => {
    const requests = await loadPage({ state: SAVED });
    const box = document.querySelector("#self-sourced");
    box.checked = true;
    box.dispatchEvent(new Event("change"));
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.length).toBeGreaterThan(requests)
    );

    const body = JSON.parse(fetchMock.mock.calls.at(-1)[1].body);
    expect(body.params.self_sourced).toBe(true);
    // ... and the choice is remembered for the next visit.
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      mastery_self_sourced: true,
    });
  });

  test("both 'cannot' sections populate and unhide", async () => {
    await loadPage({ state: SAVED });
    expect(document.querySelector("#unmasterable-details").hidden).toBe(false);
    expect(document.querySelector("#unmasterable-summary").textContent).toBe(" (2)");
    expect(document.querySelector("#unmasterable-list").textContent).toContain(
      "Unpolished Flarite (unobtainable)"
    );
    expect(document.querySelector("#limited-details").hidden).toBe(false);
    expect(document.querySelector("#limited-list").textContent).toContain(
      "cannot reach Megamaster"
    );
  });

  test("an empty 'cannot' list leaves its section hidden", async () => {
    await loadPage({
      state: SAVED,
      payload: { ...PAYLOAD, limited: [] },
    });
    expect(document.querySelector("#unmasterable-details").hidden).toBe(false);
    expect(document.querySelector("#limited-details").hidden).toBe(true);
  });

  test("nothing pasted yet shows the empty state and never requests", async () => {
    document.body.innerHTML = buildPage();
    setSavedState(null);
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.resetModules();
    await import("./masteries.js");
    document.dispatchEvent(new Event("DOMContentLoaded"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.querySelector("#empty-state").hidden).toBe(false);
    expect(document.querySelector("#mastery-controls").hidden).toBe(true);
    expect(document.querySelector("#status").textContent).toBe("Nothing pasted yet.");
  });
});

// This page's side of the shared settings popover (settings.page.test.js covers
// the staging cadence itself, on the solver page). What is specific here: a save
// has to WRITE the state both pages read -- keeping the solver page's goal,
// which the popover never shows -- and then re-request off it.
describe("mastery-costs settings popover", () => {
  test("saving the popover keeps the solver page's goal and re-requests", async () => {
    const requests = await loadPage({ state: SAVED });

    document.querySelector("#settings-btn").click();
    const stamina = document.querySelector("#current-stamina");
    stamina.value = "4321";
    stamina.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#settings-save").click();

    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.length).toBe(requests + 1)
    );
    const body = JSON.parse(fetchMock.mock.calls.at(-1)[1].body);
    expect(body.params.current_stamina).toBe(4321);

    const stored = JSON.parse(localStorage.getItem("farm-solver.v1"));
    expect(stored.params.current_stamina).toBe(4321);
    expect(stored.params.target).toBe("tower");
    expect(stored.params.mastery).toBe("Gold Bar:5000");
  });

  test("a paste made here is enough to load the table", async () => {
    // Nothing saved at all: the empty state, until the popover supplies the
    // masteries/inventory this page used to have to borrow from the solver page.
    document.body.innerHTML = buildPage();
    setSavedState(null);
    fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => PAYLOAD,
    }));
    vi.stubGlobal("fetch", fetchMock);
    vi.resetModules();
    await import("./masteries.js");
    document.dispatchEvent(new Event("DOMContentLoaded"));
    expect(document.querySelector("#empty-state").hidden).toBe(false);

    document.querySelector("#settings-btn").click();
    const mastery = document.querySelector("#mastery-text");
    mastery.value = "Mastery In-Progress";
    mastery.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#settings-save").click();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls.at(-1)[1].body).mastery_text).toBe(
      "Mastery In-Progress"
    );
    await vi.waitFor(() =>
      expect(document.querySelector("#empty-state").hidden).toBe(true)
    );
    expect(document.querySelector("#mastery-controls").hidden).toBe(false);
  });
});

describe("mastery-costs filters", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    installLocalStorage();
  });

  const items = () => rowText().map((row) => row[0]);
  const notes = (prefix) =>
    [...document.querySelectorAll(`#${prefix}-list li`)].map((li) => li.textContent);
  const box = (value) => document.querySelector(`input[type="checkbox"][value="${value}"]`);
  const toggle = (value) => {
    box(value).checked = !box(value).checked;
    box(value).dispatchEvent(new Event("change"));
  };

  test("everything shows until a filter is set, without another request", async () => {
    const requests = await loadPage({ state: SAVED });
    expect(document.querySelector("#mastery-filter-details").hidden).toBe(false);
    expect(items()).toEqual(["Fast Cheap", "Fast Dear", "Slow"]);
    expect(document.querySelector("#mastery-filter-summary").textContent).toBe("");

    toggle("Fishing");
    expect(items()).toEqual(["Fast Cheap", "Fast Dear"]);
    expect(document.querySelector("#mastery-filter-summary").textContent).toBe(
      " (2 of 3 rows)"
    );
    expect(fetchMock.mock.calls.length).toBe(requests);
  });

  test("the name filter matches anywhere in the name, ignoring case", async () => {
    await loadPage({ state: SAVED });
    const name = document.querySelector("#filter-name");
    name.value = "EAR";
    name.dispatchEvent(new Event("input"));
    expect(items()).toEqual(["Fast Dear"]);
    // The "cannot" lists filter too, and hide when emptied.
    expect(document.querySelector("#unmasterable-details").hidden).toBe(true);
    expect(document.querySelector("#limited-details").hidden).toBe(true);

    name.value = "wl";
    name.dispatchEvent(new Event("input"));
    expect(document.querySelector("#mastery-costs-section").hidden).toBe(true);
    expect(notes("unmasterable")).toEqual([
      "Awl needs Flarite (which needs Unpolished Flarite (unobtainable))",
    ]);
    expect(document.querySelector("#unmasterable-summary").textContent).toBe(" (1 of 2)");
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      mastery_filter_name: "wl",
    });
  });

  test("the event button hides and shows event items everywhere", async () => {
    await loadPage({ state: SAVED });
    const button = document.querySelector("#filter-events");
    expect(button.textContent).toBe("Hide event items");

    button.click();
    expect(button.textContent).toBe("Show event items");
    expect(items()).toEqual(["Fast Dear", "Slow"]);
    expect(notes("unmasterable")).toEqual([
      "Awl needs Flarite (which needs Unpolished Flarite (unobtainable))",
    ]);

    button.click();
    expect(items()).toEqual(["Fast Cheap", "Fast Dear", "Slow"]);
  });

  test("an item with several types shows while any of them is ticked", async () => {
    await loadPage({ state: SAVED });
    toggle("Exploring");
    expect(items()).toContain("Fast Dear"); // still Crafting
    toggle("Crafting");
    expect(items()).not.toContain("Fast Dear");
    // Awl and Bouquet of Flowers are Crafting too.
    expect(notes("unmasterable")).toEqual(["Cicada (seasonal)"]);
    expect(document.querySelector("#limited-details").hidden).toBe(true);
  });

  test("each obtainability class has its own box", async () => {
    await loadPage({ state: SAVED });
    toggle("materials-unobtainable");
    expect(notes("unmasterable")).toEqual(["Cicada (seasonal)"]);
    toggle("unobtainable");
    expect(document.querySelector("#unmasterable-details").hidden).toBe(true);
    expect(items()).toEqual(["Fast Cheap", "Fast Dear", "Slow"]);

    toggle("obtainable");
    expect(document.querySelector("#mastery-costs-section").hidden).toBe(true);
    expect(document.querySelector("#limited-details").hidden).toBe(true);
    expect(document.querySelector("#filter-empty").hidden).toBe(false);
    // Stored as what is unticked, so a box added later starts ticked.
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      mastery_hidden_statuses: ["obtainable", "materials-unobtainable", "unobtainable"],
    });
  });

  test("the filters are restored on the next visit", async () => {
    await loadPage({
      state: SAVED,
      ui: {
        mastery_filter_name: "fast",
        mastery_hide_events: true,
        mastery_hidden_categories: ["Farming"],
      },
    });
    await vi.waitFor(() => expect(items()).toEqual(["Fast Dear"]));
    expect(document.querySelector("#filter-name").value).toBe("fast");
    expect(document.querySelector("#filter-events").textContent).toBe("Show event items");
    expect(box("Farming").checked).toBe(false);
    expect(box("Fishing").checked).toBe(true);
  });
});
