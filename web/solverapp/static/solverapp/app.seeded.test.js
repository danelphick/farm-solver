// The other half of the mastery-costs drill-down: arriving at the solver page
// as "/?mastery=Item:TIER" must seed that one goal and solve it. Driven against
// index.html's REAL markup (Django tags stripped), so a renamed id fails here
// rather than in a browser.
//
// app.js registers its DOMContentLoaded -> init() listener at module scope, so
// the DOM, the injected #config-defaults JSON and the fetch/localStorage stubs
// all have to exist before the dynamic import.

import { beforeEach, describe, expect, test, vi } from "vitest";

import TEMPLATE from "../../templates/solverapp/index.html?raw";

function templateBody() {
  const html = TEMPLATE.replace(/\{%.*?%\}/g, "").replace(/\{\{.*?\}\}/g, "");
  return html.slice(html.indexOf("<body>") + 6, html.indexOf("</body>"));
}

// init() reads the config the index view injects (webapi.config_defaults) and
// fetches the two datalist catalogues; an empty config and empty catalogues are
// enough for everything this test asserts.
function buildPage(search) {
  document.body.innerHTML =
    templateBody() +
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

async function loadPage({ search, saved } = {}) {
  buildPage(search);
  const store = installLocalStorage(saved);
  fetchMock = vi.fn(async (url) =>
    url === "/api/solve"
      ? { ok: true, status: 200, json: async () => SOLVE_RESPONSE }
      : { ok: true, status: 200, text: async () => "" }  // a datalist catalogue
  );
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
  await import("./app.js");
  document.dispatchEvent(new Event("DOMContentLoaded"));
  return store;
}

// Each test re-imports app.js (resetModules) while `document` is shared across
// the file, so every earlier import's DOMContentLoaded listener fires again and
// solves too -- they all send the identical body, so assertions read the first
// call and only ever check that *some* solve happened. See the same note in
// masteries.page.test.js.
const solveCalls = () =>
  fetchMock.mock.calls.filter(([url]) => url === "/api/solve");

const goalRows = () =>
  [...document.querySelectorAll("#mastery-rows .goal-row")].map((r) => {
    const [item, amount] = r.querySelectorAll("input");
    return { item: item.value, amount: amount.value };
  });

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

  test("a normal visit leaves the banner hidden and the saved goal intact", async () => {
    await loadPage({ search: "/", saved: SAVED });
    expect(document.querySelector("#seeded-banner").hidden).toBe(true);
    expect(goalRows()).toEqual([{ item: "Gold Bar", amount: "5000" }]);
    // "" is the dropdown's "Tower (highest reachable)" option, which readParams
    // sends as target="tower" -- unlike the seeded "none".
    expect(document.querySelector("#target").value).toBe("");
  });
});
