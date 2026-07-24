// Shared `[?]` help tooltips, used by both pages (app.js and masteries.js).
//
// Each parameter carries a `[?]` button (`.help-icon`) with its description in
// `data-help`; hovering or keyboard-focusing one shows a single shared floating
// tooltip, positioned above the icon (flipped below near the top edge, clamped
// horizontally to the viewport). One delegated set of listeners on `document`
// covers every icon, generated or hand-written in either template.
//
// Its own module rather than an app.js export: app.js registers a
// DOMContentLoaded -> init() listener at module scope, so importing it from the
// mastery-costs page would run the solver page's whole wiring against markup
// that isn't there.

let helpTooltipEl = null;

export function createHelpIcon(text) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "help-icon";
  btn.textContent = "?";
  btn.dataset.help = text;
  btn.setAttribute("aria-label", text);  // read out for screen readers too
  return btn;
}

function ensureHelpTooltip() {
  if (!helpTooltipEl) {
    helpTooltipEl = document.createElement("div");
    helpTooltipEl.className = "help-tooltip";
    helpTooltipEl.setAttribute("role", "tooltip");
    helpTooltipEl.hidden = true;
    document.body.appendChild(helpTooltipEl);
  }
  return helpTooltipEl;
}

function positionHelpTooltip(icon, tip) {
  const r = icon.getBoundingClientRect();
  const margin = 8;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let top = r.top - th - margin;             // above the icon by default
  if (top < margin) top = r.bottom + margin; // not enough room -> below
  let left = r.left + r.width / 2 - tw / 2;   // centred on the icon
  left = Math.max(margin, Math.min(left, window.innerWidth - tw - margin));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function showHelpTooltip(icon) {
  const text = icon.dataset.help;
  if (!text) return;
  const tip = ensureHelpTooltip();
  tip.textContent = text;
  tip.hidden = false;
  positionHelpTooltip(icon, tip);
}

export function hideHelpTooltip() {
  if (helpTooltipEl) helpTooltipEl.hidden = true;
}

export function initHelpTooltips() {
  const iconOf = (event) =>
    event.target.closest && event.target.closest(".help-icon");
  document.addEventListener("pointerover", (e) => {
    const icon = iconOf(e);
    if (icon) showHelpTooltip(icon);
  });
  document.addEventListener("pointerout", (e) => {
    if (iconOf(e)) hideHelpTooltip();
  });
  document.addEventListener("focusin", (e) => {
    const icon = iconOf(e);
    if (icon) showHelpTooltip(icon);
  });
  document.addEventListener("focusout", (e) => {
    if (iconOf(e)) hideHelpTooltip();
  });
  // The tooltip is viewport-positioned, so a scroll would strand it -- hide it
  // (any capture-phase scroll, since panes/textarea can scroll). Escape too.
  window.addEventListener("scroll", hideHelpTooltip, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideHelpTooltip();
  });
}
