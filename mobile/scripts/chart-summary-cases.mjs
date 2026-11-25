// What a chart says in words, one case table for both clients: the web's
// webapp/src/chartsummary.ts and the phone's mobile/src/lib/chart-summary.ts
// (server/tests/test_charts_say_their_figures.py runs it against each, and
// scripts/pure-logic-tests.mjs against the phone's). Every figure in a
// sentence or a table row is one of the points as given, printed by the
// formatter passed in — a summed or averaged figure would show up as a
// number no point had.
export function chartSummaryCases(impl, assert) {
  const fmt = (v) => `$${v.toLocaleString("en-US")}`;
  const months = (vals, from = [2025, 12]) => vals.map((v, i) => {
    const m = from[1] - 1 + i;
    const y = from[0] + Math.floor(m / 12);
    return [`${y}-${String((m % 12) + 1).padStart(2, "0")}`, v];
  });
  return {
    "a month, a day and any other label in words": () => {
      assert.equal(impl.pointName("2026-11"), "November 2026");
      assert.equal(impl.pointName("2026-11-04"), "November 4, 2026");
      assert.equal(impl.pointName("2026-11-04", false), "November 4");
      assert.equal(impl.pointName("2031"), "2031");
      assert.equal(impl.pointName("Jan 26'"), "Jan 26'");
      assert.equal(impl.pointName("2026-13"), "2026-13");
    },
    "the owner's example sentence": () => {
      const pts = months([41200, 41900, 42750, 43120, 45310, 45980, 46440, 47050,
                          47880, 48210, 49300, 48900]);
      assert.equal(impl.seriesSentence("Net worth over the last 12 months", pts, fmt),
        "Net worth over the last 12 months: $41,200 in December 2025, "
        + "$48,900 in November 2026; highest $49,300 in October.");
    },
    "a high or low at an end is not said twice": () => {
      const s = impl.seriesSentence("Spending", months([100, 300, 200]), fmt);
      assert.equal(s, "Spending: $100 in December 2025, $200 in February 2026; "
        + "highest $300 in January.");
      const rising = impl.seriesSentence("Saved", months([1, 2, 3]), fmt);
      assert.equal(rising, "Saved: $1 in December 2025, $3 in February 2026.");
    },
    "a month that repeats keeps its year": () => {
      const pts = months([5, 9, 1, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 6]);
      const s = impl.seriesSentence("Income", pts, fmt);
      assert.match(s, /highest \$9 in January 2026; lowest \$1 in February\.$/);
    },
    "days say 'on', forecast points are marked, a gap is skipped": () => {
      const pts = [["2026-11-01", 10], ["2026-11-02", null], ["2026-11-03", -4],
                   ["2026-11-04", 2]];
      assert.equal(impl.seriesSentence("Cash", pts, fmt, 3),
        "Cash: $10 on November 1, 2026, $2 on November 4, 2026 (forecast); "
        + "lowest $-4 on November 3.");
      assert.equal(impl.seriesSentence("Cash", [["Nov", null]], fmt), "Cash: no figures yet.");
      assert.equal(impl.seriesSentence("Cash", [["Nov", 7]], fmt), "Cash: $7 in Nov.");
    },
    "every figure said is a point as given": () => {
      const pts = months([1234.5, 99.25, 777]);
      const said = impl.seriesSentence("X", pts, (v) => `<${v}>`);
      for (const m of said.matchAll(/<([^>]+)>/g))
        assert.ok(pts.some(([, v]) => String(v) === m[1]), m[1]);
    },
    "several series: the first under the subject, the rest by name": () => {
      const a = months([3, 2]), b = months([5, 1]);
      assert.equal(impl.chartSentence("Owed", [{ name: "Avalanche", points: a },
                                               { name: "Snowball", points: b }], fmt),
        "Owed: $3 in December 2025, $2 in January 2026. "
        + "Snowball: $5 in December 2025, $1 in January 2026.");
    },
    "the table lists every point, a long daily run by month end": () => {
      const t = impl.chartTable([{ name: "Saved", points: [["Jan", 5], ["Feb", null], ["Mar", 7]] }],
                                fmt, { forecastFrom: 2, missing: "no bank feed" });
      assert.deepEqual(t, { columns: ["Point", "Saved"],
        rows: [["Jan", "$5"], ["Feb", "no bank feed"], ["Mar (forecast)", "$7"]] });
      const days = [];
      for (let d = 0; d < 90; d++) {
        const dt = new Date(Date.UTC(2026, 0, 1 + d));
        days.push([dt.toISOString().slice(0, 10), d]);
      }
      const long = impl.chartTable([{ name: "Net worth", points: days }], fmt);
      assert.equal(long.columns[0], "Date");
      assert.deepEqual(long.rows.map((r) => r[0]),
        ["January 31, 2026", "February 28, 2026", "March 31, 2026"]);
      assert.deepEqual(long.rows.map((r) => r[1]), ["$30", "$58", "$89"]);
      const mon = impl.chartTable([{ name: "Spending", points: months([1, 2]) }], fmt);
      assert.equal(mon.columns[0], "Month");
    },
    "parts of a whole in words, shares as printed": () => {
      assert.equal(impl.partsSentence([
        { head: "Where the money came from",
          parts: [{ label: "Paychecks", figure: "$5,000", share: 83 },
                  { label: "Overspent by", figure: "$200", share: null }] },
        { head: "Nothing", parts: [] },
        { head: "Where it went", parts: [{ label: "Fixed bills", figure: "$3,000", share: 50 }] }]),
        "Where the money came from: Paychecks $5,000 (83%), Overspent by $200. "
        + "Where it went: Fixed bills $3,000 (50%).");
    },
    "arrow keys walk the points, Home and End the ends": () => {
      assert.equal(impl.stepPoint("ArrowRight", null, 5), 0);
      assert.equal(impl.stepPoint("ArrowLeft", null, 5), 4);
      assert.equal(impl.stepPoint("ArrowRight", 4, 5), 4);
      assert.equal(impl.stepPoint("ArrowLeft", 0, 5), 0);
      assert.equal(impl.stepPoint("ArrowUp", 1, 5), 2);
      assert.equal(impl.stepPoint("Home", 3, 5), 0);
      assert.equal(impl.stepPoint("End", 0, 5), 4);
      assert.equal(impl.stepPoint("Tab", 2, 5), null);
      assert.equal(impl.stepPoint("End", null, 0), null);
    },
    "a hovered day lists every line drawn, values as given": () => {
      const a = { name: "Your usual", points: [["2026-11-01", 900], ["2026-11-02", 850]] };
      const b = { name: "heavier month", points: [["2026-11-01", 900], ["2026-11-02", null]] };
      const c = { name: "Pay all cards now", points: [["2026-11-01", 400]] };
      assert.deepEqual(impl.hoverRows([a, b, c], 0), [
        { name: "Your usual", value: 900 }, { name: "heavier month", value: 900 },
        { name: "Pay all cards now", value: 400 }]);
      assert.deepEqual(impl.hoverRows([a, b, c], 1), [{ name: "Your usual", value: 850 }]);
    },
  };
}
