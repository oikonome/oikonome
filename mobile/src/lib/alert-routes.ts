// Where each alert kind gets fixed — the web maps alert.link to a page;
// mobile routes by KIND to the native screen that owns the fix. Shared by
// the Today strip and the Alerts page so the two never disagree.
import { Alert } from "./api";

export const KIND_ROUTE: Record<string, string> = {
  "stale-pull": "/accounts", "connection-removed": "/accounts",
  setup: "/accounts", proposals: "/bills", drift: "/bills", merges: "/merchants",
  reimb: "/reimburse", funding: "/networth", transfer: "/transactions",
  anomaly: "/transactions", bonus: "/transactions",
};

/** The native screen that fixes this alert, or null when only the web
 *  has one. */
export function alertRoute(a: Pick<Alert, "kind">): string | null {
  return KIND_ROUTE[a.kind] ?? null;
}
