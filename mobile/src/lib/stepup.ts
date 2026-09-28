// Step-up for destructive account actions on a passkey-only hosted
// account. The server refuses password-alone with 401 "recovery_required"
// (a session thief holds the token and maybe the password — not the
// passkey). Preferred proof is the passkey itself: an inline assertion
// mints a single-use 5-minute ticket that rides the same recovery_code
// field every destructive endpoint already accepts. A recovery code typed
// into the card's field is the lost-key fallback.
//
// This is the native half of webapp/src/stepup.ts. The web raises the
// browser's WebAuthn prompt and falls back to a window.prompt; here the
// platform credential sheet plays the first part, and the fallback is the
// recovery-code field the cards already grow — so a dismissed sheet just
// lets the original refusal through, unchanged.
import { withRecoveryCode } from "./pure";
import { passkeyMaybeAvailable } from "./passkey";

/** The refusal that step-up answers — same test the web makes. */
export const stepupRequired = (e: unknown) =>
  String(e instanceof Error ? e.message : e).includes("recovery_required");

/** Raise the credential sheet and turn the assertion into a step-up
 *  ticket. Every failure — no module in this build, an unsupported
 *  device, a self-host server with no step-up door, the person closing
 *  the sheet — resolves to "" so the caller falls back rather than
 *  replacing the server's own refusal with a platform error nobody asked
 *  about. Exported for the elevation sheet, which redeems the same
 *  ticket at /api/auth/elevate. */
export async function stepupTicket(
  options: () => Promise<{ challenge_id: string; options: unknown }>,
  verify: (challengeId: string, credential: unknown) =>
    Promise<{ stepup_ticket: string }>,
): Promise<string> {
  let passkeys: typeof import("react-native-passkeys");
  try {
    passkeys = await import("react-native-passkeys");
  } catch {
    return "";
  }
  if (!passkeys.isSupported()) return "";
  const o = await options();
  // the request options may arrive wrapped in { publicKey } — unwrap the
  // same way login does
  const wrapped = o.options as { publicKey?: unknown };
  const credential = await passkeys.get(
    (wrapped?.publicKey ?? o.options) as Parameters<typeof passkeys.get>[0]);
  if (!credential) return "";
  return (await verify(o.challenge_id, credential)).stepup_ticket;
}

/** The request to retry after a `recovery_required` refusal, or null when
 *  there is nothing better to try than surfacing the refusal.
 *
 *  Body shape is checked BEFORE the sheet goes up: raising a credential
 *  prompt whose result could not be sent anywhere is worse than not
 *  offering it. */
export async function stepupRetry(
  baseUrl: string, init: RequestInit | undefined,
  options: () => Promise<{ challenge_id: string; options: unknown }>,
  verify: (challengeId: string, credential: unknown) =>
    Promise<{ stepup_ticket: string }>,
): Promise<RequestInit | null> {
  if (!withRecoveryCode(init, "probe")) return null;
  if (!passkeyMaybeAvailable(baseUrl)) return null;
  let ticket = "";
  try {
    ticket = await stepupTicket(options, verify);
  } catch {
    return null;
  }
  return ticket ? withRecoveryCode(init, ticket) : null;
}
