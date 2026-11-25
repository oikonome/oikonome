import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ext } from "./ext";
import { MutationCache, QueryCache, QueryClient, QueryClientProvider }
  from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import App, { notePreRoute } from "./App";
import { HOME_KEY } from "./launchhome";
import { warmRoute } from "./pageChunks";
import { api, AuthError } from "./api/client";
import { ElevationProvider } from "./elevation";
import ErrorBoundary from "./components/ErrorBoundary";
import { isReauthReply } from "./reauth";
import { retryRead, retryReadDelay } from "./moneymath";
import "./index.css";

// last time the read-only (402) toast fired — module-level so repeated
// blocked writes inside the window collapse into one banner
let lastPaywallToast = 0;

/** A read that comes back 401 means the session is gone, as a write's
 *  does: back to sign-in, instead of the page showing its error until
 *  /api/me is next asked. Not the ["me"] read itself (App shows the sign-in
 *  page from it), not a step-up refusal, and only while this tab believed
 *  it was signed in — after the sign-out clears the cache there is no
 *  `me`, so a read still in flight cannot start a second one. */
function readLostSession(err: unknown, key: readonly unknown[]): boolean {
  if (!(err instanceof AuthError) || isReauthReply(err.message)) return false;
  if (key[0] === "me") return false;
  return qc.getQueryData(["me"]) !== undefined;
}

const qc = new QueryClient({
  defaultOptions: { queries: {
    // one retry was enough for a flaky request but not for a phone waking
    // up: the first attempts can fail while the radio reconnects. Retry a
    // few times with backoff, and refetch when the browser reports the
    // connection is back, so returning to a backgrounded tab self-heals
    // instead of needing a manual reload. Which failures are asked again
    // is one rule with the phone's (retryRead, moneymath.ts): no answer,
    // a 5xx or a 408 — never a 429, which counts toward the address ban.
    retry: retryRead,
    retryDelay: retryReadDelay,
    refetchOnReconnect: true,
    staleTime: 30_000,
  } },
  queryCache: new QueryCache({
    onError: (err, query) => {
      if (readLostSession(err, query.queryKey))
        window.dispatchEvent(new Event("oiko-auth-lost"));
    },
  }),
  mutationCache: new MutationCache({
    onError: (err) => {
      // Session truly gone → bounce to login. Re-auth failures on an
      // in-app form (wrong password, totp_required, recovery_required,
      // password_required, passkey step-up) are NOT a lost session —
      // treating them as one reads as a random logout.
      // Only our own server's 401 status counts, which the client carries
      // as AuthError. A 401 quoted inside some other reply's text (an AI
      // backend refusing its key, a bucket named "401(k)") is that
      // page's error to show, not a reason to sign out.
      if (err instanceof AuthError) {
        // The marker and wording list live in reauth.ts, shared case for
        // case with the phone's copy.
        if (!isReauthReply(err.message))
          window.dispatchEvent(new Event("oiko-auth-lost"));
        return;
      }
      // An installed add-on may make an account read-only: every write
      // 402s while reads keep working, so buttons read as silent no-ops.
      // Toast the server's sentence — it names the way out — and let a
      // click on the toast land where the add-on says. One toast per short
      // window: a page that fires several writes must not stack the same
      // banner. The add-on's own endpoints are exempt server-side, so this
      // never talks over that page's own error handling.
      if (ext.paywall && /\b402\b/.test(String(err))) {
        const now = Date.now();
        if (now - lastPaywallToast < 10_000) return;
        lastPaywallToast = now;
        const detail = /"detail"\s*:\s*"([^"]+)"/.exec(String(err))?.[1];
        window.dispatchEvent(new CustomEvent("oiko-toast", {
          detail: { text: detail ?? ext.paywall.fallback, to: ext.paywall.to },
        }));
        return;
      }
      // viewers still see mutation controls on some pages; the server
      // 403s, and without this the UI fails silently. Toast a reason.
      // not every 403 is a viewer — demo lockdown and other Forbidden
      // responses carry their own detail. Prefer the server's words; the
      // view-only wording only fits when this user actually is a viewer.
      if (/\b403\b/.test(String(err))) {
        const detail = /"detail"\s*:\s*"([^"]+)"/.exec(String(err))?.[1];
        const me = qc.getQueryData<{ role?: string }>(["me"]);
        window.dispatchEvent(new CustomEvent("oiko-toast", {
          detail: detail
            ?? (me?.role === "viewer"
              ? "View-only access — ask the instance owner to make changes."
              : "Not allowed."),
        }));
      }
    },
  }),
});

// The first requests start here, at module evaluation, so they overlap
// App's own evaluation and first render instead of waiting behind them:
// the query each home page paints from first, keyed exactly as the page
// keys it (Transactions: the current month; the others: their one report)
const HOME_PREFETCH: Record<string, () => void> = {
  "/transactions": () => {
    const now = new Date();
    const params = { y: String(now.getFullYear()), m: String(now.getMonth() + 1) };
    qc.prefetchQuery({ queryKey: ["txns", params],
                       queryFn: () => api.transactions(params) });
  },
  "/bills": () => qc.prefetchQuery({ queryKey: ["bills"], queryFn: () => api.bills() }),
  "/accounts": () => qc.prefetchQuery({ queryKey: ["accounts"], queryFn: api.accounts }),
  "/networth": () => qc.prefetchQuery({ queryKey: ["report", "networth"],
                                        queryFn: api.reportNetworth }),
  "/cashflow": () => qc.prefetchQuery({ queryKey: ["report", "cashflow"],
                                        queryFn: api.reportCashflow }),
};

// /api/me is the gate every page waits on, and the Today payload is what
// the landing page renders. Same keys as the components' useQuery calls,
// so those observers join the in-flight fetch rather than starting one.
// retry:false on me, as App has it: a signed-out browser must reach the
// login redirect at once, not after three retries.
qc.prefetchQuery({ queryKey: ["me"], queryFn: api.me, retry: false });
{
  const path = window.location.pathname.replace(/\/+$/, "");
  const sp = new URLSearchParams(window.location.search);
  // only the live Today view is cheap to guess at — a lens or a past date
  // in the URL means a different query, which the page keys itself
  // a household whose home is another page (Settings → Home) lands
  // there instead: warm that page's chunk and leave the Today payload —
  // the costliest request the server answers — for when it is shown
  let home: string | null = null;
  try { home = localStorage.getItem(HOME_KEY); } catch { /* private mode */ }
  const liveToday = !sp.has("date")
    && (!sp.has("lens") || sp.get("lens") === "today");
  if (path === "/app" && !window.location.search && home && home !== "/"
      && home in HOME_PREFETCH) {
    // the address moves to the home BEFORE React mounts: a redirect
    // issued from inside the router waits for /api/me and /api/settings
    // and, on a cold bundle, flashes Today while the home's chunk loads.
    // The route's own query starts here too, beside /api/me, so the page
    // paints from it the moment its chunk is up. (A home set on another
    // device applies from the next launch: App stores it on settings.)
    // App checks the guess against the server once settings arrive and
    // moves a launch that no longer matches (home back on Today, another
    // household signed in on this browser) to where the server says.
    notePreRoute(home);
    window.history.replaceState(window.history.state, "", "/app" + home);
    warmRoute(home);
    HOME_PREFETCH[home]?.();
  } else if (path === "/app" && liveToday) {
    qc.prefetchQuery({ queryKey: ["today", "now"],
                       queryFn: () => api.todayFull() });
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* outermost: a throw in the shell itself, a lockout screen or the
        elevation sheet has no page boundary above it. App holds a second one
        around the routes, which keeps the nav usable when only a page fails. */}
    <ErrorBoundary>
      <QueryClientProvider client={qc}>
        {/* above the router: the elevation sheet must sit over every page,
            the 2FA gate included, and the request wrapper needs it mounted
            before the first guarded click */}
        <ElevationProvider>
          <BrowserRouter basename="/app">
            <App />
          </BrowserRouter>
        </ElevationProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);

// No app-shell service worker: a cached shell serves stale UI after
// deploys — a real confusion cost for a finance dashboard, where offline is
// a marginal benefit and Vite's hashed asset names already cache-bust
// correctly.
// Web push NEEDS a service worker, so exactly ONE is registered —
// /app/sw.js, display-only, with NO fetch handler, so it can never serve a
// stale bundle (the risk the unregister-everything rule exists for). Any
// OTHER registration a returning browser still carries is dropped, and old
// caches still get cleared.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations()
    .then((regs) => regs.forEach((r) => {
      const src = r.active?.scriptURL || r.installing?.scriptURL
        || r.waiting?.scriptURL || "";
      if (!src.endsWith("/app/sw.js")) r.unregister();
    }))
    .catch(() => {});
  navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" })
    .catch(() => {});   // push simply stays unavailable (http, old browser)
  if (window.caches)
    caches.keys().then((keys) => keys.forEach((k) => caches.delete(k)))
      .catch(() => {});
}
