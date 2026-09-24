import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, memo, Suspense, useCallback, useEffect, useMemo, useRef,
  useState, type ReactElement } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation,
  useNavigate, useNavigationType } from "react-router";
import { WEB_HOMES, abortRequestEpoch, api, assistant as assistantApi, AuthError, errText, mmdd, type Me } from "./api/client";
import { saveSettings } from "./api/cache";

// "auto" follows the OS (no data-theme attribute); light/dark pin
type Theme = "auto" | "light" | "dark";
function applyTheme(t: Theme) {
  if (t === "light" || t === "dark")
    document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  localStorage.setItem("oiko-theme", t);
  // browser chrome color follows the effective theme
  const light = t === "light" || (t === "auto" &&
    matchMedia("(prefers-color-scheme: light)").matches);
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", light ? "#f2f5f7" : "#0f141a");
}
import TwoFactorGate from "./components/TwoFactorGate";
import { ext } from "./ext";
import Home from "./pages/Home";
import InviteClaim from "./pages/InviteClaim";
import { deviceZone } from "./devicezone";
import { hasLeftWizard } from "./wizardexit";
import { isOwner, isViewer } from "./role";

// Logged-out users are redirected to the server-rendered /login; there is
// no second in-SPA login screen.

// Every page except Home is a chunk of its own, fetched the first time its
// route is opened. Home is eager because a cold load is usually for it;
// one bundle for the rest would have to be parsed before the first API
// call could even be made, and most of it is pages the session never opens. Keyed by route so warmRoute() below can start
// the right download from the URL alone.
const PAGE = {
  "/welcome": () => import("./pages/Welcome"),
  "/business/setup": () => import("./pages/BusinessSetup"),
  "/retirement/setup": () => import("./pages/RetirementSetup"),
  "/transactions": () => import("./pages/Transactions"),
  "/items": () => import("./pages/Items"),
  "/budget": () => import("./pages/Budget"),
  "/rules": () => import("./pages/Rules"),
  "/merchants": () => import("./pages/Merchants"),
  "/assistant": () => import("./pages/Assistant"),
  "/cashflow": () => import("./pages/CashFlow"),
  "/retirement": () => import("./pages/Retirement"),
  "/debt": () => import("./pages/Debt"),
  "/networth": () => import("./pages/NetWorth"),
  "/alerts": () => import("./pages/Alerts"),
  "/bills": () => import("./pages/Bills"),
  "/bills/history": () => import("./pages/BillsHistory"),
  "/accounts": () => import("./pages/Accounts"),
  "/reimburse": () => import("./pages/Reimburse"),
  "/business": () => import("./pages/Business"),
  "/import": () => import("./pages/Import"),
  "/settings": () => import("./pages/Settings"),
  "/doctor": () => import("./pages/Doctor"),
  "/help": () => import("./pages/Help"),
  "/feedback": () => import("./pages/Feedback"),
  "/testing": () => import("./pages/Feedback"),
};
const Welcome = lazy(PAGE["/welcome"]);
const BusinessSetup = lazy(PAGE["/business/setup"]);
const RetirementSetup = lazy(PAGE["/retirement/setup"]);
const Transactions = lazy(PAGE["/transactions"]);
const Items = lazy(PAGE["/items"]);
const Budget = lazy(PAGE["/budget"]);
const Rules = lazy(PAGE["/rules"]);
const Merchants = lazy(PAGE["/merchants"]);
const Assistant = lazy(PAGE["/assistant"]);
const CashFlow = lazy(PAGE["/cashflow"]);
const Retirement = lazy(PAGE["/retirement"]);
const NetWorth = lazy(PAGE["/networth"]);
const Alerts = lazy(PAGE["/alerts"]);
const Debt = lazy(PAGE["/debt"]);
const Bills = lazy(PAGE["/bills"]);
const BillsHistory = lazy(PAGE["/bills/history"]);
const Accounts = lazy(PAGE["/accounts"]);
const Reimburse = lazy(PAGE["/reimburse"]);
const Business = lazy(PAGE["/business"]);
const Import = lazy(PAGE["/import"]);
const Settings = lazy(PAGE["/settings"]);
const Doctor = lazy(PAGE["/doctor"]);
const Help = lazy(PAGE["/help"]);
const Feedback = lazy(PAGE["/feedback"]);

// The chunk for the page a cold load is opening starts downloading at
// module evaluation, alongside /api/me, instead of after the auth gate has
// resolved — otherwise its round-trip is simply added to the API's. Longest
// prefix wins, so /bills/history warms the history page and /settings/email
// warms Settings. A miss (Home, an unknown path) warms nothing.
function warmRoute(pathname: string) {
  const hit = (Object.keys(PAGE) as (keyof typeof PAGE)[])
    .filter((p) => pathname === p || pathname.startsWith(p + "/"))
    .sort((a, b) => b.length - a.length)[0];
  if (hit) PAGE[hit]().catch(() => {});
}
warmRoute(window.location.pathname.replace(/^\/app(?=\/|$)/, "") || "/");

const LOADING = (
  <div className="centered"><span className="mut">loading…</span></div>);
// One Suspense per route element: a chunk still arriving shows the same
// "loading…" the auth gate shows, inside the shell, with the nav usable.
const lazyRoute = (el: ReactElement) =>
  <Suspense fallback={LOADING}>{el}</Suspense>;

const tab = ({ isActive }: { isActive: boolean }) => (isActive ? "on" : "");

// Every route names its tab, so a
// bookmark says which page it is. "/" (Home) titles itself by lens/date
// and Login owns the signed-out title; every other route resolves here.
// A demo instance says so in the brand — its bookmarks shouldn't
// masquerade as someone's real books.
const PAGE_TITLES: Record<string, string> = {
  "/welcome": "Welcome", "/transactions": "Transactions",
  "/items": "Items", "/budget": "Budget",
  "/rules": "Rules", "/merchants": "Merchants", "/assistant": "Assistant", "/cashflow": "Cash Flow",
  "/retirement": "Retirement", "/networth": "Net Worth", "/debt": "Debt",
  "/alerts": "Alerts", "/bills": "Bills & Income",
  "/bills/history": "Bill history", "/accounts": "Accounts",
  "/reimburse": "Reimbursements", "/business": "Business",
  "/import": "Import", "/settings": "Settings",
  "/doctor": "System health",
  "/help": "Help", "/testing": "Feedback & bug reports",
  "/feedback": "Feedback & bug reports",
  // The two /setup routes carry titles of their own: a half-finished wizard
  // is the kind of page most likely to be left open in a tab, and the bare
  // wordmark would leave its bookmark saying nothing.
  "/business/setup": "Set up a business",
  "/retirement/setup": "Set up retirement",
};

// A route change in an SPA is not a document load, so nothing resets the
// scroll — the new page simply inherits wherever the last one was left.
// Scroll a long Transactions list down, click the logo, and Today opens
// hundreds of pixels in with its verdict off-screen.
// The document is what scrolls here: main is not a scroll container
// (overflow:visible, scrollHeight == clientHeight), so window is the
// element to reset, not any wrapper.
//
// PUSH and REPLACE only. Back and forward arrive as POP, and the browser's
// own scroll restoration is most of what makes them feel like going BACK
// rather than re-opening a page from scratch — forcing those to the top
// would be the same bug wearing the opposite mask. A hash is left alone for
// the same reason: it is an explicit request for a position on the page.
function ScrollToTop() {
  const { pathname, hash } = useLocation();
  const navType = useNavigationType();
  // Only a change of PATH is a new page. A search-param change (the
  // Bills page opens a row's editor as ?edit=payee, replacing the URL) is
  // movement within one, and must not scroll the whole page to the top the
  // moment the ✎ is clicked — which re-running on a navigation-type flip
  // from POP to REPLACE would do. Remember the last path and act only when
  // it actually differs.
  const lastPath = useRef(pathname);
  useEffect(() => {
    const changed = lastPath.current !== pathname;
    lastPath.current = pathname;
    if (navType === "POP" || hash) return;
    if (!changed) return;
    // instant, never smooth: this is a new page arriving, not movement
    // within one. Animating it drags the OLD page's content up the screen
    // on every single click, which reads as the app lurching.
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [pathname, hash, navType]);
  return null;
}

function RouteTitle({ demo }: { demo: boolean }) {
  const { pathname } = useLocation();
  useEffect(() => {
    const brand = demo ? "Oikonome Demo" : "Oikonome";
    // /settings/<section> is still the Settings page — without this
    // every section route fell through to the bare brand, so a bookmarked
    // section said nothing about what it was
    const t = PAGE_TITLES[pathname]
      ?? (pathname.startsWith("/settings/") ? "Settings" : undefined);
    if (t) document.title = `${brand} — ${t}`;
    else if (pathname !== "/") document.title = brand;
  }, [pathname, demo]);
  return null;
}

// a minimal global toast. Listens for the "oiko-toast" event
// (fired by the QueryClient's 402/403 handlers in main.tsx) and shows a
// dismissable banner — so viewers get a reason when a write is blocked.
// The detail is a plain string, or { text, to } when the banner should
// also be a door: an add-on's read-only (402) toast points at its own
// Settings section, and clicking it goes there instead of merely dismissing.
type ToastMsg = { text: string; to?: string };
function Toaster() {
  const [msg, setMsg] = useState<ToastMsg | null>(null);
  const nav = useNavigate();
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<string | ToastMsg>).detail;
      setMsg(typeof d === "string" ? { text: d } : d);
      window.clearTimeout((on as unknown as { t?: number }).t);
      // a long message (a scan summary, a partial-failure list) needs
      // longer than a four-word confirmation — roughly reading speed,
      // never under six seconds
      const text = typeof d === "string" ? d : d.text;
      (on as unknown as { t?: number }).t = window.setTimeout(
        () => setMsg(null), Math.max(6000, text.length * 50));
    };
    window.addEventListener("oiko-toast", on);
    return () => window.removeEventListener("oiko-toast", on);
  }, []);
  if (!msg) return null;
  return (
    <div role="status"
      onClick={() => { if (msg.to) nav(msg.to); setMsg(null); }}
      style={{ position: "fixed", top: 12, left: "50%",
               transform: "translateX(-50%)", zIndex: 1000,
               background: "var(--card)", color: "var(--ink)",
               border: "1px solid var(--line)", borderRadius: "var(--rs)",
               padding: ".6rem 1rem", boxShadow: "0 4px 16px rgba(0,0,0,.3)",
               cursor: "pointer", maxWidth: "90vw" }}>
      {msg.text}
    </div>
  );
}

// the verify-email banner waits out a grace window so a brand-new
// signup isn't nagged on day one. True once the account is ≥3 days old (or
// if we somehow have no timestamp — fail toward showing the nag).
const VERIFY_GRACE_MS = 3 * 24 * 60 * 60 * 1000;
function verifyBannerDue(createdAt: string | null): boolean {
  if (!createdAt) return true;
  const t = Date.parse(createdAt);
  if (Number.isNaN(t)) return true;
  return Date.now() - t >= VERIFY_GRACE_MS;
}

// which help guide the header ? opens per route (longest prefix
// wins). Routes without a guide land on the plain catalog.
const HELP_TOPIC_BY_ROUTE: [string, string][] = [
  ["/transactions", "transactions"], ["/bills", "bills"],
  ["/budget", "budget"], ["/rules", "rules"], ["/merchants", "merchants"],
  ["/assistant", "assistant"], ["/cashflow", "cash-flow"],
  ["/retirement", "retirement"], ["/debt", "debt"],
  ["/networth", "net-worth"], ["/alerts", "alerts"],
  ["/accounts", "accounts"], ["/items", "accounts"],
  ["/reimburse", "reimbursements"], ["/import", "import-history"],
  ["/business", "business"],
  ["/settings", "security"], ["/doctor", "troubleshooting"],
  ["/", "today"],   // last: matches everything (the home lenses)
];

function helpHref(pathname: string): string {
  const hit = HELP_TOPIC_BY_ROUTE.find(([p]) =>
    p === "/" ? pathname === "/" : pathname.startsWith(p));
  return hit ? `/help?topic=${hit[1]}` : "/help";
}

// ---- rail icons -------------------------------------------------
// Inline SVG on currentColor, never an icon font or CDN: the CSP blocks
// external origins outright, and currentColor is what lets one glyph serve idle, hover and active
// states without a second asset. 24px viewBox, 1.75 stroke, round caps —
// matched to the header bell so the two never read as different families.
const I = (d: React.ReactNode) => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
       stroke="currentColor" strokeWidth="1.75" strokeLinecap="round"
       strokeLinejoin="round" aria-hidden="true" focusable="false">{d}</svg>
);

const ICON: Record<string, React.ReactNode> = {
  // a price tag: the merchants you pay, as things with names and logos
  "/merchants": I(<><path d="M20.6 13.4L11 3.8H4v7l9.6 9.6a1.5 1.5 0 0 0 2.1 0l4.9-4.9a1.5 1.5 0 0 0 0-2.1z" />
                    <circle cx="7.5" cy="7.5" r="1.3" /></>),
  // a receipt, not a generic list: this is the money ledger
  "/transactions": I(<><path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3z" />
                      <path d="M9 8h6M9 12h6" /></>),
  // calendar with a due mark — bills are dated obligations
  "/bills": I(<><rect x="3" y="5" width="18" height="16" rx="2" />
                <path d="M3 10h18M8 3v4M16 3v4" /><circle cx="12" cy="15" r="1.6" /></>),
  // a filling gauge: a budget is a bounded amount you are spending into
  "/budget": I(<><path d="M4 17a8 8 0 0 1 16 0" />
                 <path d="M12 17l4.5-4.5" /><circle cx="12" cy="17" r="1.4" /></>),
  // spark: the assistant is generative, not a chat log
  "/assistant": I(<><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
                    <path d="M18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" /></>),
  // a line that goes up AND down — forecast, not growth marketing
  "/cashflow": I(<><path d="M3 17l4.5-5 3.5 3.2L15 9l6-4.5" />
                   <path d="M21 9V4.5h-4.5" /></>),
  // briefcase: the business lane is separate money
  "/business": I(<><rect x="2.5" y="7" width="19" height="13" rx="2" />
                   <path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M2.5 12h19" /></>),
  // hourglass: retirement is the time axis, not a piggy bank
  "/retirement": I(<><path d="M7 3h10M7 21h10" />
                     <path d="M8 3v3.5c0 2 4 3.8 4 5.5s-4 3.5-4 5.5V21" />
                     <path d="M16 3v3.5c0 2-4 3.8-4 5.5s4 3.5 4 5.5V21" /></>),
  // a stair descending to the baseline: balances stepping down to zero
  "/debt": I(<><path d="M3 5h5v5h5v5h5v4H3z" />
               <path d="M3 19h18" /></>),
  // stacked bars climbing: net worth over time
  "/networth": I(<><path d="M4 20V13M10 20V8M16 20v-9M22 20V4" />
                   <path d="M2 20h20" /></>),
  // institution columns — connected banks, not "my account"
  "/accounts": I(<><path d="M3 9.5L12 4l9 5.5" /><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8" />
                   <path d="M3 21h18" /></>),
};

export default function App() {
  const qc = useQueryClient();
  const loc = useLocation();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false });
  // A household with no zone of its own keeps its hours on the instance's
  // clock — fine for a self-hosted box, 2am mail for a hosted household
  // west of the operator. So the first time an OWNER's browser loads with
  // the household still on the instance default and the browser somewhere
  // else, that zone is adopted, once; Settings → Email & Push names it and can
  // change it. Never for a member (their zone is not the household's),
  // never on a demo (read-only), and never over a zone already chosen.
  const zoneOffered = useRef(false);
  useEffect(() => {
    const m = me.data;
    if (!m || zoneOffered.current || m.role !== "owner" || m.demo) return;
    if (m.timezone_source !== "instance") return;
    const device = deviceZone();
    if (!device || device === m.timezone) return;
    zoneOffered.current = true;
    api.settingsSave({ timezone: device })
      .then(() => { qc.invalidateQueries({ queryKey: ["me"] });
                    qc.invalidateQueries({ queryKey: ["settings"] }); })
      .catch(() => {});
  }, [me.data, qc]);
  // the rail is icon-only until you hover it; PINNED keeps it open
  // and lets it take layout width instead of overlaying. Persisted because
  // it is a workspace preference, not a per-visit one — someone still
  // learning nine glyphs should not have to re-pin every session.
  const [railPinned, setRailPinned] = useState(
    () => localStorage.getItem("oiko-rail-pinned") === "1");
  useEffect(() => {
    localStorage.setItem("oiko-rail-pinned", railPinned ? "1" : "0");
  }, [railPinned]);
  // On a phone, hover is the rail's entire reveal mechanism and a touch
  // screen has no hover, so below the breakpoint the SAME <nav> becomes a
  // drawer that slides in from the left behind a ☰ — the rail's open state,
  // permanently expanded, since there is nothing left to gain from hiding
  // labels once the panel is on screen. Same DOM either way, so a
  // destination still cannot exist in one shape and not the other.
  //
  // This one IS React state, unlike the rail's collapse: it renders a scrim
  // and swaps the button's glyph, it fires on a deliberate tap rather than
  // on every navigation, and there is no width transition racing the
  // route's render for the main thread.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const burgerRef = useRef<HTMLButtonElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  // Navigating closes it. The nav's own click handler closes it too, because
  // choosing the route you are already on does not change loc.pathname and
  // would otherwise leave the drawer sitting open over the answer.
  useEffect(() => { setDrawerOpen(false); }, [loc.pathname]);
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setDrawerOpen(false);
      burgerRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    // Without the lock the page behind scrolls under the finger, and on iOS
    // that also drags the drawer itself: position:fixed is relative to the
    // visual viewport, which moves during an overscroll bounce.
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // Focus a control INSIDE the panel, or the next Tab continues from the
    // ☰ behind the scrim and walks the top bar the drawer is covering.
    closeRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [drawerOpen]);
  // Clicking a destination should hand the rail back to the page, but the
  // pointer is still sitting on the link that was just clicked, so :hover
  // holds the panel open over the content the click asked for. CSS cannot
  // undo its own :hover, so the suppression is a class.
  //
  // It is applied by touching the DOM directly, NOT through React state.
  // State here re-renders the whole app — the route's own tree included —
  // and this fires on every navigation, when that tree is already the most
  // expensive thing on the frame. Re-rendering it stalls the main thread on
  // every click, during which the width transition freezes half-collapsed
  // and the rail reads as broken. The rail's open
  // state is not React's business; nothing renders from it.
  const headerRef = useRef<HTMLElement | null>(null);
  const railHold = useRef<{ open: number; move: ((e: MouseEvent) => void) | null }>(
    { open: 0, move: null });
  const releaseRail = useCallback(() => {
    const st = railHold.current;
    if (st.move) {
      document.removeEventListener("mousemove", st.move);
      st.move = null;
    }
    headerRef.current?.classList.remove("rail-hold");
  }, []);
  // Release by POINTER POSITION, not by the header's mouseleave: the collapse
  // pulls the panel out from under the pointer and so fires that very
  // mouseleave, which would reopen the rail; and once collapsed the pointer is
  // already outside the header, so no later mouseleave ever arrives either.
  // Hold across the whole footprint the open panel occupied — going left of
  // the icons is NOT fresh intent, it is where the pointer already sits after
  // clicking an icon rather than a label, which is most clicks.
  const holdRail = useCallback(() => {
    const el = headerRef.current;
    const shell = el?.closest(".shell");
    if (!el || !shell || shell.classList.contains("rail-pinned")) return;
    // read the width ONCE per hold; reading it inside the move handler forces
    // a style recalculation on every pointer event for the whole document
    const open = parseFloat(
      getComputedStyle(shell).getPropertyValue("--rail-open"));
    if (!open) return;                    // phone layout — there is no rail
    releaseRail();
    const move = (e: MouseEvent) => {
      if (e.clientX > railHold.current.open) releaseRail();
    };
    railHold.current = { open, move };
    el.classList.add("rail-hold");
    document.addEventListener("mousemove", move, { passive: true });
  }, [releaseRail]);
  useEffect(() => releaseRail, [releaseRail]);
  // The open rail is as wide as its longest label, not a guessed constant —
  // a guessed fixed width leaves ~85px dead to the right of every item.
  // Measure each item's natural width — set width:max-content,
  // read, restore — and hand the result to the CSS var everything else
  // (main's margin, the clip-path) already derives from.
  // Re-runs when the pin label swaps (Keep open ↔ Unpin) and when `me`
  // resolves, which is when conditional items like Finish setup appear.
  const meData = me.data;
  useEffect(() => {
    const el = headerRef.current;
    const shell = el?.closest<HTMLElement>(".shell");
    if (!el || !shell) return;
    let need = 0;
    el.querySelectorAll<HTMLElement>("nav a, .rail-pin").forEach((a) => {
      const prev = a.style.width;
      a.style.width = "max-content";
      need = Math.max(need, a.offsetWidth);
      a.style.width = prev;
    });
    if (!need) return;
    // + the .bar's horizontal padding and the rail's right border
    const bar = el.querySelector(".bar");
    const pad = bar ? parseFloat(getComputedStyle(bar).paddingLeft) * 2 : 20;
    shell.style.setProperty("--rail-open", `${Math.ceil(need + pad + 1)}px`);
  }, [railPinned, meData]);
  // when any action hits a 401 (session expired/absent), reset `me` so the
  // app drops to the Login screen instead of leaving stale data on screen
  // while every request silently fails (main.tsx fires this).
  useEffect(() => {
    // clear EVERYTHING, not just `me`: the session is gone, and cached
    // financial data must not sit in memory behind the login screen
    // (nor replay if a different account signs in without a full reload)
    const onLost = () => { abortRequestEpoch(); qc.clear(); };
    window.addEventListener("oiko-auth-lost", onLost);
    return () => window.removeEventListener("oiko-auth-lost", onLost);
  }, [qc]);
  // Keep the active destination visible when the drawer opens: ten items
  // plus the separators overflow a short phone in landscape, and opening a
  // nav scrolled to the top hides exactly the item that tells you where you
  // are. block:"nearest" so the page behind never jumps.
  //
  // It runs when the drawer opens, not on navigation: the drawer does not
  // scroll sideways and is closed on every navigation, so running it there
  // would only scroll an off-screen panel.
  useEffect(() => {
    if (!drawerOpen) return;
    document.querySelector(".navscroll a.on")
      ?.scrollIntoView({ block: "nearest" });
  }, [drawerOpen]);
  // theme: localStorage wins instantly (index.html pre-paint script);
  // the server-stored preference reconciles across devices once loaded.
  // The config key is the OWNER's: a viewer's cycle would POST
  // /api/settings (silent 403) and then be reverted by this reconcile.
  // Viewers keep their theme purely client-side (localStorage), so the
  // control works for them and never writes.
  const viewerNow = isViewer(me);
  // A demo is per-device for the same reason a viewer is: its settings
  // writes are refused wholesale (shared printed login), so a server-backed
  // cycle would swallow a 403 and then be quietly reverted by the reconcile
  // below the next time /api/settings is refetched — the control would work
  // for a few minutes and then undo itself.
  const perDeviceTheme = viewerNow || !!me.data?.demo;
  // default DARK — only an explicit "auto" follows the OS
  const [theme, setTheme] = useState<Theme>(
    (localStorage.getItem("oiko-theme") as Theme) || "dark");
  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: api.settings,
                               retry: false, staleTime: 300_000,
                               // header chrome: it waits for /api/me
                               // rather than competing with it
                               enabled: !!me.data });
  useEffect(() => {
    if (!me.data || perDeviceTheme) return;   // per-device choice stands
    // no server-pinned theme → the dark default (not OS-follow)
    const server: Theme = settingsQ.data?.theme ?? "dark";
    if (settingsQ.data && server !== theme) { setTheme(server); applyTheme(server); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsQ.data?.theme, me.data, perDeviceTheme]);
  const cycleTheme = () => {
    const next: Theme = theme === "auto" ? "light"
      : theme === "light" ? "dark" : "auto";
    setTheme(next);
    applyTheme(next);
    if (perDeviceTheme) return;         // localStorage IS the persistence
    // the save returns the full settings view — seed it, instead of
    // refetching to learn the value just written
    saveSettings(qc, { theme: next }).catch(() => {});
  };

  if (me.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (me.isError) {
    if (me.error instanceof AuthError) {
      // family invite links land pre-auth
      if (location.pathname.endsWith("/invite"))
        // a claim starts a new session, so the whole cache is stale —
        // reset (not invalidate) so /me goes back to pending and the app
        // shows "loading…", instead of this page re-peeking the consumed
        // invite into "not valid" while /me is still in its error state
        return <InviteClaim onDone={() => qc.resetQueries()} />;
      // post-erasure landing (the delete cleared the session, so this
      // renders pre-auth) — a clear confirmation, not a bare login form
      if (location.pathname.endsWith("/account-deleted"))
        return <AccountDeletedPage />;
      // single unified login: send logged-out users to the server /login
      // page (passkey + password + forgot all live there now) rather than a
      // second in-SPA login screen.
      window.location.replace("/login");
      return <div className="centered"><span className="mut">redirecting…</span></div>;
    }
    // operator suspension is TOTAL (every API call 403s) — say so
    // plainly instead of the generic can't-reach card
    if (String(me.error).includes("suspended"))
      return <SuspendedPage />;
    // an installed add-on's own frozen states (the server's lockout table
    // carries them too) — each brings the screen that is its way out
    for (const l of ext.lockouts)
      if (l.matches(String(me.error)))
        return <l.Page />;
    // A tenant inside its delete grace window is frozen the same way, and
    // the server says so plainly. Without this branch the one lockout state
    // that is RECOVERABLE would read as "can't reach the server", i.e. as an
    // app bug rather than a countdown the person can still stop.
    if (String(me.error).includes("scheduled for deletion"))
      return <PendingDeletePage />;
    // a signup nobody ever confirmed, frozen by the nightly sweep: the
    // way out is the emailed link, so this screen can ask for it again
    if (String(me.error).includes("never confirmed"))
      return <UnconfirmedPage />;
    // A TRANSIENT failure must not blank a working app. On mobile, leaving
    // the browser and coming back fires a focus-refetch while the radio is
    // still asleep; that one failed request must not replace the whole UI
    // with this card, which only a manual reload would bring back. Auth,
    // suspension and the add-on's lockouts are handled above and still hard-stop
    // regardless — those are real states, not connectivity. Here, if we
    // already have data, keep rendering it and let the query retry.
    if (!me.data)
      return (
        <div className="centered">
          <div className="card">Can't reach the server — is it running?</div>
        </div>
      );
  }

  // hosted accounts must enroll a second factor before the app is
  // usable — the gate renders instead of the routes until it's satisfied
  // (server also exposes the same needs_2fa; this is the client enforcement)
  if (me.data.needs_2fa)
    return <TwoFactorGate onDone={() => qc.invalidateQueries({ queryKey: ["me"] })} />;

  const viewer = isViewer(me);
  // demo: feedback is off and cannot be switched on — the
  // server reports feedback_enabled false for demos regardless of stored
  // config and denies the submit door, so this is belt-and-braces.
  const feedbackOn = settingsQ.data?.feedback_enabled !== false
    && !me.data.demo;
  const owner = isOwner(me);

  return (
    <>
      <Toaster />
      {/* Above the shell, so it covers every route the authed layout can
          reach rather than any one page having to remember to do it. */}
      <ScrollToTop />
      <div className={"shell" + (railPinned ? " rail-pinned" : "")
                      + (drawerOpen ? " drawer-open" : "")}>
      <header className="top" ref={headerRef}><div className="bar"
        onClick={(e) => {
          // On the BAR, not on <nav>. The brand is the Home link and it sits
          // in .topline, OUTSIDE nav, so a handler down there never sees it
          // and clicking the logo would leave the panel stuck open. Anything that navigates from the rail belongs here.
          //
          // A destination, then. Not the pin (a button), not the dead space
          // nav fills to the bottom of the rail, and not the top bar, whose
          // links are painted outside the rail and must not suppress a later
          // hover over the rail itself.
          const link = (e.target as HTMLElement).closest("a[href]");
          if (e.detail > 0 && link && !link.closest(".rt")) {
            // Drop focus, or the rail opens itself later. A click leaves
            // focus on the link it activated, :focus-within stays true
            // indefinitely, and .rail-hold only masks it while the pointer
            // is still over the rail — so the panel sprang open the moment
            // the hold released, with the mouse already on its way somewhere
            // else. Mouse only: a keyboard activation (detail 0) must keep
            // its focus, both to reveal the labels and because moving focus
            // out from under a keyboard user loses their place entirely.
            (link as HTMLElement).blur();
            holdRail();
          }
          // …and on a phone the drawer closes on the same condition,
          // including for a link to the route already showing, which never
          // reaches the pathname effect
          if ((e.target as HTMLElement).closest("a[href]"))
            setDrawerOpen(false);
          // Same blind spot, same fix: choosing the page you are ALREADY on
          // does not change pathname, so ScrollToTop never fires — and the
          // logo while already on Today is exactly the click someone makes
          // to get back to the top. Cheap to do unconditionally; when the
          // route does change the effect just repeats a scroll to 0.
          if (link && !link.closest(".rt"))
            window.scrollTo({ top: 0, left: 0, behavior: "instant" });
        }}>
        <div className="topline">
          {/* Phone only (display:none above the breakpoint, where the rail
              opens on hover and needs no button). It sits BEFORE the brand
              because that is where a drawer's handle is expected, and the
              drawer opens from that edge. */}
          <button type="button" className="navtoggle" ref={burgerRef}
                  aria-expanded={drawerOpen} aria-controls="mainnav"
                  aria-label="Open navigation" title="Menu"
                  onClick={() => setDrawerOpen((v) => !v)}>
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none"
                 stroke="currentColor" strokeWidth="1.9" strokeLinecap="round"
                 strokeLinejoin="round" aria-hidden="true" focusable="false">
              <path d="M4 7h16M4 12h16M4 17h16" />
            </svg>
          </button>
          {/* the logo IS the home button: home carries the
              Today|Week|Month|Year lens picker; no Today tab */}
          <NavLink to="/" className="brand" title="Home">
            <img src="/app/icon.svg" alt="" width={20} height={20} />
            <span className="hide-m">Oikonome</span>
          </NavLink>
          <span className="rt">
            {/* no "view-only" pill: the role shows in what a viewer can
                and cannot do, not as a badge */}
            {/* STATUS, not a control. A sync button here would be almost
                always a no-op on a webhook-fed instance: transactions
                arrive within seconds of the
                bank feeding Plaid, and everything else is on the hourly
                sweep — so the click usually did nothing visible, which
                is how a person learns a control is broken. The manual
                door that survives is the per-connection ↻ on Accounts,
                where the result can be explained against that bank's own
                clock. Nothing here can make a bank send data; only
                Plaid's /transactions/refresh could, and the app does not
                call it. */}
            <SyncChip viewer={viewer} />
            {/* Feedback is icon-only, left of Help — keeps the top bar
                quiet while still one click away. */}
            {feedbackOn && (
              <NavLink to="/feedback"
                title="Feedback & bug reports"
                aria-label="Feedback & bug reports"
                style={{ alignSelf: "center", width: 28, height: 28,
                         lineHeight: "28px", textAlign: "center",
                         borderRadius: "50%", border: "1px solid var(--line)",
                         fontSize: 14, textDecoration: "none",
                         color: "var(--blue)" }}>
                {/* Inline SVG, not the 💬 emoji: the emoji renders as a
                    filled colour glyph no CSS can restyle, so it sat white
                    and solid in a bar of currentColor outlines. Same lesson
                    as the bell below. */}
                <svg aria-hidden="true" width="14" height="14"
                     viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2" strokeLinecap="round"
                     strokeLinejoin="round"
                     style={{ display: "block", margin: "0 auto",
                              position: "relative", top: 6 }}>
                  <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 8.5-8.5h.5a8.5 8.5 0 0 1 8 8v.5z" />
                </svg>
              </NavLink>
            )}
            {/* help lives behind a ? by the user menu, not a
                nav text item. the ? deep-links the guide for the
                page you're ON (?topic=…); unknown routes land on the
                catalog as before. */}
            {/* Alerts as a bell instead of a nav item.
                A text tab cannot carry a COUNT; this can, and the count
                is the whole reason to look. Severity drives the dot colour
                from the same g/a/r vocabulary the Alerts rows use, so an
                amber "heads up" never looks like a red "something broke".
                Matches the ? circle's geometry deliberately — two icons of
                different sizes in one cluster read as an accident. */}
            <AlertsBell />
            <NavLink to={helpHref(loc.pathname)}
              title="Help — guides, search, FAQ"
              aria-label="Help — guides, search, FAQ"
              style={{ alignSelf: "center", width: 28, height: 28,
                       lineHeight: "28px", textAlign: "center",
                       borderRadius: "50%", border: "1px solid var(--line)",
                       fontSize: 13, textDecoration: "none",
                       color: "var(--blue)" }}>?</NavLink>
            <details className="usermenu">
              {/* long addresses truncated unreadably — show the
                  local part in the chrome; the full address lives on
                  hover and as the menu's first line */}
              <summary title={me.data.email}>
                {me.data.email.split("@")[0]}</summary>
              <div className="usermenu-panel">
                <span className="usermenu-email">{me.data.email}</span>
                <NavLink to="/settings"
                  onClick={(e) => (e.target as HTMLElement).closest("details")
                    ?.removeAttribute("open")}>Settings</NavLink>
                <button onClick={cycleTheme}>
                  Theme: {theme}
                  {theme === "auto" ? " (follows device)" : ""}</button>
                <button onClick={() =>
                  // hard redirect, not invalidateQueries() — the latter
                  // refetched every query, each now 401, cascading through
                  // the auth-redirect (the visible sign-out lag). Land on
                  // the SPA login (/app/) so the demo returns to its
                  // unified login: land on /login (the single login page —
                  // passkey + password + forgot + demo-prefill all live there).
                  api.logout().finally(() => { window.location.href = "/login"; })}>
                  Sign out</button>
              </div>
            </details>
          </span>
        </div>
        {/* one <nav> serves both shapes — a left icon rail on
            desktop, a slide-in drawer on a phone. Same DOM, different
            CSS, so a destination can never exist in one and not the
            other. Groups run in order: manage / reports / wealth /
            system. */}
        <nav id="mainnav">
          {/* Drawer-only head. The open panel covers the top bar, so the
              brand and the ☰ that opened it are both behind the scrim and
              unreachable — the close control and the Home link have to be
              inside the panel or they do not exist while it is open. */}
          <div className="drawer-head">
            <NavLink to="/" className="drawer-brand" title="Home">
              <img src="/app/icon.svg" alt="" width={22} height={22} />
              <span>Oikonome</span>
            </NavLink>
            <button type="button" className="drawer-close" ref={closeRef}
                    aria-label="Close navigation" title="Close"
                    onClick={() => { setDrawerOpen(false);
                                     burgerRef.current?.focus(); }}>
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
                   stroke="currentColor" strokeWidth="1.9"
                   strokeLinecap="round" strokeLinejoin="round"
                   aria-hidden="true" focusable="false">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
          </div>
          <div className="navscroll">
          <NavLink to="/transactions" className={tab}><span className="nav-i">{ICON["/transactions"]}</span><span className="nav-t">Transactions</span></NavLink>
          <NavLink to="/bills" end className={tab}><span className="nav-i">{ICON["/bills"]}</span><span className="nav-t">Bills &amp; Income</span></NavLink>
          <NavLink to="/budget" end className={tab}><span className="nav-i">{ICON["/budget"]}</span><span className="nav-t">Budget</span></NavLink>
          <span className="navsep" />
          <AssistantTab />
          <NavLink to="/cashflow" className={tab}><span className="nav-i">{ICON["/cashflow"]}</span><span className="nav-t">Cash Flow</span></NavLink>
          <NavLink to="/debt" className={tab}><span className="nav-i">{ICON["/debt"]}</span><span className="nav-t">Debt</span></NavLink>
          {/* Merchants is deliberately NOT in the main nav: it is a
              correction tool you visit occasionally, not a daily surface,
              and lives under Settings → Merchants (its
              own page at /merchants, same as Rules). */}
          <span className="navsep" />
          <NavLink to="/retirement" className={tab}><span className="nav-i">{ICON["/retirement"]}</span><span className="nav-t">Retire</span></NavLink>
          <NavLink to="/networth" className={tab}><span className="nav-i">{ICON["/networth"]}</span><span className="nav-t">Net Worth</span></NavLink>
          <span className="navsep" />
          {/* Alerts moved to the header bell (it needed a count) and
              Settings has always been in the user menu as "Settings &
              sessions" — a second entry point earned nothing. Accounts
              stays: it is the only route to fixing a broken bank
              connection, and "Accounts" inside a menu opened from your own
              email address reads as user accounts, not bank accounts. */}
          {/* the Business tab appears only once a business EXISTS.
              Most households never run one, and a
              permanently empty top-level tab makes the app look like it is
              built for someone else. Set one up in Settings → Guided setup →
              Business wizard and the tab appears. The /business route stays
              reachable either way, so a bookmark or a stale link still lands
              on the page (which has its own empty state pointing at the
              wizard) instead of a blank screen. */}
          {me.data.has_business &&
            <NavLink to="/business" className={tab}><span className="nav-i">{ICON["/business"]}</span><span className="nav-t">Business</span></NavLink>}
          <NavLink to="/accounts" className={tab}><span className="nav-i">{ICON["/accounts"]}</span><span className="nav-t">Accounts</span></NavLink>
          <SetupTab owner={owner} />
          {/* pin lives at the END of the rail, after the destinations: it
              is chrome for the nav, not a destination itself. Hidden on the
              phone bar, where there is nothing to pin. */}
          <button type="button" className="rail-pin"
                  aria-pressed={railPinned}
                  title={railPinned ? "Unpin the sidebar" : "Keep the sidebar open"}
                  aria-label={railPinned ? "Unpin the sidebar" : "Keep the sidebar open"}
                  onClick={(e) => {
                    setRailPinned((v) => !v);
                    // Drop focus on mouse activation, exactly as the nav
                    // links do: unpinning leaves focus on this button, and
                    // :focus-within would hold the rail open long after
                    // the pointer had gone. Keyboard clicks
                    // (detail 0) keep focus and the labels stay revealed.
                    if (e.detail > 0) e.currentTarget.blur();
                  }}>
            <span className="nav-i" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
                   stroke="currentColor" strokeWidth="1.75"
                   strokeLinecap="round" strokeLinejoin="round">
                {railPinned ? <path d="M15 6l-6 6 6 6" />
                            : <path d="M9 6l6 6-6 6" />}
              </svg>
            </span>
            <span className="nav-t">{railPinned ? "Unpin" : "Keep open"}</span>
          </button>
        </div></nav>
      {/* The scrim is a SIBLING OF <nav>, inside the header, and that
          placement is the whole fix for a drawer that came up dimmed.
          header.top is position:sticky with z-index:5, which makes it a
          stacking context, so nav's z-index:40 never escapes it — it is
          only ever "40 within the header's 5". Rendered outside the header
          the scrim's 39 beat that 5 outright and painted over the panel,
          menu and all, which read as the nav dimming itself. Exactly the
          trap .rt hits one element over, in the other direction: a z-index
          cannot lift a descendant out of its ancestor's layer. Inside, the
          two compare in one context and 40 > 39 means what it says.

          Rendered only while open, so it costs nothing the rest of the time
          and can never swallow a tap because a transition left it around.
          aria-hidden: the drawer already carries the close control, and a
          screen reader announcing an unlabelled div is noise. */}
      {drawerOpen && (
        <div className="navscrim" aria-hidden="true"
             onClick={() => { setDrawerOpen(false);
                              burgerRef.current?.focus(); }} />
      )}
      </div></header>
      {/* demo instances wipe + regenerate on the hour (worker
          cron minute=0) — a visible countdown so visitors know the sandbox
          resets itself */}
      {me.data?.demo && <DemoResetCountdown />}
      {/* operator broadcast. A deadline makes it a REBOOT
          countdown — not dismissible; ticks down, then overlays and
          reconnects. Plain broadcasts stay session-dismissible. */}
      {me.data?.broadcast && (me.data.broadcast.deadline
        ? <RebootCountdown deadline={me.data.broadcast.deadline}
                           message={me.data.broadcast.message} />
        : sessionStorage.getItem("bc-" + me.data.broadcast.message) === null && (
        <div className={`note topnote ${me.data.broadcast.severity === "warn" ? "bad" : "info"}`}>
          <span style={{ flex: 1 }}>
            {me.data.broadcast.severity === "warn" ? "⚠" : "•"}{" "}
            {me.data.broadcast.message}</span>
          <button className="dismiss" title="dismiss"
                  onClick={(e) => {
                    sessionStorage.setItem("bc-" + me.data!.broadcast!.message, "1");
                    (e.currentTarget.parentElement as HTMLElement).style.display = "none";
                  }}>✕</button>
        </div>
      ))}
      {/* the mail provider has told us this address is
          undeliverable. That is a FACT about the address, not a guess about
          a young account, so it skips the grace window and it shows
          on self-host too — a refused relay is exactly the thing a
          self-hoster needs told. Red, not yellow: the daily verdict is the
          product, and right now it is not arriving. The provider's own
          words are quoted rather than paraphrased, because "unknown user"
          is what makes a typo obvious to the person who typed it. */}
      {me.data?.email_delivery && (
        <div className="note bad topnote">
          <span style={{ flex: 1 }}>
            <b>Your email isn't arriving.</b>{" "}
            {me.data.email_delivery.state === "complained"
              ? <>Mail to <b>{me.data.email}</b> was marked as spam, so we've
                  stopped sending it.</>
              : <>We couldn't deliver to <b>{me.data.email}</b>
                  {me.data.email_delivery.bounce_type === "HardBounce"
                    ? " — the mail server says the address doesn't exist"
                    : ""}.</>}
            {me.data.email_delivery.did_you_mean && (
              <> Did you mean <b>{me.data.email_delivery.did_you_mean}</b>?</>)}
            {" "}Scheduled emails are paused until it's fixed.{" "}
            <Link to="/settings/email">Fix your address</Link>.
          </span>
        </div>
      )}
      {/* hosted accounts verify their email — the daily verdict
          email is the product, so an unverified address gets one calm,
          persistent nudge (server 403s nothing; this is only a nag).
          HELD until the account is ≥3 days old — a fresh signup
          isn't nagged on day one; the banner appears only if still
          unverified after the grace window.
          suppressed while the bounce banner is up — two banners
          about the same address, one calm and one urgent, would read as a
          contradiction. */}
      {me.data?.hosted && me.data.verified === false
        && !me.data.email_delivery
        && verifyBannerDue(me.data.created_at) && (
        <div className="note topnote">
          <span style={{ flex: 1 }}>
            Verify your email — we sent a link to <b>{me.data.email}</b>.
            No email? Check spam, or resend.
            {me.data.verify_deadline && <>
              {" "}Unconfirmed accounts are frozen on{" "}
              {mmdd(me.data.verify_deadline)} and deleted a week later.</>}
          </span>
          <button style={{ whiteSpace: "nowrap" }}
            onClick={() => api.verifyResend()
              .then((r) => window.dispatchEvent(new CustomEvent("oiko-toast",
                { detail: r.verified ? "Already verified — reload."
                                     : "Verification email sent." })))
              .catch((e) => window.dispatchEvent(new CustomEvent("oiko-toast",
                { detail: `Resend failed: ${errText(e)}` })))}>
            Resend</button>
        </div>
      )}
      <RouteTitle demo={!!me.data.demo} />
      <main>
        <AppRoutes me={me.data} homeWeb={settingsQ.data?.home_web}
                   homeKnown={settingsQ.isSuccess || settingsQ.isError} />
        <footer className="mut" style={{ textAlign: "center", fontSize: 12,
                 padding: "1.2rem 0 .8rem", opacity: .7 }}>
          {/* the version opens the release notes on GitHub — the releases
              index, which always exists, not a tag page */}
          <a href={RELEASES} target="_blank"
             rel="noopener noreferrer" style={{ color: "inherit" }}>
            Oikonome {me.data.version}</a>
          {ext.legal.privacy && (<>
            {" · "}
            <a href={ext.legal.privacy} target="_blank"
               rel="noopener noreferrer" style={{ color: "inherit" }}>Privacy</a>
          </>)}
          {me.data.donate_url && (<>
            {" · "}
            <a href={me.data.donate_url} target="_blank"
               rel="noopener noreferrer" style={{ color: "inherit" }}>
              ♥ Support development</a>
          </>)}
        </footer>
      </main>
      </div>{/* .shell */}
    </>
  );
}

const RELEASES = "https://github.com/oikonome/oikonome/releases";

// ---- header chrome as leaves --------------------------------------
// Each of these owns its own query, so a poll or resolve re-renders one
// chip, one bell, one tab — not App, whose re-render takes the nav, the
// theme, the drawer AND the current page with it. What each one fetches starts once /api/me has resolved, because
// none of them mounts before the auth gate passes.

function SyncChip({ viewer }: { viewer: boolean }) {
  const qc = useQueryClient();
  // header chip: relative age of the last successful pull (last_sync)
  // while a sync is in flight this is the progress feed, so it
  // polls at a pace you can watch. Idle it is 20s, not 60s, and that is
  // the whole difference between showing scheduled syncs
  // and only pretending to: a cron pull often finishes inside 40 seconds,
  // so a minute-long gap would step straight over most of them and the
  // feature would work solely for syncs the user started by hand. Three
  // requests a minute for a couple of indexed reads, and react-query stops
  // polling entirely once the tab is hidden.
  const conns = useQuery({ queryKey: ["connections"], queryFn: api.connections,
                           retry: false,
                           refetchInterval: (q) =>
                             q.state.data?.syncing ? 1_500 : 20_000 });
  const syncing = conns.data?.syncing ?? false;
  const syncProg = conns.data?.sync_progress ?? null;
  // When a sync FINISHES, every figure on screen is stale — balances, the
  // verdict, the month's totals. Refetch on the running→idle edge only: a
  // plain "invalidate whenever not syncing" would refetch the whole app on
  // every 60s poll forever.
  const wasSyncing = useRef(false);
  useEffect(() => {
    if (wasSyncing.current && !syncing) qc.invalidateQueries();
    wasSyncing.current = syncing;
  }, [syncing, qc]);
  // mounted for a viewer too (the refetch-on-finish above is for them as
  // well); only the chip itself is owner/member chrome
  if (viewer) return null;
  const rows = conns.data?.connections ?? [];
  // nothing to report until there is a connection to have read
  if (!syncing && rows.length === 0) return null;
  // The clock is the server's phrase ("synced 12m ago"); the chip owns
  // the verb, so only the age is kept.
  const age = (conns.data?.last_sync ?? "").replace(/^synced\s+/, "");
  // amber = a connection that needs the person's hand (re-auth, gone);
  // a bank that is merely down or slow is green like any other
  // "not right now" — red/amber for that is what made them stop meaning
  // anything. Same rule as the dot on Accounts.
  const attention = rows.some((c) => {
    const k = c.status_kind
      ?? (c.status && c.status !== "ok" ? "attention" : "ok");
    return k !== "ok" && k !== "retrying";
  });
  // Status, not a control: a small dot says "healthy" at a glance, the
  // words carry the clock; no
  // border, no background, no hover — nothing that reads as a button,
  // because on a webhook-fed instance there is nothing to press. While a
  // sync runs the dot turns blue and pulses and the fill behind the words
  // is the progress bar, so the one moment it carries information still
  // shows.
  return (
    <span className={"syncchip" + (syncing ? " syncing" : "")}
      aria-live="polite"
      aria-busy={syncing}
      title={syncing
        ? "checking your connections for anything new"
        : (attention
            ? "a connection needs your attention — see Accounts"
            : "when your connections were last read. Banks send data "
              + "to Plaid on their own schedule; each connection's own "
              + "clock is on the Accounts page.")}>
      {/* the fill IS the progress bar: the chip does not change
          shape, it just fills, so the header never reflows mid-sync.
          Determinate once the worker has reported how many
          connections there are; a slow indeterminate sweep until
          then, because a bar that sits at 0% reads as stuck. */}
      {syncing && (
        // done===0 is still indeterminate. Knowing the denominator
        // is not the same as having made progress, and a fill of
        // width 0 shows nothing moving at all: "syncing 0 of N" over
        // a 0px bar.
        <span className={"syncfill" + (syncProg && syncProg.done > 0
                                       ? "" : " indet")}
              style={syncProg && syncProg.done > 0
                ? { width: `${Math.round(100 * syncProg.done / syncProg.total)}%` }
                : undefined} />
      )}
      <span className="syncchip-t">
        <span className={"syncdot"
                         + (syncing ? " busy" : attention ? " warn" : "")}
              aria-hidden="true" />
        {syncing
          ? (!syncProg
              ? "checking banks…"
              // every bank has answered but the run is still going:
              // the post-pull passes. A real sync can sit on "N of N"
              // and keep spinning for a while,
              // which reads as stuck rather than nearly done — so the
              // tail NAMES its stage. "finishing up…" stays
              // as the fallback for a phase the server did not send.
              : syncProg.done >= syncProg.total
                ? (syncProg.phase === "products"
                    ? "balances & holdings…"
                    : syncProg.phase === "categorize"
                      ? "categorising…"
                      : "finishing up…")
                : `checking banks · ${syncProg.done} of ${syncProg.total}`)
          : (attention
              ? (age ? `needs attention · ${age}` : "needs attention")
              : (age ? `up to date · ${age}` : "not read yet"))}
      </span>
    </span>
  );
}

function AlertsBell() {
  // Alerts live in the header bell, so the count has to be here rather
  // than on the Alerts page. Cheap: the same /alerts/history the
  // page already uses, shared through the query cache, so opening Alerts is
  // still one request in total. Only ACTIVE and UNDISMISSED rows count — a
  // dismissed warning that is still technically active is one the user has
  // already answered, and re-badging it would train them to ignore the bell.
  const alertsQ = useQuery({ queryKey: ["alertsHistory"],
                             queryFn: api.alertsHistory, retry: false,
                             staleTime: 60_000 });
  const live = useMemo(
    () => (alertsQ.data?.alerts || []).filter(
      (a) => a.active && !a.dismissed),
    [alertsQ.data]);
  const alertN = live.length;
  // worst severity drives the dot colour, using the same vocabulary as the
  // rows on the page: bad → red, warn → amber, anything else → blue. An
  // amber "heads up" must not look like a red "something broke".
  const alertColor = live.some((a) => a.severity === "bad") ? "var(--red)"
    : live.some((a) => a.severity === "warn") ? "var(--amber)"
    : "var(--blue)";
  const alertLabel = alertN === 0 ? "Alerts — nothing needs attention"
    : `Alerts — ${alertN} need${alertN === 1 ? "s" : ""} attention`;
  return (
    <NavLink to="/alerts"
      title={alertLabel} aria-label={alertLabel}
      style={{ alignSelf: "center", position: "relative",
               width: 28, height: 28, lineHeight: "28px",
               textAlign: "center", borderRadius: "50%",
               border: "1px solid var(--line)", fontSize: 14,
               textDecoration: "none", color: "var(--blue)" }}>
      {/* Inline SVG, not the 🔔 emoji: the emoji renders gold on
          every platform and no CSS can recolour it, so it read as a
          warning badge sitting permanently next to a neutral ?.
          currentColor inherits var(--ink) exactly like the ? glyph,
          so the two icons are the same weight and colour until the
          badge says otherwise. */}
      <svg aria-hidden="true" width="14" height="14"
           viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="2" strokeLinecap="round"
           strokeLinejoin="round"
           style={{ display: "block", margin: "0 auto",
                    position: "relative", top: 6 }}>
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
        <path d="M13.73 21a2 2 0 0 1-3.46 0" />
      </svg>
      {alertN > 0 && (
        <span aria-hidden="true" style={{
          position: "absolute", top: -3, right: -3, minWidth: 15,
          height: 15, lineHeight: "15px", padding: "0 3px",
          borderRadius: 999, fontSize: 10, fontWeight: 700,
          color: "#fff", background: alertColor,
          border: "1px solid var(--card)" }}>
          {alertN > 9 ? "9+" : alertN}</span>
      )}
    </NavLink>
  );
}

function AssistantTab() {
  const asstAvail = useQuery({ queryKey: ["assistant-status"],
                              queryFn: assistantApi.status, retry: false });
  if (!asstAvail.data?.available) return null;
  return (
    <NavLink to="/assistant" className={tab}><span className="nav-i">{ICON["/assistant"]}</span><span className="nav-t">Assistant</span></NavLink>
  );
}

// Whether the guided setup still has steps to offer — the nav pill and
// the root-route redirect both ask. Same cached query either way.
function useSetupPending(owner: boolean): boolean {
  // offer the guided wizard until every step is done or dismissed
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding,
                        retry: false, staleTime: 30_000 });
  // the pill leads into the guided setup, which connects banks and
  // configures the instance — the owner's walk, so a member is not
  // offered a shortcut into a page that tells them to ask someone else
  const setupPending = owner && !!ob.data && !ob.data.wizard_done &&
    !(ob.data.accounts > 0 && ob.data.transactions > 0 &&
      ob.data.bills > 0 && ob.data.budgets_set);

  return setupPending;
}

function SetupTab({ owner }: { owner: boolean }) {
  const setupPending = useSetupPending(owner);
  if (!setupPending) return null;
  return (
    /* Same anatomy as every sibling — .nav-i icon + .nav-t label —
       because the collapsed rail clips anything else — a bare text
       node would show as "Fini…". The green lives on the class. */
    <NavLink to="/welcome" title="Finish setup"
      className={(p) => tab(p) + " nav-finish"}>
      <span className="nav-i">{I(<>
        <path d="M4 21V4" />
        <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V4s-1 1-4 1-5-2-8-2-4 1-4 1" />
      </>)}</span>
      <span className="nav-t">Finish setup</span>
    </NavLink>
  );
}

// The routes, memoised on the signed-in row alone: App re-renders for the
// drawer, the theme, the rail pin and every /api/me refetch, and none of
// those is a reason to re-render the page. Navigation still reaches
// <Routes> through the router context, not through props.
// The default home page applies to a LAUNCH, not to a navigation: decided
// once at module load from where the page actually opened. A deep link,
// a bookmark to another page, or a lens query on "/" is an intent of its
// own and is left alone; a later click on the logo really means Today.
// (Decided here rather than in render so React's double-invoked renders
// under StrictMode cannot consume the redirect before it happens.)
let launchHome = (() => {
  const p = window.location.pathname.replace(/\/+$/, "");
  return (p === "/app" || p === "") && !window.location.search;
})();

function HomeGate({ homeWeb, homeKnown, hasBusiness }: {
  homeWeb: string | null | undefined; homeKnown: boolean;
  hasBusiness: boolean;
}) {
  const pending = launchHome;
  useEffect(() => { if (homeKnown) launchHome = false; }, [homeKnown]);
  if (pending) {
    if (!homeKnown) return null;
    const known = WEB_HOMES.some(([r]) => r === homeWeb);
    const target = known && (homeWeb !== "/business" || hasBusiness)
      ? homeWeb : "/";
    if (target && target !== "/") return <Navigate to={target} replace />;
  }
  return <Home />;
}

const AppRoutes = memo(function AppRoutes({ me, homeWeb, homeKnown }: {
  me: Me; homeWeb: string | null | undefined; homeKnown: boolean;
}) {
  // A new account lands in the guided setup, not on an empty dashboard.
  //
  // Signup redirects to /app/welcome, but a LOGIN always lands on /app/ —
  // and on hosted the first thing after signup is email verification and
  // the 2FA gate, so the very next thing anyone does is log in. The wizard
  // would otherwise be reachable only through the green "Finish setup"
  // pill in the nav: easy to miss, and on a phone it sits behind the menu.
  // First login should walk you through the welcome wizard.
  //
  // ONLY from the root, and only until they leave on purpose — the wizard's
  // own exits navigate here, so without `hasLeftWizard()` this would bounce
  // them back in with no way out. `ob.data` gates it, so nothing redirects
  // before onboarding state has loaded (no flash, no wrong guess).
  const wantsWizard = useSetupPending(isOwner({ data: me }))
    && !hasLeftWizard(me.tenant_id);
  return (
    <Routes>
      <Route path="/" element={
        wantsWizard ? <Navigate to="/welcome" replace />
        : <HomeGate homeWeb={homeWeb} homeKnown={homeKnown}
                    hasBusiness={!!me.has_business} />} />
      {/* signed-in users hitting an invite link just go home */}
      {/* an invite link opened while SOMEONE ELSE is signed in on
          this browser (the owner, typically) must not be swallowed
          into their session — the invitee gets a fresh login */}
      <Route path="/invite" element={<InviteWhileSignedIn email={me.email} />} />
      <Route path="/welcome" element={lazyRoute(<Welcome />)} />
      <Route path="/business/setup" element={lazyRoute(<BusinessSetup />)} />
      <Route path="/retirement/setup" element={lazyRoute(<RetirementSetup />)} />
      <Route path="/transactions" element={lazyRoute(<Transactions />)} />
      <Route path="/items" element={lazyRoute(<Items />)} />
      <Route path="/budget" element={lazyRoute(<Budget />)} />
      {/* the planner lives on /budget */}
      <Route path="/balance"
             element={<Navigate to="/budget" replace />} />
      {/* Spending is the Cash Flow "spending" tab, not a page of its
          own — old bookmarks and emailed links still land on the report */}
      <Route path="/spending"
             element={<Navigate to="/cashflow?tab=spending" replace />} />
      <Route path="/rules" element={lazyRoute(<Rules />)} />
      <Route path="/merchants" element={lazyRoute(<Merchants />)} />
      <Route path="/assistant" element={lazyRoute(<Assistant />)} />
      <Route path="/cashflow" element={lazyRoute(<CashFlow />)} />
      {/* Fees is a section of Net Worth, not a tab —
          old bookmarks and emailed links still land somewhere real */}
      <Route path="/fees" element={<Navigate to="/networth" replace />} />
      <Route path="/retirement" element={lazyRoute(<Retirement />)} />
      <Route path="/networth" element={lazyRoute(<NetWorth />)} />
      <Route path="/debt" element={lazyRoute(<Debt />)} />
      <Route path="/alerts" element={lazyRoute(<Alerts />)} />
      <Route path="/bills" element={lazyRoute(<Bills />)} />
      <Route path="/bills/history" element={lazyRoute(<BillsHistory />)} />
      <Route path="/accounts" element={lazyRoute(<Accounts />)} />
      <Route path="/reimburse" element={lazyRoute(<Reimburse />)} />
      <Route path="/business" element={lazyRoute(<Business />)} />
      <Route path="/import" element={lazyRoute(<Import />)} />
      <Route path="/settings" element={lazyRoute(<Settings />)} />
      {/* a section is a route, so it deep-links and Back works */}
      <Route path="/settings/:sec" element={lazyRoute(<Settings />)} />
      <Route path="/doctor" element={lazyRoute(<Doctor />)} />
      <Route path="/help" element={lazyRoute(<Help />)} />
      <Route path="/testing" element={lazyRoute(<Feedback />)} />
      <Route path="/feedback" element={lazyRoute(<Feedback />)} />
      {/* unknown client routes get a branded not-found view
          instead of a blank main area (the server serves index.html
          for every /app path, so this is the only 404 a user sees
          under /app) */}
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
});

// The demo's hourly-reset countdown. The reset cron fires at
// minute 0 UTC, i.e. on the epoch hour boundary — so the remaining time
// is purely client-clock math, no server round-trip.
// branded not-found for unknown client routes — renders inside
// the app chrome (nav stays usable), styled like the suspended/deleted
// cards, linking back to Today. The classical garnish mirrors the
// server-side error pages: real quotes, Greek first, translation beneath
// (οἰκονόμος is a Greek word — the house style gets to wink in Greek).
const NOT_FOUND_QUOTES: [string, string, string][] = [
  ["πάντα χωρεῖ καὶ οὐδὲν μένει",
   "Everything flows and nothing stays.",
   "Heraclitus (Plato, Cratylus 402a)"],
  ["φύσις κρύπτεσθαι φιλεῖ",
   "Nature loves to hide.",
   "Heraclitus, DK B123"],
  ["ἐὰν μὴ ἔλπηται ἀνέλπιστον, οὐκ ἐξευρήσει",
   "If you do not expect the unexpected, you will not find it.",
   "Heraclitus, DK B18"],
  ["ψυχῆς πείρατα ἰὼν οὐκ ἂν ἐξεύροιο, πᾶσαν ἐπιπορευόμενος ὁδόν",
   "You could travel every road and never find the limits of the soul.",
   "Heraclitus, DK B45"],
  ["ἄνδρα μοι ἔννεπε, Μοῦσα, πολύτροπον, ὃς μάλα πολλὰ πλάγχθη",
   "Sing to me, Muse, of the man of many turns, who wandered far and wide.",
   "Homer, Odyssey 1.1"],
  ["Οὖτις ἐμοί γ᾽ ὄνομα",
   "Nobody — that is my name.",
   "Odysseus to the Cyclops — Homer, Odyssey 9.366"],
  ["οὐ μὰν ἐκλελάθοντ᾽, ἀλλ᾽ οὐκ ἐδύναντ᾽ ἐπίκεσθαι",
   "Like the sweet apple high on the topmost bough: not forgotten — the "
   + "pickers could not reach it.",
   "Sappho, fr. 105a"],
  ["ἄνθρωπον ζητῶ",
   "I am looking for a human being.",
   "Diogenes, lantern in hand (Diogenes Laertius 6.41)"],
];

function NotFoundPage() {
  const [el, en, src] = useMemo(
    () => NOT_FOUND_QUOTES[Math.floor(Math.random() * NOT_FOUND_QUOTES.length)],
    []);
  return (
    <div className="centered">
      <div className="card" style={{ maxWidth: "28rem" }}>
        <h2 style={{ marginTop: 0, fontSize: "2rem" }}>404</h2>
        <p className="mut">
          There's nothing at this address — the link may be stale or
          mistyped.
        </p>
        <blockquote className="mut"
          style={{ margin: "1rem 0 .8rem", fontStyle: "italic" }}>
          <span lang="grc">{el}</span><br />
          <span className="sub">{en}<br />— {src}</span>
        </blockquote>
        <NavLink to="/">Back to Today</NavLink>
      </div>
    </div>
  );
}

// One lockout shell for every frozen-account state: title, paragraphs,
// the sign-out that stays open server-side on purpose. Every API call 403s in these states, so
// the app can't render anything else.
function LockoutPage({ title, paras, action }: {
  title: string; paras: string[];
  // the one way out that isn't signing out — a lockout the person can
  // escape from inside offers it here, beside Sign out
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div className="centered">
      <div className="card" style={{ maxWidth: "28rem" }}>
        <h2 style={{ marginTop: 0 }}>{title}</h2>
        {paras.map((p, i) => <p key={i} className="mut">{p}</p>)}
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
          {action && <button onClick={action.onClick}>{action.label}</button>}
          <button onClick={() =>
            api.logout().finally(() => { window.location.href = "/login"; })}>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}

function UnconfirmedPage() {
  const toast = (detail: string) =>
    window.dispatchEvent(new CustomEvent("oiko-toast", { detail }));
  return (
    <LockoutPage title="Confirm your email to keep this account"
      paras={[
        "The email address on this account was never confirmed, so the "
        + "account is frozen and will be deleted when the grace period "
        + "ends — with everything in it.",
        "We emailed a fresh confirmation link to the address you sign in "
        + "with. Open it and press “Reopen this account” on the page it "
        + "takes you to — confirming the address alone does not bring the "
        + "account back. No email? Check spam, or resend it.",
      ]}
      action={{ label: "Resend link", onClick: () => api.verifyResend()
        .then((r) => toast(r.verified
          ? "This account is already reopened — reload."
          : "Confirmation email sent."))
        .catch((e) => toast(`Resend failed: ${errText(e)}`)) }} />
  );
}

function PendingDeletePage() {
  return (
    <LockoutPage title="This account is scheduled for deletion"
      paras={[
        "The grace window is still open, so nothing has been erased yet "
        + "and everything is recoverable — but the account is frozen "
        + "until someone cancels the deletion.",
        "Contact support to cancel it, and mention the email address you "
        + "sign in with. When the window lapses the data is purged for "
        + "good.",
      ]} />
  );
}

function SuspendedPage() {
  return (
    <LockoutPage title="Your account has been suspended"
      paras={[
        "Sign-in works but the account is on hold, usually while "
        + "something is sorted out with the operator. Your data is "
        + "intact — nothing has been deleted.",
        "If you think this is a mistake, contact support and mention "
        + "the email address you sign in with.",
      ]} />
  );
}

// A landing page after a successful account deletion follows below (the
// server wiped the tenant and cleared the session cookie before the SPA
// navigates there).
/** /app/invite reached with a live session: the link is for someone
 *  else — offer to sign out and take it fresh. Without a token it is just
 *  a stray URL → home. */
function InviteWhileSignedIn({ email }: { email: string }) {
  const token = new URLSearchParams(window.location.search).get("token");
  if (!token) return <Navigate to="/" replace />;
  return (
    <div className="centered"><div className="card"
         style={{ maxWidth: "26rem" }}>
      <h1 style={{ marginTop: 0 }}>Household invite</h1>
      <p className="mut">This browser is signed in as <b>{email}</b>. The
        invite is for a new member, so accepting it starts a fresh sign-in
        — you'll be signed out here first.</p>
      <button className="pri" onClick={async () => {
        try { await api.logout(); } catch { /* cookie may already be gone */ }
        window.location.href = `/app/invite?token=${encodeURIComponent(token)}`;
      }}>Sign out and accept the invite</button>
      <p className="mut" style={{ marginTop: ".8rem" }}>
        <Link to="/">Not now — back to the app</Link></p>
    </div></div>
  );
}

function AccountDeletedPage() {
  return (
    <div className="centered">
      <div className="card" style={{ maxWidth: "28rem" }}>
        {/* the one line, nothing else: no paragraphs, no
            "back to sign-in" — there is no account to sign in to */}
        <h2 style={{ margin: 0 }}>Your account has been deleted</h2>
      </div>
    </div>
  );
}

function DemoResetCountdown() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const left = Math.floor((3_600_000 - (now % 3_600_000)) / 1000);
  const mm = Math.floor(left / 60), ss = left % 60;
  return (
    <div className="note topnote" style={{ color: "var(--amber, #e6b159)" }}>
      <span style={{ flex: 1 }}>
        Demo instance — synthetic data. Everything resets on the hour.</span>
      <b style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
        resets in {mm}:{String(ss).padStart(2, "0")}</b>
    </div>
  );
}

// reboot choreography for a signed-in tab. Countdown until the
// broadcast deadline, then a full-screen "restarting" overlay that polls
// /readyz and reloads the app the moment the server answers again — the
// user always sees SOMETHING, and gets back in without lifting a finger.
function RebootCountdown({ deadline, message }: {
  deadline: string; message: string;
}) {
  const [now, setNow] = useState(Date.now());
  const [serverBack, setServerBack] = useState(false);
  const left = Math.max(0, Math.round((new Date(deadline).getTime() - now) / 1000));
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (left > 0 || serverBack) return;
    // restart window: give the box a moment to actually go down, then
    // poll until it answers and reload into the fresh session state
    let stop = false;
    const started = Date.now();
    const poll = () => {
      if (stop) return;
      fetch("/readyz", { cache: "no-store" })
        .then((r) => {
          // only trust an OK that arrives after the box had time to drop
          if (r.ok && Date.now() - started > 20_000) {
            setServerBack(true);
            setTimeout(() => window.location.reload(), 800);
          } else setTimeout(poll, 4000);
        })
        .catch(() => setTimeout(poll, 4000));
    };
    setTimeout(poll, 8000);
    return () => { stop = true; };
  }, [left === 0, serverBack]);
  if (left > 0)
    return (
      <div className="note bad topnote">
        <span style={{ flex: 1 }}>⚠ {message}</span>
        <b style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
          restarting in {left}s</b>
      </div>
    );
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 9999,
                  background: "var(--bg)", display: "flex",
                  alignItems: "center", justifyContent: "center",
                  flexDirection: "column", gap: "1rem", textAlign: "center",
                  padding: "2rem" }}>
      <svg width="64" height="64" viewBox="0 0 100 100"
           style={{ animation: "spin 2.2s linear infinite" }}>
        <path d="M39.7 78.2 A30 30 0 1 1 60.3 78.2" fill="none"
              stroke="var(--blue)" strokeWidth="11" strokeLinecap="round" />
      </svg>
      <h2 style={{ margin: 0 }}>
        {serverBack ? "Back — reloading…" : "Restarting the server"}</h2>
      <p className="mut" style={{ maxWidth: "24rem" }}>
        Routine maintenance, about a minute. This page reconnects by
        itself — no need to refresh.</p>
    </div>
  );
}
