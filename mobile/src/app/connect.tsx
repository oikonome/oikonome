// First run: which Oikonome instance is yours? Self-host and hosted
// users type the same thing — the URL their browser already uses.
import { router } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput,
         View } from "react-native";

import { normalizeServerUrl, PINNED_SERVER_URL, probeServer,
         setVerifiedServerUrl } from "../lib/api";
import { serverUrlVerdict } from "../lib/pure";
import { C } from "../lib/theme";
import { LogoSpinner } from "../components/logo-spinner";

export default function Connect() {
  // a pinned build has nothing to ask — this screen is only reachable
  // here by deep link, and it goes straight to sign-in
  useEffect(() => {
    if (PINNED_SERVER_URL) router.replace("/login");
  }, []);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const next = async () => {
    setBusy(true);
    setErr(null);
    const base = normalizeServerUrl(url);
    // plain http is refused for anything reachable from the internet —
    // the sign-in that follows would send a password and a live TOTP
    // code readable to anyone on the path. A LAN / loopback address is
    // the self-hosted case and is allowed with the warning below.
    if (serverUrlVerdict(base) === "http-public") {
      setBusy(false);
      setErr("That address is plain http:// on a public host. Your "
             + "password and sign-in codes would travel unencrypted — "
             + "use https:// (the server's reverse-proxy guide sets it "
             + "up), or a private LAN address.");
      return;
    }
    const ok = await probeServer(base);
    setBusy(false);
    if (!ok) {
      setErr("Couldn't reach an Oikonome server there. Check the address — "
             + "it's the same one you open in your browser.");
      return;
    }
    // hand the server over in memory, never as a route param — /login
    // is deep-linkable from outside the app, so a URL param would let a
    // phishing link point the real login form at an attacker's server
    setVerifiedServerUrl(base);
    router.push("/login");
  };

  return (
    <View style={s.wrap}>
      <Text style={s.h1}>Your server</Text>
      <Text style={s.mut}>
        Enter the address of your Oikonome instance — the URL you use in
        the browser.
      </Text>
      <TextInput
        style={s.input}
        placeholder="https://oikonome.example.com"
        placeholderTextColor={C.mut}
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
        value={url}
        onChangeText={setUrl}
        onSubmitEditing={next}
      />
      {serverUrlVerdict(normalizeServerUrl(url)) === "http-private" && (
        <Text style={s.warn}>
          Plain http:// — only safe on a network you trust (a home LAN or
          VPN). Everything, including your password, crosses that network
          readable. Prefer https:// once the server has a certificate.
        </Text>
      )}
      {err && <Text style={s.err}>{err}</Text>}
      <Pressable style={[s.btn, (!url.trim() || busy) && s.btnOff]}
                 disabled={!url.trim() || busy} onPress={next}>
        {busy ? <LogoSpinner color={C.text} />
              : <Text style={s.btnText}>Continue</Text>}
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg, padding: 24, gap: 12,
          justifyContent: "center" },
  h1: { color: C.text, fontSize: 28, fontWeight: "700" },
  mut: { color: C.mut, fontSize: 15, lineHeight: 21 },
  input: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
           borderRadius: 10, color: C.text, fontSize: 16, padding: 14 },
  err: { color: C.bad, fontSize: 14 },
  warn: { color: C.warn, fontSize: 14, lineHeight: 20 },
  btn: { backgroundColor: C.accent, borderRadius: 10, padding: 14,
         alignItems: "center" },
  btnOff: { opacity: 0.5 },
  btnText: { color: C.text, fontSize: 16, fontWeight: "600" },
});
