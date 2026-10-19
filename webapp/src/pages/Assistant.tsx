// local ledger assistant. Ask plain-language questions about your
// own finances; the model routes to read-only tools and answers from what
// the engine computes. On the bundled LLM your data never leaves the box;
// when the assistant backend is a remote host the header says where
// answers go. The whole page hides (via the nav gate) when no LLM is
// configured.
//
// Answered questions are the household's log, kept on the server as
// CONVERSATIONS: every question opens one, and typing into an open
// conversation continues it — the model then sees that thread's earlier
// turns, and only that thread's. The page is a chat: a rail of
// conversations (newest first; one asked on the phone is here too) and
// the open one as messages with the box to continue it at the bottom.
// `?c=` names the open conversation (`new` for a fresh one), so a
// conversation is a link and the back button leaves it. Anyone who can
// edit may tidy them. Only the question in flight, one that failed, and
// an answer the log does not carry (a server that predates it, or a turn
// it could not keep) live in this tab alone.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, assistant, errText, type AssistantCursor, type AssistantTurn } from "../api/client";
import AssistantBlocks, { type AnswerBlock, RichText } from "../components/AssistantBlocks";
import { plainText } from "../richtext";
import { canEdit } from "../role";

/** a question in flight (or one that failed); `thread` is the
 *  conversation it continues, null for a new one; `note` says why it
 *  is no longer the follow-up it was asked as */
interface Pending { key: number; q: string; thread: string | null; err?: string; note?: string }
/** the conversation that is open; `last_at` is its latest turn when it
 *  was opened, which tells a removed thread from one that has merely
 *  slid past the first page */
interface FollowUp { id: string; question: string; last_at: string }


// ---- answered questions this tab keeps itself (the same rules as the
// mobile screen's lib/answer-pure.ts). The household's log lives on the
// server, but an answer must never depend on it: a server older than the
// log has no history route, and a current one still returns the answer
// when it could not keep the turn. Such a turn is kept here, marked
// local, until a history read carries a row with its id.

/** a turn only this tab holds; `id` is set when the server kept it and
 *  the log read that will show it has not landed yet. `note` says why the
 *  answer is not the follow-up it was asked as (RESTARTED_NOTE,
 *  REMOVED_NOTE); once the log carries the id it moves to TurnNotes */
interface LocalTurn extends Omit<AssistantTurn, "id"> {
  key: string; id: string | null; local: true; note?: string;
}
/** `saved: false` or `removed: true` (with a null `thread_id`) is the
 *  server saying the answer was not kept as a conversation — it could
 *  not record the turn, or the thread went away while it was answering */
interface AskReply {
  answer: string; blocks?: AnswerBlock[]; tools_used?: string[]; id?: string | null;
  thread_id?: string | null; saved?: boolean; removed?: boolean;
}

/** the answer reached the asker but belongs to no conversation the
 *  server keeps: there is nothing to follow up on */
const replyNotKept = (reply: AskReply) => reply.saved === false || reply.removed === true;

/** a follow-up the server could not keep still belongs to the thread it
 *  was asked in (`thread`): the server says so by keeping `thread_id`,
 *  and the conversation is still there. An unkept answer has no id, so it
 *  offers no follow-up of its own. Only a thread removed mid-answer
 *  (`removed`) leaves it a conversation of one — and then it says why,
 *  whatever `note` it was asked with */
function localTurn(key: string, question: string, reply: AskReply, now: Date,
                   thread?: string | null, note?: string): LocalTurn {
  const turn: LocalTurn = {
    key, id: replyNotKept(reply) ? null : reply.id ?? null, local: true, question,
    answer: reply.answer, blocks: reply.blocks ?? [],
    tools_used: reply.tools_used ?? [],
    asked_at: now.toISOString(), asked_by: "",
    thread_id: reply.removed === true ? null
      : reply.thread_id ?? thread ?? reply.id ?? null,
  };
  const why = reply.removed === true ? REMOVED_NOTE : note;
  if (why) turn.note = why;
  return turn;
}

/** a log row may carry the note of the local turn it replaced */
type Turn = (AssistantTurn & { note?: string }) | LocalTurn;

// ---- conversations (mobile: groupThreads / threadKey in lib/answer-pure.ts).
// The log is a flat list of turns that name their thread; the page
// groups them: one card per conversation, its turns oldest first, the
// conversations newest first by their latest turn. A turn from a server
// that predates threads, or one the server never kept, is a conversation
// of one.

/** `id` is the thread the server knows (null for one it never kept —
 *  no follow-up is possible). `partial` means its opening question is not
 *  loaded: the log pages by turn, not by conversation, so a long-running
 *  thread can straddle a page boundary and its start sits on an older page. */
interface Thread { key: string; id: string | null; turns: Turn[]; last_at: string; partial: boolean }

const PARTIAL_NOTE =
  "continues a conversation that started earlier — “older conversations” loads its start";
/** the line it carries when there is no older page: the start is not in
 *  the log at all (an app that removed single answers took it) */
const START_GONE_NOTE = "the start of this conversation is no longer in the log";

/** the line above a conversation card: none when its start is loaded;
 *  PARTIAL_NOTE only while "older conversations" can still load it —
 *  promising a start no page holds would send the reader paging for
 *  nothing */
function partialNote(partial: boolean, olderToLoad: boolean): string | null {
  if (!partial) return null;
  return olderToLoad ? PARTIAL_NOTE : START_GONE_NOTE;
}

const threadKey = (t: Turn) => t.thread_id ?? ("local" in t ? t.id ?? t.key : t.id);

function groupThreads(turns: Turn[]): Thread[] {
  const byKey = new Map<string, Thread>();
  for (const t of turns) {
    const key = threadKey(t);
    let th = byKey.get(key);
    if (!th) {
      th = { key, id: t.thread_id ?? t.id ?? null, turns: [], last_at: t.asked_at,
             partial: false };
      byKey.set(key, th);
    }
    th.turns.push(t);
    if (t.asked_at > th.last_at) th.last_at = t.asked_at;
    if (th.id === null && (t.thread_id ?? t.id)) th.id = t.thread_id ?? t.id ?? null;
  }
  const out = [...byKey.values()];
  for (const th of out) {
    th.turns.sort((a, b) => a.asked_at < b.asked_at ? -1 : a.asked_at > b.asked_at ? 1 : 0);
    // a conversation's opening turn is the one whose id names the thread;
    // when the oldest loaded turn is a follow-up, the start is unloaded
    const first = th.turns[0];
    th.partial = !!first?.thread_id && first.id !== first.thread_id;
  }
  out.sort((a, b) => a.last_at < b.last_at ? 1 : a.last_at > b.last_at ? -1 : 0);
  return out;
}

/** notes the page keeps for log rows, by turn id: a local turn's note
 *  moves here once the log carries its id, and the local turn itself goes.
 *  The note is all it lends — a kept local turn would come back as a live
 *  conversation (with a follow-up) once its row left the loaded log */
type TurnNotes = Readonly<Record<string, string>>;

/** newest first: local turns the log does not carry yet on top, then the
 *  log's first page, then older pages (a row in both pages shows once).
 *  A log row takes the note of the local turn it replaced (from `notes`,
 *  or from that turn while the prune has not run yet) — the log knows
 *  nothing of why the question was re-asked, and the note must not vanish
 *  the moment the answer is read back. A note whose row is not in the log
 *  shows nowhere. */
function mergeTurns(local: LocalTurn[], first: AssistantTurn[],
                    older: AssistantTurn[], notes: TurnNotes = {}): Turn[] {
  const lent = new Map<string, string>(Object.entries(notes));
  for (const t of local) if (t.id !== null && t.note) lent.set(t.id, t.note);
  const noted = (t: AssistantTurn): Turn =>
    lent.has(t.id) ? { ...t, note: lent.get(t.id) } : t;
  const seen = new Set(first.map((t) => t.id));
  // a row read twice (two reads of one older page) shows once
  const rest: AssistantTurn[] = [];
  for (const t of older) if (!seen.has(t.id)) { seen.add(t.id); rest.push(t); }
  const mine = local.filter((t) => t.id === null || !seen.has(t.id));
  return [...[...mine].reverse(), ...first.map(noted), ...rest.map(noted)];
}

/** local turns the log has now caught up with, dropped (their notes go
 *  to keepNotes first). The same array when nothing goes, so the page
 *  can run this on every change without re-rendering for nothing */
function pruneLocal(local: LocalTurn[], kept: { id: string; asked_at: string }[],
                    more = true): LocalTurn[] {
  const ids = new Set(kept.map((t) => t.id));
  // A kept turn the log does not carry was removed elsewhere before the
  // first re-read — but only when the page is both FRESH enough (its newest
  // row is not older than the turn, so it was read after the ask) and SPANS
  // the turn's time (its oldest row is not newer, or there is no older
  // page). A stale page, or one filled by newer questions, says nothing,
  // and the turn waits for the next read.
  const newest = kept[0], oldest = kept[kept.length - 1];
  const gone = (t: LocalTurn) => t.id !== null && !ids.has(t.id) && !!newest
    && newest.asked_at >= t.asked_at && (!more || oldest.asked_at <= t.asked_at);
  const left = local.filter((t) => t.id === null || (!ids.has(t.id) && !gone(t)));
  return left.length === local.length ? local : left;
}

/** `notes` plus the note of every local turn the log now carries — taken
 *  before pruneLocal drops the turn. The same object when nothing is new */
function keepNotes(notes: TurnNotes, local: LocalTurn[],
                   kept: { id: string }[]): TurnNotes {
  const ids = new Set(kept.map((t) => t.id));
  const add = local.filter((t) => t.id !== null && t.note && ids.has(t.id)
                                  && notes[t.id] !== t.note);
  if (add.length === 0) return notes;
  const out: Record<string, string> = { ...notes };
  for (const t of add) out[t.id as string] = t.note as string;
  return out;
}

/** a 404 from the history read means the server keeps no shared log (it
 *  predates it) — not a failure to show; every other error is one */
function historyNotKept(e: unknown): boolean {
  if ((e as { status?: unknown } | null)?.status === 404) return true;
  return e instanceof Error && /^404:/.test(e.message);
}

// ---- a conversation that went away (mobile: conversationGone /
// followUpGone in lib/answer-pure.ts). Another member, or this person on
// another device, can remove the conversation that is open here. The
// server then answers the next question in it with a 404; the question
// is asked again as a new conversation, once, and the dead thread is
// closed — resending its id could only fail the same way.

const RESTARTED_NOTE =
  "the earlier conversation was removed, so this was asked as a new question";
/** the note an answer carries when its conversation was removed while it
 *  was being written: the server kept it nowhere, so it stands alone */
const REMOVED_NOTE =
  "the conversation was removed while this was answered, so this answer was not kept";

/** the ask call's 404: the conversation named no longer exists */
const conversationGone = (e: unknown) => historyNotKept(e);

/** after a log read, is the open conversation gone? Only when the
 *  read still covers the time of its latest turn (the page reaches back
 *  that far, or there is no more) and no loaded turn belongs to it; a
 *  thread that slid past the first page stays open — the ask's 404
 *  covers that case if it was removed too */
function followUpGone(f: FollowUp, turns: Turn[],
                      page: { turns: { asked_at: string }[]; more: boolean }): boolean {
  if (turns.some((t) => threadKey(t) === f.id)) return false;
  const oldest = page.turns[page.turns.length - 1];
  return !page.more || !oldest || oldest.asked_at <= f.last_at;
}

// ---- removing a conversation from a server that may predate the door
// (mobile: threadMissing / threadDoorMissing / turnMissing in
// lib/answer-pure.ts). A server older than conversations has no
// /assistant/threads/{id}; the framework answers it with a bare "Not
// Found" 404, which must not read as "someone removed it first". Only the
// server's own sentence for a missing conversation means that; any other
// 404 is the door missing, and on such a server a thread's id is its one
// turn's id, so the turn door removes it.

const THREAD_MISSING = "no such conversation";
const TURN_MISSING = "no such question";

/** a 404 that says the conversation is gone — the outcome a remove wants */
const threadMissing = (e: unknown) => historyNotKept(e) && errText(e) === THREAD_MISSING;
/** a 404 from the single-turn door that says the turn is gone */
const turnMissing = (e: unknown) => historyNotKept(e) && errText(e) === TURN_MISSING;
/** a 404 from the threads door that is not its own: the server predates
 *  it, so the remove falls back to the single-turn door */
const threadDoorMissing = (e: unknown) => historyNotKept(e) && !threadMissing(e);

// ---- paging the shared log (mobile: nextCursor / pagingCursor in
// lib/answer-pure.ts). The log pages by (asked_at, id): several rows can
// share one asked_at, so the time alone skips the rest of them at a page
// boundary. The server hands back the next page's cursor as `next`.

interface HistoryPage { turns: AssistantTurn[]; more: boolean; next?: AssistantCursor | null }

/** the cursor for the page after `page`, or null when there is none; a
 *  server that predates `next` gets the last row's time and id */
function nextCursor(page: HistoryPage | null | undefined): AssistantCursor | null {
  if (!page || !page.more) return null;
  if (page.next && page.next.before) return page.next;
  const last = page.turns[page.turns.length - 1];
  return last ? { before: last.asked_at, before_id: last.id } : null;
}

// ---- who may delete from the log (mobile: mayTidyLog / sharedLogDenied).
// The shared demonstration login is every visitor at once, so the server
// refuses it both delete doors with a 403 naming the shared login; every
// other editor keeps them.

function sharedLogDenied(e: unknown): boolean {
  const status = (e as { status?: unknown } | null)?.status;
  const msg = e instanceof Error ? e.message : "";
  return (status === 403 || /^403:/.test(msg))
    && /shared demonstration login/.test(msg);
}

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
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const yy = String(d.getFullYear()).slice(2);
  const h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0");
  return `${mm}/${dd}/${yy} ${((h + 11) % 12) + 1}:${m} ${h < 12 ? "am" : "pm"}`;
};

/** `?c=`: nothing = the list (on a phone) or a fresh conversation (on a
 *  wide screen, beside the list); `new` = a fresh conversation; else the
 *  key of the open one */
const NEW = "new";

export default function Assistant() {
  const qc = useQueryClient();
  const [sp, setSp] = useSearchParams();
  const openKey = sp.get("c");
  const status = useQuery({ queryKey: ["assistant-status"], queryFn: assistant.status });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // `denied` is the server refusing the shared login all the same
  const [denied, setDenied] = useState<string | null>(null);
  const mayEdit = canEdit(me) && !me.data?.shared_login && !denied;
  const history = useQuery({ queryKey: ["assistant-history"],
                             queryFn: () => assistant.history(),
                             enabled: !!status.data?.available });
  const [q, setQ] = useState("");
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
  const [err, setErr] = useState<string | null>(null);
  const msgsRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  // the open conversation's latest turn as last seen, by key: a thread
  // that slid past the first page (other members asking) is told from a
  // removed one by whether the page still reaches back to it
  const openLast = useRef<{ key: string; last_at: string } | null>(null);
  // what is open right now, for the ask's landing: an answer must never
  // switch the view away from a conversation the person moved to
  const openKeyRef = useRef<string | null>(null);
  openKeyRef.current = openKey;
  // the conversations on screen, for the same check: a key no loaded
  // conversation answers to (a cold link to one on an older page) is
  // not a view a landing answer must respect
  const threadsRef = useRef<Set<string>>(new Set());

  const show = (key: string | null) => {
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (key === null) next.delete("c"); else next.set("c", key);
      return next;
    });
  };

  // a 404 is a server older than the shared log: this tab's own turns
  // are all there is, and that is not an error
  const noLog = history.isError && historyNotKept(history.error);
  // once the log carries a turn this tab kept, the log's row is the one;
  // the local turn hands over only its note, so it cannot come back as a
  // live conversation when its row later leaves the log
  const page = history.data;
  useEffect(() => {
    if (!page) return;
    setNotes((n) => keepNotes(n, mine, page.turns));
    setMine((m) => pruneLocal(m, page.turns, page.more));
  }, [page, mine]);

  const turns = mergeTurns(mine, history.data?.turns ?? [], older, notes);
  const threads = groupThreads(turns);
  const open = openKey && openKey !== NEW ? threads.find((th) => th.key === openKey) ?? null : null;
  if (open) openLast.current = { key: open.key, last_at: open.last_at };
  threadsRef.current = new Set(threads.map((th) => th.key));
  // a conversation removed elsewhere drops out of the log: the one open
  // here closes too, so the next question is not sent to a dead thread.
  // One that merely slid past the first page stays open (its latest turn
  // is older than the page reaches); a cold link to one never seen
  // closes only when there is no older page it could be on — the ask's
  // 404 covers a removed thread either way
  const olderToLoad = !noMore && nextCursor(lastOlder ?? history.data) !== null;
  useEffect(() => {
    if (!page || !openKey || openKey === NEW || open) return;
    const seen = openLast.current?.key === openKey ? openLast.current.last_at : null;
    // every older page read and still no such conversation: it is gone
    const gone = !olderToLoad || (seen !== null
      && followUpGone({ id: openKey, question: "", last_at: seen }, turns, page));
    if (gone) show(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, openKey, open, olderToLoad]);
  useEffect(() => { setPageFailed(false); }, [openKey]);
  // a conversation the server no longer has: its rows leave the page and
  // it closes if it was the open one
  const dropThread = (threadId: string) => {
    setOlder((o) => o.filter((t) => (t.thread_id ?? t.id) !== threadId));
    setMine((m) => m.filter((t) => t.thread_id !== threadId));
    if (openKey === threadId) show(null);
  };

  const ask = useMutation({
    // a follow-up to a conversation removed elsewhere answers 404: the
    // question is asked once more as a new conversation, and the card
    // says so — retrying the dead thread could only fail the same way
    mutationFn: async ({ key, q: question, thread }: Pending) => {
      try {
        return { reply: await assistant.ask(question, thread), thread,
                 note: undefined as string | undefined };
      } catch (e) {
        if (!thread || !conversationGone(e)) throw e;
        dropThread(thread);
        setPending((p) => p.map((t) => t.key === key
          ? { ...t, thread: null, note: RESTARTED_NOTE } : t));
        return { reply: await assistant.ask(question, null), thread: null,
                 note: RESTARTED_NOTE };
      }
    },
    onMutate: (p) => { setPending((x) => [...x, p]); setQ(""); },
    // the reply itself is shown at once and the log re-read: the log may
    // not have it (an older server, or a turn it could not keep), and an
    // answer must never vanish because the log is missing it
    // the restart note rides on the answer's card, not the pending one,
    // so it is still there once the answer lands
    onSuccess: ({ reply, thread, note }, { key, q: question }) => {
      setPending((p) => p.filter((t) => t.key !== key));
      const turn = localTurn(`local-${key}`, question, reply, new Date(), thread, note);
      setMine((m) => [...m, turn]);
      // the thread went away while the answer was being written
      if (reply.removed && thread) dropThread(thread);
      // a fresh conversation becomes the open one as soon as it exists —
      // unless the person has since moved to another conversation, whose
      // draft must not be sent to the new one
      if (!thread && (openKeyRef.current === null || openKeyRef.current === NEW
                      || !threadsRef.current.has(openKeyRef.current))) show(threadKey(turn));
      void qc.invalidateQueries({ queryKey: ["assistant-history"] });
    },
    onError: (e, { key }) => setPending((p) =>
      p.map((t) => t.key === key ? { ...t, err: errText(e) } : t)),
  });
  // the box continues the open conversation, or opens a new one when
  // none is (or the open one was never kept); the chips always open one
  const submit = (question: string, thread: string | null = open?.id ?? null) => {
    const v = question.trim();
    if (v && !ask.isPending) ask.mutate({ key: Date.now(), q: v, thread });
  };

  const report = useMutation({
    // an answer the log kept empty still has a line in the page, so a
    // turn never vanishes from the file without a trace
    mutationFn: (th: Thread) =>
      assistant.report(th.turns.map((t) => ({
        question: t.question,
        blocks: t.blocks.length ? t.blocks
          : [{ kind: "text", text: t.answer || "(no answer was kept)" }] as AnswerBlock[],
      })), th.partial),
    onMutate: () => setErr(null),
    onError: (e) => setErr(errText(e)),
  });
  const forget = useMutation({
    // a server that predates conversations answers the threads door with
    // a bare 404; there the conversation is one turn under the same id,
    // removed by the turn door (whose own 404 means it is already gone)
    mutationFn: async (threadId: string) => {
      try {
        return await assistant.forgetThread(threadId);
      } catch (e) {
        if (!threadDoorMissing(e)) throw e;
        try {
          return await assistant.forgetTurn(threadId);
        } catch (e2) {
          if (turnMissing(e2)) return { ok: true, deleted: 0 };
          throw e2;
        }
      }
    },
    onMutate: () => setErr(null),
    onSuccess: (_r, threadId) => {
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
      else setErr(errText(e));
    },
  });
  const clear = useMutation({
    mutationFn: () => assistant.clear(),
    onMutate: () => setErr(null),
    onSuccess: () => { setOlder([]); setMine([]); setNotes({}); setLastOlder(null); setNoMore(true);
                       show(null);
                       void qc.invalidateQueries({ queryKey: ["assistant-history"] }); },
    onError: (e) => sharedLogDenied(e) ? setDenied(errText(e)) : setErr(errText(e)),
  });
  const more = useMutation({
    mutationFn: (cursor: AssistantCursor) => assistant.history(cursor),
    onSuccess: (r) => { setOlder((o) => [...o, ...r.turns]); setLastOlder(r);
                        if (!r.more) setNoMore(true); },
    onError: (e) => { setPageFailed(true); setErr(errText(e)); },
    onSettled: () => { paging.current = false; },
  });

  // a cold link to a conversation that is not on the first page: page
  // back until it is found or there is no older page (then the close
  // effect above lets it go)
  const unresolved = !!openKey && openKey !== NEW && !open && !!page;
  const pageCursor = noMore ? null : nextCursor(lastOlder ?? history.data);
  // keyed by the cursor's VALUE, with one request in flight at a time: a
  // server that hands no `next` makes a fresh cursor object every render
  const pageKey = pageCursor ? `${pageCursor.before}|${pageCursor.before_id ?? ""}` : null;
  useEffect(() => {
    if (!unresolved || !pageCursor || pageFailed || paging.current) return;
    paging.current = true;
    more.mutate(pageCursor);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unresolved, pageKey, pageFailed]);

  // the newest message is the one to read: the open conversation scrolls
  // to its end when a turn lands or a question goes out
  const openTurnCount = open?.turns.length ?? 0;
  const pendingCount = pending.length;
  useEffect(() => {
    const el = msgsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [openKey, openTurnCount, pendingCount]);

  if (status.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (!status.data?.available) {
    return (
      <>
        <h1>Assistant</h1>
        <div className="note">
          The assistant needs a language model backend and none is
          configured. It appears as soon as one is: the bundled model on a
          standard self-hosted install, or an endpoint you point it
          at. <Link to="/settings/categorize">Set one up in Settings ›</Link>
        </div>
      </>
    );
  }

  const rowKey = (t: Turn) => "local" in t ? t.key : t.id;
  // a conversation the server never kept has nothing to delete there
  const remove = (th: Thread) => {
    if (th.id === null) setMine((m) => m.filter((x) => !th.turns.includes(x)));
    else forget.mutate(th.id);
  };
  // a question in flight draws inside its conversation; a new one in the
  // fresh pane
  const pendingFor = (th: Thread) => pending.filter((p) => p.thread === th.id && th.id !== null);
  const pendingNew = pending.filter((p) => p.thread === null
    || !threads.some((th) => th.id === p.thread));
  const pendingRows = (rows: Pending[]) => rows.map((t) => (
    <div key={t.key} className="chat-turn">
      <div className="chat-msg me"><div className="b">{t.q}</div></div>
      <div className="chat-msg ai"><div className="b">
        {t.note && <div className="sub" style={{ fontSize: 12, marginBottom: ".3rem" }}>{t.note}</div>}
        {t.err
          ? <>
              <div className="note bad">{t.err}</div>
              <div style={{ marginTop: ".5rem", display: "flex", gap: ".5rem" }}>
                <button style={{ fontSize: 12 }} onClick={() => submit(t.q, t.thread)}>try again</button>
                <button style={{ fontSize: 12 }}
                        onClick={() => setPending((p) => p.filter((x) => x.key !== t.key))}>
                  dismiss</button>
              </div>
            </>
          : <span className="chat-typing" aria-label="thinking"><i /><i /><i /></span>}
      </div></div>
    </div>
  ));
  // paging walks the log by (asked_at, id), from the cursor the last page
  // read handed back; a page with no more ends it
  const cursor = noMore ? null : nextCursor(lastOlder ?? history.data);
  const title = (th: Thread) => th.turns[0]?.question ?? "";
  const whereLine = status.data.local
    ? "answered by the model on this server — read-only tools, nothing leaves the box"
    : `read-only tools — your questions and the figures they return go to ${
        status.data.host || "the model host this instance is configured with"}`;
  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(q); }
  };
  const composer = (placeholder: string) => (
    <div className="chat-compose">
      <div className="box">
        <textarea ref={boxRef} rows={1} value={q} placeholder={placeholder}
                  onChange={(e) => { setQ(e.target.value);
                                     e.target.style.height = "auto";
                                     e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`; }}
                  onKeyDown={onKey} />
        <button className="pri" disabled={ask.isPending || !q.trim()} onClick={() => submit(q)}>
          {ask.isPending ? "thinking…" : "Send"}</button>
      </div>
      <div className="hint"><span>Enter to send · Shift+Enter for a new line</span><span>{whereLine}</span></div>
    </div>
  );
  const mode = openKey === NEW ? "new" : open ? "open" : "list";

  return (
    <>
      <h1>Assistant</h1>
      {err && <div className="note bad" style={{ marginBottom: ".6rem" }}>{err}</div>}
      <div className={`chat chat-${mode}`}>
        <aside className="chat-rail">
          <div className="head">
            <button className="pri" style={{ flex: 1 }} onClick={() => { show(NEW); boxRef.current?.focus(); }}>
              + New conversation</button>
          </div>
          <div className="list">
            {threads.map((th) => (
              <div key={th.key} className={`row${th.key === openKey ? " on" : ""}`}
                   role="button" tabIndex={0}
                   onClick={() => show(th.key)}
                   onKeyDown={(e) => e.key === "Enter" && show(th.key)}>
                <div className="q">{th.partial ? "… " : ""}{title(th)}</div>
                <div className="m">{when(th.last_at)} · {th.turns.length}{" "}
                  {th.turns.length === 1 ? "question" : "questions"}
                  {th.turns[0]?.asked_by ? ` · ${th.turns[0].asked_by}` : ""}</div>
              </div>
            ))}
            {history.isPending && <p className="mut" style={{ padding: ".6rem .75rem" }}>loading history…</p>}
            {history.isError && !noLog && (
              <div className="note bad" style={{ margin: ".5rem" }}>
                Couldn&apos;t load the history: {errText(history.error)}{" "}
                <button style={{ fontSize: 12 }} onClick={() => void history.refetch()}>
                  try again</button>
              </div>
            )}
            {!history.isPending && (!history.isError || noLog) && threads.length === 0 && (
              <p className="mut" style={{ padding: ".6rem .75rem", fontSize: 13 }}>
                {noLog ? "Nothing asked yet."
                  : "Nothing asked yet. Every conversation is kept here for the household."}
              </p>
            )}
            {cursor && (
              <button style={{ fontSize: 12.5, margin: ".5rem .75rem" }} disabled={more.isPending}
                      onClick={() => more.mutate(cursor)}>
                {more.isPending ? "loading…" : "older conversations"}</button>
            )}
          </div>
          <div className="foot">
            <span>{noLog ? "this session only" : "kept for the household"}</span>
            {mayEdit && !noLog && threads.length > 0 && (
              <button style={{ fontSize: 12 }} disabled={clear.isPending}
                      onClick={() => { if (window.confirm("Clear every conversation?")) clear.mutate(); }}>
                clear all</button>
            )}
          </div>
          {denied && <p className="mut" style={{ fontSize: 12, margin: ".2rem .75rem .5rem" }}>{denied}</p>}
        </aside>

        {open ? (
          <section className="chat-pane">
            <div className="head">
              <button className="back" style={{ fontSize: 12 }} onClick={() => show(null)}>‹ Conversations</button>
              <div className="t" title={title(open)}>{title(open)}</div>
              {status.data.local && <span className="pill g">local</span>}
              {/* the whole conversation — every answer, charts included,
                  every figure also as a table — as one self-contained
                  HTML file to keep or send */}
              <button style={{ fontSize: 12 }} disabled={report.isPending}
                      onClick={() => report.mutate(open)}>
                {report.isPending ? "saving…" : "save as report"}</button>
              {mayEdit && (
                <button style={{ fontSize: 12 }} disabled={forget.isPending}
                        onClick={() => remove(open)}>remove</button>
              )}
            </div>
            <div className="chat-msgs" ref={msgsRef}>
              {/* the log pages by turn, so a conversation can straddle a page:
                  its first loaded question is then not its first — and when
                  no older page is left, its start is gone */}
              {partialNote(open.partial, cursor !== null) && (
                <div className="sub" style={{ fontSize: 12, textAlign: "center" }}>
                  {partialNote(open.partial, cursor !== null)}</div>
              )}
              {open.turns.map((t) => (
                <div key={rowKey(t)} className="chat-turn">
                  <div className="chat-msg me">
                    <div className="b">{t.question}</div>
                    <div className="meta">{when(t.asked_at)}{t.asked_by ? ` · ${t.asked_by}` : ""}</div>
                  </div>
                  <div className="chat-msg ai">
                    <div className="b">
                      {/* why this answer is not the follow-up it was asked as */}
                      {t.note && <div className="sub" style={{ fontSize: 12, marginBottom: ".3rem" }}>{t.note}</div>}
                      {/* answers carry typed blocks — prose, charts, tables — drawn
                          in order; a turn kept by an older server has the prose alone */}
                      {t.blocks.length
                        ? <AssistantBlocks blocks={t.blocks} />
                        : <RichText text={t.answer} />}
                      <div className="acts">
                        <button style={{ fontSize: 12 }}
                                onClick={() => void navigator.clipboard?.writeText(plainText(t.answer))}>
                          copy</button>
                        {/* which read-only tools fed the answer, as on mobile;
                            provenance beats a bare paragraph */}
                        {t.tools_used.length > 0 && (
                          <span className="from">from {t.tools_used.join(" · ")}</span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
              {pendingRows(pendingFor(open))}
            </div>
            {/* the box continues THIS conversation; one the server never
                kept cannot be continued, and its box opens a new one */}
            {composer(open.id !== null ? "Continue the conversation…" : "Ask a new question…")}
          </section>
        ) : (
          <section className="chat-pane">
            <div className="head">
              <button className="back" style={{ fontSize: 12 }} onClick={() => show(null)}>‹ Conversations</button>
              <div className="t">New conversation</div>
              {status.data.local && <span className="pill g">local</span>}
            </div>
            <div className="chat-msgs" ref={msgsRef}>
              {unresolved && cursor && !pageFailed ? (
                <div className="chat-empty"><div className="mut">loading the conversation…</div></div>
              ) : pendingNew.length === 0 ? (
                <div className="chat-empty">
                  <h2>Ask about your money</h2>
                  <div className="mut">Plain questions, answered from your own ledger.
                    {!noLog && " Every conversation is kept for the household."}</div>
                  <div className="chips">
                    {EXAMPLES.map((ex) => (
                      <button key={ex} onClick={() => submit(ex, null)} disabled={ask.isPending}>{ex}</button>
                    ))}
                  </div>
                </div>
              ) : pendingRows(pendingNew)}
            </div>
            {composer("Ask anything…")}
          </section>
        )}
      </div>
    </>
  );
}
