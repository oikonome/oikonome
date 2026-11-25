// Reimburse — the pending list, and PAIRING: pick the deposit(s) that
// paid a flagged expense back. Same explicit-pairing doctrine as the
// web: never a pattern rule, always a chosen match.
import { keepPreviousData, useInfiniteQuery, useMutation, useQuery,
         useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Alert, KeyboardAvoidingView, Modal, Pressable, ScrollView,
         StyleSheet, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { EmptyState, FailedLine, StaleLine } from "../components/loaded";
import { OfflineNote, offlineGrey, useOffline } from "../components/offline";
import { Card, H, HelpLink } from "../components/ui";
import { ApiError, errText, PendingReimb, ReimbCandidate, ReimbPair, ReimbPairsPage }
  from "../lib/api";
import { patchList, patchQueries, removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { EMPTY_LINES, expectedFromDraft, failLine, listPhase, pairGapHint, pairSelection,
         reimbCandidateWhy, reimbLeft, reimbNeed }
  from "../lib/pure";
import { C, mmddyy, money } from "../lib/theme";
import { checkbox, useSheetAnimation } from "../lib/a11y";
import { fieldA11y } from "../components/field";

export default function Reimburse() {
  const sheetAnim = useSheetAnimation();
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  // offline, the writes here are greyed with "You're offline." beside them
  const offline = useOffline();
  const [pairing, setPairing] = useState<PendingReimb | null>(null);
  // the chosen deposits by id, with the row as it was seen: the filter
  // can take a chosen deposit out of view, and it is still sent with Pair
  const [chosen, setChosen] =
    useState<Map<string, ReimbCandidate>>(new Map());
  const [candQ, setCandQ] = useState("");
  // debounced: the candidate search is a request per keystroke otherwise
  const [candSearch, setCandSearch] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setCandSearch(candQ.trim()), 300);
    return () => clearTimeout(id);
  }, [candQ]);
  // per-match override — defaults from the flag, but a full-flagged
  // charge can still be partial-paired at match time (web checkbox)
  const [partialPick, setPartialPick] = useState(false);
  const query = useQuery({
    queryKey: ["reimburse"],
    queryFn: () => client!.reimbPending(),
    enabled: !!client,
  });
  // the undo surface (mirrors the web's Matched card): a wrong match takes
  // the charge off the list above and stamps both sides as transfers, so
  // without this the mistake is invisible everywhere the person would look.
  // It pages back ("show more") and searches by either side's merchant: a
  // wrong pair older than the newest page could otherwise never be undone.
  const [pairQ, setPairQ] = useState("");
  const [pairSearch, setPairSearch] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setPairSearch(pairQ.trim()), 300);
    return () => clearTimeout(id);
  }, [pairQ]);
  const pairs = useInfiniteQuery({
    queryKey: ["reimb-pairs", pairSearch],
    queryFn: ({ pageParam }) => client!.reimbPairs(pageParam, pairSearch),
    initialPageParam: 1,
    getNextPageParam: (last: ReimbPairsPage, all) =>
      last.more ? all.length + 1 : undefined,
    enabled: !!client,
    placeholderData: keepPreviousData,
  });
  const pairRows = pairs.data?.pages.flatMap((pg) => pg.pairs) ?? [];
  const unlink = useMutation({
    mutationFn: (p: ReimbPair) =>
      client!.reimbUnlink(p.expense_id, p.reimburse_id),
    onSuccess: (_r, p) => {
      // every cached search and page drops the row at once
      patchQueries<InfiniteData<ReimbPairsPage>>(qc, ["reimb-pairs"],
        (old) => ({ ...old, pages: old.pages.map((pg) => ({
          ...pg, pairs: pg.pairs.filter((x) => !(
            x.expense_id === p.expense_id
            && x.reimburse_id === p.reimburse_id)) })) }));
      qc.invalidateQueries({ queryKey: ["reimburse"] });
      qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
      qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
    },
    // a failed undo must not read as done — the row stays and says why
    // (the web surfaces the same error through its notice line)
    onError: (e) => Alert.alert("Couldn't undo the match", errText(e)),
  });
  const candidates = useQuery({
    queryKey: ["reimb-candidates", pairing?.id, candSearch],
    queryFn: () => client!.reimbCandidates(pairing!.id, candSearch),
    enabled: !!client && !!pairing,
    // keep the last list up while the narrower search loads — no flash —
    // but only for the SAME charge: another charge's deposits under this
    // one's title are tappable wrong answers
    placeholderData: (prev, q) =>
      q?.queryKey[1] === pairing?.id ? prev : undefined,
  });
  // What the charge being paired still needs and what already came back,
  // from the candidates door (the pending row's own server-sent figures
  // until it answers): a charge partly repaid is matched against the rest, not its
  // face value — the figures the server ranks and links by.
  const live = pairing ? {
    ...pairing,
    left_amount: candidates.data?.anchor?.left_amount
      ?? (pairing.left_amount !== undefined ? pairing.left_amount
        // older-server path: stands in for `left_amount`
        : Math.abs(pairing.amount) - (pairing.received ?? 0)),
    received: candidates.data?.anchor?.received ?? pairing.received,
  } : null;
  // "selected $X of $Y — …" is the server's: the link rule run on the
  // chosen rows and undone, so it says what Pair will really do. Asked
  // once the choosing pauses: each ask runs the link under the household's
  // pairing lock, so a burst of taps is one request, not one per tap.
  const [settled, setChosenSettled] = useState<string[]>([]);
  useEffect(() => {
    const id = setTimeout(() => setChosenSettled([...chosen.keys()].sort()),
                          300);
    return () => clearTimeout(id);
  }, [chosen]);
  const sel = useQuery({
    queryKey: ["reimb-selection", pairing?.id, settled, partialPick],
    queryFn: () => client!.reimbSelection(pairing!.id, settled, partialPick),
    enabled: !!client && !!pairing && settled.length > 0,
    placeholderData: keepPreviousData,
    retry: false,
  });
  // a server older than the selection door answers it 404 (405 behind
  // some proxies)
  const noSelectionDoor = sel.error instanceof ApiError
    && (sel.error.status === 404 || sel.error.status === 405);
  // pairing or unflagging takes the charge out of (or nets it in) spend,
  // so the ledger and the verdict really move
  const done = () => {
    setPairing(null);
    setChosen(new Map());
    qc.invalidateQueries({ queryKey: ["reimburse"] });
    qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
  };
  const link = useMutation({
    // a partial-flagged expense must PARTIAL-pair — a full link would
    // net the whole expense out of spend instead of only the received
    mutationFn: () => client!.reimbLink(pairing!.id, [...chosen.keys()],
                                        partialPick),
    onSuccess: (r) => {
      // A clean full match leaves the list now; the refetch in done()
      // settles the rest. A partial link (asked for, or downgraded by the
      // server because the deposit is bigger than the charge) is not
      // patched: what it netted is min(the charge's remaining need, each
      // deposit's remaining balance), decided server-side and not in the
      // response, so the deposits' face value would overstate it. A link
      // that partly failed is not paired either: the row stays, and the
      // alert below says why.
      const errs: string[] = (r as { errors?: string[] }).errors ?? [];
      // notes are outcomes, not refusals (the web shows them the same way)
      const notes: string[] = r.notes ?? [];
      const p = pairing!;
      if (!partialPick && errs.length === 0 && notes.length === 0)
        removeFromList<{ pending: PendingReimb[] }, PendingReimb>(
          qc, ["reimburse"], "pending", (x) => x.id === p.id);
      done();
      // the new pair belongs in the Matched card, the undo surface
      qc.invalidateQueries({ queryKey: ["reimb-pairs"] });
      // a partly-failed pairing must not read as success — and a clean
      // one says so (the web's sentence), not silence
      const linked = (r as { linked?: number }).linked ?? chosen.size;
      const noteText = notes.length ? `\n\n${notes.join("\n")}` : "";
      if (errs.length)
        Alert.alert("Some deposits didn't pair",
          errs.slice(0, 3).join("\n")
          + (errs.length > 3 ? `\n…and ${errs.length - 3} more` : "")
          + noteText);
      else
        Alert.alert("Linked",
          `Linked ${p.payee} to ${linked} deposit${
            linked === 1 ? "" : "s"}.` + noteText);
    },
  });
  const unflag = useMutation({
    mutationFn: (id: string) => client!.reimbUnflag(id),
    onSuccess: (_r, id) => {
      // the row leaves at once; unflagging puts the charge back into
      // spend, so the ledger and verdict refetch too
      removeFromList<{ pending: PendingReimb[] }, PendingReimb>(
        qc, ["reimburse"], "pending", (x) => x.id === id);
      done();
    },
    // a refused unflag must not read as done — the row stays
    onError: (e) => Alert.alert("Couldn't remove it from the list",
                                errText(e)),
  });
  // re-flagging an already-flagged row updates its partial/expected —
  // the web's after-the-fact edit path
  const reflag = useMutation({
    mutationFn: ({ id, partial, expected }:
        { id: string; partial: boolean; expected?: number | null }) =>
      client!.reimbFlag(id, { partial, expected }),
    onSuccess: (_r, v) => {
      // "✓ partial" and the progress line flip now; the refetch confirms
      patchList<{ pending: PendingReimb[] }, PendingReimb>(
        qc, ["reimburse"], "pending", (x) => x.id === v.id,
        { partial: v.partial ? 1 : 0, expected: v.expected ?? null });
      qc.invalidateQueries({ queryKey: ["reimburse"] });
      if (v.id === expFor) { setExpFor(null); setExpErr(null); }
    },
    // the server refuses a bad amount (400) and a flag that changed since
    // this list loaded (409): an open editor stays open with the reason,
    // a toggle says it did not take, and the list refetches so a stale
    // row shows what it is now
    onError: (e, v) => {
      if (v.id === expFor) setExpErr(errText(e));
      else Alert.alert("Couldn't update the reimbursement", errText(e));
      qc.invalidateQueries({ queryKey: ["reimburse"] });
    },
  });
  const [expFor, setExpFor] = useState<string | null>(null);
  const [expDraft, setExpDraft] = useState("");
  const [expErr, setExpErr] = useState<string | null>(null);
  const pending = query.data?.pending ?? [];
  // the page's headline number: how much is still owed back — the
  // server's (alerts.reimb_owed), the figure Today and the daily email
  // state, as on the web; an older server sends none and the line leaves
  // the amount out
  const owed = query.data?.owed;
  // a first fetch that failed is not "nothing awaiting payback"
  const phase = listPhase({ isError: query.isError, hasData: !!query.data },
                          pending.length);
  // how long each has waited is the server's, in the household's days
  // older-server path: stands in for `oldest_label` / `waited_label`
  // (counted on this device's clock, as before)
  const waitDays = (iso: string) => Math.max(0, Math.round(
    (Date.now() - new Date(iso + "T00:00:00").getTime()) / 86_400_000));
  const waitWords = (n: number) => n === 0 ? "today"
    : n === 1 ? "waiting 1 day" : `waiting ${n} days`;
  const oldestDays = pending.length
    ? Math.max(...pending.map((p) => waitDays(p.date))) : 0;
  const oldestLabel = query.data && query.data.oldest_label !== undefined
    ? query.data.oldest_label
    : (oldestDays > 0 ? waitWords(oldestDays) : null);

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleLine query={query} />
      {!viewer && <OfflineNote />}
      {phase === "loading" && <Text style={s.center}>Loading…</Text>}
      {phase === "failed" && (
        <FailedLine q={query} what="reimbursements" />
      )}
      {phase === "empty" && (
        // the web's line and action
        <EmptyState title={EMPTY_LINES.reimburse} to="/(tabs)/transactions"
                    action={EMPTY_LINES.reimburseAction} />
      )}
      {pending.length > 0 && (
        <Card>
          <H>Awaiting payback</H>
          <Text style={[s.mut, { marginBottom: 4 }]}>
            {owed != null && (
              <Text style={{ color: C.text, fontSize: 16, fontWeight: "700" }}>
                {money(owed)}
              </Text>)}
            {owed != null ? " owed back across " : "Owed back across "}
            {pending.length}
            {" charge"}{pending.length === 1 ? "" : "s"}
            {oldestLabel ? ` · oldest ${oldestLabel}` : ""}
          </Text>
          <Text style={[s.mut, { marginBottom: 4 }]}>
            Flagged charges still count as real spending until matched.
          </Text>
          {pending.map((p) => {
            // per-row pending: only the tapped row dims and ignores a
            // second tap (which would toggle partial straight back)
            const reflagBusy = reflag.isPending
              && reflag.variables?.id === p.id;
            const unflagBusy = unflag.isPending && unflag.variables === p.id;
            return (
            <View key={p.id} style={[s.row, unflagBusy && { opacity: 0.5 }]}>
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center",
                               gap: 6 }}>
                  <Text style={[s.payee, { flexShrink: 1 }]}
                        numberOfLines={1}>{p.payee}</Text>
                  {p.pending ? <Text style={s.pill}>pend</Text> : null}
                  {p.partial ? <Text style={s.pill}>partial</Text> : null}
                </View>
                <Text style={s.mut}>
                  {/* charges are money out — signed, like the web */}
                  {mmddyy(p.date)} · −{money(Math.abs(p.amount))}
                  {p.account ? ` · ${p.account}` : ""}
                  {p.waited_label !== undefined
                    ? (p.waited_label ? ` · ${p.waited_label}` : "")
                    : ` · ${waitWords(waitDays(p.date))}`}
                </Text>
                {/* a PARTIAL flag shows its progress from $0 — hiding
                    the line until money arrives would make "partial"
                    invisible exactly when it matters (the web shows got
                    $0 of $X) */}
                {(p.partial || (p.received ?? 0) > 0) && (
                  <Text style={s.mut}>
                    got {money(p.received ?? 0)} of{" "}
                    {money(p.expected ?? Math.abs(p.amount))}
                    {/* the server's percent (alerts.reimb_received_pct,
                        the web's rule); an older server sends none and
                        the line leaves it out */}
                    {p.received_pct != null ? ` (${p.received_pct}%)` : ""}
                  </Text>
                )}
                {!viewer && (
                  <View style={{ flexDirection: "row", gap: 12 }}>
                    <Text style={[{ color: C.accent, fontSize: 11 },
                                  reflagBusy && { opacity: 0.4 }]}
                          onPress={() => !reflagBusy && reflag.mutate({
                            id: p.id, partial: !p.partial,
                            expected: p.expected ?? null })}>
                      {p.partial
                        ? "✓ partial reimbursement"
                        : "only part comes back"}
                    </Text>
                    {p.partial ? (
                      <Text style={{ color: C.accent, fontSize: 11 }}
                            onPress={() => { setExpFor(p.id);
                              setExpErr(null);
                              setExpDraft(p.expected != null
                                ? String(p.expected) : ""); }}>
                        edit expected
                      </Text>
                    ) : null}
                  </View>
                )}
                {expFor === p.id && (
                  <View style={{ flexDirection: "row", gap: 6,
                                 marginTop: 4 }}>
                    <TextInput {...fieldA11y("Expected $")} style={s.expInput}
                               keyboardType="decimal-pad"
                               placeholder="expected $"
                               placeholderTextColor={C.mut}
                               value={expDraft}
                               onChangeText={setExpDraft} />
                    <Text style={{ color: C.accent, fontSize: 12,
                                   alignSelf: "center" }}
                          onPress={() => { if (reflagBusy) return;
                            // blank or 0 clears the expectation (the
                            // web's reading); a negative or unreadable
                            // amount is refused here, never sent
                            const v = expectedFromDraft(expDraft);
                            if (!v.ok) {
                              setExpErr("Enter an amount above $0, or "
                                + "leave it blank to clear it.");
                              return;
                            }
                            setExpErr(null);
                            reflag.mutate({ id: p.id, partial: true,
                                            expected: v.expected }); }}>
                      {reflagBusy ? "saving…" : "save"}
                    </Text>
                    <Text style={{ color: C.mut, fontSize: 12,
                                   alignSelf: "center" }}
                          onPress={() => { setExpFor(null);
                                           setExpErr(null); }}>
                      cancel
                    </Text>
                  </View>
                )}
                {expFor === p.id && expErr ? (
                  <Text style={{ color: C.bad, fontSize: 12,
                                 marginTop: 2 }}>
                    {expErr}
                  </Text>
                ) : null}
              </View>
              {!viewer && (<>
              <Pressable accessibilityRole="button" style={s.btn}
                         onPress={() => { setChosen(new Map());
                                          setCandQ("");
                                          // another charge's refusal must
                                          // not show under this one
                                          link.reset();
                                          setPartialPick(!!p.partial);
                                          setPairing(p); }}>
                <Text style={s.btnText}>Pair</Text>
              </Pressable>
              <Pressable accessibilityRole="button"
                         style={[s.btn, s.btnQuiet, offlineGrey(offline)]}
                         disabled={unflagBusy || offline}
                         onPress={() => unflag.mutate(p.id)}>
                <Text style={[s.btnText, { color: C.mut }]}>
                  remove from this list</Text>
              </Pressable>
              </>)}
            </View>
            );
          })}
        </Card>
      )}

      {/* nothing ever matched: no card; a search with no hits keeps it,
          so the search can be changed */}
      {(pairRows.length > 0 || !!pairSearch || !!pairQ
        || (pairs.data === undefined && pairs.isError)) && (
        <Card>
          <H>Matched</H>
          <Text style={[s.mut, { marginBottom: 4 }]}>
            Each pair nets a charge against the deposit that paid it back.
            Undo a wrong match and both sides count as real spending and
            income again.
          </Text>
          <TextInput style={s.candSearch}
                     placeholder="find a pair by merchant…"
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     accessibilityLabel="find a matched pair by merchant"
                     value={pairQ} onChangeText={setPairQ} />
          {/* a failed search is not "no pair names it" */}
          {pairs.data === undefined && pairs.isError && (
            <FailedLine q={pairs} what="the matched pairs" />)}
          {pairRows.length === 0 && pairs.data !== undefined && (
            <Text style={s.mut}>No matched pair names “{pairSearch}”.</Text>
          )}
          {pairRows.map((p) => {
            const busy = unlink.isPending
              && unlink.variables?.expense_id === p.expense_id
              && unlink.variables?.reimburse_id === p.reimburse_id;
            return (
              <View key={p.expense_id + p.reimburse_id}
                    style={[s.row, busy && { opacity: 0.5 }]}>
                <View style={{ flex: 1 }}>
                  <View style={{ flexDirection: "row",
                                 alignItems: "center", gap: 6 }}>
                    <Text style={[s.payee, { flexShrink: 1 }]}
                          numberOfLines={1}>{p.expense_payee}</Text>
                    {p.partial ? <Text style={s.pill}>partial</Text> : null}
                  </View>
                  <Text style={s.mut}>
                    {/* the server's words for what the pair nets, as the
                        web prints them: no sign, a partial names the
                        charge it came back of */}
                    {/* older-server path: stands in for `amount_label` */}
                    {mmddyy(p.expense_date)}{p.amount_label !== undefined
                      ? (p.amount_label ? ` · ${p.amount_label}` : "")
                      : ` · −${money(Math.abs(p.partial ? (p.received ?? 0)
                                                        : p.expense_amount))}`}
                  </Text>
                  {/* the server says when a deposit came well before
                      its charge, as the web prints it */}
                  <Text style={s.mut} numberOfLines={p.deposit_note ? 2 : 1}>
                    paid back by {p.deposit_payee} · {mmddyy(p.deposit_date)}
                    {p.deposit_note ? ` · ${p.deposit_note}` : ""}
                  </Text>
                </View>
                {!viewer && (
                  <Pressable accessibilityRole="button"
                             style={[s.btn, s.btnQuiet, offlineGrey(offline)]} disabled={busy || offline}
                             onPress={() => unlink.mutate(p)}>
                    <Text style={[s.btnText, { color: C.mut }]}>
                      undo match</Text>
                  </Pressable>
                )}
              </View>
            );
          })}
          {pairs.hasNextPage && (
            <Pressable accessibilityRole="button"
                       style={[s.btn, s.btnQuiet, { alignSelf: "center" }]}
                       disabled={pairs.isFetchingNextPage}
                       onPress={() => pairs.fetchNextPage()}>
              <Text style={[s.btnText, { color: C.mut }]}>
                {pairs.isFetchingNextPage ? "loading…" : "show more"}</Text>
            </Pressable>
          )}
          {pairs.isFetchNextPageError && (
            <Text style={{ color: C.bad, fontSize: 12 }}>
              {failLine(pairs.error, "more", "phone").text}</Text>
          )}
        </Card>
      )}

      <Modal visible={!!pairing} animationType={sheetAnim} transparent
             onRequestClose={() => setPairing(null)}>
        {/* edge-to-edge Android does not resize for the keyboard: without
            the avoiding view the filter is typed under the keys */}
        <KeyboardAvoidingView style={s.sheetWrap} behavior="padding">
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>
              Payback for {pairing?.payee} ({pairing ? money(pairing.amount) : ""})
            </Text>
            <Text style={s.mut}>
              {/* the candidate search spans 180 days either side of the
                  charge, so this states the window, not a start date */}
              Deposits near {pairing ? money(pairing.amount) : ""}, within
              180 days — pick the one that paid this back. Tick every
              deposit that covers this charge — one deposit can reimburse
              many charges.
              {pairing?.partial
                ? " This expense expects a PARTIAL payback — only the"
                  + " received amount nets out."
                : ""}
            </Text>
            <TextInput {...fieldA11y("Filter deposits by merchant")} style={s.candSearch}
                       placeholder="filter deposits by merchant…"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={candQ} onChangeText={setCandQ} />
            {/* a stale list (same charge, older search) shows dimmed and
                ignores taps until the current one lands */}
            <ScrollView style={{ maxHeight: 420 }}
                        keyboardShouldPersistTaps="handled"
                        pointerEvents={candidates.isPlaceholderData
                                       ? "none" : "auto"}>
              {candidates.isPending && <Text style={s.mut}>Loading…</Text>}
              {candidates.isError && (
                <FailedLine q={candidates} what="the deposits" />)}
              {(candidates.data?.candidates ?? []).map((c) => {
                const on = chosen.has(c.id);
                return (
                  <Pressable key={c.id}
                             {...checkbox(on, `${mmddyy(c.date)} · ${c.payee} · `
                               + `+${money(-c.amount)}`)}
                             accessibilityHint={[c.pending ? "pending" : "", c.account,
                               live ? reimbCandidateWhy(c, live) : ""]
                               .filter(Boolean).join(" · ") || undefined}
                             style={[s.candRow, candidates.isPlaceholderData
                                                && { opacity: 0.5 }]}
                             onPress={() => setChosen((prev) => {
                               const next = new Map(prev);
                               if (on) next.delete(c.id); else next.set(c.id, c);
                               return next;
                             })}>
                    <Text style={{ color: on ? C.accent : C.text,
                                   fontSize: 15 }}>
                      {on ? "☑" : "☐"} {mmddyy(c.date)} · {c.payee} ·{" "}
                      +{money(-c.amount)}
                    </Text>
                    <Text style={[s.mut, { marginLeft: 24 }]}
                          numberOfLines={1}>
                      {c.pending ? "pending · " : ""}
                      {c.account ? `${c.account} · ` : ""}
                      {live ? reimbCandidateWhy(c, live) : ""}
                    </Text>
                  </Pressable>
                );
              })}
              {candidates.data && candidates.data.candidates.length === 0 && (
                <Text style={s.mut}>
                  No matching deposits found yet — pair it once the payback
                  posts.
                </Text>
              )}
            </ScrollView>
            {link.isError ? (
              <Text style={{ color: C.bad, fontSize: 12 }}>
                {errText(link.error)}
              </Text>
            ) : null}
            <Pressable style={{ flexDirection: "row", gap: 8,
                                alignItems: "center" }}
                       {...checkbox(partialPick, "partial reimbursement — only the "
                         + "received amount nets out of spend")}
                       onPress={() => setPartialPick((x) => !x)}>
              <Text style={{ color: partialPick ? C.accent : C.mut,
                             fontSize: 15 }}>
                {partialPick ? "☑" : "☐"}
              </Text>
              <Text style={{ color: C.text, fontSize: 12, flex: 1 }}>
                partial reimbursement — only the received amount nets
                out of spend
              </Text>
            </Pressable>
            {chosen.size > 0 && pairing && (() => {
              // every chosen deposit counts, including one the filter has
              // since hidden — Pair sends it, so the line must cover it
              const { total, hidden } = pairSelection(chosen,
                (candidates.data?.candidates ?? []).map((c) => c.id));
              const a = live ?? pairing;
              const line = sel.data?.line !== undefined ? sel.data.line
                : noSelectionDoor
                  // older-server path: stands in for `selection.line`
                  ? `selected +${money(total)} of ${money(reimbNeed(a))}`
                    + ((a.received ?? 0) > 0.005 && pairing.amount > 0
                      ? " still owed" : "")
                    + pairGapHint([...chosen.values()].map(reimbLeft), a,
                                  partialPick)
                  : null;
              if (line === null && sel.isError)
                return <FailedLine q={sel} what="the selected total" />;
              if (line === null && !hidden) return null;
              return (
                <Text style={[s.mut, { textAlign: "center" }]}>
                  {line ?? ""}
                  {hidden > 0
                    ? `\n${hidden} selected deposit${hidden === 1
                        ? " is" : "s are"} hidden by the filter and ${
                        hidden === 1 ? "is" : "are"} still included`
                    : ""}
                </Text>
              );
            })()}
            <View style={{ flexDirection: "row", gap: 8,
                           justifyContent: "center" }}>
              <Pressable accessibilityRole="button"
                         style={[s.btn, chosen.size === 0 && { opacity: 0.5 }, offlineGrey(offline)]}
                         disabled={chosen.size === 0 || link.isPending || offline}
                         onPress={() => link.mutate()}>
                <Text style={s.btnText}>
                  Pair {chosen.size > 0 ? `(${chosen.size})` : ""}
                </Text>
              </Pressable>
              <OfflineNote />
              <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]}
                         onPress={() => setPairing(null)}>
                <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
              </Pressable>
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>
      <HelpLink topic="reimbursements" />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  row: { flexDirection: "row", alignItems: "center", gap: 8,
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 8 },
  payee: { color: C.text, fontSize: 14, fontWeight: "600" },
  mut: { color: C.mut, fontSize: 12 },
  pill: { color: C.warn, fontSize: 10, borderColor: C.warn, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 6, paddingVertical: 1,
          overflow: "hidden" },
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 7 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 10 },
  sheetTitle: { color: C.text, fontSize: 16, fontWeight: "700" },
  candRow: { paddingVertical: 9, borderTopColor: C.border,
             borderTopWidth: StyleSheet.hairlineWidth },
  candSearch: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
                borderRadius: 8, color: C.text, fontSize: 14,
                paddingHorizontal: 10, paddingVertical: 7 },
  expInput: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
              borderRadius: 8, color: C.text, fontSize: 13, width: 110,
              paddingHorizontal: 8, paddingVertical: 5 },
});
