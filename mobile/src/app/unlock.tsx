// The biometric gate. Reopening the app lands here; Face ID / Touch ID /
// fingerprint (or the device passcode) releases the stored credential —
// literally: where the device has strong biometrics the token is bound to
// them in the enclave and the prompt is the read.
import { useEffect, useRef, useState } from "react";
import { Alert, Pressable, StyleSheet, Text, View } from "react-native";

import { useSession } from "../lib/session";
import { C } from "../lib/theme";

export default function Unlock() {
  const { unlock, signOut } = useSession();

  // one prompt at a time: without this guard the auto-prompt on mount and
  // a tap (or two taps) on the button each queue their own biometric sheet
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const tryUnlock = async () => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try { await unlock(); }
    finally { inFlight.current = false; setBusy(false); }
  };
  useEffect(() => { tryUnlock(); }, [unlock]);  // eslint-disable-line react-hooks/exhaustive-deps

  // the way out when the biometric can't be produced (a bandaged finger,
  // a face the sensor no longer matches): drop this device's token and
  // sign in again with the password — never a bypass of the gate
  const startOver = () => Alert.alert("Sign in again?",
    "This forgets the sign-in on this phone. You'll enter your password "
    + "(and second factor) again.",
    [{ text: "Cancel", style: "cancel" },
     { text: "Sign in again", style: "destructive", onPress: signOut }]);

  return (
    <View style={s.wrap}>
      <Text style={s.h1}>Oikonome</Text>
      <Text style={s.mut}>Locked</Text>
      <Pressable style={[s.btn, busy && { opacity: 0.5 }]} disabled={busy}
                 onPress={tryUnlock}>
        <Text style={s.btnText}>Unlock</Text>
      </Pressable>
      <Pressable onPress={startOver} style={{ marginTop: 24 }}>
        <Text style={s.link}>Can't unlock? Sign in again</Text>
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg, alignItems: "center",
          justifyContent: "center", gap: 12 },
  h1: { color: C.text, fontSize: 32, fontWeight: "700" },
  mut: { color: C.mut, fontSize: 15 },
  btn: { backgroundColor: C.accent, borderRadius: 10, marginTop: 16,
         paddingHorizontal: 32, paddingVertical: 14 },
  btnText: { color: C.text, fontSize: 16, fontWeight: "600" },
  link: { color: C.mut, fontSize: 13, textDecorationLine: "underline" },
});
