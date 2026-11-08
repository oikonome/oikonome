// The buttons under a secret shown once (recovery codes, a script token, a
// webhook signing secret, a one-time link). A copy the platform refuses
// must leave the secret on screen — it can still be selected by hand — and
// a plain Dismiss is always there so a refused clipboard never traps the
// sheet. Where the clipboard can be read back without prompting (Android),
// a copy that lands is taken back off once a minute has passed, at the
// first moment the app can do it: its timer in the foreground, the app
// coming back to the foreground, or a secret screen being left. A clip
// still there when the app is never reopened stays (no native clip expiry
// here). On iOS reading it asks the user for permission, so it is left.
import * as Clipboard from "expo-clipboard";
import { useEffect, useState } from "react";
import { AppState, Platform, Pressable, StyleSheet, Text, View }
  from "react-native";

import { copySecretWith, SecretClipboardClear, SECRET_CLIPBOARD_TTL_MS }
  from "../lib/pure";
import { C } from "../lib/theme";

// One pending clear for the whole app: Copy & dismiss unmounts the sheet at
// once, so the clear has to outlive the component that armed it.
const clipClear = new SecretClipboardClear();
// Android grants clipboard reads only once the window has focus, which can
// land a beat after AppState reports "active".
const FOCUS_SETTLE_MS = 500;
let watching = false;

function watchForeground() {
  if (watching) return;
  watching = true;
  AppState.addEventListener("change", (state) => {
    if (state === "active") {
      setTimeout(() => { void clipClear.appStateChanged(state); },
                 FOCUS_SETTLE_MS);
    }
  });
}

export function copySecret(text: string): Promise<boolean> {
  const readable = Platform.OS === "android";
  if (readable) watchForeground();
  return copySecretWith({
    set: (t) => Clipboard.setStringAsync(t),
    get: readable ? () => Clipboard.getStringAsync() : undefined,
  }, text, undefined, SECRET_CLIPBOARD_TTL_MS, clipClear);
}

export function SecretCopyButtons({ text, what, plural = false, onDone }: {
  text: string;
  // what the hand-copy hint tells the user to select ("codes", "token")
  what: string;
  plural?: boolean;
  onDone: () => void;
}) {
  const [failed, setFailed] = useState(false);
  // leaving a secret screen is another chance to clear an expired clip
  useEffect(() => () => { void clipClear.released(); }, []);
  return (
    <View style={{ gap: 4 }}>
      <View style={{ flexDirection: "row", gap: 8 }}>
        <Pressable style={s.btn}
                   onPress={async () => {
                     if (await copySecret(text)) onDone();
                     else setFailed(true);
                   }}>
          <Text style={s.btnText}>Copy &amp; dismiss</Text>
        </Pressable>
        <Pressable style={[s.btn, s.btnQuiet]} onPress={onDone}>
          <Text style={s.btnText}>Dismiss</Text>
        </Pressable>
      </View>
      {failed && (
        <Text style={{ color: C.bad, fontSize: 12 }}>
          Couldn&apos;t copy — select the {what} and copy{" "}
          {plural ? "them" : "it"} by hand,
          then press Dismiss.
        </Text>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
