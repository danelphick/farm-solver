// Unit tests for the timeline page's pure parts: the chart's geometry and the
// delta table's rows. Everything here takes a history.seriesFor series and
// returns markup or plain rows, so none of it needs the page or the store.

import { describe, expect, test } from "vitest";

import {
  axisLabel,
  chartRange,
  chartSvg,
  compactNumber,
  deltaRows,
  filterIndexRows,
  indexRows,
  nextGoalHref,
  nextGoalLabel,
  nextGoalTitle,
  niceRange,
  resolveScale,
  sortIndexRows,
  sortSeriesRows,
  timelineHref,
  tooltipBox,
  tooltipLines,
  transitiveConsumers,
  windowSeries,
} from "./timeline.js";

const point = (takenAt, progress) => ({
  taken_at: takenAt,
  date: takenAt.slice(0, 10),
  progress,
});

// The rendered chart, as a document to query rather than a string to regex.
function draw(series, item = "Tomato") {
  return new DOMParser().parseFromString(chartSvg(series, item), "image/svg+xml");
}

const cx = (svg) =>
  [...svg.querySelectorAll("circle.pt")].map((c) => Number(c.getAttribute("cx")));

describe("niceRange", () => {
  // The case that prompted the rule: a Bamboo Fence running 212,401 → 333,631
  // used to be drawn 194k → 352k. Both bounds now carry a 0 in their third
  // figure, and the midpoint tick lands on one too.
  test("rounds the bounds out to a whole step of the span", () => {
    expect(niceRange(212_401, 333_631)).toEqual({ lo: 210_000, hi: 350_000 });
  });

  // Steps come from the span, not the values: rounding 995,000 → 1,005,000 to
  // the values' own significant figures would give 990k → 1.1M and flatten a
  // line that is the whole point of the chart.
  test("steps by the span even when the values are far larger", () => {
    expect(niceRange(995_000, 1_005_000)).toEqual({ lo: 995_000, hi: 1_005_000 });
    expect(niceRange(339_062, 342_487)).toEqual({ lo: 339_000, hi: 342_600 });
  });

  // chartSvg labels lo, the midpoint and hi, so an odd number of steps would
  // put the middle label on a half step (210k / 275k / 340k).
  test("leaves an even number of steps, so the midpoint is round too", () => {
    const { lo, hi } = niceRange(212_401, 333_631);
    expect((lo + hi) / 2).toBe(280_000);
    // 47,512 → 61,004 rounds to 47k → 62k, which is 15 steps: one more.
    expect(niceRange(47_512, 61_004)).toEqual({ lo: 47_000, hi: 63_000 });
  });

  test("never steps below a whole unit of progress, or below zero", () => {
    expect(niceRange(0, 1)).toEqual({ lo: 0, hi: 2 });
    expect(niceRange(212_401, 212_450)).toEqual({ lo: 212_401, hi: 212_451 });
  });
});

describe("chartRange", () => {
  // A lone snapshot has no shape to show, so it keeps the tier context.
  test("one point runs from zero to the next tier above it", () => {
    expect(chartRange([point("2026-07-01T12:00:00.000Z", 4_000)])).toEqual({
      lo: 0,
      hi: 10_000,
    });
    expect(chartRange([point("2026-07-01T12:00:00.000Z", 412_900)])).toEqual({
      lo: 0,
      hi: 1_000_000,
    });
  });

  test("keeps the full range while the series spans a useful part of it", () => {
    expect(
      chartRange([
        point("2026-07-01T12:00:00.000Z", 100_000),
        point("2026-07-15T12:00:00.000Z", 700_000),
      ])
    ).toEqual({ lo: 0, hi: 1_000_000 });
  });

  // Under half the tier scale, the movement is real but the chart is mostly
  // empty above and below it -- 307k → 603k used to sit in the middle third of
  // a 0 → 1M chart.
  test("zooms a series that uses less than half the tier scale", () => {
    expect(
      chartRange([
        point("2026-07-29T12:00:00.000Z", 307_000),
        point("2026-08-16T12:00:00.000Z", 603_000),
      ])
    ).toEqual(niceRange(307_000, 603_000));
  });

  // The reason the zoom exists: a week's real progress is ~1% of a tier, which
  // drawn from zero is a flat line -- the one thing this page must not be.
  test("zooms to the data when the movement would be invisible", () => {
    expect(
      chartRange([
        point("2026-07-01T12:00:00.000Z", 339_062),
        point("2026-07-24T12:00:00.000Z", 342_487),
      ])
    ).toEqual(niceRange(339_062, 342_487));
  });

  // A flat series has no span for niceRange to step by, so it is given a window
  // first and that window is what gets rounded.
  test("a flat series still gets a window to sit in", () => {
    const { lo, hi } = chartRange([
      point("2026-07-01T12:00:00.000Z", 339_101),
      point("2026-07-08T12:00:00.000Z", 339_101),
    ]);
    expect(lo).toBeLessThan(339_101);
    expect(hi).toBeGreaterThan(339_101);
    expect(lo % 10).toBe(0);
    expect(hi % 10).toBe(0);
  });

  test("never drops below zero when zooming a small progress", () => {
    const { lo } = chartRange([
      point("2026-07-01T12:00:00.000Z", 0),
      point("2026-07-08T12:00:00.000Z", 1),
    ]);
    expect(lo).toBe(0);
  });

  // Past Megamaster there is no next threshold, so the top is just headroom.
  test("leaves headroom above a megamastered item", () => {
    const { lo, hi } = chartRange([point("2026-07-01T12:00:00.000Z", 2_000_000)]);
    expect(lo).toBe(0);
    expect(hi).toBeGreaterThan(2_000_000);
  });

  // The chart's toggle: either scale can be asked for outright, whatever the
  // automatic rule would have picked.
  test("takes the scale the reader asked for", () => {
    const series = [
      point("2026-07-29T12:00:00.000Z", 307_000),
      point("2026-08-16T12:00:00.000Z", 603_000),
    ];
    expect(chartRange(series, "full")).toEqual({ lo: 0, hi: 1_000_000 });
    expect(chartRange(series, "zoom")).toEqual(niceRange(307_000, 603_000));
    expect(chartRange(series)).toEqual(chartRange(series, "zoom"));

    // A lone snapshot is "full" on its own, but can still be zoomed to a window
    // around the one point it has.
    const lone = [point("2026-07-01T12:00:00.000Z", 412_900)];
    expect(resolveScale(lone)).toBe("full");
    const { lo, hi } = chartRange(lone, "zoom");
    expect(lo).toBeLessThan(412_900);
    expect(hi).toBeGreaterThan(412_900);
  });
});

describe("windowSeries", () => {
  const NOW = new Date("2026-08-16T06:00:00.000Z");
  const series = [
    point("2025-06-01T12:00:00.000Z", 100), // over a year back
    point("2026-06-20T12:00:00.000Z", 200), // ~2 months
    point("2026-08-01T12:00:00.000Z", 300), // ~2 weeks
    point("2026-08-12T12:00:00.000Z", 400), // 4 days
    point("2026-08-14T12:00:00.000Z", 500), // 2 days
    point("2026-08-15T12:00:00.000Z", 600), // 18 hours
  ];
  const kept = (key) => windowSeries(series, key, NOW).map((p) => p.progress);

  // Each window reaches back a day further than it says, so what it shows is
  // that many days of movement: "1d" is a day's climb (two readings), not one
  // point on its own.
  test("keeps the points inside the window, measured back from now", () => {
    expect(kept("1d")).toEqual([500, 600]);
    expect(kept("7d")).toEqual([400, 500, 600]);
    expect(kept("1m")).toEqual([300, 400, 500, 600]);
    expect(kept("1y")).toEqual([200, 300, 400, 500, 600]);
  });

  test("keeps everything for the All window", () => {
    expect(kept("all")).toEqual([100, 200, 300, 400, 500, 600]);
  });

  // What a saved preference from an older build looks like.
  test("treats an unknown window as All", () => {
    expect(kept("6h")).toEqual([100, 200, 300, 400, 500, 600]);
  });

  // A window can be empty -- the page disables the button rather than drawing
  // an empty axis, but the function itself just says so.
  test("can come back empty", () => {
    expect(windowSeries([point("2026-01-01T12:00:00.000Z", 1)], "7d", NOW)).toEqual([]);
  });
});

describe("resolveScale", () => {
  const wide = [
    point("2026-07-01T12:00:00.000Z", 120_000),
    point("2026-07-15T12:00:00.000Z", 820_000),
  ];
  const narrow = [
    point("2026-07-01T12:00:00.000Z", 307_000),
    point("2026-07-15T12:00:00.000Z", 603_000),
  ];

  test("auto keeps the tier scale only for a series that fills it", () => {
    expect(resolveScale(wide)).toBe("full");
    expect(resolveScale(narrow)).toBe("zoom");
  });

  test("an explicit choice wins over the rule", () => {
    expect(resolveScale(wide, "zoom")).toBe("zoom");
    expect(resolveScale(narrow, "full")).toBe("full");
  });
});

describe("chartSvg", () => {
  test("plots one point per snapshot, in series order", () => {
    const svg = draw([
      point("2026-07-01T12:00:00.000Z", 100),
      point("2026-07-08T12:00:00.000Z", 200),
      point("2026-07-15T12:00:00.000Z", 300),
    ]);
    const xs = cx(svg);
    expect(xs).toHaveLength(3);
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
    // Evenly spaced dates, so the middle point lands halfway along.
    expect(xs[1] - xs[0]).toBeCloseTo(xs[2] - xs[1], 1);
  });

  // The reason the X axis is built from the timestamps rather than the index:
  // irregular gaps have to read as irregular.
  test("spaces points by elapsed time, not by position", () => {
    const [a, b, c] = cx(
      draw([
        point("2026-07-01T12:00:00.000Z", 100),
        point("2026-07-02T12:00:00.000Z", 110),
        point("2026-07-15T12:00:00.000Z", 300),
      ])
    );
    expect(b - a).toBeLessThan((c - a) / 10);
  });

  test("two snapshots the same day sit next to each other", () => {
    const [morning, evening, later] = cx(
      draw([
        point("2026-07-01T08:00:00.000Z", 100),
        point("2026-07-01T20:00:00.000Z", 140),
        point("2026-07-15T08:00:00.000Z", 300),
      ])
    );
    expect(evening).toBeGreaterThan(morning);
    expect(evening - morning).toBeLessThan((later - morning) / 10);
  });

  test("a lone snapshot still gets a scale to sit in", () => {
    const xs = cx(draw([point("2026-07-01T12:00:00.000Z", 100)]));
    expect(xs).toHaveLength(1);
    expect(Number.isFinite(xs[0])).toBe(true);
  });

  test("higher progress is drawn higher up", () => {
    const svg = draw([
      point("2026-07-01T12:00:00.000Z", 100),
      point("2026-07-08T12:00:00.000Z", 5_000),
    ]);
    const ys = [...svg.querySelectorAll("circle.pt")].map((c) => Number(c.getAttribute("cy")));
    expect(ys[1]).toBeLessThan(ys[0]); // SVG y grows downwards
  });

  // The X axis is labelled at both ends and the midpoint -- but they are dates,
  // and inside the 1d window all three can land on the same day.
  const xLabels = (svg) =>
    [...svg.querySelectorAll("text.muted")]
      .map((t) => t.textContent)
      .filter((label) => /[A-Za-z]/.test(label));

  test("labels the X axis at both ends and the middle", () => {
    expect(
      xLabels(
        draw([
          point("2026-07-01T12:00:00.000Z", 100),
          point("2026-07-31T12:00:00.000Z", 300),
        ])
      )
    ).toHaveLength(3);
  });

  test("drops a midpoint date that repeats an end", () => {
    expect(
      xLabels(
        draw([
          point("2026-08-15T12:00:00.000Z", 100),
          point("2026-08-16T12:00:00.000Z", 300),
        ])
      )
    ).toEqual(["Aug 15", "Aug 16"]);
  });

  const tierLabels = (svg) =>
    [...svg.querySelectorAll("text.tier-label")].map((t) => t.textContent);

  test("draws only the tier lines inside the range", () => {
    // Best progress 4,000 -> the chart tops out at Master, so GM/MM are off it.
    expect(tierLabels(draw([point("2026-07-01T12:00:00.000Z", 4_000)]))).toEqual(["M"]);
  });

  // On a scale that reaches Megamaster the Master line sits at 1% of the
  // height, hard against the axis, and adds nothing to the Grandmaster line.
  test("drops Master once Grandmaster and Megamaster are both drawn", () => {
    expect(tierLabels(draw([point("2026-07-01T12:00:00.000Z", 412_900)]))).toEqual([
      "GM",
      "MM",
    ]);
    // Two of the three is not the crowded case: M stays while MM is off-chart.
    expect(tierLabels(draw([point("2026-07-01T12:00:00.000Z", 40_000)]))).toEqual([
      "M",
      "GM",
    ]);
  });

  // A zoomed range (see chartRange) usually excludes every threshold -- except
  // the one being crossed, which is exactly when it is worth seeing.
  test("a zoomed range keeps only a threshold it actually contains", () => {
    expect(
      tierLabels(
        draw([
          point("2026-07-01T12:00:00.000Z", 339_062),
          point("2026-07-24T12:00:00.000Z", 342_487),
        ])
      )
    ).toEqual([]);
    expect(
      tierLabels(
        draw([
          point("2026-07-01T12:00:00.000Z", 99_000),
          point("2026-07-24T12:00:00.000Z", 101_000),
        ])
      )
    ).toEqual(["GM"]);
  });

  test("higher progress is still drawn higher up on a zoomed range", () => {
    const svg = draw([
      point("2026-07-01T12:00:00.000Z", 339_062),
      point("2026-07-24T12:00:00.000Z", 342_487),
    ]);
    const ys = [...svg.querySelectorAll("circle.pt")].map((c) => Number(c.getAttribute("cy")));
    expect(ys[1]).toBeLessThan(ys[0]);
  });

  test("escapes the item name in the accessible label", () => {
    const svg = chartSvg([point("2026-07-01T12:00:00.000Z", 100)], "Ap<ple>");
    expect(svg).not.toContain("Ap<ple>");
    expect(svg).toContain("Ap&lt;ple&gt;");
  });

  // Every point carries its own hover target and text; positioning them is
  // wireChartTooltip's job (see the page tests), sized off the one shared box
  // this markup ends with.
  test("gives every point a hit target carrying its tooltip text", () => {
    const svg = draw([
      point("2026-07-01T12:00:00.000Z", 400_000),
      point("2026-07-08T12:00:00.000Z", 404_800),
    ]);
    const hits = [...svg.querySelectorAll("circle.pt-hit")];
    expect(hits).toHaveLength(2);
    // Over the dot it stands in for, and bigger than it.
    const dots = [...svg.querySelectorAll("circle.pt")];
    expect(hits[0].getAttribute("cx")).toBe(dots[0].getAttribute("cx"));
    expect(hits[0].getAttribute("cy")).toBe(dots[0].getAttribute("cy"));
    expect(Number(hits[0].getAttribute("r"))).toBeGreaterThan(
      Number(dots[0].getAttribute("r"))
    );
    expect(hits[1].getAttribute("data-tip").split("|")).toEqual([
      expect.stringMatching(/^2026-07-08 \d\d:\d\d$/),
      "404,800 mastery",
    ]);
    expect(svg.querySelectorAll("g.tip")).toHaveLength(1);
  });

  // The hover is a pointer affordance; a screen reader gets the same words off
  // the point itself.
  test("labels each point with what its tooltip says", () => {
    const svg = draw([point("2026-07-01T12:00:00.000Z", 412_900)]);
    expect(svg.querySelector("g.pt-group").getAttribute("aria-label")).toMatch(
      /^2026-07-01 \d\d:\d\d, 412,900 mastery$/
    );
  });
});

describe("tooltipLines", () => {
  // The tier is deliberately not among them: the chart draws the M/GM/MM lines
  // and the table below has a column for it.
  test("name the snapshot and the mastery it recorded", () => {
    expect(tooltipLines(point("2026-07-01T12:00:00.000Z", 412_900))).toEqual([
      expect.stringMatching(/^2026-07-01 \d\d:\d\d$/),
      "412,900 mastery",
    ]);
  });
});

describe("tooltipBox", () => {
  const LINES = ["2026-07-01 12:00", "412,900 mastery"];

  test("sits above the point, centred on it", () => {
    const box = tooltipBox(LINES, 300, 200);
    expect(Math.abs(box.x + box.width / 2 - 300)).toBeLessThanOrEqual(1);
    expect(box.y + box.height).toBeLessThan(200);
  });

  test("flips below a point too near the top to fit above it", () => {
    const box = tooltipBox(LINES, 300, 20);
    expect(box.y).toBeGreaterThan(20);
  });

  // Otherwise the first and last points -- the two most worth reading -- would
  // hang their box off the edge of the viewBox and lose half of it.
  test("stays inside the chart at either end", () => {
    const first = tooltipBox(LINES, 62, 150);
    expect(first.x).toBeGreaterThanOrEqual(0);
    const last = tooltipBox(LINES, 606, 150);
    expect(last.x + last.width).toBeLessThanOrEqual(640);
  });

  test("grows with the longest line and the number of them", () => {
    const box = tooltipBox(LINES, 300, 200);
    expect(box.height).toBeGreaterThan(tooltipBox(LINES.slice(0, 1), 300, 200).height);
    expect(tooltipBox(["a much, much longer line than either of those"], 300, 200).width)
      .toBeGreaterThan(box.width);
  });
});

describe("deltaRows", () => {
  const SERIES = [
    point("2026-07-01T12:00:00.000Z", 400_000),
    point("2026-07-08T12:00:00.000Z", 404_800),
    point("2026-07-15T12:00:00.000Z", 412_900),
  ];

  test("newest first, with the gain against the previous snapshot", () => {
    const rows = deltaRows(SERIES);
    expect(rows.map((r) => r.date)).toEqual(["2026-07-15", "2026-07-08", "2026-07-01"]);
    expect(rows.map((r) => r.gain)).toEqual([8_100, 4_800, null]);
  });

  test("gain per day divides by the real elapsed time", () => {
    const rows = deltaRows(SERIES);
    expect(rows[0].perDay).toBeCloseTo(8_100 / 7, 5);
    expect(rows[2].perDay).toBeNull(); // nothing before it to measure from
  });

  // Two saves minutes apart would otherwise extrapolate a handful of items into
  // thousands a day.
  test("no per-day rate for snapshots less than an hour apart", () => {
    const rows = deltaRows([
      point("2026-07-15T12:00:00.000Z", 412_900),
      point("2026-07-15T12:10:00.000Z", 412_950),
    ]);
    expect(rows[0].gain).toBe(50);
    expect(rows[0].perDay).toBeNull();
  });

  test("flags the rows whose day holds more than one snapshot", () => {
    const rows = deltaRows([
      point("2026-07-01T08:00:00.000Z", 100),
      point("2026-07-15T08:00:00.000Z", 200),
      point("2026-07-15T20:00:00.000Z", 260),
    ]);
    expect(rows.map((r) => r.showTime)).toEqual([true, true, false]);
  });

  test("derives each row's tier from its progress", () => {
    const rows = deltaRows([
      point("2026-07-01T12:00:00.000Z", 9_999),
      point("2026-07-08T12:00:00.000Z", 100_000),
      point("2026-07-15T12:00:00.000Z", 1_000_000),
    ]);
    expect(rows.map((r) => r.tier)).toEqual(["MM", "GM", ""]);
  });
});

describe("compactNumber", () => {
  test("shortens axis labels", () => {
    expect(compactNumber(0)).toBe("0");
    expect(compactNumber(900)).toBe("900");
    expect(compactNumber(10_000)).toBe("10k");
    expect(compactNumber(500_000)).toBe("500k");
    expect(compactNumber(1_000_000)).toBe("1.0M");
    expect(compactNumber(12_500_000)).toBe("13M");
  });
});

describe("axisLabel", () => {
  // A zoomed axis can be a couple of thousand wide, where "339k" three times
  // over would be the whole scale.
  test("spells out the exact figure on a narrow axis", () => {
    expect(axisLabel(339_062, 4_000)).toBe("339,062");
    expect(axisLabel(500_000, 1_000_000)).toBe("500k");
  });
});

describe("indexRows", () => {
  // Read as of 2026-07-28 12:00Z, so the 7-day window opens on the 21st and the
  // 1-day window on the 27th.
  const NOW = new Date("2026-07-28T12:00:00.000Z");
  const snapshot = (takenAt, items) => ({
    taken_at: takenAt,
    date: takenAt.slice(0, 10),
    items,
  });

  const SNAPSHOTS = [
    snapshot("2026-07-01T12:00:00.000Z", { Tomato: 400_000, Hops: 900 }),
    snapshot("2026-07-24T12:00:00.000Z", { Tomato: 404_800, Hops: 900 }),
    snapshot("2026-07-28T08:00:00.000Z", { Tomato: 412_900, Hops: 950 }),
  ];

  test("carries the tier and both windowed changes the table shows", () => {
    expect(indexRows(SNAPSHOTS, NOW)).toEqual([
      // Hops moved only inside the last day; Tomato moved in both windows.
      { item: "Hops", latest: 950, tier: "", change7: 50, change1: 50 },
      { item: "Tomato", latest: 412_900, tier: "GM", change7: 12_900, change1: 8_100 },
    ]);
  });

  // The headline change of this table: not "since your first ever snapshot".
  test("ignores movement from before the window", () => {
    const rows = indexRows(
      [
        snapshot("2026-01-01T12:00:00.000Z", { Tomato: 1 }),
        snapshot("2026-07-21T06:00:00.000Z", { Tomato: 400_000 }),
        snapshot("2026-07-28T08:00:00.000Z", { Tomato: 412_900 }),
      ],
      NOW
    );
    // From where it stood when the window opened on the 21st at noon -- the
    // reading six hours earlier -- not from January's.
    expect(rows[0].change7).toBe(12_900);
  });

  // The flip side of measuring from the last reading before the window: with
  // nothing recorded for months, that reading is months old, so the figure
  // covers more than the window names. It is still the only answer there is,
  // and it beats the alternative -- measuring from the oldest reading *inside*
  // the window, which for the ordinary once-a-day snapshot would leave the
  // 1-day column with nothing to compare against at all.
  test("measures from a stale reading when that is all there is", () => {
    const rows = indexRows(
      [
        snapshot("2026-01-01T12:00:00.000Z", { Tomato: 1 }),
        snapshot("2026-07-28T08:00:00.000Z", { Tomato: 412_900 }),
      ],
      NOW
    );
    expect(rows[0].change7).toBe(412_899);
  });

  // "—", not a 0 that would read as "no progress this week".
  test("has no figure for a window it cannot answer", () => {
    const stale = indexRows(
      [snapshot("2026-06-01T12:00:00.000Z", { Tomato: 400_000 })],
      NOW
    );
    expect(stale[0]).toMatchObject({ change7: null, change1: null });

    const brandNew = indexRows(
      [snapshot("2026-07-28T08:00:00.000Z", { Tomato: 400_000 })],
      NOW
    );
    expect(brandNew[0]).toMatchObject({ change7: null, change1: null });
  });
});

describe("sortIndexRows", () => {
  const ROWS = [
    { item: "Apple", latest: 1_400_000, change7: 400, change1: 0, tier: "MM" },
    { item: "hops", latest: 900, change7: 20, change1: 20, tier: "" },
    { item: "Tomato", latest: 412_900, change7: 12_900, change1: 8_100, tier: "GM" },
  ];

  const names = (rows) => rows.map((row) => row.item);

  test("sorts by item, case-insensitively", () => {
    expect(names(sortIndexRows(ROWS, "item", "asc"))).toEqual(["Apple", "hops", "Tomato"]);
    expect(names(sortIndexRows(ROWS, "item", "desc"))).toEqual(["Tomato", "hops", "Apple"]);
  });

  test("sorts by each numeric column", () => {
    expect(names(sortIndexRows(ROWS, "latest", "asc"))).toEqual(["hops", "Tomato", "Apple"]);
    expect(names(sortIndexRows(ROWS, "change7", "desc"))).toEqual(["Tomato", "Apple", "hops"]);
    expect(names(sortIndexRows(ROWS, "change1", "desc"))).toEqual(["Tomato", "hops", "Apple"]);
  });

  // A window with no answer is not a small number: it sinks either way, so
  // flipping the column never floats "—" rows to the top.
  test("keeps rows with no figure at the bottom whichever way it points", () => {
    const rows = [
      ...ROWS,
      { item: "Stale", latest: 1_000, change7: null, change1: null, tier: "" },
    ];
    for (const direction of ["asc", "desc"]) {
      expect(names(sortIndexRows(rows, "change7", direction)).at(-1)).toBe("Stale");
    }
  });

  // The tier is the progress, coarsened -- so the two columns agree, and rows
  // sharing a tier still read in progress order rather than arbitrarily.
  test("sorts by tier exactly as it sorts by the mastery itself", () => {
    const rows = [
      ...ROWS,
      { item: "Corn", latest: 999_999, change7: 5, change1: 5, tier: "GM" },
    ];
    for (const direction of ["asc", "desc"]) {
      expect(names(sortIndexRows(rows, "tier", direction))).toEqual(
        names(sortIndexRows(rows, "latest", direction))
      );
    }
    expect(names(sortIndexRows(rows, "tier", "asc"))).toEqual([
      "hops",
      "Tomato",
      "Corn",
      "Apple",
    ]);
  });

  test("leaves the input alone and falls back to item order", () => {
    const before = names(ROWS);
    expect(names(sortIndexRows(ROWS, "nonesuch", "asc"))).toEqual(["Apple", "hops", "Tomato"]);
    expect(names(ROWS)).toEqual(before);
  });
});

describe("sortSeriesRows", () => {
  const ROWS = deltaRows([
    point("2026-07-01T12:00:00.000Z", 400_000),
    point("2026-07-08T12:00:00.000Z", 404_800),
    point("2026-07-15T12:00:00.000Z", 412_900),
  ]);

  const dates = (rows) => rows.map((row) => row.date);

  test("sorts by date either way", () => {
    expect(dates(sortSeriesRows(ROWS, "date", "asc"))).toEqual([
      "2026-07-01",
      "2026-07-08",
      "2026-07-15",
    ]);
    expect(dates(sortSeriesRows(ROWS, "date", "desc"))).toEqual([
      "2026-07-15",
      "2026-07-08",
      "2026-07-01",
    ]);
  });

  test("sorts by progress, gain and rate", () => {
    expect(dates(sortSeriesRows(ROWS, "progress", "asc"))[0]).toBe("2026-07-01");
    expect(dates(sortSeriesRows(ROWS, "gain", "desc"))[0]).toBe("2026-07-15");
    expect(dates(sortSeriesRows(ROWS, "perday", "desc"))[0]).toBe("2026-07-15");
  });

  // The oldest row has nothing to measure a gain against ("—"): that is no
  // number rather than a small one, so it belongs at the bottom either way.
  test("keeps rows with no figure at the bottom whichever way it points", () => {
    expect(dates(sortSeriesRows(ROWS, "gain", "asc")).at(-1)).toBe("2026-07-01");
    expect(dates(sortSeriesRows(ROWS, "gain", "desc")).at(-1)).toBe("2026-07-01");
    expect(dates(sortSeriesRows(ROWS, "perday", "asc")).at(-1)).toBe("2026-07-01");
  });

  test("sorts by tier exactly as it sorts by progress", () => {
    for (const direction of ["asc", "desc"]) {
      expect(dates(sortSeriesRows(ROWS, "tier", direction))).toEqual(
        dates(sortSeriesRows(ROWS, "progress", direction))
      );
    }
  });
});

describe("transitiveConsumers", () => {
  // The chain the feature exists for: Straw makes Twine, Twine makes Rope and
  // Yarn, Rope makes Fishing Nets. A pile of Straw is all of them.
  const USES = {
    straw: ["twine", "broom"],
    twine: ["rope", "yarn"],
    rope: ["fishing net"],
    wood: ["board"],
    board: ["bucket"],
  };

  test("follows the recipes all the way down, not just one step", () => {
    expect([...transitiveConsumers(USES, ["straw"])].sort()).toEqual([
      "broom",
      "fishing net",
      "rope",
      "twine",
      "yarn",
    ]);
  });

  test("unions several materials", () => {
    expect([...transitiveConsumers(USES, ["rope", "wood"])].sort()).toEqual([
      "board",
      "bucket",
      "fishing net",
    ]);
  });

  test("a material nothing is made from reaches nothing", () => {
    expect(transitiveConsumers(USES, ["bucket"]).size).toBe(0);
    expect(transitiveConsumers(USES, ["nonesuch"]).size).toBe(0);
    expect(transitiveConsumers(USES, []).size).toBe(0);
  });

  // Nothing in the data guarantees the graph is acyclic, and a cycle must not
  // hang the page.
  test("terminates on a cycle", () => {
    const cyclic = { a: ["b"], b: ["c"], c: ["a"] };
    expect([...transitiveConsumers(cyclic, ["a"])].sort()).toEqual(["a", "b", "c"]);
  });
});

describe("filterIndexRows", () => {
  const rows = [
    { item: "Twine", tier: "MM" },
    { item: "Rope", tier: "GM" },
    { item: "Broom", tier: "M" },
    { item: "Bucket", tier: "" },
  ];
  const allTiers = new Set(["", "M", "GM", "MM"]);

  test("keeps only the ticked tiers", () => {
    const kept = filterIndexRows(rows, {
      tiers: new Set(["", "M", "GM"]), // the default: MM off
      allowed: null,
    });
    expect(kept.map((row) => row.item)).toEqual(["Rope", "Broom", "Bucket"]);
  });

  test("keeps only what the materials reach", () => {
    const kept = filterIndexRows(rows, {
      tiers: allTiers,
      allowed: new Set(["twine", "rope"]),
    });
    expect(kept.map((row) => row.item)).toEqual(["Twine", "Rope"]);
  });

  test("applies both filters together", () => {
    const kept = filterIndexRows(rows, {
      tiers: new Set(["GM"]),
      allowed: new Set(["twine", "rope"]),
    });
    expect(kept.map((row) => row.item)).toEqual(["Rope"]);
  });

  test("matches the reachable set case-insensitively", () => {
    const kept = filterIndexRows([{ item: "Fishing Net", tier: "M" }], {
      tiers: allTiers,
      allowed: new Set(["fishing net"]),
    });
    expect(kept).toHaveLength(1);
  });
});

describe("nextGoalHref", () => {
  test("aims at the tier above where the item stands", () => {
    expect(nextGoalHref("Apple", "")).toBe("/?mastery=Apple%3AM");
    expect(nextGoalHref("Apple", "M")).toBe("/?mastery=Apple%3AGM");
    expect(nextGoalHref("Apple", "GM")).toBe("/?mastery=Apple%3AMM");
  });

  // Past Megamaster every threshold is behind you, so a mastery goal of any
  // size is already met and would plan nothing: ask for a plain count instead.
  test("asks for 10,000 more of a Megamastered item", () => {
    expect(nextGoalHref("Rope", "MM")).toBe("/?items=Rope%3A10000");
  });

  test("encodes a name with spaces and a colon", () => {
    expect(nextGoalHref("Oak Table", "GM")).toBe("/?mastery=Oak%20Table%3AMM");
    // parseGoalSpec splits on the LAST colon, so the item's own survives.
    expect(nextGoalHref("Time: A Gift", "MM")).toBe(
      "/?items=Time%3A%20A%20Gift%3A10000"
    );
  });

  test("labels and titles say which goal it is", () => {
    expect(nextGoalLabel("M")).toBe("→ GM");
    expect(nextGoalTitle("Apple", "GM")).toBe("Plan Apple to Megamaster");
    // An item goal is a target to HOLD that many (the solver counts what is in
    // inventory towards it), so it reads as "towards", not "10,000 more".
    expect(nextGoalLabel("MM")).toBe("→ 10,000");
    expect(nextGoalTitle("Rope", "MM")).toBe("Plan 10,000 Rope");
  });
});

describe("timelineHref", () => {
  test("encodes the item into the page's own query string", () => {
    expect(timelineHref("Oak Table")).toBe("/timeline?item=Oak%20Table");
  });
});
