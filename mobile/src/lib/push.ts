// Native push registration. The push token is a routing handle from the
// platform (FCM on Android, APNs on iOS); the server forwards it to the
// Oikonome push relay, and pushes carry no content — the app wakes and
// refetches from its own server.
//
// Expo Go cannot receive remote pushes (that needs a development or
// store build carrying the Firebase config), so every step here fails
// soft: no permission, no token, or an older server without the
// endpoint all leave the app fully functional, just untickled.
import Constants from "expo-constants";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import { Client } from "./api";
import { pushStillLive } from "./pure";

// In Expo Go on Android, importing expo-notifications THROWS at module
// evaluation (remote push left Expo Go in SDK 53) — so the capability
// check must run before any import of it.
export function pushSupported(): boolean {
  return !(Constants.appOwnership === "expo" && Platform.OS === "android");
}

// Once per SIGNED-IN identity, not per process: a sign-out and a fresh
// login in the same app launch mint a new device row on the server, and a
// process-lifetime flag would leave that row without a push token until
// the app was killed. session.tsx calls resetPushRegistration() when the
// session ends, so the next login registers again.
let attempted = false;
// the token the server last accepted from this install: FCM/APNs hand
// the same token back launch after launch, and re-sending it is a write
// on every cold start for nothing. Cleared with the sign-in (a new
// device row must learn it again). It is only a hint: the relay can mark
// the token dead server-side (and a re-register is what revives it), so
// a match is trusted only while the server's roster still reads this
// device's push as live — see pushStillLive.
const KEY_PUSH_SENT = "oikonome.pushToken.sent";

export function resetPushRegistration(): void {
  attempted = false;
  SecureStore.deleteItemAsync(KEY_PUSH_SENT).catch(() => {});
}

export type PushRegistration = "ok" | "denied" | "unsupported" | "failed"
                              | "skipped";

/** Register this phone for push. The launch-time call runs once per
 *  sign-in; `force` (the setup wizard's "Push" choice) asks again even
 *  after that, and the answer says what happened so a screen can tell the
 *  person — "denied" means the OS permission is off for this app.
 *  `recheck` (Settings' test push) runs again after the launch call too,
 *  without re-asking a refused permission: it re-sends only if the
 *  server no longer holds this device's token live. */
export async function registerNativePush(
    client: Client, opts: { force?: boolean; recheck?: boolean } = {},
): Promise<PushRegistration> {
  if (!pushSupported()) return "unsupported";
  // once per sign-in is plenty
  if (attempted && !opts.force && !opts.recheck) return "skipped";
  attempted = true;
  try {
    const Notifications = await import("expo-notifications");
    const perm = await Notifications.getPermissionsAsync();
    let status = perm.status;
    if (status !== "granted" && (perm.canAskAgain || opts.force)) {
      status = (await Notifications.requestPermissionsAsync()).status;
    }
    if (status !== "granted") return "denied";
    const { data } = await Notifications.getDevicePushTokenAsync();
    if (typeof data !== "string" || data.length < 10) return "failed";
    const sent = `${client.baseUrl} ${data}`;
    if (!opts.force) {
      const last = await SecureStore.getItemAsync(KEY_PUSH_SENT)
        .catch(() => null);
      // the roster is read only when the hint matches — a changed token
      // or server is sent regardless
      if (last === sent) {
        const roster = await client.devices()
          .then((r) => r.devices, () => null);
        if (pushStillLive(last, sent, roster)) return "ok";
      }
    }
    await client.registerPush(data, Platform.OS === "ios" ? "apns" : "fcm");
    await SecureStore.setItemAsync(KEY_PUSH_SENT, sent).catch(() => {});
    return "ok";
  } catch {
    // Expo Go, denied permission, or an unreachable server — all fine
    return "failed";
  }
}
