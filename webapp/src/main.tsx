import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ext } from "./ext";
import { MutationCache, QueryClient, QueryClientProvider }
  from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import App, { notePreRoute } from "./App";
import { HOME_KEY } from "./launchhome";
import { warmRoute } from "./pageChunks";
import { api, AuthError } from "./api/client";
import { ElevationProvider } from "./elevation";
import "./index.css";

// last time the read-only (402) toast fired — module-level so repeated
// blocked writes inside the window collapse into one banner
let lastPaywallToast = 0;

// The status a failed request came back with, read from where the client
// puts it — AuthError for a 401, a leading "NNN: " for every other non-2xx —
// and never from a number that happens to sit in the response body: a 503
// whose detail says "450 rows" is still a 503.
function httpStatus(e: unknown): number | null {
  if (e instanceof AuthError) return 401;
  const m = e instanceof Error ? /^(\d{3}): /.exec(e.message) : null;
  return m ? Number(m[1]) : null;
}

const qc = new QueryClient({
  defaultOptions: { queries: {
    // one retry was enough for a flaky request but not for a phone waking
    // up: the first attempts can fail while the radio reconnects. Retry a
    // few times with backoff, and refetch when the browser reports the
    // connection is back, so returning to a backgrounded tab self-heals
    // instead of needing a manual reload.
    // Only a failure that can heal is worth another attempt: no status
    // at all (the network dropped), a 5xx or a 408. Any other 4xx is the
    // server's answer and asking again only delays it — a 429 included:
    // every refused request counts as a strike toward the address ban,
    // so retrying one would turn a busy minute into a lockout.
    retry: (n: number, e: unknown) => {
      if (n >= 3) return false;
      const s = httpStatus(e);
      return s === null || s >= 500 || s === 408;
    },
    retryDelay: (n: number) => Math.min(1000 * 2 ** n, 8000),
    refetchOnReconnect: true,
    staleTime: 30_000,
  } },
  mutationCache: new MutationCache({
    onError: (err) => {
      // Session truly gone → bounce to login. Re-auth failures on an
      // in-app form (wrong password, totp_required, recovery_required,
      // password_required, passkey step-up) are NOT a lost session —
      // treating them as one reads as a random logout.
      if (err instanceof AuthError || /\b401\b/.test(String(err))) {
        const m = String(err instanceof Error ? err.message : err);
        // "one-time code" rather than "bad one-time": the server also says
        // "current one-time code required" (change email, delete account),
        // which a narrower pattern misses — and a missed re-auth reply
        // blanks the whole app to the login screen, swallowing the message
        // the person needed to read.
        const reauth = /totp_required|recovery_required|password_required|wrong password|one-time code|bad credentials|passkey/i
          .test(m);
        if (!reauth)
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
  "/bills": () => qc.prefetchQuery({ queryKey: ["bills"], queryFn: api.bills }),
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
