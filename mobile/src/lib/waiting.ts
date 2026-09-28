// The waiting-receipt list and the writes that move a receipt between it
// and the ledger — shared by the Receipts screen, the Transactions tab and
// the transaction screen (webapp/src/api/waiting.ts, the same three
// hooks). Patch-then-invalidate: each write seeds the list from the
// server's answer and marks only the ledger keys it moved.
import { useMutation, useQuery, useQueryClient, type QueryClient }
  from "@tanstack/react-query";

import type { TodayFull, Txn, TxnPage, WaitingReceipts } from "./api";
import { patchQueries } from "./cache";
import { useSession } from "./session";

export const WAITING_KEY = ["receipts-waiting"] as const;

/** The waiting list, polled while a snapped receipt is still being read —
 *  its parse finishing is also the moment the server tries to match it. */
export function useWaitingReceipts() {
  const { client } = useSession();
  const qc = useQueryClient();
  return useQuery({
    queryKey: WAITING_KEY,
    queryFn: () => loadWaiting((o) => client!.receiptsWaiting(o),
      qc.getQueryData<WaitingReceipts>(WAITING_KEY)?.waiting.length ?? 0),
    enabled: !!client,
    refetchInterval: (query) =>
      query.state.data?.waiting.some((r) => r.status === "parsing")
        ? 3000 : false,
  });
}

/** One more page of the waiting list after the rows already shown: a row
 *  already there is kept once (the list may have shifted between reads),
 *  and `count` and the rest are the newer answer's. */
export function mergeWaitingPage(prev: WaitingReceipts | undefined,
                                 page: WaitingReceipts): WaitingReceipts {
  if (!prev) return page;
  const seen = new Set(prev.waiting.map((w) => w.id));
  return { ...page, offset: 0,
           waiting: [...prev.waiting,
                     ...page.waiting.filter((w) => !seen.has(w.id))] };
}

/** Are there waiting receipts past the ones shown? `count` is the true
 *  total; the list is paged. */
export const moreWaiting = (d?: { count: number; waiting: unknown[] }) =>
  !!d && d.waiting.length < d.count;

/** Seed the cache with a write's answer — the first page of the waiting
 *  list — without dropping the rows "show more" loaded past it: those are
 *  kept after the fresh page (less `drop`, the receipt the write took off
 *  the list), then re-read, since only the first page is current. */
export function seedWaiting(qc: QueryClient, page: WaitingReceipts,
                            drop?: string | null) {
  const prev = qc.getQueryData<WaitingReceipts>(WAITING_KEY);
  if (!prev || prev.waiting.length <= page.waiting.length) {
    qc.setQueryData(WAITING_KEY, page);
    return;
  }
  const seen = new Set(page.waiting.map((w) => w.id));
  qc.setQueryData<WaitingReceipts>(WAITING_KEY, { ...page, offset: 0,
    waiting: [...page.waiting,
              ...prev.waiting.filter((w) => !seen.has(w.id) && w.id !== drop)] });
  qc.invalidateQueries({ queryKey: WAITING_KEY });
}

// Re-read as many rows as are shown, so a poll or a refetch does not fold
// a list the person paged through back to its first page. Stops at a page
// that brings nothing new.
async function loadWaiting(page: (offset: number) => Promise<WaitingReceipts>,
                           shown: number): Promise<WaitingReceipts> {
  let d = await page(0);
  while (d.waiting.length < Math.min(shown, d.count)) {
    const next = mergeWaitingPage(d, await page(d.waiting.length));
    if (next.waiting.length === d.waiting.length) break;
    d = next;
  }
  return d;
}

// The next page after the rows shown, read at the shown length. The offset
// only lines up while the list holds the rows it held when they were read,
// and the page's `count` against the cached one says whether it still does:
// - same count: append; the de-duplication keeps a row that moved within the
//   list once.
// - lower count: rows left the list, every later row moved UP, and the page
//   at the shown length starts past a row nobody fetched.
// - higher count: new snaps arrived at the top, above anything an append can
//   reach, and pushed the shown rows down.
// The one rule right in both moved cases, and when rows left and arrived at
// once, is to drop the page and re-read from offset 0 up to the shown length
// plus one page (loadWaiting), so the shown rows are current and the next
// page follows on from them. A removal and a snap that cancel out keep the
// count: rows past the shown ones sit where they were, so the append stays
// right, and the new top row arrives with the next refetch.
export async function moreWaitingPage(
    page: (offset: number) => Promise<WaitingReceipts>,
    prev: WaitingReceipts | undefined): Promise<WaitingReceipts> {
  const shown = prev?.waiting.length ?? 0;
  const next = await page(shown);
  if (!prev || next.count === prev.count) return mergeWaitingPage(prev, next);
  return loadWaiting(page, shown + (next.limit ?? next.waiting.length));
}

// A match moves one ledger row's 📎: set it on the rows the lists hold, and
// let the ledgers catch up when they are next shown (the tabs are frozen
// while hidden, so they are marked, not refetched).
function markRow(qc: QueryClient, txnId: string, has: boolean) {
  const fn = (rows: Txn[]) => rows.map(
    (t) => t.id === txnId ? { ...t, has_receipt: has } : t);
  patchQueries<TxnPage>(qc, ["transactions"],
    (o) => o.rows ? { ...o, rows: fn(o.rows) } : o);
  patchQueries<TodayFull>(qc, ["today"],
    (o) => o.recent ? { ...o, recent: fn(o.recent) } : o);
  qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
  qc.invalidateQueries({ queryKey: ["receipts", txnId] });
}

/** Snap a receipt with no transaction. */
export function useSnapReceipt() {
  const { client } = useSession();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ uri, name, type }: { uri: string; name?: string;
                                         type?: string }) =>
      client!.receiptSnap(uri, name, type),
    // the door answers with the whole waiting list — seed it, so the new
    // receipt (and its "reading…" state) is on screen now
    onSuccess: (r) => {
      const { id: _id, ...list } = r;
      seedWaiting(qc, list);
    },
  });
}

/** "Show more": the next page of the waiting list, in the cache. Nothing
 *  else moved, so nothing is invalidated. */
export function useMoreWaiting() {
  const { client } = useSession();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => moreWaitingPage((o) => client!.receiptsWaiting(o),
      qc.getQueryData<WaitingReceipts>(WAITING_KEY)),
    onSuccess: (d) => qc.setQueryData(WAITING_KEY, d),
  });
}

/** Pair a waiting receipt with a transaction a person chose. */
export function useMatchReceipt() {
  const { client } = useSession();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, txnId }: { id: string; txnId: string }) =>
      client!.receiptMatch(id, txnId),
    onSuccess: (list, { id, txnId }) => {
      seedWaiting(qc, list, id);
      markRow(qc, txnId, true);
    },
  });
}

/** Put a matched receipt back to waiting. */
export function useUnmatchReceipt() {
  const { client } = useSession();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => client!.receiptUnmatch(id),
    onSuccess: (r) => {
      const { txn_id, ...list } = r;
      seedWaiting(qc, list);
      // the row may still carry another receipt — the refetch decides
      qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
      qc.invalidateQueries({ queryKey: ["receipts", txn_id] });
    },
  });
}


/** Type in a receipt's total, date and store — for an instance with no AI
 *  to read it, or a read that failed or got the total wrong. The answer is
 *  the waiting list after the server tried to match it. */
export function useReceiptDetails() {
  const { client } = useSession();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string;
                                 body: { total: number; date: string;
                                         merchant?: string } }) =>
      client!.receiptDetails(id, body),
    onSuccess: (r, { id }) => {
      const { id: _id, txn_id, ...list } = r;
      seedWaiting(qc, list, txn_id ? id : null);
      if (txn_id) markRow(qc, txn_id, true);
    },
  });
}

// ---- choosing a waiting receipt for one transaction ------------------------
// (webapp/src/api/waiting.ts, the same three helpers)

const dayNo = (ymd: string | null | undefined): number | null => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ymd ?? "");
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86_400_000 : null;
};

/** The waiting receipts in the order they could be THIS transaction: one the
 *  matcher already offers for it first, then by how close the receipt's
 *  total is to the charge, then by the days between them. A receipt with no
 *  total or date yet sorts after those that have one; ties keep the list's
 *  own order (newest snap first). The whole list comes back — the panel
 *  shows the head and keeps the rest one tap away, so any waiting receipt
 *  can be paired from any transaction. */
export function rankWaiting<W extends { total: number | null;
                                         date: string | null;
                                         candidates: { txn_id: string }[] }>(
    list: W[], txn: { id: string; amount: number; date: string } | null)
    : W[] {
  if (!txn) return list;
  const amt = Math.abs(txn.amount);
  const day = dayNo(txn.date);
  const key = (w: W): [number, number, number] => {
    const d = dayNo(w.date);
    return [w.candidates.some((c) => c.txn_id === txn.id) ? 0 : 1,
            w.total != null ? Math.abs(w.total - amt) : Infinity,
            d != null && day != null ? Math.abs(d - day) : Infinity];
  };
  return list.map((w, i) => ({ w, i, k: key(w) }))
    .sort((a, b) => a.k[0] - b.k[0] || a.k[1] - b.k[1] || a.k[2] - b.k[2]
                    || a.i - b.i)
    .map((x) => x.w);
}

/** Does a waiting receipt answer a typed search — its store, its total
 *  ("27.35", "$27"), or its date as shown (MM/DD/YY) or stored? */
export function waitingMatches(w: { merchant: string | null;
                                    total: number | null;
                                    date: string | null },
                               q: string): boolean {
  const t = q.trim().toLowerCase().replace(/^\$/, "");
  if (!t) return true;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(w.date ?? "");
  const hay = [w.merchant ?? "receipt",
               w.total != null ? w.total.toFixed(2) : "",
               w.date ?? "",
               m ? `${m[2]}/${m[3]}/${m[1].slice(2)}` : ""]
    .join(" ").toLowerCase();
  return hay.includes(t);
}

/** The dates the server takes for a receipt snapped at `createdAt`: from a
 *  year before the snap up to the day after it — the window the matcher
 *  trusts a printed date in. Read off the snap's own calendar day as the
 *  server stored it, so the picker and the server agree on every edge. */
export function receiptDateBounds(createdAt: string): { min: string;
                                                        max: string } | null {
  const snapped = dayNo(createdAt);
  if (snapped == null) return null;
  const iso = (n: number) => new Date(n * 86_400_000).toISOString().slice(0, 10);
  return { min: iso(snapped - 366), max: iso(snapped + 1) };
}
