import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type ReactNode, useEffect, useRef, useState }
  from "react";
import { Link, useNavigate } from "react-router";
import { api, errText, kindIs, money, type Onboarding, type SetupMode,
         type Settings, type WizardStep } from "../api/client";
import { saveSettings, SETTINGS_SAVES } from "../api/cache";
import { browserPushBlocked, enableBrowserPush } from "../push";
import { leftWizard } from "../wizardexit";
import BudgetPlanner from "../components/BudgetPlanner";
import ConnectHub, { FIRST_SYNC_RUNNING } from "../components/ConnectHub";
import HistoryChoice, { historyLabel } from "../components/HistoryChoice";
import PrimaryCheckingPick from "../components/PrimaryCheckingPick";
import { billsContinue, checklistSummary, connectDoneHint } from "../checklist";
import ManualBudgets from "../components/ManualBudgets";
import { chooseTheme, type Theme } from "../components/themeChoice";
import { VerifyEmailCard } from "../components/VerifyEmailCard";
import { AutoBillsPane, ManualBillsPane, SetupQuestion }
  from "../components/WizardBills";
import { isOwner, isViewer } from "../role";
import { looksLikeMailbox, NEED_BASE_URL, recipientDuplicate }
  from "../recipients";
import { readStore } from "../safestorage";
import { sharedLock } from "../sharedlogin";

/** strict linear guided setup. One step at a time, one clear
 *  primary action per step; the dots are a passive progress indicator.
 *  Steps: Appearance → Connect → Bills & budgets (a question, then the
 *  finder's proposals or a hand-entry form) → Budgets → Email & push →
 *  Done (summary). The same six on every instance: what differs between a
 *  hosted and a self-hosted one (bank linking, the outbound mail account)
 *  differs inside a step, never in the step list, so a resume mark always
 *  means the same step.
 *  Step completion is an explicit user action (done/skipped marks in
 *  tenant config via wizard_steps); the resume pill re-enters at the
 *  first unfinished step. */

const STEP_KEYS: WizardStep[] =
  ["appearance", "connect", "bills", "budgets", "email", "finish"];

// prominent forward/primary button in the wizard footer
const fwdBtn: CSSProperties = {
  padding: ".55rem 1.15rem", fontSize: 15, fontWeight: 600,
};

// "7:00 am" — the wording of the send-hour picker and the closing summary
const hourLabel = (h: number) =>
  `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? "am" : "pm"}`;

const THEMES: { t: Theme; title: string; sw: string; preview: string;
                caption: string }[] = [
  { t: "auto", title: "Auto", sw: "auto", preview: "follows the system",
    caption: "Matches your device's light/dark setting. Default." },
  { t: "light", title: "Light", sw: "light", preview: "Today · $1,240 left",
    caption: "Always light, on every device you sign in on." },
  { t: "dark", title: "Dark", sw: "dark", preview: "Today · $1,240 left",
    caption: "Always dark, on every device you sign in on." },
];

/** The theme this login is using: a viewer or the demo keeps it on the
 *  device, everyone else on the household (absent = auto). */
function useThemeNow(): Theme {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const perDevice = isViewer(me) || !!me.data?.demo;
  const stored = readStore("local", "oiko-theme");
  const local: Theme = stored === "light" || stored === "dark" ? stored : "auto";
  return perDevice ? local : (s.data?.theme ?? "auto");
}

/** Step 1. A tap applies at once and is saved by the shell (which knows
 *  whether this login keeps it per device). */
function AppearanceStep() {
  const now = useThemeNow();
  const [picked, setPicked] = useState<Theme | null>(null);
  const cur = picked ?? now;
  return (
    <div className="wiz-choice three" role="radiogroup" aria-label="Appearance">
      {THEMES.map((o) => (
        <button key={o.t} type="button" role="radio" aria-checked={cur === o.t}
                className={"wiz-opt" + (cur === o.t ? " sel" : "")}
                onClick={() => { setPicked(o.t); chooseTheme(o.t); }}>
          {cur === o.t && <span className="wiz-opt-pick">SELECTED</span>}
          <b>{o.title}</b>
          <span className={"wiz-swatch " + o.sw} aria-hidden>{o.preview}</span>
          <small>{o.caption}</small>
        </button>
      ))}
    </div>
  );
}

/** Imported and manual accounts start at $0, which poisons the cash
 *  forecast until the owner states today's real balance — offer that
 *  here, optional, while the wizard still has their attention. */
function BalanceNudge() {
  const qc = useQueryClient();
  const today = useQuery({ queryKey: ["today", "now"], queryFn: () => api.todayFull() });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [vals, setVals] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, boolean>>({});
  const save = useMutation({
    mutationFn: ({ id, v }: { id: string; v: string }) =>
      api.accountBalance(id, v),
    onSuccess: (_r, { id }) => {
      setSaved((s) => ({ ...s, [id]: true }));
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  if (!today.data?.runway?.balance_unreliable) return null;
  const manual = (accounts.data?.accounts ?? [])
    .filter((a) => kindIs(a, "depository")
                   && (a.institution_name ?? "").toLowerCase() === "manual");
  if (manual.length === 0) return null;
  return (
    <div style={{ margin: ".8rem 0" }}>
      <h3 style={{ marginBottom: ".3rem" }}>One number makes the forecast real</h3>
      <p className="mut" style={{ marginTop: 0 }}>
        Imported accounts start at $0, so the cash forecast can't be
        trusted yet. Type today's balance (optional — Accounts has this
        too):</p>
      {manual.map((a) => (
        <div key={a.id} style={{ display: "flex", gap: ".5rem",
                                 alignItems: "center", marginTop: ".3rem" }}>
          <span style={{ flex: "0 1 14rem" }}>{a.name}</span>
          $<input inputMode="decimal" style={{ width: "8rem" }}
                  value={vals[a.id] ?? ""} placeholder="0.00"
                  onChange={(e) =>
                    setVals({ ...vals, [a.id]: e.target.value })} />
          <button disabled={save.isPending || !(vals[a.id] ?? "").trim()}
                  onClick={() => save.mutate({ id: a.id, v: vals[a.id] })}>
            {saved[a.id] ? "set ✓" : "set"}</button>
        </div>
      ))}
    </div>
  );
}

/** The import bar. Determinate — the fill is how much of the two-year
 *  window has landed — with a sweep running over it while the bank is
 *  still sending, so a fill that has not moved for a while still reads
 *  as working rather than hung. Full and still only when the bank says
 *  the window is complete. */
function BackfillBar({ frac, live, tall, muted }: {
  frac: number; live: boolean; tall?: boolean; muted?: boolean }) {
  const pct = Math.round(Math.max(0, Math.min(1, frac)) * 100);
  return (
    <div className={"bf-bar" + (tall ? " tall" : "")} role="progressbar"
         aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
         style={{ flexBasis: "100%" }}>
      <div className={"bf-fill" + (live ? " live" : "")
                      + (muted ? " muted" : "")}
           style={{ width: `${pct}%` }} />
    </div>
  );
}

/** A count that rolls up to its new value and flashes while it does —
 *  the bank delivers the whole history in one drop (tens to hundreds of
 *  rows in a single pull), and a number that snaps reads as a glitch
 *  where one that counts up reads as arrival. */
function LiveCount({ n }: { n: number }) {
  const [shown, setShown] = useState(n);
  const [flash, setFlash] = useState(false);
  const prev = useRef(n);
  useEffect(() => {
    const from = prev.current;
    prev.current = n;
    if (n <= from) { setShown(n); return; }
    setFlash(true);
    const t0 = performance.now(), dur = 1500;
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      setShown(Math.round(from + (n - from) * e));
      if (k < 1) raf = requestAnimationFrame(step);
      else setFlash(false);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [n]);
  return <span className={flash ? "bf-tick" : undefined}>
    {shown.toLocaleString()}</span>;
}

/** Step-2 doorman. Income/bill detection is only as good as the history
 *  it scans, and Plaid delivers its two-year backfill ASYNCHRONOUSLY —
 *  minutes after the link, not at it. Run the finder against the first
 *  ~30-day sliver and it proposes a handful of bills and no income,
 *  which reads as "detection is broken".
 *  So the detection step waits, visibly, until every Plaid connection
 *  reports its historical window complete; polling the endpoint also
 *  nudges the server to keep pulling. There is deliberately NO "scan now
 *  anyway" escape: everything downstream — income, bills, the budget the
 *  next step pre-fills — is derived from this history, so skipping ahead
 *  buys a wrong budget, not a faster one. Fails open only where waiting
 *  cannot help: no Plaid items, a connection in error (surfaced as
 *  needs-attention, excluded from the gate), or the endpoint itself
 *  erroring — none of those may strand the wizard. */
function BackfillGate({ children }: { children: ReactNode }) {
  const q = useQuery({
    queryKey: ["onboarding-backfill"],
    queryFn: api.onboardingBackfill,
    refetchInterval: (query) =>
      query.state.data?.all_ready ? false : 4000,
  });
  // The poll is read-only. Kicking a quiet backfill is a write, so it is
  // an owner-only POST — a self-host without a public webhook URL (and
  // Plaid Sandbox, whose HISTORICAL_UPDATE webhook does not arrive) would
  // otherwise wait for the hourly cron. A new bank's first pull is not
  // made at link time: it is queued on the worker, and a queued first sync
  // counts as syncing, so the nudge only asks the worker and never starts
  // a second pull beside it. Once on mount was not enough: while a pull
  // is queued or just ran the server rightly declines to start another,
  // and nothing pulls again — the bar sits at its first fill looking stuck.
  // So it repeats every 30 s while the wait lasts; the server keeps its
  // guards (no pull in the last 45 s, no sync running, no first sync queued, at most one queued pull per bank a minute), so a
  // re-nudge costs nothing while history is flowing and rescues the quiet
  // case.
  // A viewer's 403 is expected and ignored.
  const waiting = !!q.data && !q.data.all_ready;
  useEffect(() => {
    api.onboardingBackfillNudge().catch(() => {});
    if (!waiting) return;
    const t = setInterval(() => {
      api.onboardingBackfillNudge().catch(() => {});
    }, 30_000);
    return () => clearInterval(t);
  }, [waiting]);
  // When the last connection completes, hold the finished bars on screen
  // for a moment before the detection body replaces them: the fill
  // reaching the end IS the "it's done" signal, and swapping the view on
  // the same poll that reported it means nobody ever sees the bar full —
  // the progress appears never to reach 100%.
  const [reveal, setReveal] = useState(false);
  const finished = !!q.data && q.data.all_ready && q.data.items.length > 0;
  useEffect(() => {
    if (!finished) return;
    const t = setTimeout(() => setReveal(true), 1800);
    return () => clearTimeout(t);
  }, [finished]);
  if (q.isError || (q.data && q.data.all_ready && (reveal || !finished)))
    return <>{children}</>;
  if (!q.data) return <p className="mut">Checking your import status…</p>;
  const items = q.data.items;
  const done = items.filter((it) => it.ready).length;
  const monthYear = (iso: string) =>
    new Date(iso + "T00:00:00").toLocaleDateString(undefined,
      { month: "short", year: "numeric" });
  // one bar for the whole import: the mean of the connections' fills,
  // moving as each one reaches further back
  const overall = items.length
    ? items.reduce((a, it) => a + (it.stalled ? 1 : it.progress), 0)
      / items.length : 0;
  return (
    <div>
      <p style={{ marginTop: 0 }}>
        <b>Importing your full transaction history — {done}/{items.length}{" "}
        {items.length === 1 ? "connection" : "connections"} complete.</b></p>
      <BackfillBar frac={overall} live={items.some((it) => !it.ready && !it.stalled)}
                   tall />
      <p className="mut">
        Your bank sends up to two years of transactions after it connects,
        and this step waits for all of it. That history is what everything
        next is built from: it is how Oikonome <b>finds your paychecks and
        recurring bills automatically</b>, and how the next step
        <b> pre-fills your budget</b> from what you really spend. Scanning
        a few weeks instead would find almost nothing and set the wrong
        numbers. This one-time import usually takes a few minutes; the
        scan starts by itself the moment it finishes.</p>
      <div style={{ display: "flex", flexDirection: "column", gap: ".4rem",
                    margin: ".8rem 0" }}>
        {items.map((it) => (
          <div key={it.id} style={{ display: "flex", gap: ".6rem",
                                    alignItems: "baseline", flexWrap: "wrap" }}>
            <span style={{ flex: "0 1 16rem" }}>
              {it.institution || "bank"}</span>
            <span className="mut" style={{ fontSize: 13 }}>
              <LiveCount n={it.transactions} /> transaction
              {it.transactions === 1 ? "" : "s"}{it.ready ? "" : " so far"}
              {it.earliest && <> · back to {monthYear(it.earliest)}</>}</span>
            {it.ready ? (
              <span style={{ color: "var(--green, #4a4)", fontSize: 13 }}>
                ✓ complete</span>
            ) : it.stalled ? (
              <span style={{ color: "var(--amber, #e6b159)", fontSize: 13 }}>
                needs attention (Accounts page) — won't hold up this step
              </span>
            ) : (
              <span style={{ color: "var(--blue, #58a6ff)", fontSize: 13 }}>
                {it.active ? "pulling from your bank…"
                  // nothing has landed yet: the first sync is queued
                  : it.transactions === 0 ? FIRST_SYNC_RUNNING
                  : "waiting for the bank to send more…"}</span>
            )}
            <BackfillBar frac={it.stalled ? 1 : it.progress}
                         live={!it.ready && !it.stalled} muted={it.stalled} />
          </div>
        ))}
      </div>
      <p className="mut" style={{ fontSize: 13, marginBottom: 0 }}>
        You can leave this page — the import keeps going and the wizard
        resumes here.</p>
    </div>
  );
}

/** How the daily verdict is delivered: the email flag (`on`, the
 *  back-compat name) and the push flag of the daily schedule entry, read
 *  as one choice. Exported for the logic tests. */
export type Delivery = "email" | "push" | "both" | "off";
export function deliveryOf(on: boolean, push: boolean): Delivery {
  return on && push ? "both" : on ? "email" : push ? "push" : "off";
}
export function flagsOf(d: Delivery): { on: boolean; push: boolean } {
  return { on: d === "email" || d === "both", push: d === "push" || d === "both" };
}
const DELIVERY_WORDS: Record<Delivery, string> = {
  email: "Email", push: "Push", both: "Email and push", off: "Off" };

/** Who the scheduled mail reaches: the owner (always), then every other
 *  address on the settings view with its own on/off. The Email & push step
 *  lists them; the closing summary counts them. */
function recipientRows(st: Settings | undefined) {
  const muted = new Set((st?.email_muted ?? []).map((m) => m.toLowerCase()));
  const rows = (st?.email_recipient_status ?? []).map((r) => ({
    ...r, off: !r.owner && (!!r.muted || muted.has(r.email.toLowerCase())) }));
  return [...rows.filter((r) => r.owner), ...rows.filter((r) => !r.owner)];
}

/** "Who gets the email": the household's addresses with their on/off and
 *  an add box. The same doors Settings → Email & Push uses — the mute
 *  lists and email_recipients_added — so an address added here is invited
 *  exactly as one added there. The owner's own row has no switch here:
 *  turning yourself off belongs in Settings, not in a first run. */
function RecipientsBlock() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const notify = useQuery({ queryKey: ["notify"], queryFn: api.notifyStatus });
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => saveSettings(qc, body),
    // one settings write at a time, in order, with the other email saves
    scope: SETTINGS_SAVES,
    onError: (e) => {
      setNote({ text: errText(e), bad: true });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  const rows = recipientRows(s.data);
  const owner = rows.find((r) => r.owner);
  const addLock = sharedLock(me.data, "grant");
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    if (notify.data?.invites_need_base_url) {
      setNote({ text: NEED_BASE_URL, bad: true }); return;
    }
    if (!looksLikeMailbox(v)) {
      setNote({ text: "That doesn't look like an email address.", bad: true });
      return;
    }
    const dupe = recipientDuplicate(v, s.data?.email_recipients ?? [],
                                    owner?.email ?? me.data?.email);
    if (dupe) { setNote({ text: dupe }); setDraft(""); return; }
    setNote(null); setDraft("");
    save.mutate({ email_recipients_added: [v] }, {
      onSuccess: () => setNote({ text: `Invitation sent to ${v}.` }) });
  };
  const toggle = (email: string, off: boolean) =>
    save.mutate(off ? { email_muted_remove: [email.toLowerCase()] }
                    : { email_muted_add: [email.toLowerCase()] });
  const role = (r: (typeof rows)[number]) =>
    r.owner ? "owner" : r.member ? "member"
      : r.invite === "accepted" ? "accepted"
      : r.invite === "invited" ? "invitation sent"
      : r.invite ? `invitation ${r.invite}` : "added";
  return (
    <div className="wiz-sub">
      <b>Who gets the email</b>
      <div className="wiz-rows">
        {rows.length === 0 && me.data?.email && (
          <div className="wiz-row"><div className="wiz-row-main">
            <span className="wiz-addr">{me.data.email}{" "}
              <small className="mut">· owner</small></span>
            <span className="pill g">on</span></div></div>)}
        {rows.map((r) => (
          <div key={r.email} className="wiz-row"><div className="wiz-row-main">
            <span className="wiz-addr">{r.email}{" "}
              <small className="mut">· {role(r)}</small></span>
            {r.owner ? <span className="pill g">on</span> : (
              <button type="button" role="switch" aria-checked={!r.off}
                      aria-label={`${r.email} receives the email`}
                      className={"wiz-pill" + (r.off ? "" : " sel")}
                      disabled={save.isPending}
                      onClick={() => toggle(r.email, r.off)}>
                {r.off ? "off" : "on"}</button>)}
          </div></div>
        ))}
      </div>
      {addLock ? (
        <p className="mut" style={{ color: "var(--amber)", fontSize: 13 }}>
          {addLock}</p>
      ) : (
        <div className="wiz-addrow">
          <input type="email" placeholder="Add another address" value={draft}
                 aria-label="Add another address"
                 onChange={(e) => setDraft(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
          <button type="button" disabled={save.isPending || !draft.trim()}
                  onClick={add}>Add</button>
        </div>
      )}
      {note && <p className={note.bad ? "neg" : "mut"}
                  style={{ fontSize: 13, margin: ".3rem 0 0" }}>{note.text}</p>}
      <p className="mut" style={{ fontSize: 12, margin: ".3rem 0 0" }}>
        A new address gets one confirmation email first and can unsubscribe
        from any message. Members can also turn their own copy off in
        Settings.</p>
    </div>
  );
}

/** Step 5: how the daily verdict reaches the household — email, push,
 *  both or off, the hour, the email's face, this device's push — and who
 *  gets it. Reflects what is ALREADY set: with no schedule saved the daily
 *  email is on by default (the worker's behaviour), so a lone "Enable"
 *  button would be both redundant and, once pressed, irreversible from
 *  here. Choosing push also registers THIS browser, the same door
 *  Settings → Email & Push opens, so the choice is not a promise the
 *  browser has not agreed to. */
function EmailPushStep() {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const notify = useQuery({ queryKey: ["notify"], queryFn: api.notifyStatus });
  const qc = useQueryClient();
  const daily = s.data?.email_schedule?.daily;
  // absent schedule = daily on (the default), full report, 7am local.
  // Each control keeps a local override while its write lands (delivery
  // included — reading the query alone leaves the choice showing the OLD
  // state until the refetch), and the save seeds ["settings"] from the
  // response so the overrides and the server agree when they clear.
  const [deliveryOv, setDeliveryOv] = useState<Delivery | null>(null);
  const delivery = deliveryOv
    ?? deliveryOf(daily?.on ?? true, !!daily?.push);
  const { on, push } = flagsOf(delivery);
  const [hour, setHour] = useState<number | null>(null);
  const [summary, setSummary] = useState<boolean | null>(null);
  const hourVal = hour ?? daily?.hour ?? 7;
  const summaryVal = summary ?? daily?.summary ?? false;
  const [pushNote, setPushNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const save = useMutation({
    mutationFn: (next: { on: boolean; push: boolean; hour: number;
                         summary: boolean }) =>
      saveSettings(qc, {
        email_schedule: {
          ...(s.data?.email_schedule ?? {}),
          daily: { ...(daily ?? {}), on: next.on, push: next.push,
                   hour: next.hour, summary: next.summary },
        },
      }),
    scope: SETTINGS_SAVES,
    onError: () => setDeliveryOv(null),     // failed write ≠ new truth
  });
  const commit = (patch: Partial<{ on: boolean; push: boolean; hour: number;
                                    summary: boolean }>) =>
    save.mutate({ on, push, hour: hourVal, summary: summaryVal, ...patch });
  // browser push (VAPID) and phone push (the relay) are separate channels:
  // an instance with only the relay can still deliver to the mobile apps,
  // so the choice has to stay offered even though THIS browser cannot
  // subscribe — otherwise a phone-only household cannot pick the one
  // delivery it can actually receive
  const webPush = !!notify.data?.push_available;
  const pushReady = webPush || !!notify.data?.native_push_available;
  const pushHere = !!notify.data?.push_subscribed;
  const phones = notify.data?.native_push_devices ?? 0;
  const enableHere = async () => {
    if (!notify.data?.vapid_public_key) return;
    // the shared demonstration login may choose push for the schedule
    // (phones already registered still get it) but may not plant this
    // browser as a new destination: the server refuses the subscription,
    // so stop before the browser asks for permission it can't use
    const grantLock = sharedLock(me.data, "grant");
    if (grantLock) { setPushNote({ bad: true, text: grantLock }); return; }
    if (browserPushBlocked()) {
      setPushNote({ bad: true, text: "This browser cannot receive push on "
        + "http:// — the phone app can, or open the instance over https." });
      return;
    }
    try {
      const r = await enableBrowserPush(notify.data.vapid_public_key);
      setPushNote(r.ok ? { text: "This browser will receive it." }
                       : { bad: true, text: r.reason });
      if (r.ok) qc.invalidateQueries({ queryKey: ["notify"] });
    } catch (e) { setPushNote({ bad: true, text: errText(e) }); }
  };
  const choose = async (next: Delivery) => {
    setDeliveryOv(next); setPushNote(null);
    commit(flagsOf(next));
    // the browser's own subscription rides along with the choice: a
    // schedule that says "push" to a browser that never agreed sends to
    // nobody, and the person would only learn that tomorrow morning
    if ((next === "push" || next === "both") && webPush && !pushHere)
      await enableHere();
  };
  const hosted = !!me.data?.hosted;
  const smtpSet = !!(s.data?.smtp_host || s.data?.smtp_password_set);
  return (
    <div>
      <div className="wiz-field">
        <span><b>Daily verdict</b></span>
        <span className="wiz-pills" role="radiogroup" aria-label="Daily verdict">
          {(["email", "push", "both", "off"] as Delivery[]).map((v) => {
            const noPush = (v === "push" || v === "both") && !pushReady;
            return (
              <button key={v} type="button" role="radio"
                      aria-checked={delivery === v}
                      className={"wiz-pill" + (delivery === v ? " sel" : "")}
                      disabled={save.isPending || noPush}
                      title={noPush ? "Push is not configured on this instance" : ""}
                      onClick={() => choose(v)}>
                {v === "both" ? "Both" : DELIVERY_WORDS[v]}</button>
            );
          })}
        </span>
      </div>
      {/* hosted mail goes to confirmed addresses only: a fresh signup
          choosing Email here would otherwise wait for a morning that
          never comes, with the way to a fresh link three days off */}
      {on && (
        <VerifyEmailCard me={me.data} inline>
          Email delivery starts once your address is confirmed.
        </VerifyEmailCard>)}
      {(on || push) && (
        <div className="wiz-field">
          <span>Send at</span>
          <select value={hourVal} aria-label="Send at"
            onChange={(e) => { setHour(+e.target.value);
                               commit({ hour: +e.target.value }); }}>
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>{hourLabel(h)}</option>))}
          </select>
        </div>
      )}
      {on && (
        <label className="wiz-field">
          <span>Short summary instead of the full report{" "}
            <small className="mut">— just the verdict card: on budget or
              not, left to spend, pinned bills</small></span>
          <input type="checkbox" checked={summaryVal}
                 onChange={(e) => { setSummary(e.target.checked);
                                    commit({ summary: e.target.checked }); }} />
        </label>
      )}
      {webPush && (
        <div className="wiz-field">
          <span>Push to this device{" "}
            <small className="mut">(asks the browser or phone for
              permission)</small></span>
          {pushHere ? <span className="pill g">on</span>
            : <button type="button" onClick={() => { setPushNote(null);
                                                     void enableHere(); }}>
                Turn on</button>}
        </div>
      )}
      {(push || pushNote) && (
        <p className="mut" style={{ margin: "0 0 .5rem", fontSize: 13 }}>
          {pushNote ? <span className={pushNote.bad ? "neg" : ""}>{pushNote.text}</span>
            : pushHere ? "This browser will receive it."
            : webPush ? "Push goes to the browsers and phones you have enabled."
            : "Push goes to the phones you have enabled — this browser "
              + "cannot receive it on this instance."}
          {phones > 0 && ` ${phones} phone${phones === 1 ? "" : "s"} registered via the mobile app.`}
        </p>
      )}
      {save.isError && <p className="neg">{errText(save.error)}</p>}
      <RecipientsBlock />
      {/* a self-hosted instance sends through its own outbound account;
          a hosted one has the operator's, so the line never appears there */}
      {!hosted && (
        <p className="wiz-sub" style={{ fontSize: 14 }}>
          Outbound mail account —{" "}
          <b>{smtpSet ? "set up" : "not set up yet"}</b> ·{" "}
          <Link to="/settings/email">Open email settings</Link></p>
      )}
    </div>
  );
}

/** Step 6: what the walk set up, one line per step, read back from the
 *  server — so a resumed wizard summarises what is stored, not what this
 *  tab happens to remember. */
function DoneStep({ d }: { d: Onboarding | undefined }) {
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const bills = useQuery({ queryKey: ["bills"], queryFn: () => api.bills() });
  const theme = useThemeNow();
  const tracked = (bills.data?.bills ?? []).filter((b) => !b.disabled);
  const nInc = tracked.filter((b) => b.income).length;
  const nBills = tracked.length - nInc;
  const daily = s.data?.email_schedule?.daily;
  const delivery = deliveryOf(daily?.on ?? true, !!daily?.push);
  const nRecip = Math.max(1, recipientRows(s.data).filter((r) => !r.off).length);
  const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const conns = d?.connections ?? 0;
  const reach = d && d.history_months !== undefined ? d.history_months
    : s.data?.history_months;
  const accounts = conns > 0 ? `${conns} connected`
    : (d?.accounts ?? 0) > 0 ? `${plural(d?.accounts ?? 0, "account")} from files`
    : "none yet";
  // the figures only when the household set a budget (onboarding's
  // budgets_set, the verdict's own gate): under a month of history the
  // settings carry a provisional seed nobody chose, and the summary would
  // otherwise call the app's guess the person's budget
  const budgets = d?.budgets_set
    ? `Food ${money(s.data?.food_monthly ?? 0)} · Everything else `
      + money(s.data?.other_monthly ?? 0)
    : "not set yet (Budget page any time)";
  const rows: [string, string][] = [
    ["Appearance", theme.charAt(0).toUpperCase() + theme.slice(1)],
    ["Accounts", `${accounts} · ${historyLabel(reach)}`],
    ["Bills & income",
      `${plural(nBills, "bill")}, ${plural(nInc, "paycheck")} approved`],
    ["Budgets", budgets],
    ["Email & push", `${DELIVERY_WORDS[delivery]}`
      + (delivery === "off" ? "" : ` at ${hourLabel(daily?.hour ?? 7)}`)
      + ` · ${plural(nRecip, "recipient")}`],
    // what the Today page's checklist will say — the same list it reads
    ["Today checklist", d ? checklistSummary(d) : "…"],
  ];
  return (
    <div>
      <div className="wiz-rows">
        {rows.map(([k, v]) => (
          <div key={k} className="wiz-row"><div className="wiz-row-main">
            <span>{k}</span><small className="mut">{v}</small>
          </div></div>
        ))}
      </div>
      <BalanceNudge />
    </div>
  );
}

type StepView = {
  key: WizardStep;
  // the progress row's words; the heading when `title` is absent
  label: string; title?: string; blurb: string;
  primary: { label: string; disabled?: boolean; hint?: string;
             onClick?: () => void } | null;
  skippable: boolean; body: ReactNode;
  // Back inside the step (the bills step's two panes); absent = the
  // previous step
  onBack?: () => void;
};

export default function Welcome() {
  const nav = useNavigate();
  const qc = useQueryClient();
  // poll so marks and counts keep up with work done inside embedded pages
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding,
                        refetchInterval: 5000 });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const d = ob.data;
  const keys = STEP_KEYS;
  const [step, setStep] = useState<number | null>(null);
  // the bills step opens on its question; Continue there shows the pane
  // the answer picked, inside the same step
  const [billsPane, setBillsPane] = useState<"ask" | "work">("ask");
  const [modeOv, setModeOv] = useState<SetupMode | null>(null);
  const mode: SetupMode = modeOv ?? st.data?.setup_mode ?? d?.setup_mode
    ?? "auto";
  // resume at the first step with no done/skipped mark
  useEffect(() => {
    if (step === null && d && !me.isPending) {
      const first = keys.findIndex((k) => !d.wizard_steps?.[k]);
      // a FINISHED wizard has nothing to resume — arriving here anyway
      // (React Router keeps the pre-login path, so a sign-in that starts
      // on /welcome lands back on /welcome: stale bookmark, old /setup
      // redirect, a seeded demo instance) would park on a dead "step 6 of
      // 6 — Done" screen. Bounce to Today instead. Mid-walk states (all steps
      // marked but Finish not clicked) still render so the user can
      // finish deliberately.
      if (first === -1 && d.wizard_done) {
        nav("/", { replace: true });
        return;
      }
      setStep(first === -1 ? keys.length - 1 : first);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d, step, me.isPending]);
  const mark = useMutation({
    mutationFn: (m: { key: WizardStep; state: "done" | "skipped" }) =>
      api.settingsSave({ wizard_steps: { [m.key]: m.state } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["onboarding"] }),
  });
  const finish = useMutation({
    mutationFn: () => api.settingsSave(
      { wizard_done: true, wizard_steps: { finish: "done" } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      leftWizard(me.data?.tenant_id);
      nav("/");
    },
  });
  // The answer to the bills question is saved on the tap: manual also
  // turns the household's finder off, which the Bills page and the next
  // step's prefill read. Writes go one at a time on the settings queue, so
  // two quick taps cannot land out of order and store the earlier answer;
  // a tap made while one is in flight is only remembered, and the queued
  // write sends the newest answer — the last tap wins. The cards show the
  // tap until the last write settles, then the stored answer.
  const modeWant = useRef<SetupMode | null>(null);
  const modeInFlight = useRef(0);
  const pickMode = useMutation({
    scope: SETTINGS_SAVES,
    mutationFn: async () => {
      const m = modeWant.current;
      // an earlier queued write already sent the newest answer
      if (!m) return null;
      modeWant.current = null;
      // "auto" says so explicitly: the server does not turn the finder
      // back on by itself when the mode flips back from manual
      return saveSettings(qc, { setup_mode: m,
                                bill_detection_auto: m === "auto" });
    },
    onSuccess: (r) => {
      if (!r) return;
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
    onError: (e) => window.alert(errText(e)),
    onSettled: () => {
      modeInFlight.current -= 1;
      if (modeInFlight.current <= 0) {
        modeInFlight.current = 0;
        setModeOv(null);
      }
    },
  });
  const chooseMode = (m: SetupMode, then?: () => void) => {
    setModeOv(m);
    modeWant.current = m;
    modeInFlight.current += 1;
    pickMode.mutate(undefined, then ? { onSuccess: () => then() } : undefined);
  };
  // the bills step's auto pane is gated by the SAME readiness BackfillGate
  // reads (shared query key, so this is one poll, not two): while the
  // backfill is still pulling history there is no Continue and no Skip — a
  // second tab or an impatient click on the 30-day sliver would seed
  // income, bills and the budget from an incomplete ledger, which is
  // exactly what the gate exists to stop. Fails open with the gate
  // (endpoint error) so nothing strands the wizard.
  const backfill = useQuery({
    queryKey: ["onboarding-backfill"],
    queryFn: api.onboardingBackfill,
    refetchInterval: (query) =>
      query.state.data?.all_ready ? false : 4000,
  });
  const backfillReady = backfill.isError
    || !!(backfill.data && backfill.data.all_ready);
  // The wizard connects a bank and configures the household's mail, both
  // of which are the owner's — so a member landing here (deep link, stale
  // bookmark) gets the same note a viewer does. Editing budgets, which is
  // theirs, lives on the Budget page.
  if (!isOwner(me))
    return (
      <>
        <h1>Setup</h1>
        <p className="sub">The account owner runs the guided setup — it
          connects banks and configures this instance.</p>
      </>
    );

  const idx = step ?? 0;
  const goTo = (i: number) => {
    // walking forward into the bills step opens its question; Back from
    // budgets returns to the pane that was left
    if (keys[i] === "bills" && i > idx) setBillsPane("ask");
    setStep(i);
  };
  const advance = (state: "done" | "skipped") => {
    mark.mutate({ key: keys[idx], state });
    goTo(Math.min(idx + 1, keys.length - 1));
  };
  // the Done step's summary reads budgets from ["settings"]; without this
  // it shows the pre-save seed over the numbers just saved
  const refreshBadges = () => {
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["settings"] });
  };
  const daily = st.data?.email_schedule?.daily;
  const delivery = deliveryOf(daily?.on ?? true, !!daily?.push);
  const firstSend = delivery === "off" ? ""
    : `; the first ${delivery === "push" ? "notification" : "email"} `
      + `arrives tomorrow at ${hourLabel(daily?.hour ?? 7)}`;

  const billsOut = billsContinue(d?.pending_proposals ?? 0);
  const billsPrimary = { label: billsOut.label, hint: billsOut.hint,
                         onClick: () => advance(billsOut.state) };
  const askChecking = connectDoneHint(d?.checking_candidates?.length ?? 0,
                                      !!d?.primary_checking_id);
  const billsStep: StepView = billsPane === "ask" ? {
    key: "bills", label: "Bills & budgets",
    title: "Bills & budgets — how do you want to set them up?",
    blurb: "Your history is in. Oikonome can read it and propose your " +
           "recurring bills, paychecks and a starting budget — or you can " +
           "enter them yourself.",
    primary: { label: "Continue →", disabled: pickMode.isPending,
               onClick: () => {
                 // the pre-selected answer was never tapped: save it now,
                 // before the pane that depends on it opens
                 if (st.data?.setup_mode === mode) { setBillsPane("work"); return; }
                 chooseMode(mode, () => setBillsPane("work"));
               } },
    skippable: true,
    body: <SetupQuestion mode={mode} onPick={(m) => chooseMode(m)} />,
  } : mode === "manual" ? {
    key: "bills", label: "Enter bills & income",
    blurb: "Add the bills and paychecks you know. Each one matches its " +
           "charges in your ledger by merchant from here on, so renames and " +
           "price drift keep working. You can switch to automatic detection " +
           "any time from Bills.",
    primary: { label: "Continue →" }, skippable: true,
    onBack: () => setBillsPane("ask"),
    body: <ManualBillsPane />,
  } : {
    key: "bills", label: "Confirm bills & income",
    blurb: "The finder scanned your ledger for recurring bills and " +
           "paychecks. Approve what's right, dismiss what isn't — approved " +
           "(and still-pending) income seeds the next step's budget. " +
           "Continue any time and revisit later.",
    // detection AND the footer wait for the full bank backfill — see
    // BackfillGate and backfillReady above. With proposals still pending
    // the step is left as skipped, not done: the Today checklist counts
    // approved bills, and a Continue past pending offers must not say
    // the step is finished
    primary: backfillReady ? { ...billsPrimary } : null,
    skippable: backfillReady,
    onBack: () => setBillsPane("ask"),
    body: <BackfillGate><AutoBillsPane /></BackfillGate>,
  };

  const steps: StepView[] = [
    { key: "appearance", label: "Appearance",
      blurb: "Oikonome follows your phone or computer by default — light " +
             "by day, dark at night, whatever the system says. Pin one if " +
             "you prefer. You can change this any time in Settings.",
      primary: { label: "Continue →" }, skippable: false,
      body: <AppearanceStep /> },
    { key: "connect", label: "Connect your accounts",
      blurb: "Link every bank you use — the grid shows what's already " +
             "connected. When you've added everything you plan to, say so " +
             "below. (File imports alone work completely too.)",
      // files-only households have no connection but DO have transactions
      // after an inline import — that also counts as "connected enough"
      primary: { label: "Done connecting →",
                 disabled: ((d?.connections ?? 0) === 0
                   && (d?.transactions ?? 0) === 0) || !!askChecking,
                 hint: (d?.connections ?? 0) === 0
                       && (d?.transactions ?? 0) === 0
                   ? "connect at least one source, or skip" : askChecking },
      skippable: true,
      body: (
        <>
          <ConnectHub />
          <p style={{ margin: ".6rem 0 0" }}>
            <Link to="/import">Import a file instead</Link></p>
          <div className="wiz-sub"><HistoryChoice /></div>
          {/* the forecast's anchor: one checking account is pinned on the
              way out; two or more are asked here, before the step is left */}
          {(d?.checking_candidates?.length ?? 0) > 0 && (
            <div className="wiz-sub">
              <PrimaryCheckingPick candidates={d?.checking_candidates ?? []}
                                   pinned={d?.primary_checking_id ?? null} />
            </div>)}
        </>
      ) },
    billsStep,
    { key: "budgets", label: "Set your budgets",
      blurb: mode === "manual"
        ? "Your monthly plan: income and bills from the last step, a " +
          "number for each kind of spending, and what's left as savings. " +
          "The figures start blank — tap a suggestion from your history to " +
          "use it. The daily verdict only judges the variable spending."
        : "Your monthly plan, pre-filled from your real data: income " +
          "and bills from the last step, spending budgets from your " +
          "history, and what's left as savings. The daily verdict only " +
          "judges the variable spending.",
      primary: { label: "Continue →", disabled: !d?.budgets_set,
                 hint: d?.budgets_set ? undefined : "save budgets first" },
      skippable: true,
      body: mode === "manual" ? <ManualBudgets onSaved={refreshBadges} />
        : <BudgetPlanner onSaved={refreshBadges} /> },
    { key: "email", label: "Email & push",
      blurb: "One message a day with your verdict, and alerts when " +
             "something needs you. Choose how it reaches you, when, and who " +
             "else in the household gets it.",
      primary: { label: "Continue →" }, skippable: true,
      body: <EmailPushStep /> },
    { key: "finish", label: "Done",
      blurb: "That's everything. The Today page carries your daily verdict " +
             `from here on${firstSend}.`,
      primary: null, skippable: false,
      body: <DoneStep d={d} /> },
  ];
  const s = steps[idx];
  const last = idx === steps.length - 1;

  return (
    <>
      <div className="card wiz-card" style={{ marginBottom: "1rem" }}>
        {/* one progress row: dots · where you are · the way out. A
            full-width h1, an exit sentence dressed as a button and a
            separate dots line would be three rows of chrome above a form
            whose own title then repeats itself. */}
        <div className="wiz">
          <span className="wiz-dots">
            {steps.map((x, i) => {
              const done = !!d?.wizard_steps?.[x.key];
              return (
                <i key={x.key} title={`${i + 1}. ${x.label}`}
                   className={i === idx ? "on" : done ? "done" : ""} />
              );
            })}
          </span>
          <span className="wiz-where">
            Step {idx + 1} of {steps.length} · <b>{s.label}</b></span>
          <button className="linklike mut wiz-later"
            style={{ background: "none", border: "none",
                     cursor: "pointer", fontSize: 12 }}
            onClick={() => { leftWizard(me.data?.tenant_id); nav("/"); }}
            title="your answers so far are saved; the nav keeps a
                   “Finish setup” door">
            save &amp; finish later</button>
        </div>
        <h2 style={{ margin: ".1rem 0 .3rem", fontSize: "1.1rem" }}>
          {s.title ?? s.label}</h2>
        <p className="mut" style={{ margin: ".7rem 0 0" }}>{s.blurb}</p>
        {/* the step's content lives INSIDE the wizard pane — one big card */}
        <div style={{ marginTop: "1rem", paddingTop: "1rem",
                      borderTop: "1px solid var(--line)" }}>
          {s.body}
        </div>
        {/* nav at the very bottom right: Skip · Back · Continue */}
        <div className="wiz-nav">
          {s.skippable && (
            <button className="linklike mut wiz-skip" style={{ background: "none",
                border: "none", cursor: "pointer", fontSize: 13 }}
              onClick={() => advance("skipped")}>
              Skip this step</button>
          )}
          <button disabled={idx === 0 && !s.onBack}
                  onClick={() => s.onBack ? s.onBack() : goTo(idx - 1)}>
            ← Back</button>
          {s.primary && (
            <span title={s.primary.hint || ""}>
              <button className="pri" style={fwdBtn}
                disabled={s.primary.disabled}
                onClick={s.primary.onClick ?? (() => advance("done"))}>
                {s.primary.label}</button>
            </span>
          )}
          {last && (
            <button className="pri" style={fwdBtn}
              disabled={finish.isPending} onClick={() => finish.mutate()}>
              Go to Today →</button>
          )}
        </div>
        <div style={{ textAlign: "right", marginTop: ".5rem" }}>
          <button className="linklike mut" style={{ background: "none",
              border: "none", cursor: "pointer", fontSize: 12 }}
            disabled={finish.isPending}
            onClick={() => finish.mutate()}>
            don't show this again</button>
        </div>
      </div>
    </>
  );
}
