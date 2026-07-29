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
  indexRows,
  sortIndexRows,
  sortSeriesRows,
  timelineHref,
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
        point("2026-07-15T12:00:00.000Z", 600_000),
      ])
    ).toEqual({ lo: 0, hi: 1_000_000 });
  });

  // The reason the zoom exists: a week's real progress is ~1% of a tier, which
  // drawn from zero is a flat line -- the one thing this page must not be.
  test("zooms to the data when the movement would be invisible", () => {
    const { lo, hi } = chartRange([
      point("2026-07-01T12:00:00.000Z", 339_062),
      point("2026-07-24T12:00:00.000Z", 342_487),
    ]);
    expect(lo).toBeGreaterThan(300_000);
    expect(lo).toBeLessThan(339_062);
    expect(hi).toBeGreaterThan(342_487);
    expect(hi).toBeLessThan(400_000);
  });

  test("a flat series still gets a window to sit in", () => {
    const { lo, hi } = chartRange([
      point("2026-07-01T12:00:00.000Z", 339_101),
      point("2026-07-08T12:00:00.000Z", 339_101),
    ]);
    expect(lo).toBeLessThan(339_101);
    expect(hi).toBeGreaterThan(339_101);
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

  const tierLabels = (svg) =>
    [...svg.querySelectorAll("text.tier-label")].map((t) => t.textContent);

  test("draws only the tier lines inside the range", () => {
    // Best progress 4,000 -> the chart tops out at Master, so GM/MM are off it.
    expect(tierLabels(draw([point("2026-07-01T12:00:00.000Z", 4_000)]))).toEqual(["M"]);
  });

  test("draws all three once the range reaches Megamaster", () => {
    expect(tierLabels(draw([point("2026-07-01T12:00:00.000Z", 412_900)]))).toEqual([
      "M",
      "GM",
      "MM",
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
  const SNAPSHOTS = [
    { taken_at: "2026-07-01T12:00:00.000Z", date: "2026-07-01", items: { Tomato: 400_000, Hops: 900 } },
    { taken_at: "2026-07-15T12:00:00.000Z", date: "2026-07-15", items: { Tomato: 412_900 } },
  ];

  test("carries the change and tier the table shows", () => {
    expect(indexRows(SNAPSHOTS)).toEqual([
      { item: "Hops", first: 900, latest: 900, change: 0, tier: "" },
      { item: "Tomato", first: 400_000, latest: 412_900, change: 12_900, tier: "GM" },
    ]);
  });
});

describe("sortIndexRows", () => {
  const ROWS = [
    { item: "Apple", latest: 1_400_000, change: 400, tier: "MM" },
    { item: "hops", latest: 900, change: 20, tier: "" },
    { item: "Tomato", latest: 412_900, change: 12_900, tier: "GM" },
  ];

  const names = (rows) => rows.map((row) => row.item);

  test("sorts by item, case-insensitively", () => {
    expect(names(sortIndexRows(ROWS, "item", "asc"))).toEqual(["Apple", "hops", "Tomato"]);
    expect(names(sortIndexRows(ROWS, "item", "desc"))).toEqual(["Tomato", "hops", "Apple"]);
  });

  test("sorts by each numeric column", () => {
    expect(names(sortIndexRows(ROWS, "latest", "asc"))).toEqual(["hops", "Tomato", "Apple"]);
    expect(names(sortIndexRows(ROWS, "change", "desc"))).toEqual(["Tomato", "Apple", "hops"]);
  });

  // The tier is the progress, coarsened -- so the two columns agree, and rows
  // sharing a tier still read in progress order rather than arbitrarily.
  test("sorts by tier exactly as it sorts by the mastery itself", () => {
    const rows = [
      ...ROWS,
      { item: "Corn", latest: 999_999, change: 5, tier: "GM" },
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

describe("timelineHref", () => {
  test("encodes the item into the page's own query string", () => {
    expect(timelineHref("Oak Table")).toBe("/timeline?item=Oak%20Table");
  });
});
