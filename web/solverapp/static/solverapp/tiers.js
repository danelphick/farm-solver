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

// Sort order for the Target column: the tiers rank by how much work they are
// (M < GM < MM), never alphabetically. A token that isn't one of the three (a
// plain amount) ranks after all of them rather than throwing off the order.
const TIER_RANKS = { M: 0, GM: 1, MM: 2 };

export function tierRank(tier) {
  const rank = TIER_RANKS[String(tier).toUpperCase()];
  return rank === undefined ? TIER_RANKS.MM + 1 : rank;
}

// The tier an absolute progress figure has already reached: "MM"/"GM"/"M", or
// "" below Master. The thresholds themselves live in history.TIER_THRESHOLDS
// (the timeline chart draws them as reference lines); this is the lookup the
// timeline's Tier column wants, and it belongs beside the other tier knowledge.
export function tierForProgress(progress) {
  if (progress >= 1_000_000) return "MM";
  if (progress >= 100_000) return "GM";
  if (progress >= 10_000) return "M";
  return "";
}
