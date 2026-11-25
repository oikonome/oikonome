// Which help guide the header ? opens per route (longest prefix wins).
// Routes without a guide land on the plain catalog. The home page is one
// route with three lenses: Today opens the Today guide, Month and Year
// the guide that covers them.
const HELP_TOPIC_BY_ROUTE: [string, string][] = [
  ["/transactions", "transactions"], ["/bills", "bills"],
  ["/budget", "budget"], ["/rules", "rules"], ["/merchants", "merchants"],
  ["/assistant", "assistant"], ["/cashflow", "cash-flow"],
  ["/retirement", "retirement"], ["/debt", "debt"],
  ["/networth", "net-worth"], ["/alerts", "alerts"],
  ["/accounts", "accounts"], ["/items", "receipts"],
  ["/reimburse", "reimbursements"], ["/import", "import-history"],
  ["/business", "business"],
  ["/settings", "security"], ["/doctor", "troubleshooting"],
  ["/", "today"],   // last: matches everything (the home lenses)
];

export function helpHref(pathname: string, search = ""): string {
  if (pathname === "/") {
    const lens = new URLSearchParams(search).get("lens");
    if (lens === "month" || lens === "year") return "/help?topic=lenses";
  }
  const hit = HELP_TOPIC_BY_ROUTE.find(([p]) =>
    p === "/" ? pathname === "/" : pathname.startsWith(p));
  return hit ? `/help?topic=${hit[1]}` : "/help";
}
