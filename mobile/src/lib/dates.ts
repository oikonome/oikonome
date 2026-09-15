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
 *  real weekday column (packing only event days consecutively corrupted
 *  the alignment after the first gap). Mirrors the web's
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
 *  UI: when we last pulled, and when the BANK last fed the aggregator. */
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
