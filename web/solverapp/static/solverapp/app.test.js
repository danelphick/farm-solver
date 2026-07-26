import { describe, expect, test } from "vitest";
import {
  parseGoalSpec,
  parseSeededGoal,
  serializeGoalEntries,
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
  // The mastery-costs page links each row here (masteries.js's masteryHref).
  test("reads the goal a mastery-costs row links with", () => {
    expect(parseSeededGoal("?mastery=Compass%3AGM")).toEqual({
      item: "Compass",
      amount: "GM",
    });
  });

  test("splits on the last ':' so a colon-bearing item name survives", () => {
    expect(parseSeededGoal("?mastery=Time%3A%20A%20Gift%3AM")).toEqual({
      item: "Time: A Gift",
      amount: "M",
    });
  });

  test("ignores other query strings, and an incomplete goal", () => {
    expect(parseSeededGoal("")).toBeNull();
    expect(parseSeededGoal("?plan=3")).toBeNull();
    expect(parseSeededGoal("?mastery=Compass")).toBeNull(); // no amount
    expect(parseSeededGoal("?mastery=")).toBeNull();
  });
});
