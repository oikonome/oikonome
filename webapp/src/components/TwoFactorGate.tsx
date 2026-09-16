import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, errText } from "../api/client";

/** a blocking enrollment gate. Hosted accounts must set up a
 *  second factor (authenticator app OR passkey) before the app is usable
 *  — /api/me.needs_2fa drives whether App renders this instead of the
 *  routes. Enrolling TOTP as the first factor returns one-time recovery
 *  codes, shown here once so a lost authenticator isn't a lockout. */
const passkeysAvailable = () =>
  typeof window !== "undefined" && window.isSecureContext &&
  !!window.PublicKeyCredential;

const inputStyle = {
  width: "100%", padding: ".5rem", background: "var(--hover)",
  border: "1px solid var(--line)", borderRadius: "var(--rs)",
  color: "var(--ink)",
} as const;

export default function TwoFactorGate({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"choose" | "totp" | "codes">("choose");
  const [enroll, setEnroll] = useState<{ secret: string; qr: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const finish = () => { qc.invalidateQueries({ queryKey: ["me"] }); onDone(); };

  const startTotp = async () => {
    setBusy(true); setErr(null);
    try {
      setEnroll(await api.totpEnroll());
      setMode("totp");
    } catch (e) {
      setErr(errText(e));
    } finally { setBusy(false); }
  };
  const confirmTotp = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await api.totpConfirm(enroll!.secret, code.trim());
      if (r.recovery_codes && r.recovery_codes.length) {
        setCodes(r.recovery_codes); setMode("codes");
      } else { finish(); }
    } catch (e) {
      setErr(String(e).includes("match")
        ? "That code didn't match — check your app's clock." : errText(e));
    } finally { setBusy(false); }
  };
  const addPasskey = async () => {
    setBusy(true); setErr(null);
    try {
      const { startRegistration } = await import("@simplewebauthn/browser");
      // elevation is proved up front: the server refuses to mint the
      // challenge without it, so the browser/password manager never creates
      // a credential the server then rejects (an orphan passkey in the vault)
      const o = await api.passkeyRegisterOptions();
      const cred = await startRegistration({
        optionsJSON: ((o.options as { publicKey?: unknown }).publicKey ??
          o.options) as never });
      const label = window.prompt("Name this passkey (e.g. 'laptop', 'phone'):") ?? "";
      const r = await api.passkeyRegister(o.challenge_id, cred, label);
      // a passkey enrolled as the FIRST strong factor returns one-time
      // recovery codes — show them once (lockout protection) before finishing
      if (r.recovery_codes && r.recovery_codes.length) {
        setCodes(r.recovery_codes); setMode("codes");
      } else { finish(); }
    } catch (e) {
      if (String(e).includes("NotAllowedError")) { /* user cancelled */ }
      else setErr(errText(e));
    } finally { setBusy(false); }
  };

  return (
    <div className="centered" style={{ minHeight: "100vh" }}>
      <div className="card" style={{ maxWidth: "30rem", width: "100%" }}>
        <h1 style={{ marginTop: 0 }}>Secure your account</h1>
        {err && <div className="note bad" style={{ marginBottom: ".7rem" }}>{err}</div>}

        {mode === "codes" ? (
          <>
            <p>Save these <b>recovery codes</b> somewhere safe — each works
              once to sign in if you lose your authenticator or passkey. They
              won't be shown again.</p>
            <div className="card" style={{ fontFamily: "monospace",
                 fontSize: 14, lineHeight: 1.9, letterSpacing: ".02em",
                 userSelect: "all", background: "var(--hover)" }}>
              {codes.map((c) => <div key={c}>{c}</div>)}
            </div>
            <button className="pri" style={{ marginTop: ".8rem" }}
                    onClick={() => {
                      navigator.clipboard?.writeText(codes.join("\n"));
                      finish();
                    }}>
              I've saved them — continue</button>
          </>
        ) : mode === "totp" ? (
          <>
            <p className="mut">Scan this QR with your authenticator app (or
              type the secret in manually), then enter the 6-digit code it
              shows.</p>
            {enroll?.qr && (
              <img src={enroll.qr} alt="Authenticator setup QR code"
                   width={180} height={180}
                   style={{ display: "block", background: "#fff", padding: 8,
                            borderRadius: 8, margin: "0 0 .6rem" }} />
            )}
            <p>Secret: <code style={{ userSelect: "all" }}>{enroll?.secret}</code></p>
            <input inputMode="numeric" autoComplete="one-time-code" autoFocus
                   placeholder="6-digit code" style={inputStyle}
                   value={code} onChange={(e) => setCode(e.target.value)} />
            <button className="pri" style={{ marginTop: ".7rem" }}
                    disabled={busy || !code.trim()} onClick={confirmTotp}>
              {busy ? "confirming…" : "Confirm & continue"}</button>
          </>
        ) : (
          <>
            <p className="mut">Oikonome protects your financial data — before
              you continue, add a second factor so a stolen password alone
              can't reach your account. You'll confirm your password once
              when you pick one.</p>
            <div style={{ display: "flex", flexDirection: "column",
                 gap: ".5rem", marginTop: ".8rem" }}>
              <button className="pri" disabled={busy} onClick={startTotp}>
                Set up an authenticator app</button>
              {passkeysAvailable() && (
                <button disabled={busy} onClick={addPasskey}>
                  Add a passkey (Face ID / fingerprint / security key)</button>
              )}
            </div>
            <p className="mut" style={{ fontSize: 13, marginTop: ".7rem" }}>
              An authenticator app works everywhere. Passkeys use your
              device's biometrics and need a secure (https) connection.</p>
          </>
        )}
        {/* a session that pre-dates the requirement (or an invite taken by
            mistake) must still be able to leave — the gate is not a trap */}
        {mode !== "codes" && (
          <p className="mut" style={{ fontSize: 13, marginTop: "1rem",
                                      marginBottom: 0 }}>
            <a href="#" onClick={(e) => { e.preventDefault();
              api.logout().finally(() => { window.location.href = "/login"; });
            }}>Sign out</a>
          </p>
        )}
      </div>
    </div>
  );
}
