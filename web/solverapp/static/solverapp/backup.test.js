// The backup file format (backup.js): what one holds, and -- the half that
// matters -- what parseBackup refuses. A restore overwrites the state the user
// has, so every rejection here is a file that must never get as far as writing.

import { describe, expect, test } from "vitest";

import {
  BACKUP_FORMAT,
  BACKUP_VERSION,
  backupFilename,
  buildBackup,
  byteLength,
  formatBytes,
  parseBackup,
} from "./backup.js";

const STATE = {
  mastery_text: "Tomato\n992,332 / 1,000,000 Progress\n",
  inventory_text: "Items chevron_down",
  params: { mastery: "Tomato:MM", max_gold: "500" },
};

const SNAPSHOT = {
  taken_at: "2026-07-28T20:03:11.482Z",
  date: "2026-07-28",
  items: { Tomato: 992332 },
};

function backupText(overrides = {}) {
  return JSON.stringify({
    ...buildBackup({ state: STATE, ui: { panes: { "paste-details": true } }, snapshots: [SNAPSHOT] }),
    ...overrides,
  });
}

describe("buildBackup", () => {
  test("carries the state, the preferences and the whole history", () => {
    const backup = buildBackup({
      state: STATE,
      ui: { panes: {} },
      snapshots: [SNAPSHOT],
      when: new Date("2026-07-31T09:00:00.000Z"),
    });
    expect(backup).toEqual({
      format: BACKUP_FORMAT,
      version: BACKUP_VERSION,
      exported_at: "2026-07-31T09:00:00.000Z",
      state: STATE,
      ui: { panes: {} },
      snapshots: [SNAPSHOT],
    });
  });

  test("an empty browser still produces a valid backup", () => {
    expect(parseBackup(JSON.stringify(buildBackup()))).toEqual({
      state: {},
      ui: {},
      snapshots: [],
      exportedAt: expect.any(String),
    });
  });
});

describe("backupFilename", () => {
  test("is dated by the local day, like a snapshot", () => {
    expect(backupFilename(new Date(2026, 6, 31, 23, 30))).toBe(
      "farm-solver-backup-2026-07-31.json"
    );
  });
});

describe("parseBackup", () => {
  test("round-trips what buildBackup wrote", () => {
    const restored = parseBackup(backupText());
    expect(restored.state).toEqual(STATE);
    expect(restored.ui).toEqual({ panes: { "paste-details": true } });
    expect(restored.snapshots).toEqual([SNAPSHOT]);
  });

  test("rejects a file that is not JSON at all", () => {
    expect(() => parseBackup("<html>nope</html>")).toThrow(/not JSON/);
  });

  test("rejects JSON that is not one of ours", () => {
    expect(() => parseBackup('{"hello": "world"}')).toThrow(/not a farm-solver backup/);
    expect(() => parseBackup("[1, 2, 3]")).toThrow(/not a farm-solver backup/);
  });

  test("rejects a backup written by a newer version", () => {
    expect(() => parseBackup(backupText({ version: BACKUP_VERSION + 1 }))).toThrow(
      /can only read version/
    );
  });

  test("rejects a mangled state, history or preference block", () => {
    expect(() => parseBackup(backupText({ state: "text" }))).toThrow(/saved state/);
    expect(() => parseBackup(backupText({ ui: [] }))).toThrow(/preferences/);
    expect(() => parseBackup(backupText({ snapshots: {} }))).toThrow(/history is not a list/);
  });

  // Each of these would poison the history rather than fail loudly: a snapshot
  // with no timestamp has no key, and a non-numeric progress plots as a hole.
  test("rejects an unusable snapshot, naming which one", () => {
    expect(() => parseBackup(backupText({ snapshots: [SNAPSHOT, { items: {} }] }))).toThrow(
      /snapshot 2 has no taken_at/
    );
    expect(() => parseBackup(backupText({ snapshots: [{ ...SNAPSHOT, taken_at: "soon" }] })))
      .toThrow(/unreadable taken_at/);
    expect(() => parseBackup(backupText({ snapshots: [{ taken_at: SNAPSHOT.taken_at }] })))
      .toThrow(/no items/);
    expect(() =>
      parseBackup(backupText({ snapshots: [{ ...SNAPSHOT, items: { Tomato: "lots" } }] }))
    ).toThrow(/non-numeric progress for Tomato/);
  });

  // The local day cannot be recovered from a UTC timestamp, so a hand-edited
  // file missing it is filed under the RESTORING browser's day instead.
  test("derives a missing date from the timestamp", () => {
    const [restored] = parseBackup(
      backupText({ snapshots: [{ taken_at: SNAPSHOT.taken_at, items: { Tomato: 1 } }] })
    ).snapshots;
    expect(restored.date).toBe(
      new Date(SNAPSHOT.taken_at).getFullYear() +
        "-" +
        String(new Date(SNAPSHOT.taken_at).getMonth() + 1).padStart(2, "0") +
        "-" +
        String(new Date(SNAPSHOT.taken_at).getDate()).padStart(2, "0")
    );
  });
});

describe("formatBytes", () => {
  test("scales to the size of the file", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024 * 312)).toBe("312 KB");
    expect(formatBytes(1024 * 1024 * 2.5)).toBe("2.5 MB");
  });

  test("byteLength counts bytes, not code units", () => {
    expect(byteLength("abc")).toBe(3);
    expect(byteLength("∞")).toBe(3);
  });
});
