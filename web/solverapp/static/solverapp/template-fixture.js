// The real page templates, as DOM-ready markup for the page-level tests.
//
// Both pages are driven against their ACTUAL template (not a hand-written
// fixture), so an id renamed in one file and not the other fails in a test
// rather than silently in a browser. That means expanding the
// `{% include "solverapp/_settings.html" %}` both pages use -- every settings
// control lives in that partial now, so a naive tag-strip would drop the lot
// and applyParams would throw on the first missing input.
//
// Vite's `?raw` rather than fs: it resolves relative to this file at transform
// time, so the tests don't depend on the working directory (which the IDE's
// Vitest runner and `npm test` disagree about -- see vitest.config.js).

import INDEX from "../../templates/solverapp/index.html?raw";
import MASTERY_COSTS from "../../templates/solverapp/mastery_costs.html?raw";
import TIMELINE from "../../templates/solverapp/timeline.html?raw";
import SETTINGS from "../../templates/solverapp/_settings.html?raw";

const TEMPLATES = {
  index: INDEX,
  mastery_costs: MASTERY_COSTS,
  timeline: TIMELINE,
};

const INCLUDE_RE = /\{%\s*include\s+"solverapp\/_settings\.html"\s*%\}/g;

// `{% comment %}` blocks are stripped whole (their body is not a tag, so the
// generic strip below would leave the prose behind).
const COMMENT_RE = /\{%\s*comment\s*%\}[\s\S]*?\{%\s*endcomment\s*%\}/g;

// One page's <body> contents: includes expanded, then all remaining Django
// syntax removed (the `<script>`/`<link>` tags around it live in <head> and go
// with the rest of it).
export function pageBody(name) {
  const template = TEMPLATES[name];
  if (!template) throw new Error(`unknown template: ${name}`);
  const html = template
    .replace(INCLUDE_RE, () => SETTINGS)
    .replace(COMMENT_RE, "")
    .replace(/\{%.*?%\}/g, "")
    .replace(/\{\{.*?\}\}/g, "");
  return html.slice(html.indexOf("<body>") + 6, html.indexOf("</body>"));
}
