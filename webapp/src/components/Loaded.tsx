// What a section shows for each state of the read it renders from
// (specs/screen-states.md): loading, failed, stale, empty, ready. The phone
// keeps the same pieces with the same words in
// mobile/src/components/loaded.tsx; the words come from the screen-states
// twin block in moneymath.ts.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "../api/client";
import { EMPTY_LINES, failLine, inputPhase, noAccountsYet, staleLine }
  from "../moneymath";
import EmptyState from "./EmptyState";
import PageSkeleton from "./PageSkeleton";
import Note from "./Note";

type Read = Pick<UseQueryResult<unknown>,
  "data" | "error" | "isError" | "dataUpdatedAt" | "refetch">;

/** The one line in place of a section whose read failed, with "Try again"
 *  when asking again can help. */
export function FailedLine({ q, what }: { q: Read; what: string }) {
  const { text, retry } = failLine(q.error, what, "web");
  return (
    <Note as="p" tone="bad">
      <span style={{ flex: 1 }}>{text}</span>
      {retry && (
        <button type="button" onClick={() => { void q.refetch(); }}>
          Try again</button>
      )}
    </Note>
  );
}

/** The one line above data kept from before a refetch that failed; nothing
 *  while the read is current. */
export function StaleLine({ query }: { query: Read }) {
  if (!query.isError || query.data === undefined) return null;
  return (
    <Note as="p">
      {staleLine(query.error, query.dataUpdatedAt, "web", Date.now())}
    </Note>
  );
}

/** A section rendered from one read: loading → the page skeleton (or
 *  "Loading…"), failed with nothing kept → the failed line in its place,
 *  ready → its children, under the stale line when the latest refetch
 *  failed. `empty` + `emptyTitle` cover a read that answered with nothing. */
export function Loaded<D>({ q, what, skeleton, empty, emptyTitle, emptyText,
                            emptyTo, emptyAction, children }: {
  q: UseQueryResult<D>; what: string; skeleton?: boolean;
  empty?: (d: D) => boolean; emptyTitle?: string; emptyText?: string;
  emptyTo?: string; emptyAction?: string;
  children: (d: D) => ReactNode;
}) {
  const phase = inputPhase(q);
  if (phase === "loading")
    return skeleton ? <PageSkeleton /> : <p className="mut">Loading…</p>;
  if (phase === "failed") return <FailedLine q={q} what={what} />;
  const d = q.data as D;
  return (
    <>
      <StaleLine query={q} />
      {empty && emptyTitle && empty(d)
        ? <EmptyState title={emptyTitle} text={emptyText} to={emptyTo}
                      action={emptyAction} />
        : children(d)}
    </>
  );
}

/** Whether the household has no accounts at all, from the accounts read
 *  (false while it has not answered, or failed): the one case an empty
 *  screen offers "Connect or import". */
export function useNoAccounts(): boolean {
  const q = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  return noAccountsYet(q);
}

/** The empty line of a screen with nothing to show because nothing is
 *  linked yet, with its one action. */
export function NoAccountsLine() {
  return <EmptyState title={EMPTY_LINES.noAccounts} to="/accounts"
                     action={EMPTY_LINES.connect} />;
}
