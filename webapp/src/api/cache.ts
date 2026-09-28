// Cache patch helpers for the moment right after a write succeeds.
//
// The server has already committed the change when onSuccess runs; what the
// user is waiting for is the SCREEN. Refetching to re-prove the write leaves
// a dismissed row sitting there — and on pages that mount one query per row,
// or invalidate a wide prefix, it sits there behind a dozen requests. So the
// rule is: patch the cached shape first, then invalidate only the keys the
// write could really have moved. These are the shared one-liners for that.
import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { api, type Settings } from "./client";

/** Shallow-merge `partial` into a cached object (no-op if nothing cached). */
export function patchQuery<T extends object>(
  qc: QueryClient, key: QueryKey, partial: { [K in keyof T]?: T[K] },
): void {
  // `{ [K in keyof T]?: T[K] }` rather than Partial<T>: when T is inferred
  // from a query's `data` TanStack wraps it in NoInfer, and Partial of that
  // does not spread back into T under TS 6.
  qc.setQueryData<T>(key, (old) => old && ({ ...old, ...partial } as T));
}

/** Same, applied to EVERY cached query under a prefix — for components
 *  that don't know the exact params their list was fetched with
 *  (TxnTable inside Transactions, BillsHistory, …). */
export function patchQueries<T extends object>(
  qc: QueryClient, prefix: QueryKey, fn: (old: T) => T,
): void {
  qc.setQueriesData<T>({ queryKey: prefix }, (old) => old && fn(old));
}

/** Patch the rows of a list field inside a cached object that satisfy
 *  `pred`. `{alerts: [...]}`, `{bills: [...]}`, `{rows: [...]}` — the field
 *  name is the argument, so one helper fits every list page. */
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
 *  so every card bound to ["settings"] flips at once — instead of the
 *  toggle snapping back until a refetch lands. The caller still decides
 *  which OTHER keys (today, calendar, …) the change can have moved. */
export function saveSettings(
  qc: QueryClient, body: Record<string, unknown>,
): Promise<Settings> {
  return api.settingsSave(body).then((v) => {
    qc.setQueryData<Settings>(["settings"], v);
    return v;
  });
}
