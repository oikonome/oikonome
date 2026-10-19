// The ledger assistant — ask about your money in plain words. The
// server does all the thinking, and keeps the household's log of
// answered questions as CONVERSATIONS: every question opens one, and
// typing into an open conversation continues it — the model then sees
// that thread's earlier turns, and only that thread's. The screen is a
// chat: the list of conversations (newest first; one asked on the web
// is here too), and the open one as messages with the box to continue
// it at the bottom; the device's back button leaves it for the list.
// Anyone who can edit may tidy them. Only the question in flight, one
// that failed, and an answer the log does not carry (a server that
// predates it, or a turn it could not keep) live in this screen alone.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Alert, BackHandler, Pressable, ScrollView, StyleSheet, Text,
         TextInput, useWindowDimensions, View } from "react-native";

import { useSession } from "../lib/session";
import { C, mmddyy } from "../lib/theme";
import { LogoSpinner } from "../components/logo-spinner";
import { AnswerBlocks, RichText } from "../components/answer-blocks";
import { conversationGone, followUpGone, groupThreads, type HistoryCursor,
         type HistoryPage, historyNotKept, keepNotes, type LocalTurn, localTurn, mayTidyLog,
         mergeTurns, pagingCursor, partialNote, pruneLocal, reportBlocks,
         RESTARTED_NOTE, sharedLogDenied, type Thread, threadDoorMissing,
         threadKey, threadMissing, type TurnNotes, turnMissing } from "../lib/answer-pure";
import { type AssistantTurn, errText } from "../lib/api";
import { useMe, useViewer } from "../lib/viewer";

/** a question in flight (or one that failed); `thread` is the
 *  conversation it continues, null for a new one; `note` says why it
 *  is no longer the follow-up it was asked as */
interface Pending { key: number; q: string; thread: string | null; err?: string; note?: string }

const EXAMPLES = [
  "What's my net worth?",
  "Where does my money go?",
  "How much did I spend on groceries this year?",
  "Chart my spending by month this year",
  "What are my recurring bills?",
];

const when = (iso: string) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0");
  return `${mmddyy(iso)} ${((h + 11) % 12) + 1}:${m} ${h < 12 ? "am" : "pm"}`;
};

/** a log row may carry the note of the local turn it replaced */
type Turn = (AssistantTurn & { note?: string }) | LocalTurn;
const rowKey = (t: Turn) => "local" in t ? t.key : t.id;

/** what is on screen: the list, a fresh conversation, or an open one's key */
const NEW = "new";

export default function Assistant() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const me = useMe();
  // the shared demonstration login is refused both delete doors; every
  // other editor — a demonstration household's own logins included —
  // may tidy the log. `denied` is the server saying so all the same.
  const [denied, setDenied] = useState<string | null>(null);
  const canEdit = mayTidyLog(!viewer, me.data) && !denied;
  const [q, setQ] = useState("");
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [older, setOlder] = useState<AssistantTurn[]>([]);
  const [mine, setMine] = useState<LocalTurn[]>([]);
  const [notes, setNotes] = useState<TurnNotes>({});
  const [lastOlder, setLastOlder] = useState<HistoryPage | null>(null);
  const [noMore, setNoMore] = useState(false);
  // a page read that failed: the automatic paging back (below) stops,
  // and "older conversations" is the retry; cleared when the open
  // conversation changes
  const [pageFailed, setPageFailed] = useState(false);
  const paging = useRef(false);
  const [saving, setSaving] = useState(false);
  const msgsRef = useRef<ScrollView>(null);
  // the open conversation's latest turn as last seen, by key: a thread
  // that slid past the first page (other members asking) is told from a
  // removed one by whether the page still reaches back to it
  const openLast = useRef<{ key: string; last_at: string } | null>(null);
  // the conversations on screen: a key no loaded conversation answers
  // to (one that slid past the loaded pages) is not a view a landing
  // answer must respect
  const threadsRef = useRef<Set<string>>(new Set());
  // the answer bubble is 96% of the screen less its padding and the
  // list's; the charts fill that width
  const chartWidth = Math.floor(useWindowDimensions().width * 0.96) - 44;
  const status = useQuery({
    queryKey: ["assistant-status"],
    queryFn: () => client!.assistantStatus(),
    enabled: !!client,
  });
  const history = useQuery({
    queryKey: ["assistant-history"],
    queryFn: () => client!.assistantHistory(),
    enabled: !!client && !!status.data?.available,
  });
  // a 404 is a server older than the shared log: this screen's own
  // turns are all there is, and that is not an error
  const noLog = history.isError && historyNotKept(history.error);
  // once the log carries a turn this screen kept, the log's row is the
  // one; the local turn hands over only its note, so it cannot come back
  // as a live conversation when its row later leaves the log
  const page = history.data;
  useEffect(() => {
    if (!page) return;
    setNotes((n) => keepNotes(n, mine, page.turns));
    setMine((m) => pruneLocal(m, page.turns, page.more));
  }, [page, mine]);

  const turns = mergeTurns(mine, history.data?.turns ?? [], older, notes);
  const threads = groupThreads<Turn>(turns);
  const open = openKey && openKey !== NEW ? threads.find((th) => th.key === openKey) ?? null : null;
  if (open) openLast.current = { key: open.key, last_at: open.last_at };
  threadsRef.current = new Set(threads.map((th) => th.key));
  // a conversation removed elsewhere drops out of the log: the one open
  // here closes too, so the next question is not sent to a dead thread.
  // One that merely slid past the first page stays open (its latest turn
  // is older than the page reaches); one never seen closes only when
  // there is no older page it could be on — the ask's 404 covers a
  // removed thread either way
  const olderToLoad = !noMore && pagingCursor(history.data, lastOlder) !== null;
  useEffect(() => {
    if (!page || !openKey || openKey === NEW || open) return;
    const seen = openLast.current?.key === openKey ? openLast.current.last_at : null;
    // every older page read and still no such conversation: it is gone
    const gone = !olderToLoad || (seen !== null
      && followUpGone({ id: openKey, question: "", last_at: seen }, turns, page));
    if (gone) setOpenKey(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, openKey, open, olderToLoad]);
  useEffect(() => { setPageFailed(false); }, [openKey]);
  // the device's back button leaves an open conversation for the list
  // before it leaves the screen
  useEffect(() => {
    if (openKey === null) return;
    const sub = BackHandler.addEventListener("hardwareBackPress", () => {
      setOpenKey(null);
      return true;
    });
    return () => sub.remove();
  }, [openKey]);
  // a conversation the server no longer has: its rows leave the screen
  // and it closes if it was the open one
  const dropThread = (threadId: string) => {
    setOlder((o) => o.filter((t) => (t.thread_id ?? t.id) !== threadId));
    setMine((m) => m.filter((t) => t.thread_id !== threadId));
    setOpenKey((k) => k === threadId ? null : k);
  };
  // turns key on an id, not the question text — asking the same
  // question twice must not write one answer into both bubbles
  const ask = useMutation({
    // a follow-up to a conversation removed elsewhere answers 404: the
    // question is asked once more as a new conversation, and the card
    // says so — retrying the dead thread could only fail the same way
    mutationFn: async ({ key, q: question, thread }: Pending) => {
      try {
        return { reply: await client!.assistantAsk(question, thread), thread,
                 note: undefined as string | undefined };
      } catch (e) {
        if (!thread || !conversationGone(e)) throw e;
        dropThread(thread);
        setPending((p) => p.map((x) => x.key === key
          ? { ...x, thread: null, note: RESTARTED_NOTE } : x));
        return { reply: await client!.assistantAsk(question, null), thread: null,
                 note: RESTARTED_NOTE };
      }
    },
    // the reply itself is shown at once and the log re-read: the log may
    // not have it (an older server, or a turn it could not keep), and an
    // answer must never vanish because the log is missing it
    // the restart note rides on the answer's card, not the pending one,
    // so it is still there once the answer lands
    onSuccess: ({ reply, thread, note }, v) => {
      setPending((p) => p.filter((x) => x.key !== v.key));
      const turn = localTurn(`local-${v.key}`, v.q, reply, new Date(), thread, note);
      setMine((m) => [...m, turn]);
      // the thread went away while the answer was being written
      if (reply.removed && thread) dropThread(thread);
      // a fresh conversation becomes the open one as soon as it exists —
      // unless the person has since moved elsewhere, whose draft must not
      // be sent to the new one
      if (!thread) setOpenKey((k) => k === NEW || (k !== null && !threadsRef.current.has(k))
        ? threadKey(turn) : k);
      void qc.invalidateQueries({ queryKey: ["assistant-history"] });
    },
    onError: (e, v) =>
      setPending((p) => p.map((x) =>
        x.key === v.key ? { ...x, err: errText(e) } : x)),
  });
  const forget = useMutation({
    // a server that predates conversations answers the threads door with
    // a bare 404; there the conversation is one turn under the same id,
    // removed by the turn door (whose own 404 means it is already gone)
    mutationFn: async (threadId: string) => {
      try {
        return await client!.assistantForgetThread(threadId);
      } catch (e) {
        if (!threadDoorMissing(e)) throw e;
        try {
          return await client!.assistantForgetTurn(threadId);
        } catch (e2) {
          if (turnMissing(e2)) return { ok: true, deleted: 0 };
          throw e2;
        }
      }
    },
    onSuccess: (_d, threadId) => {
      dropThread(threadId);
      void qc.invalidateQueries({ queryKey: ["assistant-history"] });
    },
    // the server's own "no such conversation" is someone having removed
    // it first: the outcome asked for
    onError: (e, threadId) => {
      if (threadMissing(e)) {
        dropThread(threadId);
        void qc.invalidateQueries({ queryKey: ["assistant-history"] });
      } else if (sharedLogDenied(e)) setDenied(errText(e));
      else Alert.alert("Couldn't remove it", errText(e));
    },
  });
  const clear = useMutation({
    mutationFn: () => client!.assistantClear(),
    onSuccess: () => { setOlder([]); setMine([]); setNotes({}); setLastOlder(null); setNoMore(true);
                       setOpenKey(null);
                       void qc.invalidateQueries({ queryKey: ["assistant-history"] }); },
    onError: (e) => sharedLogDenied(e) ? setDenied(errText(e))
      : Alert.alert("Couldn't clear the history", errText(e)),
  });
  const more = useMutation({
    mutationFn: (cursor: HistoryCursor) => client!.assistantHistory(cursor),
    onSuccess: (r) => { setOlder((o) => [...o, ...r.turns]); setLastOlder(r);
                        if (!r.more) setNoMore(true); },
    onError: (e) => { setPageFailed(true); Alert.alert("Couldn't load more", errText(e)); },
    onSettled: () => { paging.current = false; },
  });
  // an open conversation that is not on the loaded pages: page back
  // until it is found or there is no older page (then the close effect
  // above lets it go)
  const unresolved = !!openKey && openKey !== NEW && !open && !!page;
  const pageCursor = noMore ? null : pagingCursor(history.data, lastOlder);
  // keyed by the cursor's VALUE, with one request in flight at a time: a
  // server that hands no `next` makes a fresh cursor object every render
  const pageKey = pageCursor ? `${pageCursor.before}|${pageCursor.before_id ?? ""}` : null;
  useEffect(() => {
    if (!unresolved || !pageCursor || pageFailed || paging.current) return;
    paging.current = true;
    more.mutate(pageCursor);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unresolved, pageKey, pageFailed]);
  // a conversation the server never kept has nothing to delete there
  const remove = (th: Thread<Turn>) => {
    const go = () => {
      if (th.id === null) { setMine((m) => m.filter((x) => !th.turns.includes(x))); setOpenKey(null); }
      else forget.mutate(th.id);
    };
    Alert.alert("Remove this conversation?", "", [
      { text: "Cancel", style: "cancel" },
      { text: "Remove", style: "destructive", onPress: go }]);
  };
  // the whole conversation — every answer, charts included — as one
  // self-contained HTML file on the share sheet
  const save = async (th: Thread<Turn>) => {
    setSaving(true);
    try {
      // an answer the log kept empty still has a line in the page
      await client!.assistantReport(th.turns.map((t) => ({
        question: t.question,
        blocks: reportBlocks(t.answer || "(no answer was kept)", t.blocks) })), th.partial);
    } catch (e) {
      Alert.alert("Couldn't save the report", errText(e));
    }
    setSaving(false);
  };
  // the box continues the open conversation, or opens a new one when
  // none is (or the open one was never kept); the chips always open one
  const submit = (question: string, thread: string | null = open?.id ?? null) => {
    const v = question.trim();
    if (!v || ask.isPending) return;
    const key = Date.now();
    const p = { key, q: v, thread };
    setPending((x) => [...x, p]);
    setQ("");
    ask.mutate(p);
  };

  if (status.data && !status.data.available) {
    return (
      <View style={s.wrap}>
        <Text style={s.center}>
          The assistant isn&apos;t enabled on this server — it needs an
          AI backend. Add one under Settings → AI (self-host installs
          can use the bundled Ollama).
        </Text>
      </View>
    );
  }
  // a question in flight draws inside its conversation; a new one in
  // the fresh screen
  const pendingFor = (th: Thread<Turn>) => pending.filter((p) => p.thread === th.id && th.id !== null);
  const pendingNew = pending.filter((p) => p.thread === null
    || !threads.some((th) => th.id === p.thread));
  // paging walks the log by (asked_at, id), from the cursor the last page
  // read handed back; a page with no more ends it
  const cursor = noMore ? null : pagingCursor(history.data, lastOlder);
  const title = (th: Thread<Turn>) => th.turns[0]?.question ?? "";
  const whereLine = status.data?.local === false
    ? `read-only tools — your questions and the figures they return go to ${
        status.data.host || "the model host this instance is configured with"}`
    : "answered on this instance — read-only tools, nothing leaves the box";

  const pendingRows = (rows: Pending[]) => rows.map((t) => (
    <View key={t.key} style={{ gap: 6 }}>
      <View style={[s.bubble, s.me]}>
        <Text style={s.bubbleText}>{t.q}</Text>
      </View>
      <View style={[s.bubble, s.them]}>
        {t.note && (
          <Text style={{ color: C.mut, fontSize: 12, marginBottom: 4 }}>{t.note}</Text>
        )}
        {t.err
          ? (<>
              <Text style={{ color: C.bad }}>{t.err}</Text>
              <View style={{ flexDirection: "row", gap: 14, marginTop: 6 }}>
                <Pressable onPress={() => submit(t.q, t.thread)} hitSlop={8}>
                  <Text style={s.act}>try again</Text></Pressable>
                <Pressable hitSlop={8}
                           onPress={() => setPending((p) => p.filter((x) => x.key !== t.key))}>
                  <Text style={s.act}>dismiss</Text></Pressable>
              </View>
            </>)
          : <LogoSpinner color={C.mut} />}
      </View>
    </View>
  ));

  const composer = (placeholder: string) => (
    <View style={s.compose}>
      <View style={s.inputRow}>
        <TextInput style={s.input} placeholder={placeholder}
                   placeholderTextColor={C.mut} value={q} multiline
                   onChangeText={setQ} submitBehavior="blurAndSubmit"
                   onSubmitEditing={() => submit(q)} returnKeyType="send" />
        <Pressable style={[s.send, (!q.trim() || ask.isPending) && { opacity: 0.5 }]}
                   disabled={!q.trim() || ask.isPending} onPress={() => submit(q)}>
          <Text style={s.sendText}>Send</Text>
        </Pressable>
      </View>
      <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>{whereLine}</Text>
    </View>
  );

  // ---- the list of conversations
  if (openKey === null) {
    return (
      <View style={s.wrap}>
        <ScrollView style={{ flex: 1 }} contentContainerStyle={{ padding: 12, gap: 8 }}>
          <Pressable style={s.newBtn} onPress={() => setOpenKey(NEW)}>
            <Text style={s.sendText}>+ New conversation</Text>
          </Pressable>
          {threads.length > 0 && (
            <View style={{ flexDirection: "row", justifyContent: "space-between",
                           alignItems: "center", marginTop: 4 }}>
              <Text style={{ color: C.mut, fontSize: 12 }}>
                {noLog ? "This session's conversations — this server keeps no shared history"
                  : "Conversations — shared with the household"}</Text>
              {canEdit && !noLog && (
                <Pressable hitSlop={8} disabled={clear.isPending}
                           onPress={() => Alert.alert("Clear every conversation?", "",
                             [{ text: "Cancel", style: "cancel" },
                              { text: "Clear", style: "destructive",
                                onPress: () => clear.mutate() }])}>
                  <Text style={s.act}>clear all</Text>
                </Pressable>
              )}
            </View>
          )}
          {denied && <Text style={{ color: C.mut, fontSize: 12 }}>{denied}</Text>}
          {threads.map((th) => (
            <Pressable key={th.key} style={s.row} onPress={() => setOpenKey(th.key)}>
              <Text style={s.rowQ} numberOfLines={1}>{th.partial ? "… " : ""}{title(th)}</Text>
              <Text style={s.rowM}>{when(th.last_at)} · {th.turns.length}{" "}
                {th.turns.length === 1 ? "question" : "questions"}
                {th.turns[0]?.asked_by ? ` · ${th.turns[0].asked_by}` : ""}</Text>
            </Pressable>
          ))}
          {history.isPending && status.data?.available && <LogoSpinner color={C.mut} />}
          {history.isError && !noLog && (
            <View style={{ alignItems: "center", gap: 6 }}>
              <Text style={{ color: C.bad, textAlign: "center" }}>
                Couldn&apos;t load the history: {errText(history.error)}</Text>
              <Pressable onPress={() => void history.refetch()} hitSlop={8}>
                <Text style={s.act}>try again</Text></Pressable>
            </View>
          )}
          {!history.isPending && (!history.isError || noLog) && threads.length === 0 && (
            <Text style={s.center}>
              {noLog ? "Nothing asked yet."
                : "Nothing asked yet. Every conversation is kept here for the household."}
            </Text>
          )}
          {cursor && (
            <Pressable onPress={() => more.mutate(cursor)} hitSlop={8}
                       disabled={more.isPending} style={{ alignSelf: "center" }}>
              <Text style={s.act}>{more.isPending ? "loading…" : "older conversations"}</Text>
            </Pressable>
          )}
        </ScrollView>
      </View>
    );
  }

  // ---- a fresh conversation
  if (!open) {
    return (
      <View style={s.wrap}>
        <View style={s.head}>
          <Pressable onPress={() => setOpenKey(null)} hitSlop={8}>
            <Text style={s.act}>‹ Conversations</Text></Pressable>
          <Text style={s.headT} numberOfLines={1}>New conversation</Text>
        </View>
        <ScrollView ref={msgsRef} style={{ flex: 1 }}
                    contentContainerStyle={{ padding: 12, gap: 10, flexGrow: 1 }}
                    onContentSizeChange={() => msgsRef.current?.scrollToEnd({ animated: true })}>
          {unresolved && cursor && !pageFailed ? (
            <View style={{ flex: 1, justifyContent: "center" }}><LogoSpinner color={C.mut} /></View>
          ) : pendingNew.length === 0 ? (
            <View style={{ flex: 1, justifyContent: "center", alignItems: "center", gap: 10 }}>
              <Text style={{ color: C.text, fontSize: 18, fontWeight: "600" }}>Ask about your money</Text>
              <Text style={[s.center, { padding: 0 }]}>
                Plain questions, answered from your own ledger.
                {!noLog && " Every conversation is kept for the household."}
              </Text>
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6,
                             justifyContent: "center", marginTop: 6 }}>
                {EXAMPLES.map((ex) => (
                  <Pressable key={ex} style={s.chip} disabled={ask.isPending}
                             onPress={() => submit(ex, null)}>
                    <Text style={{ color: C.mut, fontSize: 12 }}>{ex}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          ) : pendingRows(pendingNew)}
        </ScrollView>
        {composer("Ask anything…")}
      </View>
    );
  }

  // ---- the open conversation
  return (
    <View style={s.wrap}>
      <View style={s.head}>
        <Pressable onPress={() => setOpenKey(null)} hitSlop={8}>
          <Text style={s.act}>‹ Conversations</Text></Pressable>
        <Text style={s.headT} numberOfLines={1}>{title(open)}</Text>
        <Pressable onPress={() => void save(open)} hitSlop={8} disabled={saving}>
          <Text style={s.act}>{saving ? "saving…" : "save as report"}</Text>
        </Pressable>
        {canEdit && (
          <Pressable onPress={() => remove(open)} hitSlop={8} disabled={forget.isPending}>
            <Text style={[s.act, { color: C.mut }]}>remove</Text>
          </Pressable>
        )}
      </View>
      <ScrollView ref={msgsRef} style={{ flex: 1 }}
                  contentContainerStyle={{ padding: 12, gap: 10 }}
                  onContentSizeChange={() => msgsRef.current?.scrollToEnd({ animated: true })}>
        {/* the log pages by turn, so a conversation can straddle a
            page: its first loaded question is then not its first —
            and when no older page is left, its start is gone */}
        {partialNote(open.partial, cursor !== null) && (
          <Text style={{ color: C.mut, fontSize: 12, textAlign: "center" }}>
            {partialNote(open.partial, cursor !== null)}</Text>
        )}
        {open.turns.map((t) => (
          <View key={rowKey(t)} style={{ gap: 6 }}>
            <View style={{ alignSelf: "flex-end", maxWidth: "88%", gap: 2 }}>
              <View style={[s.bubble, s.me, { maxWidth: "100%" }]}>
                <Text style={s.bubbleText}>{t.question}</Text>
              </View>
              <Text style={{ color: C.mut, fontSize: 10, textAlign: "right" }}>
                {when(t.asked_at)}{t.asked_by ? ` · ${t.asked_by}` : ""}
              </Text>
            </View>
            <View style={[s.bubble, s.them]}>
              {/* why this answer is not the follow-up it was asked as */}
              {t.note && (
                <Text style={{ color: C.mut, fontSize: 12, marginBottom: 4 }}>{t.note}</Text>
              )}
              {/* typed blocks — prose, charts, tables — in order; a turn
                  kept by an older server has the prose alone.
                  selectable = long-press copies */}
              {t.blocks.length
                ? <AnswerBlocks blocks={t.blocks} width={chartWidth} />
                : <RichText text={t.answer} />}
              {t.tools_used.length > 0 && (
                <Text style={{ color: C.mut, fontSize: 10, marginTop: 6 }}>
                  from {t.tools_used.join(" · ")}
                </Text>
              )}
            </View>
          </View>
        ))}
        {pendingRows(pendingFor(open))}
      </ScrollView>
      {/* the box continues THIS conversation; one the server never kept
          cannot be continued, and its box opens a new one */}
      {composer(open.id !== null ? "Continue the conversation…" : "Ask a new question…")}
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  head: { flexDirection: "row", gap: 12, alignItems: "center", paddingHorizontal: 12,
          paddingVertical: 8, borderBottomColor: C.border, borderBottomWidth: 1,
          backgroundColor: C.card },
  headT: { color: C.text, fontWeight: "600", flex: 1 },
  newBtn: { backgroundColor: C.accent, borderRadius: 10, paddingVertical: 10,
            alignItems: "center" },
  row: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1, borderRadius: 10,
         paddingHorizontal: 12, paddingVertical: 9 },
  rowQ: { color: C.text, fontWeight: "600", fontSize: 14 },
  rowM: { color: C.mut, fontSize: 11, marginTop: 2 },
  bubble: { borderRadius: 12, padding: 10, maxWidth: "88%" },
  me: { backgroundColor: C.accent, alignSelf: "flex-end" },
  them: { backgroundColor: C.card, alignSelf: "flex-start", maxWidth: "96%",
          borderColor: C.border, borderWidth: 1 },
  bubbleText: { color: C.text, fontSize: 14, lineHeight: 20 },
  act: { color: C.accent, fontSize: 12 },
  compose: { padding: 10, borderTopColor: C.border, borderTopWidth: 1, backgroundColor: C.card },
  inputRow: { flexDirection: "row", gap: 8, alignItems: "flex-end" },
  input: { flex: 1, backgroundColor: C.bg, borderColor: C.border,
           borderWidth: 1, borderRadius: 10, color: C.text, fontSize: 15,
           paddingHorizontal: 12, paddingVertical: 9, maxHeight: 120 },
  send: { backgroundColor: C.accent, borderRadius: 10,
          paddingHorizontal: 18, paddingVertical: 10, justifyContent: "center" },
  sendText: { color: "#fff", fontSize: 15, fontWeight: "600" },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
});
