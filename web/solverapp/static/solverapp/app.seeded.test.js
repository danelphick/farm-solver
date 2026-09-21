// The other half of the mastery-costs drill-down: arriving at the solver page
// as "/?mastery=Item:TIER" must seed that one goal and solve it. Driven against
// index.html's REAL markup (Django tags stripped), so a renamed id fails here
// rather than in a browser.
//
// app.js registers its DOMContentLoaded -> init() listener at module scope, so
// the DOM, the injected #config-defaults JSON and the fetch/localStorage stubs
// all have to exist before the dynamic import.

import { beforeEach, describe, expect, test, vi } from "vitest";

import { pageBody } from "./template-fixture.js";

// init() reads the config the index view injects (webapi.config_defaults) and
// fetches the two datalist catalogues; an empty config and empty catalogues are
// enough for everything this test asserts.
function buildPage(search) {
  document.body.innerHTML =
    pageBody("index") +
    '<script id="config-defaults" type="application/json">{}</script>';
  window.history.replaceState({}, "", search || "/");
}

// See masteries.page.test.js: jsdom ships no Storage implementation here.
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

async function loadPage({ search, saved, solutionHtml } = {}) {
  buildPage(search);
  const store = installLocalStorage(saved);
  const response = { ...SOLVE_RESPONSE, solution_html: solutionHtml || "" };
  fetchMock = vi.fn(async (url) =>
    url === "/api/solve"
      ? { ok: true, status: 200, json: async () => response }
      : { ok: true, status: 200, text: async () => "" }  // a datalist catalogue
  );
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
  await import("./app.js");
  document.dispatchEvent(new Event("DOMContentLoaded"));
  return store;
}

// The datalist catalogues are fetched through the same stub, so the solves have
// to be picked out by URL.
const solveCalls = () =>
  fetchMock.mock.calls.filter(([url]) => url === "/api/solve");

const rowsIn = (containerId) =>
  [...document.querySelectorAll(`#${containerId} .goal-row`)]
    .map((r) => {
      const [item, amount] = r.querySelectorAll("input");
      return { item: item.value, amount: amount.value };
    })
    // renderGoalRows always leaves one blank row to type into, which is not a
    // goal -- only the filled ones are.
    .filter((row) => row.item || row.amount);

const goalRows = () => rowsIn("mastery-rows");
const itemRows = () => rowsIn("items-rows");

const SAVED = {
  mastery_text: "Mastery In-Progress",
  inventory_text: "Items chevron_down",
  params: { target: "tower", mastery: "Gold Bar:5000", max_gold: "500" },
};

describe("seeded mastery goal", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  test("seeds the one goal, drops the tower, and keeps other settings", async () => {
    await loadPage({ search: "/?mastery=Compass%3AGM", saved: SAVED });

    expect(goalRows()).toEqual([{ item: "Compass", amount: "GM" }]);
    expect(document.querySelector("#target").value).toBe("none");
    // The restored settings survive -- only the goal is replaced.
    expect(document.querySelector("#max-gold").value).toBe("500");

    const banner = document.querySelector("#seeded-banner");
    expect(banner.hidden).toBe(false);
    expect(document.querySelector("#seeded-goal").textContent).toBe(
      "Compass → Grandmaster"
    );

    await vi.waitFor(() => expect(solveCalls().length).toBeGreaterThan(0));
    const body = JSON.parse(solveCalls()[0][1].body);
    expect(body.params.mastery).toBe("Compass:GM");
    expect(body.params.target).toBe("none");
  });

  test("following the link does not overwrite the saved goal", async () => {
    const store = await loadPage({ search: "/?mastery=Compass%3AGM", saved: SAVED });
    await vi.waitFor(() => expect(solveCalls().length).toBeGreaterThan(0));

    const stillSaved = JSON.parse(store.get("farm-solver.v1"));
    expect(stillSaved.params.mastery).toBe("Gold Bar:5000");
    expect(stillSaved.params.target).toBe("tower");
  });

  test("a seeded goal solves even with nothing saved to restore", async () => {
    // No saved state at all -- but the URL carries a goal, so there is
    // something to solve... except the paste boxes are empty, so solve()
    // short-circuits before requesting. The seeding itself still applies.
    await loadPage({ search: "/?mastery=Valve%3AM" });
    expect(goalRows()).toEqual([{ item: "Valve", amount: "M" }]);
    expect(document.querySelector("#seeded-banner").hidden).toBe(false);
    expect(solveCalls()).toEqual([]);
  });

  // The timeline index's link for an item that is already Megamastered: no
  // tier is left to aim at, so it asks for a plain count instead. That goes in
  // the ITEM list, not the mastery one -- a mastery goal of 10,000 on a
  // Megamastered item is already met, so it would plan nothing at all.
  test("an items goal seeds the item list, not the mastery one", async () => {
    await loadPage({ search: "/?items=Rope%3A10000", saved: SAVED });

    expect(goalRows()).toEqual([]); // no mastery rows
    expect(itemRows()).toEqual([{ item: "Rope", amount: "10000" }]);
    expect(document.querySelector("#target").value).toBe("none");
    expect(document.querySelector("#seeded-goal").textContent).toBe("10,000 × Rope");

    await vi.waitFor(() => expect(solveCalls().length).toBeGreaterThan(0));
    const body = JSON.parse(solveCalls()[0][1].body);
    expect(body.params.items).toBe("Rope:10000");
    expect(body.params.mastery).toBeUndefined();
  });

  test("a normal visit leaves the banner hidden and the saved goal intact", async () => {
    await loadPage({ search: "/", saved: SAVED });
    expect(document.querySelector("#seeded-banner").hidden).toBe(true);
    expect(goalRows()).toEqual([{ item: "Gold Bar", amount: "5000" }]);
    // "" is the dropdown's "Tower (highest reachable)" option, which readParams
    // sends as target="tower" -- unlike the seeded "none".
    expect(document.querySelector("#target").value).toBe("");
  });
});

// --- Focus: drilling into one row of the rendered Goal table ----------------

// What markdown_solution's Goal section renders to (Purpose/Item/Have/Target/
// Still needed), as api_solve's `solution_html` delivers it.
function goalTableHtml(rows) {
  const body = rows
    .map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`)
    .join("");
  return (
    "<h2>Goal</h2><table><thead><tr><th>Purpose</th><th>Item</th><th>Have</th>" +
    `<th>Target</th><th>Still needed</th></tr></thead><tbody>${body}</tbody></table>`
  );
}

const TWO_GOALS = goalTableHtml([
  ["T240", "Gold Bar", "1,200", "100,000", "98,800"],
  ["Inventory", "Rope", "5", "500", "495"],
]);

const focusLinks = () =>
  [...document.querySelectorAll("#solution-section .focus-link")].map((a) => ({
    text: a.textContent,
    href: a.getAttribute("href"),
  }));

describe("Focus buttons on the Goal table", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  test("every goal row gets one, seeded with that row's own goal", async () => {
    await loadPage({ search: "/", saved: SAVED, solutionHtml: TWO_GOALS });
    await vi.waitFor(() => expect(focusLinks().length).toBe(2));

    expect(focusLinks()).toEqual([
      { text: "Focus", href: "/?mastery=Gold%20Bar%3A100000&from=goal" },
      { text: "Focus", href: "/?items=Rope%3A500&from=goal" },
    ]);
    // The item name itself is untouched -- the link is appended beside it.
    const cell = document.querySelector("#solution-section tbody td:nth-child(2)");
    expect(cell.textContent.trim().startsWith("Gold Bar")).toBe(true);
  });

  test("a sole goal gets none -- it is already focused", async () => {
    const single = goalTableHtml([["Mastery", "Compass", "0", "100,000", "100,000"]]);
    await loadPage({ search: "/", saved: SAVED, solutionHtml: single });
    await vi.waitFor(() => expect(solveCalls().length).toBeGreaterThan(0));
    await vi.waitFor(() =>
      expect(document.querySelector("#solution-section table")).not.toBeNull()
    );
    expect(focusLinks()).toEqual([]);
  });

  // The "Materials with no known source" table is a separate <h2> section in
  // the same container; only the Goal table's rows are goals.
  test("the no-known-source table is left alone", async () => {
    const html =
      TWO_GOALS +
      "<h2>Materials with no known source (supply yourself)</h2><table><tbody>" +
      "<tr><td>Magna Core</td><td>3</td></tr></tbody></table>";
    await loadPage({ search: "/", saved: SAVED, solutionHtml: html });
    await vi.waitFor(() => expect(focusLinks().length).toBe(2));
    expect(document.querySelectorAll("#solution-section table")[1].querySelector(".focus-link")).toBeNull();
  });
});

describe("Make Sole Goal", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  test("substitutes the seeded goal into the saved parameters", async () => {
    const store = await loadPage({
      search: "/?mastery=Compass%3AGM&from=goal",
      saved: SAVED,
    });
    await vi.waitFor(() => expect(solveCalls().length).toBeGreaterThan(0));

    document.querySelector("#seeded-adopt").click();

    const saved = JSON.parse(store.get("farm-solver.v1"));
    expect(saved.params.mastery).toBe("Compass:GM");
    expect(saved.params.target).toBe("none");
    // Everything else the user had is carried through untouched.
    expect(saved.params.max_gold).toBe("500");
    // The banner has done its job, and the query goes with it so a reload
    // doesn't re-seed what is now the saved goal.
    expect(document.querySelector("#seeded-banner").hidden).toBe(true);
    expect(window.location.search).toBe("");
    // Adopting is a save, not a re-solve: the results already match the goal.
    expect(solveCalls().length).toBe(1);
  });

  test("a Focus drill-down points back at the user's own goals", async () => {
    await loadPage({ search: "/?mastery=Compass%3AGM&from=goal", saved: SAVED });
    const back = document.querySelector("#seeded-back");
    expect(back.getAttribute("href")).toBe("/");
    expect(back.textContent).toContain("back to your goals");
    // A plain mastery number reads as an amount, not a tier token.
    await loadPage({ search: "/?mastery=Compass%3A100000&from=goal", saved: SAVED });
    expect(document.querySelector("#seeded-goal").textContent).toBe(
      "Compass → 100,000 mastery"
    );
  });

  test("a mastery-costs link still points back there", async () => {
    await loadPage({ search: "/?mastery=Compass%3AGM", saved: SAVED });
    const back = document.querySelector("#seeded-back");
    expect(back.getAttribute("href")).toBe("/masteries");
    expect(back.textContent).toContain("back to mastery costs");
  });
});
