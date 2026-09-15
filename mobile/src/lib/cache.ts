// Cache patch helpers for the moment right after a write succeeds.
//
// The server has already committed the change when onSuccess runs; what the
// user is waiting for is the SCREEN. Refetching to re-prove the write leaves
// a dismissed row sitting there — and every refetch on a phone is a radio
// round trip. So the rule is: patch the cached shape first, then invalidate
// only the keys the write could really have moved. These are the shared
// one-liners for that; mirrors webapp/src/api/cache.ts.
import type { QueryClient, QueryKey } from "@tanstack/react-query";
import type { Client, SettingsView } from "./api";

/** Shallow-merge `partial` into a cached object (no-op if nothing cached). */
export function patchQuery<T extends object>(
  qc: QueryClient, key: QueryKey, partial: { [K in keyof T]?: T[K] },
): void {
  // `{ [K in keyof T]?: T[K] }` rather than Partial<T>: when T is inferred
  // from a query's `data` TanStack wraps it in NoInfer, and Partial of that
  // does not spread back into T under TS 6.
  qc.setQueryData<T>(key, (old) => old && ({ ...old, ...partial } as T));
}

/** Same, applied to EVERY cached query under a prefix — for screens that
 *  don't know the exact params a list was fetched with (every cached
 *  ["transactions", params] page, ["business", year, limit], …). */
export function patchQueries<T extends object>(
  qc: QueryClient, prefix: QueryKey, fn: (old: T) => T,
): void {
  qc.setQueriesData<T>({ queryKey: prefix }, (old) => old && fn(old));
}

/** Patch the rows of a list field inside a cached object that satisfy
 *  `pred`. `{alerts: [...]}`, `{bills: [...]}`, `{rows: [...]}` — the field
 *  name is the argument, so one helper fits every list screen. */
export function patchList<T extends object, R>(
  qc: QueryClient, key: QueryKey, field: keyof T,
  pred: (row: R) => boolean, patch: Partial<R> | ((row: R) => R),
): void {
  qc.setQueryData<T>(key, (old) => {
    if (!old) return old;
    const rows = (old[field] as unknown as R[] | undefined);
    if (!Array.isArray(rows)) return old;
    return {
      ...old,
      [field]: rows.map((r) => !pred(r) ? r
        : typeof patch === "function" ? (patch as (row: R) => R)(r)
        : { ...r, ...patch }),
    };
  });
}

/** Drop the rows of a list field that satisfy `pred`. */
export function removeFromList<T extends object, R>(
  qc: QueryClient, key: QueryKey, field: keyof T, pred: (row: R) => boolean,
): void {
  qc.setQueryData<T>(key, (old) => {
    if (!old) return old;
    const rows = (old[field] as unknown as R[] | undefined);
    if (!Array.isArray(rows)) return old;
    return { ...old, [field]: rows.filter((r) => !pred(r)) };
  });
}

/** Save settings and seed the cache with the full view the server returns,
 *  so every card bound to ["settings"] flips at once — instead of a Switch
 *  springing back until a refetch lands. Also refreshes the two other keys
 *  that are really GET /api/settings under another name. The caller still
 *  decides which OTHER keys (today, calendar, …) the change can have moved. */
export function saveSettings(
  qc: QueryClient, client: Client, body: Record<string, unknown>,
): Promise<SettingsView> {
  return client.settingsSave(body).then((v) => {
    seedSettings(qc, v);
    return v;
  });
}

/** Put a freshly returned settings view into every cache key that reads
 *  GET /api/settings. */
export function seedSettings(qc: QueryClient, v: SettingsView): void {
  qc.setQueryData<SettingsView>(["settings"], v);
  qc.setQueryData(["schedule"], v.email_schedule ?? null);
  qc.setQueryData(["settings-feedback"], (old: unknown) => old === undefined ? old : v);
  // ["settings-accounts"] is the same GET /api/settings, projected to the
  // two fields accounts.tsx's editor needs (api.ts:1803) — without this
  // a save made elsewhere (e.g. the planner) never reaches that screen.
  // The two fields aren't modeled on SettingsView, so read them the same
  // way api.ts's settingsAccounts() does.
  const accts = v as unknown as
    { checking_account_id?: string | null; excluded_accounts?: string[] | null };
  qc.setQueryData(["settings-accounts"], (old: unknown) => old === undefined
    ? old
    : { checking_account_id: accts.checking_account_id ?? null,
        excluded_accounts: accts.excluded_accounts ?? [] });
}

/** Reflect a fit-hint dismiss/restore in the cached settings. The server
 *  stores the dismissal under settings.dismissed_hints (api.py's
 *  /bills/hint), and Budget's "N hints dismissed" tweak line reads its
 *  key count (budget.tsx) — so a dismiss/restore on Bills or Bill
 *  history must be visible there without a refetch. Health dismissals
 *  live server-side under a separate key the app never reads, so only
 *  the "fit" target touches settings. */
export function patchDismissedHint(
  qc: QueryClient, payee: string, dismissed: boolean,
): void {
  qc.setQueryData<SettingsView>(["settings"], (old) => {
    if (!old) return old;
    const hints = { ...(old.dismissed_hints ?? {}) };
    if (dismissed) hints[payee] = true;
    else delete hints[payee];
    return { ...old, dismissed_hints: hints };
  });
}

/** Every entity-scoped query a business write can move. Named, so a write
 *  refreshes this list instead of the whole cache: a bare invalidateQueries()
 *  also refetched Today, every transactions page and the accounts tab
 *  behind a single tap. */
export const bizKeys = (id: string): QueryKey[] => [
  ["biztxns", id], ["pnl", id], ["balancesheet", id], ["esttax", id],
  ["biz-suggestions", id], ["vendors", id]];

/** The personal side of the same move: business money leaving (or
 *  rejoining) the personal budget changes the verdict, the ledger, the
 *  reports and the month/year lenses. */
export const personalKeys: QueryKey[] =
  [["today"], ["transactions"], ["report"], ["lens"]];

// resolves when every key's active queries have refetched — PullRefresh
// keeps its spinner up on exactly that promise
export const invalidateAll = (qc: QueryClient, keys: QueryKey[]): Promise<void> =>
  Promise.all(keys.map((k) => qc.invalidateQueries({ queryKey: k })))
    .then(() => undefined);
