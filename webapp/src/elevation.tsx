import { createContext, useCallback, useContext, useEffect, useRef,
         useState, type ReactNode } from "react";
import { api, errText, setElevator, type Elevation } from "./api/client";
import { passkeysAvailable, passkeyTicket } from "./stepup";

// Session elevation ("sudo mode"). Security-posture routes take no password
// of their own: they answer 403 elevation_required, the
// request wrapper calls elevate(), and this provider shows ONE sheet that
// re-proves identity — the passkey where the account has one (the proof a
// session thief cannot forge), else password + authenticator code — after
// which the server grants a 10-minute window on this session and the
// wrapper retries. `fresh` (account delete, the full export) always asks
// again, even inside the window.

type Elevate = (fresh: boolean) => Promise<void>;
const Ctx = createContext<Elevate | null>(null);

/** Only the wrapper needs elevate(); a component that wants to warm the
 *  window ahead of a multi-step flow may ask for it too. */
export function useElevate(): Elevate {
  const e = useContext(Ctx);
  if (!e) throw new Error("useElevate outside ElevationProvider");
  return e;
}

type Ask = { fresh: boolean; resolve: () => void;
             reject: (e: Error) => void };

export function ElevationProvider({ children }: { children: ReactNode }) {
  const [ask, setAsk] = useState<Ask | null>(null);
  // the last thing the server told us about this session: skips the GET
  // when a second prompt lands inside a window we already opened (only a
  // `fresh` route asks then), and is thrown away once that window lapses
  // because the methods can change — adding a passkey flips the proof
  const info = useRef<Elevation | null>(null);
  // several guarded calls can fail together (a page firing two mutations):
  // they share one sheet rather than stacking; a fresh ask queues behind
  const chain = useRef<Promise<void>>(Promise.resolve());
  const shared = useRef<Promise<void> | null>(null);

  const show = useCallback((fresh: boolean) => new Promise<void>(
    (resolve, reject) => setAsk({ fresh, resolve, reject })), []);

  const elevate = useCallback<Elevate>((fresh) => {
    if (!fresh && shared.current) return shared.current;
    const p = chain.current.catch(() => {}).then(() => show(fresh));
    chain.current = p;
    if (!fresh) {
      shared.current = p;
      const clear = () => { if (shared.current === p) shared.current = null; };
      p.then(clear, clear);
    }
    return p;
  }, [show]);

  useEffect(() => { setElevator(elevate); return () => setElevator(null); },
            [elevate]);

  return (
    <Ctx.Provider value={elevate}>
      {children}
      {ask && (
        <ElevateSheet fresh={ask.fresh}
          cached={info.current}
          onDone={(e) => { info.current = e; setAsk(null); ask.resolve(); }}
          onCancel={() => { setAsk(null);
            ask.reject(new Error("Confirmation cancelled — nothing was changed.")); }} />
      )}
    </Ctx.Provider>
  );
}

const inputStyle = {
  width: "100%", padding: ".5rem", background: "var(--hover)",
  border: "1px solid var(--line)", borderRadius: "var(--rs)",
  color: "var(--ink)",
} as const;

/** The sentence for an elevate failure — the server's re-auth replies are
 *  token-ish ("wrong password", "bad one-time code") and the sheet is the
 *  one place they now surface. */
function elevateError(e: unknown): string {
  const m = String(e instanceof Error ? e.message : e);
  if (m.includes("NotAllowedError")) return "The passkey prompt was dismissed.";
  if (m.includes("wrong password") || m.includes("password_required"))
    return "That password didn't match.";
  if (m.includes("totp_required")) return "Enter the 6-digit code from your authenticator app.";
  if (m.includes("one-time code")) return "That code didn't match — check your app's clock.";
  if (m.includes("recovery")) return "That recovery code didn't match.";
  if (m.includes("too many")) return "Too many attempts — try again in a few minutes.";
  return errText(e);
}

function ElevateSheet({ fresh, cached, onDone, onCancel }: {
  fresh: boolean; cached: Elevation | null;
  onDone: (e: Elevation) => void; onCancel: () => void;
}) {
  const inWindow = !!cached?.until && Date.parse(cached.until) > Date.now();
  const [info, setInfo] = useState<Elevation | null>(inWindow ? cached : null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState("");
  // which proof this sheet is collecting when the account's preferred one
  // is not the one at hand. The passkey path has a way out for a browser
  // without the key — the password form when the account also has one
  // (methods lists both, passkey first), else a typed recovery code — and
  // so does the password form, because a recovery code stands in for a
  // lost authenticator exactly as it does at login. Without that second
  // door a password+TOTP account whose authenticator is gone could never
  // export or close the account at all.
  const [alt, setAlt] = useState<"password" | "recovery" | null>(null);

  useEffect(() => {
    if (info) return;
    let live = true;
    api.elevation().then((e) => { if (live) setInfo(e); })
      .catch((e) => { if (live) setErr(errText(e)); });
    return () => { live = false; };
  }, [info]);

  const passkey = !!info?.methods.includes("passkey");
  const hasPassword = !!info?.methods.includes("password");

  const submit = async (body: Parameters<typeof api.elevate>[0]) => {
    setBusy(true); setErr(null);
    try { onDone(await api.elevate(body)); }
    catch (e) { setErr(elevateError(e)); }
    finally { setBusy(false); }
  };
  // A recovery code stands in for ONE factor, never for the whole proof:
  // an account that has a password sends both (the {password,
  // recovery_code} shape POST /api/auth/elevate documents), and only a
  // passkey-only account sends the code alone — a bare code on a password
  // account is refused with password_required.
  const recoveryReady = !!recovery.trim() && (!hasPassword || !!pw);
  const submitRecovery = () => {
    if (!recoveryReady) return;
    submit(hasPassword
      ? { password: pw, recovery_code: recovery.trim() }
      : { recovery_code: recovery.trim() });
  };
  const viaPasskey = async () => {
    setBusy(true); setErr(null);
    let ticket = "";
    try { ticket = await passkeyTicket(); }
    catch (e) { setErr(elevateError(e)); setBusy(false); return; }
    await submit({ stepup_ticket: ticket });
  };

  // the passkey prompt opens by itself the moment the sheet knows it is
  // the proof — the click that triggered the guarded action was the
  // gesture; the button below is for a dismissed prompt
  const auto = useRef(false);
  useEffect(() => {
    if (passkey && passkeysAvailable() && !auto.current) {
      auto.current = true;
      viaPasskey();
    }
  }, [passkey]);

  const onKey = (fn: () => void) => (e: { key: string }) => {
    if (e.key === "Enter" && !busy) fn();
  };

  return (
    <div role="dialog" aria-modal="true" aria-label="Confirm it's you"
      style={{ position: "fixed", inset: 0, zIndex: 1100,
               background: "rgba(0,0,0,.55)", display: "flex",
               alignItems: "center", justifyContent: "center",
               padding: "1rem" }}>
      <div className="card" style={{ maxWidth: "26rem", width: "100%",
                                     marginTop: 0 }}>
        <h2>Confirm it's you</h2>
        <p className="mut" style={{ fontSize: 13 }}>
          {fresh
            ? "This action always asks again, even right after a confirmation."
            : "Changing security settings needs a fresh confirmation; after "
              + "this one, everything here just works for 10 minutes."}
        </p>
        {err && <div className="note bad" style={{ marginBottom: ".7rem" }}
                     onClick={() => setErr(null)}>{err}</div>}
        {!info ? (
          <p className="mut">checking…</p>
        ) : passkey && !alt ? (
          <>
            <p>This account signs in with a passkey — approve the prompt on
              your device.</p>
            <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
              {passkeysAvailable() && (
                <button className="pri" disabled={busy} onClick={viaPasskey}>
                  {busy ? "waiting for your device…" : "Use my passkey"}
                </button>
              )}
              <button disabled={busy} onClick={onCancel}>cancel</button>
            </div>
            <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
              {passkeysAvailable()
                ? "Don't have the key on this device? "
                : "Passkeys need a secure (https) connection, which this "
                  + "isn't. "}
              {hasPassword && (
                <a href="#" onClick={(e) => { e.preventDefault();
                  setErr(null); setAlt("password"); }}>
                  Use password instead</a>
              )}
              {hasPassword && info.totp && " · "}
              {/* the recovery code is the proof of last resort: the whole
                  proof on a passkey-only account, and the stand-in for a
                  lost authenticator on one that also has TOTP. Offered only
                  when there is no password, it would strand anybody who has
                  both and has lost the key AND the authenticator. */}
              {(!hasPassword || info.totp) && (
                <a href="#" onClick={(e) => { e.preventDefault();
                  setErr(null); setAlt("recovery"); }}>
                  Use a recovery code instead</a>
              )}
            </p>
          </>
        ) : alt === "recovery" ? (
          <>
            {hasPassword && (
              <p><label>Your password<br />
                <input type="password" autoComplete="current-password"
                       autoFocus style={inputStyle} value={pw}
                       onChange={(e) => setPw(e.target.value)}
                       onKeyDown={onKey(submitRecovery)} />
              </label></p>
            )}
            {/* no length cap here: a recovery code is five dash-separated
                groups, not six digits, and the authenticator field's
                maxLength would make it untypeable */}
            <p><label>One of your recovery codes<br />
              <input autoFocus={!hasPassword} autoComplete="off"
                     autoCapitalize="none" spellCheck={false}
                     style={inputStyle} value={recovery}
                     onChange={(e) => setRecovery(e.target.value)}
                     onKeyDown={onKey(submitRecovery)} />
            </label></p>
            <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
              <button className="pri" disabled={busy || !recoveryReady}
                onClick={submitRecovery}>
                {busy ? "checking…" : "Confirm"}</button>
              <button disabled={busy} onClick={onCancel}>cancel</button>
            </div>
            {(passkey && passkeysAvailable()) || hasPassword ? (
              <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
                {passkey && passkeysAvailable() && (
                  <a href="#" onClick={(e) => { e.preventDefault();
                    setErr(null); setAlt(null); }}>
                    Back to the passkey</a>
                )}
                {passkey && passkeysAvailable() && hasPassword && " · "}
                {hasPassword && (
                  <a href="#" onClick={(e) => { e.preventDefault();
                    setErr(null); setRecovery(""); setAlt("password"); }}>
                    Use my authenticator instead</a>
                )}
              </p>
            ) : null}
          </>
        ) : (
          <>
            <p><label>Your password<br />
              <input type="password" autoComplete="current-password" autoFocus
                     style={inputStyle} value={pw}
                     onChange={(e) => setPw(e.target.value)}
                     onKeyDown={onKey(() => pw && (!info.totp || code.trim())
                       && submit({ password: pw, totp_code: code.trim() }))} />
            </label></p>
            {info.totp && (
              <p><label>6-digit code from your authenticator<br />
                <input inputMode="numeric" autoComplete="one-time-code"
                       maxLength={6} style={inputStyle} value={code}
                       onChange={(e) => setCode(e.target.value)}
                       onKeyDown={onKey(() => pw && code.trim()
                         && submit({ password: pw, totp_code: code.trim() }))} />
              </label></p>
            )}
            <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
              <button className="pri"
                disabled={busy || !pw || (info.totp && !code.trim())}
                onClick={() => submit({ password: pw, totp_code: code.trim() })}>
                {busy ? "checking…" : "Confirm"}</button>
              <button disabled={busy} onClick={onCancel}>cancel</button>
            </div>
            {((passkey && passkeysAvailable()) || info.totp) && (
              <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
                {passkey && passkeysAvailable() && (
                  <a href="#" onClick={(e) => { e.preventDefault();
                    setErr(null); setAlt(null); }}>
                    Back to the passkey</a>
                )}
                {passkey && passkeysAvailable() && info.totp && " · "}
                {info.totp && (
                  <a href="#" onClick={(e) => { e.preventDefault();
                    setErr(null); setCode(""); setAlt("recovery"); }}>
                    Lost your authenticator? Use a recovery code</a>
                )}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
