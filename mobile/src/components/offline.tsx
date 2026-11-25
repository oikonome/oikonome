// The one gate every write button reads while the phone is offline
// (specs/screen-states.md, "Offline saves"): the button greys and says
// "You're offline." beside itself, instead of spinning and then failing
// with "Couldn't save — you're offline.". The signal is lib/connection.ts:
// the app's own requests, plus a look at the health door here on every
// return to the app and every few seconds while offline.
import { useEffect, useSyncExternalStore } from "react";
import { AppState, StyleSheet, Text, type TextStyle } from "react-native";

import { isOffline, probeReach, subscribeOffline } from "../lib/connection";
import { OFFLINE_WRITE } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";

/** True while the phone has no answer from its server. A write button
 *  adds it to `disabled` and greys with `offlineGrey`. */
export function useOffline(): boolean {
  return useSyncExternalStore(subscribeOffline, isOffline, isOffline);
}

/** The greyed look of a write button while offline (spread into its
 *  style array: `[s.btn, offlineGrey(offline)]`). */
export function offlineGrey(offline: boolean) {
  return offline ? st.grey : null;
}

/** "You're offline." beside a write button, only while offline. */
export function OfflineNote({ style }: { style?: TextStyle }) {
  const offline = useOffline();
  if (!offline) return null;
  return <Text style={[st.note, style]} accessibilityLiveRegion="polite">
    {OFFLINE_WRITE}</Text>;
}

/** How often, while offline, the health door is asked again. */
const RECHECK_MS = 15_000;

/** Mounted once in the root layout: looks at the server on every return
 *  to the app, and every RECHECK_MS while offline and in front. */
export function ConnectionWatch() {
  const { serverUrl } = useSession();
  useEffect(() => {
    if (!serverUrl) return;
    const look = () => { void probeReach(serverUrl); };
    const sub = AppState.addEventListener("change", (s) => {
      if (s === "active") look();
    });
    const id = setInterval(() => {
      if (isOffline() && AppState.currentState === "active") look();
    }, RECHECK_MS);
    return () => { sub.remove(); clearInterval(id); };
  }, [serverUrl]);
  return null;
}

const st = StyleSheet.create({
  grey: { opacity: 0.4 },
  note: { color: C.warn, fontSize: 12, marginTop: 4 },
});
