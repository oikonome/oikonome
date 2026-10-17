// Ask the store whether a newer build exists, and offer it — once per
// foreground stretch, never more than every few hours, and not again for
// a day after "Later". Android goes through Play's own in-app update flow
// (the store downloads and restarts the app; it works for any build Play
// installed, test tracks included), iOS opens the App Store page, since
// iOS has no in-app update. A build that did not come from the store
// (a sideload, the emulator) gets a refusal from the check, which is
// swallowed: there is nothing to offer it.
//
// iOS looks the build up in the App Store by the AppStoreID in its
// Info.plist, not by its bundle id, so a build without one (anyone
// building the app for themselves) has no listing of its own and is
// never checked: asking with someone else's id would offer that
// listing's app as an "update".
import Constants from "expo-constants";
import { useEffect, useRef } from "react";
import { Alert, Platform } from "react-native";
import { MMKV } from "react-native-mmkv";

const store = new MMKV({ id: "oikonome-updates" });
const CHECK_EVERY_MS = 6 * 60 * 60 * 1000;
const SNOOZE_MS = 24 * 60 * 60 * 1000;

/** The App Store listing this build was configured for, if any. */
function appStoreId(): string {
  const plist = Constants.expoConfig?.ios?.infoPlist as
    Record<string, unknown> | undefined;
  return String(plist?.AppStoreID ?? "").trim();
}

export async function checkForAppUpdate(force = false): Promise<void> {
  if (__DEV__ || Platform.OS === "web") return;
  if (Platform.OS === "ios" && !appStoreId()) return;
  const now = Date.now();
  if (!force && now - (store.getNumber("lastCheck") ?? 0) < CHECK_EVERY_MS) return;
  store.set("lastCheck", now);
  const Updates = await import("expo-in-app-updates");
  let info: Awaited<ReturnType<typeof Updates.checkForUpdate>>;
  try {
    info = await Updates.checkForUpdate();
  } catch {
    return;           // not a store install, or the store is unreachable
  }
  if (!info.updateAvailable) return;
  const version = String(info.storeVersion ?? "");
  if (!force && store.getString("snoozed") === version
      && now - (store.getNumber("snoozedAt") ?? 0) < SNOOZE_MS) return;
  Alert.alert(
    "Update available",
    Platform.OS === "android"
      ? "A newer version of Oikonome is on Google Play. Update now? The app restarts when it is done."
      : `Oikonome ${version} is on the App Store. Update now?`,
    [{ text: "Update", isPreferred: true, onPress: async () => {
         try {
           // immediate when Play allows it: a full-screen store flow that
           // restarts into the new build; otherwise the flexible flow,
           // which downloads behind the app and installs when ready
           await Updates.startUpdate(Platform.OS === "android"
                                       ? info.immediateAllowed !== false : undefined);
         } catch (e) {
           Alert.alert("Couldn't start the update",
                       e instanceof Error ? e.message : String(e));
         }
       } },
     { text: "Later", style: "cancel", onPress: () => {
         store.set("snoozed", version); store.set("snoozedAt", Date.now());
       } }]);
}

/** Run the check when the app comes to the front (and once at launch). */
export function useAppUpdateCheck(inFront: boolean) {
  const was = useRef(false);
  useEffect(() => {
    if (inFront && !was.current) void checkForAppUpdate();
    was.current = inFront;
  }, [inFront]);
}
