import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState,
         type ReactNode } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router";
import { MxFlow, PlaidFlow, SimplefinFlow } from "../components/ConnectHub";
import DemoLock from "../components/DemoLock";
import { api, errText, mmdd, MOBILE_HOMES, WEB_HOMES,
         type ApiToken, type Invite, type Me, type Member,
         type MobileDevice, type NotifyStatus, type Session,
         type Settings as SettingsData } from "../api/client";
import { patchQuery, removeFromList, saveSettings } from "../api/cache";
import ManageAccounts from "../components/ManageAccounts";
import Doctor from "./Doctor";
import Rules from "./Rules";
import Merchants from "./Merchants";
import { canEdit, isOwner, isViewer } from "../role";
import { ext } from "../ext";

// the add-on's account card, fixed for the life of the bundle
const AccountCard = ext.account?.Card;
import { deviceZone } from "../devicezone";
import { useNarrow } from "../narrow";
import { browserPushBlocked, disableBrowserPush, enableBrowserPush,
         ownPushEndpoint as ownPushEndpointShared } from "../push";

// one section at a time, behind its own route — a section list on
// the left, that section's cards on the right (spec:
// specs/settings-two-pane.md). A single long scroll behind a chip bar
// cannot carry navigation for twenty-odd cards and a hundred controls,
// which is why the sections are routes.
const SECS = [
  // Order is the order of a person's attention, not the order these were
  // built. Where the money comes from, then what we send
  // about it, then who else sees it, then what is stored, then the engine
  // that sorts it — and Security last, because that is where the password,
  // the second factor and the delete button are, and a destructive control
  // should never be the thing you land next to.
  { id: "connections", label: "Connections", title: "Connections" },
  // the id stays "email" for deep links; the section owns every channel
  // the summaries go out on — email, push, SMS
  { id: "email", label: "Email & Push", title: "Email & Push" },
  // labelled Users; the id stays "household" for deep links
  { id: "household", label: "Users", title: "Users" },
  // how the app presents itself to this household: which page each app
  // opens on. Not delivery, not data — its own small subject.
  { id: "prefs", label: "Preferences", title: "Preferences" },
  { id: "data", label: "Data", title: "Data & maintenance" },
  // Self-host only — hosted instance health is operator-owned. Next to Data
  // for the same reason: both are about the box, not the budget.
  { id: "system", label: "System", title: "System health" },
  // merged `llm` + `rules`: both answer "how does a transaction get
  // its category", and neither filled a destination alone
  { id: "categorize", label: "Categorization", title: "Categorization" },
  // Merchants is a Settings page of its own: who a payee
  // is — names, merges, logos, where it has been seen. The full catalog
  // renders here; /merchants stays as the deep link.
  { id: "merchants", label: "Merchants", title: "Merchants" },
  // the optional LLM is its OWN subject: categorization works without it
  // thanks to the local classifier, and the
  // endpoint also powers the Assistant — filing it under Categorization
  // undersold half of what it does.
  { id: "ai", label: "AI", title: "AI" },
  // Both guided walkthroughs are re-runnable from here, so neither is a
  // one-way door: Welcome's nav pill disappears once setup is marked done
  // and Retire's wizard only offers itself on a first visit, so someone who
  // skipped early, or who wants to redo it after a life change, would
  // otherwise have no route back short of editing config. Near the end
  // because re-running a walkthrough is a rare, deliberate act, not a
  // place to start.
  // no budgets — the Budget page owns the plan, its fine-tuning,
  // income scenarios and the bill tweaks that feed it.
  { id: "wizards", label: "Wizards", title: "Wizards" },
  // The installed add-on's own section, if it has one — the account's
  // contract, visited rarely and deliberately, so late in the list, before
  // Security. The id stays "billing" for deep links and return URLs.
  ...(ext.account
    ? [{ id: "billing", label: ext.account.label, title: ext.account.label }]
    : []),
  { id: "security", label: "Security", title: "Security" },
];

// Deep links minted by older emails, docs and the browser histories of
// anyone who bookmarked a section. They cost four lines here, and a
// 404-feeling dead scroll if left out.
const OLD_SEC: Record<string, string> = {
  setup: "wizards", planning: "wizards", llm: "ai",
  rules: "categorize",
  // "general" is a retired id — cheap to honor, and it was a real URL
  general: "wizards",
};

// This browser's own web-push endpoint, for the posture changes that drop
// every OTHER browser's subscription. `serviceWorker.ready` never rejects
// — with no registration (a proxy served sw.js wrong, private browsing) it
// simply never resolves — so the wait is capped: the security action must
// go through even when the answer is "none".
const ownPushEndpoint = ownPushEndpointShared;

function SettingsSide({ items, show, active, summary, top, filter, setFilter,
                        onPick }: {
  items: typeof SECS; show: Record<string, boolean>; active: string;
  summary: Record<string, { text: string; warn?: boolean } | undefined>;
  top: number;
  filter: string; setFilter: (v: string) => void; onPick: () => void;
}) {
  return (
    <aside className="setnav" style={{ top }}>
      <input className="settings-filter" type="search"
             placeholder="Search settings…" value={filter}
             onChange={(e) => setFilter(e.target.value)} />
      {items.map((s) => (
        <NavLink key={s.id} to={"/settings/" + s.id} onClick={onPick}
                 className={(active === s.id ? "on" : "")
                            + (show[s.id] ? "" : " off")}>
          <span className="setnav-l">{s.label}</span>
          {summary[s.id] && (
            <span className={"setnav-s" + (summary[s.id]!.warn ? " warn" : "")}>
              {summary[s.id]!.warn && "⚠ "}{summary[s.id]!.text}</span>)}
        </NavLink>
      ))}
    </aside>
  );
}

// A section mounts the first time it is shown and then stays mounted,
// hidden with display:none, so a card's half-typed form survives moving
// between sections. It does NOT mount before that: mounting every section
// on entry would fire every card's query with it — thirty-odd requests for
// sections nobody is looking at — so only the one in the URL (and, while
// searching, the ones that match) costs anything.
function Sec({ id, show, children }: {
  id: string; show: boolean; children: ReactNode;
}) {
  const opened = useRef(show);
  if (show) opened.current = true;
  return (
    <section className="sgrp" data-sec={id} id={"sec-" + id}
             style={{ display: show ? undefined : "none" }}>
      <h2 className="ssec">{SECS.find((s) => s.id === id)!.title}</h2>
      {opened.current && children}
    </section>
  );
}

// display:none (not unmount) so a card's half-typed form survives
// filtering, and moving between sections too
function Show({ on, children }: { on: boolean; children: ReactNode }) {
  return <div style={on ? undefined : { display: "none" }}>{children}</div>;
}

function NoHits() {
  return (
    <p className="mut" style={{ marginTop: "1.2rem" }}>
      Nothing matches that. Search covers every setting's name and its
      keywords — try <code>smtp</code>, <code>2fa</code>, <code>export</code>.
    </p>
  );
}

export default function Settings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  interface Sched {
    daily: { on: boolean; hour: number; sms: boolean; push: boolean;
             summary: boolean };
    weekly: { on: boolean; hour: number; weekday: number;
              sms: boolean; push: boolean };
    monthly: { on: boolean; hour: number; sms: boolean; push: boolean };
    yearly: { on: boolean; hour: number; sms: boolean; push: boolean };
  }
  const [form, setForm] = useState<{
    // a list, not a comma-joined string — the editor adds and
    // removes whole addresses, and the round trip through a joined string
    // is where "a, b" vs "a,b" bugs come from
    recipients: string[]; muted: string[]; sched: Sched;
    // "" = follow the instance's zone (the server reads "" as clear)
    timezone: string;
  } | null>(null);
  // A failure and a success must not render in the same amber note, nor a
  // failure as the raw error string — status code, JSON braces and all.
  // The flag is what lets them look different.
  const [notice, setNotice] = useState<{ text: string; bad?: boolean } | null>(
    null);
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  // whether this household has the add-on's section (a hook of the add-on's,
  // fixed for the life of the bundle, so calling it here is unconditional)
  const useAccountSection = ext.account?.useEnabled ?? (() => false);
  const billingOn = useAccountSection();
  // Same key ConnectionsCard already uses, so the list subtitle rides its
  // response instead of asking again. Viewers can't read it — and don't get
  // the section either.
  // these three feed subtitles for sections a member does not get, so
  // asking for them buys nothing — and the invite list is owner-only
  // server-side, so for a member it buys a 403
  const connQ = useQuery({ queryKey: ["connections"], queryFn: api.connections,
                           enabled: isOwner(meQ) });
  // Every section that can honestly state its current shape gets a
  // subtitle, so the pane answers "what is my setup?" for the whole page
  // rather than part of it. These are the same keys the cards inside
  // already use, so React Query serves them from cache; no extra request.
  const membersQ = useQuery({ queryKey: ["members"], queryFn: api.members });
  const invitesQ = useQuery({ queryKey: ["invites"], queryFn: api.invites,
                              enabled: isOwner(meQ) });
  const passkeyQ = useQuery({ queryKey: ["passkeys"], queryFn: api.passkeys });
  const onbQ = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding });
  // Doctor is self-host only, and it already computes the verdict the System
  // subtitle needs — the card below shares this exact cache entry. Must sit
  // with the other hooks: the early returns further down are conditional.
  const doctorQ = useQuery({ queryKey: ["doctor"], queryFn: api.doctor,
                             enabled: !meQ.data?.hosted
                                      && isOwner(meQ) });

  // the section list is sticky under the top bar, so it needs that
  // bar's height measured.
  const [filter, setFilter] = useState("");
  const [topOff, setTopOff] = useState(52);
  useEffect(() => {
    const measure = () => {
      // What sits across the top of the content column is NOT header.top
      // above 760px: the layout turns that element into a full-height left
      // rail, so its offsetHeight is the viewport. There the strip over the
      // top edge is .rt (fixed, 46px); on phones .rt is a plain flex child
      // inside the header bar, so the header is still the answer.
      const rt = document.querySelector<HTMLElement>(".rt");
      const bar = rt && getComputedStyle(rt).position === "fixed"
        ? rt : document.querySelector<HTMLElement>("header.top");
      setTopOff(bar?.offsetHeight ?? 0);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  // Phones get the two panes as two SCREENS: the list is the index, picking a
  // section replaces it, and a back link returns. Two 13rem columns on a
  // 390px screen would be neither a list nor a settings page.
  const narrow = useNarrow();

  // The active section is the URL, not state: /app/settings/email deep-links,
  // Back works, and a bookmark survives. Older hash links (#sec-llm) are
  // translated once on arrival.
  const nav = useNavigate();
  const { sec: secParam } = useParams<{ sec?: string }>();
  useEffect(() => {
    const h = window.location.hash.replace(/^#sec-/, "");
    if (h && h !== window.location.hash) {
      nav("/settings/" + (OLD_SEC[h] ?? h), { replace: true });
      return;
    }
    if (secParam && OLD_SEC[secParam])
      nav("/settings/" + OLD_SEC[secParam], { replace: true });
    // Inbound links to an add-on's account section carry no section — a
    // payment return lands on /app/settings?billing=success, and the mail
    // that leads there points at the same place. With no section in the
    // URL the page falls back to ids[0], which is Connections, so the
    // banner would attach to the wrong card. Send those to the account
    // section, keeping the query so the banner still renders.
    if (!secParam
        && new URLSearchParams(window.location.search).has("billing"))
      nav("/settings/billing" + window.location.search, { replace: true });
  }, [secParam, nav]);

  // One-shot flag: a FAILED auto-save must snap the form back to server
  // truth once the refetch lands. The seed effect otherwise only fires
  // while form is null, so without this a rejected change stays on screen
  // looking saved — and gating on the flag (not seeding on every fetch)
  // is what keeps routine background refetches from clobbering a form
  // someone is mid-edit in.
  const emailResync = useRef(false);
  useEffect(() => {
    if (q.data && (form === null || emailResync.current)) {
      emailResync.current = false;
      const d = q.data;
      setForm({
        recipients: d.email_recipients ?? [],
        muted: d.email_muted ?? [],
        timezone: d.timezone ?? "",
        sched: {
          daily: { on: d.email_schedule?.daily?.on ?? true,
                   hour: d.email_schedule?.daily?.hour ?? 7,
                   sms: d.email_schedule?.daily?.sms ?? false,
                   push: d.email_schedule?.daily?.push ?? false,
                   summary: d.email_schedule?.daily?.summary ?? false },
          weekly: { on: d.email_schedule?.weekly?.on ?? false,
                    hour: d.email_schedule?.weekly?.hour ?? 7,
                    weekday: d.email_schedule?.weekly?.weekday ?? 0,
                    sms: d.email_schedule?.weekly?.sms ?? false,
                    push: d.email_schedule?.weekly?.push ?? false },
          monthly: { on: d.email_schedule?.monthly?.on ?? false,
                     hour: d.email_schedule?.monthly?.hour ?? 7,
                     sms: d.email_schedule?.monthly?.sms ?? false,
                     push: d.email_schedule?.monthly?.push ?? false },
          yearly: { on: d.email_schedule?.yearly?.on ?? false,
                    hour: d.email_schedule?.yearly?.hour ?? 7,
                    sms: d.email_schedule?.yearly?.sms ?? false,
                    push: d.email_schedule?.yearly?.push ?? false },
        },
      });
    }
    // dataUpdatedAt is a dep because a rejected save refetches UNCHANGED
    // data — React Query's structural sharing then hands back the same
    // q.data reference and the effect would never re-run on it.
  }, [q.data, q.dataUpdatedAt, form]);

  // The Budget page owns budgets, so email is all this form carries
  // (settingsSave bodies are partial anyway).
  // AUTO-SAVE: a change IS the save — adds, removes,
  // mutes and cadence toggles post as they happen; there is no Save
  // button to remember. Every mutate carries the COMPLETE next form, so
  // rapid changes are last-write-wins under the server's settings lock,
  // and an error refetches so the controls snap back to server truth
  // instead of showing a change that didn't take.
  type EmailForm = NonNullable<typeof form>;
  const saveEmail = useMutation({
    // saveSettings seeds ["settings"] with the view the server returns, so
    // the recipient badges flip on the response instead of a second fetch
    mutationFn: (f: EmailForm) => saveSettings(qc, {
      email_recipients: f.recipients, email_muted: f.muted,
      email_schedule: f.sched, timezone: f.timezone }),
    // what the schedule and zone were BEFORE the write, so success can
    // tell a recipient edit (moves nothing else) from a cadence or zone
    // change (moves the verdict and the calendar)
    onMutate: () => {
      const s = qc.getQueryData<SettingsData>(["settings"]);
      return { sched: JSON.stringify(s?.email_schedule ?? null),
               tz: s?.timezone ?? "" };
    },
    onSuccess: (v, _f, prev) => {
      setNotice({ text: "Saved." });
      if (JSON.stringify(v.email_schedule ?? null) !== prev?.sched
          || (v.timezone ?? "") !== prev?.tz) {
        qc.invalidateQueries({ queryKey: ["today"] });
        qc.invalidateQueries({ queryKey: ["calendar"] });
      }
    },
    onError: (e) => {
      setNotice({ text: errText(e), bad: true });
      emailResync.current = true;
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  const applyEmail = (next: EmailForm) => {
    // a newer edit supersedes a pending snap-back — its own save decides
    // again, so a stale refetch must not overwrite what was just typed
    emailResync.current = false;
    setForm(next);
    saveEmail.mutate(next);
  };
  // "Send today's email now" lives IN the Daily email card, not under
  // Maintenance: it belongs beside the list of people it goes to.
  // Same recipients rules as the scheduled send;
  // forces the email channel, never SMS/push, never the heartbeat.
  const sendNow = useMutation({
    mutationFn: api.jobsEmail,
    onSuccess: (r) => setNotice({ text: `Sent to your recipients: ${r.subject}` }),
    onError: (e) => setNotice({ text: errText(e), bad: true }),
  });

  if (q.isPending || form === null)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't load settings: {String(q.error)}</div>;

  // card-level filter hits — title + hand-picked keywords per card,
  // section shows while any of its cards match
  const q2 = filter.trim().toLowerCase();
  const hit = (words: string) => q2 === "" || words.toLowerCase().includes(q2);
  const h = {
    daily: hit("daily email verdict recipients schedule cadence weekly monthly report send email now"),
    smtp: hit("email delivery smtp host port password sender test"),
    conn: hit("connections providers plaid mx simplefin bank keys client "
              + "secret scripts manage accounts rename hide remove shared "
              + "add link reconnect recurring streams enrich"),
    tokens: hit("script tokens api bearer collectors community push import"),
    llm: hit("ai assistant smart categorization llm ollama openai model endpoint api key extra body vision receipts"),
    setup: hit("setup wizard walkthrough guided onboarding welcome tour "
               + "re-run rerun again restart redo first run getting started "
               // the retirement and business wizards live in this card, so
               // "retirement" has to find it here — the word must not stop
               // finding anything just because the read-only inputs moved
               + "retirement retire business birthdate"),
    family: hit("users family household members invite viewer role"),
    email: hit("change email username login address account"),
    password: hit("change password current new"),
    totp: hit("two-factor authentication totp authenticator code 2fa"),
    passkeys: hit("passkeys webauthn security key fingerprint face touch"),
    sessions: hit("sessions devices signed in sign out"),
    support: hit("support access grant operator help data consent"),
    deleteacct: hit("delete account erase data close leave remove everything"),
    maint: hit("maintenance jobs run sync now"),
    data: hit("your data export csv dump database backup restore"),
    move: hit("move fresh environment bundle passphrase connections backup import"),
    feedback: hit("feedback bug report reports"),
    billing: !!ext.account && hit(ext.account.keywords),
    rules: hit("rules categorization learn model seed correct disable"),
    merchants: hit("merchant merchants payee rename merge logos icons minimal location map where"),
    system: hit("system health doctor checks diagnostics collectors scripts bundle"),
    // the theme words point here too: the toggle itself lives in the nav,
    // but Preferences is where someone looking for appearance settings goes
    // the web has no theme card here — the toggle lives in the nav — so only
    // the home-page words: a hit on "dark" would open an empty section
    prefs: hit("preferences home page default landing screen opens"),
  };
  const hosted = !!meQ.data?.hosted;
  // Does this section hold anything the search matched? Every card the
  // section renders has to appear here: a card missing from this map hides
  // the whole section around the one card the search actually matched.
  const secShow: Record<string, boolean> = {
    wizards: h.setup,
    billing: billingOn && h.billing,
    email: h.daily || h.smtp,
    connections: h.conn || h.tokens,
    categorize: h.rules,
    merchants: h.merchants,
    ai: h.llm,
    household: h.family || (hosted && h.billing),
    prefs: h.prefs,
    data: h.maint || h.data || h.move || h.feedback,
    security: h.email || h.password || h.totp || h.passkeys || h.sessions
              || h.support || h.deleteacct,
    // Hosted: no system-health section — the host runs the box
    system: !hosted && h.system,
  };

  // Which sections this instance has at all (hosted/self-host, billing on).
  // a viewer has an account and a household roster and nothing else
  // — every tenant-config section is owner chrome (writes 403 server-side).
  const viewer = isViewer(meQ);
  const owner = isOwner(meQ);
  // A member gets the sections that hold the household's MONEY — rules,
  // merchants, the roster, maintenance, their own account. The ones left
  // out are the account rather than the money: bank connections and their
  // keys, the AI endpoint, exports and backups, billing, the instance's
  // own health, and the setup wizard that walks through connecting a bank.
  // Every one of them would 403 on save, and a settings field that quietly
  // does nothing is worse than one that is not there.
  const MEMBER_SECS = ["email", "categorize", "merchants", "household",
                       "data", "security"];
  if (!owner && !viewer)
    for (const k of Object.keys(secShow))
      if (!MEMBER_SECS.includes(k)) secShow[k] = false;
  const ids = (viewer ? ["email", "household", "security"]
               : owner ? SECS.map((s) => s.id) : MEMBER_SECS)
    .filter((id) => (id !== "billing" || billingOn)
                    && (id !== "system" || !hosted));
  // Searching shows every section that matched, stacked — the results ARE
  // the page, so a hit in a section you weren't looking at is still findable.
  // Otherwise exactly one section renders: the one in the URL. On a phone
  // with no section in the URL, none does, and the list stands alone.
  const searching = filter.trim() !== "";
  // A URL naming a section this instance does NOT have — /settings/system on
  // hosted, /settings/billing on self-host — must not silently render a
  // different section while the address bar goes on claiming the first one.
  // Say so instead; the link is usually from a doc or an old bookmark, and
  // "that section doesn't exist here" is the answer.
  const missingSec = !!secParam && !ids.includes(secParam)
    && !OLD_SEC[secParam];
  const active = secParam && ids.includes(secParam) ? secParam
                 : missingSec ? "" : narrow ? "" : ids[0];
  const secOn = (id: string) => searching ? !!secShow[id] : id === active;

  // Left-list subtitles: current state, read off data already loaded — never
  // a second request, and never a guess. A section with nothing honest to
  // say gets no subtitle rather than a vague one.
  const hourLabel = (n: number) =>
    `${((n + 11) % 12) + 1}${n < 12 ? "am" : "pm"}`;
  const nRecip = (q.data.email_recipients ?? []).length;
  const nConn = connQ.data?.connections?.length ?? 0;
  // A connection that stopped working is the page's most actionable fact,
  // so it must not be reachable only by opening the section. It renders
  // amber here AND at the top of the section that owns it — findable from
  // anywhere without a floating banner that could appear over any section.
  const nBad = (connQ.data?.connections ?? [])
    .filter((c) => c.status && c.status !== "ok" && c.status !== "healthy")
    .length;
  const nUsers = membersQ.data?.users?.length ?? 0;
  const nInv = (invitesQ.data?.invites ?? []).length;
  const nKeys = (passkeyQ.data?.passkeys ?? []).length;
  const twoFA = !!meQ.data?.totp_enabled;
  const nTxn = onbQ.data?.transactions ?? 0;

  // A subtitle may state a PROBLEM, which is what turns this list into the
  // page's status board. `warn` marks the ones that do.
  const summary: Record<string, { text: string; warn?: boolean } | undefined> = {
    // A delivery failure visible only inside this section would be in the
    // one place nobody looks, since the symptom is "no mail arrived". It
    // states itself here instead.
    email: meQ.data?.email_delivery
      ? { text: meQ.data.email_delivery.state === "complained"
                ? "reported as spam — paused" : "not arriving — paused",
          warn: true }
      : { text: form.sched.daily.on
      ? `daily ${hourLabel(form.sched.daily.hour)}`
        + (nRecip ? ` · +${nRecip} recipient${nRecip > 1 ? "s" : ""}` : "")
      : "daily email off" },
    connections: connQ.data
      ? (nBad ? { text: `${nConn} linked · ${nBad} needs attention`, warn: true }
         : { text: nConn ? `${nConn} linked` : "nothing linked" })
      : undefined,
    // ONLY the tenant's own model. An empty llm_model does not mean "none":
    // llm_categorize falls back to an env-provided backend (how the bundled
    // Ollama is wired), which this payload cannot see — so saying "no model
    // set" here would be false on every standard install.
    ai: q.data.llm_model ? { text: q.data.llm_model }
      : { text: "no endpoint — built-in only" },
    billing: ext.account?.summary?.(meQ.data?.plan),
    household: membersQ.data
      ? { text: `${nUsers} ${nUsers === 1 ? "person" : "people"}`
                + (nInv ? ` · ${nInv} invited` : "") }
      : undefined,
    // What is stored — the honest answer to "what's in here", and the only
    // number the Data section is really about.
    data: onbQ.data
      ? { text: `${nTxn.toLocaleString()} transaction${nTxn === 1 ? "" : "s"}` }
      : undefined,
    // System is the Doctor's own verdict; it already computes it.
    system: doctorQ.data
      ? (doctorQ.data.ok ? { text: "all checks passing" }
         : { text: (() => {
               const n = doctorQ.data.checks.filter((c) => !c.ok).length;
               return `${n} check${n === 1 ? "" : "s"} failing`;
             })(), warn: true })
      : undefined,
    wizards: onbQ.data
      ? { text: onbQ.data.wizard_done ? "setup complete" : "setup unfinished" }
      : undefined,
    // No second factor AT ALL is a problem worth saying out loud, in the
    // one place someone scanning their setup will see it. A passkey IS a
    // second factor — password + passkey is the model working as intended,
    // so it must not read as a warning: a fresh passkey signup seeing
    // "⚠ 2FA off · 1 passkey" would take it for an error.
    security: meQ.data
      ? (twoFA
         ? { text: `2FA on${nKeys ? ` · ${nKeys} passkey${nKeys > 1 ? "s" : ""}` : ""}` }
         : nKeys
           ? { text: `${nKeys} passkey${nKeys > 1 ? "s" : ""}` }
           : { text: "no 2FA or passkey", warn: true })
      : undefined,
  };

  // viewers get their own account + the household roster; every
  // tenant-config card is owner chrome (writes are 403'd server-side too)
  // The list and the body are two panes on a desktop and two screens on a
  // phone: there, the list stands alone until you pick something, and the
  // body replaces it behind a back link.
  // The search box lives in the list pane, so on a phone the list has to
  // survive typing in it. Were `searching` alone to decide, it would flip
  // true on the first keystroke and unmount the pane holding the focused
  // input — one character in, the box vanishes and the keyboard closes.
  // Keep the list up while a search is running, and let the results below
  // it be the answer; the body pane waits until a section is picked.
  const showSide = !narrow || !active || searching;
  const showBody = !narrow || (active !== "" && !searching);
  const side = (
    <SettingsSide items={SECS.filter((s) => ids.includes(s.id))}
                  show={secShow} active={searching ? "" : active}
                  summary={summary} top={topOff}
                  filter={filter} setFilter={setFilter}
                  onPick={() => window.scrollTo(0, 0)} />
  );
  const back = narrow && (
    <Link className="setback" to="/settings"
          onClick={() => setFilter("")}>‹ All settings</Link>
  );

  if (viewer)
    return (
      <>
        <h1>Settings</h1>
        <p className="mut">You have view-only access — the budget and instance
          configuration are managed by the owner.</p>
        <div className="setpane">
          {showSide && side}
          {showBody && (
          <div className="setbody">
            {back}
            {missingSec && (
              <div className="note" style={{ marginTop: 0 }}>
                This instance has no <b>{secParam}</b> settings section —
                it belongs to the {secParam === "system" ? "self-hosted"
                  : "hosted"} setup. Pick one from the list.
              </div>
            )}
            {/* a member's own delivery: on/off for THEIR inbox; the
                schedule is the owner's */}
            <Sec id="email" show={secOn("email")}>
              <MyEmailCard />
            </Sec>
            <Sec id="household" show={secOn("household")}>
              <Show on={h.family}><FamilyCard /></Show>
            </Sec>
            <Sec id="security" show={secOn("security")}>
              <Show on={h.password}><PasswordCard /></Show>
              <Show on={h.totp}><TwoFactorCard /></Show>
              <Show on={h.passkeys}><PasskeysCard /></Show>
              {/* demo: the account is shared, so the session list is a roster
                  of strangers — hidden, and the API returns none either way */}
              {!meQ.data?.demo && <Show on={h.sessions}><SessionsCard /></Show>}
              <LeaveHouseholdCard />
            </Sec>
            {searching && !ids.some((id) => secShow[id]) && <NoHits />}
          </div>)}
        </div>
      </>
    );

  // The demo instance is READ-ONLY: its credentials are shared and printed
  // publicly, so one visitor's edit would reconfigure the instance for
  // everyone after them. Rather than lock cards one by one (a per-card
  // DemoLock keeps missing new cards as they are added),
  // the whole page is greyed out under a single banner — and the server
  // refuses every settings write regardless, so this can't drift out of
  // being true.
  const demo = !!meQ.data?.demo;

  return (
    <>
      <h1>Settings</h1>
      {demo && (
        <div className="note" style={{ borderColor: "var(--amber)",
              color: "var(--amber)" }}>
          <b>Demo instance — settings are read-only.</b> Everything here is
          shown exactly as the real app renders it, but nothing can be
          changed: the login is shared, so a change would follow the next
          visitor. Explore freely; the data is synthetic.
        </div>
      )}
      {notice && <div className={"note" + (notice.bad ? " bad" : " good")}
                      onClick={() => setNotice(null)}>{notice.text}</div>}
      <fieldset disabled={demo} style={demo
          ? { border: 0, margin: 0, padding: 0, opacity: 0.55 }
          : { border: 0, margin: 0, padding: 0 }}>
      <div className="setpane">
      {showSide && side}
      {showBody && (
      <div className="setbody">
      {back}

      {/* The guided walkthroughs, re-runnable. Savings goals live on
          /budget and property & vehicles on /networth; the read-only
          retirement inputs are not here either — the Retire page states
          your SS at your claim age and derives age from the birthdate, and
          the retirement wizard below is where both are set, so a copy here
          could only be read. */}
      <Sec id="wizards" show={secOn("wizards")}>
      {/* the guided walkthrough starts by connecting a bank and ends at
          the instance's mail settings — both the owner's */}
      {owner && (
      <Show on={h.setup}>
      <GuidedSetupCard />
      </Show>
      )}
      </Sec>

      <Sec id="email" show={secOn("email")}>
      {/* before anything about WHAT we send, the answer to whether
          it is arriving. Rendered only when it is not — a permanent "mail
          is fine" badge would be a claim we cannot honestly make (a relay
          accepting a message is not a person reading it), and the section
          should be quiet when there is nothing wrong. */}
      {meQ.data?.email_delivery && (
        <DeliveryProblemCard state={meQ.data.email_delivery}
                             email={meQ.data.email} />)}
      {/* WHO the household's mail goes to, and when, is the owner's —
          the recipient list is a door out of the instance. A member sets
          their own delivery on or off, exactly as a viewer does. */}
      {!owner && <MyEmailCard />}
      {owner && (<>
      <Show on={h.daily}>
      <DemoLock on={meQ.data?.demo}>
      <div className="card">
        <h2>Daily email</h2>
        <p className="mut">One email every morning: the verdict and the
          number to hit today.{hosted
            ? <> We send it — there is nothing to configure.</>
            : <> Needs SMTP configured below or in <code>docker/.env</code> —
                System health shows whether it is.</>}</p>
        <div className="field-grid">
          {/* not a comma-separated text box: such a list can only tell you
              what you typed — never that an address has never been
              confirmed, or that the provider has been refusing it for three
              days. Those are exactly the facts someone adding a recipient
              needs, because the failure mode here is silence. */}
          <RecipientsEditor
            recipients={form.recipients}
            muted={form.muted}
            status={q.data.email_recipient_status ?? []}
            onChange={(recipients) => applyEmail({ ...form, recipients })}
            onMutedChange={(muted) => applyEmail({ ...form, muted })} />
          <CadenceEditor sched={form.sched}
            onChange={(sched) => applyEmail({ ...form, sched })}
            timezone={form.timezone}
            onTimezone={(timezone) => applyEmail({ ...form, timezone })} />
        </div>
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                      marginTop: ".8rem", alignItems: "center" }}>
          {saveEmail.isPending && <span className="sub">saving…</span>}
          <button disabled={sendNow.isPending}
                  title="sends to everyone on the list above, even if the daily toggle is off; SMS/push don't fire and the scheduled send still happens"
                  onClick={() => sendNow.mutate()}>
            {sendNow.isPending ? "sending…" : "Send today's email now"}
          </button>
        </div>
      </div>
      </DemoLock>
      </Show>

      {/* Hosted sends the mail. /api 403s the smtp_* group and withholds
          it from GET, so there is nothing to render and nothing to render
          it from. */}
      {!hosted && (
      <Show on={h.smtp}>
      <DemoLock on={meQ.data?.demo}>
      <SmtpCard data={q.data} onSaved={() => {
        setNotice({ text: "Email delivery settings saved." });
        // the card seeds ["settings"] from the save response itself; the
        // mail-delivery health check is the one other thing that moved
        qc.invalidateQueries({ queryKey: ["doctor"] });
      }} />
      </DemoLock>
      </Show>)}
      </>)}
      </Sec>

      <Sec id="connections" show={secOn("connections")}>
      {owner && (<>
      <Show on={h.conn}>
      <DemoLock on={meQ.data?.demo}>
      <ConnectionsCard data={q.data} hosted={hosted} onSaved={() => {
        setNotice({ text: "Connection settings updated." });
        // the card patches or refetches ["settings"] itself, per action
        qc.invalidateQueries({ queryKey: ["connections"] });
      }} />
      </DemoLock>
      </Show>
      <Show on={h.conn}>
      <DemoLock on={meQ.data?.demo}>
      <div className="card" style={{ marginTop: "1rem" }}>
        <b>Plaid's recurring detection</b>
        <div className="sub" style={{ margin: ".2rem 0 .5rem" }}>
          Plaid detects recurring charges across every spelling of a
          merchant, with more history than this instance may have. Each
          nightly pass offers what it finds as bill proposals beside our
          own — never applied without you.
        </div>
        <label className="check">
          {/* the box is bound to the cached setting, so it flips the
              moment it is clicked and the response re-seeds the view; a
              rejected write says so, puts the old value back and refetches,
              so the checkbox snaps back to server truth */}
          <input type="checkbox" checked={q.data.plaid_recurring !== false}
            onChange={(e) => {
              const prev = q.data.plaid_recurring;
              patchQuery<SettingsData>(qc, ["settings"],
                                       { plaid_recurring: e.target.checked });
              saveSettings(qc, { plaid_recurring: e.target.checked })
                .catch((err) => {
                  setNotice({ text: errText(err), bad: true });
                  patchQuery<SettingsData>(qc, ["settings"],
                                           { plaid_recurring: prev });
                  qc.invalidateQueries({ queryKey: ["settings"] });
                });
            }} />
          Cross-check bills against Plaid's recurring streams
        </label>
      </div>
      </DemoLock>
      <DemoLock on={meQ.data?.demo}>
      <EnrichCard />
      </DemoLock>
      </Show>
      <Show on={h.tokens}>
      <DemoLock on={meQ.data?.demo}>
      <ScriptTokensCard />
      </DemoLock>
      </Show>
      </>)}
      </Sec>

      <Sec id="categorize" show={secOn("categorize")}>
      <Show on={h.rules}>
      <div className="card" style={{ marginTop: "1rem" }}>
        <Rules embedded />
      </div>
      </Show>
      </Sec>

      <Sec id="merchants" show={secOn("merchants")}>
      <Show on={h.merchants}>
      <div className="card" style={{ marginTop: "1rem" }}>
        <b>Merchants</b>
        <div className="sub" style={{ margin: ".2rem 0 .5rem" }}>
          Every name your ledger shows, and the source strings underneath it.
          Rename a merchant, or merge two that are the same shop under
          different names — every change is undoable. Open a merchant for
          its details and where it has been seen.
        </div>
        <label className="check" style={{ marginTop: ".4rem" }}>
          <input type="checkbox" checked={q.data.merchant_logos !== false}
            onChange={(e) => {
              // flips at once in both caches — the avatars everywhere read
              // the flag off ["me"], and that key has observers on every
              // page, so it is patched rather than refetched
              const on = e.target.checked;
              const prev = q.data.merchant_logos;
              patchQuery<SettingsData>(qc, ["settings"], { merchant_logos: on });
              patchQuery<Me>(qc, ["me"], { merchant_logos: on });
              saveSettings(qc, { merchant_logos: on })
                .then((v) => patchQuery<Me>(qc, ["me"],
                  { merchant_logos: v.merchant_logos !== false }))
                .catch((err) => {
                  setNotice({ text: errText(err), bad: true });
                  patchQuery<SettingsData>(qc, ["settings"],
                                           { merchant_logos: prev });
                  patchQuery<Me>(qc, ["me"], { merchant_logos: prev !== false });
                  qc.invalidateQueries({ queryKey: ["settings"] });
                });
            }} />
          Show merchant logos <span className="mut">— on the ledger,
            merchants, spending and accounts; off for a plainer look
            (the daily email never carries images)</span>
        </label>
      </div>
      {/* mounted only while its section is visible, unlike the cards above:
          the catalog is a query of its own, and a hidden mount would fire it
          on every Settings visit for a list nobody is looking at */}
      {secOn("merchants") && h.merchants && <Merchants embedded />}
      </Show>
      </Sec>

      <Sec id="ai" show={secOn("ai")}>
      {owner && (<>
      <Show on={h.llm}>
      {/* the llm_* keys are demo-door keys — the server
          403s them, so show the card disabled-in-place like SMTP */}
      <DemoLock on={meQ.data?.demo}>
      <LlmCard data={q.data} hosted={hosted} onSaved={() => {
        // the card seeds ["settings"] from the save response itself
        setNotice({ text: "LLM settings saved." });
      }} />
      </DemoLock>
      </Show>
      </>)}
      </Sec>

      <Sec id="household" show={secOn("household")}>
      <Show on={h.family}>
      <DemoLock on={meQ.data?.demo}>
      <FamilyCard />
      </DemoLock>
      </Show>
      {ext.householdCards.map((Card, i) => (
      <Show key={i} on={h.billing || h.family}>
      <DemoLock on={meQ.data?.demo}>
      <Card />
      </DemoLock>
      </Show>
      ))}
      </Sec>

      <Sec id="prefs" show={secOn("prefs")}>
        <Show on={h.prefs}><HomePageCard /></Show>
      </Sec>
      <Sec id="data" show={secOn("data")}>
      <Show on={h.maint}>
      <DemoLock on={meQ.data?.demo}>
      <MaintenanceCard onDone={() => {
        qc.invalidateQueries({ queryKey: ["accounts"] });
        qc.invalidateQueries({ queryKey: ["connections"] });
        qc.invalidateQueries({ queryKey: ["today"] });
      }} />
      </DemoLock>
      </Show>

      {owner && (
      <Show on={h.data}>
      <DataExportCard hosted={hosted} />
      </Show>
      )}

      {/* credentials bundle is a SELF-HOST migration tool — hosted owns
          the providers, so there is nothing of the user's to bundle and
          the card would only invite confusion */}
      {!hosted && owner && (
      <Show on={h.move}>
      <DemoLock on={meQ.data?.demo}>
      <ConnectionsBackupCard onImported={() => {
        setNotice({ text: "Connections imported — run a sync to pull accounts." });
        // restores provider config and bank links, and nothing the ledger
        // reads until the sync it asks for — so only those three move
        qc.invalidateQueries({ queryKey: ["settings"] });
        qc.invalidateQueries({ queryKey: ["connections"] });
        qc.invalidateQueries({ queryKey: ["accounts"] });
      }} />
      </DemoLock>
      </Show>
      )}

      {!demo && (
      <Show on={h.feedback}>
      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Feedback &amp; bug reports</h2>
        <p className="mut" style={{ marginBottom: ".5rem" }}>The Feedback
          &amp; bug reports tab (feedback and bug reports to the maintainer,
          with the system-health bundle attached). Turn it off if you'd
          rather not see it.</p>
        <label className="check">
          <input type="checkbox" checked={q.data.feedback_enabled !== false}
            onChange={(e) => {
              const on = e.target.checked;
              const prev = q.data.feedback_enabled;
              patchQuery<SettingsData>(qc, ["settings"], { feedback_enabled: on });
              saveSettings(qc, { feedback_enabled: on })
                .then(() => {
                  // the Feedback page keys its own gate off ["testing"]
                  patchQuery(qc, ["testing"], { enabled: on });
                  qc.invalidateQueries({ queryKey: ["testing"] });
                })
                .catch((err) => {
                  setNotice({ text: errText(err), bad: true });
                  patchQuery<SettingsData>(qc, ["settings"],
                                           { feedback_enabled: prev });
                  qc.invalidateQueries({ queryKey: ["settings"] });
                });
            }} />
          Feedback enabled on this instance
        </label>
      </div>
      </Show>
      )}
      </Sec>

      {!hosted && owner && (
      <Sec id="system" show={secOn("system")}>
      {/* NOT wrapped in a card: Doctor renders one card per check group, and
          a wrapper makes the whole stack of them read as a single wall.

          MOUNTED ONLY WHILE SHOWN, unlike every other section. `Sec` hides
          with display:none rather than unmounting, deliberately, so a
          half-typed form survives moving between sections — but Doctor
          holds no form and polls at 10s and 30s, so staying mounted would
          run two health sweeps a minute for as long as Settings is open, no
          matter which section is actually being read. */}
      {secOn("system") && (
      <Show on={h.system}>
      <Doctor embedded />
      </Show>
      )}
      </Sec>
      )}

      {/* demo creds are shared/printed — password, 2FA and
          passkey changes could only lock the next visitor out */}
      {billingOn && owner && AccountCard && (
      <Sec id="billing" show={secOn("billing")}>
      <Show on={h.billing}>
      <DemoLock on={meQ.data?.demo}>
      <AccountCard />
      </DemoLock>
      </Show>
      </Sec>
      )}

      <Sec id="security" show={secOn("security")}>
      <Show on={h.email}>
      <DemoLock on={meQ.data?.demo}>
      <EmailCard current={meQ.data?.email} onChanged={() => {
        // the card patches ["me"] from the response; what a login
        // rotation ALSO empties are these lists, and they live on this
        // same page
        qc.invalidateQueries({ queryKey: ["sessions"] });
        qc.invalidateQueries({ queryKey: ["passkeys"] });
        qc.invalidateQueries({ queryKey: ["devices"] });
        qc.invalidateQueries({ queryKey: ["tokens"] });
        qc.invalidateQueries({ queryKey: ["invites"] });
      }} />
      </DemoLock>
      </Show>
      <Show on={h.password}>
      <DemoLock on={meQ.data?.demo}>
      <PasswordCard />
      </DemoLock>
      </Show>
      <Show on={h.totp}>
      <DemoLock on={meQ.data?.demo}>
      <TwoFactorCard />
      </DemoLock>
      </Show>
      <Show on={h.passkeys}>
      <DemoLock on={meQ.data?.demo}>
      <PasskeysCard />
      </DemoLock>
      </Show>
      {/* demo: shared account → the session list is other visitors'
          browsers and activity; hidden here, empty from the API */}
      {!demo && (
      <Show on={h.sessions}>
      <SessionsCard />
      </Show>
      )}
      {meQ.data?.hosted && (
      <Show on={h.support}>
      <DemoLock on={meQ.data?.demo}>
      <SupportAccessCard owner={meQ.data?.role === "owner"} />
      </DemoLock>
      </Show>
      )}
      <Show on={h.deleteacct}>
      <DemoLock on={meQ.data?.demo}>
      <DeleteAccountCard owner={meQ.data?.role === "owner"} />
      </DemoLock>
      </Show>
      {/* the member's counterpart to Delete account: it removes THEIR
          login and nothing else. A viewer has always had it (their
          Settings renders it below); a member needs the same way out. */}
      {!owner && <LeaveHouseholdCard />}
      </Sec>

      {searching && !ids.some((id) => secShow[id]) && <NoHits />}
      </div>)}
      </div>
      </fieldset>
    </>
  );
}

// script tokens — bearer credentials for host-side collector
// scripts. The plaintext exists only in the mint response; shown once.
function EnrichCard() {
  // Plaid Enrich for imported history: what Plaid could still describe,
  // this month's cap, and a button that sends the next batch. Billed per
  // row, so it never runs on its own.
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["enrich-status"], queryFn: api.enrichStatus });
  const [last, setLast] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => api.enrichRun(100),
    onSuccess: (r) => {
      setLast(r.error === "already running" ? "An Enrich run is already in progress."
        : r.error ? `Plaid said: ${r.error}`
        : r.cap_hit ? "This month's cap is used up."
        : `Sent ${r.sent}, described ${r.enriched}.`);
      qc.invalidateQueries({ queryKey: ["enrich-status"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
    },
    onError: (e) => setLast(errText(e)),
  });
  const d = q.data;
  // the cap is the only thing on this card that is a setting rather than an
  // action; null draft = untouched, so the box follows the server until the
  // moment someone types in it
  const [capDraft, setCapDraft] = useState<string | null>(null);
  const capValue = capDraft ?? (d ? String(d.cap) : "");
  const capNum = Number.parseInt(capValue, 10);
  const capOk = Number.isFinite(capNum) && capNum >= 0
    && !!d && capNum !== d.cap;
  const saveCap = useMutation({
    mutationFn: (cap: number) => saveSettings(qc, { plaid_enrich_cap: cap }),
    onSuccess: (_v, cap) => {
      // the box follows the cached status once the draft clears, so the
      // new cap lands in the cache BEFORE the draft goes, or it snaps back
      // to the old number until the refetch; `remaining` is the server's
      // to recompute, hence the refetch still follows
      patchQuery(qc, ["enrich-status"], { cap });
      setCapDraft(null); setLast("Monthly cap saved.");
      qc.invalidateQueries({ queryKey: ["enrich-status"] });
    },
    onError: (e) => setLast(errText(e)),
  });
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <b>Describe imported history with Plaid</b>
      <div className="sub" style={{ margin: ".2rem 0 .5rem" }}>
        Rows from files and other providers have none of Plaid's merchant
        resolution — no logo, no location, no confidence. Plaid Enrich can
        describe them the same way, {" "}
        <b>billed per row</b>, so it runs only when you press the button,
        newest rows first, inside a monthly cap.
      </div>
      {d && (
        <div className="sub" style={{ marginBottom: ".5rem" }}>
          {d.candidates.toLocaleString()} row{d.candidates === 1 ? "" : "s"} still
          undescribed · {d.used.toLocaleString()} of {d.cap.toLocaleString()} used
          this month{!d.plaid_configured && " · Plaid is not configured"}
        </div>
      )}
      <button className="pri" disabled={!d || !d.plaid_configured
                || d.candidates === 0 || d.remaining === 0 || run.isPending}
              onClick={() => run.mutate()}>
        {run.isPending ? "sending…" : "Describe the next 100 rows"}</button>
      <div className="sub" style={{ marginTop: ".6rem" }}>
        <label>Monthly cap (rows){" "}
          <input type="number" min={0} step={100} value={capValue}
                 onChange={(e) => setCapDraft(e.target.value)}
                 style={{ width: "7rem" }} /></label>{" "}
        <button disabled={!capOk || saveCap.isPending}
                onClick={() => saveCap.mutate(capNum)}>
          {saveCap.isPending ? "saving…" : "Save"}</button>
      </div>
      {last && <div className="sub" style={{ marginTop: ".4rem" }}>{last}</div>}
    </div>
  );
}


function ScriptTokensCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["tokens"], queryFn: api.tokens });
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState<{ name: string; token: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // a durable credential — the server wants an elevated session, and the
  // request wrapper's sheet collects the proof when it is not
  const minted = useMutation({
    mutationFn: () => api.tokenMint(name.trim()),
    onSuccess: (r) => {
      setErr(null); setFresh({ name: r.name, token: r.token });
      setName("");
      // the mint response IS the new row (minus the plaintext): append it
      // so the table grows at once, then confirm the list
      const row: ApiToken = { id: r.id, name: r.name, created_at: r.created_at,
        last_used_at: r.last_used_at, revoked_at: r.revoked_at };
      qc.setQueryData<{ tokens: ApiToken[] }>(["tokens"],
        (old) => old && { tokens: [...old.tokens, row] });
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  const revoke = useMutation({
    // identity is the elevation sheet's job; a plain confirm here keeps a
    // stray click from killing a running collector
    mutationFn: (id: string) => {
      if (!window.confirm("Revoke this token? Scripts using it stop working."))
        throw new Error("cancelled");
      return api.tokenRevoke(id);
    },
    // the row leaves the table on the response — the server has already
    // revoked it, and the refetch only confirms
    onSuccess: (_r, id) => {
      removeFromList<{ tokens: ApiToken[] }, ApiToken>(
        qc, ["tokens"], "tokens", (t) => t.id === id);
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
    onError: (e) => { if (!String(e).includes("cancelled")) setErr(errText(e)); },
  });
  const rows = (q.data?.tokens ?? []).filter((t) => !t.revoked_at);
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Script tokens</h2>
      <p className="mut">Credentials for your own collector scripts
        (community scripts). A token can push data through the import
        endpoints — nothing else: it can't read your ledger or change
        settings. Put it in the script's config instead of your login
        password; revoke it here any time.</p>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      {fresh && (
        <div className="note good" style={{ display: "block" }}>
          <div><b>{fresh.name}</b> — copy this token now; it won't be shown
            again:</div>
          <input readOnly value={fresh.token} style={{ width: "100%",
              marginTop: ".4rem", fontFamily: "monospace", fontSize: 12 }}
            onFocus={(e) => e.target.select()} />
        </div>
      )}
      {rows.length > 0 && (
        <table style={{ marginTop: ".5rem" }}>
          <thead><tr><th>Name</th><th>Created</th><th>Last used</th><th /></tr></thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id}>
                <td>{t.name}</td>
                <td>{mmdd(t.created_at)}</td>
                <td>{t.last_used_at ? mmdd(t.last_used_at)
                  : <span className="mut">never</span>}</td>
                <td><button onClick={() => revoke.mutate(t.id)}
                            disabled={revoke.isPending && revoke.variables === t.id}>
                  {revoke.isPending && revoke.variables === t.id
                    ? "revoking…" : "revoke"}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".6rem",
                    flexWrap: "wrap" }}>
        <input placeholder="script name (e.g. payroll-plan)" value={name}
               onChange={(e) => setName(e.target.value)} />
        <button disabled={minted.isPending || !name.trim()}
                onClick={() => minted.mutate()}>
          {minted.isPending ? "creating…" : "Create token"}</button>
      </div>
    </div>
  );
}

// the household's data leaving the instance: full-data ZIP (every table
// as CSV, receipt images included) and, self-hosted, the exact database
// dump. Both are downloads, but never plain links — a plain link on any
// other site could start one against the signed-in owner. The server
// wants an elevated session first and hands back a one-shot URL good for
// a minute; the browser then navigates to it and saves the file. The
// ticket is a FRESH-elevation route: the sheet asks again even inside an
// open window, so the confirm here is only the are-you-sure.
function DataExportCard({ hosted }: { hosted: boolean }) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const [err, setErr] = useState<string | null>(null);
  const owner = me.data?.role === "owner";
  const get = useMutation({
    mutationFn: (kind: "zip" | "dump") => {
      if (!window.confirm("Download everything? This is your whole "
                          + "household's data leaving the instance."))
        throw new Error("cancelled");
      return api.exportTicket(kind);
    },
    onSuccess: (r) => { setErr(null); window.location.assign(r.url); },
    onError: (e) => { if (!String(e).includes("cancelled")) setErr(errText(e)); },
  });
  const ready = !get.isPending;
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Your data</h2>
      <p className="mut">Everything is exportable, always: the full export
        ZIP (every table as CSV, with a README that maps what each file is
        — opens in any spreadsheet){hosted
          ? <>. Instance health is managed by the host.</>
          : <>, or the exact database dump (.sql.gz — the same format the
        manager's backups use; restore one with{" "}
        <code>./oikonome.sh restore &lt;file&gt;</code>). Instance
        health lives under{" "}
        <Link to="/settings/system">System health</Link>.</>}
        {" "}You'll confirm it's you before the download starts — it is
        your whole household leaving the instance, so a signed-in tab alone
        is not enough.
      </p>
      <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem",
                    flexWrap: "wrap" }}>
        <button className="pri" disabled={!ready}
          onClick={() => get.mutate("zip")}>
          {get.isPending ? "preparing…" : "Download export ZIP"}
        </button>
        {!hosted && owner && (
          <button disabled={!ready} onClick={() => get.mutate("dump")}>
            Download database dump (.sql.gz)
          </button>
        )}
      </div>
      {err && <p style={{ color: "var(--red)", marginTop: ".6rem" }}>{err}</p>}
    </div>
  );
}

// move a whole environment's config to a fresh install. Unlike the
// db dump, the sealed bundle survives a different master key — provider keys,
// LLM + SMTP secrets, and live bank tokens re-encrypt under the destination.
function ConnectionsBackupCard({ onImported }: { onImported: () => void }) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const [pass, setPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [importPass, setImportPass] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  // both doors are elevation-gated — what crosses here is every credential
  // the tenant has. Export is a FRESH route (the sheet asks again even
  // inside an open window), so the confirm is only the are-you-sure.

  const exp = useMutation({
    mutationFn: () => {
      if (!window.confirm("Download the connections bundle? It carries "
                          + "every credential this instance holds."))
        throw new Error("cancelled");
      return api.connectionsExport(pass);
    },
    onSuccess: ({ blob, filename }) => {
      setErr(null);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      setPass(""); setConfirm("");
      setOk("Bundle downloaded. Keep it and its passphrase safe.");
    },
    onError: (e) => { if (String(e).includes("cancelled")) return;
                      setOk(null); setErr(errText(e)); },
  });
  const imp = useMutation({
    mutationFn: () => api.connectionsImport(file as File, importPass),
    onSuccess: (r) => {
      setErr(null);
      const grp = r.config.length ? r.config.join(", ") : "none";
      setOk(`Imported ${r.items} bank link(s); config restored: ${grp}.`);
      setImportPass(""); setFile(null);
      onImported();
    },
    onError: (e) => { setOk(null); setErr(errText(e)); },
  });

  // owner-only, and FAIL CLOSED while /api/me is in flight — spelled the
  // other way round, this would render the credential-migration card to
  // anyone until the role arrived (see role.ts)
  if (!isOwner(me)) return null;
  const passOk = pass.length >= 8 && pass === confirm;

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Move to a fresh environment</h2>
      <p className="mut">A passphrase-sealed bundle of your instance
        credentials — Plaid/MX provider keys, the LLM endpoint + key, SMTP
        mail settings, and every live bank link. Unlike the database dump it
        survives a different install: banks stay linked and email/LLM stay
        wired on the new box (or the hosted version), nothing to re-enter. It
        carries credentials only; accounts, history, and budgets ride the
        export ZIP above. One sync after import repopulates accounts.</p>

      {/* this is the whole credential set leaving (or entering) the
          instance — either door confirms it's you first (passkey, or
          password + code), so a stolen session cookie alone gets nothing */}
      <h3 style={{ marginTop: ".8rem", marginBottom: ".3rem" }}>Export</h3>
      <div className="field-grid">
        <label>Passphrase (8+ chars)
          <input type="password" value={pass}
            onChange={(e) => setPass(e.target.value)}
            placeholder="you'll need this on the other side" />
        </label>
        <label>Confirm passphrase
          <input type="password" value={confirm}
            onChange={(e) => setConfirm(e.target.value)} />
        </label>
      </div>
      <button className="pri" style={{ marginTop: ".5rem" }}
        disabled={!passOk || exp.isPending}
        title={passOk ? "" : "passphrases must match and be 8+ chars"}
        onClick={() => exp.mutate()}>
        {exp.isPending ? "sealing…" : "Download connections bundle"}
      </button>

      <h3 style={{ marginTop: "1.1rem", marginBottom: ".3rem" }}>Import</h3>
      <div className="field-grid">
        <label>Bundle file (.oikx)
          <input type="file" accept=".oikx,application/octet-stream"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label>Passphrase
          <input type="password" value={importPass}
            onChange={(e) => setImportPass(e.target.value)} />
        </label>
      </div>
      <button style={{ marginTop: ".5rem" }}
        disabled={!file || !importPass || imp.isPending}
        onClick={() => imp.mutate()}>
        {imp.isPending ? "importing…" : "Import connections"}
      </button>

      {err && <p style={{ color: "var(--red)", marginTop: ".6rem" }}>{err}</p>}
      {ok && <p className="pill g" style={{ marginTop: ".6rem" }}>{ok}</p>}
    </div>
  );
}

// passkeys — optional WebAuthn sign-in for this account.
// Needs a secure context (https or localhost); plain-http LAN installs
// get an explanation instead of a broken button.
function PasskeysCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["passkeys"], queryFn: api.passkeys });
  const [err, setErr] = useState<string | null>(null);
  // adding or removing a factor is elevation-gated, so a stolen session
  // can't enroll its own key (or strip yours) and keep access — the sheet
  // collects the proof when the window is not open
  // one-time recovery codes returned when a passkey is the FIRST strong
  // factor — shown once for lockout protection (mirrors the TOTP card)
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const available = typeof window !== "undefined" && window.isSecureContext &&
    !!window.PublicKeyCredential;
  const add = useMutation({
    mutationFn: async () => {
      const { startRegistration } = await import("@simplewebauthn/browser");
      // the elevation check comes first: the server refuses to start the
      // ceremony unelevated, so the authenticator never creates a
      // credential the server then rejects
      const o = await api.passkeyRegisterOptions();
      const cred = await startRegistration({
        optionsJSON: ((o.options as { publicKey?: unknown }).publicKey ??
          o.options) as never });
      const label = window.prompt(
        "Name this passkey (e.g. 'laptop', 'phone'):") ?? "";
      return api.passkeyRegister(o.challenge_id, cred, label);
    },
    onSuccess: (r) => { setErr(null);
                        if (r?.recovery_codes && r.recovery_codes.length)
                          setRecovery(r.recovery_codes);
                        // the response carries only the id, so the list is
                        // refetched; what ["me"] learns from a new factor
                        // is known here — the second-factor requirement is
                        // met, and a first-factor enrolment's code count
                        patchQuery<Me>(qc, ["me"], {
                          needs_2fa: false,
                          ...(r?.recovery_codes?.length
                            ? { recovery_codes_left: r.recovery_codes.length }
                            : {}) });
                        qc.invalidateQueries({ queryKey: ["passkeys"] }); },
    onError: (e) => {
      if (!String(e).includes("NotAllowedError")) setErr(errText(e));
    },
  });
  const del = useMutation({
    // identity is the elevation sheet's job; a plain confirm here keeps one
    // stray click from stripping a sign-in method
    mutationFn: (id: string) => {
      if (!window.confirm("Remove this passkey?"))
        throw new Error("cancelled");
      return api.passkeyDelete(id);
    },
    // the removed key leaves the list on the response; the refetch confirms
    onSuccess: (_r, id) => { setErr(null);
                       removeFromList<{ passkeys: { id: string }[] },
                                      { id: string }>(
                         qc, ["passkeys"], "passkeys", (p) => p.id === id);
                       qc.invalidateQueries({ queryKey: ["passkeys"] }); },
    onError: (e) => {
      const m = String(e instanceof Error ? e.message : e);
      if (m.includes("cancelled")) return;
      setErr(m.includes("last_factor")
        ? "This is your only two-factor method — add another passkey or an "
          + "authenticator app before removing it."
        : errText(e));
    },
  });
  if (q.isPending || q.isError) return null;
  // passkeys are offered in hosted mode — absent on self-hosted
  // installs
  if (!me.data?.hosted) return null;
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Passkeys</h2>
      <p className="mut">Add a fingerprint, face, or security key. You can
        keep an authenticator app <b>and</b> passkeys — at sign-in, use
        either <b>password + app code</b> or <b>passkey alone</b> (your choice).
        Each device can have its own key.</p>
      {!available && (
        <p className="mut">Passkeys need a <b>secure context</b> — serve
          this instance over https (or open it as <code>localhost</code>)
          and this card comes alive. Password sign-in is unaffected.</p>
      )}
      {err && <div className="note bad" style={{ marginBottom: ".6rem" }}
                   onClick={() => setErr(null)}>{err}</div>}
      {recovery && (
        <div className="note good" style={{ display: "block" }}>
          <b>Recovery codes</b> — save these somewhere safe. Each works once
          to sign in if you lose your passkey or authenticator; they won't be
          shown again.
          <div style={{ fontFamily: "monospace", fontSize: 14,
               lineHeight: 1.9, userSelect: "all", marginTop: ".4rem" }}>
            {recovery.map((c) => <div key={c}>{c}</div>)}
          </div>
          <button style={{ marginTop: ".4rem" }}
                  onClick={() => { navigator.clipboard?.writeText(
                    recovery.join("\n")); setRecovery(null); }}>
            Copy & dismiss</button>
        </div>
      )}
      {q.data.passkeys.length > 0 ? (
        <table>
          <tbody>
            {q.data.passkeys.map((p) => (
              <tr key={p.id}>
                <td>{p.label || "unnamed passkey"}</td>
                <td className="mut">added {mmdd(p.created_at)}
                  {p.last_used && ` · used ${mmdd(p.last_used)}`}</td>
                <td style={{ textAlign: "right" }}>
                  <button disabled={del.isPending && del.variables === p.id}
                    title="Remove this passkey"
                    onClick={() => del.mutate(p.id)}>
                    {del.isPending && del.variables === p.id
                      ? "removing…" : "remove"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="mut">No passkeys enrolled yet.</p>
      )}
      {available && (
        <button className="pri" style={{ marginTop: ".65rem" }}
          disabled={add.isPending}
          onClick={() => add.mutate()}>
          {add.isPending ? "waiting for your device…" : "Add a passkey"}
        </button>
      )}
    </div>
  );
}


// three cadences, local-time am/pm pickers (instance timezone)
const HOURS12 = Array.from({ length: 24 }, (_, h) => ({
  h, label: `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? "am" : "pm"}` }));
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday"];
// phone verification + this-browser push enrollment + channel
// test buttons — rendered under the cadence matrix.
function NotifyChannels({ status }: { status?: NotifyStatus }) {
  const qc = useQueryClient();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  // {text,bad} so a failed verify/test reads as an error, not the same muted
  // grey as success (matches every other card in this file).
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const ok = (text: string) => setMsg({ text, bad: false });
  const err = (e: unknown) => setMsg({ text: errText(e), bad: true });
  // Every control here is keyed on the cached flags — the code box appears
  // on phone_pending, the verified pill on phone_verified — so each write
  // patches the flag it just proved before the refetch confirms the rest
  // (the server's formatting of the number, for one).
  const inval = () => qc.invalidateQueries({ queryKey: ["notify"] });
  const patch = (p: Partial<NotifyStatus>) => {
    patchQuery<NotifyStatus>(qc, ["notify"], p); inval();
  };
  const setP = useMutation({
    mutationFn: () => api.notifyPhone(phone.trim(), smsConsent),
    onSuccess: () => { ok("Code sent — enter it below.");
                       patch({ phone_pending: phone.trim() }); },
    onError: err,
  });
  const verify = useMutation({
    mutationFn: () => api.notifyPhoneVerify(code.trim()),
    onSuccess: () => { ok("Phone verified."); setCode("");
                       patch({ phone: status?.phone_pending ?? phone.trim(),
                               phone_verified: true, phone_pending: null }); },
    onError: err,
  });
  const clearP = useMutation({
    mutationFn: () => api.notifyPhone(""),
    onSuccess: () => { setMsg(null);
                       patch({ phone: null, phone_verified: false,
                               phone_pending: null }); },
  });
  const test = useMutation({
    mutationFn: (channel: "sms" | "push") => api.notifyTest(channel),
    onSuccess: () => ok("Test sent."),
    onError: err,
  });
  const [pushBusy, setPushBusy] = useState(false);
  // Deliberately NOT persisted across reloads: the box must be ticked in the
  // same interaction that submits the number, so the affirmative action and
  // the opt-in are one event rather than a setting someone flipped once.
  // One flag PER MESSAGE PROGRAM — see the consent block below for why.
  const [cSummary, setCSummary] = useState(false);
  // summary only. An 'alerts' program (large-transaction / bill-due texts)
  // is deliberately NOT offered here: nothing in the server sends one, so
  // ticking it would deliver silence forever, and consent copy is a promise
  // to a carrier about what the code sends. It comes back WITH the sender,
  // not before it.
  const smsConsent = cSummary ? ["summary"] : [];
  // Web push needs a SECURE CONTEXT: https, or localhost. On a plain-http
  // origin — which is exactly how most self-hosters reach a box on their LAN
  // — the browser does not expose navigator.serviceWorker at all, and
  // Notification.requestPermission() resolves "denied" INSTANTLY without ever
  // prompting. Reporting that as "Notifications were blocked." would send
  // the user off to un-block a permission Chrome never asked for. The
  // constraint is the origin, and only the browser knows it: the server's
  // push_available only means "VAPID keys are configured".
  const pushBlockedByOrigin = browserPushBlocked();

  // the same door the setup wizard's delivery choice uses (push.ts)
  const enablePush = async () => {
    if (!status?.vapid_public_key) return;
    setPushBusy(true); setMsg(null);
    try {
      const r = await enableBrowserPush(status.vapid_public_key);
      if (!r.ok) { err(r.reason); return; }
      ok("Push enabled in this browser."); patch({ push_subscribed: true });
    } catch (e) { err(e); }
    finally { setPushBusy(false); }
  };
  const disablePush = async () => {
    setPushBusy(true);
    try {
      await disableBrowserPush();
      ok("Push disabled in this browser."); patch({ push_subscribed: false });
    } catch (e) { err(e); }
    finally { setPushBusy(false); }
  };
  if (!status) return null;
  return (
    <div style={{ marginTop: ".6rem", fontSize: 14 }}>
      {status.sms_available ? (<>
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                      alignItems: "center", marginTop: ".3rem" }}>
          <span className="mut">SMS number:</span>
          {status.phone_verified ? (<>
            <span>{status.phone} <span className="pill g">verified</span></span>
            <button onClick={() => test.mutate("sms")}
                    disabled={test.isPending}>send test SMS</button>
            <button onClick={() => clearP.mutate()}
                    disabled={clearP.isPending}>remove</button>
          </>) : (<>
            <input value={phone} placeholder="+12175551234"
                   aria-label="SMS phone number"
                   style={{ maxWidth: "11rem" }}
                   onChange={(e) => setPhone(e.target.value)} />
            <button disabled={setP.isPending || !phone.trim()
                              || !smsConsent.length}
                    title={!smsConsent.length
                             ? "Tick at least one SMS consent box first" : ""}
                    onClick={() => setP.mutate()}>
              {status.phone_pending ? "re-send code" : "send code"}</button>
            {status.phone_pending && (<>
              <input value={code} placeholder="6-digit code"
                     aria-label="SMS verification code"
                     inputMode="numeric" style={{ maxWidth: "8rem" }}
                     onChange={(e) => setCode(e.target.value)} />
              <button className="pri" disabled={verify.isPending || !code.trim()}
                      onClick={() => verify.mutate()}>verify</button>
            </>)}
          </>)}
        </div>
        {!status.phone_verified && (
          // Carrier-required SMS consent. Consent must be its OWN
          // affirmative action, specific to SMS, and NOT bundled with
          // Terms/Privacy acceptance: one paragraph reading "by verifying
          // your number you agree…" with the policy links inline presents
          // as a single combined agreement, and carriers reject that.
          // Hence dedicated checkboxes that GATE the send-code button, the
          // message-type/frequency/rates/STOP-HELP disclosure attached to
          // each checkbox, and the policy links on a separate line that is
          // plainly not part of the consent sentence. Carrier verification
          // reviews this block as the opt-in proof, so its wording and
          // layout are compliance surface, not decoration.
          //
          // One checkbox PER MESSAGE PROGRAM: a single box covering two
          // programs is one consent for both, which carriers reject. Each
          // box is independently tickable, carries its own frequency, and
          // is stored and enforced separately (notify/sms.py PROGRAMS) — so
          // ticking only one really does mean only those texts. Keep the
          // wording here in step with the sample messages and use-case
          // description an operator registers with the carrier.
          <div style={{ margin: ".55rem 0 0", maxWidth: "44rem",
                        padding: ".6rem .7rem", borderRadius: 8,
                        border: "1px solid var(--line)" }}>
            <label style={{ display: "flex", gap: ".5rem",
                            alignItems: "flex-start", fontSize: 13,
                            lineHeight: 1.5, cursor: "pointer" }}>
              <input type="checkbox" checked={cSummary}
                     aria-label="Consent to receive budget summary text messages"
                     onChange={(e) => setCSummary(e.target.checked)}
                     style={{ marginTop: 3, flex: "0 0 auto" }} />
              <span>
                <b>Yes, text me my budget verdict.</b> I agree to receive
                recurring automated texts from Oikonome telling me whether I
                am on budget, on the schedule I set below — about one message
                a day, up to ~30/month. Msg &amp; data rates may apply. Reply{" "}
                <b>STOP</b> to cancel or <b>HELP</b> for help.
              </span>
            </label>
            <p className="mut" style={{ margin: ".5rem 0 0", fontSize: 11.5,
                                        lineHeight: 1.5 }}>
              Consent is not a condition of purchase and is not required to
              use Oikonome.
            </p>
            <p className="mut" style={{ margin: ".35rem 0 0", fontSize: 11.5,
                                        lineHeight: 1.5 }}>
              {ext.legal.terms && ext.legal.privacy && (<>
                Separately, our{" "}
                <a href={ext.legal.terms} target="_blank"
                   rel="noreferrer">Terms</a> and{" "}
                <a href={ext.legal.privacy} target="_blank"
                   rel="noreferrer">Privacy Policy</a> apply to your use of
                Oikonome.{" "}
              </>)}
              We never sell or share your phone number.
            </p>
          </div>
        )}
      </>) : status.sms_entitled === false ? (
        // the gate withholds SMS from this household; there is nothing on
        // this page to upgrade to, so this must not offer it.
        <p className="mut" style={{ margin: ".3rem 0 0" }}>Text alerts aren't
          available on this account — your daily verdict and alerts arrive by
          <b> email and push</b>.</p>
      ) : (
        <p className="mut" style={{ margin: ".3rem 0 0" }}>SMS: not
          configured on this instance (needs Twilio credentials in the
          environment).</p>
      )}
      {status.push_available && (
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                      alignItems: "center", marginTop: ".4rem" }}>
          <span className="mut">Push:</span>
          <button disabled={pushBusy || pushBlockedByOrigin}
                  title={pushBlockedByOrigin
                    ? "Browsers only allow notifications on https://or localhost"
                    : ""}
                  onClick={enablePush}>
            {status.push_subscribed ? "re-enable in this browser"
                                    : "enable in this browser"}</button>
          {pushBlockedByOrigin && (
            // stated AT the control, not only in the note at the foot of the
            // card: a message that far from the button leaves the click
            // reading as "nothing happened"
            <span className="mut" style={{ fontSize: 12 }}>
              needs <b>https</b> — this page is on http, so the browser will
              not allow it
            </span>
          )}
          {status.push_subscribed && (
            <button disabled={pushBusy} onClick={disablePush}>disable</button>
          )}
          {(status.native_push_devices ?? 0) > 0 && (
            <span className="mut" style={{ fontSize: 12 }}>
              {status.native_push_devices} phone
              {status.native_push_devices === 1 ? "" : "s"} registered via
              the mobile app
            </span>
          )}
          {(status.push_subscribed
            || (status.native_push_devices ?? 0) > 0) && (
            <button onClick={() => test.mutate("push")}
                    disabled={test.isPending}>send test push</button>
          )}
        </div>
      )}
      {msg && <p className={"note " + (msg.bad ? "bad" : "good")}
                 style={{ margin: ".4rem 0 0" }}
                 onClick={() => setMsg(null)}>{msg.text}</p>}
    </div>
  );
}

// The zones a browser knows how to name. Intl.supportedValuesOf is the
// honest list (every IANA zone this browser can render); an older browser
// without it gets the device's own zone and the instance's, which is still
// the choice that matters.
function knownZones(extra: string[]): string[] {
  let all: string[] = [];
  try {
    const f = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf;
    if (f) all = f.call(Intl, "timeZone");
  } catch { /* fall through */ }
  return Array.from(new Set([...extra.filter(Boolean), ...all])).sort();
}

/** "America/Los_Angeles" → "Los Angeles (America)" — the name people
 *  recognise first, the region second, underscores gone. */
function zoneLabel(z: string): string {
  const i = z.lastIndexOf("/");
  if (i < 0) return z;
  return `${z.slice(i + 1).split("_").join(" ")} (${z.slice(0, i).split("_").join(" ")})`;
}

function CadenceEditor({ sched, onChange, timezone, onTimezone }: {
  sched: { daily: { on: boolean; hour: number; sms: boolean; push: boolean;
                    summary: boolean };
           weekly: { on: boolean; hour: number; weekday: number;
                     sms: boolean; push: boolean };
           monthly: { on: boolean; hour: number; sms: boolean; push: boolean };
           yearly: { on: boolean; hour: number; sms: boolean; push: boolean } };
  onChange: (s: typeof sched) => void;
  /** the household's zone; "" = following the instance */
  timezone: string;
  onTimezone: (z: string) => void;
}) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const instanceZone = me.data?.instance_timezone ?? "";
  const effective = timezone || instanceZone;
  const device = deviceZone();
  const zones = knownZones([timezone, instanceZone, device]);
  // Not a cadence × channel checkbox matrix — a control cluster you would
  // have to READ to learn the current value. Each cadence is a row that
  // states what it does in words, and the controls open in place behind
  // "change"; every channel is still there, just not at rest.
  const notify = useQuery({ queryKey: ["notify"], queryFn: api.notifyStatus });
  const smsOk = !!notify.data?.sms_available && !!notify.data?.phone_verified;
  // push means browser push OR the mobile apps through the relay — a
  // household whose phones can receive it must be able to tick the box on
  // an instance that never configured VAPID keys
  const pushOk = !!notify.data?.push_available
    || !!notify.data?.native_push_available
    || (notify.data?.native_push_devices ?? 0) > 0;
  const smsWhy = !notify.data?.sms_available
    ? "SMS isn't configured on this instance"
    : !notify.data?.phone_verified
      ? "verify a phone number below first" : "";
  const [openCad, setOpenCad] = useState<string | null>(null);
  const hourSel = (value: number, set: (h: number) => void) => (
    <select value={value} onChange={(e) => set(Number(e.target.value))}>
      {HOURS12.map((o) => <option key={o.h} value={o.h}>{o.label}</option>)}
    </select>
  );
  type Cad = keyof typeof sched;
  const setFlag = (cad: Cad, key: "on" | "sms" | "push", v: boolean) =>
    onChange({ ...sched, [cad]: { ...sched[cad], [key]: v } });
  const hourLabel = (h: number) =>
    HOURS12.find((o) => o.h === h)?.label ?? `${h}:00`;

  const rows: { cad: Cad; label: string; when: string; edit: ReactNode }[] = [
    { cad: "daily", label: "Daily verdict",
      when: `every day at ${hourLabel(sched.daily.hour)}`,
      edit: <>at {hourSel(sched.daily.hour, (hour) =>
        onChange({ ...sched, daily: { ...sched.daily, hour } }))}</> },
    { cad: "weekly", label: "Weekly report",
      when: `every ${WEEKDAYS[sched.weekly.weekday]} at `
            + hourLabel(sched.weekly.hour),
      edit: <>every{" "}
        <select value={sched.weekly.weekday}
          onChange={(e) => onChange({ ...sched, weekly:
            { ...sched.weekly, weekday: Number(e.target.value) } })}>
          {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
        </select>{" "}at{" "}
        {hourSel(sched.weekly.hour, (hour) =>
          onChange({ ...sched, weekly: { ...sched.weekly, hour } }))}</> },
    { cad: "monthly", label: "Monthly report card",
      when: `on the 1st at ${hourLabel(sched.monthly.hour)}`,
      edit: <>on the 1st at {hourSel(sched.monthly.hour, (hour) =>
        onChange({ ...sched, monthly: { ...sched.monthly, hour } }))}</> },
    { cad: "yearly", label: "Year in review",
      when: `on Jan 1 at ${hourLabel(sched.yearly.hour)}`,
      edit: <>on Jan 1 at {hourSel(sched.yearly.hour, (hour) =>
        onChange({ ...sched, yearly: { ...sched.yearly, hour } }))}</> },
  ];

  // what a row says at rest: the channels it actually reaches you on
  const channels = (cad: Cad) => [
    sched[cad].on
      ? cad === "daily" && sched.daily.summary ? "email (summary)" : "email"
      : null,
    sched[cad].sms ? "SMS" : null,
    sched[cad].push ? "push" : null,
  ].filter(Boolean).join(", ");

  const chanBox = (cad: Cad, key: "on" | "sms" | "push", label: string,
                   ok: boolean, why: string) => (
    <label className="sub" style={{ cursor: ok || sched[cad][key]
                                    ? "pointer" : "not-allowed" }}
           title={key !== "on" && !ok ? why : ""}>
      <input type="checkbox" checked={sched[cad][key]}
        style={{ verticalAlign: "middle" }}
        disabled={key !== "on" && !ok && !sched[cad][key]}
        onChange={(e) => setFlag(cad, key, e.target.checked)} />
      {" "}{label}
    </label>
  );

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <span className="mut" style={{ fontSize: 13 }}>Scheduled summaries —
        times are {effective ? <b>{zoneLabel(effective)}</b> : "local time"}
        {!timezone && instanceZone ? " (your instance's zone)" : ""}.
        SMS is the short form; push notifies this browser (and any other
        you enable).</span>
      {/* The zone the hours are kept in. A hosted instance runs on one
          clock and its households on many, so a 9am here is 2am for a
          household west of the instance unless they can say where they
          are. Blank = follow the instance, which is right for a box in
          your own closet. */}
      <div style={{ marginTop: ".35rem", display: "flex", gap: ".5rem",
                    alignItems: "center", flexWrap: "wrap" }}>
        <span className="mut" style={{ fontSize: 13 }}>Time zone</span>
        <select value={timezone}
                onChange={(e) => onTimezone(e.target.value)}>
          <option value="">
            {instanceZone ? `instance default — ${zoneLabel(instanceZone)}`
                          : "instance default"}
          </option>
          {zones.map((z) => <option key={z} value={z}>{zoneLabel(z)}</option>)}
        </select>
        {device && device !== effective && (
          <button type="button" onClick={() => onTimezone(device)}>
            use this device's ({zoneLabel(device)})
          </button>
        )}
      </div>
      <div style={{ marginTop: ".35rem" }}>
        {rows.map(({ cad, label, when, edit }) => {
          const chans = channels(cad);
          const open = openCad === cad;
          return (
            <div key={cad} className="setrow">
              <span className="lab">{label}</span>
              <span className="val">{chans
                ? `${when} · ${chans}` : "off"}</span>
              <span className="act">
                <button style={{ fontSize: 13 }}
                        aria-expanded={open}
                        onClick={() => setOpenCad(open ? null : cad)}>
                  {open ? "done" : chans ? "change" : "turn on"}</button>
              </span>
              {open && (
                <div style={{ flexBasis: "100%", display: "flex", gap: ".8rem",
                              flexWrap: "wrap", alignItems: "center",
                              padding: ".4rem 0 .1rem" }}>
                  <span className="sub">{edit}</span>
                  {chanBox(cad, "on", "email", true, "")}
                  {/* daily only: the email's own summary/detail choice —
                      summary = the verdict card alone,
                      detail = the full report. Deliberately NOT linked to
                      the Today page's toggle: what lands in the inbox and
                      what the page opens to are separate preferences. */}
                  {cad === "daily" && (
                    <label className="sub"
                           title="summary = the verdict card alone (left-to-spend, pinned bills); detail = the full report (plan, money map, cash timeline)">
                      email face{" "}
                      <select value={sched.daily.summary ? "summary" : "detail"}
                        disabled={!sched.daily.on}
                        onChange={(e) => onChange({ ...sched, daily:
                          { ...sched.daily,
                            summary: e.target.value === "summary" } })}>
                        <option value="summary">summary</option>
                        <option value="detail">detail</option>
                      </select>
                    </label>
                  )}
                  {chanBox(cad, "sms", "SMS", smsOk, smsWhy)}
                  {chanBox(cad, "push", "push", pushOk,
                           "push needs a supported browser over HTTPS, "
                           + "or the mobile app")}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <NotifyChannels status={notify.data} />
    </div>
  );
}

// What each role is called on screen. One spelling, so the roster, the
// invite picker and the pending-invite row cannot describe the same grant
// three ways.
const ROLE_LABEL: Record<string, string> = {
  owner: "owner", member: "member · can edit", viewer: "view-only",
};

// household members + one-time invite links. Everyone sees the
// member list (shared truth); only the owner mints/revokes invites,
// removes members, and decides what each of them may do.
function FamilyCard() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const members = useQuery({ queryKey: ["members"], queryFn: api.members });
  const owner = me.data?.role === "owner";
  const invites = useQuery({ queryKey: ["invites"], queryFn: api.invites,
                             enabled: owner });
  const [label, setLabel] = useState("");
  // What the invite will create. View-only is the default because it is
  // the smaller grant: an owner who does not read the dropdown hands out
  // the safer of the two.
  const [invRole, setInvRole] = useState("viewer");
  const [invErr, setInvErr] = useState<string | null>(null);
  // What the mint actually did, not just the link it may or may not have
  // handed back. Hosted keeps the URL — it mails the claim link to the
  // address on the invite, and holding a claimable link is the only
  // mailbox proof the pre-auth claim door gets — so a card rendering
  // `url` alone would draw nothing at all there, leaving the owner unable
  // to tell a sent invitation from a dead button. Read the outcome off the
  // response rather than off the hosted flag: the response is what
  // actually happened. Mirrored in mobile's `inviteOutcome`.
  const [fresh, setFresh] = useState<
    { url: string | null; label: string; emailed: boolean } | null>(null);
  const hosted = !!me.data?.hosted;
  // An invite op moves the invite list and a member op the member list —
  // never both, so each refetches only its own. Rows leave or change on
  // the response; the refetch confirms.
  const invKey = ["invites"];
  const memKey = ["members"];
  const create = useMutation({
    // a durable principal grant — elevation-gated; the sheet collects the
    // proof (passkey, or password + code) when the window is not open
    mutationFn: () => api.inviteCreate(label, invRole),
    onSuccess: (r) => {
      setInvErr(null); setFresh(r); setLabel("");
      // the response has no token hash (the link carries the secret), so
      // the new row only arrives with the refetch
      qc.invalidateQueries({ queryKey: invKey });
    },
    onError: (e) => setInvErr(errText(e)),
  });
  const revoke = useMutation({
    mutationFn: (h: string) => api.inviteRevoke(h),
    onSuccess: (_r, h) => {
      removeFromList<{ invites: Invite[] }, Invite>(
        qc, invKey, "invites", (i) => i.token_hash === h);
      qc.invalidateQueries({ queryKey: invKey });
    } });
  // Changing what somebody may do is a durable grant — elevation-gated,
  // the same proof removing them asks for.
  const setRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: string }) =>
      api.memberSetRole(id, role),
    // the picker is bound to the cached role, so the pick shows the old
    // value until the cache says otherwise — patch it from the response
    onSuccess: (r, v) => {
      qc.setQueryData<{ users: Member[]; assignable_roles: string[] }>(
        memKey, (old) => old && { ...old, users: old.users.map((u) =>
          u.id === v.id ? { ...u, role: r.role ?? v.role } : u) });
      qc.invalidateQueries({ queryKey: memKey });
    },
    onError: (e) => { if (!String(e).includes("cancelled"))
      window.alert(errText(e)); } });
  const remove = useMutation({
    // identity is the elevation sheet's job; a plain confirm here keeps one
    // stray click from kicking somebody out
    mutationFn: (id: string) => {
      if (!window.confirm("Remove this person from the household?"))
        throw new Error("cancelled");
      return api.memberRemove(id);
    },
    onSuccess: (_r, id) => {
      removeFromList<{ users: Member[] }, Member>(
        qc, memKey, "users", (u) => u.id === id);
      qc.invalidateQueries({ queryKey: memKey });
    },
    onError: (e) => { if (!String(e).includes("cancelled"))
      window.alert(errText(e)); } });
  if (members.isPending || members.isError) return null;
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Family</h2>
      <p className="mut">Everyone signed into this household. Everyone
        here sees everything; what differs is what they can change.
        {owner && " A member can edit the money — transactions, bills, "
                + "budgets, rules. Connections, exports, billing, who is in "
                + "the household and deleting the account stay yours. "
                + "Invitations are one-time and expire in 7 days"
                + (hosted ? " — we email the link to the address you "
                          + "enter, and only that address can use it." : ".")}
      </p>
      <table>
        <tbody>
          {members.data.users.map((u) => (
            <tr key={u.id}>
              <td>{u.email}{u.me &&
                <span className="pill g" style={{ marginLeft: 6 }}>you</span>}</td>
              <td className="mut">{ROLE_LABEL[u.role] ?? u.role}
                {" · joined "}{mmdd(u.created_at)}</td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                {owner && !u.me && (<>
                  {/* the picker offers only what an owner may hand out —
                      the server names the list, so the two cannot drift */}
                  <select value={u.role}
                    disabled={setRole.isPending && setRole.variables?.id === u.id}
                    title="what this person can do"
                    style={{ marginRight: ".4rem" }}
                    onChange={(e) => setRole.mutate(
                      { id: u.id, role: e.target.value })}>
                    {(members.data.assignable_roles
                      ?? ["member", "viewer"]).map((r) => (
                      <option key={r} value={r}>
                        {ROLE_LABEL[r] ?? r}</option>
                    ))}
                  </select>
                  <button disabled={remove.isPending && remove.variables === u.id}
                    onClick={() => remove.mutate(u.id)}>
                    {remove.isPending && remove.variables === u.id
                      ? "removing…" : "remove"}</button>
                </>)}
              </td>
            </tr>
          ))}
          {owner && (invites.data?.invites ?? []).map((i) => (
            <tr key={i.token_hash}>
              <td className="mut">{i.label || "pending invite"}</td>
              <td className="mut">invite · {ROLE_LABEL[i.role] ?? i.role}
                {" · expires "}{mmdd(i.expires_at)}</td>
              <td style={{ textAlign: "right" }}>
                <button disabled={revoke.isPending
                                  && revoke.variables === i.token_hash}
                  onClick={() => revoke.mutate(i.token_hash)}>
                  {revoke.isPending && revoke.variables === i.token_hash
                    ? "revoking…" : "revoke"}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {owner && (
        <>
          {fresh && (
            <div className="note" style={{ margin: ".6rem 0",
                  background: "rgba(92,181,107,.10)",
                  borderColor: "rgba(92,181,107,.4)",
                  wordBreak: "break-all" }}>
              {fresh.url ? (<>
                Share this one-time link (shown once)
                {fresh.emailed
                  ? <>. We also emailed it to <b>{fresh.label}</b>.</>
                  : ":"}<br />
                <b>{fresh.url}</b>{" "}
                <button style={{ marginLeft: 6 }} onClick={() =>
                  navigator.clipboard?.writeText(fresh.url ?? "")}>
                  copy</button>
              </>) : (<>
                Invitation emailed to <b>{fresh.label}</b>. The link works
                once, expires in 7 days, and only that address can use
                it.{" "}
                <button style={{ marginLeft: 6 }}
                  onClick={() => setFresh(null)}>dismiss</button>
              </>)}
            </div>
          )}
          {invErr && <div className="note bad"
            onClick={() => setInvErr(null)}>{invErr}</div>}
          <div style={{ display: "flex", gap: ".5rem", marginTop: ".6rem",
                        flexWrap: "wrap" }}>
            {/* Hosted mints must NAME an address — the link is mailed
                there and nowhere else — so ask for one on the field
                instead of letting the owner learn it from a refusal. */}
            <input value={label} type={hosted ? "email" : "text"}
              placeholder={hosted ? "their email address"
                                  : "who's it for? (name or email)"}
              onChange={(e) => setLabel(e.target.value)}
              style={{ flex: 1, minWidth: "12rem", padding: ".5rem",
                       background: "var(--hover)", color: "var(--ink)",
                       border: "1px solid var(--line)",
                       borderRadius: "var(--rs)" }} />
            <select value={invRole} title="what this person can do"
              onChange={(e) => setInvRole(e.target.value)}
              style={{ padding: ".5rem", background: "var(--hover)",
                       color: "var(--ink)", border: "1px solid var(--line)",
                       borderRadius: "var(--rs)" }}>
              <option value="viewer">{ROLE_LABEL.viewer}</option>
              <option value="member">{ROLE_LABEL.member}</option>
            </select>
            <button className="pri"
              disabled={create.isPending}
              onClick={() => create.mutate()}>
              {create.isPending ? (hosted ? "sending…" : "creating…")
                : hosted ? "Send invitation" : "Create invite link"}</button>
          </div>
          {create.isError && (
            <p style={{ color: "var(--red)" }}>{String(create.error)}</p>
          )}
        </>
      )}
    </div>
  );
}


// per-provider status + key management, reusing the connect
// hub's guided flows. Secrets stay write-only (presence flags).
function ConnectionsCard({ data, hosted = false, onSaved }: {
  data: SettingsData; hosted?: boolean; onSaved: () => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const conns = useQuery({ queryKey: ["connections"],
                           queryFn: api.connections });
  // Clearing a provider's keys drops exactly the presence flags this card
  // renders, so the row reads "not configured" on the response — the
  // server returns no view to seed from, hence the explicit fields.
  // the server refuses to clear keys while live connections still depend
  // on them (they could never be released upstream afterwards) — that
  // refusal names the institutions, so it must be shown, not swallowed
  const [clearErr, setClearErr] = useState<string | null>(null);
  const clearPlaid = useMutation({ mutationFn: api.plaidKeysClear,
    onSuccess: () => {
      setClearErr(null);
      patchQuery<SettingsData>(qc, ["settings"], {
        plaid_client_id: null, plaid_secret_set: false, plaid_env: null });
      onSaved();
    },
    onError: (e) => setClearErr(errText(e)) });
  const clearMx = useMutation({ mutationFn: api.mxKeysClear,
    onSuccess: () => {
      setClearErr(null);
      patchQuery<SettingsData>(qc, ["settings"], {
        mx_client_id: null, mx_api_key_set: false, mx_env: null });
      onSaved();
    },
    onError: (e) => setClearErr(errText(e)) });
  // a guided flow finished: it wrote keys, links, and accounts of its own
  const done = () => { setOpen(null); onSaved();
                       qc.invalidateQueries({ queryKey: ["settings"] });
                       qc.invalidateQueries({ queryKey: ["accounts"] }); };
  const sfin = (conns.data?.connections ?? []).filter(
    (c) => c.aggregator === "simplefin");
  const row = (label: string, status: React.ReactNode,
               actions: React.ReactNode) => (
    <tr><td style={{ whiteSpace: "nowrap" }}><b>{label}</b></td>
      <td>{status}</td>
      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{actions}</td>
    </tr>
  );
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Connections</h2>
      {hosted ? (
        /* The aggregator runs under the platform's credentials and /api
           refuses a tenant's own keys, so the SPA must not offer the
           buttons — otherwise the only way to learn that is to fill in the
           form and be told 403. Banks are linked from Accounts here; what
           remains tenant-owned is the push door. */
        <p className="mut">Your banks connect through the operator's provider —
          there are no keys to add. Everything account-shaped lives under{" "}
          <b>Manage accounts</b> below (also on{" "}
          <Link to="/accounts">Accounts</Link>).</p>
      ) : (
      <p className="mut">Provider credentials and live feeds. Keys are
        stored encrypted and never shown back; validation happens against
        the provider before anything saves.</p>)}
      {clearErr && <p style={{ color: "var(--red)" }}>{clearErr}</p>}
      <table style={{ fontSize: 14 }}>
        <tbody>
          {!hosted && row("Plaid",
            data.plaid_client_id
              ? <>keys set <span className="mut">({data.plaid_client_id.slice(0, 8)}… · {data.plaid_env ?? "production"})</span></>
              : <span className="mut">no keys</span>,
            <>
              <button onClick={() => setOpen(open === "plaid" ? null : "plaid")}>
                {data.plaid_client_id ? "replace keys" : "set up"}</button>
              {data.plaid_client_id && (
                <button style={{ marginLeft: 6 }} disabled={clearPlaid.isPending}
                  onClick={() => clearPlaid.mutate()}>clear</button>)}
            </>)}
          {!hosted && row("MX",
            data.mx_client_id
              ? <>keys set <span className="mut">({data.mx_client_id.slice(0, 8)}… · {data.mx_env ?? "production"})</span></>
              : <span className="mut">no keys</span>,
            <>
              <button onClick={() => setOpen(open === "mx" ? null : "mx")}>
                {data.mx_client_id ? "replace keys" : "set up"}</button>
              {data.mx_client_id && (
                <button style={{ marginLeft: 6 }} disabled={clearMx.isPending}
                  onClick={() => clearMx.mutate()}>clear</button>)}
            </>)}
          {!hosted && row("SimpleFIN",
            sfin.length
              ? <>connected <span className="mut">({sfin.length} bridge{sfin.length > 1 ? "s" : ""})</span></>
              : <span className="mut">not connected</span>,
            <button onClick={() => setOpen(open === "simplefin" ? null : "simplefin")}>
              {sfin.length ? "add / replace token" : "set up"}</button>)}
          {row("API / community scripts",
            <span className="mut">push-based{hosted ? "" : " — health under System health"}</span>,
            /* System health is the operator's on hosted, so the section —
               and this link to it — does not exist there */
            hosted ? <span className="mut">—</span>
              : <Link className="btn" to="/settings/system">
                  System health → collectors</Link>)}
        </tbody>
      </table>
      {open === "plaid" && <div style={{ marginTop: "1rem" }}><PlaidFlow onDone={done} /></div>}
      {open === "mx" && <div style={{ marginTop: "1rem" }}><MxFlow onDone={done} /></div>}
      {open === "simplefin" && <div style={{ marginTop: "1rem" }}><SimplefinFlow onDone={done} /></div>}
      {/* the account workbench, always open — not minimized, and not a
          copy of the Accounts page: settings up front. Purpose-built
          management rows, every control visible, no pencil to find. The
          Accounts page stays the balances/holdings view. */}
      <h3 style={{ margin: "1.1rem 0 .1rem" }}>Manage accounts</h3>
      <p className="mut" style={{ margin: "0 0 .2rem" }}>
        Rename, retype, set primary or excluded, hide, remove — per
        account; sync, fix and shared-accounts per connection.</p>
      <ManageAccounts />
    </div>
  );
}

// email delivery (SMTP) via the web UI — no docker/.env editing.
// Host anchors the group (clearing it reverts to the operator's env
// fallback); the password is write-only like the LLM key.
function SmtpCard({ data, onSaved }: {
  data: SettingsData; onSaved: () => void;
}) {
  const [host, setHost] = useState(data.smtp_host ?? "");
  const [port, setPort] = useState(String(data.smtp_port ?? 587));
  const [user, setUser] = useState(data.smtp_user ?? "");
  const [from, setFrom] = useState(data.smtp_from ?? "");
  const [pw, setPw] = useState("");             // never prefilled
  const [tls, setTls] = useState(data.smtp_starttls !== false);
  const [err, setErr] = useState<string | null>(null);
  const qc = useQueryClient();
  const save = useMutation({
    // seeds ["settings"] from the response — the password-set flag and
    // the sender line read from there
    mutationFn: () => saveSettings(qc, {
      smtp_host: host.trim(), smtp_port: port.trim(),
      smtp_user: user.trim(), smtp_from: from.trim(),
      smtp_starttls: tls,
      ...(pw.trim() ? { smtp_password: pw.trim() } : {}),
    }),
    onSuccess: () => { setErr(null); setPw(""); onSaved(); },
    onError: (e) => setErr(errText(e)),
  });
  const test = useMutation({
    mutationFn: api.smtpTest,
    onSuccess: () => setErr(null),
    onError: (e) => setErr(errText(e)),
  });
  const input = (v: string, set: (s: string) => void, ph: string,
                 type = "text") => (
    <input type={type} value={v} placeholder={ph}
      onChange={(e) => set(e.target.value)}
      style={{ width: "100%", padding: ".5rem", background: "var(--hover)",
               border: "1px solid var(--line)", borderRadius: "var(--rs)",
               color: "var(--ink)" }} />
  );
  // Collapsed. Most installs never set this — they fall back to
  // OIKONOME_SMTP_* and work — so six fields sitting open above the
  // recipients they exist to serve are six fields in the way. Open when it
  // IS configured here, because then it is state worth reading.
  return (
    <details className="card" style={{ marginTop: "1rem" }}
             open={!!data.smtp_host}>
      <summary style={{ cursor: "pointer", display: "flex", gap: ".6rem",
                        alignItems: "baseline", flexWrap: "wrap" }}>
        <b>Email delivery (SMTP)</b>
        <span className="sub">{data.smtp_host
          ? `${data.smtp_host}:${data.smtp_port ?? 587}`
          : "using the operator's OIKONOME_SMTP_* settings"}</span>
      </summary>
      <p className="mut">Where the daily verdict, alerts, invites, and
        feedback emails go OUT through — any provider works (Proton,
        Fastmail, Gmail app-password…). Saved here it applies immediately;
        leaving the host empty falls back to <code>OIKONOME_SMTP_*</code> in
        docker/.env if the operator set those.</p>
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      <div className="field-grid">
        <label>SMTP host{input(host, setHost, "smtp.fastmail.com")}</label>
        <label>Port{input(port, setPort, "587")}</label>
        <label>Username{input(user, setUser, "you@example.com")}</label>
        <label>Password <span className="sub">
          {data.smtp_password_set
            ? "(one is set — type to replace)" : ""}</span>
          {input(pw, setPw, data.smtp_password_set ? "••••••••" : "",
                 "password")}</label>
        <label>From address{input(from, setFrom, "oikonome@example.com")}</label>
      </div>
      {/* resolve_smtp reads this, so it must be writable here: without the
          control the UI path forces STARTTLS and the documented
          OIKONOME_SMTP_STARTTLS escape hatch stops existing the moment you
          configure SMTP here. */}
      <label style={{ display: "flex", alignItems: "center", gap: ".4rem",
                      marginTop: ".6rem" }}>
        <input type="checkbox" checked={tls}
               onChange={(e) => setTls(e.target.checked)} />
        Use STARTTLS
        <span className="sub">— turn off only for a plain relay on your own
          network that doesn't offer it</span>
      </label>
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".6rem" }}>
        <button className="pri" disabled={save.isPending}
          onClick={() => save.mutate()}>
          {save.isPending ? "saving…" : "Save SMTP settings"}</button>
        <button disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? "sending…" : "Send test email"}</button>
        {/* every recipient is named, and a partial failure is shown as a
            failure — a single "sent ✓" cannot say WHO got it, which is the
            only question this button is ever pressed to answer. */}
        {test.isSuccess && (
          <span style={{ alignSelf: "center" }}
                className={test.data.failed?.length ? "bad" : "mut"}>
            sent to {test.data.sent_to.join(", ") || "nobody"} via{" "}
            {test.data.via} {test.data.failed?.length ? "" : "✓"}
            {/* the button is pressed to answer "did adding this person
                work?" — an address it deliberately skipped has to be
                named, or the green tick answers a question nobody asked */}
            {test.data.skipped?.map((sk) => (
              <div key={sk.email} className="sub">
                {sk.email} — not sent: {sk.reason}</div>
            ))}
            {test.data.failed?.map((f) => (
              <div key={f.email} style={{ color: "var(--red)" }}>
                {f.email} failed — {f.error}</div>
            ))}</span>
        )}
      </div>
    </details>
  );
}
// ONE place for every AI setting: named backends —
// several at once, local or cloud — and a per-task routing table
// (assistant / categorization / receipts & documents). The bundled env
// backend shows as a built-in row; the legacy single-endpoint keys
// surface as a "Custom endpoint" entry so there is one list to manage,
// and the server folds their stored key in on first save. API keys are
// write-only: the server reports presence, never the value.
// Hosted: only public HTTPS APIs (localhost / LAN blocked by SSRF) —
// copy and placeholders must not suggest Ollama-on-this-machine.
type LlmBackendRow = {
  id: string; name: string; url: string; model: string;
  // extra_body mirrors the api_key contract: the server reports only
  // presence (extra_body_set), "" on save keeps the stored JSON, and an
  // explicit null clears it
  vision_model: string; extra_body: string | null;
  extra_body_set: boolean; api_key_set: boolean;
  api_key?: string;                     // present only when (re)typed
};
const LLM_TASKS: [string, string][] = [
  ["assistant", "Assistant"],
  ["categorize", "Categorization & tags"],
  ["vision", "Receipts & documents"],
];

function LlmCard({ data, hosted = false, onSaved }: {
  data: SettingsData; hosted?: boolean; onSaved: () => void;
}) {
  const seed = (): LlmBackendRow[] => {
    const rows: LlmBackendRow[] = (data.llm_backends ?? []).map((b) => ({
      id: b.id, name: b.name, url: b.url, model: b.model,
      vision_model: b.vision_model, extra_body: b.extra_body,
      extra_body_set: b.extra_body_set, api_key_set: b.api_key_set,
    }));
    if (!rows.length && data.llm_url) {
      // the legacy JSON options are masked like the per-backend twin, so
      // presence comes from the flag and the value starts blank —
      // "" keeps whatever is stored, and only the clear switch sends null
      rows.push({ id: "legacy", name: "Custom endpoint", url: data.llm_url,
                  model: data.llm_model ?? "",
                  vision_model: data.llm_vision_model ?? "",
                  extra_body: "",
                  extra_body_set: !!data.llm_extra_body_set,
                  api_key_set: data.llm_api_key_set });
    }
    return rows;
  };
  const [backends, setBackends] = useState<LlmBackendRow[]>(seed);
  const [roles, setRoles] = useState<Record<string, string>>(
    { ...(data.llm_roles ?? {}) });
  const [editing, setEditing] = useState<string | null>(null);
  const [taxOk, setTaxOk] = useState(!!data.taxdocs_allow_remote_llm);
  // vision model for the bundled/env default backend — the top-level
  // config key the receipt reader falls back to when no added backend is
  // routed. The per-backend vision_model fields don't cover it, so a
  // bundled-Ollama-only install needs this input or the value is
  // unreachable from the UI.
  const [bundledVision, setBundledVision] =
    useState(data.llm_vision_model ?? "");
  const [err, setErr] = useState<string | null>(null);
  const qc = useQueryClient();
  const save = useMutation({
    // seeds ["settings"] from the response; the key-set flags and the
    // bundled-model line read from there
    mutationFn: () => saveSettings(qc, {
      llm_backends: backends.map(
        ({ api_key_set, extra_body_set, ...rest }) => rest),
      llm_roles: roles,
      taxdocs_allow_remote_llm: taxOk ? "1" : "",
      // empty clears; only sent when the input is shown, so installs
      // without a bundled backend never touch the key
      ...(data.llm_bundled
        ? { llm_vision_model: bundledVision.trim() } : {}),
    }),
    onSuccess: () => { setErr(null); setEditing(null); onSaved(); },
    onError: (e) => setErr(errText(e)),
  });
  const isLocal = (url: string) => {
    const h = (url.split("//")[1] ?? "").split(/[/:]/)[0].toLowerCase();
    return !!h && (h === "localhost" || h.endsWith(".local")
      || !h.includes(".") || /^(10\.|192\.168\.|127\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h));
  };
  const pill = (local: boolean) => (
    <span className={"pill " + (local ? "g" : "a")}>
      {local ? "local" : "cloud"}</span>);
  const upsert = (row: LlmBackendRow) => {
    setBackends((bs) => bs.some((b) => b.id === row.id)
      ? bs.map((b) => (b.id === row.id ? row : b)) : [...bs, row]);
    setEditing(null);
  };
  const remove = (id: string) => {
    setBackends((bs) => bs.filter((b) => b.id !== id));
    setRoles((r) => Object.fromEntries(
      Object.entries(r).filter(([, v]) => v !== id)));
  };
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>AI (optional)</h2>
      <p className="mut">Everything core works without AI. Add one or more
        OpenAI-compatible backends{hosted
          ? <> — <b>public</b> APIs only (OpenAI, OpenRouter, a cloud
            endpoint you run…); a model on your laptop or home network is
            not reachable from this server</>
          : <> — local (Ollama, LM Studio…, no API key needed) or cloud —
            and pick which one handles each task</>}. They power the{" "}
        <b>Assistant</b> page, categorize unknown merchants, summarize
        matched Amazon orders, tag recurring proposals, and — with a
        vision model — read receipts and paper documents.</p>
      <p className="mut" style={{ fontSize: 13 }}>
        Free options that work here: <b>Google Gemini</b> (AI Studio free
        tier, OpenAI-compatible endpoint), <b>Groq</b>, and{" "}
        <b>OpenRouter</b>'s free-routed models. One honest caveat: free
        tiers generally reserve the right to use what you send them to
        improve their models — and what this sends is merchant names from
        your transactions. Read the provider's data policy before wiring
        one in.</p>
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}

      <h3 style={{ margin: ".6rem 0 .3rem" }}>Backends</h3>
      {data.llm_bundled && (
        <>
          <div style={{ display: "flex", gap: ".5rem",
                        alignItems: "baseline",
                        padding: ".25rem 0", flexWrap: "wrap" }}>
            <b>Bundled Ollama</b>
            <span className="sub">
              {data.llm_bundled.model || "(no model)"}</span>
            {pill(true)}
            <span className="sub">built-in · managed by the install</span>
          </div>
          <p style={{ margin: ".15rem 0 .4rem" }}><label>
            <span className="sub">Vision model</span>{" "}
            <span className="sub">— reads receipts and documents when the
              bundled (or environment-default) backend handles them;
              backends you add below carry their own vision model.
              Bundled Ollama: <code>./oikonome.sh llm vision</code> pulls
              qwen2.5vl:3b. Empty turns it off.</span><br />
            <input type="text" value={bundledVision}
              placeholder="qwen2.5vl:3b"
              onChange={(e) => setBundledVision(e.target.value)}
              style={{ width: "14rem", padding: ".45rem",
                       background: "var(--hover)",
                       border: "1px solid var(--line)",
                       borderRadius: "var(--rs)", color: "var(--ink)" }} />
          </label></p>
        </>
      )}
      {backends.map((b) => (
        <Fragment key={b.id}>
          <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                        padding: ".25rem 0", flexWrap: "wrap" }}>
            <b>{b.name}</b>
            <span className="sub">{b.model}{b.vision_model
              ? ` · vision ${b.vision_model}` : ""}</span>
            {pill(isLocal(b.url))}
            <span className="sub" style={{ overflow: "hidden",
              textOverflow: "ellipsis", maxWidth: "16rem" }}>{b.url}</span>
            <span style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>
              <button onClick={() =>
                setEditing(editing === b.id ? null : b.id)}>
                {editing === b.id ? "cancel" : "edit"}</button>{" "}
              <button onClick={() => remove(b.id)}>remove</button>
            </span>
          </div>
          {editing === b.id && (
            <LlmBackendForm initial={b} hosted={hosted} onDone={upsert}
                            onCancel={() => setEditing(null)} />
          )}
        </Fragment>
      ))}
      {editing === "new"
        ? <LlmBackendForm hosted={hosted} onDone={upsert}
                          onCancel={() => setEditing(null)} />
        : <p style={{ margin: ".3rem 0" }}>
            <button onClick={() => setEditing("new")}>+ Add backend</button>
          </p>}

      <h3 style={{ margin: ".8rem 0 .3rem" }}>What uses what</h3>
      {/* categorization is NOT LLM-owned: seed rules, the nightly
          per-household model and the trained local classifier run first;
          the routed backend only sees the merchants they all abstain on
          (plus tags and Amazon summaries) */}
      {/* the per-household overlay needs no operator setup — it trains
          from the household's own corrections whether or not a shipped
          model file is mounted, so this line renders unconditionally:
          hiding it on installs without categorizer_active would leave the
          one "what uses what" panel silent about a layer acting nightly */}
      <p className="sub" style={{ margin: "0 0 .35rem", fontSize: 12.5 }}>
        Each night this instance also retrains a small per-household model
        from your accumulated category corrections — it categorizes new
        lookalike merchants on this machine, before any backend below is
        consulted, and your data never leaves the instance.
      </p>
      {data.categorizer_active && (
        <p className="sub" style={{ margin: "0 0 .35rem", fontSize: 12.5 }}>
          A built-in trained classifier also categorizes merchants on this
          machine, after the per-household model — the backend routed to
          Categorization only handles what both abstain on, plus proposal
          tags and Amazon summaries.
        </p>
      )}
      {LLM_TASKS.map(([role, label]) => {
        const eff = data.llm_effective?.[role];
        return (
          <p key={role} style={{ display: "flex", gap: ".6rem",
                                 alignItems: "baseline", flexWrap: "wrap",
                                 margin: ".25rem 0" }}>
            <span style={{ minWidth: "11rem" }}>{label}</span>
            <select value={roles[role] ?? ""}
                    onChange={(e) => setRoles((r) => {
                      const next = { ...r };
                      if (e.target.value) next[role] = e.target.value;
                      else delete next[role];
                      return next;
                    })}>
              <option value="">
                default{eff ? ` (${eff.model || eff.url})` : " (off)"}
              </option>
              {data.llm_bundled && (
                <option value="bundled">
                  Bundled Ollama — {data.llm_bundled.model}</option>
              )}
              {backends.map((b) => (
                <option key={b.id} value={b.id}>{b.name} — {b.model}</option>
              ))}
            </select>
            {eff && pill(eff.local)}
          </p>
        );
      })}

      {/* W-2s, 1040s and check images carry a full SSN or bank
          account/routing number, and the vision path sends the whole
          page. That must not ride the consent given for coffee receipts.
          Only meaningful for a REMOTE endpoint — a local model never
          leaves the machine. */}
      <p><label className="mut" style={{ cursor: "pointer" }}>
        <input type="checkbox" checked={taxOk}
               onChange={(e) => setTaxOk(e.target.checked)} />{" "}
        Allow sensitive documents to be sent to my remote model
        <div className="sub" style={{ marginLeft: "1.4rem" }}>
          Off by default. W-2 and 1040 imports send an image of the whole
          document, <b>including your Social Security number</b>; reading a
          photographed check sends its face, <b>including the full bank
          routing and account number</b>. Leave this off unless the routed
          endpoint is one you trust with that; it has no effect when the
          model runs locally.
        </div>
      </label></p>
      <button className="pri" disabled={save.isPending || editing !== null}
              title={editing !== null
                ? "finish or cancel the open backend form first" : undefined}
              onClick={() => save.mutate()}>
        {save.isPending ? "saving…" : "Save AI settings"}</button>
    </div>
  );
}

// add/edit one backend. The key field is write-only: a set key shows as
// dots, typing replaces it, and leaving it untouched keeps the stored one.
function LlmBackendForm({ initial, hosted, onDone, onCancel }: {
  initial?: LlmBackendRow; hosted: boolean;
  onDone: (row: LlmBackendRow) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [url, setUrl] = useState(initial?.url ?? "");
  const [model, setModel] = useState(initial?.model ?? "");
  const [vision, setVision] = useState(initial?.vision_model ?? "");
  // the stored JSON never comes back from the server (it can carry
  // credentials), so the field starts blank; blank keeps, typing
  // replaces, and the explicit switch clears — the api_key contract
  const [extra, setExtra] = useState(initial?.extra_body || "");
  const [key, setKey] = useState("");            // never prefilled
  // explicit "drop the stored key" switch — a blank field always means
  // "keep", so clearing needs its own control. A row already carrying a
  // pending clear (api_key === "") re-opens with it on.
  const [clearKey, setClearKey] = useState(initial?.api_key === "");
  const [clearExtra, setClearExtra] =
    useState(initial?.extra_body === null);
  const field = (v: string, set: (s: string) => void, ph: string,
                 type = "text", disabled = false) => (
    <input type={type} value={v} placeholder={ph} disabled={disabled}
      onChange={(e) => set(e.target.value)}
      style={{ width: "100%", padding: ".45rem", background: "var(--hover)",
               border: "1px solid var(--line)", borderRadius: "var(--rs)",
               color: "var(--ink)" }} />
  );
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: "var(--rs)",
                  padding: ".6rem .7rem", margin: ".3rem 0" }}>
      <p><label>Name<br />
        {field(name, setName, hosted ? "OpenAI" : "Ollama on my LAN")}
      </label></p>
      <p><label>Endpoint URL<br />
        {field(url, setUrl,
          hosted ? "https://api.openai.com/v1" : "http://localhost:11434/v1")}
      </label></p>
      <p><label>Model<br />
        {field(model, setModel, hosted ? "gpt-4o-mini" : "llama3.1:8b")}
      </label></p>
      <p><label>Vision model <span className="sub">(optional — reads
        receipt images/PDFs{hosted
          ? "; use a vision-capable model from the same API"
          : <>{" "}; bundled Ollama: <code>./oikonome.sh llm vision</code>{" "}
            pulls qwen2.5vl:3b</>})</span><br />
        {field(vision, setVision, hosted ? "gpt-4o-mini" : "qwen2.5vl:3b")}
      </label></p>
      <p><label>API key <span className="sub">
        {clearKey
          ? "(will be removed when this is applied and saved)"
          : initial?.api_key
            ? "(entered, not saved yet — leave blank to keep it)"
            : initial?.api_key_set
              ? "(one is set — type to replace, leave untouched to keep it)"
              : hosted ? "(required for most cloud providers)"
                       : "(optional — only for cloud providers)"}</span><br />
        {field(key, setKey,
               !clearKey && (initial?.api_key || initial?.api_key_set)
                 ? "••••••••" : "",
               "password", clearKey)}
      </label></p>
      {initial?.api_key_set && (
        <p><label className="mut" style={{ cursor: "pointer" }}>
          <input type="checkbox" checked={clearKey}
                 onChange={(e) => {
                   setClearKey(e.target.checked);
                   if (e.target.checked) setKey("");
                 }} />{" "}
          Remove the stored API key
          <span className="sub"> — this backend keeps working without one;
            leaving the field above blank keeps the current key</span>
        </label></p>
      )}
      <p><label>Extra request body <span className="sub">(advanced, JSON
        merged into every completion call{hosted ? "" : <> — e.g. gemma's{" "}
          {'{"chat_template_kwargs": {"enable_thinking": false}}'}</>})
        </span><br />
        <textarea rows={2} value={extra} disabled={clearExtra}
          placeholder={initial?.extra_body_set
            ? "JSON options set — leave blank to keep them" : ""}
          onChange={(e) => setExtra(e.target.value)}
          style={{ width: "100%", padding: ".5rem",
                   background: "var(--hover)", color: "var(--ink)",
                   border: "1px solid var(--line)",
                   borderRadius: "var(--rs)",
                   fontFamily: "monospace", fontSize: 12 }} />
      </label></p>
      {initial?.extra_body_set && (
        <p><label>
          <input type="checkbox" checked={clearExtra}
                 onChange={(e) => setClearExtra(e.target.checked)} />{" "}
          Remove the stored JSON options
          <span className="sub"> — leaving the field above blank keeps
            them</span>
        </label></p>
      )}
      <button className="pri" disabled={!url.trim() || !model.trim()}
        onClick={() => onDone({
          id: initial?.id ?? Math.random().toString(36).slice(2, 10),
          name: name.trim() || "Backend", url: url.trim(),
          model: model.trim(), vision_model: vision.trim(),
          // "" keeps the stored JSON, null is the explicit clear —
          // mirrored from the api_key switch above
          extra_body: clearExtra ? null : extra.trim(),
          extra_body_set: initial?.extra_body_set ?? false,
          api_key_set: initial?.api_key_set ?? false,
          // blank never means "drop": an unsaved row's just-typed key is
          // carried forward, and only the explicit switch produces the
          // empty string the server treats as "clear the stored key"
          ...(clearKey ? { api_key: "" }
            : key.trim() ? { api_key: key.trim() }
            : initial?.api_key ? { api_key: initial.api_key } : {}),
        })}>
        {initial ? "apply" : "add"}</button>{" "}
      <button onClick={onCancel}>cancel</button>
      <p className="sub" style={{ margin: ".4rem 0 0" }}>
        Nothing is saved until you hit “Save AI settings” below.</p>
    </div>
  );
}


/** Re-run either guided walkthrough on demand.
 *
 *  Welcome needs its per-step marks CLEARED, not just a link: the wizard
 *  resumes at the first unfinished step, so a completed one would open on
 *  "Finish" and teach nothing. The settings endpoint already treats a null
 *  step value as "forget this mark", so this is a normal save, not a new
 *  API. Retire keeps its own state and only needs ?wizard=1.
 */
function GuidedSetupCard() {
  const qc = useQueryClient();
  const nav = useNavigate();
  // "continue" only while a step is left: Welcome sends a finished
  // household straight back to Today, so the button would do nothing
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding });
  const [msg, setMsg] = useState<string | null>(null);
  const restart = useMutation({
    mutationFn: () => saveSettings(qc, {
      wizard_done: false,
      wizard_steps: {
        connect: null, sync: null, import: null,
        bills: null, budgets: null, email: null, finish: null,
      },
    }),
    onSuccess: () => {
      // the step marks are a setting (seeded from the response) and the
      // wizard's progress read; nothing else in the app moved
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      nav("/welcome");
    },
    onError: (e) => setMsg(errText(e)),
  });
  return (
    <div className="card">
      <h2>Guided setup</h2>
      {/* One vocabulary for all three: "<thing> wizard". Naming them
          separately ("Re-run welcome walkthrough", "Set up a business", …)
          gives one kind of thing three names, and the card then reads like
          three unrelated features. */}
      <p className="sub">Run any wizard again — nothing is deleted, and you
        can skip out at any step.</p>
      {msg && <div className="note bad">{msg}</div>}
      <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap",
                    alignItems: "center", marginTop: ".4rem" }}>
        {/* resumes at the first unfinished step, marks kept */}
        {ob.data && !ob.data.wizard_done && (
          <button onClick={() => nav("/welcome")}>Finish setup</button>
        )}
        <button disabled={restart.isPending}
                onClick={() => restart.mutate()}>
          {restart.isPending ? "starting…" : "Onboarding wizard"}
        </button>
        {/* Never gated: this button is the only way to set a business up, and
            the Business tab stays hidden until one exists — gating the way in
            on having already been in would make the tab unreachable. */}
        <Link to="/business/setup"><button>
          Business wizard
        </button></Link>
        <Link to="/retirement/setup"><button>
          Retirement wizard
        </button></Link>
      </div>
      <p className="sub" style={{ marginTop: ".5rem" }}>
        The onboarding wizard clears its step marks so it starts from the
        beginning; your accounts, budgets and bills are untouched.
      </p>
    </div>
  );
}

// on-demand job triggers: run the worker task bodies for THIS tenant
// right now (POST /api/jobs/sync · /api/jobs/email)
function MaintenanceCard({ onDone }: { onDone: () => void }) {
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const sync = useMutation({
    mutationFn: api.jobsSync,
    onSuccess: (r) => {
      const items = Object.entries(r.results);
      // "a sync is already running" and "you have no connections" both
      // arrive as an empty result set, so they must be told apart by
      // `started` — otherwise the second message answers the first case,
      // telling someone to connect a bank they already connected while its
      // data is being pulled.
      if (r.started === false) {
        setMsg({ text: "A sync is already running — this one didn't start. "
                       + "Give it a minute.", bad: false });
      } else if (!items.length) {
        setMsg({ text: "No synced connections — connect a bank on the " +
                       "Accounts page first.", bad: false });
      } else {
        setMsg({ text: "Synced — " +
                       items.map(([k, v]) => `${k}: ${v}`).join(" · "),
                 bad: items.some(([, v]) => v.startsWith("error")) });
      }
      onDone();
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  // the send-now email button lives on the Daily email card — beside the
  // recipients it goes to, one door per action
  const busy = sync.isPending;
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Maintenance</h2>
      <p className="mut">Sync runs hourly on its own — this runs one now.</p>
      {msg && <div className={"note " + (msg.bad ? "bad" : "good")}
                   onClick={() => setMsg(null)}>{msg.text}</div>}
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
        <button disabled={busy} onClick={() => sync.mutate()}>
          {sync.isPending ? "syncing…" : "Sync all connections now"}</button>
      </div>
    </div>
  );
}

// who gets the scheduled mail — a list of people, not a line of text.
//
// The alternative is an `<input>` holding "a@x.com, b@y.com". Three things
// are wrong with that, and only the first is cosmetic:
//
//   * It cannot show STATUS. An address that has never been confirmed and
//     one the provider has been refusing for three days would look exactly
//     like one that works. Silence is this feature's whole failure mode, so
//     a control that cannot show it is the wrong control.
//   * Removing someone would mean editing punctuation. Delete the wrong
//     comma and two addresses silently merge into one invalid one.
//   * It hides the owner. The owner is mailed by construction
//     (`worker._recipients`), but such a field lists only the extras — which
//     is how it comes to READ as the whole list, and how filling it in gets
//     understood as replacing the owner rather than adding to them.
//
// Adds apply IMMEDIATELY (auto-save): pressing Add (or Enter) is the save.
// A typed-but-not-added draft says so on screen — the silent-discard
// failure mode, made visible instead of flushed.
function RecipientsEditor({ recipients, muted, status, onChange,
                            onMutedChange }: {
  recipients: string[];
  // addresses opted out of the scheduled mail — the per-person off
  // switch, the owner included; an always-on owner would make
  // "partner gets it, I don't" impossible
  muted: string[];
  status: NonNullable<SettingsData["email_recipient_status"]>;
  onChange: (next: string[]) => void;
  onMutedChange: (next: string[]) => void;
}) {
  const isMuted = (e: string) =>
    muted.some((m) => m.toLowerCase() === e.toLowerCase());
  const toggleMuted = (e: string) =>
    onMutedChange(isMuted(e)
      ? muted.filter((m) => m.toLowerCase() !== e.toLowerCase())
      : [...muted, e.toLowerCase()]);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const qc = useQueryClient();
  // "they never got it" is the one thing an owner can act on
  // themselves, so it is a button next to the row rather than a support
  // question. Rotates the link; the old one dies with it.
  // A refusal — most often the 10/hour limit, which is exactly what
  // someone clicking twice will hit — must be shown rather than swallowed,
  // and the pending state has to be per ROW: `resend.isPending` is
  // per-hook, so it would disable every row's button at once.
  const [resendMsg, setResendMsg] = useState<
    { text: string; bad: boolean } | null>(null);
  const [resending, setResending] = useState<string | null>(null);
  const resend = useMutation({
    mutationFn: (email: string) => api.recipientInviteResend(email),
    onMutate: (email) => { setResending(email); setResendMsg(null); },
    onSuccess: (r, email) => {
      setResendMsg({ text: `Invitation re-sent to ${email}.`, bad: false });
      // the response carries the refreshed per-recipient status — the only
      // part of the settings view a resend can move
      patchQuery<SettingsData>(qc, ["settings"],
        { email_recipient_status: r.email_recipient_status });
    },
    onError: (e) => setResendMsg({ text: errText(e), bad: true }),
    onSettled: () => setResending(null),
  });
  const byEmail = new Map(status.map((s) => [s.email.toLowerCase(), s]));
  const owner = status.find((s) => s.owner);
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
      setErr("That doesn't look like an email address."); return;
    }
    const dupe = recipients.some((r) => r.toLowerCase() === v.toLowerCase())
      || (owner && owner.email.toLowerCase() === v.toLowerCase());
    if (dupe) {
      // not an error worth a red message — say why nothing happened and
      // clear the box, which is what the person meant anyway
      setErr(owner && owner.email.toLowerCase() === v.toLowerCase()
        ? "That's you — you're already on the list."
        : "Already on the list.");
      setDraft(""); return;
    }
    setErr(null); setDraft("");
    onChange([...recipients, v]);
  };
  const rows = [
    ...(owner ? [{ email: owner.email, removable: false }] : []),
    ...recipients.map((e) => ({ email: e, removable: true })),
  ];
  return (
    <div>
      <label style={{ display: "block" }}>Who receives it</label>
      <div style={{ display: "grid", gap: ".35rem", margin: ".35rem 0 .6rem" }}>
        {rows.map((r) => {
          const s = byEmail.get(r.email.toLowerCase());
          const rowMuted = isMuted(r.email);
          return (
            <div key={r.email}
                 style={{ display: "flex", alignItems: "center", gap: ".5rem",
                          padding: ".45rem .6rem", background: "var(--hover)",
                          border: "1px solid var(--line)",
                          borderRadius: "var(--rs)",
                          opacity: rowMuted ? 0.6 : 1 }}>
              {/* the per-person switch — the OWNER'S included. Removing a
                  row deletes the person from the list; this keeps them on
                  it and just stops their copy, which is the only way
                  "partner gets it, I don't" can exist. */}
              <input type="checkbox" checked={!rowMuted}
                     aria-label={`${r.email} receives the scheduled email`}
                     title={rowMuted ? "not receiving — tick to resume"
                                     : "receiving — untick to stop their copy"}
                     onChange={() => toggleMuted(r.email)} />
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden",
                             textOverflow: "ellipsis" }}>{r.email}</span>
              {rowMuted
                ? <span style={{ fontSize: 12, whiteSpace: "nowrap",
                                 padding: ".05rem .45rem",
                                 borderRadius: "999px",
                                 border: "1px solid var(--mut)",
                                 color: "var(--mut)" }}
                        title="Opted out of the scheduled email.">
                    not receiving</span>
                : <RecipientBadge status={s} owner={!r.removable} />}
              {/* A failing address needs Resend MOST, so the button cannot
                  hide on invite === "accepted" — that is exactly what a
                  bounced-but-accepted recipient is. Accepting an invite
                  clears the bounce, so re-inviting is the recovery, and it
                  has to be reachable from here. */}
              {r.removable && s && (s.delivery
                || (s.invite !== "accepted" && s.invite !== "declined")) && (
                <button title={`Send the invitation to ${r.email} again`}
                        disabled={resending === r.email}
                        style={{ padding: ".1rem .5rem", lineHeight: 1.4,
                                 fontSize: 12 }}
                        onClick={() => resend.mutate(r.email)}>
                  {resending === r.email ? "sending…"
                    : s.invite ? "Resend" : "Invite"}</button>)}
              {/* a word, not a bare ×: a glyph's meaning lives in a hover
                  title, which a phone never shows, and this is the one
                  control here that cannot be undone by re-typing */}
              {r.removable && (
                <button aria-label={`Remove ${r.email}`}
                        style={{ padding: ".1rem .5rem", lineHeight: 1.4,
                                 fontSize: 12, background: "none",
                                 border: "none", color: "var(--mut)" }}
                        onClick={() => onChange(
                          recipients.filter((x) => x !== r.email))}>remove</button>)}
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap" }}>
        <input value={draft} type="email" placeholder="partner@example.com"
               aria-label="Add an email recipient"
               style={{ flex: "1 1 14rem", minWidth: 0 }}
               onChange={(e) => { setDraft(e.target.value); setErr(null); }}
               // Enter adds — the whole point of this control is that adding
               // someone is one gesture. preventDefault so it can never
               // submit an enclosing form instead.
               onKeyDown={(e) => {
                 if (e.key === "Enter") { e.preventDefault(); add(); }
               }} />
        <button onClick={add} disabled={!draft.trim()}>Add</button>
      </div>
      {/* the failure mode to avoid is a typed address SILENTLY discarded;
          with auto-save there is no Save to flush it, so the truth goes on
          screen instead of a claim of success */}
      {draft.trim() && !err && (
        <p className="sub" style={{ marginTop: ".3rem" }}>
          Not added yet — press Add (or Enter) to save and invite.</p>
      )}
      {err && <p className="note bad" style={{ marginTop: ".3rem" }}
                 role="alert">{err}</p>}
      {resendMsg && (
        <p className={"note " + (resendMsg.bad ? "bad" : "good")}
           role="status" style={{ marginTop: ".3rem" }}
           onClick={() => setResendMsg(null)}>{resendMsg.text}</p>)}
      <p className="sub" style={{ marginTop: ".3rem" }}>
        Adding someone emails them an invitation right away — they start
        receiving the summary once they accept.
      </p>
    </div>
  );
}

// The one-word answer to "is this person actually getting it". Ordered by
// what matters most: a bouncing address beats a pending invitation, because
// it is a fact rather than a step somebody has yet to take.
// "member" rows appear only when NO list is configured: the worker then
// mails every verified user, so listing the owner alone would have a
// three-person household read "you" while all three get the balances.
function RecipientBadge({ status, owner }: {
  status?: NonNullable<SettingsData["email_recipient_status"]>[number];
  owner: boolean;
}) {
  const pill = (text: string, color: string, title?: string) => (
    <span title={title} style={{ fontSize: 12, whiteSpace: "nowrap",
            padding: ".05rem .45rem", borderRadius: "999px",
            border: `1px solid ${color}`, color }}>{text}</span>);
  if (!status)
    // typed into the box but never saved — the server has no opinion yet
    return pill("not saved yet", "var(--mut)");
  if (status.delivery)
    return pill(status.delivery === "complained"
      ? "marked as spam" : "not arriving", "var(--red)",
      status.delivery_reason ?? undefined);
  if (owner) return pill("you", "var(--mut)");
  if (status.via === "member")
    return pill("receiving — household member", "var(--mut)",
                "No recipient list is configured, so everyone with a "
                + "verified account here receives the daily email. Add an "
                + "address above to make the list explicit.");
  // for everyone else the invitation IS the status, because it is the
  // thing that decides whether mail flows. Deliberately the only pill: one
  // describing the person's own ACCOUNT answers a different question from
  // "did they agree to receive this household's money", and showing both
  // is two pills answering neither.
  if (status.invite === "accepted")
    return pill("invite accepted", "var(--green)",
                "They confirmed — this address receives the email.");
  if (status.invite === "invited")
    return pill("invite sent", "var(--amber, #e6c874)",
                "Waiting for them to confirm. Nothing else is sent to this "
                + "address until they do.");
  if (status.invite === "expired")
    return pill("invite expired", "var(--amber, #e6c874)",
                "The link timed out after 14 days — Resend to send a fresh "
                + "one.");
  if (status.invite === "declined")
    return pill("declined", "var(--mut)",
                "They said no. Re-adding them will not ask again.");
  // saved, but never invited — a restored backup (invitations don't travel
  // in an export) or an invite that couldn't be minted. Actionable, so it
  // says so rather than showing a neutral pill.
  return pill("not invited", "var(--amber, #e6c874)",
              "This address hasn't been asked yet, so it isn't receiving "
              + "the email.");
}

// this instance's mail is not reaching this address, and here is the provider's
// own reason. Shown in Settings → Email & Push, which is where both the banner and
// the worker's log line point, so the three agree on one destination.
//
// The tone is deliberate: this is not the user's fault to be scolded for,
// it is a fact they are the only person who can fix, so the card leads with
// what is happening, quotes the provider verbatim, and ends with the one
// control that resolves it.
function DeliveryProblemCard({ state, email }: {
  state: NonNullable<Me["email_delivery"]>; email?: string;
}) {
  const complained = state.state === "complained";
  return (
    <div className="card" style={{ borderColor: "rgba(224,105,93,.4)" }}>
      <h2>Your email isn't arriving</h2>
      <p className="mut">
        {complained
          ? <>Mail to <b>{email}</b> was reported as spam, so we stopped
              sending it. Scheduled emails are paused.</>
          : <>We couldn't deliver to <b>{email}</b>, so the scheduled emails
              are paused. Sending to an address that bounces every morning
              gets the rest of our mail treated as spam too.</>}
      </p>
      {state.reason && (
        <p className="sub" style={{ fontFamily: "monospace" }}>
          {state.bounce_type ? state.bounce_type + ": " : ""}{state.reason}
        </p>)}
      {state.did_you_mean && (
        <p><b>Did you mean {state.did_you_mean}?</b> The domain you have looks
          like a near-miss of a common one.</p>)}
      <p className="sub">
        {complained
          ? "Mark a message as \"not spam\" in your mail client, then change "
            + "and re-confirm the address below to start delivery again."
          : "Fix the address and confirm the link we send to it — that's what "
            + "restarts delivery. Everything you missed stays in the app."}
      </p>
      <Link className="btn" to="/settings/security">Change your email address</Link>
    </div>
  );
}

// Change the login email/username (hosted: re-verifies). Re-auth like
// PasswordCard — the /api/email/change door does password + TOTP.
function EmailCard({ current, onChanged }: {
  current?: string; onChanged: () => void;
}) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const change = useMutation({
    // the rotation drops every browser's web push but this one's — name it
    mutationFn: async () => {
      const keep = await ownPushEndpoint();
      return api.emailChange(email.trim(), keep);
    },
    onSuccess: (r) => {
      // the change is already saved — the hint is a nudge, never a
      // refusal (some real domains genuinely look like typos of big ones).
      // It rides on the success message so it is impossible to miss and
      // impossible to be blocked by.
      const hint = r.hint ? ` Did you mean ${r.hint}?` : "";
      const pkr = r as { ok: boolean; passkeys_removed?: number;
                         passkeys_kept?: number };
      const cost = pkr.passkeys_removed
        ? ` ${pkr.passkeys_removed} passkey(s) were removed — re-add them.`
        : "";
      // the key that proved this change survives when it is the account's
      // last second factor — the same rule the password card reports
      const kept = pkr.passkeys_kept
        ? ` ${pkr.passkeys_kept} passkey(s) kept — your only second factor.`
        : "";
      setMsg({ text: (r.verify_sent
        ? "Email changed — check the new address for a verification link."
        : "Email changed.") + cost + kept + hint, bad: false });
      setEmail("");
      // the heading reads the address off ["me"]; a hosted change also
      // un-verifies it until the new inbox clicks through
      patchQuery<Me>(qc, ["me"], { email: r.email,
        ...(r.verify_sent ? { verified: false } : {}) });
      // the patch covers the address; the old inbox's bounce state and the
      // hosted verify banner are the server's to recompute
      qc.invalidateQueries({ queryKey: ["me"] });
      onChanged();
    },
    onError: (e) => {
      const m = String(e instanceof Error ? e.message : e);
      setMsg({ text:
        m.includes("already in use") ? "That email is already in use."
        : m.includes("look like an email") ? "That doesn't look like an email."
        : m.includes("too many requests") ? "Too many attempts - try later."
        : errText(e), bad: true });
    },
  });
  return (
    <div className="card">
      <h2>Email / username</h2>
      <p className="mut">Your login email is <b>{current}</b>. Change it
        below — you'll confirm it's you first. The email is your
        login identifier, so changing it is a credential rotation: every
        other session is signed out, and every passkey, signed-in phone,
        script token and pending family invite is removed — re-add them
        after.</p>
      {msg && <div className={`note ${msg.bad ? "bad" : "good"}`}
                   onClick={() => setMsg(null)}>{msg.text}</div>}
      <div className="field-grid" style={{ maxWidth: "26rem" }}>
        <label>New email
          <input type="email" value={email} autoComplete="email"
                 onChange={(e) => setEmail(e.target.value)} /></label>
      </div>
      <button className="pri" style={{ marginTop: ".8rem" }}
              disabled={change.isPending || !email.trim()}
              onClick={() => change.mutate()}>
        {change.isPending ? "saving…" : "Change email"}
      </button>
    </div>
  );
}

function PasswordCard() {
  const qc = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);

  const change = useMutation({
    // the rotation drops every browser's web push but this one's — name it
    mutationFn: async () => {
      const keep = await ownPushEndpoint();
      return api.passwordChange(current, next, needTotp ? totp : undefined,
                                keep);
    },
    onSuccess: (r) => {
      // the server reports what the rotation actually cost, so a passkey
      // or phone that stops working right after is explained, not a mystery
      const c = r as { ok: boolean; passkeys_removed?: number;
                       passkeys_kept?: number;
                       tokens_revoked?: number; devices_revoked?: number };
      const cost = [
        c.passkeys_removed ? `${c.passkeys_removed} passkey(s) removed` : "",
        // the key that proved this change survives when it is the account's
        // last second factor; unexplained, a passkey that still works after
        // "every passkey was removed" reads as a bug
        c.passkeys_kept
          ? `${c.passkeys_kept} passkey(s) kept — your only second factor`
          : "",
        c.devices_revoked ? `${c.devices_revoked} device(s) signed out` : "",
        c.tokens_revoked ? `${c.tokens_revoked} script token(s) revoked` : "",
      ].filter(Boolean).join(", ");
      setMsg({ text: "Password changed — every other session was signed out"
               + (cost ? `; ${cost}.` : "."), bad: false });
      setCurrent(""); setNext(""); setConfirm(""); setTotp("");
      setNeedTotp(false);
      // the rotation empties these lists, and they live on this same page
      qc.invalidateQueries({ queryKey: ["sessions"] });
      qc.invalidateQueries({ queryKey: ["passkeys"] });
      qc.invalidateQueries({ queryKey: ["devices"] });
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
    onError: (e) => {
      const m = String(e instanceof Error ? e.message : e);
      if (m.includes("totp_required")) {
        setNeedTotp(true);
        setMsg({ text: "Two-factor is on — enter your 6-digit code to confirm.",
                 bad: true });
      } else if (m.includes("wrong password")) {
        setMsg({ text: "Current password is wrong.", bad: true });
      } else if (m.includes("one-time code")) {
        setMsg({ text: "That code didn't match — check your app's clock.",
                 bad: true });
      } else if (m.includes("at least 10")) {
        setMsg({ text: "New password must be at least 10 characters.",
                 bad: true });
      } else if (m.includes("too many requests")) {
        setMsg({ text: "Too many attempts — try again later.", bad: true });
      } else {
        setMsg({ text: errText(e), bad: true });
      }
    },
  });

  const submit = () => {
    if (next !== confirm) {
      setMsg({ text: "New passwords don't match.", bad: true });
      return;
    }
    if (next.length < 10) {
      setMsg({ text: "New password must be at least 10 characters.",
               bad: true });
      return;
    }
    setMsg(null);
    change.mutate();
  };

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Change password</h2>
      <p className="mut">Changing it is a full credential rotation: every
        other session is signed out, and every passkey, signed-in phone,
        script token and pending family invite is removed — re-add them
        after. That is deliberate: nothing enrolled by a thief survives.</p>
      {msg && (
        <div className={"note " + (msg.bad ? "bad" : "good")}
             onClick={() => setMsg(null)}>{msg.text}</div>
      )}
      <div className="field-grid">
        <label>Current password
          <input type="password" autoComplete="current-password"
                 value={current}
                 onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label>New password <span className="mut">(at least 10 characters)</span>
          <input type="password" autoComplete="new-password" value={next}
                 onChange={(e) => setNext(e.target.value)} />
        </label>
        <label>Confirm new password
          <input type="password" autoComplete="new-password" value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} />
        </label>
        {needTotp && (
          <label>6-digit code from your authenticator
            <input inputMode="numeric" autoFocus value={totp}
                   onChange={(e) => setTotp(e.target.value)} />
          </label>
        )}
      </div>
      <button style={{ marginTop: ".5rem" }}
              disabled={change.isPending || !current || !next || !confirm
                        || (needTotp && !totp)}
              onClick={submit}>
        {change.isPending ? "changing…" : "Change password"}
      </button>
    </div>
  );
}

function TwoFactorCard() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const [enroll, setEnroll] = useState<{ secret: string; qr: string } | null>(null);
  const [code, setCode] = useState("");
  // every door here is elevation-gated (a stolen session must not be able
  // to bolt 2FA on and sign the real owner out, nor strip it) — the
  // request wrapper's sheet collects the proof; no password boxes here
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);

  const fail = (e: unknown) => {
    const m = String(e instanceof Error ? e.message : e);
    if (m.includes("last_factor"))
      setMsg({ text: "This is your only two-factor method — add a passkey " +
                     "before turning off your authenticator app.", bad: true });
    else if (m.includes("doesn't match") || m.includes("bad one-time code"))
      setMsg({ text: "That code didn't match — check your app's clock.",
               bad: true });
    else if (m.includes("current one-time code"))
      setMsg({ text: "Two-factor is already on — enter your current code first.",
               bad: true });
    else setMsg({ text: errText(e), bad: true });
  };

  const start = useMutation({
    mutationFn: () => api.totpEnroll(),
    onSuccess: (r) => { setEnroll(r); setCode(""); setMsg(null); },
    onError: fail,
  });
  const confirm = useMutation({
    mutationFn: () => api.totpConfirm(enroll!.secret, code.trim()),
    onSuccess: (r) => {
      setEnroll(null); setCode("");
      // first-factor enrolment returns one-time recovery codes
      if (r.recovery_codes && r.recovery_codes.length)
        setRecovery(r.recovery_codes);
      setMsg({ text: "Two-factor is on — every other session was signed out.",
               bad: false });
      // the card's whole shape keys on ["me"].totp_enabled, and that key
      // has observers on every page — patch what the response proves
      // instead of refetching it
      patchQuery<Me>(qc, ["me"], { totp_enabled: true, needs_2fa: false,
        ...(r.recovery_codes?.length
          ? { recovery_codes_left: r.recovery_codes.length } : {}) });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: fail,
  });
  // one-time recovery codes to display after enrol / regenerate
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const regen = useMutation({
    // elevation-gated; the live code a TOTP account owes was collected at
    // elevate time, so nothing is typed here
    mutationFn: () => api.recoveryRegenerate(),
    onSuccess: (r) => { setRecovery(r.recovery_codes);
                        // the unused-codes count is the batch just minted
                        patchQuery<Me>(qc, ["me"],
                          { recovery_codes_left: r.recovery_codes.length }); },
    onError: fail,
  });
  const disable = useMutation({
    // elevation-gated AND the route's own live code — a stolen signed-in
    // tab plus one glimpsed code must not strip the second factor
    mutationFn: () => api.totpDisable(code.trim()),
    onSuccess: () => {
      setCode("");
      setMsg({ text: "Two-factor is off.", bad: false });
      // flip the flag the card keys on now; whether a hosted account is
      // back to owing a second factor depends on its passkeys, which only
      // the server knows — hence the refetch too
      patchQuery<Me>(qc, ["me"], { totp_enabled: false });
      qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: fail,
  });

  if (me.isPending || me.isError) return null;
  const enabled = me.data.totp_enabled;
  const busy = start.isPending || confirm.isPending || disable.isPending;

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Two-factor authentication</h2>
      {msg && (
        <div className={"note " + (msg.bad ? "bad" : "good")}
             onClick={() => setMsg(null)}>{msg.text}</div>
      )}
      {/* one-time recovery codes shown once after enrol/regenerate */}
      {recovery && (
        <div className="note good" style={{ display: "block" }}>
          <b>Recovery codes</b> — save these somewhere safe. Each works once
          to sign in if you lose your authenticator or passkey; they won't be
          shown again.
          <div style={{ fontFamily: "monospace", fontSize: 14,
               lineHeight: 1.9, userSelect: "all", marginTop: ".4rem" }}>
            {recovery.map((c) => <div key={c}>{c}</div>)}
          </div>
          <button style={{ marginTop: ".4rem" }}
                  onClick={() => { navigator.clipboard?.writeText(
                    recovery.join("\n")); setRecovery(null); }}>
            Copy & dismiss</button>
        </div>
      )}
      {/* This panel must stay OUTSIDE the `enabled` branch: a passkey-only
          account also has recovery codes (they are the escape hatch when
          the key is lost), and inside the branch it would have no way to
          see how many were left or mint new ones — leaving the person who
          most needs the escape hatch as the one who cannot reach it.
          Either shape confirms through the elevation sheet. */}
      {me.data.recovery_codes_left !== undefined && (
        <p className="sub">
          Recovery codes: <b>{me.data.recovery_codes_left}</b> unused
          {me.data.recovery_codes_left <= 2 &&
            <span style={{ color: "var(--amber)" }}> — running low</span>}
          {" · "}
          <button disabled={busy || regen.isPending}
            style={{ background: "none", border: "none", padding: 0,
              color: "var(--blue)", cursor: "pointer", font: "inherit",
              textDecoration: "underline" }}
            onClick={() => regen.mutate()}>
            {regen.isPending ? "generating…" : "regenerate"}</button>
        </p>
      )}
      {enabled ? (
        <>
          <p><span className="pill g">enabled</span>{" "}
            <span className="mut">Signing in requires your password and a
            6-digit code.</span></p>
          <div className="field-grid">
            <label>Current 6-digit code <span className="mut">(to turn 2FA off)</span>
              <input inputMode="numeric" autoComplete="one-time-code"
                     value={code} onChange={(e) => setCode(e.target.value)} />
            </label>
          </div>
          <button style={{ marginTop: ".5rem" }}
                  disabled={busy || !code.trim()}
                  onClick={() => disable.mutate()}>
            {disable.isPending ? "turning off…" : "Turn off 2FA"}
          </button>
        </>
      ) : enroll ? (
        <>
          <p className="mut">Scan this QR with your authenticator app (or type
            the secret in manually), then enter the 6-digit code it shows to
            confirm. Your existing sign-in keeps working until you confirm.</p>
          {enroll.qr && (
            <img src={enroll.qr} alt="Authenticator setup QR code"
                 width={180} height={180}
                 style={{ display: "block", background: "#fff", padding: 8,
                          borderRadius: 8, margin: ".2rem 0 .6rem" }} />
          )}
          <p>Secret (base32):{" "}
            <code style={{ userSelect: "all" }}>{enroll.secret}</code></p>
          <div className="field-grid">
            <label>6-digit code from your authenticator
              <input inputMode="numeric" autoComplete="one-time-code" autoFocus
                     value={code} onChange={(e) => setCode(e.target.value)} />
            </label>
          </div>
          <div style={{ marginTop: ".5rem", display: "flex", gap: ".5rem" }}>
            <button className="pri" disabled={busy || !code.trim()}
                    onClick={() => confirm.mutate()}>
              {confirm.isPending ? "confirming…" : "Confirm and enable"}
            </button>
            <button disabled={busy}
                    onClick={() => { setEnroll(null); setCode(""); setMsg(null); }}>
              cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="mut">Add a second factor: after your password, sign-in
            asks for a 6-digit code from an authenticator app. Confirming
            enrollment signs out every other session.</p>
          <p className="mut" style={{ fontSize: 13 }}>
            You'll confirm it's you first, so someone who steals a
            signed-in tab can't turn on 2FA and lock you out of your own
            account.</p>
          <button disabled={busy} onClick={() => start.mutate()}>
            {start.isPending ? "starting…" : "Enable 2FA"}
          </button>
        </>
      )}
    </div>
  );
}

/** A household member's own daily-email switch. The household schedule
 *  (hour, verdict-only vs full) is the owner's; this only says whether the
 *  mail reaches THIS inbox — POST /api/me/email toggles my address in
 *  the household's mute list; without it a viewer hits a 403 trying to
 *  reach any email setting at all. */
function MyEmailCard() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const d = me.data?.daily_email;
  const [err, setErr] = useState<string | null>(null);
  const set = useMutation({
    mutationFn: (muted: boolean) => api.meEmail(muted),
    // the label reads ["me"].daily_email.muted and the response says what
    // it now is — patch it; that key has observers on every page
    onSuccess: (r) => { setErr(null);
      qc.setQueryData<Me>(["me"], (old) => old && old.daily_email && {
        ...old, daily_email: { ...old.daily_email, muted: r.muted } }); },
    onError: (e) => setErr(errText(e)),
  });
  if (!d) return null;
  const hourLabel = (h: number) =>
    h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`;
  const receiving = d.on && !d.muted;
  return (
    <div className="card">
      <h2>Daily email</h2>
      <p className="mut">
        The household's verdict email goes out at <b>{hourLabel(d.hour)}</b>
        {" "}as {d.summary ? "the verdict-only card" : "the full report"} —
        the owner sets that. Whether it reaches <b>your</b> inbox is yours:
      </p>
      {!d.on && <p className="mut">The owner has the daily email turned off
        for the household right now.</p>}
      <p>
        <b>{receiving ? "You receive it." : d.on ? "You've turned it off for yourself."
                                          : "Off."}</b>{" "}
        {d.on && (
          <button className={d.muted ? "pri" : ""} disabled={set.isPending}
                  onClick={() => set.mutate(!d.muted)}>
            {d.muted ? "Turn on for me" : "Turn off for me"}
          </button>
        )}
      </p>
      {err && <p className="mut">{err}</p>}
    </div>
  );
}

/** A household member deletes their OWN login — the owner's account and
 *  every household row stay. Same re-auth as the
 *  owner's erasure door; lands on the sign-in page afterwards. */
function LeaveHouseholdCard() {
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const leave = useMutation({
    // elevation-gated; the typed phrase is the are-you-sure
    mutationFn: () => api.accountLeave(),
    onSuccess: () => { window.location.href = "/login"; },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <div className="card" style={{ borderColor: "rgba(224,105,93,.45)" }}>
      <h2 style={{ color: "var(--red)" }}>Delete my login</h2>
      <p className="mut">Removes <b>your</b> login from this household and
        signs you out. The household's data and the owner's account are not
        affected — the owner can invite you again any time.</p>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      <div className="field-grid" style={{ maxWidth: "26rem" }}>
        <label>Type <b>delete my login</b> to confirm
          <input value={confirm} placeholder="delete my login"
                 onChange={(e) => setConfirm(e.target.value)} /></label>
      </div>
      <button className="pri" disabled={leave.isPending
                || confirm !== "delete my login"}
              style={{ marginTop: ".8rem", background: "var(--red)" }}
              onClick={() => leave.mutate()}>
        {leave.isPending ? "deleting…" : "Delete my login"}
      </button>
    </div>
  );
}

// Delete-account GUI over the existing /api/account/delete (full
// tenant erasure, FRESH elevation — the sheet asks again even inside an
// open window — rate-limited). Hosted CCPA duty and self-host courtesy
// alike; owner-only (viewers leave the household from Family, they don't
// erase it).
function DeleteAccountCard({ owner }: { owner: boolean }) {
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const del = useMutation({
    // the typed phrase is the are-you-sure; identity is the sheet's job
    mutationFn: () => api.accountDelete(),
    // land on the dedicated confirmation page, not a bare login form
    onSuccess: () => { window.location.href = "/app/account-deleted"; },
    onError: (e) => {
      const m = String(e instanceof Error ? e.message : e);
      setErr(m.includes("too many requests") ? "Too many attempts - try later."
        : errText(e));
    },
  });
  if (!owner) return null;
  return (
    <div className="card" style={{ borderColor: "rgba(224,105,93,.45)" }}>
      <h2 style={{ color: "var(--red)" }}>Delete account</h2>
      <p className="mut">Erases this household permanently: every account,
        transaction, budget, and setting — and signs everyone out. There is
        no undo. Export your data first (Data section) if you want a copy.</p>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      <div className="field-grid" style={{ maxWidth: "26rem" }}>
        <label>Type <b>delete everything</b> to confirm
          <input value={confirm} placeholder="delete everything"
                 onChange={(e) => setConfirm(e.target.value)} /></label>
      </div>
      <button className="pri" disabled={del.isPending
                || confirm !== "delete everything"}
              style={{ marginTop: ".8rem", background: "var(--red)" }}
              onClick={() => del.mutate()}>
        {del.isPending ? "deleting…" : "Delete my account forever"}
      </button>
    </div>
  );
}

// Consented support access — the tenant grants a bounded, revocable
// window during which an operator's data-touching actions are permitted
// and audited. Default (no grant) = the operator sees no data.
function SupportAccessCard({ owner }: { owner: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["support-access"], queryFn: api.supportAccess });
  const [hours, setHours] = useState(24);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  // The card is one of two faces keyed on the cached `granted`, so each
  // write patches that before anything else. The grant's expiry is the
  // server's clock plus the hours — close enough to show, so the refetch
  // that follows is for the exact timestamp, not the flip.
  const inval = () => qc.invalidateQueries({ queryKey: ["support-access"] });
  type Grant = { granted: boolean; expires_at: string | null;
                 reason: string | null };
  const grant = useMutation({
    // a stolen session alone must not open a support window — the route
    // is elevation-gated and the sheet collects the proof
    mutationFn: () => api.supportAccessGrant(hours, reason.trim()),
    onSuccess: () => {
      patchQuery<Grant>(qc, ["support-access"], {
        granted: true, reason: reason.trim() || null,
        expires_at: new Date(Date.now() + hours * 3600_000).toISOString() });
      setReason(""); setErr(null); inval();
    },
    onError: (e) => setErr(errText(e)),
  });
  const revoke = useMutation({
    mutationFn: () => api.supportAccessRevoke(),
    // nothing left to learn from the server after a revoke — the patch is
    // the whole truth
    onSuccess: () => patchQuery<Grant>(qc, ["support-access"],
      { granted: false, expires_at: null, reason: null }),
  });
  if (q.isPending || q.isError) return null;
  const g = q.data;
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Support access</h2>
      <p className="mut">Whoever runs this instance can never see your data
        unless you grant it. A grant is time-boxed, revocable, and every access it
        allows is logged.</p>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      {g.granted ? (
        <div className="note good" style={{ display: "block" }}>
          Support access is <b>on</b>{g.expires_at &&
            <> until {new Date(g.expires_at).toLocaleString()}</>}.
          {g.reason && <div className="sub">Reason: {g.reason}</div>}
          {owner && <button style={{ marginTop: ".5rem" }}
            disabled={revoke.isPending}
            onClick={() => revoke.mutate()}>revoke now</button>}
        </div>
      ) : owner ? (
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                      alignItems: "center" }}>
          <label className="mut" style={{ fontSize: 13 }}>for{" "}
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              <option value={1}>1 hour</option>
              <option value={24}>24 hours</option>
              <option value={72}>3 days</option>
              <option value={168}>7 days</option>
            </select></label>
          <input value={reason} placeholder="reason (optional)"
                 style={{ minWidth: "14rem" }}
                 onChange={(e) => setReason(e.target.value)} />
          <button className="pri" disabled={grant.isPending}
                  onClick={() => grant.mutate()}>grant support access</button>
        </div>
      ) : (
        <p className="mut">Only the account owner can grant support access.</p>
      )}
    </div>
  );
}

function SessionsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const devQ = useQuery({ queryKey: ["devices"], queryFn: api.devices });
  const [allErr, setAllErr] = useState<string | null>(null);
  // signing any other session or device out is elevation-gated on the
  // server; the request wrapper's sheet collects the proof
  const stepErr = (e: unknown) => setAllErr(errText(e));
  const devRevoke = useMutation({
    mutationFn: (body: { id: string }) => api.deviceRevoke(body),
    // the device leaves the list on the response; the refetch confirms
    onSuccess: (_r, v) => { setAllErr(null);
      removeFromList<{ devices: MobileDevice[] }, MobileDevice>(
        qc, ["devices"], "devices", (d) => d.id === v.id);
      qc.invalidateQueries({ queryKey: ["devices"] }); },
    onError: stepErr,
  });
  const revoke = useMutation({
    mutationFn: async (body: { all_others?: boolean; id?: string }) => {
      // kicking any other session drops the other browsers' push
      // subscriptions; name this browser's so it keeps its own
      const keep = await ownPushEndpoint();
      return api.sessionRevoke(keep ? { ...body, keep_push_endpoint: keep }
                                    : body);
    },
    // one row, or every row but this one, leaves on the response; the
    // refetch confirms. Signing out everywhere else also signs the phones
    // out, which is the only time the device list moves here.
    onSuccess: (_r, v) => { setAllErr(null);
      removeFromList<{ sessions: Session[] }, Session>(
        qc, ["sessions"], "sessions",
        (s) => v.all_others ? !s.current : s.id === v.id);
      qc.invalidateQueries({ queryKey: ["sessions"] });
      if (v.all_others) qc.invalidateQueries({ queryKey: ["devices"] }); },
    onError: stepErr,
  });
  if (q.isPending || q.isError) return null;
  const rows = q.data.sessions;
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Sessions</h2>
      <p className="mut">Everywhere you're signed in. Changing your password
        (or confirming 2FA) signs out every other session automatically.</p>
      <table>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}>
              <td>{browserLabel(s.user_agent)}
                {s.ip && <span className="mut"> · {s.ip}</span>}
                {s.current && <span className="pill g" style={{ marginLeft: 6 }}>this device</span>}
              </td>
              <td className="mut">signed in {mmdd(s.created_at)}
                {s.last_seen && ` · active ${mmdd(s.last_seen)}`}
                {` · expires ${mmdd(s.expires_at)}`}</td>
              <td style={{ textAlign: "right" }}>
                {!s.current && (
                  <button disabled={revoke.isPending
                              && (revoke.variables?.id === s.id
                                  || !!revoke.variables?.all_others)}
                          title="Sign this session out"
                          onClick={() => revoke.mutate({ id: s.id })}>
                    {revoke.isPending && revoke.variables?.id === s.id
                      ? "signing out…" : "sign out"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {(devQ.data?.devices.length ?? 0) > 0 && (
        <>
          <h3 style={{ marginTop: "1rem" }}>Mobile devices</h3>
          <p className="mut">Phones and tablets signed in with the Oikonome
            app. Signing one out here makes it ask for your login again.</p>
          <table>
            <tbody>
              {devQ.data!.devices.map((d) => (
                <tr key={d.id}>
                  <td>{d.device_name || "Mobile device"}
                    {d.platform && <span className="mut"> · {d.platform}</span>}
                  </td>
                  <td className="mut">added {mmdd(d.created_at)}
                    {d.last_seen && ` · active ${mmdd(d.last_seen)}`}
                    {d.push === "on" && " · push on"}
                    {d.push === "dead" && " · push broken (open the app)"}</td>
                  <td style={{ textAlign: "right" }}>
                    <button disabled={devRevoke.isPending
                                && devRevoke.variables?.id === d.id}
                            title="Sign this device out"
                            onClick={() => devRevoke.mutate({ id: d.id })}>
                      {devRevoke.isPending && devRevoke.variables?.id === d.id
                        ? "signing out…" : "sign out"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {(rows.filter((s) => !s.current).length > 0
        || (devQ.data?.devices.length ?? 0) > 0) && (
        <>
          {allErr && <div className="note bad"
            onClick={() => setAllErr(null)}>{allErr}</div>}
          <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem",
                        flexWrap: "wrap", alignItems: "center" }}>
            <button disabled={revoke.isPending}
                    onClick={() => revoke.mutate({ all_others: true })}>
              Sign out everywhere else
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function browserLabel(ua: string) {
  if (!ua) return "Unknown device";
  if (/python|httpx|curl/i.test(ua)) return "API client";
  const browser = /firefox/i.test(ua) ? "Firefox"
    : /edg/i.test(ua) ? "Edge"
    : /chrome|chromium/i.test(ua) ? "Chrome"
    : /safari/i.test(ua) ? "Safari" : null;
  const os = /iphone|ipad/i.test(ua) ? "iOS"
    : /android/i.test(ua) ? "Android"
    : /windows/i.test(ua) ? "Windows"
    : /mac os/i.test(ua) ? "macOS"
    : /linux/i.test(ua) ? "Linux" : null;
  if (browser && os) return `${browser} on ${os}`;
  if (browser) return browser;
  if (/mobile/i.test(ua)) return "Mobile browser";
  return ua.slice(0, 40);
}


// ---- default home page, per client ----
// Household-level like `theme` (the settings document has no per-user
// half): where the web app opens, and which tab the mobile app opens on.
// Both halves are editable from both clients so the phone can set the
// laptop's landing page and vice versa.
function HomePageCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const [err, setErr] = useState<string | null>(null);
  // Per-KEY request sequencing (the two selects are independent saves):
  // a slower request for the same key must neither roll back nor
  // overwrite a later one that already landed — and saveSettings writes
  // the whole document back, so even a slow SUCCESS would revert a newer
  // choice unless the newest value is re-applied on top of it.
  const seq = useRef<Record<string, number>>({});
  const latest = useRef<Record<string, string>>({});
  // a viewer cannot write settings; the card is editing chrome
  if (!canEdit(meQ) || !q.data) return null;
  const set = (key: "home_web" | "home_mobile", value: string) => {
    const prev = q.data?.[key];
    const mine = (seq.current[key] = (seq.current[key] ?? 0) + 1);
    latest.current[key] = value;
    patchQuery<SettingsData>(qc, ["settings"], { [key]: value });
    saveSettings(qc, { [key]: value })
      .then(() => {
        if (mine !== seq.current[key])       // a newer choice landed after
          patchQuery<SettingsData>(qc, ["settings"],
                                   { [key]: latest.current[key] });
      })
      .catch((e) => { if (mine !== seq.current[key]) return;
                      setErr(errText(e));
                      patchQuery<SettingsData>(qc, ["settings"],
                                               { [key]: prev }); });
  };
  // a page the household cannot reach is not a home: no entity, no
  // Business
  const webHomes = WEB_HOMES.filter(([r]) =>
    r !== "/business" || meQ.data?.has_business);
  return (
    <div className="card">
      <h2>Home page</h2>
      <p className="mut">Where each app opens. Applied once per launch —
        the logo still goes to Today.</p>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      <div className="field-grid">
        <label>Web app opens on
          <select value={webHomes.some(([r]) => r === q.data?.home_web)
                           ? q.data.home_web! : "/"}
                  onChange={(e) => set("home_web", e.target.value)}>
            {webHomes.map(([r, l]) => <option key={r} value={r}>{l}</option>)}
          </select></label>
        <label>Mobile app opens on
          <select value={q.data.home_mobile || "index"}
                  onChange={(e) => set("home_mobile", e.target.value)}>
            {MOBILE_HOMES.map(([r, l]) =>
              <option key={r} value={r}>{l}</option>)}
          </select></label>
      </div>
    </div>
  );
}
