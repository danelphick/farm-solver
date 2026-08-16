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

// The clock the page reads its Change windows back from, a few hours after the
// newest fixture snapshot. Pinned because those columns are relative to *now*:
// left on the real clock, every windowed assertion would rot into "—" the week
// after it was written.
const NOW = new Date(2026, 6, 16, 6, 0);

// Seed the store, build the page at `url`, import the module and initialise it.
// The seeding goes through history.js itself (after resetModules, so it is the
// same module instance timeline.js will import) rather than poking at the raw
// database -- that keeps the fixture honest about the shape actually stored.
// What GET /api/catalogue serves (webapi.crafting_catalogue's shape): the
// inverted recipe graph, display names, and each zone's drops.
const CATALOGUE = {
  uses: {
    straw: ["twine"],
    twine: ["rope"],
    rope: ["fishing net"],
    wood: ["board"],
  },
  names: {
    straw: "Straw",
    twine: "Twine",
    rope: "Rope",
    wood: "Wood",
    "fishing net": "Fishing Net",
    board: "Board",
    "wooden mask": "Wooden Mask",
  },
  zones: [
    // Wooden Mask is a drop that no recipe uses -- adding this zone must not
    // put a chip there that could never match anything.
    { kind: "exploring", name: "Small Cave", items: ["wood", "wooden mask"] },
    { kind: "fishing", name: "Small Pond", items: ["straw"] },
  ],
};

async function loadPage({
  url = "/timeline",
  snapshots = SNAPSHOTS,
  ui,
  now = NOW,
  catalogue = CATALOGUE,
} = {}) {
  // Only Date is faked: faking the timers themselves deadlocks
  // fake-indexeddb's queue (and the debounce test below needs real ones).
  vi.useFakeTimers({ toFake: ["Date"], now });
  vi.stubGlobal("indexedDB", new IDBFactory());
  installLocalStorage();
  // The per-table sort each page load restores from (settings.readUi).
  if (ui) localStorage.setItem("farm-solver.ui.v1", JSON.stringify(ui));
  // Two things are fetched: the item <datalist> (a plain text catalogue) and
  // the recipe graph the "made from" filter walks. Both are best-effort in the
  // page, but stub them so no test depends on jsdom's networking.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) =>
      url === "/api/catalogue"
        ? { ok: true, json: async () => catalogue }
        : { ok: true, text: async () => "" }
    )
  );
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

// A row's cells, everything in them (the delta table's Tier cell spells the
// tier out in a nested span, which is part of what it says).
const rowText = (selector) =>
  [...document.querySelectorAll(selector)].map((tr) =>
    [...tr.children].map((td) => td.textContent.trim())
  );

// The index's cells, each stripped to its OWN text: an Item cell also carries
// the next-goal link ("→ GM"), which is asserted on separately rather than
// being part of every row expectation.
const cellText = (td) =>
  [...td.childNodes]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent)
    .join("")
    .trim();

const indexRowText = () =>
  [...document.querySelectorAll("#timeline-index-body tr")].map((tr) =>
    [...tr.children].map(cellText)
  );

describe("the timeline page", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  test("with no ?item= it lists everything with history", async () => {
    await loadPage();
    expect(document.querySelector("#timeline-index").hidden).toBe(false);
    expect(document.querySelector("#timeline-chart").hidden).toBe(true);
    expect(document.querySelector("#timeline-section").hidden).toBe(true);

    const rows = indexRowText();
    expect(rows.map((cells) => cells[0])).toEqual(["Hops", "Tomato"]);
    // Item, tier, latest, then the two windowed changes -- both measured from
    // the 8 July reading, the newest one before either window opened, so the
    // 1 July → 8 July climb is excluded from each.
    expect(rows[1]).toEqual(["Tomato", "GM", "412,900", "+8,100", "+8,100"]);
  });

  // The columns are windows, not "since your first ever snapshot": each
  // measures from where the item stood when its own window opened.
  test("the two Change columns cover different windows", async () => {
    await loadPage({
      snapshots: [
        { when: new Date(2026, 6, 1, 12, 0), items: { Tomato: 400_000 } },
        { when: new Date(2026, 6, 8, 12, 0), items: { Tomato: 404_800 } },
        { when: new Date(2026, 6, 15, 0, 0), items: { Tomato: 412_900 } },
        { when: new Date(2026, 6, 16, 3, 0), items: { Tomato: 415_000 } },
      ],
    });
    // 7d opens on the 9th (baseline: the 8th), 1d on the 15th at 06:00
    // (baseline: that day's 00:00 reading).
    expect(indexRowText()[0]).toEqual([
      "Tomato",
      "GM",
      "415,000",
      "+10,200",
      "+2,100",
    ]);
  });

  test('a window with nothing to measure reads "—", not zero', async () => {
    await loadPage({
      snapshots: [
        { when: new Date(2026, 5, 1, 12, 0), items: { Tomato: 400_000 } },
        { when: new Date(2026, 5, 8, 12, 0), items: { Tomato: 404_800 } },
      ],
    });
    // A history that stopped six weeks ago: nothing moved *in* either window,
    // which is not the same as no progress having been made.
    expect(indexRowText()[0]).toEqual([
      "Tomato",
      "GM",
      "404,800",
      "—",
      "—",
    ]);
  });

  test("an index row links to that item's own timeline", async () => {
    await loadPage();
    const row = document.querySelector("#timeline-index-body tr");
    expect(row.dataset.href).toBe("/timeline?item=Hops");
  });

  // Beside each name, a link to the solver seeded with that item's next goal:
  // the tier above where it stands, or 10,000 more of an already-Megamastered
  // one (Hops is at 1,200, Tomato at 412,900).
  test("each item name carries a link to its next goal", async () => {
    await loadPage({
      snapshots: [
        {
          when: new Date(2026, 6, 15, 12, 0),
          items: { Hops: 1_200, Tomato: 412_900, Rope: 1_400_000 },
        },
      ],
      ui: { timeline_tier_MM: true },
    });
    const links = [...document.querySelectorAll("#timeline-index-body .solver-link")];
    expect(links.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["→ M", "/?mastery=Hops%3AM"],
      ["→ 10,000", "/?items=Rope%3A10000"],
      ["→ MM", "/?mastery=Tomato%3AMM"],
    ]);
  });

  // The row itself goes to the item's timeline, so its handler must leave the
  // anchor alone or the next-goal link would never reach the solver.
  test("clicking the next-goal link does not hijack the row's navigation", async () => {
    await loadPage();
    const assign = vi.fn();
    // jsdom refuses a real navigation, so intercept the assignment -- and put
    // the real location back afterwards, since everything else on this page
    // (selectItem's replaceState) reads it.
    const real = Object.getOwnPropertyDescriptor(window, "location");
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { set href(value) { assign(value); } },
    });
    try {
      document.querySelector("#timeline-index-body .solver-link").dispatchEvent(
        new Event("click", { bubbles: true })
      );
    } finally {
      Object.defineProperty(window, "location", real);
    }
    expect(assign).not.toHaveBeenCalled();
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

  // The scale toggle above the chart's top-right corner. Tomato moves 12,900
  // inside a 0 → 1M tier scale, so it starts zoomed; the button offers the
  // scale it is not in, and the tier lines follow the scale it lands on.
  test("the scale toggle flips between the data and the tier scale", async () => {
    await loadPage({ url: "/timeline?item=Tomato" });
    const button = document.querySelector("#chart-scale");
    const tiers = () =>
      [...document.querySelectorAll("#timeline-chart text.tier-label")].map(
        (t) => t.textContent
      );

    expect(button.textContent).toBe("Show next tier");
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(tiers()).toEqual([]);

    button.click();

    expect(button.textContent).toBe("Zoom to data");
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(tiers()).toEqual(["GM", "MM"]);
    // Redrawn, not duplicated -- and the table below is untouched.
    expect(document.querySelectorAll("#timeline-chart svg")).toHaveLength(1);
    expect(document.querySelectorAll("#timeline-body tr")).toHaveLength(3);

    button.click();
    expect(tiers()).toEqual([]);
  });

  test("the chosen scale is remembered for the next visit", async () => {
    await loadPage({ url: "/timeline?item=Tomato" });
    document.querySelector("#chart-scale").click();
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      timeline_scale: "full",
    });

    await loadPage({ url: "/timeline?item=Tomato", ui: { timeline_scale: "full" } });
    expect(document.querySelector("#chart-scale").textContent).toBe("Zoom to data");
    expect(
      [...document.querySelectorAll("#timeline-chart text.tier-label")].map(
        (t) => t.textContent
      )
    ).toEqual(["GM", "MM"]);
  });

  // The chart's one live control: hovering a point raises the shared tooltip
  // box, fills it with that snapshot's date/time and mastery, and moves it over
  // the point. Leaving puts it away again.
  test("hovering a point shows its date and mastery", async () => {
    await loadPage({ url: "/timeline?item=Tomato" });
    const tip = document.querySelector("#timeline-chart .tip");
    expect(tip.getAttribute("opacity")).toBe("0");

    const hits = document.querySelectorAll("#timeline-chart circle.pt-hit");
    hits[1].dispatchEvent(new Event("mouseover", { bubbles: true }));

    expect(tip.getAttribute("opacity")).toBe("1");
    expect([...tip.querySelectorAll(".tip-line")].map((t) => t.textContent)).toEqual([
      "2026-07-08 12:00",
      "404,800 mastery",
    ]);
    // Positioned on the point it belongs to, and given a box to sit in.
    const [x] = tip.getAttribute("transform").match(/-?\d+/g).map(Number);
    const cx = Number(hits[1].getAttribute("cx"));
    expect(Math.abs(x + Number(tip.querySelector(".tip-box").getAttribute("width")) / 2 - cx))
      .toBeLessThanOrEqual(1);

    hits[1].dispatchEvent(new Event("mouseout", { bubbles: true }));
    expect(tip.getAttribute("opacity")).toBe("0");
  });

  // One box serves every point, so moving between them has to refill it and
  // move it, not leave the previous snapshot's reading sitting there.
  test("the tooltip follows the pointer from one point to the next", async () => {
    await loadPage({ url: "/timeline?item=Hops" });
    const tip = document.querySelector("#timeline-chart .tip");
    const hits = document.querySelectorAll("#timeline-chart circle.pt-hit");
    hits[0].dispatchEvent(new Event("mouseover", { bubbles: true }));
    const first = tip.getAttribute("transform");
    hits[1].dispatchEvent(new Event("mouseover", { bubbles: true }));
    expect([...tip.querySelectorAll(".tip-line")].map((t) => t.textContent)).toEqual([
      "2026-07-15 12:00",
      "1,200 mastery",
    ]);
    expect(tip.getAttribute("transform")).not.toBe(first);
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
    expect(document.querySelector("#status").hidden).toBe(false);
    expect(document.querySelector("#status").textContent).toMatch(/No snapshot mentions/);
    expect(document.querySelector("#timeline-chart").hidden).toBe(true);
    expect(document.querySelector("#timeline-section").hidden).toBe(true);
  });

  test("points at the settings popover when there is no history at all", async () => {
    await loadPage({ snapshots: [] });
    expect(document.querySelector("#timeline-empty").hidden).toBe(false);
    expect(document.querySelector("#timeline-index").hidden).toBe(true);
    // #timeline-empty covers this one, so the box stays out of it.
    expect(document.querySelector("#status").hidden).toBe(true);
  });

  // The box is for the states that would otherwise go unexplained; when there
  // is a chart or a listed index to read, it says nothing at all.
  test("hides the status box once there is something to show", async () => {
    await loadPage();
    expect(document.querySelector("#status").hidden).toBe(true);

    await loadPage({ url: "/timeline?item=Tomato" });
    expect(document.querySelector("#timeline-chart").hidden).toBe(false);
    expect(document.querySelector("#status").hidden).toBe(true);
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
    expect(indexRowText().map((cells) => cells[0])).toEqual([
      "Hops",
      "Tomato",
    ]);

    clickSort("timeline-index-head", "latest");
    expect(indexRowText().map((cells) => cells[0])).toEqual([
      "Hops",
      "Tomato",
    ]);
    // Clicking the active column reverses it.
    clickSort("timeline-index-head", "latest");
    expect(indexRowText().map((cells) => cells[0])).toEqual([
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
    clickSort("timeline-index-head", "change7");

    // Item, Tier, Latest, Change (7d), Change (1d) -- the sorted one fourth.
    const headers = [...document.querySelectorAll("#timeline-index-head th")];
    expect(headers.map((th) => th.getAttribute("aria-sort"))).toEqual([
      "none",
      "none",
      "none",
      "ascending",
      "none",
    ]);
    expect(
      document.querySelector('#timeline-index-head button[data-sort="change7"] .sort-arrow')
        .textContent
    ).toBe("▲");
    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      timeline_index_sort: "change7",
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

// The index's filters: which tiers to list, and what the items could be made
// from. Both belong to the index, so neither appears while one item is charted.
describe("the index filters", () => {
  // Rope is Megamastered, so the default filters hide it; the rest are spread
  // across the other three tiers.
  const CRAFTED = [
    {
      when: new Date(2026, 6, 15, 12, 0),
      items: {
        Twine: 400_000, // GM, made from Straw
        Rope: 1_400_000, // MM, made from Twine
        "Fishing Net": 20_000, // M, made from Rope
        Board: 500, // not mastered, made from Wood
      },
    },
  ];

  const items = () =>
    [...document.querySelectorAll("#timeline-index-body tr td:first-child")].map(cellText);
  const chips = () =>
    [...document.querySelectorAll("#material-chips .chip")].map((chip) =>
      chip.textContent.replace("×", "").trim()
    );
  const $$ = (selector) => document.querySelector(selector);

  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  test("hides Megamastered items until MM is ticked", async () => {
    await loadPage({ snapshots: CRAFTED });
    expect($$("#tier-mm").checked).toBe(false);
    expect(items()).toEqual(["Board", "Fishing Net", "Twine"]);

    $$("#tier-mm").checked = true;
    $$("#tier-mm").dispatchEvent(new Event("change"));
    expect(items()).toEqual(["Board", "Fishing Net", "Rope", "Twine"]);
  });

  test("unticking a tier drops its rows", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#tier-none").checked = false;
    $$("#tier-none").dispatchEvent(new Event("change"));
    expect(items()).toEqual(["Fishing Net", "Twine"]); // Board was the only one
  });

  // The point of the whole filter: Straw reaches Twine, then Rope, then
  // Fishing Net -- not just the recipes that name Straw itself.
  test('"made from" follows the recipes all the way down', async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#tier-mm").checked = true;
    $$("#tier-mm").dispatchEvent(new Event("change"));

    $$("#material-input").value = "Straw";
    $$("#material-add").click();

    expect(chips()).toEqual(["Straw"]);
    expect(items()).toEqual(["Fishing Net", "Rope", "Twine"]); // no Board
  });

  test("a material can be added by pressing Enter", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#material-input").value = "Wood";
    $$("#material-input").dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true })
    );
    expect(chips()).toEqual(["Wood"]);
    expect($$("#material-input").value).toBe(""); // and the box is cleared
    expect(items()).toEqual(["Board"]);
  });

  test("materials union rather than narrowing each other", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#material-input").value = "Straw";
    $$("#material-add").click();
    $$("#material-input").value = "Wood";
    $$("#material-add").click();

    expect(chips()).toEqual(["Straw", "Wood"]);
    expect(items()).toEqual(["Board", "Fishing Net", "Twine"]);
  });

  test("an item no recipe uses is not accepted as a material", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#material-input").value = "Fishing Net"; // nothing is made from it
    $$("#material-add").click();
    expect(chips()).toEqual([]);
  });

  test("a zone fills in the drops that are actually materials", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#zone-select").value = "0"; // Small Cave: Wood + Wooden Mask
    $$("#zone-add").click();

    // Wooden Mask is a drop, but no recipe uses it, so it never becomes a chip.
    expect(chips()).toEqual(["Wood"]);
    expect(items()).toEqual(["Board"]);
  });

  test("the same zone name can be either an exploring or a fishing one", async () => {
    await loadPage({ snapshots: CRAFTED });
    const options = [...document.querySelectorAll("#zone-select optgroup")].map(
      (group) => [group.label, [...group.children].map((o) => o.textContent)]
    );
    expect(options).toEqual([
      ["Exploring", ["Small Cave"]],
      ["Fishing", ["Small Pond"]],
    ]);
    // Zones are picked by index, not name, so the two are never confused.
    $$("#zone-select").value = "1";
    $$("#zone-add").click();
    expect(chips()).toEqual(["Straw"]);
  });

  test("a chip's × removes it and Clear all empties the filter", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#material-input").value = "Straw";
    $$("#material-add").click();
    $$("#material-input").value = "Wood";
    $$("#material-add").click();

    document.querySelector('#material-chips button[data-material="straw"]').click();
    expect(chips()).toEqual(["Wood"]);

    $$("#material-clear").click();
    expect(chips()).toEqual([]);
    expect($$("#material-chips").hidden).toBe(true);
    expect(items()).toEqual(["Board", "Fishing Net", "Twine"]); // unfiltered again
  });

  test("the filters are remembered for the next visit", async () => {
    await loadPage({ snapshots: CRAFTED });
    $$("#tier-mm").checked = true;
    $$("#tier-mm").dispatchEvent(new Event("change"));
    $$("#material-input").value = "Straw";
    $$("#material-add").click();

    expect(JSON.parse(localStorage.getItem("farm-solver.ui.v1"))).toMatchObject({
      timeline_tier_MM: true,
      timeline_materials: ["straw"],
    });

    await loadPage({
      snapshots: CRAFTED,
      ui: { timeline_tier_MM: true, timeline_materials: ["straw"] },
    });
    expect($$("#tier-mm").checked).toBe(true);
    expect(chips()).toEqual(["Straw"]);
    expect(items()).toEqual(["Fishing Net", "Rope", "Twine"]);
  });

  test("says so when the filters leave nothing, without hiding them", async () => {
    await loadPage({ snapshots: CRAFTED });
    ["#tier-none", "#tier-m", "#tier-gm"].forEach((selector) => {
      $$(selector).checked = false;
      $$(selector).dispatchEvent(new Event("change"));
    });
    expect(items()).toEqual([]);
    expect($$("#status").hidden).toBe(false);
    expect($$("#status").textContent).toMatch(/Nothing matches these filters/);
    expect($$("#filter-details").hidden).toBe(false);
  });

  test("are hidden while one item is charted", async () => {
    await loadPage({ url: "/timeline?item=Twine", snapshots: CRAFTED });
    expect($$("#filter-details").hidden).toBe(true);
  });

  // The graph is a nicety: without it the tier filter and the rest of the page
  // must still work.
  test("survive a catalogue that never arrives", async () => {
    await loadPage({ snapshots: CRAFTED, catalogue: null });
    expect(items()).toEqual(["Board", "Fishing Net", "Twine"]);
    $$("#material-input").value = "Straw";
    $$("#material-add").click();
    expect(chips()).toEqual([]); // nothing to check it against, so nothing added
  });
});
