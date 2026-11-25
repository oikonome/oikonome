// Local calendar-date helpers. toISOString() converts to UTC first, so
// a Y-M-D string built through it reports yesterday all evening west of
// Greenwich, and lands a day off after any local-Date arithmetic east
// of it — every on-screen date string must come from the local
// getFullYear/getMonth/getDate parts instead. Dependency-free so the
// pure-logic tests can pin the day-boundary behaviour under plain node.

/** local Y-M-D for a Date — never toISOString().slice(0, 10) */
export const ymd = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${
    String(d.getDate()).padStart(2, "0")}`;

/** the Y-M-D string `days` calendar days away from `date`, in local
 *  time — month, year and DST boundaries handled by Date itself */
export const shiftYmd = (date: string, days: number): string => {
  const d = new Date(date + "T00:00:00");
  d.setDate(d.getDate() + days);
  return ymd(d);
};

/** The 5-week grid's cells: `count` consecutive dates from the Monday
 *  of `start`'s week, each looked up in the event days by date — a day
 *  without events still occupies its cell, so every date sits under its
 *  real weekday column (packing only event days consecutively would
 *  break the alignment after the first gap). Mirrors the web's
 *  UpcomingCalendar cell construction. */
export function calendarCells<T extends { date: string }>(
  days: T[], start: string, count = 35): { date: string; d?: T }[] {
  const byDate = new Map(days.map((d) => [d.date, d]));
  const monday = new Date(start + "T00:00:00");
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  return Array.from({ length: count }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    const key = ymd(d);
    return { date: key, d: byDate.get(key) };
  });
}

/** "2 min ago" — coarse on purpose. Reads two different clocks in the
 *  UI: when the app last pulled, and when the BANK last fed the aggregator. */
export function ago(iso: string): string {
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 90) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
  return `${Math.round(sec / 86400)} d ago`;
}

/** A month's calendar as 6 Monday-first weeks of Y-M-D strings, padded
 *  with the neighbouring months' days so every date sits under its real
 *  weekday column (the Bills grid's rule). `inMonth` marks the padding. */
export function monthGrid(y: number, m1: number):
    { date: string; inMonth: boolean }[][] {
  const first = new Date(y, m1 - 1, 1);
  const start = new Date(first);
  start.setDate(1 - ((first.getDay() + 6) % 7));
  const weeks: { date: string; inMonth: boolean }[][] = [];
  for (let w = 0; w < 6; w++) {
    const row = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + w * 7 + i);
      row.push({ date: ymd(d), inMonth: d.getMonth() === m1 - 1 });
    }
    weeks.push(row);
  }
  return weeks;
}

// Above this text size a seventh of the screen cannot hold "+$1,250": the
// month is listed as days in words instead of drawn as a grid
// (specs/accessibility.md, decision 4). Below it the cells wrap.
export const CALENDAR_LIST_SCALE = 1.3;

/** One listed day in words — "Tue 12: +$1,250 in, $86 out" — from the
 *  server's in and out for that day; an older server sends only the net,
 *  which is then what the grid cell would have shown. */
/** A calendar day's two figures as its grid cell prints them, each with
 *  its sign and never netted: "+$2,400" in, "−$15" out (the web cell
 *  prints the same). Empty for a server that sends no in and out. */
export function calendarCellFigures(day: { came_in?: number; went_out?: number },
                                    fmt: (v: number) => string):
    { text: string; tone: "in" | "out" }[] {
  const out: { text: string; tone: "in" | "out" }[] = [];
  if ((day.came_in ?? 0) > 0) out.push({ text: `+${fmt(day.came_in!)}`, tone: "in" });
  if ((day.went_out ?? 0) > 0) out.push({ text: `−${fmt(day.went_out!)}`, tone: "out" });
  return out;
}

export function calendarDayLine(day: { date: string; total: number;
                                       came_in?: number; went_out?: number },
                                fmt: (v: number) => string): string {
  const d = new Date(`${day.date}T12:00:00`);
  const name = `${d.toLocaleDateString("en-US", { weekday: "short" })} ${Number(day.date.slice(8, 10))}`;
  if (day.came_in === undefined || day.went_out === undefined)
    return `${name}: ${day.total > 0 ? "+" : ""}${fmt(Math.abs(day.total))}`;
  const parts = [];
  if (day.came_in > 0) parts.push(`+${fmt(day.came_in)} in`);
  if (day.went_out > 0) parts.push(`${fmt(day.went_out)} out`);
  return `${name}: ${parts.join(", ") || fmt(0)}`;
}
