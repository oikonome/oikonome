// The React-free half of useRefetchWhenShown (focus.ts), kept apart so
// the logic tests can drive it against a real QueryClient.
//
// A screen that is not on screen is frozen, so the writes that touch a
// heavy key invalidate it with refetchType "none" and the screen that
// shows the key catches up by itself. TanStack's own cache events cannot
// tell it when: a query already marked invalid raises no second
// "invalidate" event, and a refetch that was in flight when the write
// landed clears the mark as it settles, with an answer the server read
// BEFORE the write. The screen would show the old list as fresh and
// nothing would fetch again. So the app's client announces every
// deferred invalidation itself, whatever state the query is in.
import { partialMatchKey, QueryClient, type InvalidateOptions,
         type InvalidateQueryFilters, type Query,
         type QueryKey } from "@tanstack/react-query";

type DeferredListener = (query: Query) => void;

export class AppQueryClient extends QueryClient {
  #deferred = new Set<DeferredListener>();

  /** Called with every cached query a refetchType "none" invalidation
   *  just covered. Returns the unsubscribe. */
  onDeferredInvalidate(fn: DeferredListener): () => void {
    this.#deferred.add(fn);
    return () => { this.#deferred.delete(fn); };
  }

  override invalidateQueries<T extends QueryKey = QueryKey>(
      filters?: InvalidateQueryFilters<T>,
      options?: InvalidateOptions): Promise<void> {
    const done = super.invalidateQueries(filters, options);
    if (filters?.refetchType === "none" && this.#deferred.size) {
      const hit = this.getQueryCache().findAll(
        filters as InvalidateQueryFilters);
      for (const q of hit) for (const fn of [...this.#deferred]) fn(q);
    }
    return done;
  }
}

export type Live = { isStale: boolean; isFetching: boolean;
                     refetch: () => unknown };

/** What useRefetchWhenShown decides, as plain calls: `attach` on mount
 *  (returns the detach), `focus` when the screen is shown, `blur` when it
 *  is left.
 *
 *  - Shown: refetch if the data is stale (a failed answer counts: coming
 *    back to a tab that errored is the retry — once per showing, at the
 *    pace a person switches tabs, so it cannot hammer a server that is
 *    down).
 *  - A write while shown: refetch now, cancelling a fetch in flight,
 *    whose answer predates the write.
 *  - A write while hidden: remembered, so the next showing refetches even
 *    if a fetch in flight since has landed and made the data look fresh.
 *
 *  Nothing else fetches: not the stale timer (a screen left open must not
 *  poll its heaviest endpoint every minute). */
export function shownRefetcher(qc: QueryClient, queryKey: QueryKey,
                               live: () => Live) {
  let focused = false;
  let missed = false;
  let queued = false;
  // one write usually invalidates the key several times over (the list,
  // then a prefix that covers it); one refetch after the handler is done
  const refetchSoon = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      void live().refetch();
    });
  };
  return {
    attach(): () => void {
      if (!(qc instanceof AppQueryClient)) return () => {};
      return qc.onDeferredInvalidate((q) => {
        if (!partialMatchKey(q.queryKey, queryKey)) return;
        if (focused) refetchSoon();
        else missed = true;
      });
    },
    focus() {
      focused = true;
      const c = live();
      if (missed) {
        missed = false;
        refetchSoon();
      } else if (c.isStale && !c.isFetching) {
        refetchSoon();
      }
    },
    blur() { focused = false; },
  };
}
