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

/** A write that moves a transaction's category (or whether it counts at
 *  all) changes every per-category rollup: the Spending and Cash Flow
 *  reports and both lenses. Each is keyed under its own prefix, so the
 *  ledger lists refetching is not enough to move their totals. */
export function invalidateRollups(qc: QueryClient): void {
  for (const k of ["report", "lens-month", "lens-year"])
    qc.invalidateQueries({ queryKey: [k] });
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
/** The one queue settings saves that write the whole stored view back go
 *  through, so their replies land in the order they were sent and the last
 *  one to land holds every change before it. */
export const SETTINGS_SAVES = { id: "settings-email" };

export function saveSettings(
  qc: QueryClient, body: Record<string, unknown>,
): Promise<Settings> {
  return api.settingsSave(body).then((v) => {
    qc.setQueryData<Settings>(["settings"], v);
    return v;
  });
}

/** A password, login-address or second-factor change pauses the
 *  household's text number until a fresh code sent to it is typed back, so
 *  a phone someone else verified stops getting the summaries. The reply
 *  says when that happened; the owner hears it in the same sentence as the
 *  rest of the change, and the notify card refetches so it shows the pause
 *  instead of a stale "verified". Empty when nothing was paused. */
export function smsPauseNote(
  qc: QueryClient, r: { sms_number_needs_verify?: boolean } | null | undefined,
): string {
  if (!r?.sms_number_needs_verify) return "";
  qc.invalidateQueries({ queryKey: ["notify"] });
  return " Texts to the household's number are paused until you confirm "
    + "it again (Settings → Daily email).";
}

/** A credential rotation (password, login address, a first second factor)
 *  also ends every support-access grant this person made and can pause
 *  outbound mail to the household's extra recipients — both shown on the
 *  Settings screens, so both are refetched with the lists it empties. */
export function invalidateAfterRotation(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ["support-access"] });
  qc.invalidateQueries({ queryKey: ["settings"] });
}
