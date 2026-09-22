// Mastery history: dated snapshots of the pasted mastery list, kept in this
// browser's IndexedDB and read back by the timeline page.
//
// Registers nothing at module scope (like settings.js/help.js/tiers.js, unlike
// app.js), so any page may import it.
//
// Why IndexedDB rather than the localStorage the rest of the app uses: a single
// snapshot of a well-progressed save is ~15 KB of JSON and history accumulates,
// which would eat the whole ~5 MB localStorage budget the solver state lives in.
// Nothing here ever reaches the server -- history is per-browser, and is neither
// exported nor backed up.
//
// Retention: one snapshot a day, every day -- each day is thinned to its last
// snapshot, so saving again the same evening replaces the morning's rather than
// recording a second point. The prune runs inside each save's transaction --
// the one moment history grows.

// Bumped only for a schema change (a new store/index); the upgrade handler
// creates whatever a given version needs.
const DB_NAME = "farm-solver-history";
const DB_VERSION = 1;
const STORE = "mastery_snapshots";
const DATE_INDEX = "date";

// The three mastery tiers as absolute progress thresholds (mirrors
// main._MASTERY_TIERS; tiers.js owns the labels and the tier-of-a-progress
// lookup). Exported for the timeline chart's reference lines.
export const TIER_THRESHOLDS = [
  { tier: "M", progress: 10_000 },
  { tier: "GM", progress: 100_000 },
  { tier: "MM", progress: 1_000_000 },
];

// --- Parsing the paste ------------------------------------------------------

// Mirrors parsing._PROGRESS_RE: "976,311 / 1,000,000 Progress", or the fully
// mastered "832,681,556 / ∞ Progress". Only the numerator is kept -- the
// denominator is the next tier threshold, which is a constant (TIER_THRESHOLDS),
// so storing it per item per day would be bytes for nothing.
const PROGRESS_RE = /^([\d,]+)\s*\/\s*([\d,]+|∞)\s+Progress$/;

// Mirrors parsing._split_blocks: blank-line separated runs of non-blank lines.
export function splitBlocks(text) {
  const blocks = [];
  let current = [];
  for (const raw of String(text).split("\n")) {
    const line = raw.trim();
    if (line) {
      current.push(line);
    } else if (current.length) {
      blocks.push(current);
      current = [];
    }
  }
  if (current.length) blocks.push(current);
  return blocks;
}

// A parsed paste: `{"Tomato": 992332, ...}`, keyed by the item name exactly as
// pasted (see the module note on lookups being case-insensitive anyway).
//
// The JS mirror of parsing.parse_masteries / _parse_mastery_block -- the same
// paste this page POSTs to /api/solve, parsed here so saving a snapshot and
// drawing the timeline need no server round trip at all. Like the Python it
// throws on a block it cannot read rather than silently dropping items: a paste
// this can't read is one the solver would 422 on, and a snapshot that quietly
// lost half your masteries would poison the history for good.
export function parseMasteryText(text) {
  const items = {};
  for (const block of splitBlocks(text)) {
    // The header block ("MASTERY IN-PROGRESS...", the tracking buttons and the
    // tier chevron) has no progress line; skip it rather than reading it as an
    // item, exactly as _parse_mastery_block does.
    if (block[0].toLowerCase().startsWith("mastery in-progress")) continue;
    if (block.length < 2) {
      throw new Error(`mastery block too short: ${block.join(" / ")}`);
    }
    const match = PROGRESS_RE.exec(block[1]);
    if (!match) {
      throw new Error(`could not parse progress line: ${block[1]}`);
    }
    items[block[0]] = Number(match[1].replace(/,/g, ""));
  }
  return items;
}

// --- Dates ------------------------------------------------------------------

// The LOCAL calendar day, "YYYY-MM-DD". Deliberately not
// `toISOString().slice(0, 10)`, which files an evening paste under tomorrow for
// anyone east of UTC (and a morning one under yesterday for anyone west of it) --
// the day a snapshot belongs to is the day the user was playing on.
export function localDateKey(when = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

// --- Retention --------------------------------------------------------------

// Which snapshots to delete, given `[{taken_at, date}]` for everything stored.
//
// Every day keeps only its LAST snapshot -- the most recent state of that day,
// and the one whose gains cover the whole day. Today included: a second save
// this evening supersedes this morning's rather than sitting beside it, so the
// history is exactly one point per day however often the button is pressed.
//
// Pure, so the rule is unit-tested directly (history.test.js) rather than
// through a database.
export function keysToPrune(entries) {
  const lastOfDay = new Map();
  const stale = [];
  // Sorted rather than trusting the caller's order (the index cursor already
  // yields date order, but the pure function shouldn't depend on that).
  const sorted = [...entries].sort((a, b) =>
    a.date === b.date
      ? String(a.taken_at).localeCompare(String(b.taken_at))
      : String(a.date).localeCompare(String(b.date))
  );
  for (const entry of sorted) {
    const previous = lastOfDay.get(entry.date);
    if (previous !== undefined) stale.push(previous);
    lastOfDay.set(entry.date, entry.taken_at);
  }
  return stale;
}

// --- Series (what the timeline page plots) ----------------------------------

// One snapshot's progress for `item`, or undefined when it holds no such item.
// Exact key first (the common case, one lookup); otherwise a case-insensitive
// scan, so a paste whose capitalisation changed still lines up.
function progressOf(items, wantedLower, wanted) {
  if (items && typeof items[wanted] === "number") return items[wanted];
  for (const [name, progress] of Object.entries(items || {})) {
    if (name.toLowerCase() === wantedLower) return progress;
  }
  return undefined;
}

// One item's progress over time, oldest first.
//
// A snapshot that doesn't mention the item is SKIPPED, never plotted as zero: a
// mastery paste lists what you have started (fully mastered items stay on it,
// as "N / ∞"), so an absence means "not started yet" or "the paste was
// truncated" -- both of which a 0 point would draw as a cliff that never
// happened.
export function seriesFor(snapshots, item) {
  const exact = String(item || "").trim();
  const wanted = exact.toLowerCase();
  if (!wanted) return [];
  return snapshots
    .map((snapshot) => ({
      taken_at: snapshot.taken_at,
      date: snapshot.date,
      progress: progressOf(snapshot.items, wanted, exact),
    }))
    .filter((point) => typeof point.progress === "number")
    .sort((a, b) => String(a.taken_at).localeCompare(String(b.taken_at)));
}

// Every item any snapshot mentions, as `[{item, points}]` in item-name order --
// `seriesFor` for all of them at once, in one pass, for the timeline page's
// no-item index (which needs each item's whole series to work out what changed
// inside a window, not just its endpoints).
//
// Grouped case-insensitively (like parsing.parse_masteries' lower-cased keys),
// displaying the NEWEST snapshot's spelling -- if the game ever restyles a name,
// the list should show what it is called now.
export function seriesByItem(snapshots) {
  const ordered = [...snapshots].sort((a, b) =>
    String(a.taken_at).localeCompare(String(b.taken_at))
  );
  const byItem = new Map();
  for (const snapshot of ordered) {
    for (const [name, progress] of Object.entries(snapshot.items || {})) {
      const key = name.toLowerCase();
      const point = { taken_at: snapshot.taken_at, date: snapshot.date, progress };
      const entry = byItem.get(key);
      if (entry === undefined) {
        byItem.set(key, { item: name, points: [point] });
      } else {
        entry.item = name;
        entry.points.push(point);
      }
    }
  }
  return [...byItem.values()].sort((a, b) => a.item.localeCompare(b.item));
}

// How much an item gained over the window starting at `cutoff` (an ISO
// timestamp): its newest progress minus what it stood at when the window
// opened. `points` is one item's series, oldest first.
//
// `null` -- shown as "—" rather than a misleading 0 -- when the window cannot
// be answered at all: nothing recorded since it opened (a history that stopped
// before it), or no earlier reading to measure from (the item's first ever
// snapshot falls inside it). A history that merely *starts* inside the window
// does measure from its oldest reading: that is all the change there is on
// record for the period, and it is the figure the user is asking about.
export function changeSince(points, cutoff) {
  if (!points.length) return null;
  const latest = points[points.length - 1];
  if (latest.taken_at <= cutoff) return null;

  let baseline = points[0];
  for (const point of points) {
    if (point.taken_at > cutoff) break;
    baseline = point;
  }
  return baseline === latest ? null : latest.progress - baseline.progress;
}

// --- The store --------------------------------------------------------------

// Promise wrapper for one IDBRequest.
function requestDone(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

// Promise wrapper for a transaction's completion, so a write is only reported
// saved once it has actually committed.
function transactionDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
  });
}

let dbPromise = null;

// The history database, or null when this browser has no IndexedDB at all
// (jsdom in the page tests, a locked-down private mode). Callers treat null the
// way readUi() treats corrupt localStorage: no history, no error -- persistence
// here is a feature, not something the page depends on to work.
export function openHistoryDb() {
  if (typeof indexedDB === "undefined" || !indexedDB) return Promise.resolve(null);
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          // Keyed by the ISO timestamp: it sorts chronologically, so a plain
          // cursor over the store is already the time series, and every record
          // keeps the instant it was taken at (what the delta table divides a
          // gain by). A day's repeat saves are separate records until the prune
          // picks the one to keep, rather than a date key silently overwriting.
          const store = db.createObjectStore(STORE, { keyPath: "taken_at" });
          // The local calendar day is stored, not derived at read time (it
          // cannot be recovered from the timestamp once the user travels), and
          // indexed so the prune walks days with openKeyCursor -- no record
          // bodies loaded to decide what to delete.
          store.createIndex(DATE_INDEX, "date", { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("history database is blocked"));
    }).catch((err) => {
      dbPromise = null; // a failed open shouldn't poison every later attempt
      throw err;
    });
  }
  return dbPromise;
}

// Delete the stale snapshots, from inside the caller's readwrite transaction.
//
// Every delete is issued synchronously from the cursor's final onsuccess, so
// there is no await between reading the keys and using them -- an intervening
// microtask turn would let the transaction auto-commit out from under us.
function pruneSnapshots(store) {
  return new Promise((resolve, reject) => {
    const entries = [];
    const request = store.index(DATE_INDEX).openKeyCursor();
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor) {
        entries.push({ date: cursor.key, taken_at: cursor.primaryKey });
        cursor.continue();
        return;
      }
      const pruned = keysToPrune(entries);
      pruned.forEach((key) => store.delete(key));
      resolve(pruned.length);
    };
  });
}

// Record `items` as a snapshot and prune in the same transaction. Returns the
// stored record (null when there is no IndexedDB), so callers can report the
// date and item count they just saved.
//
// `when` is a parameter rather than always `new Date()` so history can be
// back-filled from older pastes (and so the tests don't have to travel).
export async function putSnapshot(items, when = new Date()) {
  const db = await openHistoryDb();
  if (!db) return null;
  const record = {
    taken_at: when.toISOString(),
    date: localDateKey(when),
    items,
  };
  const tx = db.transaction(STORE, "readwrite");
  const store = tx.objectStore(STORE);
  store.put(record);
  await pruneSnapshots(store);
  await transactionDone(tx);
  return record;
}

// Merge restored snapshots (from a backup file -- see backup.js) into the
// history, then prune to the usual retention rule. Returns how many records
// were written.
//
// A MERGE, not a replace: restoring on a browser that has its own history must
// not throw that history away. Records collide only on an identical `taken_at`,
// where the incoming one wins (it is the same snapshot, taken at the same
// instant). The prune runs afterwards in the same transaction, so a backup
// holding several snapshots of a day collapses to one the way a save would.
export async function putSnapshots(records) {
  const db = await openHistoryDb();
  if (!db) return 0;
  const tx = db.transaction(STORE, "readwrite");
  const store = tx.objectStore(STORE);
  records.forEach((record) => store.put(record));
  await pruneSnapshots(store);
  await transactionDone(tx);
  return records.length;
}

// Every snapshot, oldest first (key order is timestamp order).
export async function getSnapshots() {
  const db = await openHistoryDb();
  if (!db) return [];
  const tx = db.transaction(STORE, "readonly");
  const snapshots = await requestDone(tx.objectStore(STORE).getAll());
  await transactionDone(tx);
  return snapshots;
}

export async function deleteSnapshot(takenAt) {
  const db = await openHistoryDb();
  if (!db) return;
  const tx = db.transaction(STORE, "readwrite");
  tx.objectStore(STORE).delete(takenAt);
  await transactionDone(tx);
}
