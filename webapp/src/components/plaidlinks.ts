// The bank links this tab is waiting on, as one list every connect door
// reads — so a button is busy only while the bank tab opens, and each
// open session is a row of its own below it (its own cancel, its own
// outcome) while the person links the next bank or carries on.
//
// Also the pollers' alarm clock: a poll sleeps between status checks,
// and anything that suggests the bank step just finished — this tab
// regaining focus, the completion page in the bank tab calling over —
// wakes every sleeper at once and makes them poll fast for a while, so
// the row flips to a bank within a second of the handoff instead of
// after the next tick. Mobile keeps the same list in lib/plaid.ts.
import { useSyncExternalStore } from "react";

export type PendingLinkKind = "add" | "relink" | "fix";
export type PendingLink = {
  token: string; kind: PendingLinkKind; itemId?: string;
  startedAt: number; cancelled: boolean;
};
export type LinkOutcome = { id: number; at: number; ok: boolean; text: string };

/** The channel the completion page posts on (pages/PlaidDone.tsx). */
export const LINK_DONE_CHANNEL = "oikonome-plaid-link";
/** How long an outcome line stays under the button before it goes:
 *  the bank appearing in the linked list is the confirmation, the line
 *  only covers the moment before that list has refetched. */
export const OUTCOME_SHOWN_MS = 12_000;
/** After a wake-up, polls run this fast for this long. */
export const BURST_MS = 30_000;
export const POLL_FAST_MS = 1000;
export const POLL_MS = 2500;

const pending = new Map<string, PendingLink>();
let outcomes: LinkOutcome[] = [];
let nextId = 1;
const subs = new Set<() => void>();
let snap = { pending: [] as PendingLink[], outcomes };

function emit() {
  snap = { pending: [...pending.values()], outcomes };
  for (const f of subs) f();
}
function subscribe(f: () => void) { subs.add(f); return () => { subs.delete(f); }; }

/** The pending sessions and the recent outcomes, live. */
export function usePlaidLinks(): { pending: PendingLink[];
                                   outcomes: LinkOutcome[] } {
  return useSyncExternalStore(subscribe, () => snap, () => snap);
}

export function markPending(token: string, kind: PendingLinkKind,
                            itemId?: string): void {
  installWakers();
  pending.set(token, { token, kind, itemId, startedAt: Date.now(),
                       cancelled: false });
  emit();
}
export function isPending(token: string): boolean { return pending.has(token); }
export function isCancelled(token: string): boolean {
  return !!pending.get(token)?.cancelled;
}
/** The person says they closed the bank window: the poll ends on its
 *  next look (and the server stops holding the session's slot). */
export function cancelPending(token: string): void {
  const p = pending.get(token);
  if (!p) return;
  p.cancelled = true;
  emit();
  nudgePolls();
}
/** A session's answer: the row leaves, the outcome line stays a moment. */
export function finishPending(token: string, outcome?: { ok: boolean;
                                                          text: string }): void {
  pending.delete(token);
  if (outcome) {
    const o = { id: nextId++, at: Date.now(), ...outcome };
    outcomes = [...outcomes, o];
    setTimeout(() => {
      outcomes = outcomes.filter((x) => x.id !== o.id);
      emit();
    }, OUTCOME_SHOWN_MS);
  }
  emit();
}

// ---- the alarm clock ----
const wakers = new Set<() => void>();
let burstUntil = 0;

/** The next poll's wait: fast inside a burst, else the normal tick. */
export function pollWait(now = Date.now()): number {
  return now < burstUntil ? POLL_FAST_MS : POLL_MS;
}

/** Wake every sleeping poll now and keep them fast for BURST_MS. */
export function nudgePolls(): void {
  burstUntil = Date.now() + BURST_MS;
  for (const w of [...wakers]) w();
}

/** Sleep `ms`, or less when a nudge arrives. */
export function sleepOrWake(ms: number): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const wake = () => {
      if (done) return;
      done = true;
      wakers.delete(wake);
      clearTimeout(t);
      resolve();
    };
    const t = setTimeout(wake, ms);
    wakers.add(wake);
  });
}

let wakersInstalled = false;
/** Once: the tab coming back (the person returned from the bank's tab)
 *  and the completion page's message both wake the polls. */
function installWakers(): void {
  if (wakersInstalled || typeof window === "undefined") return;
  wakersInstalled = true;
  const onBack = () => { if (pending.size) nudgePolls(); };
  window.addEventListener("focus", onBack);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") onBack();
  });
  try {
    const ch = new BroadcastChannel(LINK_DONE_CHANNEL);
    ch.onmessage = () => nudgePolls();
  } catch { /* no BroadcastChannel: the focus wake-up still serves */ }
}

/** The row's words for a session still open. */
export function pendingLine(p: PendingLink): string {
  const head = p.kind === "fix" ? "Finishing the sign-in at your bank…"
    : p.kind === "relink" ? "Relinking at your bank for more history…"
    : "Finish signing in at your bank…";
  return p.cancelled ? `${head} (closing)` : head;
}
