// A bill rule on several days of the month (the 15th and the last day:
// [15, -1]). The wire keeps saying MONTHLY:1 beside by_month_day, because
// clients already in the field send that back on every edit; the server keeps
// the days unless by_month_day names new ones. So the rule's picker value
// lives only here, in the forms, and cadenceFields turns a pick into what a
// save sends. Free of React so the mobile pure-logic suite can check this
// copy and the phone's are the same text.

/** The picker's value for "on the rule's several days". Never sent. */
export const TWICE_A_MONTH = "TWICE_MONTHLY:1";

export function onSeveralDays(days: number[] | null | undefined): days is number[] {
  return Array.isArray(days) && days.length >= 2;
}

/** "twice a month (15th and last day)": the server's phrasing. */
export function severalDaysWords(days: number[]): string {
  const word = (d: number) => {
    if (d < 0) return d === -1 ? "last day" : `${-d} days before month end`;
    const t = d % 100;
    const tail = t >= 11 && t <= 13 ? "th"
      : d % 10 === 1 ? "st" : d % 10 === 2 ? "nd" : d % 10 === 3 ? "rd" : "th";
    return `${d}${tail}`;
  };
  const words = [...days].sort((a, b) => (+(a < 0) - +(b < 0)) || a - b).map(word);
  const list = words.length <= 2 ? words.join(" and ")
    : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
  return `${days.length === 2 ? "twice" : `${days.length} times`} a month (${list})`;
}

/** Where a picker rests for this cadence and these days. */
export function cadencePick(cadence: string,
                            days: number[] | null | undefined): string {
  return onSeveralDays(days) ? TWICE_A_MONTH : cadence;
}

/** A picker's options with the rule's days first, when there are days to
 *  offer. There is none otherwise: a form cannot invent someone's paydays. */
export function withSeveralDays(list: [string, string][],
                                days: number[] | null | undefined
                                ): [string, string][] {
  return onSeveralDays(days)
    ? [[TWICE_A_MONTH, severalDaysWords(days)], ...list] : list;
}

/** What a save sends for a pick. `had` is the rule the bill is on now, `days`
 *  the ones the picker's option stands for (the bill's, or a ledger fit's).
 *  Days travel only when the person changed them; leaving a several-day rule
 *  for plain monthly names the one day, which is the explicit ask the server
 *  needs to drop the other. */
export function cadenceFields(pick: string,
                              had: number[] | null | undefined,
                              days: number[] | null | undefined,
                              due: string | null | undefined
                              ): { cadence: string; by_month_day?: number[] } {
  if (pick === TWICE_A_MONTH) {
    return onSeveralDays(days) && JSON.stringify(days) !== JSON.stringify(had ?? null)
      ? { cadence: "MONTHLY:1", by_month_day: days }
      : { cadence: "MONTHLY:1" };
  }
  if (pick === "MONTHLY:1" && onSeveralDays(had)) {
    return { cadence: pick,
             by_month_day: [parseInt((due ?? "").slice(8, 10), 10) || 1] };
  }
  return { cadence: pick };
}
