// Bill cadence words and the edit picker's option list, kept free of React so
// the mobile pure-logic suite can run the same cases against this copy and
// the phone's (mobile/src/lib/pure.ts) and check the two are the same text.

/** A FREQ:interval cadence value in the words the bills list uses for it
 *  ("monthly", "every 4 months", "envelope (annual pool)") — the server's
 *  phrasing, so a bill outside the standard list reads the same in the edit
 *  picker as it does in the row it was opened from. */
export function cadenceWords(value: string): string {
  const [freq, raw] = value.toUpperCase().split(":");
  const n = parseInt(raw ?? "1", 10) || 1;
  if (freq === "ENVELOPE")
    return n >= 12 ? "envelope (annual pool)" : "envelope (monthly pool)";
  if (!freq || freq === "ONE_TIME") return "one-time";
  const unit: Record<string, [string, string]> = {
    DAILY: ["daily", "day"], WEEKLY: ["weekly", "week"],
    MONTHLY: ["monthly", "month"], YEARLY: ["yearly", "year"] };
  const u = unit[freq];
  if (!u) return value;
  return n === 1 ? u[0] : `every ${n} ${u[1]}s`;
}

/** The edit picker's options: the server's list, then every cadence the
 *  bill must still be able to land on that the list lacks — the bill's own
 *  saved interval (an "every 4 months" bill), and a ledger-fit prefill or
 *  the current pick. Without the saved one, touching the picker once loses
 *  the bill's real cadence for good. */
export function cadenceChoices(list: [string, string][],
                               keep: (string | null | undefined)[]
                               ): [string, string][] {
  const out = [...list];
  for (const v of keep) {
    if (v && !out.some(([k]) => k === v)) out.push([v, cadenceWords(v)]);
  }
  return out;
}

/** The ADD pickers' short list: the common cadences, one-time, and the
 *  envelopes. Anything unusual (every 2 weeks, every 14 days, every 3
 *  months) is one "Custom interval…" pick away — see CUSTOM_UNITS. The
 *  values are the server's FREQ:interval wire format, never the words. */
export const ADD_CADENCES: [string, string][] = [
  ["DAILY:1", "daily"], ["WEEKLY:1", "weekly"], ["MONTHLY:1", "monthly"],
  ["YEARLY:1", "yearly"], ["ONE_TIME:1", "one-time"],
  ["ENVELOPE:1", "envelope (monthly pool)"],
  ["ENVELOPE:12", "envelope (annual pool)"],
];

/** "Custom interval…" units. Days and weeks are here because a paycheck
 *  every 2 weeks or a bill every 14 days is common, and months/years alone
 *  could not express either. */
export const CUSTOM_UNITS: [string, string][] = [
  ["DAILY", "day(s)"], ["WEEKLY", "week(s)"], ["MONTHLY", "month(s)"],
  ["YEARLY", "year(s)"],
];

/** The largest custom count the server takes for each unit: about 20
 *  years of that unit, because the server bounds an interval by how long
 *  one cycle lasts, not by the bare number. A bill saved past it is
 *  refused, so the picker never sends one. */
export const CUSTOM_MAX: Record<string, number> = {
  DAILY: 7300, WEEKLY: 1043, MONTHLY: 240, YEARLY: 20,
};

/** The cap for one unit. A unit the table does not know gets the smallest
 *  cap, so a guess can never send a count the server refuses. */
export function customMax(unit: string): number {
  return CUSTOM_MAX[unit.toUpperCase()] ?? 20;
}

/** The cadence a custom interval sends: a blank or nonsense count is 1,
 *  and a count past the unit's cap is the cap. */
export function customCadence(unit: string, n: string): string {
  return `${unit}:${Math.min(customMax(unit), Math.max(1, parseInt(n, 10) || 1))}`;
}
