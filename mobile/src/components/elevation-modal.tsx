// The elevation sheet — the UI half of the elevation controller in
// lib/api.ts ("session elevation" section). Mounted once in the root
// layout; a guarded call refused with elevation_required raises
// it through the controller and retries when it resolves.
//
// One sheet, one proof. An account with a passkey proves itself with the
// passkey (the platform credential sheet goes up by itself; a recovery
// code is the lost-key fallback). Anyone else types the password and, on
// a TOTP account, the current code — or, when that authenticator is gone,
// the password beside a recovery code, which is the same stand-in the
// login screen offers. A password is never collected by a settings card:
// this sheet is the only place it is asked for.
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, KeyboardAvoidingView, Modal, Platform,
         Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { ELEVATION_CANCELLED, errText, registerElevationPrompter,
         type ElevationPrompt, type ElevationProof,
         type ElevationStatus } from "../lib/api";
import { passkeyMaybeAvailable } from "../lib/passkey";
import { elevationProof } from "../lib/pure";
import { useSession } from "../lib/session";
import { stepupTicket } from "../lib/stepup";
import { C } from "../lib/theme";

// the web's error → sentence map for the same door
function explain(e: unknown): string {
  const t = errText(e);
  if (/wrong password|bad credentials|password_required/i.test(t))
    return "That password didn't match.";
  if (/totp_required/i.test(t))
    return "Two-factor is on — enter the current 6-digit code too.";
  if (/one-time code|doesn't match/i.test(t))
    return "That code didn't match — check your app's clock.";
  if (/recovery/i.test(t))
    return "That recovery code didn't match.";
  if (/too many/i.test(t))
    return "Too many attempts — try again later.";
  return t;
}

export default function ElevationModal() {
  const { client } = useSession();
  const [prompt, setPrompt] = useState<ElevationPrompt | null>(null);
  // the prompt on screen, readable outside render (the controller calls
  // in from a promise); prompts that arrive while one is up wait their
  // turn — never dropped, so the second of two guarded calls still gets
  // its answer
  const current = useRef<ElevationPrompt | null>(null);
  const queue = useRef<ElevationPrompt[]>([]);
  const [status, setStatus] = useState<ElevationStatus | null>(null);
  // "recovery" collects a recovery code — the whole proof on a
  // passkey-only account, and the stand-in for a lost authenticator
  // beside the password on a TOTP one; "password" collects the ordinary
  // pair. Chosen once the status lands, and switchable both ways after.
  const [form, setForm] = useState<"password" | "recovery" | null>(null);
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [rc, setRc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    registerElevationPrompter((p) => {
      if (current.current) { queue.current.push(p); return; }
      current.current = p;
      setPrompt(p);
    });
    return () => {
      registerElevationPrompter(null);
      // nothing will answer these once the sheet is gone
      for (const p of [current.current, ...queue.current])
        p?.reject(new Error(ELEVATION_CANCELLED));
      current.current = null; queue.current = [];
    };
  }, []);

  const finish = useCallback((p: ElevationPrompt, ok: boolean) => {
    if (ok) p.resolve(); else p.reject(new Error(ELEVATION_CANCELLED));
    setPw(""); setCode(""); setRc(""); setErr(null); setBusy(false);
    setStatus(null); setForm(null);
    current.current = queue.current.shift() ?? null;
    setPrompt(current.current);
  }, []);

  const prove = useCallback(async (p: ElevationPrompt,
                                   proof: ElevationProof) => {
    if (!client) { finish(p, false); return; }
    setBusy(true); setErr(null);
    try {
      await client.elevateWith(proof);
      finish(p, true);
    } catch (e) {
      setErr(explain(e));
      setBusy(false);
    }
  }, [client, finish]);

  // The passkey route: raise the platform sheet, redeem the ticket. A
  // closed sheet, a build without the module or an iOS build against a
  // server it holds no passkeys for all land on the recovery-code form —
  // the person keeps a way through, and is told why the sheet did not.
  const passkey = useCallback(async (p: ElevationPrompt) => {
    if (!client) { finish(p, false); return; }
    setBusy(true); setErr(null);
    let ticket = "";
    try {
      if (passkeyMaybeAvailable(client.baseUrl))
        ticket = await stepupTicket(client.stepupPasskeyOptions,
                                    client.stepupPasskey);
    } catch (e) {
      setErr(explain(e));
    }
    if (ticket) { await prove(p, { stepup_ticket: ticket }); return; }
    setBusy(false);
    setForm("recovery");
  }, [client, finish, prove]);

  // a prompt just opened: learn how this account proves itself
  useEffect(() => {
    if (!prompt) return;
    if (!client) { finish(prompt, false); return; }
    let gone = false;
    client.elevationStatus().then((st) => {
      if (gone) return;
      setStatus(st);
      if (st.methods.includes("passkey")) passkey(prompt);
      else setForm("password");
    }).catch((e) => {
      if (gone) return;
      // the door itself failed (self-host server older than the app, a
      // network drop): nothing to type into, so say so and let go
      setErr(explain(e));
      setForm("password");
    });
    return () => { gone = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prompt]);

  if (!prompt) return null;
  const passkeyAccount = !!status?.methods.includes("passkey");
  const canPasskey = !!client && passkeyMaybeAvailable(client.baseUrl);
  // an account with a passkey AND a password+TOTP may prove itself either
  // way: the passkey runs first, but a phone that does not hold the key
  // should type the password rather than burn one of ten recovery codes
  const passwordToo = !!status?.methods.includes("password");
  // one decision for both the button's enabled state and what gets sent,
  // so they cannot disagree about whether there is enough to send
  const proof = form
    ? elevationProof(form, status, { password: pw, code,
                                     recoveryCode: rc })
    : null;
  const ready = proof !== null;
  const submit = () => {
    if (!proof || busy) return;
    prove(prompt, proof);
  };
  return (
    <Modal visible animationType="slide" transparent
           onRequestClose={() => finish(prompt, false)}>
      <KeyboardAvoidingView style={s.wrap}
                            behavior={Platform.OS === "ios" ? "padding"
                                                            : undefined}>
        <View style={s.sheet}>
          <Text style={s.title}>Confirm it&apos;s you</Text>
          <Text style={s.mut}>
            {prompt.fresh
              ? "This action asks every time, even right after another "
                + "confirmation."
              : "Confirm once — changes for the next 10 minutes won't ask "
                + "again."}
          </Text>
          {!form ? (
            <ActivityIndicator color={C.accent} style={{ marginTop: 8 }} />
          ) : form === "password" ? (
            <>
              <TextInput style={s.input} placeholder="your password"
                         placeholderTextColor={C.mut} secureTextEntry
                         autoFocus value={pw} onChangeText={setPw}
                         onSubmitEditing={status?.totp ? undefined : submit} />
              {status?.totp && (
                <TextInput style={s.input}
                           placeholder="6-digit code from your authenticator"
                           placeholderTextColor={C.mut}
                           keyboardType="number-pad" maxLength={6}
                           value={code} onChangeText={setCode}
                           onSubmitEditing={submit} />
              )}
            </>
          ) : (
            <>
              <Text style={s.mut}>
                {!passkeyAccount
                  ? "Your authenticator isn't needed — confirm with your "
                    + "password and one of your recovery codes."
                  : canPasskey
                    ? "This account signs in with a passkey. Use it, or "
                      + "confirm with one of your recovery codes."
                    : "This account signs in with a passkey, which this "
                      + "app cannot use against this server — confirm "
                      + "with one of your recovery codes."}
              </Text>
              {/* a recovery code stands in for ONE factor, so an account
                  that has a password still sends it — the shape the
                  elevate door documents, and the reason a bare code is
                  refused there with password_required */}
              {passwordToo && (
                <TextInput style={s.input} placeholder="your password"
                           placeholderTextColor={C.mut} secureTextEntry
                           autoFocus value={pw} onChangeText={setPw} />
              )}
              {/* no maxLength: a recovery code is five dash-separated
                  groups, not six digits */}
              <TextInput style={s.input} placeholder="recovery code"
                         placeholderTextColor={C.mut} autoCapitalize="none"
                         autoCorrect={false} value={rc} onChangeText={setRc}
                         onSubmitEditing={submit} />
            </>
          )}
          {err && (
            <Text style={{ color: C.bad, fontSize: 12 }}
                  onPress={() => setErr(null)}>{err}</Text>
          )}
          <View style={{ flexDirection: "row", gap: 8, marginTop: 4,
                         alignItems: "center" }}>
            {form && (
              <Pressable style={[s.btn, (!ready || busy) && { opacity: 0.5 }]}
                         disabled={!ready || busy} onPress={submit}>
                <Text style={s.btnText}>
                  {busy ? "confirming…" : "Confirm"}
                </Text>
              </Pressable>
            )}
            {form === "recovery" && passkeyAccount && canPasskey && (
              <Pressable style={[s.btn, s.btnQuiet, busy && { opacity: 0.5 }]}
                         disabled={busy} onPress={() => passkey(prompt)}>
                <Text style={[s.btnText, { color: C.accent }]}>
                  Use passkey
                </Text>
              </Pressable>
            )}
            {form === "recovery" && passwordToo && (
              <Pressable style={[s.btn, s.btnQuiet, busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => { setErr(null); setRc("");
                                          setForm("password"); }}>
                <Text style={[s.btnText, { color: C.accent }]}>
                  {status?.totp ? "Use my authenticator"
                                : "Use password instead"}
                </Text>
              </Pressable>
            )}
            {/* A lost authenticator must not be a dead end: reaching the
                recovery form only from the passkey path's failure would
                leave a password+TOTP account unable to confirm at all —
                and elevation guards the export and the account delete. */}
            {form === "password" && status?.totp && (
              <Pressable style={[s.btn, s.btnQuiet, busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => { setErr(null); setCode("");
                                          setForm("recovery"); }}>
                <Text style={[s.btnText, { color: C.accent }]}>
                  Lost your authenticator?
                </Text>
              </Pressable>
            )}
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => finish(prompt, false)}>
              <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
            </Pressable>
          </View>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, justifyContent: "flex-end",
          backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, paddingBottom: 28,
           gap: 10 },
  title: { color: C.text, fontSize: 16, fontWeight: "700" },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
