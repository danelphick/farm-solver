import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // app.js registers a DOMContentLoaded listener at module scope, so
    // importing it needs a `document` even for tests that only exercise its
    // pure exports (parseGoalSpec/serializeGoalEntries).
    environment: "jsdom",
  },
});
