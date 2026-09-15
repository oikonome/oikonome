import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ext } from "./ext";
import { MutationCache, QueryClient, QueryClientProvider }
  from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import App from "./App";
import { api, AuthError } from "./api/client";
import { ElevationProvider } from "./elevation";
import "./index.css";

// last time the read-only (402) toast fired — module-level so repeated
// blocked writes inside the window collapse into one banner
let lastPaywallToast = 0;

const qc = new QueryClient({
  defaultOptions: { queries: {
    // one retry was enough for a flaky request but not for a phone waking
    // up: the first attempts can fail while the radio reconnects. Retry a
    // few times with backoff, and refetch when the browser reports the
    // connection is back, so returning to a backgrounded tab self-heals
    // instead of needing a manual reload.
    retry: 3,
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
  if (path === "/app" && !sp.has("lens") && !sp.has("date"))
    qc.prefetchQuery({ queryKey: ["today", "now"],
                       queryFn: () => api.todayFull() });
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
