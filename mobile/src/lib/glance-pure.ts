// The home-screen widget's face, decided from data alone — no React
// Native import, so node tests it (scripts/pure-logic-tests.mjs) and the
// Android widget renderer and the app's own preview both read the same
// decision. The iOS widget is Swift (targets/glance) and mirrors these
// rules line for line; the strings it composes are the server's.
// (.ts on purpose: node runs this file in the pure tests)
import { heroLabel } from "./pure.ts";

/** GET /api/today/glance — the Today hero's simple face and nothing
 *  past it. Every string is composed server-side so the widget can never
 *  show a number the app would not. */
export interface Glance {
  date: string;
  budgets_set: boolean;
  verdict?: string;
  left_today?: number;
  day_spent?: number;
  day_allow?: number;
  days_left?: number;
  pace_line?: string;
  chips?: { text: string; tone: string }[];
  as_of: string;
}

/** What the phone keeps between refreshes: the last payload, when it
 *  was fetched (ms since the epoch, the phone's clock) and the stamp of
 *  the widget credential that was current when the fetch began. */
export interface GlanceCache { glance: Glance; fetched_at: number;
                               owner?: string }

/** A payload older than this is shown grey with its time, never as a
 *  fresh verdict: a cached green over a day that has since gone red is
 *  the one thing a glance must not do. */
export const STALE_AFTER_MS = 60 * 60 * 1000;

export type GlanceFace =
  | { kind: "signin" }
  | { kind: "noplan" }
  | { kind: "verdict";
      stale: boolean;
      /** "$47" — whole dollars, like the hero */
      number: string;
      /** "On budget" / "Over budget" / "Under budget" */
      verdict: string;
      tone: "good" | "bad";
      /** "$12/day ahead" — the hero's pace phrase, may be empty */
      pace: string;
      /** "$29 of $76 spent" or "$18 over · 9 days left" */
      sub: string;
      /** day bar fill, 0..1 */
      fill: number;
      /** the day as a whole is over: every allowance spent and then some */
      over: boolean;
      chips: { text: string; neg: boolean }[];
      /** "7:15 AM" — when the payload was fetched */
      asOf: string };

const dollars = (n: number) =>
  "$" + Math.round(Math.abs(n)).toLocaleString("en-US");

/** "7:15 AM" from a phone-clock timestamp, in local time. */
export function clockLabel(ms: number): string {
  const d = new Date(ms);
  const h = d.getHours(), m = d.getMinutes();
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${m < 10 ? "0" : ""}${m} ${h < 12 ? "AM" : "PM"}`;
}

/** Decide the face. `signedIn` is whether a widget credential exists at
 *  all; `now` is the phone's clock. A missing cache with a credential is
 *  drawn as a stale verdict with no numbers — the widget renderers map
 *  that to "…" — rather than "Sign in", which would be a lie. */
export function glanceFace(cache: GlanceCache | null, signedIn: boolean,
                           now: number): GlanceFace {
  if (!signedIn) return { kind: "signin" };
  if (!cache) {
    return { kind: "verdict", stale: true, number: "…", verdict: "",
             tone: "good", pace: "", sub: "refreshing", fill: 0,
             over: false, chips: [], asOf: "" };
  }
  const g = cache.glance;
  if (!g.budgets_set) return { kind: "noplan" };
  const allow = g.day_allow ?? 0, spent = g.day_spent ?? 0;
  const over = spent > allow + 0.5;
  const fill = over ? 1 : allow > 0 ? Math.min(1, spent / allow) : 0;
  const sub = over
    ? `${dollars(spent - allow)} over · ${g.days_left ?? 0} days left`
    : `${dollars(spent)} of ${dollars(allow)} spent`;
  return {
    kind: "verdict",
    stale: now - cache.fetched_at > STALE_AFTER_MS,
    number: dollars(g.left_today ?? 0),
    verdict: heroLabel(g.verdict ?? "ON BUDGET"),
    tone: g.verdict === "OVER BUDGET" ? "bad" : "good",
    pace: g.pace_line ?? "",
    sub, fill, over,
    chips: (g.chips ?? []).map((c) => ({ text: c.text,
                                          neg: c.tone === "neg" })),
    asOf: clockLabel(cache.fetched_at),
  };
}

/** The widget's size class from its cell width in dp: the medium face
 *  (chips beside the number) needs room for two columns. */
export const wideEnoughForChips = (widthDp: number) => widthDp >= 250;

/** Whose numbers the widget's cache holds: the server and the household
 *  the credential was minted for. A cached glance is one household's day,
 *  and both widgets fall back to it on a failed fetch — so when a sign-in
 *  lands on a different owner, the cache is dropped before the new
 *  credential is written. */
export const widgetOwner = (serverUrl: string, tenantId: string) =>
  `${serverUrl.trim().replace(/\/+$/, "").toLowerCase()}|${tenantId}`;

/** Drop the cache unless it is known to be this owner's. No record (a
 *  build that predates the record, or a cleared one) counts as a change:
 *  a cache nobody can vouch for is not shown. */
export const widgetOwnerChanged = (stored: string | null, now: string) =>
  stored !== now;

/** A stamp for the widget credential, kept beside the cached payload so a
 *  cache entry names the credential it was fetched under without holding
 *  the token itself. Equality is all it is for — the cache sits in the
 *  same secure store as the token — so a fast 53-bit string hash does. */
export function credentialStamp(token: string): string {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < token.length; i++) {
    const c = token.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507)
     ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507)
     ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
}

/** The cached glance, only if it was fetched under the credential the
 *  widget holds now. A fetch that was in flight across a sign-out or a
 *  change of household can land after the caches were dropped; its entry
 *  carries the old credential's stamp, so it is never shown under the new
 *  one. An entry with no stamp (an older build) is nobody's. */
export function ownedCache(cache: GlanceCache | null,
                           token: string | null | undefined):
    GlanceCache | null {
  if (!cache || !token || !cache.owner) return null;
  return cache.owner === credentialStamp(token) ? cache : null;
}

/** Whether a fetch's result may be saved: the credential it began under
 *  is still the one the widget holds. A sign-out (no token) or a re-mint
 *  for another household between the fetch and the save skips the write. */
export const saveStillOwned = (startedWith: string | null | undefined,
                               now: string | null | undefined) =>
  !!startedWith && startedWith === now;
