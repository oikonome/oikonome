import { startAuthentication } from "@simplewebauthn/browser";
import { useEffect, useState } from "react";
import { api } from "../api/client";

// passkeys are offered in hosted mode and need a secure context
// (https). Self-hosted LAN installs run over http, so this is already
// hidden there; on the hosted domain it shows and works.
const passkeysAvailable = () =>
  typeof window !== "undefined" && window.isSecureContext &&
  !!window.PublicKeyCredential;

export default function Login({ onDone }: { onDone: () => void }) {
  // name the tab while signed out; back to the plain app title
  // once the form unmounts
  useEffect(() => {
    document.title = "Oikonome — Sign in";
    return () => { document.title = "Oikonome"; };
  }, []);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState<{ email: string; password: string } | null>(null);
  // A passkey-only account gets its own state rather than reaching it by
  // FAILING a password login and reading a red box — an error-shaped
  // experience for an account working exactly as configured.
  const [passkeyOnly, setPasskeyOnly] = useState(false);
  // Recovery codes get a standing link. Mentioned only inside a red
  // failure box, someone locked out of their authenticator would have to
  // fail first to learn there was a way through.
  const [recovery, setRecovery] = useState(false);
  // Where a person with NOTHING left — no authenticator, no recovery
  // codes — is sent: the reset page's cooling-off door, and the support
  // inbox the server names (hosted only; self-host has no such inbox).
  const [supportEmail, setSupportEmail] = useState<string | null>(null);
  useEffect(() => {
    api.access().then((a) => setSupportEmail(a.support_email ?? null))
      .catch(() => {});
  }, []);

  // a demo instance shares its (synthetic-data) credentials pre-auth, so
  // the login page can hand them to the visitor instead of making them
  // dig through the terminal that seeded it
  useEffect(() => {
    api.demoLogin().then((d) => {
      if (d.demo && d.email && d.password) {
        setDemo({ email: d.email, password: d.password });
        setEmail((v) => v || d.email!);
        setPassword((v) => v || d.password!);
      }
    }).catch(() => {});
  }, []);

  const passkey = async () => {
    // Email optional: empty → discoverable (usernameless) WebAuthn flow.
    // Typed email still helps non-resident keys via the allow-list.
    setBusy(true);
    setError("");
    try {
      const o = await api.passkeyLoginOptions(email.trim() || undefined);
      const cred = await startAuthentication({
        optionsJSON: ((o.options as { publicKey?: unknown }).publicKey ??
          o.options) as never });
      await api.passkeyLogin(o.challenge_id, cred);
      onDone();
    } catch (err) {
      if (!String(err).includes("NotAllowedError"))
        setError("Passkey sign-in didn't work. Use password"
          + (needTotp ? " + authenticator code" : "")
          + ", or try again from the device that has your passkey.");
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(email, password, needTotp ? totp : undefined);
      onDone();
    } catch (err) {
      const msg = String(err);
      if (msg.includes("totp_required")) setNeedTotp(true);
      // a passkey-only account can't finish password login — the server
      // demands the passkey, so drive the WebAuthn flow directly
      else if (msg.includes("passkey_required")) {
        // Not an error: the account is configured this way. Show the state
        // that belongs to it, with the passkey as the primary action and
        // both recovery routes stated. (The server still accepts a recovery
        // code in the totp_code field — that is the "different device" door.)
        setPasskeyOnly(true);
        if (passkeysAvailable() && !totp.trim()) {
          try { await passkey(); return; } catch { /* fall through */ }
        }
      } else setError("Wrong email or password" + (needTotp ? " or code" : ""));
    } finally {
      setBusy(false);
    }
  };

  const brand = (
    <h1 style={{ marginTop: 0, display: "flex", alignItems: "center",
                 gap: ".5rem" }}>
      {/* the logo mark beside the wordmark */}
      <svg width="26" height="26" viewBox="0 0 100 100" aria-hidden="true">
        <path d="M39.7 78.2 A30 30 0 1 1 60.3 78.2" fill="none"
          stroke="#4f9bd6" strokeWidth="11" strokeLinecap="round" />
      </svg>
      Oikonome
      {demo && <span className="pill a" style={{ fontSize: 10.5 }}>demo</span>}
    </h1>
  );

  // A passkey-only account: its own state, with the passkey as the primary
  // action. Both ways back in from a device that has no passkey are named
  // here rather than implied by a failure message.
  if (passkeyOnly) {
    return (
      <div className="centered">
        <form className="card" style={{ width: "23rem" }}
              onSubmit={(e) => { e.preventDefault(); void passkey(); }}>
          {brand}
          <p className="sub" style={{ margin: "0 0 .6rem" }}>
            This account signs in with a passkey.</p>
          {error && <div className="note bad">{error}</div>}
          <button className="pri" style={{ width: "100%" }} disabled={busy}>
            {busy ? "…" : "Use your passkey"}</button>
          <p className="sub" style={{ margin: ".7rem 0 0" }}>
            On a different device?{" "}
            <a href="#" onClick={(e) => {
              e.preventDefault();
              setPasskeyOnly(false); setNeedTotp(true); setRecovery(true);
            }}>Use a recovery code</a> or{" "}
            <a href="/forgot">email yourself a link</a>.</p>
        </form>
      </div>
    );
  }

  return (
    <div className="centered">
      <form className="card" style={{ width: "23rem" }} onSubmit={submit}>
        {brand}
        {demo && (
          // credentials are pre-filled (effect above) — no need to ALSO
          // print them; just say what this box is
          <div className="sub" style={{ marginBottom: ".6rem" }}>
            Synthetic household, resets hourly. Credentials are filled in
            for you.</div>
        )}
        {error && <div className="note bad">{error}</div>}
        {/* Step 1: email+password. Step 2: code only (creds stay in state). */}
        {!needTotp ? (
          <>
            <p style={{ margin: "0 0 .6rem" }}>
              <input style={{ width: "100%" }} type="email" placeholder="email"
              autoComplete="username" aria-label="email"
              value={email} onChange={(e) => setEmail(e.target.value)} required /></p>
            <p style={{ margin: "0 0 .6rem" }}>
              <input style={{ width: "100%" }} type="password" placeholder="password"
              autoComplete="current-password" aria-label="password"
              value={password} onChange={(e) => setPassword(e.target.value)} required /></p>
          </>
        ) : (
          <>
            {/* "Signing in as …" with no way to change it would make a
                mistyped email cost a page reload. */}
            <div className="sub" style={{ marginBottom: ".5rem" }}>
              Signing in as <b style={{ color: "var(--ink)" }}>{email}</b> ·{" "}
              <a href="#" onClick={(e) => {
                e.preventDefault();
                setNeedTotp(false); setRecovery(false);
                setTotp(""); setPassword(""); setError("");
              }}>not you?</a>
            </div>
            <p style={{ margin: "0 0 .5rem" }}>
              <input style={{ width: "100%",
                fontFamily: "ui-monospace, monospace", letterSpacing: ".15em" }}
              autoFocus
              autoComplete="one-time-code"
              placeholder={recovery ? "recovery code" : "6-digit code"}
              aria-label={recovery
                ? "Recovery code" : "Authenticator code"}
              value={totp} onChange={(e) => setTotp(e.target.value)} required /></p>
          </>
        )}
        <button className="pri" style={{ width: "100%" }} disabled={busy}>
          {busy ? "…" : demo ? "Explore the demo"
            : needTotp ? "Sign in" : "Sign in"}
        </button>
        {/* passkey sign-in is hidden on the demo (synthetic shared account,
            no passkeys). Available alongside TOTP when both are enrolled:
            pick password+code OR passkey alone. There is deliberately no
            three-line explanation of what a passkey IS — it would sit next
            to the button that does it, on the one screen where a person
            wants to get in. */}
        {passkeysAvailable() && !demo && !needTotp && (
          <>
            <div className="sub" style={{ textAlign: "center",
                  margin: ".65rem 0 .35rem" }}>or</div>
            <button type="button" style={{ width: "100%" }}
              disabled={busy} onClick={passkey}>
              Sign in with a passkey
            </button>
          </>
        )}
        {needTotp ? (
          <p className="sub" style={{ margin: ".7rem 0 0" }}>
            Lost your authenticator?{" "}
            {recovery ? (
              <a href="#" onClick={(e) => {
                e.preventDefault(); setRecovery(false); setTotp("");
                setError("");
              }}>Use your authenticator code</a>
            ) : (
              <a href="#" onClick={(e) => {
                // clear the field with the mode. Leaving the typed
                // authenticator digits behind would leave the placeholder
                // saying "recovery code" over a stale 6-digit string —
                // press Sign in and you resubmit the code that just
                // failed, get the same generic error, and have no reason
                // to suspect the box was never reset.
                e.preventDefault(); setRecovery(true); setTotp("");
                setError("");
              }}>Use a recovery code</a>
            )}
            {passkeysAvailable() && !demo && (<>
              {" · "}
              <a href="#" onClick={(e) => { e.preventDefault(); void passkey(); }}
              >use a passkey instead</a>
            </>)}
            <br />
            {/* the door for someone with nothing left, stated where the
                wall is: the reset page offers a 7-day cooling-off reset
                that removes the second factor, and support can help */}
            Lost your recovery codes too?{" "}
            <a href="/forgot">Reset your password</a> — the reset page can
            start a 7-day recovery
            {supportEmail && (<>, or email{" "}
              <a href={`mailto:${supportEmail}?subject=Locked%20out%20of%20my%20account`}
              >{supportEmail}</a></>)}.
          </p>
        ) : (
          <p className="sub" style={{ margin: ".8rem 0 0", textAlign: "center" }}>
            {/* the explanation that this link also clears 2FA moves to the
                page it leads to — it is that page's job to say so */}
            <a href="/forgot">Forgot password?</a>
          </p>
        )}
      </form>
    </div>
  );
}
