import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type ReactNode, useEffect, useRef, useState }
  from "react";
import { useNavigate } from "react-router";
import { api, kindIs, type Onboarding, type WizardStep } from "../api/client";
import { saveSettings } from "../api/cache";
import { browserPushBlocked, enableBrowserPush } from "../push";
import { leftWizard } from "../wizardexit";
import BudgetPlanner from "../components/BudgetPlanner";
import ConnectHub from "../components/ConnectHub";
import Bills from "./Bills";
import { isOwner } from "../role";

/** strict linear guided setup. One step at a time, one clear
 *  primary action per step; the dots are a passive progress indicator.
 *  Steps: Connect → Bills → Budgets → Finish (recap). Self-host inserts
 *  optional SMTP before Finish. There are deliberately no sync or
 *  older-history steps: the Plaid link already pulls data, and file import
 *  lives under Connect / Accounts.
 *  Step completion is an explicit user action (done/skipped marks in
 *  tenant config via wizard_steps); the resume pill re-enters at the
 *  first unfinished step. */

// self-hosted gets an optional SMTP step before Finish; hosted deploys
// have operator mail and skip it
const stepKeys = (hosted: boolean): WizardStep[] => hosted
  ? ["connect", "bills", "budgets", "finish"]
  : ["connect", "bills", "budgets", "email", "finish"];

// prominent forward/primary button in the wizard footer
const fwdBtn: CSSProperties = {
  padding: ".55rem 1.15rem", fontSize: 15, fontWeight: 600,
};

function BudgetsStep({ onSaved }: { onSaved: () => void }) {
  return <BudgetPlanner onSaved={onSaved} />;
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
 *  rows in a single pull), and a number that snaps reads as a glitch where one that
 *  counts up reads as arrival. */
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
 *  buys a wrong budget, not a faster one. Fails open only where waiting cannot help: no Plaid items,
 *  a connection in error (surfaced as needs-attention, excluded from the
 *  gate), or the endpoint itself erroring — none of those may strand the
 *  wizard. */
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
  // otherwise wait for the hourly cron. Once on mount was not enough: the
  // link-time pull is too recent at that moment, so the server
  // rightly declines to start another, and nothing pulls again — the bar
  // sits at its first fill looking stuck.
  // So it repeats every 30 s while the wait lasts; the server keeps its
  // guards (no pull in the last 45 s, no sync running), so a re-nudge
  // costs nothing while history is flowing and rescues the quiet case.
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

/** Closing recap + the daily verdict's delivery: email, push, both or
 *  off, the hour, and the email's face. Reflects what is ALREADY set —
 *  with no schedule saved the daily email is on by default (the worker's
 *  behaviour), so a lone "Enable" button would be both redundant and, once
 *  pressed, irreversible from here — which reads as an enable/disable that
 *  does not work. Choosing push here also registers THIS browser, the same
 *  door Settings → Email & Push opens, so the choice is not a promise the
 *  browser has not agreed to. */
function FinishStep({ d }: { d: Onboarding | undefined }) {
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
  const choose = async (next: Delivery) => {
    setDeliveryOv(next); setPushNote(null);
    commit(flagsOf(next));
    // the browser's own subscription rides along with the choice: a
    // schedule that says "push" to a browser that never agreed sends to
    // nobody, and the person would only learn that tomorrow morning
    if ((next === "push" || next === "both") && pushReady && !pushHere
        && notify.data?.vapid_public_key) {
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
      } catch (e) { setPushNote({ bad: true, text: String(e) }); }
    }
  };
  const smtpSet = !!(s.data?.smtp_host || s.data?.smtp_password_set)
    || !!me.data?.hosted;
  const money = (v: number | null | undefined) =>
    v && v > 0 ? `$${Math.round(v).toLocaleString()}` : null;
  const budgets = [money(s.data?.food_monthly) &&
                   `${money(s.data?.food_monthly)} food`,
                   money(s.data?.other_monthly) &&
                   `${money(s.data?.other_monthly)} everything-else`]
    .filter(Boolean).join(" / ");
  const hourLabel = (h: number) =>
    h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`;
  return (
    <div>
      <h2 style={{ marginTop: 0 }}>You're set up ✓</h2>
      <ul style={{ margin: ".3rem 0 .8rem", paddingLeft: "1.3rem" }}>
        <li><b>{d?.accounts ?? 0}</b> account{(d?.accounts ?? 0) === 1 ? "" : "s"} ·{" "}
          <b>{(d?.transactions ?? 0).toLocaleString()}</b>{" "}
          transaction{(d?.transactions ?? 0) === 1 ? "" : "s"}</li>
        <li><b>{d?.bills ?? 0}</b> recurring bill{(d?.bills ?? 0) === 1 ? "" : "s"} & paychecks tracked</li>
        <li>{budgets ? <>budgets: <b>{budgets}</b></>
          : <span className="mut">no budgets set (Settings any time)</span>}</li>
      </ul>
      <BalanceNudge />
      <h3 style={{ marginBottom: ".3rem" }}>Daily verdict</h3>
      <p className="mut" style={{ marginTop: 0 }}>
        Each morning: on budget or not, and the number to hit. Where it
        goes:
        {!smtpSet && on && " (Email needs SMTP in Settings → Email & Push before it can send.)"}
      </p>
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                    marginBottom: ".5rem" }}>
        {([["email", "Email"], ["push", "Push notification"],
           ["both", "Both"], ["off", "Off"]] as [Delivery, string][]).map(
          ([v, label]) => (
            <button key={v} className={delivery === v ? "pri" : ""}
                    disabled={save.isPending
                              || ((v === "push" || v === "both") && !pushReady)}
                    title={(v === "push" || v === "both") && !pushReady
                      ? "Push is not configured on this instance" : ""}
                    onClick={() => choose(v)}>{label}</button>
          ))}
        {(on || push) && (
          <label style={{ alignSelf: "center" }}>at{" "}
            <select value={hourVal}
              onChange={(e) => { setHour(+e.target.value);
                                 commit({ hour: +e.target.value }); }}>
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>{hourLabel(h)}</option>))}
            </select>
          </label>
        )}
      </div>
      {push && (
        <p className="mut" style={{ margin: "0 0 .5rem", fontSize: 13 }}>
          {pushNote ? <span className={pushNote.bad ? "neg" : ""}>{pushNote.text}</span>
            : pushHere ? "This browser will receive it."
            : webPush ? "Push goes to the browsers and phones you have enabled."
            : "Push goes to the phones you have enabled — this browser "
              + "cannot receive it on this instance."}
          {phones > 0 && ` ${phones} phone${phones === 1 ? "" : "s"} registered via the mobile app.`}
        </p>
      )}
      {on && (
        <div style={{ display: "flex", flexDirection: "column", gap: ".3rem",
                      marginBottom: ".6rem" }}>
          <label style={{ display: "flex", gap: ".5rem",
                          alignItems: "baseline" }}>
            <input type="radio" name="daily-shape" checked={!summaryVal}
              onChange={() => { setSummary(false); commit({ summary: false }); }} />
            <span><b>Full report</b>{" "}
              <span className="mut">— the verdict plus the whole Today
                page: spending buckets, recent transactions, upcoming
                bills.</span></span>
          </label>
          <label style={{ display: "flex", gap: ".5rem",
                          alignItems: "baseline" }}>
            <input type="radio" name="daily-shape" checked={summaryVal}
              onChange={() => { setSummary(true); commit({ summary: true }); }} />
            <span><b>Verdict only</b>{" "}
              <span className="mut">— just the card: on budget or not,
                left to spend, pinned bills.</span></span>
          </label>
        </div>
      )}
      {/* no recipient line and no household link here: the link would
          lead OUT of the wizard, and the household is added later from
          Settings */}
      {save.isError && <p className="mut">{String(save.error)}</p>}
    </div>
  );
}

const SMTP_PROVIDERS: { name: string; host: string; port: number;
                        note: ReactNode }[] = [
  { name: "Gmail", host: "smtp.gmail.com", port: 587,
    note: <>Turn on 2-step verification, then create an <b>app password</b>{" "}
      at <a href="https://myaccount.google.com/apppasswords" target="_blank"
      rel="noreferrer">myaccount.google.com/apppasswords</a>. User = your
      full Gmail address; password = the 16-character app password (your
      normal password won't work).</> },
  { name: "Outlook / Hotmail", host: "smtp-mail.outlook.com", port: 587,
    note: <>At <a href="https://account.microsoft.com/security"
      target="_blank" rel="noreferrer">account.microsoft.com/security</a>{" "}
      enable two-step verification, then add an <b>app password</b>. User =
      your full address; password = the app password.</> },
  { name: "Yahoo", host: "smtp.mail.yahoo.com", port: 587,
    note: <>Yahoo Account Security → <b>Generate app password</b>. User =
      your full Yahoo address; password = the app password.</> },
  { name: "iCloud", host: "smtp.mail.me.com", port: 587,
    note: <>At <a href="https://appleid.apple.com" target="_blank"
      rel="noreferrer">appleid.apple.com</a> → Sign-In & Security →{" "}
      <b>App-specific passwords</b>. User = your full iCloud address.</> },
  { name: "Fastmail", host: "smtp.fastmail.com", port: 587,
    note: <>Settings → Privacy & Security → <b>New app password</b>{" "}
      (scope: SMTP). User = your full Fastmail address.</> },
  { name: "Proton Mail", host: "smtp.protonmail.ch", port: 587,
    note: <>Needs a paid plan with a custom domain: Settings →{" "}
      <b>IMAP/SMTP</b> → generate an <b>SMTP token</b>. User = your full
      address; password = the token. (Proton Bridge users: point host/port
      at the Bridge's local SMTP instead.)</> },
];

/** Optional SMTP step (self-hosted only): the daily verdict email needs an
 *  outbound mail account. Provider quick-guides prefill host/port; the
 * password is stored encrypted, write-only. */
function EmailStep() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [host, setHost] = useState<string | null>(null);
  const [port, setPort] = useState<string | null>(null);
  const [user, setUser] = useState<string | null>(null);
  const [pw, setPw] = useState("");
  const [from, setFrom] = useState<string | null>(null);
  const hostVal = host ?? s.data?.smtp_host ?? "";
  const portVal = port ?? String(s.data?.smtp_port ?? 587);
  const userVal = user ?? s.data?.smtp_user ?? "";
  const fromVal = from ?? s.data?.smtp_from ?? "";
  const save = useMutation({
    mutationFn: () => api.settingsSave({
      smtp_host: hostVal.trim(), smtp_port: portVal,
      smtp_user: userVal.trim(), smtp_password: pw,
      smtp_from: (fromVal || userVal).trim() }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
  const test = useMutation({ mutationFn: api.smtpTest });
  const saved = !!(s.data?.smtp_host && s.data?.smtp_password_set);
  const inp = (val: string, set: (v: string) => void, ph: string,
               type = "text", width = "14rem") => (
    <input type={type} value={val} placeholder={ph}
      onChange={(e) => set(e.target.value)}
      style={{ width, padding: ".5rem", background: "var(--hover)",
               border: "1px solid var(--line)", borderRadius: "var(--rs)",
               color: "var(--ink)" }} />
  );
  return (
    <div>
      <p className="mut" style={{ marginTop: 0 }}>
        The daily verdict lands by email — that needs an outbound (SMTP)
        account. Any provider works; the quick guides below cover the
        common ones. Totally optional: skip it and set it up later in
        Settings → Email & Push.</p>
      <div style={{ marginBottom: ".6rem" }}>
        {SMTP_PROVIDERS.map((p) => (
          <details key={p.name} style={{ margin: ".25rem 0" }}>
            <summary style={{ cursor: "pointer" }}>{p.name}</summary>
            <div className="mut" style={{ margin: ".35rem 0 .35rem 1rem" }}>
              {p.note}{" "}
              <button className="linklike" style={{ background: "none",
                  border: "none", cursor: "pointer", padding: 0,
                  textDecoration: "underline" }}
                onClick={() => { setHost(p.host); setPort(String(p.port)); }}>
                use {p.host}:{p.port}</button>
            </div>
          </details>
        ))}
      </div>
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
        <label>SMTP host<br />{inp(hostVal, setHost, "smtp.gmail.com")}</label>
        <label>Port<br />{inp(portVal, setPort, "587", "text", "5rem")}</label>
      </div>
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                    marginTop: ".5rem" }}>
        <label>User<br />{inp(userVal, setUser, "you@example.com")}</label>
        <label>Password{" "}
          {s.data?.smtp_password_set && !pw &&
            <span className="mut">(saved)</span>}<br />
          {inp(pw, setPw, s.data?.smtp_password_set
            ? "unchanged" : "app password", "password")}</label>
      </div>
      <div style={{ marginTop: ".5rem" }}>
        <label>From address <span className="mut">(defaults to user)</span>
          <br />{inp(fromVal, setFrom, userVal || "money@example.com")}</label>
      </div>
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".7rem",
                    alignItems: "center" }}>
        <button className="pri" disabled={save.isPending || !hostVal
                                          || !userVal}
          onClick={() => save.mutate()}>
          {save.isPending ? "saving…" : "Save"}</button>
        <button disabled={!saved || test.isPending}
          title={saved ? "" : "save settings first"}
          onClick={() => test.mutate()}>
          {test.isPending ? "sending…" : "Send a test email"}</button>
        {test.isSuccess && (
          test.data.failed?.length
            ? <span className="pill b">
                {test.data.failed.map((f) => f.email).join(", ")} failed</span>
            : <span className="pill g">
                sent to {test.data.sent_to.join(", ")} ✓</span>)}
      </div>
      {(save.isError || test.isError) && (
        <p style={{ color: "var(--red)" }}>
          {String(save.error || test.error)}</p>)}
    </div>
  );
}

export default function Welcome() {
  const nav = useNavigate();
  const qc = useQueryClient();
  // poll so marks and counts keep up with work done inside embedded pages
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding,
                        refetchInterval: 5000 });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const d = ob.data;
  const keys = stepKeys(!!me.data?.hosted);
  const [step, setStep] = useState<number | null>(null);
  // resume at the first step with no done/skipped mark (wait for `me` so
  // the hosted/self-host step list is settled before indexing into it)
  useEffect(() => {
    if (step === null && d && !me.isPending) {
      const first = keys.findIndex((k) => !d.wizard_steps?.[k]);
      // a FINISHED wizard has nothing to resume — arriving here anyway
      // (React Router keeps the pre-login path, so a sign-in that starts
      // on /welcome lands back on /welcome: stale bookmark, old /setup
      // redirect, a seeded demo instance) would park on a dead "step 7 of
      // 7 — Done" screen. Bounce to Today instead. Mid-walk states (all steps
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
  // step 2's footer is gated by the SAME readiness BackfillGate reads
  // (shared query key, so this is one poll, not two): while the backfill
  // is still pulling history there is no Continue and no Skip — a
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
  // The wizard connects a bank and configures the instance's mail, both
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
  const advance = (state: "done" | "skipped") => {
    mark.mutate({ key: keys[idx], state });
    setStep(Math.min(idx + 1, keys.length - 1));
  };
  // the Finish step's recap reads budgets from ["settings"]; without this
  // it shows the pre-save seed over the numbers just saved
  const refreshBadges = () => {
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["settings"] });
  };

  const steps: { title: string; blurb: string;
                 primary: { label: string; disabled?: boolean;
                            hint?: string } | null;
                 skippable: boolean; body: ReactNode }[] = [
    { title: "Connect your accounts",
      blurb: "Link every bank you use — the grid shows what's already " +
             "connected. When you've added everything you plan to, say so " +
             "below. (File imports alone work completely too.)",
      // files-only households have no connection but DO have transactions
      // after an inline import — that also counts as "connected enough"
      primary: { label: "Done connecting →",
                 disabled: (d?.connections ?? 0) === 0
                   && (d?.transactions ?? 0) === 0,
                 hint: (d?.connections ?? 0) === 0
                       && (d?.transactions ?? 0) === 0
                   ? "connect at least one source, or skip" : undefined },
      skippable: true,
      body: <ConnectHub /> },
    { title: "Confirm bills & income",
      blurb: "The finder already scanned your ledger for recurring bills " +
             "and paychecks. Approve what's right, dismiss what isn't — " +
             "approved (and still-pending) income seeds the next step's " +
             "budget. Continue any time and revisit later.",
      // detection AND the footer wait for the full Plaid backfill — see
      // BackfillGate and backfillReady above
      primary: backfillReady ? { label: "Continue →" } : null,
      skippable: backfillReady,
      body: <BackfillGate><Bills autoDetect /></BackfillGate> },
    { title: "Set your budgets",
      blurb: "Your monthly plan, pre-filled from your real data: income " +
             "and bills from the last step, spending budgets from your " +
             "history, and what's left as savings. The daily verdict only " +
             "judges the variable spending.",
      primary: { label: "Continue →", disabled: !d?.budgets_set,
                 hint: d?.budgets_set ? undefined : "save budgets first" },
      skippable: true,
      body: <BudgetsStep onSaved={refreshBadges} /> },
    // self-hosted only — hosted deploys send mail via the operator account
    // (this MUST mirror stepKeys(): same condition, same position)
    ...(me.data?.hosted ? [] : [{
      title: "Daily email delivery",
      blurb: "Optional: give Oikonome an outbound email (SMTP) account so " +
             "the daily verdict and alerts can reach your inbox. Guides " +
             "below for the common providers.",
      primary: { label: "Continue →" } as { label: string;
                 disabled?: boolean; hint?: string } | null,
      skippable: true,
      body: <EmailStep /> as ReactNode }]),
    { title: "Done",
      blurb: "That's everything. The Today page carries your daily verdict " +
             "from here on.",
      primary: null, skippable: false,
      body: <FinishStep d={d} /> },
  ];
  const s = steps[idx];
  const last = idx === steps.length - 1;

  return (
    <>
      <div className="card" style={{ marginBottom: "1rem" }}>
        {/* one progress row: dots · where you are · the way out. A
            full-width h1, an exit sentence dressed as a button and a
            separate dots line would be three rows of chrome above a form
            whose own title then repeats itself. */}
        <div className="wiz">
          {steps.map((x, i) => {
            const done = !!d?.wizard_steps?.[keys[i]];
            return (
              <i key={x.title} title={`${i + 1}. ${x.title}`}
                 className={i === idx ? "on" : done ? "done" : ""} />
            );
          })}
          <span style={{ marginLeft: ".4rem" }}>
            Step {idx + 1} of {steps.length} · <b>{s.title}</b></span>
          <button className="linklike mut"
            style={{ marginLeft: "auto", background: "none", border: "none",
                     cursor: "pointer", fontSize: 12 }}
            onClick={() => { leftWizard(me.data?.tenant_id); nav("/"); }}
            title="your answers so far are saved; the nav keeps a
                   “Finish setup” door">
            save &amp; finish later</button>
        </div>
        <h2 style={{ margin: ".1rem 0 .3rem", fontSize: "1.1rem" }}>
          {s.title}</h2>
        <p className="mut" style={{ margin: ".7rem 0 0" }}>{s.blurb}</p>
        {/* the step's content lives INSIDE the wizard pane — one big card */}
        <div style={{ marginTop: "1rem", paddingTop: "1rem",
                      borderTop: "1px solid var(--line)" }}>
          {s.body}
        </div>
        {/* nav at the very bottom right: Skip · Back · Continue */}
        <div style={{ display: "flex", justifyContent: "flex-end",
                      alignItems: "center", gap: ".8rem",
                      marginTop: "1.2rem", paddingTop: ".9rem",
                      borderTop: "1px solid var(--line)" }}>
          {s.skippable && (
            <button className="linklike mut" style={{ background: "none",
                border: "none", cursor: "pointer", fontSize: 13 }}
              onClick={() => advance("skipped")}>
              Skip this step</button>
          )}
          <button disabled={idx === 0} onClick={() => setStep(idx - 1)}>
            ← Back</button>
          {s.primary && (
            <span title={s.primary.hint || ""}>
              <button className="pri" style={fwdBtn}
                disabled={s.primary.disabled}
                onClick={() => advance("done")}>
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
