// What a section shows for each state of the read it renders from
// (specs/screen-states.md): loading, failed, stale, empty, ready. The web
// keeps the same pieces with the same words in
// webapp/src/components/Loaded.tsx; the words come from the screen-states
// twin block in lib/pure.ts.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import type { ReactNode } from "react";
import { StyleSheet, Text, View } from "react-native";

import { EMPTY_LINES, failLine, inputPhase, noAccountsYet, staleLine }
  from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { Note, Skeleton } from "./ui";

type Read = Pick<UseQueryResult<unknown>,
  "data" | "error" | "isError" | "dataUpdatedAt" | "refetch">;

/** The one line in place of a section whose read failed, with "Try again"
 *  when asking again can help. */
export function FailedLine({ q, what }: { q: Read; what: string }) {
  const { text, retry } = failLine(q.error, what, "phone");
  return (
    <Note bad style={s.failed}>
      {text}
      {retry && <Text>{" "}</Text>}
      {retry && (
        <Text style={s.action} accessibilityRole="button"
              onPress={() => { void q.refetch(); }}>Try again</Text>
      )}
    </Note>
  );
}

/** The one line above data kept from before a refetch that failed; nothing
 *  while the read is current. */
export function StaleLine({ query }: { query: Read }) {
  if (!query.isError || query.data === undefined) return null;
  return (
    <Note style={s.stale}>
      {staleLine(query.error, query.dataUpdatedAt, "phone", Date.now())}
    </Note>
  );
}

/** An empty list or section: one line, and at most one action. `to` is
 *  the screen the action opens (the web's EmptyState takes the same). */
export function EmptyState({ title, text, to, action, onAction }: {
  title: string; text?: string; to?: string; action?: string;
  /** the action as a button that does the step here, instead of `to` */
  onAction?: () => void;
}) {
  const router = useRouter();
  return (
    <View style={s.empty}>
      <Text style={s.emptyTitle}>{title}</Text>
      {!!text && <Text style={s.emptyText}>{text}</Text>}
      {!!to && !!action && (
        <Text style={s.action} accessibilityRole="link"
              onPress={() => router.push(to as never)}>{action}</Text>
      )}
      {!to && !!action && !!onAction && (
        <Text style={s.action} accessibilityRole="button"
              onPress={onAction}>{action}</Text>
      )}
    </View>
  );
}

/** Whether the household has no accounts at all, from the accounts read
 *  (false while it has not answered, or failed): the one case an empty
 *  screen offers "Connect or import". The web's useNoAccounts is the
 *  same. */
export function useNoAccounts(): boolean {
  const { client } = useSession();
  const q = useQuery({ queryKey: ["accounts"],
                       queryFn: () => client!.accounts(),
                       enabled: !!client });
  return noAccountsYet(q);
}

/** The empty line of a screen with nothing to show because nothing is
 *  linked yet, with its one action. */
export function NoAccountsLine() {
  return <EmptyState title={EMPTY_LINES.noAccounts} to="/connect-hub"
                     action={EMPTY_LINES.connect} />;
}

/** A section rendered from one read: loading → the skeleton (or
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
    return skeleton ? <Skeleton /> : <Text style={s.loading}>Loading…</Text>;
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

const s = StyleSheet.create({
  failed: { color: C.bad, textAlign: "center", padding: 20 },
  action: { color: C.accent, fontWeight: "600", textAlign: "center",
            marginTop: 6 },
  stale: { backgroundColor: "#3a2d12", borderRadius: 8, color: C.warn,
           fontSize: 13, overflow: "hidden", paddingHorizontal: 12,
           paddingVertical: 8, textAlign: "center" },
  loading: { color: C.mut, textAlign: "center", padding: 20 },
  empty: { alignItems: "center", padding: 20 },
  emptyTitle: { color: C.text, fontWeight: "600", textAlign: "center" },
  emptyText: { color: C.mut, marginTop: 4, textAlign: "center" },
});
