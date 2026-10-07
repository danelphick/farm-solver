import { describe, expect, test } from "vitest";
import {
  focusGoalHref,
  isIgnoredItem,
  parseGoalSpec,
  parseIgnoreSpec,
  parseSeededGoal,
  seededOrigin,
  serializeGoalEntries,
  toggleIgnoreSpec,
} from "./app.js";

describe("parseGoalSpec", () => {
  test("parses a single item:amount pair", () => {
    expect(parseGoalSpec("Grand Piano:1000000")).toEqual([
      { item: "Grand Piano", amount: "1000000" },
    ]);
  });

  test("parses multiple comma-separated pairs, trimming whitespace", () => {
    expect(parseGoalSpec("Wood:100, Stone:200 , Board:5k")).toEqual([
      { item: "Wood", amount: "100" },
      { item: "Stone", amount: "200" },
      { item: "Board", amount: "5k" },
    ]);
  });

  test("empty/blank spec yields no rows", () => {
    expect(parseGoalSpec("")).toEqual([]);
    expect(parseGoalSpec(undefined)).toEqual([]);
    expect(parseGoalSpec(",  ,")).toEqual([]);
  });

  test("a chunk with no ':' keeps the item with an empty amount, rather than being dropped", () => {
    expect(parseGoalSpec("Wood:100,Half-typed")).toEqual([
      { item: "Wood", amount: "100" },
      { item: "Half-typed", amount: "" },
    ]);
  });

  test("splits on the last ':' so a colon-bearing item name still parses", () => {
    expect(parseGoalSpec("Time: A Gift:5")).toEqual([
      { item: "Time: A Gift", amount: "5" },
    ]);
  });
});

describe("serializeGoalEntries", () => {
  test("round-trips through parseGoalSpec", () => {
    const spec = "Wood:100,Stone:200";
    expect(serializeGoalEntries(parseGoalSpec(spec))).toBe(spec);
  });

  test("preserves shorthand amounts (1M/100k/GM) unparsed", () => {
    const spec = "Apple Cider:1M,Orange Juice:100k,Steel:GM";
    expect(serializeGoalEntries(parseGoalSpec(spec))).toBe(spec);
  });

  test("drops rows missing an item or an amount", () => {
    expect(
      serializeGoalEntries([
        { item: "Wood", amount: "100" },
        { item: "", amount: "200" },
        { item: "Stone", amount: "" },
        { item: "  ", amount: "  " },
      ])
    ).toBe("Wood:100");
  });

  test("empty entries list serializes to an empty string", () => {
    expect(serializeGoalEntries([])).toBe("");
  });
});

describe("parseSeededGoal", () => {
  // The mastery-costs page links each row here (masteries.js's masteryHref),
  // as does the timeline index for an item with a tier left to reach.
  test("reads the goal a mastery-costs row links with", () => {
    expect(parseSeededGoal("?mastery=Compass%3AGM")).toEqual({
      kind: "mastery",
      item: "Compass",
      amount: "GM",
    });
  });

  // The timeline's link for an already-Megamastered item: no tier left, so the
  // goal is a plain count of the item instead.
  test("reads an item-count goal too", () => {
    expect(parseSeededGoal("?items=Rope%3A10000")).toEqual({
      kind: "items",
      item: "Rope",
      amount: "10000",
    });
  });

  test("splits on the last ':' so a colon-bearing item name survives", () => {
    expect(parseSeededGoal("?mastery=Time%3A%20A%20Gift%3AM")).toEqual({
      kind: "mastery",
      item: "Time: A Gift",
      amount: "M",
    });
  });

  test("ignores other query strings, and an incomplete goal", () => {
    expect(parseSeededGoal("")).toBeNull();
    expect(parseSeededGoal("?plan=3")).toBeNull();
    expect(parseSeededGoal("?mastery=Compass")).toBeNull(); // no amount
    expect(parseSeededGoal("?mastery=")).toBeNull();
    expect(parseSeededGoal("?items=Rope")).toBeNull();
  });

  test("a mastery goal wins if a link somehow carries both", () => {
    expect(parseSeededGoal("?items=Rope%3A10000&mastery=Compass%3AGM")).toMatchObject({
      kind: "mastery",
      item: "Compass",
    });
  });
});

describe("focusGoalHref", () => {
  // The three Purpose values report.markdown_solution writes: a tower level, a
  // plain -m goal, and a plain -i one. Only the last is an item count; the
  // other two reproduce as mastery goals, seeded with the row's own absolute
  // Target (commas stripped -- report.py groups its numbers).
  test("a tower requirement seeds a mastery goal at that row's target", () => {
    expect(focusGoalHref("T240", "Gold Bar", "100,000")).toBe(
      "/?mastery=Gold%20Bar%3A100000&from=goal"
    );
  });

  test("a Mastery row seeds a mastery goal", () => {
    expect(focusGoalHref("Mastery", "Compass", "10,000")).toBe(
      "/?mastery=Compass%3A10000&from=goal"
    );
  });

  test("an Inventory row seeds an item-count goal", () => {
    expect(focusGoalHref("Inventory", "Rope", "500")).toBe(
      "/?items=Rope%3A500&from=goal"
    );
  });

  test("a colon-bearing item name is encoded, not split", () => {
    expect(focusGoalHref("Mastery", "Time: A Gift", "10,000")).toBe(
      "/?mastery=Time%3A%20A%20Gift%3A10000&from=goal"
    );
  });

  test("no link for a row with no item or no plain-number target", () => {
    expect(focusGoalHref("Mastery", "", "10,000")).toBeNull();
    expect(focusGoalHref("Mastery", "Compass", "")).toBeNull();
    expect(focusGoalHref("Mastery", "Compass", "done")).toBeNull();
  });
});

describe("seededOrigin", () => {
  test("a Focus link off the Goal table is flagged as such", () => {
    expect(seededOrigin("?mastery=Compass%3AGM&from=goal")).toBe("goal");
  });

  test("every other seeded link keeps the mastery-costs wording", () => {
    expect(seededOrigin("?mastery=Compass%3AGM")).toBe("masteries");
    expect(seededOrigin("")).toBe("masteries");
    expect(seededOrigin("?from=elsewhere")).toBe("masteries");
  });
});

describe("the ignored-goals spec", () => {
  test("parses a comma list, trimming blanks", () => {
    expect(parseIgnoreSpec("Yule Goat, Wood")).toEqual(["Yule Goat", "Wood"]);
    expect(parseIgnoreSpec("")).toEqual([]);
    expect(parseIgnoreSpec(undefined)).toEqual([]);
    expect(parseIgnoreSpec(" , Wood , ")).toEqual(["Wood"]);
  });

  // main.parse_ignored_goals lower-cases both sides, so the client must too --
  // the name is captured from a table cell and matched against a list saved
  // from an earlier render.
  test("matches an item case-insensitively", () => {
    expect(isIgnoredItem(["Yule Goat"], "yule goat")).toBe(true);
    expect(isIgnoredItem(["Yule Goat"], " Yule Goat ")).toBe(true);
    expect(isIgnoredItem(["Yule Goat"], "Wood")).toBe(false);
    expect(isIgnoredItem([], "Wood")).toBe(false);
  });

  test("toggles an item on and back off again", () => {
    expect(toggleIgnoreSpec([], "Wood")).toEqual(["Wood"]);
    expect(toggleIgnoreSpec(["Yule Goat"], "Wood")).toEqual(["Yule Goat", "Wood"]);
    expect(toggleIgnoreSpec(["Yule Goat", "Wood"], "wood")).toEqual(["Yule Goat"]);
    // Pure: the list it was given is left alone.
    const names = ["Wood"];
    toggleIgnoreSpec(names, "Wood");
    expect(names).toEqual(["Wood"]);
  });
});
