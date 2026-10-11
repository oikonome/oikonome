// The shared demonstration login: one owner login whose password is handed
// out, so every holder signs in as the same owner. The server refuses it every
// change to the login itself and every grant that would outlive it; these
// are the sentences those refusals carry, so a control switched off for it
// says what the 403 would have said.
import type { Me } from "./api/client";

export const SHARED_LOGIN_NOTE = "This is a shared demonstration login — "
  + "its password, email, sign-in factors and sessions can't be changed.";
export const SHARED_GRANT_NOTE = "This is a shared demonstration login — "
  + "it can't add people, script tokens, support access or new outbound "
  + "destinations to this household.";
export const SHARED_CREDENTIAL_NOTE = "This is a shared demonstration login — "
  + "it can't connect, disconnect or reconnect a bank or exchange, save "
  + "aggregator keys or take the credential bundle, since those outlive "
  + "the login.";

/** Why a control is switched off for the shared demonstration login, or
 *  null when it is live. `login` is a change to the login's own posture;
 *  `grant` a principal or channel that would outlive it. A demo instance
 *  answers null — its own lock already covers the card. The mobile twin is
 *  `sharedLock` in mobile/src/lib/pure.ts. */
export function sharedLock(me: Pick<Me, "demo" | "shared_login"> | undefined,
                           kind: "login" | "grant" | "credential"): string | null {
  if (!me || me.demo || !me.shared_login) return null;
  return kind === "login" ? SHARED_LOGIN_NOTE
    : kind === "credential" ? SHARED_CREDENTIAL_NOTE : SHARED_GRANT_NOTE;
}
