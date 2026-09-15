// The passkey proof behind session elevation. A session thief holds the
// cookie and maybe the password — not the passkey — so on a passkey
// account the passkey itself is what re-proves identity: an inline
// WebAuthn assertion mints a single-use 5-minute ticket that
// /api/auth/elevate redeems. The sheet in elevation.tsx is the one place
// that ticket is spent.
import { api } from "./api/client";

export const passkeysAvailable = () =>
  typeof window !== "undefined" && window.isSecureContext &&
  !!window.PublicKeyCredential;

export async function passkeyTicket(): Promise<string> {
  const { startAuthentication } = await import("@simplewebauthn/browser");
  const o = await api.stepupPasskeyOptions();
  const cred = await startAuthentication({
    optionsJSON: ((o.options as { publicKey?: unknown }).publicKey ??
      o.options) as never });
  return (await api.stepupPasskey(o.challenge_id, cred)).stepup_ticket;
}
