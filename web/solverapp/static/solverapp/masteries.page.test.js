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

// Vite's `?raw` rather than fs: it resolves relative to this file at transform
// time, so the test doesn't depend on the working directory (which the IDE's
// Vitest runner and `npm test` disagree about -- see vitest.config.js).
import TEMPLATE from "../../templates/solverapp/mastery_costs.html?raw";

// The template's <body> contents, with `{% ... %}` tags stripped (the only
// Django syntax it uses; the two <script>/<link> tags they sit in are dropped
// along with the rest of <head>).
function templateBody() {
  const html = TEMPLATE.replace(/\{%.*?%\}/g, "");
  return html.slice(html.indexOf("<body>") + 6, html.indexOf("</body>"));
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
  unmasterable: ["Awl needs Flarite (which needs Unpolished Flarite (unobtainable))"],
  limited: ["Bouquet of Flowers cannot reach Megamaster: its sources cannot supply that many"],
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
// Returns how many requests the initial load made. Normally one -- but each
// test re-imports the module (resetModules) and `document` is shared across the
// whole file, so every earlier import's DOMContentLoaded listener is still
// attached and fires again against the current DOM. Harmless (they all render
// the same payload into the same nodes), but it means a "did this action
// request again?" assertion has to count from this baseline rather than from 0.
async function loadPage({ state, payload = PAYLOAD, ui } = {}) {
  document.body.innerHTML = templateBody();
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

const rowText = () =>
  [...document.querySelectorAll("#mastery-costs-body tr")].map((tr) =>
    [...tr.children].map((td) => td.textContent)
  );

const SAVED = {
  mastery_text: "Mastery In-Progress",
  inventory_text: "Items chevron_down",
  params: { calendar: "ignore", target: "tower" },
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
      self_sourced: false,
    });

    expect(rowText()).toEqual([
      ["Fast Cheap", "M", "1d", "100 g"],
      ["Fast Dear", "GM", "1d", "900 g"],
      ["Slow", "MM", "5d", "10 g"],
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
    expect(document.querySelector("#unmasterable-summary").textContent).toBe(" (1)");
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
    document.body.innerHTML = templateBody();
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
