// Plaid Hosted Link, native: open the bank sign-in in the in-app
// browser and POLL the server for the session's outcome — there is no
// redirect back into the app by design. The in-app browser resolving
// (user closed it) starts a short grace, because ADD-mode exit is only
// detected server-side after its own second look.
import { useQueryClient } from "@tanstack/react-query";
import * as WebBrowser from "expo-web-browser";
import { useEffect, useRef } from "react";
import { AppState } from "react-native";

import { Client, type Connection } from "./api";
import { firstSyncPending, linkPollMayRetry, linkWindowAnswer } from "./pure";

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

export async function plaidHostedLink(
  client: Client, itemId = "", manageAccounts = false,
): Promise<PlaidLinkOutcome> {
  const started = await client.plaidLinkStart(itemId, manageAccounts);
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
  WebBrowser.openBrowserAsync(started.hosted_link_url)
    .then((r) => {
      const a = linkWindowAnswer(r?.type);
      if (a.closed) closedAt = Date.now();
      else { watchReturn = true; mayReleaseSlot = false; }
    })
    .catch(() => { closedAt = Date.now(); });
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
): Promise<PlaidLinkOutcome> {
  // A failed poll backs off and polls again until the deadline (see
  // linkPollMayRetry); only a definite refusal ends the wait early.
  const deadline = Date.now() + PLAID_LINK_WAIT_MS;
  let wait = 2500;
  while (Date.now() < deadline) {
    let s: Awaited<ReturnType<Client["plaidLinkStatus"]>>;
    try {
      s = await client.plaidLinkStatus(linkToken);
    } catch (e) {
      if (!linkPollMayRetry(e)) throw e;
      wait = Math.min(wait * 2, 30_000);
      await new Promise((r) => setTimeout(r, wait));
      continue;
    }
    wait = 2500;
    if (s.done) {
      WebBrowser.dismissBrowser?.();
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
    await new Promise((r) => setTimeout(r, wait));
  }
  throw new Error("Timed out waiting for the bank connection");
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
