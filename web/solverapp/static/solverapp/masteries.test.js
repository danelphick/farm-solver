import { describe, expect, test } from "vitest";
import { masteryHref, sortRows } from "./masteries.js";
import { tierLabel } from "./tiers.js";

const row = (item, tier, seconds, gold) => ({
  item,
  tier,
  time: `${seconds}s`,
  time_seconds: seconds,
  gold,
});

describe("sortRows", () => {
  // The JS mirror of main._sorted_mastery_rows -- see tests/test_mastery_costs.py's
  // test_sort_by_time_breaks_ties_on_gold / test_sort_by_gold_breaks_ties_on_time.
  test("by time, breaking ties on gold", () => {
    const rows = [
      row("Slow", "M", 500, 10),
      row("FastDear", "M", 100, 900),
      row("FastCheap", "M", 100, 100),
    ];
    expect(sortRows(rows, "time").map((r) => r.item)).toEqual([
      "FastCheap",
      "FastDear",
      "Slow",
    ]);
  });

  test("by gold, breaking ties on time", () => {
    const rows = [
      row("DearFast", "M", 100, 500),
      row("CheapSlow", "M", 900, 20),
      row("CheapFast", "M", 200, 20),
    ];
    expect(sortRows(rows, "gold").map((r) => r.item)).toEqual([
      "CheapFast",
      "CheapSlow",
      "DearFast",
    ]);
  });

  test("rows equal on both axes keep the server's order", () => {
    const rows = [row("Beta", "M", 60, 5), row("Alpha", "GM", 60, 5)];
    expect(sortRows(rows, "time").map((r) => r.item)).toEqual(["Beta", "Alpha"]);
  });

  test("does not mutate the input array", () => {
    const rows = [row("Slow", "M", 500, 10), row("Fast", "M", 100, 10)];
    sortRows(rows, "time");
    expect(rows.map((r) => r.item)).toEqual(["Slow", "Fast"]);
  });
});

describe("masteryHref", () => {
  test("links to the solver page seeded with the goal", () => {
    expect(masteryHref(row("Compass", "GM", 60, 0))).toBe(
      "/?mastery=Compass%3AGM"
    );
  });

  test("escapes an item name with spaces and apostrophes", () => {
    expect(masteryHref(row("Witch's Brew", "M", 60, 0))).toBe(
      "/?mastery=Witch's%20Brew%3AM"
    );
  });
});

describe("tierLabel", () => {
  test("spells out the three tier tokens", () => {
    expect(tierLabel("M")).toBe("Master");
    expect(tierLabel("GM")).toBe("Grandmaster");
    expect(tierLabel("MM")).toBe("Megamaster");
  });

  test("leaves a plain amount alone", () => {
    expect(tierLabel("50000")).toBe("50000");
  });
});
