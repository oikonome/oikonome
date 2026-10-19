// The assistant answer's charts, the arithmetic half: block types, the
// palette, number formats and the geometry (bar slots, line points, ring
// arcs) — no React, no RN imports, so plain node unit-tests it
// (scripts/pure-logic-tests.mjs). The web twin draws the same shapes
// from the same rules (webapp/src/components/AssistantBlocks.tsx).

import { money } from "./pure.ts";

export interface SeriesSpec { name: string; values: number[] }
export type AnswerBlock =
  | { kind: "text"; text: string }
  | { kind: "bar" | "line" | "pie"; title: string; labels: string[];
      series: SeriesSpec[] }
  | { kind: "table"; title: string; columns: string[];
      rows: (string | number)[][] };

// the server-rendered charts' categorical order (CVD-safe); a series
// keeps its colour by position, never by rank
export const PALETTE = ["#4f9bd6", "#e8833a", "#3a9188", "#8a5fb0",
                        "#4f9d5b", "#d9a441", "#c65c4e", "#7a8a99"];

/** axis / bar-top money: $1.2k, $340k, $1.25M, -$45 */
export function shortMoney(v: number): string {
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1_000_000)
    return `${sign}$${(a / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (a >= 1000) return `${sign}$${(a / 1000).toFixed(a % 1000 ? 1 : 0)}k`;
  return `${sign}$${Math.round(a)}`;
}

/** a table number: a count, a share, a dollar figure — the column name
 *  carries the unit, so 52 visits never read as $52 */
export function plainNumber(v: number): string {
  return Number.isInteger(v)
    ? v.toLocaleString("en-US")
    : v.toLocaleString("en-US", { minimumFractionDigits: 2,
                                  maximumFractionDigits: 2 });
}

export const clip = (s: string, n: number) =>
  s.length <= n ? s : s.slice(0, n - 1) + "…";

/** the y range every chart shares: zero is always in it */
export function yRange(values: number[]) {
  const ymin = Math.min(0, ...values), ymax = Math.max(0, ...values);
  return { ymin, ymax, span: ymax - ymin || 1 };
}

export interface BarLayout {
  bars: { x: number; y: number; w: number; h: number; si: number; li: number;
          label: string | null }[];
  ticks: { y: number; text: string }[];
  zeroY: number;
  xLabels: { x: number; text: string }[];
}

/** grouped vertical bars in a W×H box; a lone series with few bars
 *  writes its value on each one, the x axis thins past a dozen labels */
export function barLayout(labels: string[], series: SeriesSpec[],
                          W: number, H: number): BarLayout {
  const padL = 46, padR = 6, padT = 14, padB = 22;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length, k = series.length;
  const { ymin, ymax, span } = yRange(series.flatMap((s) => s.values));
  const Y = (v: number) => padT + ih - (ih * (v - ymin)) / span;
  const group = iw / n, gap = Math.min(6, group * 0.15);
  const bw = Math.max(2, (group - gap) / k - (k > 1 ? 1.5 : 0));
  const zeroY = Y(0);
  const bars: BarLayout["bars"] = [];
  labels.forEach((_, i) => {
    const gx = padL + group * i + gap / 2;
    series.forEach((s, j) => {
      const v = s.values[i];
      const x = gx + j * (bw + (k > 1 ? 1.5 : 0));
      const top = v >= 0 ? Y(v) : zeroY, bot = v >= 0 ? zeroY : Y(v);
      bars.push({ x, y: top, w: bw, h: Math.max(0, bot - top), si: j, li: i,
                  label: k === 1 && n <= 12 ? shortMoney(v) : null });
    });
  });
  const every = n <= 12 ? 1 : Math.max(1, Math.ceil(n / 6));
  return {
    bars, zeroY,
    ticks: [ymax, (ymax + ymin) / 2, ymin].map((v) => ({ y: Y(v), text: shortMoney(v) })),
    xLabels: labels.flatMap((l, i) => i % every === 0
      ? [{ x: padL + group * i + group / 2, text: clip(l, 7) }] : []),
  };
}

export interface LineLayout {
  lines: { si: number; points: string }[];
  ticks: { y: number; text: string }[];
  zeroY: number | null;
  xLabels: { x: number; text: string; anchor: "start" | "middle" | "end" }[];
}

export function lineLayout(labels: string[], series: SeriesSpec[],
                           W: number, H: number): LineLayout {
  const padL = 46, padR = 6, padT = 14, padB = 22;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length;
  const { ymin, ymax, span } = yRange(series.flatMap((s) => s.values));
  const X = (i: number) => padL + (iw * i) / Math.max(1, n - 1);
  const Y = (v: number) => padT + ih - (ih * (v - ymin)) / span;
  const every = Math.max(1, Math.ceil(n / 5));
  return {
    lines: series.map((s, j) => ({
      si: j,
      points: s.values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" "),
    })),
    ticks: [ymax, (ymax + ymin) / 2, ymin].map((v) => ({ y: Y(v), text: shortMoney(v) })),
    zeroY: ymin < 0 ? Y(0) : null,
    xLabels: labels.flatMap((l, i) => i % every === 0 || i === n - 1
      ? [{ x: X(i), text: clip(l, 7),
           anchor: i === 0 ? "start" : i === n - 1 ? "end" : "middle" }] : []),
  };
}

export interface RingSeg { label: string; value: number; share: number;
                           dash: number; offset: number; si: number }

/** parts of a whole as ring arcs: non-positive parts are not drawn, the
 *  share is of what is */
export function ringLayout(labels: string[], values: number[], r: number) {
  const segs = labels.map((l, i) => ({ l, v: values[i], i }))
    .filter((s) => s.v > 0);
  const total = segs.reduce((a, s) => a + s.v, 0);
  const circ = 2 * Math.PI * r;
  let off = 0;
  const out: RingSeg[] = segs.map((s, j) => {
    const dash = total ? (s.v / total) * circ : 0;
    const seg = { label: s.l, value: s.v, share: total ? s.v / total : 0,
                  dash, offset: off, si: j };
    off += dash;
    return seg;
  });
  return { segs: out, total, circ };
}

/** the figures behind a chart as table rows, money-formatted */
export function chartRows(labels: string[], series: SeriesSpec[]) {
  return {
    columns: ["", ...series.map((s) => s.name)],
    rows: labels.map((l, i) => [l, ...series.map((s) => money(s.values[i]))]),
  };
}

/** a table cell: numbers plain, text as is */
export const cellText = (c: string | number) =>
  typeof c === "number" ? plainNumber(c) : c;

/** what the "save as report" call sends: the blocks the server gave,
 *  or — on a server that sent prose alone — that prose as one block */
export function reportBlocks(answer: string, blocks?: AnswerBlock[]): AnswerBlock[] {
  return blocks && blocks.length ? blocks : [{ kind: "text", text: answer }];
}

// ---- answered questions this screen keeps itself ----
// The household's log lives on the server, but an answer must never
// depend on it: a server older than the log has no history route, and a
// current one still returns the answer when it could not keep the turn
// (the reply reaches the asker either way). Such a turn is kept here,
// marked local, until a history read carries a row with its id.

/** the fields every answered turn has, kept or not. `thread_id` names
 *  the conversation the turn belongs to (absent on a server that
 *  predates threads: every such turn is a conversation of one) */
export interface TurnBase {
  asked_at: string; asked_by: string; question: string;
  answer: string; blocks: AnswerBlock[]; tools_used: string[];
  thread_id?: string | null;
}
/** a turn only this screen holds; `id` is set when the server kept it
 *  and the log read that will show it has not landed yet. `note` says
 *  why the answer is not the follow-up it was asked as (RESTARTED_NOTE,
 *  REMOVED_NOTE); once the log carries the id it moves to TurnNotes */
export interface LocalTurn extends TurnBase {
  key: string; id: string | null; local: true; note?: string;
}

/** what the ask call answers with, as far as this needs it. `saved:
 *  false` or `removed: true` (with a null `thread_id`) is the server
 *  saying the answer was not kept as a conversation — it could not
 *  record the turn, or the thread went away while it was answering */
export interface AskReply {
  answer: string; blocks?: AnswerBlock[]; tools_used?: string[];
  id?: string | null; thread_id?: string | null;
  saved?: boolean; removed?: boolean;
}

/** the answer reached the asker but belongs to no conversation the
 *  server keeps: there is nothing to follow up on */
export function replyNotKept(reply: AskReply): boolean {
  return reply.saved === false || reply.removed === true;
}

/** the turn to show for an answered question, before (or instead of)
 *  the shared log showing it. A follow-up the server could not keep
 *  still belongs to the thread it was asked in (`thread`): the server
 *  says so by keeping `thread_id`, and the conversation is still there.
 *  An unkept answer has no id, so it offers no follow-up of its own.
 *  Only a thread removed mid-answer (`removed`) leaves it a conversation
 *  of one — and then it says why, whatever `note` it was asked with. */
export function localTurn(key: string, question: string, reply: AskReply,
                          now: Date, thread?: string | null, note?: string): LocalTurn {
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

// ---- conversations ----
// Every question opens a conversation and a follow-up continues it; the
// log is a flat list of turns that name their thread, so the screen
// groups them: one card per conversation, its turns oldest first, the
// conversations newest first by their latest turn. A turn from a server
// that predates threads, or one the server never kept, is a conversation
// of one.

/** one conversation as the screen draws it; `id` is the thread the
 *  server knows (null for one it never kept — no follow-up is possible).
 *  `partial` means its opening question is not loaded: the log pages by
 *  turn, not by conversation, so a long-running thread can straddle a
 *  page boundary and its start sits on an older page. */
export interface Thread<T> {
  key: string; id: string | null; turns: T[]; last_at: string; partial: boolean;
}

/** the line a conversation whose start is on an older page carries */
export const PARTIAL_NOTE =
  "continues a conversation that started earlier — “older conversations” loads its start";
/** the line it carries when there is no older page: the start is not in
 *  the log at all (an app that removed single answers took it) */
export const START_GONE_NOTE = "the start of this conversation is no longer in the log";

/** the line above a conversation card: none when its start is loaded;
 *  PARTIAL_NOTE only while "older conversations" can still load it —
 *  promising a start no page holds would send the reader paging for
 *  nothing */
export function partialNote(partial: boolean, olderToLoad: boolean): string | null {
  if (!partial) return null;
  return olderToLoad ? PARTIAL_NOTE : START_GONE_NOTE;
}

/** the thread a turn belongs to, as a key the screen can group on */
export function threadKey(t: TurnBase & { id?: string | null; key?: string }): string {
  return t.thread_id ?? t.id ?? t.key ?? "";
}

/** the conversations, newest first, each with its turns oldest first */
export function groupThreads<T extends TurnBase & { id?: string | null; key?: string }>(
  turns: T[]): Thread<T>[] {
  const byKey = new Map<string, Thread<T>>();
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

// ---- a conversation that went away ----
// Another member (or this person on another device) can remove the
// conversation the asker is following up on. The server then answers the
// follow-up with a 404; the question is asked again as a new
// conversation, once, and the chip naming the dead thread is cleared —
// resending its id could only fail the same way.

/** the conversation the asker continues; `last_at` is its latest turn
 *  when the chip was set, which tells a removed thread from one that has
 *  merely slid past the first page */
export interface FollowUp { id: string; question: string; last_at: string }

/** the note a question re-asked as a new conversation carries */
export const RESTARTED_NOTE =
  "the earlier conversation was removed, so this was asked as a new question";
/** the note an answer carries when its conversation was removed while it
 *  was being written: the server kept it nowhere, so it stands alone */
export const REMOVED_NOTE =
  "the conversation was removed while this was answered, so this answer was not kept";

/** the ask call's 404: the conversation named no longer exists. Both
 *  clients' errors carry the status, as a field or as the message's
 *  "404: …" lead. */
export function conversationGone(e: unknown): boolean {
  return historyNotKept(e);
}

/** after a log read, is the followed conversation gone? Only when the
 *  read still covers the time of its latest turn (the page reaches back
 *  that far, or there is no more) and no loaded turn belongs to it; a
 *  thread that slid past the first page keeps its chip — the ask's 404
 *  covers that case if it was removed too */
export function followUpGone(f: FollowUp,
                             turns: (TurnBase & { id?: string | null; key?: string })[],
                             page: { turns: { asked_at: string }[]; more: boolean }): boolean {
  if (turns.some((t) => threadKey(t) === f.id)) return false;
  const oldest = page.turns[page.turns.length - 1];
  return !page.more || !oldest || oldest.asked_at <= f.last_at;
}

/** notes the screen keeps for log rows, by turn id: a local turn's note
 *  moves here once the log carries its id, and the local turn itself goes.
 *  The note is all it lends — a kept local turn would come back as a live
 *  conversation (with a follow-up) once its row left the loaded log */
export type TurnNotes = Readonly<Record<string, string>>;

/** the list the screen draws, newest first: local turns the log does not
 *  carry yet sit on top, then the log's first page, then older pages
 *  (a row in both pages shows once). A log row takes the note of the
 *  local turn it replaced (from `notes`, or from that turn while the prune
 *  has not run yet) — the log knows nothing of why the question was
 *  re-asked, and the note must not vanish the moment the answer is read
 *  back. A note whose row is not in the log shows nowhere. */
export function mergeTurns<T extends TurnBase & { id: string }>(
  local: LocalTurn[], first: T[], older: T[],
  notes: TurnNotes = {}): ((T & { note?: string }) | LocalTurn)[] {
  const lent = new Map<string, string>(Object.entries(notes));
  for (const t of local) if (t.id !== null && t.note) lent.set(t.id, t.note);
  const noted = (t: T): T & { note?: string } =>
    lent.has(t.id) ? { ...t, note: lent.get(t.id) } : t;
  const seen = new Set(first.map((t) => t.id));
  // a row read twice (two reads of one older page) shows once
  const rest: T[] = [];
  for (const t of older) if (!seen.has(t.id)) { seen.add(t.id); rest.push(t); }
  const mine = local.filter((t) => t.id === null || !seen.has(t.id));
  return [...[...mine].reverse(), ...first.map(noted), ...rest.map(noted)];
}

/** local turns the log has now caught up with, dropped (their notes go
 *  to keepNotes first). The same array when nothing goes, so the screen
 *  can run this on every change without re-rendering for nothing */
export function pruneLocal(local: LocalTurn[], kept: { id: string; asked_at: string }[],
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
export function keepNotes(notes: TurnNotes, local: LocalTurn[],
                          kept: { id: string }[]): TurnNotes {
  const ids = new Set(kept.map((t) => t.id));
  const add = local.filter((t) => t.id !== null && t.note && ids.has(t.id)
                                  && notes[t.id] !== t.note);
  if (add.length === 0) return notes;
  const out: Record<string, string> = { ...notes };
  for (const t of add) out[t.id as string] = t.note as string;
  return out;
}

// ---- removing a conversation from a server that may predate the door ----
// A server older than conversations has no /assistant/threads/{id}; the
// framework answers it with a bare "Not Found" 404, which must not read
// as "someone removed it first". Only the server's own sentence for a
// missing conversation means that; any other 404 is the door missing,
// and on such a server a thread's id is its one turn's id, so the turn
// door removes it.

/** the detail the threads door answers when the household has no such
 *  conversation (and the ask call, for a follow-up to one) */
export const THREAD_MISSING = "no such conversation";
/** the detail the single-turn door answers when there is no such turn */
export const TURN_MISSING = "no such question";

/** the sentence a refusal carries: mobile's error has it as `detail`, the
 *  web's as the JSON body after the message's "404: " lead */
export function errorDetail(e: unknown): string {
  const d = (e as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  const msg = e instanceof Error ? e.message : "";
  const body = msg.replace(/^\d{3}:\s*/, "").trim();
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch { /* not JSON: the body is the sentence */ }
  return body;
}

/** a 404 that says the conversation is gone — the outcome a remove wants */
export function threadMissing(e: unknown): boolean {
  return historyNotKept(e) && errorDetail(e) === THREAD_MISSING;
}

/** a 404 from the single-turn door that says the turn is gone */
export function turnMissing(e: unknown): boolean {
  return historyNotKept(e) && errorDetail(e) === TURN_MISSING;
}

/** a 404 from the threads door that is not its own: the server predates
 *  it, so the remove falls back to the single-turn door */
export function threadDoorMissing(e: unknown): boolean {
  return historyNotKept(e) && !threadMissing(e);
}

/** a 404 from the history read means the server keeps no shared log (it
 *  predates it) — not a failure to show; every other error is one. Both
 *  clients' errors carry the status, as a field or as the message's
 *  "404: …" lead. */
export function historyNotKept(e: unknown): boolean {
  if ((e as { status?: unknown } | null)?.status === 404) return true;
  const msg = e instanceof Error ? e.message : "";
  return /^404:/.test(msg);
}

// ---- paging the shared log ----
// The log pages by (asked_at, id): several rows can share one asked_at,
// so a cursor of the time alone skips the rest of them at a page
// boundary. The server hands back the cursor for the next page as
// `next`; "older questions" sends both halves of it.

/** where the next older page starts */
export interface HistoryCursor { before: string; before_id?: string | null }
/** one history read, as far as paging needs it */
export interface HistoryPage {
  turns: { id: string; asked_at: string }[];
  more: boolean; next?: HistoryCursor | null;
}

/** the cursor for the page after `page`, or null when there is none. A
 *  server that predates `next` gets the last row's time and id, which it
 *  reads as the time alone. */
export function nextCursor(page: HistoryPage | null | undefined): HistoryCursor | null {
  if (!page || !page.more) return null;
  if (page.next && page.next.before) return page.next;
  const last = page.turns[page.turns.length - 1];
  return last ? { before: last.asked_at, before_id: last.id } : null;
}

/** where "older questions" reads from: after the last older page loaded,
 *  or after the first page while none has been */
export function pagingCursor(first: HistoryPage | null | undefined,
                             lastOlder: HistoryPage | null): HistoryCursor | null {
  return nextCursor(lastOlder ?? first);
}

// ---- who may delete from the log ----
// The shared demonstration login is every visitor at once, so the server
// refuses it both delete doors (a 403 naming the shared login); any other
// login that may edit — a demonstration household's own included — keeps
// them.

/** remove / clear are shown to an editor who is not the shared login */
export function mayTidyLog(canEdit: boolean,
                           me: { shared_login?: boolean } | undefined): boolean {
  return canEdit && !!me && !me.shared_login;
}

/** the 403 the delete doors answer the shared demonstration login with */
export function sharedLogDenied(e: unknown): boolean {
  const status = (e as { status?: unknown } | null)?.status;
  const msg = e instanceof Error ? e.message : "";
  return (status === 403 || /^403:/.test(msg))
    && /shared demonstration login/.test(msg);
}
