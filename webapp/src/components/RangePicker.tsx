// The site-wide timeframe vocabulary. Every over-time toggle offers the
// same eight windows — This month · 1m · 3m · 6m · 1y · 3y · 5y · All — so a
// reader never relearns a picker per page (Net Worth set the convention;
// Cash Flow added the two short windows and the rest follow). "This month"
// is the household's month so far; "1m" is the last COMPLETE month, the one
// window that closes before today. Both are months on the same clock as
// the rest, cut server-side where the server windows
// (reporting.FLOW_RANGES) and on the calendar month here. The mobile twin
// of the constants is mobile/src/lib/pure.ts (TREND_RANGES) — keep them
// identical.
import type { CSSProperties } from "react";
import SegmentedControl from "./SegmentedControl";

export const RANGES =
  ["cur", "1m", "3m", "6m", "1y", "3y", "5y", "all"] as const;
export type Range = (typeof RANGES)[number];
/** The window every over-time toggle opens on (Net Worth, Cash Flow).
 *  Mobile twin: `DEFAULT_TREND_RANGE` in lib/pure.ts. */
export const DEFAULT_RANGE: Range = "1y";
export const RANGE_LABEL: Record<Range, string> =
  { cur: "This month", "1m": "1m", "3m": "3m", "6m": "6m", "1y": "1y",
    "3y": "3y", "5y": "5y", all: "All" };
/** How a caption names the window a figure was measured over ("+$120
 *  over this month"). Mobile twin: `rangeCaption` in lib/pure.ts. */
export function rangeCaption(range: Range): string {
  return range === "all" ? "all time" : range === "cur" ? "this month"
    : range === "1m" ? "last month" : range;
}
/** The words after a change measured over a window: "over 1y", "over all
 *  time", and for the two calendar-month windows "this month" / "last
 *  month" ("over this month" reads as nothing a person says). Mobile twin:
 *  `overCaption` in lib/pure.ts. */
export function overCaption(range: Range): string {
  return range === "cur" || range === "1m" ? rangeCaption(range)
    : `over ${rangeCaption(range)}`;
}
// months back from the household's current month, inclusive. "1m" counts
// two so a window cut on the current month reaches the last complete one
// — the cash graph's history stops before the current month, so the two
// short windows differ by whether the current month is in the picture.
export const RANGE_MONTHS: Record<Range, number | null> =
  { cur: 1, "1m": 2, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, all: null };

// ---- counting months backwards, safely ----
// Date#setUTCMonth keeps the day-of-month, so shifting a 31st back into a
// 30-day month rolls FORWARD into the next one (Mar 31 − 1 month = Mar 3).
// Every window built that way silently loses its oldest month on the last
// days of a long month. Both helpers below anchor or clamp the day first,
// which is what the server does (engine/reporting.py `_range_window`).
// Mobile's twins are `rangeCutoffMonth`/`shiftMonthsUtc` in
// mobile/src/lib/pure.ts — keep them identical.

/** The first month a range's window includes, as "YYYY-MM", counted back
 *  from `today`. null for "all", which has no cutoff. */
export function rangeCutoffMonth(range: Range, today: Date = new Date()):
    string | null {
  const months = RANGE_MONTHS[range];
  if (months === null) return null;
  return monthsBackFrom(
    // anchored to the 1st, so the day-of-month cannot overflow
    new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))
      .toISOString().slice(0, 7),
    months);
}

/** The first month of a `months`-long window whose LAST month is `anchor`
 *  ("YYYY-MM"). A 3-month window ending 2026-08 starts 2026-06. */
export function monthsBackFrom(anchor: string, months: number): string {
  const d = new Date(Date.UTC(Number(anchor.slice(0, 4)),
                              Number(anchor.slice(5, 7)) - 1, 1));
  d.setUTCMonth(d.getUTCMonth() - (months - 1));
  return d.toISOString().slice(0, 7);
}

// ---- whose "now" a window is cut on ----
// The browser's clock is not the household's: a reader in another timezone
// is in a different month for hours at each month boundary. So the Cash
// Flow chart is not cut here at all — the server names the months each
// range draws (`cash_graph.windows`, reporting._drawn_windows), windowed
// from the household's own date like every other figure on the page.

/** `iso` moved `months` months earlier, in the shape it came in: a
 *  "YYYY-MM-DD" keeps its day, clamped to the target month's length (May 31
 *  − 3 months is Feb 28, not Mar 3); a "YYYY-MM" month key — the net-worth
 *  trend's points — stays a month key. A month key has no day to read, and
 *  treating the missing day as a number makes an invalid Date whose
 *  toISOString() throws, taking the whole Net Worth screen down. */
export function shiftMonthsUtc(iso: string, months: number): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!Number.isFinite(d)) {
    return new Date(Date.UTC(y, m - 1 - months, 1)).toISOString().slice(0, 7);
  }
  // day 0 of the month AFTER the target = the target's last day
  const last = new Date(Date.UTC(y, m - months, 0)).getUTCDate();
  return new Date(Date.UTC(y, m - 1 - months, Math.min(d, last)))
    .toISOString().slice(0, 10);
}

export function RangePicker({ value, onChange, dead, options, size, style }: {
  value: Range; onChange: (r: Range) => void;
  // a range longer than the data equals All — the caller can mark it so
  // the picker doesn't draw two buttons for one view
  dead?: (r: Range) => boolean;
  // the eight site-wide windows by default; a caller may offer fewer
  options?: readonly Range[];
  size?: "sm";
  // the control is its own flex item: a wrapper around it would size to
  // the full row of options and push the page wider than a phone
  style?: CSSProperties;
}) {
  return (
    <SegmentedControl label="Timeframe" value={value} onChange={onChange}
      size={size} style={style}
      options={(options ?? RANGES).map((r) => ({
        value: r, label: RANGE_LABEL[r],
        disabled: r !== "all" && !!dead?.(r),
      }))} />
  );
}

// ---- months with no bank feed ----
// Cash Flow's month series carry only the months whose income is known: a
// month no bank account posted in (a broken link, a missing statement) is
// left out by the server rather than drawn as a loss. A chart must still
// keep that month's place, or March sits beside July and the gap reads as
// time that never passed. These put each missing month back as a null —
// drawn as an empty, labelled slot ("no bank feed").
/** Fill missing 'YYYY-MM' keys between consecutive points with null. */
export function fillMonthKeyGaps(
  points: [string, number][],
): [string, number | null][] {
  const out: [string, number | null][] = [];
  const next = (k: string) => {
    const y = Number(k.slice(0, 4)), m = Number(k.slice(5, 7));
    return m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}`;
  };
  for (const p of points) {
    const prev = out.length ? out[out.length - 1][0] : null;
    if (prev !== null && /^\d{4}-\d{2}$/.test(prev)
        && /^\d{4}-\d{2}$/.test(p[0])) {
      // bounded: a malformed or reversed pair never loops
      for (let k = next(prev), n = 0; k < p[0] && n < 1200; k = next(k), n++) {
        out.push([k, null]);
      }
    }
    out.push(p);
  }
  return out;
}

const MONTH_ABBRS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                     "Aug", "Sep", "Oct", "Nov", "Dec"];

/** One year's bars, labelled 'Jan'..'Dec': fill the missing months between
 *  the first and the last bar with null. */
export function fillMonthLabelGaps(
  points: [string, number][],
): [string, number | null][] {
  const at = new Map(points.map((p) => [p[0], p[1]] as const));
  const idx = points.map((p) => MONTH_ABBRS.indexOf(p[0]));
  if (!points.length || idx.some((i) => i < 0)) return points;
  const out: [string, number | null][] = [];
  for (let i = Math.min(...idx); i <= Math.max(...idx); i++) {
    const m = MONTH_ABBRS[i];
    out.push([m, at.has(m) ? at.get(m)! : null]);
  }
  return out;
}
