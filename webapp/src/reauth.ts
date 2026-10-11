// Which 401 replies are a step-up refusal rather than a lost session, kept
// free of React so the mobile pure-logic suite runs the same cases against
// this copy and the phone's (mobile/src/lib/pure.ts).
//
// A wrong password, a missing or spent one-time code, or a refused passkey
// on an in-app form is a 401 too, and it means "try again", not "your
// session is gone". Missing one blanks the whole app to the login screen,
// swallowing the message the person needed to read.
//
// "one-time code" rather than "bad one-time": the server also says
// "current one-time code required" (change email, delete account). A code
// the authenticator app is still showing after it was spent answers
// "code_used: that code has already been used", which names neither.

const REAUTH = /totp_required|recovery_required|password_required|code_used|already been used|wrong password|one-time code|bad credentials|passkey/i;

/** The server's own marker on a step-up refusal: `error: "reauth"`, at the
 *  top of the body or inside FastAPI's `detail` wrapper. Read before the
 *  wording, which an older server is the only reason to keep. */
function hasReauthMarker(body: unknown): boolean {
  if (typeof body === "string") {
    try { body = JSON.parse(body); } catch { return false; }
  }
  if (!body || typeof body !== "object") return false;
  const b = body as { error?: unknown; detail?: { error?: unknown } | unknown };
  if (b.error === "reauth") return true;
  const d = b.detail;
  return !!d && typeof d === "object"
    && (d as { error?: unknown }).error === "reauth";
}

/** Is this 401 a re-auth refusal the form shows, rather than a session
 *  that is gone? `detail` is the reply's text; `body`, when the caller has
 *  already parsed it, is checked for the marker too. */
export function isReauthReply(detail: string, body?: unknown): boolean {
  return hasReauthMarker(body) || hasReauthMarker(detail)
    || REAUTH.test(detail);
}
