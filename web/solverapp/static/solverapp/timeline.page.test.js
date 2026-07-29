// Page-level tests for the timeline client, driven against the REAL template
// (timeline.html with its Django tags stripped) -- so an id renamed in one file
// and not the other fails here rather than silently in a browser.
//
// Unlike the other two pages there is no API to stub: everything the page shows
// comes from the snapshots in IndexedDB, which fake-indexeddb provides (jsdom
// ships none). Each test seeds the store, then imports the module and fires
// DOMContentLoaded, which timeline.js listens for at module scope.

import { beforeEach, describe, expect, test, vi } from "vitest";
import { IDBFactory } from "fake-indexeddb";

import { pageBody } from "./template-fixture.js";

// The page markup plus the config the view injects (webapi.config_defaults),
// which the shared popover's generated panels are built from; empty is enough.
function buildPage() {
  return (
    pageBody("timeline") +
    '<script id="config-defaults" type="application/json">{}</script>'
  );
}

// jsdom ships no Storage here, and the settings popover treats missing storage
// as "nothing saved" -- without a stub every test would log its way through the
// try/catch instead of exercising the page.
function installLocalStorage() {
  const store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
    clear: () => store.clear(),
  });
}

const SNAPSHOTS = [
  { when: new Date(2026, 6, 1, 12, 0), items: { Tomato: 400_000, Hops: 900 } },
  { when: new Date(2026, 6, 8, 12, 0), items: { Tomato: 404_800 } },
  { when: new Date(2026, 6, 15, 12, 0), items: { Tomato: 412_900, Hops: 1_200 } },
];

// Seed the store, build the page at `url`, import the module and initialise it.
// The seeding goes through history.js itself (after resetModules, so it is the
// same module instance timeline.js will import) rather than poking at the raw
// database -- that keeps the fixture honest about the shape actually stored.
async function loadPage({ url = "/timeline", snapshots = SNAPSHOTS, ui } = {}) {
  vi.stubGlobal("indexedDB", new IDBFactory());
  installLocalStorage();
  // The per-table sort each page load restores from (settings.readUi).
  if (ui) localStorage.setItem("farm-solver.ui.v1", JSON.stringify(ui));
  // The item <datalist> is fetched; it is best-effort in the page, but stub it
  // so a test never depends on jsdom's networking.
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, text: async () => "" })));
  vi.resetModules();

  const history = await import("./history.js");
  for (const snapshot of snapshots) {
    await history.putSnapshot(snapshot.items, snapshot.when);
  }

  window.history.replaceState(null, "", url);
  document.body.innerHTML = buildPage();
  await import("./timeline.js");
  document.dispatchEvent(new Event("DOMContentLoaded"));
  // Loading the snapshots is async, so wait for the status line to settle.
  await vi.waitFor(() =>
    expect(document.querySelector("#status").textContent).not.toBe("Loading…")
  );
}

const rowText = (selector) =>
  [...document.querySelectorAll(selector)].map((tr) =>
    [...tr.children].map((td) => td.textContent.trim())
  );

describe("the timeline page", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  test("with no ?item= it lists everything with history", async () => {
    await loadPage();
    expect(document.querySelector("#timeline-index").hidden).toBe(false);
    expect(document.querySelector("#timeline-chart").hidden).toBe(true);
    expect(document.querySelector("#timeline-section").hidden).toBe(true);

    const rows = rowText("#timeline-index-body tr");
    expect(rows.map((cells) => cells[0])).toEqual(["Hops", "Tomato"]);
    // Item, tier, latest, change over the whole history.
    expect(rows[1]).toEqual(["Tomato", "GM", "412,900", "+12,900"]);
  });

  test("an index row links to that item's own timeline", async () => {
    await loadPage();
    const row = document.querySelector("#timeline-index-body tr");
    expect(row.dataset.href).toBe("/timeline?item=Hops");
  });

  test("?item= charts that item and tabulates its gains", async () => {
    await loadPage({ url: "/timeline?item=Tomato" });
    expect(document.querySelector("#timeline-index").hidden).toBe(true);
    expect(document.querySelector("#timeline-chart").hidden).toBe(false);
    expect(document.querySelectorAll("#timeline-chart circle.pt")).toHaveLength(3);

    expect(rowText("#timeline-body tr")).toEqual([
      ["2026-07-15", "412,900", "+8,100", "+1,157", "GM (Grandmaster)"],
      ["2026-07-08", "404,800", "+4,800", "+686", "GM (Grandmaster)"],
      ["2026-07-01", "400,000", "—", "—", "GM (Grandmaster)"],
    ]);
    expect(document.querySelector("#timeline-item").value).toBe("Tomato");
  });

  // A snapshot that doesn't mention the item is a gap, not a zero: Hops is
  // absent from the middle snapshot, so it has two points rather than a dip.
  test("skips snapshots that do not mention the item", async () => {
    await loadPage({ url: "/timeline?item=Hops" });
    expect(document.querySelectorAll("#timeline-chart circle.pt")).toHaveLength(2);
    expect(rowText("#timeline-body tr").map((cells) => cells[0])).toEqual([
      "2026-07-15",
      "2026-07-01",
    ]);
  });

  test("matches the item case-insensitively", async () => {
    await loadPage({ url: "/timeline?item=tomato" });
    expect(document.querySelectorAll("#timeline-body tr")).toHaveLength(3);
  });

  test("says so when no snapshot mentions the item", async () => {
    await loadPage({ url: "/timeline?item=Nonesuch" });
    expect(document.querySelector("#status").textContent).toMatch(/No snapshot mentions/);
    expect(document.querySelector("#timeline-chart").hidden).toBe(true);
    expect(document.querySelector("#timeline-section").hidden).toBe(true);
  });

  test("points at the settings popover when there is no history at all", async () => {
    await loadPage({ snapshots: [] });
    expect(document.querySelector("#timeline-empty").hidden).toBe(false);
    expect(document.querySelector("#timeline-index").hidden).toBe(true);
    expect(document.querySelector("#status").textContent).toMatch(/No snapshots saved yet/);
  });

  // Typing an item rewrites the query string in place, so the URL stays the
  // shareable "/timeline?item=X" the mastery-costs page links to.
  // Real timers rather than vi.useFakeTimers(): fake-indexeddb drives its own
  // queue off them, so faking them deadlocks the store this page reads from.
  // The input is debounced by 400ms, which waitFor simply outlasts.
  test("typing an item updates the URL and redraws", async () => {
    await loadPage();
    const input = document.querySelector("#timeline-item");
    input.value = "Tomato";
    input.dispatchEvent(new Event("input"));

    await vi.waitFor(() => expect(window.location.search).toBe("?item=Tomato"));
    expect(document.querySelector("#timeline-section").hidden).toBe(false);
  });

  const clickSort = (head, key) =>
    document
      .querySelector(`#${head} button[data-sort="${key}"]`)
      .dispatchEvent(new Event("click", { bubbles: true }));

  test("a column's sort button reorders the index", async () => {
    await loadPage();
    expect(rowText("#timeline-index-body tr").map((cells) => cells[0])).toEqual([
      "Hops",
      "Tomato",
    ]);

    clickSort("timeline-index-head", "latest");
    expect(rowText("#timeline-index-body tr").map((cells) => cells[0])).toEqual([
      "Hops",
      "Tomato",
    ]);
    // Clicking the active column reverses it.
    clickSort("timeline-index-head", "latest");
    expect(rowText("#timeline-index-body tr").map((cells) => cells[0])).toEqual([
      "Tomato",
      "Hops",
    ]);
  });

  test("a column's sort button reorders the delta table", async () => {
    await loadPage({ url: "/timeline?item=Tomato" });
    clickSort("timeline-head", "gain");

    // Ascending, with the oldest row's absent gain ("—") kept at the bottom.
    expect(rowText("#timeline-body tr").map((cells) => [cells[0], cells[2]])).toEqual([
      ["2026-07-08", "+4,800"],
      ["2026-07-15", "+8,100"],
      ["2026-07-01", "—"],
    ]);
  });

  test("the active column shows its direction and is remembered", async () => {
    await loadPage();
    clickSort("timeline-index-head", "change");

    // Item, Tier, Latest, Change -- the sorted one being the last of them.
    const headers = [...document.querySelectorAll("#timeline-index-head th")];
    expect(headers.map((th) => th.getAttribute("aria-sort"))).toEqual([
      "none",
      "none",
      "none",
      "ascending",
    ]);
    expect(
      document.querySelector('#timeline-index-head button[data-sort="change"] .sort-arrow')
        .textContent
    ).toBe("▲");
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      timeline_index_sort: "change",
      timeline_index_sort_dir: "asc",
    });
  });

  test("a saved sort is restored on the next visit", async () => {
    await loadPage({
      url: "/timeline?item=Tomato",
      ui: { timeline_sort: "progress", timeline_sort_dir: "asc" },
    });

    expect(rowText("#timeline-body tr").map((cells) => cells[0])).toEqual([
      "2026-07-01",
      "2026-07-08",
      "2026-07-15",
    ]);
  });

  test("carries the shared settings popover", async () => {
    await loadPage();
    expect(document.querySelector("#settings-popover")).not.toBeNull();
    expect(document.querySelector("#save-snapshot")).not.toBeNull();
  });
});
