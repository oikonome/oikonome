// Plaid Hosted Link, native: open the bank sign-in in the in-app
// browser and POLL the server for the session's outcome — there is no
// redirect back into the app by design. The in-app browser resolving
// (user closed it) starts a short grace, because ADD-mode exit is only
// detected server-side after its own second look.
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import * as WebBrowser from "expo-web-browser";
import { useEffect, useRef, useSyncExternalStore } from "react";
import { AppState } from "react-native";

import { Client, type Connection } from "./api";
import { firstSyncPending, LINK_BURST_MS, linkPollMayRetry, linkPollWait,
         linkWindowAnswer, linkedLine } from "./pure";

export type PlaidLinkOutcome = {
  kind?: "add" | "add_failed" | "update" | "update_failed" | "exited"
       | "cancelled" | "busy";
  institution?: string; error?: string;
  /** "pending": the bank is saved and its first pull is queued on the
   *  worker — accounts arrive when it lands */
  first_sync?: "pending" | "ok";
};

/** How long a client waits on one link session: the Hosted Link URL's
 *  30 minutes, plus a few for a person who opened it near the end and is
 *  still inside Link. Giving up sooner told the person the link timed out
 *  while the URL still worked. The server finishes the session either way;
 *  this is only how long the app keeps watching for the answer.
 *  (web: ConnectHub PLAID_LINK_WAIT_MS) */
export const PLAID_LINK_WAIT_MS = 35 * 60_000;

/** The link session whose sign-in is on screen in the in-app browser.
 *  Several sessions can be pending at once, and dismissBrowser closes
 *  whatever is open — so a session finishing in the background must not
 *  close the browser another bank's sign-in is using. Set when a
 *  session's URL opens; cleared when that browser is really closed. On
 *  Android the open resolves "opened" at once, so the token stays until a
 *  later session's open replaces it. */
let browserToken: string | null = null;
function openLinkBrowser(token: string, url: string,
                         onAnswer: (closed: boolean) => void): void {
  browserToken = token;
  WebBrowser.openBrowserAsync(url)
    .then((r) => {
      const a = linkWindowAnswer(r?.type);
      if (a.closed && browserToken === token) browserToken = null;
      onAnswer(a.closed);
    })
    .catch(() => {
      if (browserToken === token) browserToken = null;
      onAnswer(true);
    });
}

export async function plaidHostedLink(
  client: Client, itemId = "", manageAccounts = false,
  // add mode only: a linked bank to link afresh with the household's
  // (wider) history window. It stays live and syncing until the new link
  // of the same bank is saved; a sign-in at another bank just adds that
  relinkItemId = "",
): Promise<PlaidLinkOutcome> {
  const started = await client.plaidLinkStart(itemId, manageAccounts,
                                              relinkItemId);
  if (!/^https:/i.test(started.hosted_link_url))
    throw new Error("Plaid returned a non-https link URL");
  // closedAt is stamped only from a real close: iOS resolving the
  // browser, or (Android, whose browser resolves "opened" at once) the app
  // coming back to the foreground after the tab was shown. Only the iOS
  // answer is certain enough to release the session's slot.
  let closedAt: number | null = null;
  let mayReleaseSlot = true;
  let watchReturn = false;
  const appState = AppState.addEventListener("change", (st) => {
    if (!watchReturn) return;
    if (st === "active") { if (closedAt === null) closedAt = Date.now(); }
    else if (st === "background") closedAt = null;  // back in the tab
  });
  openLinkBrowser(started.link_token, started.hosted_link_url, (closed) => {
    if (closed) closedAt = Date.now();
    else { watchReturn = true; mayReleaseSlot = false; }
  });
  try {
    return await pollLink(client, started.link_token, itemId,
                          () => closedAt, () => mayReleaseSlot);
  } finally {
    appState.remove();
  }
}

async function pollLink(
  client: Client, linkToken: string, itemId: string,
  closedAt: () => number | null, mayReleaseSlot: () => boolean,
  cancelled: () => boolean = () => false,
): Promise<PlaidLinkOutcome> {
  // A failed poll backs off and polls again until the deadline (see
  // linkPollMayRetry); only a definite refusal ends the wait early.
  const deadline = Date.now() + PLAID_LINK_WAIT_MS;
  let backoff = 0;
  while (Date.now() < deadline) {
    if (cancelled()) {
      if (!itemId && mayReleaseSlot()) void setPlaidLinkAside(client, linkToken);
      return { kind: "cancelled" };
    }
    let s: Awaited<ReturnType<Client["plaidLinkStatus"]>>;
    try {
      s = await client.plaidLinkStatus(linkToken);
    } catch (e) {
      if (!linkPollMayRetry(e)) throw e;
      backoff = Math.min((backoff || 2500) * 2, 30_000);
      await sleepOrWake(backoff);
      continue;
    }
    backoff = 0;
    if (s.done) {
      // only this session's own sign-in window is ours to close
      if (browserToken === linkToken) {
        browserToken = null;
        WebBrowser.dismissBrowser?.();
      }
      return s;
    }
    // the browser closed and the server still says nothing — give its
    // 6-second exit grace room, then call it closed. "busy" is not
    // nothing: the server is completing this link behind the tenant's
    // sync lock, so keep waiting for it
    const closed = closedAt();
    if (closed !== null && s.kind !== "busy" && Date.now() - closed > 9_000) {
      // the window is gone: the server stops holding a connection slot
      // for it (and still finishes it if Plaid reports a bank after all)
      if (!itemId && mayReleaseSlot()) void setPlaidLinkAside(client, linkToken);
      return { kind: "exited" };
    }
    // the normal tick, or the fast one for a while after the app came
    // back to the foreground (the bank step just ended)
    await sleepOrWake(linkPollWait(Date.now(), burstUntil));
  }
  throw new Error("Timed out waiting for the bank connection");
}

// ---- the links this app is waiting on, as one list every connect door
// reads (the web's components/plaidlinks.ts): a button is busy only
// while the bank's browser opens, each session is a row of its own below
// it with its own cancel and outcome, and the person links the next bank
// or carries on meanwhile.
export type PendingLinkKind = "add" | "relink" | "fix";
export type PendingLink = { token: string; kind: PendingLinkKind;
                            startedAt: number; cancelled: boolean;
                            // a relink's old bank: it stays listed and
                            // live meanwhile, and the server refuses a
                            // second relink of it while this one is open
                            itemId?: string };
export type LinkOutcomeLine = { id: number; at: number; ok: boolean;
                                text: string };
export const OUTCOME_SHOWN_MS = 12_000;

const pending = new Map<string, PendingLink>();
let outcomes: LinkOutcomeLine[] = [];
let nextId = 1;
const subs = new Set<() => void>();
let snap = { pending: [] as PendingLink[], outcomes };
function emit() {
  snap = { pending: [...pending.values()], outcomes };
  for (const f of subs) f();
}
function subscribe(f: () => void) { subs.add(f); return () => { subs.delete(f); }; }
export function usePendingLinks(): { pending: PendingLink[];
                                     outcomes: LinkOutcomeLine[] } {
  return useSyncExternalStore(subscribe, () => snap, () => snap);
}
/** The linked banks with a relink session still open — their "more
 *  history" door stays shut until it lands, since the old row no longer
 *  disappears when the session starts. */
export function useRelinkingItems(): Set<string> {
  const { pending: p } = usePendingLinks();
  return new Set(p.filter((x) => x.kind === "relink" && x.itemId)
                  .map((x) => x.itemId!));
}
/** A session a screen picks up again (usePlaidLinkResume) becomes a
 *  pending row like one started here, so the "more history" door of a
 *  relinking bank stays shut after a restart; `resolvePending` ends it. */
export function markPendingResumed(token: string, kind: PendingLinkKind,
                                   itemId?: string): boolean {
  // false = another poller (a link started on this screen, another
  // screen's watcher) owns the row already; the caller then leaves it
  if (pending.has(token)) return false;
  installWaker();
  pending.set(token, { token, kind, itemId, startedAt: Date.now(),
                       cancelled: false });
  emit();
  return true;
}
export function resolvePending(token: string, r?: PlaidLinkOutcome,
                               kind: PendingLinkKind = "add"): void {
  finishPending(token, r && r.kind !== "cancelled"
                ? linkOutcomeLine(r, kind) : undefined);
}
export function isPendingCancelled(token: string): boolean {
  return !!pending.get(token)?.cancelled;
}

export function cancelPendingLink(token: string): void {
  const p = pending.get(token);
  if (!p) return;
  p.cancelled = true;
  emit();
  nudgePolls();
}
function finishPending(token: string, out?: { ok: boolean; text: string }) {
  pending.delete(token);
  if (out) {
    const o = { id: nextId++, at: Date.now(), ...out };
    outcomes = [...outcomes, o];
    setTimeout(() => { outcomes = outcomes.filter((x) => x.id !== o.id); emit(); },
               OUTCOME_SHOWN_MS);
  }
  emit();
}

// the alarm clock: a poll sleeps between checks, and the app coming
// back to the foreground wakes every sleeper and keeps them fast a while
const wakers = new Set<() => void>();
let burstUntil = 0;
export function nudgePolls(): void {
  burstUntil = Date.now() + LINK_BURST_MS;
  for (const w of [...wakers]) w();
}
function sleepOrWake(ms: number): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const wake = () => {
      if (done) return;
      done = true; wakers.delete(wake); clearTimeout(t); resolve();
    };
    const t = setTimeout(wake, ms);
    wakers.add(wake);
  });
}
let wakerInstalled = false;
function installWaker() {
  if (wakerInstalled) return;
  wakerInstalled = true;
  AppState.addEventListener("change", (st) => {
    if (st === "active" && pending.size) nudgePolls();
  });
}

/** The outcome's words under the button; `relink` reads as an add. */
export function linkOutcomeLine(r: PlaidLinkOutcome, kind: PendingLinkKind
): { ok: boolean; text: string } {
  if (r.kind === "exited" || r.kind === "cancelled")
    return { ok: false, text: kind === "fix"
      ? "No change — the sign-in window was closed."
      : "No bank was linked — the sign-in window was closed." };
  if (r.kind === "add_failed" || r.kind === "update_failed")
    return { ok: false, text: `Link failed: ${r.error ?? "unknown"}` };
  if (r.kind === "update") return { ok: true, text: "Reconnected." };
  return { ok: true, text: linkedLine(r.institution, r.first_sync) };
}

/** Open the bank's sign-in and come back as soon as it is on screen: the
 *  session is watched from here on as a pending row (usePendingLinks),
 *  and `onDone` gets its answer whenever it lands. `relinkItemId` = the
 *  same bank linked afresh for a wider history window; the old link is
 *  released only once the new one of that bank is saved.
 *  What a new bank changes (connections, accounts, the wizard) is for
 *  `onDone` to refresh — the screens differ in what they show. */
export async function startPlaidLink(
  // unused: each caller's onDone refreshes what its screen shows
  client: Client, _qc: QueryClient,
  opts: { kind: PendingLinkKind; relinkItemId?: string },
  onDone: (r: PlaidLinkOutcome) => void,
): Promise<string> {
  installWaker();
  const started = await client.plaidLinkStart("", false,
                                              opts.relinkItemId ?? "");
  if (!/^https:/i.test(started.hosted_link_url))
    throw new Error("Plaid returned a non-https link URL");
  const token = started.link_token;
  pending.set(token, { token, kind: opts.kind, startedAt: Date.now(),
                       cancelled: false,
                       ...(opts.relinkItemId ? { itemId: opts.relinkItemId }
                                             : {}) });
  emit();
  let closedAt: number | null = null;
  let mayReleaseSlot = true;
  let watchReturn = false;
  const appState = AppState.addEventListener("change", (st) => {
    if (!watchReturn) return;
    if (st === "active") { if (closedAt === null) closedAt = Date.now(); }
    else if (st === "background") closedAt = null;
  });
  openLinkBrowser(token, started.hosted_link_url, (closed) => {
    if (closed) closedAt = Date.now();
    else { watchReturn = true; mayReleaseSlot = false; }
  });
  void pollLink(client, token, "", () => closedAt, () => mayReleaseSlot,
                () => !!pending.get(token)?.cancelled)
    .then((r) => {
      finishPending(token, r.kind === "cancelled" ? undefined
                                                  : linkOutcomeLine(r, opts.kind));
      onDone(r);
    })
    .catch((e) => {
      finishPending(token, { ok: false, text: String((e as Error)?.message ?? e) });
    })
    .finally(() => { appState.remove(); });
  return token;
}

/** Tell the server an add session's window is closed, so it stops holding
 *  a place under the institution cap and is not resumed. Best-effort: an
 *  older server just refuses it. */
export function setPlaidLinkAside(client: Client,
                                  linkToken: string): Promise<void> {
  return client.req(`/accounts/plaid/link/${encodeURIComponent(linkToken)}`,
                    { method: "DELETE" })
    .then(() => undefined, () => undefined);
}

/** While a just-linked bank's first sync runs on the worker, the caller's
 *  ["connections"] query polls (the returned interval); when one lands,
 *  what it fills — accounts, the wizard, the verdict — is refreshed. The
 *  web's useFirstSyncLanding. */
export function useFirstSyncLanding(conns: Connection[] | undefined
): number | false {
  const qc = useQueryClient();
  const waiting = useRef<Set<string>>(new Set());
  const key = (conns ?? []).filter(firstSyncPending).map((c) => c.id)
    .join(",");
  useEffect(() => {
    const now = new Set(key ? key.split(",") : []);
    const landed = [...waiting.current].some((id) => !now.has(id));
    waiting.current = now;
    if (landed)
      for (const k of ["connections", "accounts", "me", "today",
                       "onboarding", "onboarding-backfill", "transactions"])
        qc.invalidateQueries({ queryKey: [k] });
  }, [key, qc]);
  return key ? 5000 : false;
}
