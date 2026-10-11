// The email schedule's client-side rules, shared by the Email & Push
// screen and the setup wizard's daily-verdict control. Pure, so the
// logic tests can hold them.
import type { EmailSchedule } from "./api";
import { rebaseKeyed } from "./pure.ts";

export const CADENCES = ["daily", "monthly", "yearly"] as const;
export type Cadence = (typeof CADENCES)[number];
export type CadenceCfg = { on: boolean; hour: number; weekday?: number;
                           sms?: boolean; push?: boolean; summary?: boolean };

/** One cadence with its unset fields filled the way the server reads
 *  them: the daily email is on by default, every other report is off
 *  (the web's schedOf). */
export function cadenceOf(sched: EmailSchedule | null | undefined,
                          cad: Cadence): CadenceCfg {
  return { on: cad === "daily", hour: 7,
           ...((sched?.[cad] ?? {}) as Partial<CadenceCfg>) };
}

/** An edit laid over the schedule stored NOW. The schedule saves whole,
 *  and the copy on screen may be older than what another device saved
 *  since, so only the fields this edit changed (mine against base) move.
 *  The edited cadence is always sent: a cadence missing from the body is
 *  stored as off. */
export function rebaseSched(mine: EmailSchedule, base: EmailSchedule,
                            live: EmailSchedule, edited?: Cadence,
): EmailSchedule {
  const out = rebaseKeyed<object>({ ...mine }, { ...base }, { ...live }) as
    EmailSchedule;
  if (edited && !out[edited] && mine[edited])
    return { ...out, [edited]: mine[edited] } as EmailSchedule;
  return out;
}

/** What a save stored: the reply's schedule, or — from a server whose
 *  reply leaves it out — the schedule that was sent. Never "nothing",
 *  which would read as every report switched off. */
export function storedSched(
  reply: Record<string, unknown> | null | undefined,
  sent: EmailSchedule,
): EmailSchedule {
  const r = reply?.email_schedule as EmailSchedule | null | undefined;
  return r && typeof r === "object" ? r : sent;
}
