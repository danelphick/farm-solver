// Backup and restore: everything this browser holds for you, as one JSON file.
//
// The app deliberately stores nothing server-side -- the pasted game state and
// every option live in localStorage, the mastery history in IndexedDB (see
// history.js) -- which means a new browser, a cleared profile or a different
// machine starts empty and there is nothing to log in to. This module is the
// escape hatch: one file you can carry across, holding
//
//   state     the saved solver state (mastery/inventory paste, goal, params)
//   ui        the UI-only preferences (collapsed panes, table sorts)
//   snapshots every mastery snapshot in this browser's history
//
// Registers nothing at module scope, and touches no DOM beyond the download
// anchor -- the file dialog, the buttons and the storage reads/writes are
// settings.js's (which owns the storage keys). Everything that decides what a
// backup *is* -- building it, validating one, naming the file -- is pure and
// unit-tested in backup.test.js, because a restore is destructive: an accepted
// file overwrites the state the user has, so a malformed one must be rejected
// before anything is written, not halfway through.

import { localDateKey } from "./history.js";

// Stamped into every file and checked on restore, so a JSON file that happens
// to be lying in the Downloads folder cannot be mistaken for a backup.
export const BACKUP_FORMAT = "farm-solver-backup";
// Bumped only for a shape change. A file from a NEWER version is refused (this
// app cannot know what it means); an older one is read as far as it goes, which
// is what makes adding a key later a non-event.
export const BACKUP_VERSION = 1;

export function buildBackup({ state, ui, snapshots, when = new Date() } = {}) {
  return {
    format: BACKUP_FORMAT,
    version: BACKUP_VERSION,
    exported_at: when.toISOString(),
    state: state || {},
    ui: ui || {},
    snapshots: snapshots || [],
  };
}

// "farm-solver-backup-2026-07-31.json" -- the local day, like a snapshot's own
// date key, so files sort by the day you actually made them.
export function backupFilename(when = new Date()) {
  return `farm-solver-backup-${localDateKey(when)}.json`;
}

function isPlainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

// One stored snapshot, validated and normalised to history.js's record shape.
// `date` is trusted when present -- it is the LOCAL day of the browser that
// took it, which cannot be recovered from the timestamp here -- and derived
// only when a hand-edited file omits it.
function normaliseSnapshot(raw, index) {
  const where = `snapshot ${index + 1}`;
  if (!isPlainObject(raw)) throw new Error(`${where} is not an object`);
  if (typeof raw.taken_at !== "string" || !raw.taken_at) {
    throw new Error(`${where} has no taken_at timestamp`);
  }
  const when = new Date(raw.taken_at);
  if (Number.isNaN(when.getTime())) {
    throw new Error(`${where} has an unreadable taken_at: ${raw.taken_at}`);
  }
  if (!isPlainObject(raw.items)) throw new Error(`${where} has no items`);
  const items = {};
  for (const [name, progress] of Object.entries(raw.items)) {
    if (typeof progress !== "number" || !Number.isFinite(progress)) {
      throw new Error(`${where} has a non-numeric progress for ${name}`);
    }
    items[name] = progress;
  }
  const date = typeof raw.date === "string" && raw.date ? raw.date : localDateKey(when);
  return { taken_at: raw.taken_at, date, items };
}

// Validate a backup file's text and return `{state, ui, snapshots, exportedAt}`
// ready to write. Throws an Error whose message is fit to show the user -- the
// caller reports it and writes nothing, so a bad file leaves the browser
// exactly as it was.
export function parseBackup(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    throw new Error("that file is not JSON");
  }
  if (!isPlainObject(data) || data.format !== BACKUP_FORMAT) {
    throw new Error("that file is not a farm-solver backup");
  }
  if (!Number.isInteger(data.version) || data.version < 1) {
    throw new Error("that backup has no version");
  }
  if (data.version > BACKUP_VERSION) {
    throw new Error(
      `that backup is version ${data.version}; this page can only read version ${BACKUP_VERSION}`
    );
  }
  if (data.state !== undefined && !isPlainObject(data.state)) {
    throw new Error("that backup's saved state is not an object");
  }
  if (data.ui !== undefined && !isPlainObject(data.ui)) {
    throw new Error("that backup's preferences are not an object");
  }
  if (data.snapshots !== undefined && !Array.isArray(data.snapshots)) {
    throw new Error("that backup's history is not a list");
  }
  return {
    state: data.state || {},
    ui: data.ui || {},
    snapshots: (data.snapshots || []).map(normaliseSnapshot),
    exportedAt: typeof data.exported_at === "string" ? data.exported_at : null,
  };
}

// "312 KB" -- the file's size, for the status line. Rounded coarsely on
// purpose: it is there to show the backup is not empty, not to be audited.
export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function byteLength(text) {
  return typeof TextEncoder === "undefined"
    ? String(text).length
    : new TextEncoder().encode(text).length;
}

// The text of a file the user picked. FileReader rather than the tidier
// `file.text()`: the Blob promise API is missing from jsdom (which the page
// tests run on) and from older Safari, while this has worked everywhere for a
// decade.
export function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error || new Error("that file could not be read"));
    reader.readAsText(file);
  });
}

// Hand `text` to the browser as a download. An object URL rather than a
// `data:` one because a backup with a long history is megabytes, past what
// some browsers accept in a URL; revoked on the next turn, once the click has
// been dispatched.
export function downloadJson(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
