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
import { Platform } from "react-native";

import { Client } from "./api";

// In Expo Go on Android, importing expo-notifications THROWS at module
// evaluation (remote push left Expo Go in SDK 53) — so the capability
// check must run before any import of it.
export function pushSupported(): boolean {
  return !(Constants.appOwnership === "expo" && Platform.OS === "android");
}

// Once per SIGNED-IN identity, not per process: a sign-out and a fresh
// login in the same app launch mint a new device row on the server, and a
// process-lifetime flag would leave that row without a push token until
// the app was killed. session.tsx calls resetPushRegistration() when
// go, so the next login registers again.
let attempted = false;

export function resetPushRegistration(): void { attempted = false; }

export type PushRegistration = "ok" | "denied" | "unsupported" | "failed"
                              | "skipped";

/** Register this phone for push. The launch-time call runs once per
 *  sign-in; `force` (the setup wizard's "Push" choice) asks again even
 *  after that, and the answer says what happened so a screen can tell the
 *  person — "denied" means the OS permission is off for this app. */
export async function registerNativePush(
    client: Client, opts: { force?: boolean } = {}): Promise<PushRegistration> {
  if (!pushSupported()) return "unsupported";
  if (attempted && !opts.force) return "skipped";  // once per sign-in is plenty
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
    await client.registerPush(data, Platform.OS === "ios" ? "apns" : "fcm");
    return "ok";
  } catch {
    // Expo Go, denied permission, or an unreachable server — all fine
    return "failed";
  }
}
