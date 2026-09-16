/** Did the person deliberately leave the setup wizard this session?
 *
 *  Landing on the app root with setup unfinished sends you into the wizard
 *  (see App.tsx). Without that redirect the only route into the guided
 *  setup is a green "Finish setup" pill in the nav, which is easy not to
 *  notice and, on a phone, sits behind the menu — so a new account meets
 *  an empty dashboard instead of the walkthrough.
 *
 *  But "send them in" must never mean "trap them there". Pressing
 *  "save & finish later" (or finishing) navigates to the root, which is
 *  exactly the path that redirects — so without this flag the wizard would
 *  bounce them straight back and there would be no way out at all.
 *
 *  sessionStorage, deliberately: the decision lasts as long as the tab. A
 *  new sign-in offers the wizard again, which is the right default while
 *  setup is genuinely unfinished, and nothing outlives the browser session.
 */
const KEY = "oiko-wizard-left";

/** Scoped to the ACCOUNT, not just the tab.
 *
 *  Signing out is a full navigation (`window.location.href = "/login"`),
 *  which does not clear sessionStorage — so user A leaving the wizard and
 *  handing the laptop to user B would send B's brand-new, genuinely
 *  unfinished account straight past setup: the very thing the redirect
 *  exists to prevent, one account along. Keying on the tenant makes the
 *  decision belong to whoever made it. */
function key(scope?: string): string {
  return scope ? `${KEY}:${scope}` : KEY;
}

export function leftWizard(scope?: string): void {
  try { sessionStorage.setItem(key(scope), "1"); } catch { /* private mode */ }
}

export function hasLeftWizard(scope?: string): boolean {
  try { return sessionStorage.getItem(key(scope)) === "1"; }
  catch { return false; }
}
