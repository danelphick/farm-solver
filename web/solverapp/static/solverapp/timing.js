// The status line's solve-time text, shared by the solver and mastery-costs
// pages. Every build shows the wall-clock time; DEBUG builds also return
// `timing` -- wall-clock and total CPU across every core the solve used
// (views._debug_timing) -- and the line then adds the CPU detail.

export function solveTimeText(data) {
  const base = data.cached
    ? `cached (${data.solved_ms} ms)`
    : `solved in ${data.solved_ms} ms`;
  const t = data.timing;
  if (!t) return base;
  return `${base} · CPU ${t.cpu_ms.toLocaleString()} ms (${t.cores.toFixed(1)} cores)`;
}
