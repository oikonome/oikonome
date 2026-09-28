// Every page except Home is a chunk of its own, fetched the first time its
// route is opened (App.tsx wraps each in React.lazy). Keyed by route so
// warmRoute() can start the right download from the URL alone — and so
// main.tsx can warm the household's chosen home before anything renders.
export const PAGE = {
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
export function warmRoute(pathname: string) {
  const hit = (Object.keys(PAGE) as (keyof typeof PAGE)[])
    .filter((p) => pathname === p || pathname.startsWith(p + "/"))
    .sort((a, b) => b.length - a.length)[0];
  if (hit) PAGE[hit]().catch(() => {});
}
warmRoute(window.location.pathname.replace(/^\/app(?=\/|$)/, "") || "/");

/** Every page chunk, fetched while the browser is idle after the first
 *  paint: together they are a fraction of the first paint's bytes, and a
 *  click on any tab then never waits on the network for code. */
export function warmEveryRoute(): () => void {
  // one at a time, and not before the page's own requests are through:
  // thirty-odd chunks fired together queued the home page's JSON behind
  // them on a cold bundle (HTTP/1.1's six connections; a phone's link
  // on hosted). Sequential, they are all in within a few seconds and
  // never ahead of a fetch the person is waiting on.
  let stopped = false;
  const run = async () => {
    for (const f of Object.values(PAGE)) {
      if (stopped) return;
      try { await f(); } catch { /* a chunk that fails now loads on the click */ }
    }
  };
  const id = window.setTimeout(() => { void run(); }, 1500);
  return () => { stopped = true; window.clearTimeout(id); };
}

