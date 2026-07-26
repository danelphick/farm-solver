// Pure helpers of the shared settings popover (settings.js), which owns every
// parameter except the solver page's Goal. The material-trading pair below is
// the pane's read/restore logic; mergeSettingsIntoState is how a page folds a
// popover Save back into its saved state.

import { describe, expect, test } from "vitest";
import {
  computeApTradingParam,
  computeDefaultAllowed,
  mergeSettingsIntoState,
} from "./settings.js";

describe("computeDefaultAllowed", () => {
  const items = ["acorn", "apple", "oak", "red dye"];

  test("no allow/deny with enabled means everything is tradeable", () => {
    expect(computeDefaultAllowed({ enabled: true }, items)).toEqual(new Set(items));
  });

  test("enabled: false means nothing is tradeable", () => {
    expect(computeDefaultAllowed({ enabled: false }, items)).toEqual(new Set());
  });

  test("an allow list keeps only the listed items (underscores normalise to spaces)", () => {
    expect(computeDefaultAllowed({ allow: ["acorn", "red_dye"] }, items)).toEqual(
      new Set(["acorn", "red dye"])
    );
  });

  test("a deny list drops the listed items", () => {
    expect(computeDefaultAllowed({ deny: ["oak", "apple"] }, items)).toEqual(
      new Set(["acorn", "red dye"])
    );
  });
});

describe("computeApTradingParam", () => {
  const defaultAllowed = new Set(["acorn", "apple", "oak"]);
  const priceOf = (item) => ({ acorn: 6, apple: 15, oak: 2 }[item]);

  test("matching the default allow set with no edits sends nothing", () => {
    expect(
      computeApTradingParam(["acorn", "apple", "oak"], {}, defaultAllowed, priceOf)
    ).toBeNull();
  });

  test("a narrowed allow list sends the allow list and no prices", () => {
    expect(
      computeApTradingParam(["acorn", "apple"], {}, defaultAllowed, priceOf)
    ).toEqual({ allow: ["acorn", "apple"] });
  });

  test("an edited price sends every allowed item's price (edited or default)", () => {
    expect(
      computeApTradingParam(["acorn", "apple"], { acorn: 9 }, defaultAllowed, priceOf)
    ).toEqual({ allow: ["acorn", "apple"], prices: { acorn: 9, apple: 15 } });
  });

  test("an edit alone (same allow set) still deviates and is sent", () => {
    expect(
      computeApTradingParam(["acorn", "apple", "oak"], { oak: 3 }, defaultAllowed, priceOf)
    ).toEqual({
      allow: ["acorn", "apple", "oak"],
      prices: { acorn: 6, apple: 15, oak: 3 },
    });
  });
});

describe("mergeSettingsIntoState", () => {
  const SAVED = {
    mastery_text: "old masteries",
    inventory_text: "old inventory",
    params: {
      target: "tower",
      mastery: "Gold Bar:5000",
      plan_select: 2,
      max_gold: "500",
      cookie_sets: 3,
    },
  };
  const PANEL = {
    mastery_text: "new masteries",
    inventory_text: "new inventory",
    params: { max_gold: "unlimited", calendar: "ignore" },
  };

  test("the popover's values replace the saved ones", () => {
    const merged = mergeSettingsIntoState(SAVED, PANEL);
    expect(merged.mastery_text).toBe("new masteries");
    expect(merged.inventory_text).toBe("new inventory");
    expect(merged.params.max_gold).toBe("unlimited");
    expect(merged.params.calendar).toBe("ignore");
  });

  test("the goal params the popover does not show survive", () => {
    const merged = mergeSettingsIntoState(SAVED, PANEL);
    expect(merged.params.target).toBe("tower");
    expect(merged.params.mastery).toBe("Gold Bar:5000");
    expect(merged.params.plan_select).toBe(2);
  });

  test("a setting blanked in the popover is dropped, not left stale", () => {
    // readSettingsParams omits a blank field entirely, so merging must not
    // fall back to the old value -- cookie_sets was 3 and is now unset.
    const merged = mergeSettingsIntoState(SAVED, PANEL);
    expect("cookie_sets" in merged.params).toBe(false);
  });

  test("no saved state at all is just the popover's own values", () => {
    expect(mergeSettingsIntoState(null, PANEL)).toEqual(PANEL);
  });
});
