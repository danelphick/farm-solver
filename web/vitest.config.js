import { fileURLToPath } from "node:url";
import { configDefaults, defineConfig } from "vitest/config";

// Pin the Vite/Vitest cache to web/node_modules/.vite (already gitignored) using
// an absolute path anchored to this config file, not the cwd. Otherwise the IDE's
// Vitest runner resolves the root to the test file's own directory and drops a
// node_modules/.vite/vitest/**/results.json cache *inside* the Django static tree
// (web/solverapp/static/solverapp/), which then shows up untracked and could be
// swept into collectstatic.
const cacheDir = fileURLToPath(new URL("./node_modules/.vite", import.meta.url));

export default defineConfig({
  cacheDir,
  test: {
    // app.js registers a DOMContentLoaded listener at module scope, so
    // importing it needs a `document` even for tests that only exercise its
    // pure exports (parseGoalSpec/serializeGoalEntries).
    environment: "jsdom",
    // collectstatic (run before a deploy) copies the whole static tree --
    // *.test.js and all -- into web/staticfiles/. Those copies sit at a
    // different depth, so template-fixture.js's `?raw` template imports don't
    // resolve from them and the suite fails on a machine that has deployed.
    exclude: [...configDefaults.exclude, "staticfiles/**"],
  },
});
