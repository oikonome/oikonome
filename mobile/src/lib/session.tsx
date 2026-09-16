// App-wide auth state. Four phases:
//   loading — reading the secure store on launch
//   setup   — no server URL or no token: the connect/login flow owns the UI
//   locked  — credentials exist but the biometric gate hasn't passed
//   ready   — unlocked; the client is live
//
// The gate is not a cover over the screens: while locked the token is
// not in JS memory (never read from the enclave, or dropped on lock),
// there is no client, and the query cache is frozen (see query.ts).
// Where the device has strong biometrics the token is bound to them in
// the enclave and reading it IS the prompt; elsewhere the device
// credential check stands in front of a plain read.
import * as LocalAuthentication from "expo-local-authentication";
import { router } from "expo-router";
import { ext } from "../ext";
import { createContext, useCallback, useContext, useEffect, useMemo,
         useRef, useState } from "react";
import { Alert, AppState, Platform } from "react-native";

import { Client, makeClient, PINNED_SERVER_URL } from "./api";
import { lockQueryCache, unlockQueryCache, wipeQueryCache } from "./query";
import { canBindTokenToBiometrics, clearCredentials, loadCredentialShape,
         loadGateOff, loadCaptureAllowed, saveCaptureAllowed, readBioToken, readPlainToken, saveGateOff,
         saveServerUrl, saveToken, TokenMode } from "./storage";

type Phase = "loading" | "setup" | "locked" | "ready";

// Iteration escape hatch: EXPO_PUBLIC_SKIP_LOCK=1 at BUILD time skips the
// biometric gate. The value is inlined when the bundle is built — a store
// or distributed build must be produced without it (the gate is the
// product behavior; this exists so a development phone isn't fingerprinted
// forty times an hour).
const SKIP_LOCK = process.env.EXPO_PUBLIC_SKIP_LOCK === "1";

interface SessionState {
  phase: Phase;
  serverUrl: string | null;
  client: Client | null;
  gateOff: boolean;
  /** screenshots / recording allowed on this device (default false) */
  captureAllowed: boolean;
  setCaptureAllowed: (ok: boolean) => Promise<void>;
  setGateOff: (off: boolean) => Promise<void>;
  unlock: () => Promise<void>;
  completeLogin: (serverUrl: string, token: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const Ctx = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [serverUrl, setServerUrl] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [gateOff, setGateOffState] = useState(false);
  const [captureAllowed, setCaptureAllowedState] = useState(false);
  // which enclave home the token is in; a ref because the unlock and
  // AppState paths read it outside render
  const tokenMode = useRef<TokenMode | null>(null);
  const gateOffRef = useRef(false);
  useEffect(() => { gateOffRef.current = gateOff; }, [gateOff]);

  // the token is gone from the enclave (revoked elsewhere and cleared,
  // or invalidated by a biometric enrollment change): back to login
  const credentialsLost = useCallback(async () => {
    await clearCredentials();
    await wipeQueryCache();
    // the next login is a new device row server-side; let it register push
    import("./push").then(({ resetPushRegistration }) =>
      resetPushRegistration()).catch(() => {});
    tokenMode.current = null;
    setToken(null);
    setPhase("setup");
  }, []);

  // Thaw the disk cache BEFORE the token and the phase land: the screens
  // (mounted under the gate's cover) start fetching the moment a client
  // exists, so a restore that ran after "ready" would paint empty, show
  // "Loading…", fire the fetches, and only then see the disk copy —
  // spinner, then cached, then fresh. The restore is a local read
  // (milliseconds), so awaiting it here means the first frame paints
  // from the last-known numbers and the fetches refine them.
  const goReady = useCallback(async (t: string) => {
    await unlockQueryCache();
    setToken(t);
    setPhase("ready");
  }, []);

  useEffect(() => {
    (async () => {
      const [{ serverUrl: u, tokenMode: mode }, off, capOk] =
        await Promise.all([loadCredentialShape(), loadGateOff(),
                           loadCaptureAllowed()]);
      setGateOffState(off);
      setCaptureAllowedState(capOk);
      gateOffRef.current = off;
      // a pinned build only ever talks to its own server: credentials
      // saved by a build that pointed elsewhere (a bring-your-own build
      // this one replaced) are for a server this build cannot reach —
      // drop them and sign in fresh rather than sending that token here
      if (u && PINNED_SERVER_URL && u !== PINNED_SERVER_URL) {
        await credentialsLost();
        return;
      }
      setServerUrl(u);
      tokenMode.current = mode;
      if (!u || !mode) { setPhase("setup"); return; }
      if (!(SKIP_LOCK || off)) { setPhase("locked"); return; }
      // gate off by choice (or a dev build): the token is read now — a
      // biometric-home token still prompts once here, then moves to the
      // plain home when Settings turns the gate off (see setGateOff)
      try {
        const t = mode === "bio" ? await readBioToken()
                                 : await readPlainToken();
        if (!t) { await credentialsLost(); return; }
        await goReady(t);
      } catch {
        setPhase("locked");
      }
    })();
  }, [credentialsLost, goReady]);

  // Leaving the app re-arms the gate — but the decision is made on
  // RETURN, from time spent away, never on the background event itself.
  // Android surfaces (the biometric dialog above all, but also
  // permission sheets and share sheets) background the app for a moment
  // as a side effect, and any lock-on-background rule turns the gate's
  // own success into the next lock: unlock → trailing background event →
  // locked → prompt, forever. Time-away is immune to event ordering; a
  // real absence still locks.
  const LOCK_AFTER_MS = 15_000;
  const backgroundAt = useRef<number | null>(null);
  useEffect(() => {
    const sub = AppState.addEventListener("change", (st) => {
      if (st === "background") backgroundAt.current = Date.now();
      if (st === "active") {
        const away = backgroundAt.current
          ? Date.now() - backgroundAt.current : 0;
        backgroundAt.current = null;
        if (away > LOCK_AFTER_MS && !SKIP_LOCK && !gateOffRef.current) {
          setPhase((p) => (p === "ready" ? "locked" : p));
        }
      }
    });
    return () => sub.remove();
  }, []);

  // locking drops the bearer from JS and freezes the cache (the thaw is
  // the other way round — it runs before "ready", see goReady)
  useEffect(() => {
    if (phase === "locked") { setToken(null); lockQueryCache(); }
  }, [phase]);

  const unlock = useCallback(async () => {
    const mode = tokenMode.current;
    if (!mode) { setPhase("setup"); return; }
    if (mode === "bio") {
      // the enclave read is the prompt; a cancelled prompt rejects and
      // the gate simply stays closed
      let t: string | null;
      try { t = await readBioToken(); } catch { return; }
      if (!t) { await credentialsLost(); return; }
      await goReady(t);
      return;
    }
    // plain home. Where the device can now bind the token to biometrics
    // (and the gate is wanted), this unlock moves it there. On Android
    // writing a biometric-bound item shows the biometric prompt itself,
    // so that write IS the presence check; on iOS the write is silent
    // and the device-credential check below stands in front of it.
    const wantBio = !SKIP_LOCK && !gateOffRef.current
      && canBindTokenToBiometrics();
    if (wantBio && Platform.OS === "android") {
      const t = await readPlainToken();
      if (!t) { await credentialsLost(); return; }
      try { await saveToken(t, "bio"); } catch { return; }
      tokenMode.current = "bio";
      await goReady(t);
      return;
    }
    // Gate on the device's enrolled security LEVEL, not on biometric
    // enrollment: isEnrolledAsync() reports biometrics only, so a phone
    // secured with just a passcode/PIN would skip the prompt entirely.
    // authenticateAsync itself falls back to the device passcode when no
    // biometric is enrolled. Only a device with no lock method at all
    // just opens — the secure store is still the credential boundary
    // there.
    const level = await LocalAuthentication.getEnrolledLevelAsync();
    if (level !== LocalAuthentication.SecurityLevel.NONE) {
      const r = await LocalAuthentication.authenticateAsync({
        promptMessage: "Unlock Oikonome" });
      if (!r.success) return;
    }
    const t = await readPlainToken();
    if (!t) { await credentialsLost(); return; }
    if (wantBio) {
      try { await saveToken(t, "bio"); tokenMode.current = "bio"; }
      catch { /* stays in the plain home; the next unlock tries again */ }
    }
    await goReady(t);
  }, [credentialsLost, goReady]);

  // The person's gate choice. Off means the token must be readable
  // without a prompt, so it moves to the plain home; on moves it back
  // where the device allows. Only reachable from Settings, i.e. with the
  // token in memory.
  const setGateOff = useCallback(async (off: boolean) => {
    await saveGateOff(off);
    setGateOffState(off);
    gateOffRef.current = off;
    if (!token) return;
    try {
      if (off && tokenMode.current === "bio") {
        await saveToken(token, "plain");
        tokenMode.current = "plain";
      } else if (!off && tokenMode.current === "plain"
                 && !SKIP_LOCK && canBindTokenToBiometrics()) {
        await saveToken(token, "bio");
        tokenMode.current = "bio";
      }
    } catch { /* the next unlock repeats the move */ }
  }, [token]);

  // a credential change is a data boundary: cached queries from the
  // previous account must not survive into (or beside) the next one —
  // the on-disk cache would otherwise outlive even a revoked token.
  // On iOS creating a biometric-bound item is silent, so the fresh token
  // goes straight to the biometric home when the gate is wanted — it never
  // sits readable-without-a-prompt waiting for the first lock. On Android
  // that write IS a biometric prompt, which would add a second prompt to
  // sign-in, so there it lands in the plain home and the first unlock
  // binds it (that unlock is a prompt anyway).
  const completeLogin = useCallback(async (u: string, t: string) => {
    const mode: TokenMode = Platform.OS === "ios" && !SKIP_LOCK
      && !gateOffRef.current && canBindTokenToBiometrics() ? "bio" : "plain";
    // three independent stores — the wipe must only finish before the
    // thaw below reads the disk, not before the credential writes
    await Promise.all([wipeQueryCache(), saveServerUrl(u),
                       saveToken(t, mode).catch(async () => {
                         // a refused biometric write leaves the plain home
                         if (mode === "bio") await saveToken(t, "plain");
                         else throw new Error("could not store the device token");
                       })]);
    tokenMode.current = mode;
    setServerUrl(u);
    await goReady(t);
  }, [goReady]);

  const signOut = useCallback(async () => {
    await credentialsLost();
  }, [credentialsLost]);

  const onAuthLost = useCallback(() => {
    // token revoked (Settings, password change) or idled out — clear it
    // and hand the UI back to the login flow
    credentialsLost();
  }, [credentialsLost]);

  // an add-on made the account read-only and a write was refused (402):
  // the session is fine — reads keep working — so this must never touch
  // the token. One dialog (the api layer collapses repeats) with the
  // server's sentence; the add-on's own dialog knows the way out.
  const onPaywall = useCallback((message: string) => {
    if (ext.paywall) { ext.paywall(message); return; }
    Alert.alert("Account is read-only", message, [{ text: "OK" }]);
  }, []);

  // no token in memory → no client: while locked nothing can call out
  const client = useMemo(
    () => (serverUrl && token
      ? makeClient(serverUrl, token, onAuthLost, onPaywall)
      : null),
    [serverUrl, token, onAuthLost, onPaywall]);

  // first unlock of a launch: offer this device for push tickles.
  // Fire-and-forget — push is a convenience, never a gate.
  useEffect(() => {
    if (phase === "ready" && client) {
      import("./push").then(({ registerNativePush }) =>
        registerNativePush(client)).catch(() => {});
    }
  }, [phase, client]);

  const setCaptureAllowed = useCallback(async (ok: boolean) => {
    await saveCaptureAllowed(ok);
    setCaptureAllowedState(ok);
  }, []);

  const value = useMemo(() => ({
    phase, serverUrl, client, gateOff, setGateOff, captureAllowed,
    setCaptureAllowed, unlock, completeLogin, signOut,
  }), [phase, serverUrl, client, gateOff, setGateOff, captureAllowed,
       setCaptureAllowed, unlock, completeLogin, signOut]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionState {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}
