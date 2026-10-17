// The ledger assistant — ask about your money in plain words. The
// server does all the thinking, and keeps the household's log of
// answered questions as CONVERSATIONS: every question opens one, and
// "follow up" continues it — the model then sees that thread's earlier
// turns, and only that thread's. This screen shows the conversations
// newest-first under the asker (one asked on the web is here too) and
// lets anyone who can edit tidy them. Only the question in flight, one
// that failed, and an answer the log does not carry (a server that
// predates it, or a turn it could not keep) live in this screen alone.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Text,
         TextInput, useWindowDimensions, View } from "react-native";

import { useSession } from "../lib/session";
import { C, mmddyy } from "../lib/theme";
import { LogoSpinner } from "../components/logo-spinner";
import { AnswerBlocks } from "../components/answer-blocks";
import { conversationGone, type FollowUp, followUpGone, groupThreads, type HistoryCursor,
         type HistoryPage, historyNotKept, keepNotes, type LocalTurn, localTurn, mayTidyLog,
         mergeTurns, pagingCursor, partialNote, pruneLocal, reportBlocks,
         RESTARTED_NOTE, sharedLogDenied, type Thread, threadDoorMissing,
         threadMissing, type TurnNotes, turnMissing } from "../lib/answer-pure";
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
  const [followUp, setFollowUp] = useState<FollowUp | null>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [older, setOlder] = useState<AssistantTurn[]>([]);
  const [mine, setMine] = useState<LocalTurn[]>([]);
  const [notes, setNotes] = useState<TurnNotes>({});
  const [lastOlder, setLastOlder] = useState<HistoryPage | null>(null);
  const [noMore, setNoMore] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  // the bubble is 88% of the screen less its padding and the list's;
  // the charts fill that width
  const chartWidth = Math.floor(useWindowDimensions().width * 0.88) - 44;
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
  // a conversation removed elsewhere drops out of the log: the chip that
  // names it goes too, so the next question is not sent to a dead thread
  useEffect(() => {
    if (!page) return;
    setFollowUp((f) => f && followUpGone(f, mergeTurns(mine, page.turns, older, notes), page)
      ? null : f);
  }, [page, mine, older, notes]);
  // a conversation the server no longer has: its rows leave the screen
  // and the chip naming it is cleared
  const dropThread = (threadId: string) => {
    setOlder((o) => o.filter((t) => (t.thread_id ?? t.id) !== threadId));
    setMine((m) => m.filter((t) => t.thread_id !== threadId));
    setFollowUp((f) => f?.id === threadId ? null : f);
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
      setMine((m) => [...m, localTurn(`local-${v.key}`, v.q, reply, new Date(), thread, note)]);
      // the thread went away while the answer was being written
      if (reply.removed && thread) dropThread(thread);
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
                       setFollowUp(null);
                       void qc.invalidateQueries({ queryKey: ["assistant-history"] }); },
    onError: (e) => sharedLogDenied(e) ? setDenied(errText(e))
      : Alert.alert("Couldn't clear the history", errText(e)),
  });
  const more = useMutation({
    mutationFn: (cursor: HistoryCursor) => client!.assistantHistory(cursor),
    onSuccess: (r) => { setOlder((o) => [...o, ...r.turns]); setLastOlder(r);
                        if (!r.more) setNoMore(true); },
    onError: (e) => Alert.alert("Couldn't load more", errText(e)),
  });
  // a conversation the server never kept has nothing to delete there
  const remove = (th: Thread<Turn>) => {
    if (th.id === null) setMine((m) => m.filter((x) => !th.turns.includes(x)));
    else forget.mutate(th.id);
  };
  const save = async (t: Turn) => {
    setSaving(rowKey(t));
    try {
      await client!.assistantReport(t.question, reportBlocks(t.answer, t.blocks));
    } catch (e) {
      Alert.alert("Couldn't save the report", errText(e));
    }
    setSaving(null);
  };
  const submit = (question: string, thread: string | null = followUp?.id ?? null) => {
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
  const turns = mergeTurns(mine, history.data?.turns ?? [], older, notes);
  const threads = groupThreads<Turn>(turns);
  // a question in flight draws inside its conversation; a new one on top
  const pendingFor = (th: Thread<Turn>) => pending.filter((p) => p.thread === th.id);
  const pendingNew = pending.filter((p) => p.thread === null
    || !threads.some((th) => th.id === p.thread));
  // paging walks the log by (asked_at, id), from the cursor the last page
  // read handed back; a page with no more ends it
  const cursor = noMore ? null : pagingCursor(history.data, lastOlder);

  const pendingRows = (rows: Pending[]) => [...rows].reverse().map((t) => (
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

  return (
    <View style={s.wrap}>
      <ScrollView style={{ flex: 1 }}
                  contentContainerStyle={{ padding: 12, gap: 10 }}>
        {threads.length === 0 && pending.length === 0 && (
          <Text style={s.center}>
            {/* "on this instance" only when the server says the model is
                local; an operator-configured remote backend has no host
                named, and the answer still leaves the box */}
            {status.data?.local === false
              ? `Ask about your money — answered from your own ledger; `
                + `your questions and the figures they return go to `
                + `${status.data.host
                    || "the model host this instance is configured with"}.`
              : "Ask about your money — everything is answered from your "
                + "own ledger, on this instance."}
          </Text>
        )}
        {/* the chips stay after the first question — that's exactly
            when someone wants the next one; each opens a new conversation */}
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
          {EXAMPLES.map((ex) => (
            <Pressable key={ex} style={s.chip}
                       disabled={ask.isPending}
                       onPress={() => submit(ex, null)}>
              <Text style={{ color: C.mut, fontSize: 12 }}>{ex}</Text>
            </Pressable>
          ))}
        </View>
        {pendingRows(pendingNew)}
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
        {denied && (
          <Text style={{ color: C.mut, fontSize: 12 }}>{denied}</Text>
        )}
        {threads.map((th) => (
          <View key={th.key} style={[s.thread, followUp?.id === th.id && th.id !== null && s.threadOpen]}>
            {/* the log pages by turn, so a conversation can straddle a
                page: its first loaded question is then not its first —
                and when no older page is left, its start is gone */}
            {partialNote(th.partial, cursor !== null) && (
              <Text style={{ color: C.mut, fontSize: 12 }}>
                {partialNote(th.partial, cursor !== null)}</Text>
            )}
            {th.turns.map((t) => (
              <View key={rowKey(t)} style={{ gap: 6 }}>
                <View style={[s.bubble, s.me]}>
                  <Text style={s.bubbleText}>{t.question}</Text>
                </View>
                <View style={[s.bubble, s.them]}>
                  <Text style={{ color: C.mut, fontSize: 10, marginBottom: 4 }}>
                    {when(t.asked_at)}{t.asked_by ? ` · ${t.asked_by}` : ""}
                  </Text>
                  {/* why this answer is not the follow-up it was asked as */}
                  {t.note && (
                    <Text style={{ color: C.mut, fontSize: 12, marginBottom: 4 }}>{t.note}</Text>
                  )}
                  {/* typed blocks — prose, charts, tables — in order; a turn
                      kept by an older server has the prose alone.
                      selectable = long-press copies */}
                  {t.blocks.length
                    ? <AnswerBlocks blocks={t.blocks} width={chartWidth} />
                    : <Text style={s.bubbleText} selectable>{t.answer}</Text>}
                  {t.tools_used.length > 0 && (
                    <Text style={{ color: C.mut, fontSize: 10, marginTop: 4 }}>
                      from {t.tools_used.join(" · ")}
                    </Text>
                  )}
                  {/* the whole answer, charts included, as one
                      self-contained HTML file to keep or send */}
                  <Pressable onPress={() => void save(t)} hitSlop={8}
                             disabled={saving !== null} style={{ marginTop: 6 }}>
                    <Text style={s.act}>
                      {saving === rowKey(t) ? "saving…" : "save as report"}</Text>
                  </Pressable>
                </View>
              </View>
            ))}
            {pendingRows(pendingFor(th))}
            <View style={{ flexDirection: "row", gap: 14, marginTop: 2,
                           flexWrap: "wrap", alignItems: "center" }}>
              {/* a conversation the server kept can be continued; the
                  model is then handed this thread and no other */}
              {th.id !== null && (
                <Pressable hitSlop={8}
                           onPress={() => setFollowUp({ id: th.id!, question: th.turns[0].question,
                                                       last_at: th.last_at })}>
                  <Text style={s.act}>
                    {followUp?.id === th.id ? "following up…" : "follow up"}</Text>
                </Pressable>
              )}
              {canEdit && (
                <Pressable onPress={() => remove(th)} hitSlop={8}
                           disabled={forget.isPending}>
                  <Text style={[s.act, { color: C.mut }]}>remove</Text>
                </Pressable>
              )}
            </View>
          </View>
        ))}
        {history.isPending && status.data?.available && (
          <LogoSpinner color={C.mut} />
        )}
        {history.isError && !noLog && (
          <View style={{ alignItems: "center", gap: 6 }}>
            <Text style={{ color: C.bad, textAlign: "center" }}>
              Couldn&apos;t load the history: {errText(history.error)}</Text>
            <Pressable onPress={() => void history.refetch()} hitSlop={8}>
              <Text style={s.act}>try again</Text></Pressable>
          </View>
        )}
        {cursor && (
          <Pressable onPress={() => more.mutate(cursor)} hitSlop={8}
                     disabled={more.isPending} style={{ alignSelf: "center" }}>
            <Text style={s.act}>{more.isPending ? "loading…" : "older conversations"}</Text>
          </Pressable>
        )}
      </ScrollView>
      {/* the input continues the conversation named in the chip, or
          opens a new one when there is none */}
      {followUp && (
        <View style={s.followRow}>
          <Text style={{ color: C.mut, fontSize: 12, flex: 1 }} numberOfLines={1}>
            following up on “{followUp.question}”</Text>
          <Pressable onPress={() => setFollowUp(null)} hitSlop={8}>
            <Text style={s.act}>new question</Text></Pressable>
        </View>
      )}
      <View style={s.inputRow}>
        <TextInput style={s.input}
                   placeholder={followUp ? "ask a follow-up…" : "ask anything…"}
                   placeholderTextColor={C.mut} value={q}
                   onChangeText={setQ} onSubmitEditing={() => submit(q)}
                   returnKeyType="send" />
        <Pressable style={[s.send, (!q.trim() || ask.isPending) && { opacity: 0.5 }]}
                   disabled={!q.trim() || ask.isPending} onPress={() => submit(q)}>
          <Text style={s.sendText}>Ask</Text>
        </Pressable>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  thread: { gap: 6, paddingBottom: 8, borderBottomColor: C.border, borderBottomWidth: 1 },
  threadOpen: { borderBottomColor: C.accent },
  bubble: { borderRadius: 12, padding: 10, maxWidth: "88%" },
  me: { backgroundColor: C.accent, alignSelf: "flex-end" },
  them: { backgroundColor: C.card, alignSelf: "flex-start",
          borderColor: C.border, borderWidth: 1 },
  bubbleText: { color: C.text, fontSize: 14, lineHeight: 20 },
  act: { color: C.accent, fontSize: 12 },
  followRow: { flexDirection: "row", gap: 8, alignItems: "center",
               paddingHorizontal: 12, paddingTop: 8 },
  inputRow: { flexDirection: "row", gap: 8, padding: 10,
              borderTopColor: C.border, borderTopWidth: 1 },
  input: { flex: 1, backgroundColor: C.card, borderColor: C.border,
           borderWidth: 1, borderRadius: 10, color: C.text, fontSize: 15,
           paddingHorizontal: 12, paddingVertical: 9 },
  send: { backgroundColor: C.accent, borderRadius: 10,
          paddingHorizontal: 18, justifyContent: "center" },
  sendText: { color: C.text, fontSize: 15, fontWeight: "600" },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
});
