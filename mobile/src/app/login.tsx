// Normal login against the chosen server, then mint the device token.
// The server runs its full defense stack on this call; the app only
// relays what it asks for (a TOTP code when 2FA is enrolled).
import * as Device from "expo-device";
import { router } from "expo-router";
import { useEffect, useState } from "react";
import { Linking, Platform, Pressable, StyleSheet, Text,
         TextInput, View } from "react-native";

import { ApiError, getVerifiedServerUrl, loginAndMintDevice, requestUnlock,
         PINNED_SERVER_URL } from "../lib/api";
import { ext } from "../ext";
import * as WebBrowser from "expo-web-browser";
import { passkeyMaybeAvailable, PasskeySheetError,
         signInWithPasskey } from "../lib/passkey";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { LogoSpinner } from "../components/logo-spinner";

/** One of the devices already holding a sign-in, as the device-limit
 *  refusal reports them. */
type LimitDevice = { id: string; device_name: string | null;
                     platform: string | null; last_seen: string | null };

const seenPhrase = (iso: string | null): string => {
  if (!iso) return "never used";
  const days = Math.floor(
    (Date.now() - new Date(iso).getTime()) / 86400000);
  return days <= 0 ? "used today"
    : days === 1 ? "used yesterday"
    : days < 30 ? `used ${days} days ago`
    : `used ${Math.floor(days / 30)} months ago`;
};

export default function Login() {
  // The server comes ONLY from the connect screen's in-memory hand-off,
  // never from route params: /login is deep-linkable from outside the
  // app, and a ?serverUrl= param would let a phishing link pre-wire
  // this form to POST credentials + TOTP to an attacker's server.
  // Reached without the hand-off (an external deep link, a cold
  // restart), there is no trusted server — go pick one.
  const serverUrl = getVerifiedServerUrl();
  useEffect(() => {
    if (!serverUrl) router.replace("/connect");
  }, [serverUrl]);
  const { completeLogin } = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  // recovery codes are letters+dashes — a numeric keypad would make the
  // passkey-account fallback untypeable
  const [recoveryMode, setRecoveryMode] = useState(false);
  const [busy, setBusy] = useState(false);
  // the server asked for a human check the app cannot show (Turnstile
  // after a few wrong passwords); the way past it here is the emailed
  // link, so the screen offers that instead of "use a browser"
  const [challenged, setChallenged] = useState(false);
  const [unlockSent, setUnlockSent] = useState(false);
  // How someone WITHOUT an account gets one here: self-service signup or
  // the operator's site — asked of the server
  // (GET /api/access, public) so the screen never sends a person to a
  // door that is closed. Null until answered; a failed fetch shows the
  // site link on hosted-looking servers and nothing on self-host.
  const [access, setAccess] = useState<{ hosted: boolean;
    signup_path: string | null;
    site_url: string | null; support_email?: string | null } | null>(null);
  useEffect(() => {
    if (!serverUrl) return;
    let live = true;
    const fallback = ext.site?.looksOurs(serverUrl)
      ? { hosted: true, signup_path: null, site_url: ext.site.url }
      : null;
    fetch(serverUrl + "/api/access")
      .then((r) => (r.ok ? r.json() : fallback))
      .then((j) => { if (live) setAccess(j ?? fallback); })
      .catch(() => { if (live) setAccess(fallback); });
    return () => { live = false; };
  }, [serverUrl]);
  // server-relative paths are built only from the verified server URL;
  // an absolute site link is opened only when it is https — anything
  // else the server might say is ignored rather than handed to a browser
  const siteHref = access?.site_url && /^https:\/\//i.test(access.site_url)
    ? access.site_url : null;
  const noAccount: { label: string; href: string } | null =
    !serverUrl ? null
    : access?.signup_path?.startsWith("/")
      ? { label: "No account? Create one", href: serverUrl + access.signup_path }
      : siteHref
        ? { label: `No account? Get one at ${siteHref.replace(/^https:\/\//i, "").replace(/[/:].*$/, "")}`,
            href: siteHref }
        : null;
  const [err, setErr] = useState<string | null>(null);
  // The device-limit refusal, held with the roster it carried. The server's
  // sentence — "revoke an unused device in Settings first" — names a screen
  // on the far side of this door, so the choice of which device to revoke
  // is offered here instead.
  const [limit, setLimit] = useState<
    { message: string; devices: LimitDevice[];
      via: "password" | "passkey" } | null>(null);

  /** Catch the 409 and show the picker; returns false for anything
   *  else so each caller keeps its own error handling. */
  const tookDeviceLimit = (e: unknown,
                           via: "password" | "passkey"): boolean => {
    const body = e instanceof ApiError && e.status === 409
      ? (e.body as { error?: string; message?: string;
                     devices?: LimitDevice[] } | undefined)
      : undefined;
    if (!body || body.error !== "device_limit") return false;
    setErr(null);
    setLimit({ message: body.message
                 || "You're signed in on too many devices.",
               devices: body.devices ?? [], via });
    return true;
  };
  // an https server, a build that can hold the native module, and a
  // platform whose trust the server publishes (Android only for now —
  // see passkeyMaybeAvailable); whether the SERVER does passkeys at all
  // is only learned when the button is tapped
  const canPasskey = passkeyMaybeAvailable(serverUrl ?? "");

  const passkey = async (replaceDeviceId = "") => {
    if (!serverUrl) { router.replace("/connect"); return; }
    setBusy(true);
    setErr(null);
    try {
      const r = await signInWithPasskey(
        serverUrl, Device.modelName || "mobile device", Platform.OS,
        replaceDeviceId);
      // null = quiet fallback (cancelled / unsupported): stay on the form
      if (r) await completeLogin(serverUrl, r.token);
    } catch (e) {
      if (tookDeviceLimit(e, "passkey")) {
        // the picker is showing; nothing else to say
      } else if (e instanceof ApiError && e.status === 404) {
        setErr("This server doesn't offer passkey sign-in. Use your "
               + "password.");
      } else if (e instanceof PasskeySheetError) {
        // the platform refused before any key was used — say what it said,
        // because a bare "didn't work" gives the person nothing to act on
        setErr("Passkey sign-in couldn't start (" + e.message + "). "
               + "Use your password, or re-add this device's passkey "
               + "under Security & household.");
      } else {
        setErr("Passkey sign-in didn't work. Use your password, or try "
               + "again from a device that has your passkey.");
      }
    } finally {
      setBusy(false);
    }
  };

  const submit = async (replaceDeviceId = "") => {
    if (!serverUrl) { router.replace("/connect"); return; }
    setBusy(true);
    setErr(null);
    try {
      const { token } = await loginAndMintDevice(
        serverUrl, email.trim(), password, totp.trim(),
        Device.modelName || "mobile device", Platform.OS, replaceDeviceId);
      await completeLogin(serverUrl, token);
    } catch (e) {
      if (tookDeviceLimit(e, "password")) {
        // the picker is showing; nothing else to say
      } else if (e instanceof ApiError && e.detail === "totp_required") {
        setNeedsTotp(true);
        setErr(null);
      } else if (e instanceof ApiError && e.detail === "passkey_required") {
        setErr(canPasskey
          ? "This account signs in with a passkey — use the passkey "
            + "button below, or enter a one-time recovery code in the "
            + "code field."
          : "This account signs in with a passkey. Enter a one-time "
            + "recovery code in the code field to sign in here.");
        setNeedsTotp(true);
        setRecoveryMode(true);
      } else if (e instanceof ApiError && e.detail === "human_check_failed") {
        setChallenged(true);
        setErr(unlockSent
          ? "Still asking for the human check — open the emailed link "
            + "first, then sign in here."
          : "The server wants a human check it can't show here. Email "
            + "yourself a sign-in link below: open it, then sign in here "
            + "as usual.");
      } else {
        setErr(e instanceof ApiError ? e.detail : String(e));
      }
    } finally {
      setBusy(false);
    }
  };

  if (limit) {
    // The form is REPLACED, not decorated: there is exactly one thing to
    // do here and the email/password fields are no longer part of it.
    return (
      <View style={s.wrap}>
        <Text style={s.h1}>Too many devices</Text>
        <Text style={s.mut}>{limit.message}</Text>
        <Text style={[s.mut, { marginTop: 8 }]}>
          Pick one to sign out. Its history and data are untouched — that
          device just has to sign in again to come back.
        </Text>
        {limit.devices.map((d) => (
          <Pressable key={d.id} style={[s.input, busy && s.btnOff]}
                     disabled={busy}
                     onPress={() => {
                       setLimit(null);
                       if (limit.via === "passkey") void passkey(d.id);
                       else void submit(d.id);
                     }}>
            <Text style={{ color: C.text, fontSize: 15 }}>
              {d.device_name || "unnamed device"}
              {d.platform ? ` · ${d.platform}` : ""}
            </Text>
            <Text style={{ color: C.mut, fontSize: 12 }}>
              {seenPhrase(d.last_seen)}
            </Text>
          </Pressable>
        ))}
        {err && <Text style={s.err}>{err}</Text>}
        <Pressable disabled={busy}
                   onPress={() => { setLimit(null); setErr(null); }}>
          <Text style={s.link}>Cancel</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={s.wrap}>
      <Text style={s.h1}>Sign in</Text>
      {/* the server is worth showing only when the person chose it */}
      {!PINNED_SERVER_URL && <Text style={s.mut}>{serverUrl}</Text>}
      <TextInput style={s.input} placeholder="email"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 autoCorrect={false} keyboardType="email-address"
                 autoComplete="email" value={email} onChangeText={setEmail} />
      <TextInput style={s.input} placeholder="password"
                 placeholderTextColor={C.mut} secureTextEntry
                 autoComplete="current-password"
                 value={password} onChangeText={setPassword} />
      {needsTotp && (
        <TextInput style={s.input}
                   // remount on mode switch — a focused field keeps its
                   // old keyboard otherwise, and the number pad has no
                   // letter keys for a dashed base32 recovery code
                   key={recoveryMode ? "recovery" : "totp"}
                   placeholder={recoveryMode ? "recovery code"
                                             : "6-digit code"}
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   autoCorrect={false}
                   keyboardType={recoveryMode ? "default" : "number-pad"}
                   value={totp} onChangeText={setTotp}
                   onSubmitEditing={() => submit()} />
      )}
      {err && <Text style={s.err}>{err}</Text>}
      {/* the web login's "Email me a sign-in link" (its /unlock page), in
          the app: the link is not a sign-in, it lifts the human check for
          this account so the password form above works again */}
      {challenged && (
        <Pressable disabled={busy || !email.trim()}
                   onPress={() => {
                     if (!serverUrl) return;
                     setBusy(true);
                     requestUnlock(serverUrl, email.trim())
                       .then(() => {
                         setUnlockSent(true);
                         setErr("If that address exists, a sign-in link was "
                                + "sent. Open it, then sign in here as "
                                + "usual — the link itself does not sign "
                                + "you in.");
                       })
                       .catch((e) => setErr(e instanceof ApiError
                         ? e.detail : String(e)))
                       .finally(() => setBusy(false));
                   }}>
          <Text style={s.link}>
            {unlockSent ? "Send the sign-in link again"
                        : "Email me a sign-in link"}
          </Text>
        </Pressable>
      )}
      <Pressable style={[s.btn, (busy || !email.trim() || !password) && s.btnOff]}
                 disabled={busy || !email.trim() || !password}
                 onPress={() => submit()}>
        {busy ? <LogoSpinner color={C.text} />
              : <Text style={s.btnText}>Sign in</Text>}
      </Pressable>
      {/* the web login's mode switch, same wording — a TOTP user whose
          authenticator is gone spends a recovery code in this same
          field, and the number pad would make it untypeable. Clear the
          field with the mode: leaving the typed digits behind means the
          placeholder says "recovery code" over a stale 6-digit string
          that just failed. */}
      {needsTotp && (
        <Pressable disabled={busy}
                   onPress={() => { setRecoveryMode((m) => !m);
                                    setTotp(""); setErr(null); }}>
          <Text style={s.link}>
            {recoveryMode
              ? "Use your authenticator code"
              : "Lost your authenticator? Use a recovery code"}
          </Text>
        </Pressable>
      )}
      {/* the web login's door for someone with nothing left, same
          wording: the server's reset page offers a 7-day cooling-off
          reset that removes the second factor, and support can help.
          The reset flow is mail-driven and lives on the server, so this
          hands off to a browser tab like "Forgot password?". */}
      {needsTotp && (
        <View style={{ gap: 4 }}>
          <Pressable disabled={busy}
                     onPress={() => serverUrl
                       && WebBrowser.openBrowserAsync(serverUrl + "/forgot")}>
            <Text style={s.link}>
              Lost your recovery codes too? Reset your password — the reset
              page can start a 7-day recovery
            </Text>
          </Pressable>
          {access?.support_email ? (
            <Pressable disabled={busy}
                       onPress={() => Linking.openURL(
                         `mailto:${access.support_email}`
                         + "?subject=Locked%20out%20of%20my%20account")}>
              <Text style={s.link}>or email {access.support_email}</Text>
            </Pressable>
          ) : null}
        </View>
      )}
      {/* the same door the web login has — the reset flow is mail-driven
          and lives on the server, so the app hands off to it in a browser
          tab, so a locked-out person has a way out of this screen */}
      <Pressable disabled={busy}
                 onPress={() => serverUrl && WebBrowser.openBrowserAsync(serverUrl + "/forgot")}>
        <Text style={s.link}>Forgot password?</Text>
      </Pressable>
      {/* the web login's door for a first-time visitor, same logic: signup
          when it is open, else the operator's site. Accounts are made on the server, never in the app —
          the link hands off to a browser tab like "Forgot password?". */}
      {noAccount && (
        <Pressable disabled={busy}
                   onPress={() => WebBrowser.openBrowserAsync(noAccount.href)}>
          <Text style={s.link}>{noAccount.label}</Text>
        </Pressable>
      )}
      {/* mirrors the web login: an "or" divider and a secondary passkey
          button; hidden when the server or this build can't do WebAuthn */}
      {canPasskey && (
        <>
          <Text style={s.or}>or</Text>
          <Pressable style={[s.btnAlt, busy && s.btnOff]} disabled={busy}
                     onPress={() => passkey()}>
            <Text style={s.btnText}>Sign in with a passkey</Text>
          </Pressable>
        </>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg, padding: 24, gap: 12,
          justifyContent: "center" },
  h1: { color: C.text, fontSize: 28, fontWeight: "700" },
  mut: { color: C.mut, fontSize: 14 },
  input: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
           borderRadius: 10, color: C.text, fontSize: 16, padding: 14 },
  err: { color: C.bad, fontSize: 14 },
  btn: { backgroundColor: C.accent, borderRadius: 10, padding: 14,
         alignItems: "center" },
  or: { color: C.mut, fontSize: 14, textAlign: "center" },
  link: { color: C.accent, fontSize: 14, textAlign: "center" },
  btnAlt: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 10, padding: 14, alignItems: "center" },
  btnOff: { opacity: 0.5 },
  btnText: { color: C.text, fontSize: 16, fontWeight: "600" },
});
