// Security & household — the web Settings' security and Users
// sections, native: change password/email, two-factor with recovery
// codes, sessions + devices with real revocation, family members and
// one-time invite links, and account deletion. No card carries a
// password box: a posture change is refused once with elevation_required
// and the api client raises the one "confirm it's you" sheet (passkey /
// biometric where the account has one, else password + code), after
// which the session stays elevated for ten minutes. Deleting the account
// asks again every time.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as Clipboard from "expo-clipboard";
import { useState } from "react";
import { Alert, Image, Pressable, ScrollView, StyleSheet, Switch, Text,
         TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { Card, H, Pill } from "../components/ui";
import { Me } from "../lib/api";
import { patchList, patchQuery, removeFromList } from "../lib/cache";
import { inviteOutcome, type InviteOutcome } from "../lib/pure";
import { useSession } from "../lib/session";
import { C, mmddyy } from "../lib/theme";
import { useOwner, useViewer } from "../lib/viewer";

// the web's error → sentence map, shared across the cards
function explain(e: unknown): string {
  const s0 = String(e);
  if (s0.includes("totp_required"))
    return "Two-factor is on — enter your 6-digit code to confirm.";
  if (s0.includes("wrong password") || s0.includes("password_required"))
    return "That password didn't match.";
  if (s0.includes("one-time code") || s0.includes("doesn't match"))
    return "That code didn't match — check your app's clock.";
  if (s0.includes("recovery_required"))
    return "This account signs in with a passkey — confirm with a "
      + "recovery code below.";
  if (s0.includes("too many requests"))
    return "Too many attempts — try again later.";
  if (s0.includes("already in use"))
    return "That email is already in use.";
  if (s0.includes("look like an email"))
    return "That doesn't look like an email.";
  if (s0.includes("at least 10"))
    return "New password must be at least 10 characters.";
  if (s0.includes("last_factor"))
    return "This is your only two-factor method — add another first.";
  return s0.replace(/^\d+:\s*/, "");
}

function browserLabel(ua: string): string {
  if (!ua) return "Unknown device";
  if (/python|httpx|curl/i.test(ua)) return "API client";
  const browser = /edg/i.test(ua) ? "Edge"
    : /firefox/i.test(ua) ? "Firefox"
    : /chrome|chromium/i.test(ua) ? "Chrome"
    : /safari/i.test(ua) ? "Safari" : "";
  const os = /iphone|ipad/i.test(ua) ? "iOS"
    : /android/i.test(ua) ? "Android"
    : /windows/i.test(ua) ? "Windows"
    : /mac os/i.test(ua) ? "macOS"
    : /linux/i.test(ua) ? "Linux" : "";
  if (browser && os) return `${browser} on ${os}`;
  if (browser) return browser;
  if (/mobile/i.test(ua)) return "Mobile browser";
  return ua.slice(0, 40);
}

export default function Security() {
  const { client, signOut, gateOff, setGateOff,
          captureAllowed, setCaptureAllowed } = useSession();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client });
  // Two questions on this screen. Changing your own login email is
  // yours as long as you can change anything at all (a view-only login
  // cannot). The household roster, and which of Delete-account /
  // Leave-household you are offered, key on being the OWNER.
  const mayEdit = !useViewer();
  const owner = useOwner();
  const demo = !!me.data?.demo;
  const resend = useMutation({
    mutationFn: () => client!.verifyResend(),
    onSuccess: (r) => Alert.alert(r.verified
      ? "Already verified — reload." : "Verification email sent."),
    onError: (e) => Alert.alert("Resend failed", explain(e)),
  });
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => {
                                        // only what this screen shows —
                                        // a bare invalidate would refetch
                                        // every mounted tab behind it
                                        return Promise.all(
                                          ["me", "sessions", "devices",
                                           "members", "invites"].map((k) =>
                                            qc.invalidateQueries(
                                              { queryKey: [k] })));
                                      }} />}>
      {demo && (
        <Text style={{ color: C.warn, fontSize: 12, textAlign: "center",
                       padding: 8 }}>
          Demo instance — settings are read-only. Everything here is
          shown exactly as the real app renders it, but nothing can be
          changed: the login is shared, so a change would follow the
          next visitor. Explore freely; the data is synthetic.
        </Text>
      )}
      {me.data?.hosted && me.data.verified === false && (
        <Card>
          <Text style={{ color: C.warn, fontSize: 13 }}>
            Verify your email — we sent a link to {me.data.email}. No
            email? Check spam, or resend.
          </Text>
          <Pressable style={[s.btn, { alignSelf: "flex-start",
                                      marginTop: 6 },
                             resend.isPending && { opacity: 0.5 }]}
                     disabled={resend.isPending}
                     onPress={() => resend.mutate()}>
            <Text style={s.btnText}>
              {resend.isPending ? "sending…" : "Resend"}
            </Text>
          </Pressable>
        </Card>
      )}
      {/* This device's own locks live here with the rest of security,
          not in Settings, where nobody looks for them. Outside the demo
          fieldset on purpose: both are stored on
          the phone, never on the server, so a demo visitor's choice
          follows nobody. */}
      <Card>
        <H>This device</H>
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 10 }}>
          <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
            Require biometric unlock when opening the app
          </Text>
          <Switch value={!gateOff}
                  onValueChange={(on) => setGateOff(!on)}
                  trackColor={{ true: C.accent, false: C.hover }}
                  thumbColor={C.text} />
        </View>
        <Text style={s.mut}>
          Off means anyone holding the unlocked phone can open the app.
        </Text>
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 10, marginTop: 12 }}>
          <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
            Block screenshots and screen recording
          </Text>
          <Switch value={!captureAllowed}
                  onValueChange={(on) => setCaptureAllowed(!on)}
                  trackColor={{ true: C.accent, false: C.hover }}
                  thumbColor={C.text} />
        </View>
        <Text style={s.mut}>
          On, every signed-in screen refuses captures and the app switcher
          shows a blank card. Turn it off to record a walkthrough or share
          your screen with support — this device only.
        </Text>
      </Card>
      {/* demo shows everything the real app shows, untouchable — the
          web renders the same cards inside a disabled fieldset */}
      <View pointerEvents={demo ? "none" : "auto"}
            style={demo ? { opacity: 0.55 } : undefined}>
        <PasswordCard />
        {/* a view-only login cannot change its own address — the server
            403s it, so mirror the web and don't offer the card */}
        {mayEdit && <EmailCard email={me.data?.email ?? ""} />}
        <TwoFactorCard enabled={!!me.data?.totp_enabled}
                       left={me.data?.recovery_codes_left} />
        <SessionsCard />
        <FamilyCard owner={owner} />
        {owner ? <DeleteCard onDeleted={signOut} />
               : <LeaveCard onDeleted={signOut} />}
      </View>
    </ScrollView>
  );
}

// ---- change password ----
function PasswordCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const run = useMutation({
    mutationFn: () => {
      if (next !== confirm)
        throw new Error("New passwords don't match.");
      if (next.length < 10)
        throw new Error("New password must be at least 10 characters.");
      return client!.passwordChange(cur, next);
    },
    onSuccess: (r) => {
      const cost = [
        r.passkeys_removed ? `${r.passkeys_removed} passkey(s) removed` : "",
        // the key that proved this change survives when it is the account's
        // last second factor; unexplained, a passkey that still works after
        // "every passkey was removed" reads as a bug
        r.passkeys_kept
          ? `${r.passkeys_kept} passkey(s) kept — your only second factor`
          : "",
        r.devices_revoked ? `${r.devices_revoked} device(s) signed out` : "",
        r.tokens_revoked ? `${r.tokens_revoked} script token(s) revoked` : "",
      ].filter(Boolean).join(", ");
      setMsg({ text: "Password changed — every other session was signed out"
                     + (cost ? `; ${cost}.` : "."), bad: false });
      setCur(""); setNext(""); setConfirm("");
      // the rotation empties all four lists, not just sessions
      for (const k of ["sessions", "devices", "tokens", "invites"])
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => setMsg({ text: explain(e), bad: true }),
  });
  return (
    <Card>
      <H>Change password</H>
      <Text style={s.mut}>
        Changing it is a full credential rotation: every other session is
        signed out, and every passkey, signed-in phone, script token and
        pending family invite is removed — re-add them after. That is
        deliberate: nothing enrolled by a thief survives.
      </Text>
      <TextInput style={s.input} placeholder="current password"
                 placeholderTextColor={C.mut} secureTextEntry
                 value={cur} onChangeText={setCur} />
      <TextInput style={s.input}
                 placeholder="new password (at least 10 characters)"
                 placeholderTextColor={C.mut} secureTextEntry
                 value={next} onChangeText={setNext} />
      <TextInput style={s.input} placeholder="confirm new password"
                 placeholderTextColor={C.mut} secureTextEntry
                 value={confirm} onChangeText={setConfirm} />
      {msg && (
        <Text style={{ color: msg.bad ? C.bad : C.good, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg.text}</Text>
      )}
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                   (!cur || !next || !confirm || run.isPending)
                     && { opacity: 0.5 }]}
                 disabled={!cur || !next || !confirm || run.isPending}
                 onPress={() => run.mutate()}>
        <Text style={s.btnText}>
          {run.isPending ? "changing…" : "Change password"}
        </Text>
      </Pressable>
    </Card>
  );
}

// ---- change email ----
function EmailCard({ email }: { email: string }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const run = useMutation({
    mutationFn: () => client!.emailChange(next.trim()),
    onSuccess: (r) => {
      setMsg({ bad: false,
        text: (r.verify_sent
          ? "Email changed — check the new address for a verification "
            + "link."
          : "Email changed.")
          + (r.passkeys_removed
              ? ` ${r.passkeys_removed} passkey(s) were removed — re-add them.`
              : "")
          // the key that proved this change survives when it is the
          // account's last second factor — the same rule the password
          // card reports
          + (r.passkeys_kept
              ? ` ${r.passkeys_kept} passkey(s) kept — your only second `
                + `factor.`
              : "")
          + (r.hint ? ` Did you mean ${r.hint}?` : "") });
      setNext("");
      patchQuery<Me>(qc, ["me"], { email: r.email,
        ...(r.verify_sent ? { verified: false } : {}) });
      // the same credential rotation a password change is
      for (const k of ["sessions", "devices", "tokens", "invites"])
        qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => setMsg({ text: explain(e), bad: true }),
  });
  return (
    <Card>
      <H>Email / username</H>
      <Text style={s.mut}>
        Your login email is{" "}
        <Text style={{ color: C.text, fontWeight: "700" }}>{email}</Text>.
        The email is your login identifier, so changing it is a credential
        rotation: every other session is signed out, and every passkey,
        signed-in phone, script token and pending family invite is removed
        — re-add them after.
      </Text>
      <TextInput style={s.input} placeholder="new email"
                 placeholderTextColor={C.mut} keyboardType="email-address"
                 autoCapitalize="none" value={next} onChangeText={setNext} />
      {msg && (
        <Text style={{ color: msg.bad ? C.bad : C.good, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg.text}</Text>
      )}
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                   (!next.trim() || run.isPending) && { opacity: 0.5 }]}
                 disabled={!next.trim() || run.isPending}
                 onPress={() => run.mutate()}>
        <Text style={s.btnText}>
          {run.isPending ? "saving…" : "Change email"}
        </Text>
      </Pressable>
    </Card>
  );
}

// ---- two-factor + recovery codes ----
function TwoFactorCard({ enabled, left }: {
  enabled: boolean; left?: number;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  // the live code: what confirms an enrollment, and what turning 2FA off
  // ALWAYS demands on top of elevation, elevated or not — switching the
  // second factor off is a downgrade, so the authenticator being removed
  // must still be in hand
  const [code, setCode] = useState("");
  const [enroll, setEnroll] =
    useState<{ secret: string; qr: string } | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const fail = (e: unknown) => setMsg({ text: explain(e), bad: true });
  const start = useMutation({
    mutationFn: () => client!.totpEnroll(),
    onSuccess: (r) => { setMsg(null); setEnroll(r); },
    onError: (e) => {
      // "current one-time code required to re-enroll" means 2FA is
      // already ON server-side and this card's cached state is stale
      // (enabled from the web or another device). The enroll branch has
      // no code field, so retrying can never succeed — refetch the
      // truth so the card flips to its enabled form, and say what
      // happened instead of explain()'s clock-skew reading.
      if (String(e).includes("re-enroll")) {
        qc.invalidateQueries({ queryKey: ["me"] });
        setMsg({ text: "Two-factor is already on for this account — "
                       + "it was turned on elsewhere. Showing the "
                       + "current state.", bad: false });
        return;
      }
      fail(e);
    },
  });
  const confirm = useMutation({
    mutationFn: () => client!.totpConfirm(enroll!.secret, code),
    onSuccess: (r) => {
      setEnroll(null); setCode("");
      if (r.recovery_codes?.length) setCodes(r.recovery_codes);
      setMsg({ text: "Two-factor is on — every other session was "
                     + "signed out.", bad: false });
      // setEnroll(null) above already re-rendered the card; without this
      // it shows the "off" form under the "on" message until ["me"] lands
      patchQuery<Me>(qc, ["me"], { totp_enabled: true,
        ...(r.recovery_codes ? { recovery_codes_left: r.recovery_codes.length }
                             : {}) });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: fail,
  });
  const disable = useMutation({
    mutationFn: () => client!.totpDisable(code),
    onSuccess: () => {
      setCode("");
      setMsg({ text: "Two-factor is off.", bad: false });
      patchQuery<Me>(qc, ["me"], { totp_enabled: false });
    },
    onError: fail,
  });
  const regen = useMutation({
    mutationFn: () => client!.recoveryRegenerate(),
    onSuccess: (r) => {
      setCodes(r.recovery_codes);
      setCode("");
      patchQuery<Me>(qc, ["me"],
                     { recovery_codes_left: r.recovery_codes.length });
    },
    onError: fail,
  });
  return (
    <Card>
      <H>Two-factor authentication</H>
      {enabled ? (
        <>
          <View style={{ flexDirection: "row", alignItems: "center",
                         gap: 6 }}>
            <Pill text="enabled" tone="good" />
            <Text style={s.mut}>
              Signing in requires your password and a 6-digit code.
            </Text>
          </View>
          <TextInput style={s.input}
                     placeholder="current 6-digit code (required to turn off)"
                     placeholderTextColor={C.mut}
                     keyboardType="number-pad" maxLength={6}
                     value={code} onChangeText={setCode} />
          <Pressable style={[s.btn, s.btnQuiet,
                       { alignSelf: "flex-start", marginTop: 6 },
                       (!code || disable.isPending) && { opacity: 0.5 }]}
                     disabled={!code || disable.isPending}
                     onPress={() => disable.mutate()}>
            <Text style={[s.btnText, { color: C.warn }]}>
              {disable.isPending ? "turning off…" : "Turn off 2FA"}
            </Text>
          </Pressable>
        </>
      ) : enroll ? (
        <>
          <Text style={s.mut}>
            Scan this QR with your authenticator app (or type the secret
            in manually), then enter the 6-digit code it shows to
            confirm. Your existing sign-in keeps working until you
            confirm.
          </Text>
          <View style={{ backgroundColor: "#fff", padding: 8,
                         alignSelf: "flex-start", borderRadius: 8 }}>
            <Image source={{ uri: enroll.qr }}
                   style={{ width: 180, height: 180 }} />
          </View>
          <Text style={s.mut} selectable>
            Secret (base32):{" "}
            <Text style={{ color: C.text, fontFamily: "monospace" }}>
              {enroll.secret}
            </Text>
          </Text>
          <TextInput style={s.input}
                     placeholder="6-digit code from your authenticator"
                     placeholderTextColor={C.mut}
                     keyboardType="number-pad" maxLength={6}
                     value={code} onChangeText={setCode} />
          <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
            <Pressable style={[s.btn, (!code || confirm.isPending)
                         && { opacity: 0.5 }]}
                       disabled={!code || confirm.isPending}
                       onPress={() => confirm.mutate()}>
              <Text style={s.btnText}>
                {confirm.isPending ? "confirming…" : "Confirm and enable"}
              </Text>
            </Pressable>
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => { setEnroll(null); setCode("");
                                        setMsg(null); }}>
              <Text style={[s.btnText, { color: C.mut }]}>cancel</Text>
            </Pressable>
          </View>
        </>
      ) : (
        <>
          <Text style={s.mut}>
            Add a second factor: after your password, sign-in asks for a
            6-digit code from an authenticator app. Confirming
            enrollment signs out every other session.
          </Text>
          <Pressable style={[s.btn, { alignSelf: "flex-start",
                       marginTop: 6 },
                       start.isPending && { opacity: 0.5 }]}
                     disabled={start.isPending}
                     onPress={() => start.mutate()}>
            <Text style={s.btnText}>
              {start.isPending ? "starting…" : "Enable 2FA"}
            </Text>
          </Pressable>
        </>
      )}
      {left !== undefined && (
        <Text style={[s.mut, { marginTop: 8 }]}>
          Recovery codes:{" "}
          <Text style={{ color: C.text, fontWeight: "700" }}>{left}</Text>
          {" unused"}
          {left <= 2 ? (
            <Text style={{ color: C.warn }}> — running low</Text>
          ) : null}
          {" · "}
          <Text style={{ color: C.accent,
                         opacity: regen.isPending ? 0.5 : 1 }}
                onPress={() => { if (!regen.isPending) regen.mutate(); }}>
            {regen.isPending ? "generating…" : "regenerate"}
          </Text>
        </Text>
      )}
      {codes && (
        <View style={{ backgroundColor: C.bg, borderColor: C.warn,
                       borderWidth: 1, borderRadius: 8, padding: 10,
                       marginTop: 8, gap: 4 }}>
          <Text style={{ color: C.text, fontSize: 13, fontWeight: "700" }}>
            Recovery codes — save these somewhere safe.
          </Text>
          <Text style={s.mut}>
            Each works once to sign in if you lose your authenticator or
            passkey; they won&apos;t be shown again.
          </Text>
          <Text style={{ color: C.text, fontFamily: "monospace",
                         fontSize: 14, lineHeight: 24 }} selectable>
            {codes.join("\n")}
          </Text>
          <Pressable style={[s.btn, { alignSelf: "flex-start" }]}
                     onPress={async () => {
                       await Clipboard.setStringAsync(codes.join("\n"));
                       setCodes(null);
                     }}>
            <Text style={s.btnText}>Copy &amp; dismiss</Text>
          </Pressable>
        </View>
      )}
      {msg && (
        <Text style={{ color: msg.bad ? C.bad : C.good, fontSize: 12,
                       marginTop: 4 }}
              onPress={() => setMsg(null)}>{msg.text}</Text>
      )}
    </Card>
  );
}

// ---- sessions + this app's devices ----
function SessionsCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"],
    queryFn: () => client!.sessions(), enabled: !!client });
  const devices = useQuery({ queryKey: ["devices"],
    queryFn: () => client!.devices(), enabled: !!client });
  const [msg, setMsg] = useState<string | null>(null);
  // the server has already revoked when this runs; what the user waits
  // for is the row leaving — so drop it from the cached list and refetch
  // only the list the write touched
  const done = (key: "sessions" | "devices") => {
    setMsg(null);
    qc.invalidateQueries({ queryKey: [key] });
  };
  const onErr = (e: unknown) => setMsg(explain(e));
  type SessionRow = { id: string; current: boolean };
  const revokeSession = useMutation({
    mutationFn: (id: string) => client!.sessionRevoke({ id }),
    onSuccess: (_r, id) => {
      removeFromList(qc, ["sessions"], "sessions",
                     (r: SessionRow) => r.id === id);
      done("sessions");
    },
    onError: onErr,
  });
  const revokeAll = useMutation({
    mutationFn: () => client!.sessionRevoke({ all_others: true }),
    onSuccess: () => {
      // the server signs out every other phone as well, so the Devices
      // card empties with the Sessions card
      removeFromList(qc, ["sessions"], "sessions",
                     (r: SessionRow) => !r.current);
      removeFromList(qc, ["devices"], "devices",
                     (d: SessionRow) => !d.current);
      done("sessions");
      qc.invalidateQueries({ queryKey: ["devices"] });
    },
    onError: onErr,
  });
  const revokeDevice = useMutation({
    mutationFn: (id: string) => client!.deviceRevoke({ id }),
    onSuccess: (_r, id) => {
      removeFromList(qc, ["devices"], "devices",
                     (d: SessionRow) => d.id === id);
      done("devices");
    },
    onError: onErr,
  });
  const rows = sessions.data?.sessions ?? [];
  const devs = devices.data?.devices ?? [];
  const anyOther = rows.some((r) => !r.current)
    || devs.some((d) => !d.current);
  return (
    <Card>
      <H>Sessions</H>
      <Text style={s.mut}>
        Everywhere you&apos;re signed in. Changing your password signs
        out every other session automatically.
      </Text>
      {rows.map((r) => (
        <View key={r.id} style={s.row}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "600" }}>
              {browserLabel(r.user_agent)}
              {r.ip ? <Text style={s.mut}> · {r.ip}</Text> : null}
              {r.current ? <Text style={{ color: C.good }}>
                {"  this device"}</Text> : null}
            </Text>
            <Text style={s.mut}>
              signed in {mmddyy(r.created_at)}
              {r.last_seen ? ` · active ${mmddyy(r.last_seen)}` : ""}
              {" · expires "}{mmddyy(r.expires_at)}
            </Text>
          </View>
          {!r.current && (
            <Text style={[{ color: C.accent, fontSize: 12, padding: 4 },
                          revokeSession.isPending && { opacity: 0.5 }]}
                  onPress={() => (revokeSession.isPending ? undefined
                    : revokeSession.mutate(r.id))}>
              {revokeSession.isPending && revokeSession.variables === r.id
                ? "signing out…" : "sign out"}
            </Text>
          )}
        </View>
      ))}
      {devs.length > 0 && (
        <>
          <Text style={[s.mut, { fontWeight: "700", marginTop: 8,
                                 color: C.text }]}>
            Mobile devices
          </Text>
          {devs.map((d) => (
            <View key={d.id} style={s.row}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 13,
                               fontWeight: "600" }}>
                  {d.device_name || "Mobile device"}
                  {d.platform ? <Text style={s.mut}>
                    {" · "}{d.platform}</Text> : null}
                  {d.current ? <Text style={{ color: C.good }}>
                    {"  this device"}</Text> : null}
                </Text>
                <Text style={s.mut}>
                  added {mmddyy(d.created_at)}
                  {d.last_seen ? ` · active ${mmddyy(d.last_seen)}` : ""}
                </Text>
              </View>
              {!d.current && (
                <Text style={[{ color: C.accent, fontSize: 12,
                                padding: 4 },
                              revokeDevice.isPending && { opacity: 0.5 }]}
                      onPress={() => (revokeDevice.isPending ? undefined
                        : revokeDevice.mutate(d.id))}>
                  {revokeDevice.isPending && revokeDevice.variables === d.id
                    ? "signing out…" : "sign out"}
                </Text>
              )}
            </View>
          ))}
        </>
      )}
      {anyOther && (
        <Pressable style={[s.btn, s.btnQuiet,
                     { alignSelf: "flex-start", marginTop: 8 },
                     revokeAll.isPending && { opacity: 0.5 }]}
                   disabled={revokeAll.isPending}
                   onPress={() => revokeAll.mutate()}>
          <Text style={[s.btnText, { color: C.warn }]}>
            {revokeAll.isPending ? "signing out…"
                                 : "Sign out everywhere else"}
          </Text>
        </Pressable>
      )}
      {msg && (
        <Text style={{ color: C.bad, fontSize: 12, marginTop: 4 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      )}
    </Card>
  );
}

// ---- family + invites ----
// What each role is called on screen — the same three words the web uses,
// so a household reading one and then the other is told the same thing.
const ROLE_LABEL: Record<string, string> = {
  owner: "owner", member: "member · can edit", viewer: "view-only",
};

function FamilyCard({ owner }: { owner: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const members = useQuery({ queryKey: ["members"],
    queryFn: () => client!.members(), enabled: !!client });
  const invites = useQuery({ queryKey: ["invites"],
    queryFn: () => client!.invites(), enabled: !!client && owner });
  // Hosted mints must NAME an address — the link is mailed there and
  // nowhere else. Say so on the field instead of letting the owner find
  // out by typing a first name and being refused.
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  const hosted = !!me.data?.hosted;
  const [label, setLabel] = useState("");
  // What the invite creates. View-only is the default: an owner who does
  // not read the choice hands out the smaller grant.
  const [invRole, setInvRole] = useState("viewer");
  const [minted, setMinted] = useState<InviteOutcome | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const stepFail = (e: unknown) => setMsg(explain(e));
  const mint = useMutation({
    mutationFn: () => client!.inviteCreate(label.trim(), invRole),
    onSuccess: (r) => {
      setMinted(inviteOutcome(r)); setLabel(""); setMsg(null);
      // the reply has no token_hash, so the new row can't be seeded —
      // but only the invite list changed
      qc.invalidateQueries({ queryKey: ["invites"] });
    },
    onError: stepFail,
  });
  const revoke = useMutation({
    mutationFn: (hash: string) => client!.inviteRevoke(hash),
    onSuccess: (_r, hash) => {
      removeFromList(qc, ["invites"], "invites",
                     (iv: { token_hash: string }) => iv.token_hash === hash);
      qc.invalidateQueries({ queryKey: ["invites"] });
    },
    onError: (e) => setMsg(explain(e)),
  });
  type MemberRow = { id: string; role: string };
  const roleChange = useMutation({
    mutationFn: (v: { id: string; role: string }) =>
      client!.memberSetRole(v.id, v.role),
    onSuccess: (r, v) => {
      patchList(qc, ["members"], "users",
                (u: MemberRow) => u.id === v.id, { role: r.role });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: stepFail,
  });
  const memberDrop = useMutation({
    mutationFn: (id: string) => client!.memberRemove(id),
    onSuccess: (_r, id) => {
      removeFromList(qc, ["members"], "users",
                     (u: MemberRow) => u.id === id);
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: stepFail,
  });
  // Changing what somebody may do asks for the same proof removing them
  // does — it is the same kind of durable grant, in the other direction.
  const setRole = (id: string, email: string, role: string) => {
    Alert.alert(
      role === "member" ? `Let ${email} make changes?`
                        : `Make ${email} view-only?`,
      role === "member"
        ? "They can edit transactions, bills and budgets. Connections, "
          + "exports and the household stay yours."
        : "They keep seeing everything and stop being able to change it.",
      [{ text: role === "member" ? "Allow edits" : "Make view-only",
         onPress: () => roleChange.mutate({ id, role }) },
       { text: "Cancel", style: "cancel" }]);
  };
  const removeMember = (id: string, email: string) => {
    Alert.alert(`Remove ${email}?`,
      "They lose access immediately.",
      [{ text: "Remove", style: "destructive",
         onPress: () => memberDrop.mutate(id) },
       { text: "Cancel", style: "cancel" }]);
  };
  return (
    <Card>
      <H>Family</H>
      <Text style={s.mut}>
        Everyone signed into this household. Everyone here sees
        everything; what differs is what they can change.
        {owner ? " A member can edit the money — transactions, bills, "
          + "budgets, rules. Connections, exports, who is in the "
          + "household and deleting the account stay yours. Invitations "
          + "are one-time and expire in 7 days"
          + (hosted ? " — we email the link to the address you enter, "
                    + "and only that address can use it." : ".")
          : ""}
      </Text>
      {(members.data?.users ?? []).map((u) => (
        <View key={u.id} style={s.row}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "600" }}>
              {u.email}
              {u.me ? <Text style={{ color: C.good }}>  you</Text> : null}
            </Text>
            <Text style={s.mut}>
              {ROLE_LABEL[u.role] ?? u.role} · joined{" "}
              {mmddyy(u.created_at)}
            </Text>
          </View>
          {owner && !u.me && (
            <Text style={[{ color: C.accent, fontSize: 12, padding: 4 },
                          roleChange.isPending && { opacity: 0.5 }]}
                  onPress={() => roleChange.isPending ? undefined
                    : setRole(u.id, u.email,
                      u.role === "member" ? "viewer" : "member")}>
              {roleChange.isPending && roleChange.variables?.id === u.id
                ? "saving…"
                : u.role === "member" ? "make view-only" : "allow edits"}
            </Text>
          )}
          {owner && !u.me && (
            <Text style={[{ color: C.bad, fontSize: 12, padding: 4 },
                          memberDrop.isPending && { opacity: 0.5 }]}
                  onPress={() => memberDrop.isPending ? undefined
                    : removeMember(u.id, u.email)}>
              {memberDrop.isPending && memberDrop.variables === u.id
                ? "removing…" : "remove"}
            </Text>
          )}
        </View>
      ))}
      {owner && (invites.data?.invites ?? []).map((iv) => (
        <View key={iv.token_hash} style={s.row}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 13 }}>
              {iv.label || "pending invite"}
            </Text>
            <Text style={s.mut}>
              invite · {ROLE_LABEL[iv.role] ?? iv.role} · expires{" "}
              {mmddyy(iv.expires_at)}
            </Text>
          </View>
          <Text style={[{ color: C.mut, fontSize: 12, padding: 4 },
                        revoke.isPending && { opacity: 0.5 }]}
                onPress={() => revoke.isPending ? undefined
                  : revoke.mutate(iv.token_hash)}>
            {revoke.isPending && revoke.variables === iv.token_hash
              ? "revoking…" : "revoke"}
          </Text>
        </View>
      ))}
      {minted && (
        <View style={{ backgroundColor: C.bg, borderColor: C.good,
                       borderWidth: 1, borderRadius: 8, padding: 10,
                       marginTop: 8, gap: 4 }}>
          {minted.kind === "link" ? (<>
            <Text style={s.mut}>
              Share this one-time link (shown once)
              {minted.emailed ? `. We also emailed it to ${minted.label}.`
                              : ":"}
            </Text>
            <Text style={{ color: C.text, fontSize: 12 }} selectable>
              {minted.url}
            </Text>
            <Pressable style={[s.btn, { alignSelf: "flex-start" }]}
                       onPress={async () => {
                         await Clipboard.setStringAsync(minted.url);
                         setMinted(null);
                       }}>
              <Text style={s.btnText}>Copy &amp; dismiss</Text>
            </Pressable>
          </>) : (<>
            <Text style={s.mut}>
              Invitation emailed to{" "}
              <Text style={{ color: C.text }}>{minted.label}</Text>. The
              link works once, expires in 7 days, and only that address
              can use it.
            </Text>
            <Pressable style={[s.btn, { alignSelf: "flex-start" }]}
                       onPress={() => setMinted(null)}>
              <Text style={s.btnText}>Done</Text>
            </Pressable>
          </>)}
        </View>
      )}
      {owner && (
        <>
          <TextInput style={[s.input, { marginTop: 8 }]}
                     placeholder={hosted ? "their email address"
                                         : "who's it for? (name or email)"}
                     placeholderTextColor={C.mut}
                     autoCapitalize="none" autoCorrect={false}
                     keyboardType={hosted ? "email-address" : "default"}
                     value={label} onChangeText={setLabel} />
          {/* two words, one tap — a phone has no select, and the choice
              is binary */}
          <View style={{ flexDirection: "row", gap: 8, marginTop: 4 }}>
            {["viewer", "member"].map((r) => (
              <Pressable key={r}
                         style={[s.btn, invRole === r
                           ? { borderColor: C.good } : { opacity: 0.6 }]}
                         onPress={() => setInvRole(r)}>
                <Text style={s.btnText}>{ROLE_LABEL[r]}</Text>
              </Pressable>
            ))}
          </View>
          {msg && (
            <Text style={{ color: C.bad, fontSize: 12 }}
                  onPress={() => setMsg(null)}>{msg}</Text>
          )}
          <Pressable style={[s.btn, { alignSelf: "flex-start",
                       marginTop: 6 },
                       mint.isPending && { opacity: 0.5 }]}
                     disabled={mint.isPending}
                     onPress={() => mint.mutate()}>
            <Text style={s.btnText}>
              {mint.isPending ? (hosted ? "sending…" : "creating…")
                : hosted ? "Send invitation" : "Create invite link"}
            </Text>
          </Pressable>
        </>
      )}
    </Card>
  );
}

// ---- leave household (a member's own login; the web's card) ----
function LeaveCard({ onDeleted }: { onDeleted: () => void }) {
  const { client } = useSession();
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => client!.accountLeave(),
    onSuccess: onDeleted,
    onError: (e) => setMsg(explain(e)),
  });
  return (
    <Card>
      <Text style={{ color: C.bad, fontSize: 16, fontWeight: "700" }}>
        Delete my login
      </Text>
      <Text style={s.mut}>
        Removes your login from this household and signs you out. The
        household's data and the owner's account are not affected — the
        owner can invite you again any time.
      </Text>
      <TextInput style={s.input}
                 placeholder="type “delete my login” to confirm"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={confirm} onChangeText={setConfirm} />
      {msg && (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      )}
      <Pressable style={[s.btn, { backgroundColor: C.bad,
                   alignSelf: "flex-start", marginTop: 6 },
                   (confirm !== "delete my login" || run.isPending)
                     && { opacity: 0.5 }]}
                 disabled={confirm !== "delete my login" || run.isPending}
                 onPress={() => run.mutate()}>
        <Text style={s.btnText}>
          {run.isPending ? "deleting…" : "Delete my login"}
        </Text>
      </Pressable>
    </Card>
  );
}

// ---- delete account ----
function DeleteCard({ onDeleted }: { onDeleted: () => void }) {
  const { client } = useSession();
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => client!.accountDelete(),
    // the session is already dead server-side — drop the local one
    onSuccess: onDeleted,
    onError: (e) => setMsg(explain(e)),
  });
  // the typed phrase, then a native confirm, then the server's own fresh
  // "confirm it's you" — three doors, because there is no undo
  const ask = () => Alert.alert("Delete this account forever?",
    "Every account, transaction, budget and setting is erased and "
    + "everyone is signed out. This cannot be undone.",
    [{ text: "Delete everything", style: "destructive",
       onPress: () => run.mutate() },
     { text: "Cancel", style: "cancel" }]);
  return (
    <Card>
      <Text style={{ color: C.bad, fontSize: 16, fontWeight: "700" }}>
        Delete account
      </Text>
      <Text style={s.mut}>
        Erases this household permanently: every account, transaction,
        budget, and setting — and signs everyone out. There is no undo.
        Export your data first (Settings → Your data) if you want a
        copy.
      </Text>
      <TextInput style={s.input}
                 placeholder="type “delete everything” to confirm"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={confirm} onChangeText={setConfirm} />
      {msg && (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      )}
      <Pressable style={[s.btn, { backgroundColor: C.bad,
                   alignSelf: "flex-start", marginTop: 6 },
                   (confirm !== "delete everything" || run.isPending)
                     && { opacity: 0.5 }]}
                 disabled={confirm !== "delete everything" || run.isPending}
                 onPress={ask}>
        <Text style={s.btnText}>
          {run.isPending ? "deleting…" : "Delete my account forever"}
        </Text>
      </Pressable>
    </Card>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  row: { flexDirection: "row", alignItems: "center", gap: 8,
         borderTopColor: C.border,
         borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 7 },
});
