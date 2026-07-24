// The three mastery tiers, shared by both pages: the mastery-costs table's
// Target column and the solver page's seeded-goal banner both spell out the
// short token a "Item:TIER" goal carries. Mirrors main._MASTERY_TIERS /
// parsing._MASTERY_TOKENS, where each token is the absolute progress threshold.

const TIER_NAMES = { M: "Master", GM: "Grandmaster", MM: "Megamaster" };

// "GM" -> "Grandmaster"; anything else (a plain amount like "50000") is
// returned unchanged, since a goal's amount is free text.
export function tierLabel(tier) {
  return TIER_NAMES[String(tier).toUpperCase()] || tier;
}
