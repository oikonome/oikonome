// Where each alert gets fixed. A live alert carries the server's own `link`
// (a web path), and that wins, exactly as on the web; history rows store
// only kind + message, so they fall back to the kind map, the same map the
// web's Alerts page uses. Shared by the Today strip and the Alerts page so
// the two never disagree.
import type { Alert } from "./api";

export const KIND_ROUTE: Record<string, string> = {
  "stale-pull": "/accounts", "connection-removed": "/accounts",
  setup: "/bills", proposals: "/bills", drift: "/bills", merges: "/merchants",
  reimb: "/reimburse", funding: "/networth", transfer: "/transactions",
  anomaly: "/transactions", bonus: "/transactions",
  "llm-backend": "/doctor", "collector-token-revoked": "/settings",
};

// "fix this" is for something broken; a queue waiting on a yes/no is
// reviewed, not fixed — the web's wording, kind for kind.
export const KIND_CTA: Record<string, string> = {
  proposals: "review", drift: "review", merges: "review", anomaly: "review",
  bonus: "review", reimb: "review", transfer: "review",
};

// web path → native screen. Only paths with a native twin are listed, so a
// link to a web-only page falls back to the kind map instead of pushing a
// route that does not exist.
const WEB_TO_NATIVE: Record<string, string> = {
  "/accounts": "/accounts", "/bills": "/bills", "/merchants": "/merchants",
  "/reimburse": "/reimburse", "/networth": "/networth",
  "/transactions": "/transactions", "/doctor": "/doctor",
  "/settings": "/settings",
};

function nativeFor(link: string): string | null {
  const [path, query = ""] = link.split("?", 2);
  // one payee's bill history is its own native screen
  if (path === "/bills/history") {
    const payee = new URLSearchParams(query).get("payee");
    return payee ? `/bill-history?payee=${encodeURIComponent(payee)}` : "/bills";
  }
  // a Settings section opens Settings; the section is a web anchor
  const base = path.startsWith("/settings/") ? "/settings" : path;
  return WEB_TO_NATIVE[base] ?? null;
}

/** The native screen that fixes this alert, or null when only the web
 *  has one. */
export function alertRoute(a: Pick<Alert, "kind"> & { link?: string | null }):
    string | null {
  return (a.link ? nativeFor(a.link) : null) ?? KIND_ROUTE[a.kind] ?? null;
}

export const alertCta = (kind: string) => KIND_CTA[kind] || "fix this";
