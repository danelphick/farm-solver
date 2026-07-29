import { describe, expect, test } from "vitest";
import { masteryHref, sortRows, timelineHref, SORT_COLUMNS } from "./masteries.js";
import { tierForProgress, tierLabel, tierRank } from "./tiers.js";

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

  test("by item, case-insensitively, breaking ties on tier order", () => {
    const rows = [
      row("banjo", "MM", 100, 10),
      row("Awl", "M", 500, 90),
      row("Banjo", "M", 900, 20),
    ];
    // "banjo" and "Banjo" tie on the item axis, so the tier order splits them.
    expect(sortRows(rows, "item").map((r) => `${r.item}:${r.tier}`)).toEqual([
      "Awl:M",
      "Banjo:M",
      "banjo:MM",
    ]);
  });

  test("by target, M before GM before MM, never alphabetically", () => {
    const rows = [
      row("Zed", "MM", 100, 10),
      row("Alpha", "GM", 100, 10),
      row("Mid", "M", 100, 10),
    ];
    // Alphabetically this would be GM, M, MM -- the tier order is the point.
    expect(sortRows(rows, "tier").map((r) => r.tier)).toEqual(["M", "GM", "MM"]);
  });

  test("by target, breaking ties on time then gold", () => {
    const rows = [
      row("SlowM", "M", 500, 10),
      row("Mega", "MM", 100, 10),
      row("FastDearM", "M", 100, 900),
      row("FastCheapM", "M", 100, 100),
    ];
    expect(sortRows(rows, "tier").map((r) => r.item)).toEqual([
      "FastCheapM",
      "FastDearM",
      "SlowM",
      "Mega",
    ]);
  });

  test("descending by target keeps time and gold ascending within a target", () => {
    const rows = [
      row("SlowM", "M", 500, 10),
      row("Mega", "MM", 100, 10),
      row("FastDearM", "M", 100, 900),
      row("FastCheapM", "M", 100, 100),
    ];
    // MM/GM/M, but each target's own rows stay cheapest-first.
    expect(sortRows(rows, "tier", "desc").map((r) => r.item)).toEqual([
      "Mega",
      "FastCheapM",
      "FastDearM",
      "SlowM",
    ]);
  });

  test("descending reverses the whole ranking", () => {
    const rows = [
      row("Slow", "M", 500, 10),
      row("FastDear", "M", 100, 900),
      row("FastCheap", "M", 100, 100),
    ];
    expect(sortRows(rows, "time", "desc").map((r) => r.item)).toEqual([
      "Slow",
      "FastDear",  // the gold tie-break reverses too
      "FastCheap",
    ]);
  });

  test("rows equal on every axis keep the server's order in both directions", () => {
    const rows = [row("Same", "M", 60, 5), row("Same", "M", 60, 5)];
    rows[0].id = "first";
    rows[1].id = "second";
    for (const direction of ["asc", "desc"]) {
      expect(sortRows(rows, "item", direction).map((r) => r.id)).toEqual([
        "first",
        "second",
      ]);
    }
  });

  test("an unknown key falls back to time", () => {
    const rows = [row("Slow", "M", 500, 10), row("Fast", "M", 100, 10)];
    expect(sortRows(rows, "nonsense").map((r) => r.item)).toEqual([
      "Fast",
      "Slow",
    ]);
  });

  test("SORT_COLUMNS names every sortable column", () => {
    expect(SORT_COLUMNS).toEqual(["item", "tier", "time", "gold"]);
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

describe("timelineHref", () => {
  test("links to that item's mastery timeline", () => {
    expect(timelineHref("Compass")).toBe("/timeline?item=Compass");
    expect(timelineHref("Oak Table")).toBe("/timeline?item=Oak%20Table");
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

describe("tierRank", () => {
  test("ranks the tiers by work, not alphabetically", () => {
    expect(tierRank("M")).toBeLessThan(tierRank("GM"));
    expect(tierRank("GM")).toBeLessThan(tierRank("MM"));
  });

  test("a plain amount ranks after every tier", () => {
    expect(tierRank("50000")).toBeGreaterThan(tierRank("MM"));
  });
});

describe("tierForProgress", () => {
  test("names the tier an absolute progress has reached", () => {
    expect(tierForProgress(0)).toBe("");
    expect(tierForProgress(9_999)).toBe("");
    expect(tierForProgress(10_000)).toBe("M");
    expect(tierForProgress(99_999)).toBe("M");
    expect(tierForProgress(100_000)).toBe("GM");
    expect(tierForProgress(1_000_000)).toBe("MM");
    expect(tierForProgress(893_793_719)).toBe("MM");
  });
});
