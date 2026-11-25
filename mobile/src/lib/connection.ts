// Whether the phone can reach its server, from what the app already knows
// (specs/screen-states.md, "Offline saves"): every read and write settles
// through the query client, and one that got no answer at all marks the
// phone offline; any answer marks it back online. A cheap look at the
// health door on every return to the app (components/offline.tsx) and,
// while offline, every few seconds, ends the offline state without
// waiting for a screen to ask for something. No native network module.
//
// React-free so the logic tests can drive it against a real QueryClient;
// the hook and the note live in components/offline.tsx.
import type { QueryClient } from "@tanstack/react-query";

import { failureStatus, fetchRead, offlineAfter } from "./pure.ts";

type Listener = () => void;

let offline = false;
const listeners = new Set<Listener>();

// Evidence is ordered by when each attempt STARTED, not when it settled:
// a request or a look that went out before the latest answer and then got
// none is stale (a dying socket, a look that gave up after a refetch was
// answered) and must not re-mark the phone offline. One counter orders
// attempt starts and answers alike.
let clock = 0;
let lastAnswer = 0;
const startOf = new WeakMap<object, number>();
let probing = false;

/** Stamp an attempt's start: call just before the request goes out. */
export function attemptStarted(): number {
  return ++clock;
}

/** Tie what a request threw to its attempt's start, so the failure is
 *  weighed against the answers that came after it went out. Returns `e`. */
export function markAttempt(e: unknown, started: number): unknown {
  if (e !== null && typeof e === "object") startOf.set(e, started);
  return e;
}

/** `fetchFn` with its rejection stamped by when the attempt started —
 *  every request the api client makes goes through here, so it is also
 *  the one place the phone puts a limit on a read (specs/screen-states.md,
 *  "A read that never answers"): a GET with no answer in READ_LIMIT_MS
 *  fails as a request with no answer does, and so marks the phone
 *  offline like one. A write is never timed (fetchRead says why). The
 *  caller hands the response to readFinished once its body is read. */
export async function fetchStamped(input: string, init?: RequestInit,
                                   fetchFn: typeof fetch = fetch)
    : Promise<Response> {
  const started = attemptStarted();
  try {
    return await fetchRead((signal) => fetchFn(input, { ...init, signal }),
                           init?.method, init?.signal);
  } catch (e) {
    throw markAttempt(e, started);
  }
}

/** The current answer: true only after a request got no answer and none
 *  has been answered since. */
export function isOffline(): boolean {
  return offline;
}

export function subscribeOffline(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** One request settled: `e` null for an answer, else what it threw.
 *  `started` (else the stamp on `e`) is when its attempt went out; a
 *  failure that is not an answer from before the latest answer is
 *  ignored. */
export function noteSettled(e: unknown, started?: number): void {
  if (e === null || failureStatus(e) !== null) {
    lastAnswer = ++clock;
  } else {
    const at = started ?? (typeof e === "object" ? startOf.get(e) : undefined);
    if (at !== undefined && at < lastAnswer) return;
  }
  const next = offlineAfter(offline, e);
  if (next === offline) return;
  offline = next;
  for (const fn of [...listeners]) fn();
}

/** Back to the start (a sign-out, a test). */
export function resetConnection(): void {
  if (!offline) return;
  offline = false;
  for (const fn of [...listeners]) fn();
}

/** Follow every fetch the client settles. A query's failed attempt
 *  counts as soon as it fails (not after its retries), so a write button
 *  greys within one attempt of the connection going. When the phone
 *  comes back, the reads that failed meanwhile and are on screen are
 *  asked again (the web's refetch on reconnect). Returns the unbind. */
export function bindConnection(qc: QueryClient): () => void {
  const unQ = qc.getQueryCache().subscribe((ev) => {
    if (ev.type !== "updated") return;
    const a = ev.action as { type: string; error?: unknown; manual?: boolean };
    // a hand-written cache patch (setQueryData) is not an answer
    if (a.type === "success" && !a.manual) noteSettled(null);
    else if (a.type === "failed" || a.type === "error") noteSettled(a.error);
  });
  const unM = qc.getMutationCache().subscribe((ev) => {
    if (ev.type !== "updated") return;
    const a = ev.action as { type: string; error?: unknown };
    if (a.type === "success") noteSettled(null);
    else if (a.type === "error") noteSettled(a.error);
  });
  const unBack = subscribeOffline(() => {
    if (offline) return;
    void qc.refetchQueries({ type: "active",
                             predicate: (q) => q.state.status === "error" });
  });
  return () => { unQ(); unM(); unBack(); };
}

/** Ask the server's health door whether it can be reached at all: any
 *  answer is online (even a 5xx — the phone reached something); none
 *  within `timeoutMs` is offline. A look already in flight is not
 *  started again. */
export async function probeReach(baseUrl: string, timeoutMs = 5000,
                                 fetchFn: typeof fetch = fetch): Promise<void> {
  if (probing) return;
  probing = true;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  const started = attemptStarted();
  try {
    await fetchFn(baseUrl + "/healthz",
                  { signal: ctl.signal,
                    headers: { Accept: "application/json",
                               "Cache-Control": "no-store" } });
    noteSettled(null);
  } catch (e) {
    // a look that timed out got no answer, the same as a refused socket
    noteSettled(ctl.signal.aborted ? new TypeError("no answer in time") : e,
                started);
  } finally {
    clearTimeout(timer);
    probing = false;
  }
}
