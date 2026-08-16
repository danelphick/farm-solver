// Unit tests for the mastery-history module: the paste parser (the JS mirror of
// parsing.parse_masteries), the local-day keys, the retention rule, and the two
// series shapes the timeline page reads. The store itself is exercised against
// fake-indexeddb -- jsdom ships no IndexedDB.

import { describe, expect, test, vi } from "vitest";
import { IDBFactory } from "fake-indexeddb";

import {
  changeSince,
  keysToPrune,
  localDateKey,
  parseMasteryText,
  seriesByItem,
  seriesFor,
} from "./history.js";

// A paste in the exact shape FarmRPG copies out (see mydata/mastery-*.txt):
// the header block, in-progress items with a "n / total Progress" line and a
// percentage line, a block carrying a trailing tier header, and a fully
// mastered item whose denominator is the infinity glyph.
const PASTE = `MASTERY IN-PROGRESSCopy Mastery Data
Stop Tracking All
Tier V (MM) chevron_down

Tomato
992,332 / 1,000,000 Progress
99.2332% Stop

Hops
965,900 / 1,000,000 Progress
96.59% Track
Tier IV (GM) chevron_down

Oak Table
1,000,333 / ∞ Progress
Complete!
`;

describe("parseMasteryText", () => {
  test("reads every block, skipping the header", () => {
    expect(parseMasteryText(PASTE)).toEqual({
      Tomato: 992332,
      Hops: 965900,
      "Oak Table": 1000333,
    });
  });

  test("empty text has no items", () => {
    expect(parseMasteryText("")).toEqual({});
    expect(parseMasteryText("\n\n  \n")).toEqual({});
  });

  // Same contract as parsing._parse_mastery_block, which raises: a paste this
  // cannot read is one /api/solve would 422 on, and a snapshot that quietly
  // dropped half the items would poison the history for good.
  test("throws on a block with no progress line", () => {
    expect(() => parseMasteryText("Tomato\nnot a progress line\n")).toThrow(
      /could not parse progress line/
    );
  });

  test("throws on a block that is only a name", () => {
    expect(() => parseMasteryText("Tomato\n")).toThrow(/too short/);
  });
});

describe("localDateKey", () => {
  // The point of the helper: a late-evening paste belongs to the day the user
  // was playing on, which toISOString().slice(0, 10) gets wrong east of UTC.
  test("uses the local calendar day, not the UTC one", () => {
    expect(localDateKey(new Date(2026, 6, 28, 23, 30))).toBe("2026-07-28");
    expect(localDateKey(new Date(2026, 0, 1, 0, 5))).toBe("2026-01-01");
  });
});

describe("keysToPrune", () => {
  const at = (date, hour) => ({ date, taken_at: `${date}T${String(hour).padStart(2, "0")}:00:00.000Z` });

  test("keeps a history that already holds one snapshot a day", () => {
    const entries = [at("2026-07-28", 9), at("2026-07-27", 21), at("2026-07-22", 8)];
    expect(keysToPrune(entries)).toEqual([]);
  });

  test("thins each day to its last snapshot", () => {
    const entries = [
      at("2026-07-10", 8),
      at("2026-07-10", 12),
      at("2026-07-10", 20),
      at("2026-07-11", 9),
      at("2026-07-28", 9),
    ];
    expect(keysToPrune(entries)).toEqual([
      at("2026-07-10", 8).taken_at,
      at("2026-07-10", 12).taken_at,
    ]);
  });

  // The rule has no window, so the newest day is thinned like any other: a
  // second save today supersedes this morning's rather than joining it.
  test("thins today too", () => {
    const entries = [at("2026-07-28", 9), at("2026-07-28", 21)];
    expect(keysToPrune(entries)).toEqual([at("2026-07-28", 9).taken_at]);
  });

  test("is order-independent", () => {
    const entries = [at("2026-07-10", 20), at("2026-07-10", 8)];
    expect(keysToPrune(entries)).toEqual([at("2026-07-10", 8).taken_at]);
  });

  test("nothing to do when there is no history at all", () => {
    expect(keysToPrune([])).toEqual([]);
  });
});

const snapshot = (date, items, hour = 12) => ({
  taken_at: `${date}T${String(hour).padStart(2, "0")}:00:00.000Z`,
  date,
  items,
});

describe("seriesFor", () => {
  const SNAPSHOTS = [
    snapshot("2026-07-01", { Tomato: 100, Hops: 5 }),
    snapshot("2026-07-08", { Hops: 9 }), // Tomato missing: a gap, not a zero
    snapshot("2026-07-15", { tomato: 300, Hops: 11 }),
  ];

  test("skips snapshots that do not mention the item", () => {
    expect(seriesFor(SNAPSHOTS, "Tomato").map((p) => [p.date, p.progress])).toEqual([
      ["2026-07-01", 100],
      ["2026-07-15", 300],
    ]);
  });

  test("matches the item case-insensitively", () => {
    expect(seriesFor(SNAPSHOTS, "tOmAtO")).toHaveLength(2);
    expect(seriesFor(SNAPSHOTS, "  Hops ")).toHaveLength(3);
  });

  test("orders oldest first whatever order the store returned", () => {
    const reversed = [...SNAPSHOTS].reverse();
    expect(seriesFor(reversed, "Hops").map((p) => p.progress)).toEqual([5, 9, 11]);
  });

  test("an unknown item (or none) has no series", () => {
    expect(seriesFor(SNAPSHOTS, "Nonesuch")).toEqual([]);
    expect(seriesFor(SNAPSHOTS, "")).toEqual([]);
  });
});

describe("seriesByItem", () => {
  test("gives every item its own series, in item-name order", () => {
    const entries = seriesByItem([
      snapshot("2026-07-01", { Tomato: 100, Hops: 5 }),
      snapshot("2026-07-15", { Tomato: 300 }),
    ]);
    expect(entries.map((entry) => entry.item)).toEqual(["Hops", "Tomato"]);
    expect(entries[1].points.map((point) => [point.date, point.progress])).toEqual([
      ["2026-07-01", 100],
      ["2026-07-15", 300],
    ]);
    // An item missing from a snapshot simply has no point for it.
    expect(entries[0].points).toHaveLength(1);
  });

  test("agrees with seriesFor, which reads one item out of the same snapshots", () => {
    const snapshots = [
      snapshot("2026-07-01", { Tomato: 100, Hops: 5 }),
      snapshot("2026-07-15", { Tomato: 300 }),
    ];
    const tomato = seriesByItem(snapshots).find((entry) => entry.item === "Tomato");
    expect(tomato.points).toEqual(seriesFor(snapshots, "Tomato"));
  });

  // Grouped case-insensitively, showing whatever the newest paste calls it.
  test("groups a respelt name and displays the newest spelling", () => {
    const entries = seriesByItem([
      snapshot("2026-07-01", { tomato: 100 }),
      snapshot("2026-07-15", { Tomato: 300 }),
    ]);
    expect(entries).toHaveLength(1);
    expect(entries[0].item).toBe("Tomato");
    expect(entries[0].points.map((point) => point.progress)).toEqual([100, 300]);
  });
});

describe("changeSince", () => {
  const points = seriesFor(
    [
      snapshot("2026-07-01", { Tomato: 400_000 }),
      snapshot("2026-07-24", { Tomato: 404_800 }),
      snapshot("2026-07-28", { Tomato: 412_900 }),
    ],
    "Tomato"
  );

  test("measures from where the item stood when the window opened", () => {
    // Window opens 2026-07-20: the 07-24 reading is the first inside it, so the
    // baseline is 07-01, the newest reading before it.
    expect(changeSince(points, "2026-07-20T00:00:00.000Z")).toBe(12_900);
    expect(changeSince(points, "2026-07-26T00:00:00.000Z")).toBe(8_100);
  });

  // A partial window is still the change on record for the period; it is the
  // windows it cannot answer at all that must not read as zero.
  test("measures from the oldest reading when the history starts inside the window", () => {
    expect(changeSince(points, "2026-06-01T00:00:00.000Z")).toBe(12_900);
  });

  test("is null when nothing was recorded inside the window", () => {
    expect(changeSince(points, "2026-08-01T00:00:00.000Z")).toBeNull();
  });

  test("is null when there is no earlier reading to measure from", () => {
    expect(changeSince(points.slice(-1), "2026-07-26T00:00:00.000Z")).toBeNull();
    expect(changeSince([], "2026-07-26T00:00:00.000Z")).toBeNull();
  });

  test("reports a fall as a negative", () => {
    const falling = seriesFor(
      [
        snapshot("2026-07-24", { Tomato: 500 }),
        snapshot("2026-07-28", { Tomato: 400 }),
      ],
      "Tomato"
    );
    expect(changeSince(falling, "2026-07-26T00:00:00.000Z")).toBe(-100);
  });
});

// --- The store ---------------------------------------------------------------

const SNAPSHOT_RECORD = {
  taken_at: "2026-07-28T20:03:11.482Z",
  date: "2026-07-28",
  items: { Tomato: 992332 },
};

// A fresh database and a fresh module instance per test: history.js memoises
// its open() in module state, so the two have to be reset together.
async function freshHistory() {
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.resetModules();
  return import("./history.js");
}

describe("the snapshot store", () => {
  test("round-trips a snapshot", async () => {
    const history = await freshHistory();
    const record = await history.putSnapshot({ Tomato: 100 }, new Date(2026, 6, 28, 21, 3));
    expect(record.date).toBe("2026-07-28");

    const stored = await history.getSnapshots();
    expect(stored).toHaveLength(1);
    expect(stored[0].items).toEqual({ Tomato: 100 });
  });

  // One point per day: the evening's save replaces the morning's, rather than
  // recording a second reading for the same date.
  test("a second save on a day supersedes the first", async () => {
    const history = await freshHistory();
    await history.putSnapshot({ Tomato: 100 }, new Date(2026, 6, 28, 9, 0));
    await history.putSnapshot({ Tomato: 140 }, new Date(2026, 6, 28, 21, 0));

    const stored = await history.getSnapshots();
    expect(stored.map((s) => s.items.Tomato)).toEqual([140]);
  });

  test("returns snapshots oldest first", async () => {
    const history = await freshHistory();
    await history.putSnapshot({ Tomato: 140 }, new Date(2026, 6, 28, 9, 0));
    await history.putSnapshot({ Tomato: 100 }, new Date(2026, 6, 20, 9, 0));

    expect((await history.getSnapshots()).map((s) => s.date)).toEqual([
      "2026-07-20",
      "2026-07-28",
    ]);
  });

  // The retention rule, end to end: the prune runs inside the saving
  // transaction, so the store holds one snapshot a day at every point along the
  // way -- never a backlog waiting for some later save to thin it.
  test("a save leaves one snapshot per day", async () => {
    const history = await freshHistory();
    await history.putSnapshot({ Tomato: 10 }, new Date(2026, 6, 1, 8, 0));
    await history.putSnapshot({ Tomato: 20 }, new Date(2026, 6, 1, 20, 0));
    expect(await history.getSnapshots()).toHaveLength(1);

    await history.putSnapshot({ Tomato: 40 }, new Date(2026, 6, 28, 8, 0));

    const stored = await history.getSnapshots();
    expect(stored.map((s) => s.items.Tomato)).toEqual([20, 40]);
  });

  // Restoring a backup (see backup.js) merges into whatever this browser
  // already has, and leaves the store obeying the same retention rule.
  test("putSnapshots merges restored records without dropping local ones", async () => {
    const history = await freshHistory();
    await history.putSnapshot({ Tomato: 40 }, new Date(2026, 6, 28, 8, 0));

    const written = await history.putSnapshots(
      [
        { taken_at: new Date(2026, 6, 27, 8, 0).toISOString(), date: "2026-07-27", items: { Tomato: 30 } },
        { taken_at: new Date(2026, 6, 28, 8, 0).toISOString(), date: "2026-07-28", items: { Tomato: 44 } },
      ]
    );

    expect(written).toBe(2);
    const stored = await history.getSnapshots();
    // The local 2026-07-27 gap is filled, and the same-instant record is
    // replaced by the restored one rather than duplicated.
    expect(stored.map((s) => [s.date, s.items.Tomato])).toEqual([
      ["2026-07-27", 30],
      ["2026-07-28", 44],
    ]);
  });

  test("putSnapshots prunes the days a restore brings in", async () => {
    const history = await freshHistory();
    await history.putSnapshots(
      [
        { taken_at: new Date(2026, 6, 1, 8, 0).toISOString(), date: "2026-07-01", items: { Tomato: 10 } },
        { taken_at: new Date(2026, 6, 1, 20, 0).toISOString(), date: "2026-07-01", items: { Tomato: 20 } },
        { taken_at: new Date(2026, 6, 28, 8, 0).toISOString(), date: "2026-07-28", items: { Tomato: 40 } },
      ]
    );

    expect((await history.getSnapshots()).map((s) => s.items.Tomato)).toEqual([20, 40]);
  });

  test("deleteSnapshot removes one record", async () => {
    const history = await freshHistory();
    const record = await history.putSnapshot({ Tomato: 100 }, new Date(2026, 6, 28, 9, 0));
    await history.deleteSnapshot(record.taken_at);
    expect(await history.getSnapshots()).toEqual([]);
  });

  // A browser with no IndexedDB (a locked-down private mode, and jsdom itself)
  // must degrade to "no history" rather than throwing into the page.
  test("degrades to no history when IndexedDB is missing", async () => {
    vi.stubGlobal("indexedDB", undefined);
    vi.resetModules();
    const history = await import("./history.js");
    expect(await history.openHistoryDb()).toBeNull();
    expect(await history.putSnapshot({ Tomato: 1 })).toBeNull();
    expect(await history.putSnapshots([SNAPSHOT_RECORD])).toBe(0);
    expect(await history.getSnapshots()).toEqual([]);
  });
});
