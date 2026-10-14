import { describe, expect, it } from "vitest";

import { solveTimeText } from "./timing.js";

describe("solveTimeText", () => {
  it("shows only the wall-clock time without debug timing", () => {
    expect(solveTimeText({ cached: false, solved_ms: 412 })).toBe("solved in 412 ms");
    expect(solveTimeText({ cached: true, solved_ms: 3, timing: null })).toBe("cached (3 ms)");
  });

  it("adds the CPU time across cores in debug builds", () => {
    const data = {
      cached: false,
      solved_ms: 2015,
      timing: { wall_ms: 2015, cpu_ms: 5220, cores: 2.59 },
    };
    expect(solveTimeText(data)).toBe("solved in 2015 ms · CPU 5,220 ms (2.6 cores)");
  });
});
