// Native passkey sign-in. Android's Credential Manager checks the
// server's /.well-known/assetlinks.json against this app's signing cert
// before offering that server's passkeys, then signs an app-identity
// origin (android:apk-key-hash:…) the server accepts alongside its
// https page origin. WebAuthn is an https-only feature, so a plain-http
// self-host server can never complete this flow — the button is hidden
// there rather than failing after a tap.
//
// Expo Go does not carry the native module (same situation as
// expo-notifications), so the import is dynamic and everything fails
// soft: missing module, unsupported device, or the user closing the
// credential sheet all fall quietly back to password login.
import Constants from "expo-constants";
import { ext } from "../ext";
import { Platform } from "react-native";

import { passkeyLoginAndMintDevice, passkeyLoginOptions } from "./api";

/** Whether showing the passkey button is even worth it: an https server,
 *  a build that can hold the native module, and a platform the server
 *  actually trusts. Server support is only learned for sure when the
 *  options call answers (404 = no passkeys).
 *
 *  Android only for now. The trust is mutual and BOTH halves have to
 *  exist: the server publishes /.well-known/assetlinks.json and accepts
 *  the android:apk-key-hash: origin the app signs with. iOS needs its
 *  own pair — an apple-app-site-association file and an
 *  associated-domains entitlement — and until the server serves one, an
 *  iOS tap raises a sheet that can offer no credential, so the button
 *  would spin and then do nothing with nothing to say. */
// iOS can only hold passkeys for domains baked into the binary's
// associated-domains entitlement (app.json ios.associatedDomains — the
// installed add-on's list must equal it), and each of those must serve
// /.well-known/apple-app-site-association naming this app. A build with
// no such list offers no passkey button on iOS; Android reads the
// server's assetlinks at runtime and works against any https server that
// serves one.

function hostOf(url: string): string {
  const m = /^https:\/\/([^/:?#]+)/i.exec(url);
  return m ? m[1].toLowerCase() : "";
}

export function passkeyMaybeAvailable(serverUrl: string): boolean {
  if (!serverUrl.startsWith("https://") || Constants.appOwnership === "expo")
    return false;
  if (Platform.OS === "android") return true;
  return Platform.OS === "ios"
    && ext.webcredentialHosts.includes(hostOf(serverUrl));
}

/** The credential sheet failed for a reason the person can act on — the
 *  platform refused the request, no provider offered a key, the build is
 *  missing its native module. Distinct from ApiError (the server refused)
 *  and from a quiet null (the person closed the sheet themselves). The
 *  message carries the platform's own error name and text, because "didn't
 *  work" hides every one of a dozen distinct causes. */
export class PasskeySheetError extends Error {}

/** The person closing the sheet is the one silence that is honest:
 *  they know what they just did. Everything else gets a message. */
const isUserCancel = (e: unknown): boolean => {
  const s = `${(e as Error)?.name ?? ""} ${(e as Error)?.message ?? ""}`;
  return /cancel|dismiss/i.test(s);
};

/** Run the whole passkey sign-in: fetch options, raise the platform
 *  credential sheet, post the assertion, mint the device token.
 *
 *  Resolves null only when the person cancelled the sheet — they know what
 *  they did, silence is honest. Throws ApiError when the SERVER refused,
 *  PasskeySheetError when the PLATFORM did (no provider offered a key,
 *  unsupported device, module missing from the build): both are messages
 *  worth surfacing, and swallowing the platform ones turns a broken flow
 *  into a button that visibly does nothing. */
export async function signInWithPasskey(
  serverUrl: string, deviceName: string, platform: string,
  /** sign this device out to make room — set only when retrying after a
   *  device-limit refusal, with an id the person picked */
  replaceDeviceId = "",
): Promise<{ token: string; id: string } | null> {
  let passkeys: typeof import("react-native-passkeys");
  try {
    passkeys = await import("react-native-passkeys");
  } catch {
    // Expo Go never carries the module; a real build missing it is broken
    if (Constants.appOwnership === "expo") return null;
    throw new PasskeySheetError("this build is missing its passkey module");
  }
  if (!passkeys.isSupported()) {
    throw new PasskeySheetError(
      "this device does not support passkey sign-in");
  }
  // server errors (404 self-host, rate limit) propagate to the caller
  const { challengeId, publicKey } = await passkeyLoginOptions(serverUrl);
  let credential: unknown;
  try {
    credential = await passkeys.get(
      publicKey as Parameters<typeof passkeys.get>[0]);
  } catch (e) {
    if (isUserCancel(e)) return null;
    const err = e as Error;
    throw new PasskeySheetError(
      `${err?.name || "error"}: ${err?.message || String(e)}`);
  }
  if (!credential) return null;
  return passkeyLoginAndMintDevice(serverUrl, challengeId, credential,
                                   deviceName, platform, replaceDeviceId);
}
