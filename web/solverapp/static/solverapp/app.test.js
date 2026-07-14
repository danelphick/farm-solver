import { describe, expect, test } from "vitest";
import { parseGoalSpec, serializeGoalEntries } from "./app.js";

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
