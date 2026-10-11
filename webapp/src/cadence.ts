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
