// Plaid Hosted Link, native: open the bank sign-in in the in-app
// browser and POLL the server for the session's outcome — there is no
// redirect back into the app by design. The in-app browser resolving
// (user closed it) starts a short grace, because ADD-mode exit is only
// detected server-side after its own second look.
import * as WebBrowser from "expo-web-browser";

import { Client } from "./api";
import { linkPollMayRetry } from "./pure";

export type PlaidLinkOutcome = {
  kind?: "add" | "add_failed" | "update" | "update_failed" | "exited"
       | "cancelled" | "busy";
  institution?: string; error?: string;
};

export async function plaidHostedLink(
  client: Client, itemId = "", manageAccounts = false,
): Promise<PlaidLinkOutcome> {
  const started = await client.plaidLinkStart(itemId, manageAccounts);
  if (!/^https:/i.test(started.hosted_link_url))
    throw new Error("Plaid returned a non-https link URL");
  let closedAt: number | null = null;
  WebBrowser.openBrowserAsync(started.hosted_link_url)
    .then(() => { closedAt = Date.now(); })
    .catch(() => { closedAt = Date.now(); });
  // A failed poll backs off and polls again until the deadline (see
  // linkPollMayRetry); only a definite refusal ends the wait early.
  const deadline = Date.now() + 15 * 60_000;
  let wait = 2500;
  while (Date.now() < deadline) {
    let s: Awaited<ReturnType<Client["plaidLinkStatus"]>>;
    try {
      s = await client.plaidLinkStatus(started.link_token);
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
    if (closedAt && s.kind !== "busy" && Date.now() - closedAt > 9_000)
      return { kind: "exited" };
    await new Promise((r) => setTimeout(r, wait));
  }
  throw new Error("Timed out waiting for the bank connection");
}
