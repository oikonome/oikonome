// Server URL + device token live in the platform secure enclave
// (Keychain/Keystore), never in AsyncStorage — the token is a long-lived
// credential and the whole point of the biometric gate is that nothing
// readable sits outside it.
//
// The token has two homes, and a plain marker says which is in use:
//   "bio"   — stored with requireAuthentication: the enclave key is
//             bound to the enrolled biometrics, so reading it IS the
//             biometric prompt, and no code running as the app (or a
//             process dump) gets the bearer without a finger or a face.
//             Enrolling a new fingerprint / face invalidates it → sign
//             in again.
//   "plain" — no strong biometric enrolled, or the person switched the
//             gate off in Settings: readable by the app without a
//             prompt; the gate is then LocalAuthentication's device
//             credential check (or nothing, by their choice).
// The session layer decides which; this module only stores.
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import { secureKey } from "./pure";

const KEY_URL = "oikonome.serverUrl";
const KEY_TOKEN = "oikonome.deviceToken";
const KEY_TOKEN_BIO = "oikonome.deviceToken.bio";
const KEY_TOKEN_MODE = "oikonome.deviceToken.mode";
const KEY_GATE_OFF = "oikonome.gateOff";
// set once the items below have been re-written with DEVICE_ONLY
const KEY_DEVICE_ONLY_DONE = "oikonome.keychainDeviceOnly";
const KEY_BIO_DEVICE_ONLY_DONE = "oikonome.keychainDeviceOnly.bio";

export type TokenMode = "bio" | "plain";

// The iOS Keychain's default accessibility is included in encrypted
// iCloud / Finder backups and migrates onto a restored phone, so a backup
// would carry the 90-day device token (and the cache key beside its
// ciphertext) to another iPhone, skipping sign-in and the gate. THIS_DEVICE_ONLY
// keeps these items on the phone that created them — the iOS side of what
// `allowBackup: false` does on Android. Android ignores the option.
export const DEVICE_ONLY: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};

const BIO_OPTS: SecureStore.SecureStoreOptions = {
  ...DEVICE_ONLY,
  requireAuthentication: true,
  authenticationPrompt: "Unlock Oikonome",
};

/** Re-write a plain item with DEVICE_ONLY. Delete-then-add, because an
 *  update of an existing Keychain item keeps its old accessibility. */
async function rewriteDeviceOnly(key: string) {
  const v = await SecureStore.getItemAsync(key);
  if (v === null) return;
  await SecureStore.deleteItemAsync(key);
  await SecureStore.setItemAsync(key, v, DEVICE_ONLY);
}

/** Installs from before DEVICE_ONLY wrote the token and its marker with
 *  the backup-able default; move them once, at launch, without a prompt.
 *  A failure leaves the marker unset and the next launch tries again. */
async function migrateToDeviceOnly() {
  if (Platform.OS !== "ios") return;
  try {
    if ((await SecureStore.getItemAsync(KEY_DEVICE_ONLY_DONE)) === "1") return;
    await rewriteDeviceOnly(KEY_TOKEN);
    await rewriteDeviceOnly(KEY_TOKEN_MODE);
    await SecureStore.setItemAsync(KEY_DEVICE_ONLY_DONE, "1", DEVICE_ONLY);
  } catch { /* retried next launch */ }
}

/** Whether the enclave can bind an item to the enrolled biometrics on
 *  this device right now (strong biometrics enrolled). */
export function canBindTokenToBiometrics(): boolean {
  try { return SecureStore.canUseBiometricAuthentication(); }
  catch { return false; }
}

/** What is known WITHOUT touching the token: where the server is and
 *  whether a token exists, and in which home. Called on launch — the
 *  bearer itself stays in the enclave until the gate opens. */
export async function loadCredentialShape():
    Promise<{ serverUrl: string | null; tokenMode: TokenMode | null }> {
  await migrateToDeviceOnly();
  const [serverUrl, mode] = await Promise.all([
    SecureStore.getItemAsync(KEY_URL),
    SecureStore.getItemAsync(KEY_TOKEN_MODE),
  ]);
  if (mode === "bio" || mode === "plain")
    return { serverUrl, tokenMode: mode };
  // an install from before the marker existed: its token is in the plain
  // home if anywhere. Learn that once and record it — the session's
  // first unlock moves it to the biometric home where the device allows.
  const legacy = await SecureStore.getItemAsync(KEY_TOKEN);
  if (legacy) {
    await SecureStore.setItemAsync(KEY_TOKEN_MODE, "plain", DEVICE_ONLY);
    return { serverUrl, tokenMode: "plain" };
  }
  return { serverUrl, tokenMode: null };
}

/** The plain-home token, no prompt. null = gone. */
export async function readPlainToken(): Promise<string | null> {
  return SecureStore.getItemAsync(KEY_TOKEN);
}

/** The biometric-home token — this call shows the platform's biometric
 *  prompt. Resolves null when the item is gone or was invalidated by an
 *  enrollment change; rejects when the person cancels or the prompt
 *  cannot be shown. */
export async function readBioToken(): Promise<string | null> {
  const t = await SecureStore.getItemAsync(KEY_TOKEN_BIO, BIO_OPTS);
  // an older biometric item carries the backup-able default; the person
  // just passed the prompt, and on iOS creating the item again is silent,
  // so move it now
  if (t && Platform.OS === "ios") {
    try {
      if ((await SecureStore.getItemAsync(KEY_BIO_DEVICE_ONLY_DONE)) !== "1") {
        await SecureStore.deleteItemAsync(KEY_TOKEN_BIO);
        await SecureStore.setItemAsync(KEY_TOKEN_BIO, t, BIO_OPTS);
        await SecureStore.setItemAsync(KEY_BIO_DEVICE_ONLY_DONE, "1", DEVICE_ONLY);
      }
    } catch { /* retried at the next unlock */ }
  }
  return t;
}

export async function saveServerUrl(url: string) {
  await SecureStore.setItemAsync(KEY_URL, url);
}

/** Store the token in one home and forget the other. Writing the "bio"
 *  home prompts for a biometric on Android (every operation on a
 *  biometric-bound key requires presence there); on iOS creating the
 *  item is silent and only reads prompt. */
export async function saveToken(token: string, mode: TokenMode) {
  if (mode === "bio") {
    await SecureStore.setItemAsync(KEY_TOKEN_BIO, token, BIO_OPTS);
    await SecureStore.setItemAsync(KEY_TOKEN_MODE, "bio", DEVICE_ONLY);
    await SecureStore.deleteItemAsync(KEY_TOKEN);
  } else {
    await SecureStore.setItemAsync(KEY_TOKEN, token, DEVICE_ONLY);
    await SecureStore.setItemAsync(KEY_TOKEN_MODE, "plain", DEVICE_ONLY);
    await SecureStore.deleteItemAsync(KEY_TOKEN_BIO);
  }
}

export async function clearCredentials() {
  await Promise.all([
    SecureStore.deleteItemAsync(KEY_TOKEN),
    SecureStore.deleteItemAsync(KEY_TOKEN_BIO),
    SecureStore.deleteItemAsync(KEY_TOKEN_MODE),
    // the server URL survives sign-out on purpose — re-login on the same
    // instance shouldn't re-ask where it lives
  ]);
}

/** The user's biometric-gate choice: "1" = gate disabled by choice.
 *  Their call (Settings) — the credential still sits in the enclave. */
export async function loadGateOff(): Promise<boolean> {
  return (await SecureStore.getItemAsync(KEY_GATE_OFF)) === "1";
}

export async function saveGateOff(off: boolean) {
  if (off) await SecureStore.setItemAsync(KEY_GATE_OFF, "1");
  else await SecureStore.deleteItemAsync(KEY_GATE_OFF);
}

/** Screenshot / screen-recording block: ON unless the person turns it
 *  off ("1" = capture allowed). Per device, like the gate — it is about
 *  what this phone's screen shows, not about the household. */
const KEY_CAPTURE_OK = "oikonome.captureOk";
export async function loadCaptureAllowed(): Promise<boolean> {
  return (await SecureStore.getItemAsync(KEY_CAPTURE_OK)) === "1";
}

export async function saveCaptureAllowed(ok: boolean) {
  if (ok) await SecureStore.setItemAsync(KEY_CAPTURE_OK, "1");
  else await SecureStore.deleteItemAsync(KEY_CAPTURE_OK);
}

// ---- "I left the wizard on purpose" ------------------------------------
// The mirror of the web's wizardexit: a fresh account lands IN the guided
// setup rather than on an empty dashboard, and stops landing there the
// moment the person says so ("save & finish later" / "don't show this
// again"). Keyed by TENANT, because the flag records a decision a
// particular household made — not a property of the phone.
//
// SecureStore rather than AsyncStorage only because it is what this module
// already owns; nothing here is secret.
const KEY_LEFT_WIZARD = "oikonome.leftWizard";

// The scope is a server URL, whose ':' and '/' SecureStore rejects
// outright: an unsanitized key throws on every read AND write, and since
// both accessors below swallow storage errors, the flag would silently
// never persist and the wizard could not be left. secureKey is tested.
const wizKey = (scope?: string) => secureKey(KEY_LEFT_WIZARD, scope);

export async function markLeftWizard(tenantId?: string) {
  try { await SecureStore.setItemAsync(wizKey(tenantId), "1"); }
  catch { /* a failed write just means the wizard offers itself again */ }
}

export async function hasLeftWizard(tenantId?: string): Promise<boolean> {
  try { return (await SecureStore.getItemAsync(wizKey(tenantId))) === "1"; }
  catch { return false; }
}
