import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { ago, api, AuthError, errText, req, toast, type Connection,
         type Settings } from "../api/client";
import { ext } from "../ext";
import { patchQuery, removeFromList } from "../api/cache";
import { sharedLock } from "../sharedlogin";
import { bankAwaitsConfirm, CONFIRM_TO_CONNECT, VerifyEmailCard }
  from "./VerifyEmailCard";
import { HISTORY_SAVING, historyRowNote, useHistorySaving, windowNarrower }
  from "./HistoryChoice";
import { cancelPending, finishPending, isCancelled, isPending, markPending,
         pendingLine, pollWait, sleepOrWake, usePlaidLinks,
         type PendingLinkKind } from "./plaidlinks";
// The file importer is a page of its own and opens only behind the
// "Import files" button, so its code arrives on that click rather than
// riding in with every screen that shows this hub.
const Import = lazy(() => import("../pages/Import"));
function FileImport() {
  return (
    <Suspense fallback={<span className="mut">loading…</span>}>
      <Import />
    </Suspense>
  );
}

/** The provider comparison grid + guided setup flows.
 *  Self-host: honest costs/effort per option and step-by-step flows with
 *  LIVE key validation. Hosted (me.hosted): Plaid/MX run under the
 *  operator's own credentials — no developer-account steps. */

type ProviderKey = "plaid" | "mx" | "simplefin" | "scripts" | "files";

/** The sentence a held bank-link door shows, or null when links through
 *  `provider` are open. After a credential rotation the household's own
 *  Plaid / MX client may be one a hijacker saved, so the server refuses a
 *  new link, a re-auth and the MX widget on it until the owner resumes in
 *  the Settings banner or removes the keys; the button is closed here with
 *  the server's own sentence (a stale tab still gets the server's 400).
 *  Only the owner is told which clients are held, so only the owner's
 *  buttons close ahead of the server. Mobile: bankLinksHeld in
 *  mobile/src/lib/pure.ts. */
export function bankLinkHoldNote(s: Settings | undefined,
                                 provider: "plaid" | "mx"): string | null {
  const p = s?.outbound_paused;
  if (!p?.aggregator
      || !(p.aggregator_keys ?? []).some((k) => k.provider === provider))
    return null;
  const why = p.reason === "recovery" ? "your account was recovered"
    : "your password, login email or second factor changed";
  return `New bank links are on hold since ${why}: confirm the `
    + `${provider === "mx" ? "MX" : "Plaid"} keys saved in Settings are `
    + "yours (Settings, top banner), or disconnect their banks and remove "
    + "the keys.";
}

/** What a finished Plaid session reports. `kind` is the server's word for
 *  how it ended: add / update on success; exited when the user left Link
 *  without finishing; add_failed / update_failed when the bank or the
 *  exchange refused; cancelled when the user told us they closed the tab. */
export type PlaidLinkResult = { kind?: string; institution?: string; error?: string;
                                /** "pending": the bank is saved and its
                                 *  first pull is queued on the worker */
                                first_sync?: "pending" | "ok" };

/** What a bank saved but not yet pulled says on its connection row and in
 *  the link's own result line. Mobile: FIRST_SYNC_RUNNING in
 *  mobile/src/lib/pure.ts. */
export const FIRST_SYNC_RUNNING =
  "first sync running — accounts appear in a minute";

/** A live Plaid connection whose first pull has not landed yet: no
 *  last_ok and nothing wrong with it. */
export function firstSyncPending(c: Connection): boolean {
  if (c.aggregator !== "plaid" || c.last_ok) return false;
  const kind = c.status_kind ?? "ok";
  return kind === "ok" && !(c.status || "").startsWith("error")
    && c.status !== "reaped";
}

/** Keep the connection list fresh while a first sync is running, and when
 *  one lands refresh what it fills (accounts, the verdict, the wizard).
 *  Returns the refetch interval for the caller's ["connections"] query. */
export function useFirstSyncLanding(conns: Connection[] | undefined
): number | false {
  const qc = useQueryClient();
  const waiting = useRef<Set<string>>(new Set());
  const pending = (conns ?? []).filter(firstSyncPending).map((c) => c.id);
  const key = pending.join(",");
  useEffect(() => {
    const now = new Set(key ? key.split(",") : []);
    const landed = [...waiting.current].some((id) => !now.has(id));
    waiting.current = now;
    if (landed) afterFirstPull(qc);
  }, [key, qc]);
  return pending.length ? 5000 : false;
}

/** The sentence a finished Plaid session earns, and whether it changed
 *  anything. The session resolves normally for a close or a failure too
 *  (a thrown error would be retried as a network blip), so every caller
 *  must read `kind` — announcing "Linked" or "Reconnected" for a window the
 *  user closed tells them a bank is connected when it is not.
 *  `mode`: add = a new bank; fix = re-authenticate a broken one; manage =
 *  the shared-accounts checklist. */
export function plaidLinkOutcome(s: PlaidLinkResult,
                                 mode: "add" | "fix" | "manage"
): { ok: boolean; text: string } {
  const detail = s.error ? ` (${s.error})` : "";
  switch (s.kind) {
    case "cancelled":
    case "exited":
      return { ok: false, text: mode === "add"
        ? "No bank was linked — the sign-in window was closed."
        : "No change — the window was closed." };
    case "add_failed":
      return { ok: false, text: `The bank link didn't complete${detail}.` };
    case "update_failed":
      return { ok: false, text: (mode === "fix" ? "Reconnect" : "That")
        + ` didn't complete — the connection still reports an error${detail}.` };
    default:
      return { ok: true, text: mode === "add"
        ? `Linked ${s.institution || "bank"}`
          + (s.first_sync === "pending" ? ` — ${FIRST_SYNC_RUNNING}.` : ".")
        : mode === "fix" ? "Reconnected." : "Connected accounts updated." };
  }
}

/** Open a new tab at a URL the server has yet to hand back, without
 *  tripping the pop-up blocker. Call it from the click handler itself: the
 *  tab opens blank NOW, while the click's user activation still holds, and
 *  is pointed at the URL once `fetch` resolves. A window opened after that
 *  round-trip has lost the activation and is blocked silently. Only https
 *  is followed; the tab is closed again if the fetch fails. */
export async function openTabAt<T>(fetch: () => Promise<T>,
                                   urlOf: (r: T) => string,
                                   who: string): Promise<T> {
  // no "noopener" in the features: with it window.open returns null, and
  // the handle is needed to navigate the tab. The opener link is cut by
  // hand before the cross-origin page loads.
  const tab = window.open("", "_blank");
  if (!tab)
    throw new Error("Your browser blocked the sign-in window — allow "
                    + "pop-ups for this site and try again.");
  try {
    const r = await fetch();
    const url = urlOf(r);
    if (!/^https:/i.test(url))
      throw new Error(`${who} returned a non-https link URL`);
    tab.opener = null;
    tab.location.href = url;
    return r;
  } catch (e) {
    tab.close();
    throw e;
  }
}

/** Open Plaid Hosted Link only (no oikonome waiting tab). Caller stays put
 *  and this promise resolves when the bank tab finishes (or fails).
 *
 *  Call it straight from the click handler. The bank tab is opened blank
 *  BEFORE the round-trip that creates the session and pointed at Plaid
 *  once the URL arrives: a window opened after an await has lost the
 *  click's user activation, so pop-up blockers stop it silently — and the
 *  caller then waited 15 minutes on a tab that never opened. */
export async function openPlaidHostedLink(itemId = "", opts?: {
  /** polled between status checks — return true to stop waiting (the user
   *  told US they closed the bank tab; the server can't always know) */
  cancelled?: () => boolean;
  /** update mode only: open Link's shared-accounts checklist so an
   *  account can be deselected — the only way to stop Plaid billing it */
  manageAccounts?: boolean;
  /** add mode only: a linked bank to relink with the household's (wider)
   *  history window — it stays live and syncing until the new link of the
   *  same bank is saved, and is released only then (a sign-in at another
   *  bank just adds that one) */
  relinkItemId?: string;
}): Promise<PlaidLinkResult> {
  const started = await openTabAt(
    () => api.plaidLinkStart(itemId, !!opts?.manageAccounts,
                             itemId ? "" : (opts?.relinkItemId ?? "")),
    (r) => r.hosted_link_url, "Plaid");
  const s = await pollPlaidLink(started.link_token, opts?.cancelled);
  // the person said they closed the bank window: tell the server, or the
  // abandoned add session keeps holding a connection slot and the next
  // Connect is refused (it still finishes it if Plaid reports a bank)
  if (s.kind === "cancelled" && !itemId)
    void setPlaidLinkAside(started.link_token);
  return s;
}

/** Open Plaid Hosted Link and come back as soon as the bank tab is on
 *  its way: the session is watched from here on as a pending row (the
 *  plaidlinks store), with its own cancel and its own outcome line, so
 *  the button that started it is free for the next bank. Resolves with
 *  the session's token once the tab is open; the answer lands in the
 *  store (and refreshes what a new bank changes).
 *  `mode` words the outcome; relink = the same bank linked afresh for a
 *  wider history window (relinkItemId stays live until the new link of the
 *  same bank is in, then is released). */
export async function startPlaidLink(qc: ReturnType<typeof useQueryClient>,
                                     opts: { mode: PendingLinkKind;
                                             relinkItemId?: string }
): Promise<string> {
  const started = await openTabAt(
    () => api.plaidLinkStart("", false, opts.relinkItemId ?? ""),
    (r) => r.hosted_link_url, "Plaid");
  const token = started.link_token;
  markPending(token, opts.mode, opts.relinkItemId);
  void pollPlaidLink(token, () => isCancelled(token))
    .then((s) => {
      // the person said they closed the bank window: tell the server, or
      // the abandoned session keeps holding a connection slot (it is
      // still finished if Plaid reports a bank after all)
      if (s.kind === "cancelled") void setPlaidLinkAside(token);
      const out = plaidLinkOutcome(s, "add");
      finishPending(token, s.kind === "cancelled" ? undefined : out);
      if (out.ok) afterFirstPull(qc);
      else for (const k of ["connections", "me"])
        qc.invalidateQueries({ queryKey: [k] });
    })
    .catch((e) => finishPending(token, { ok: false, text: errText(e) }));
  return token;
}

/** The sessions still open and the recent outcomes, under a connect
 *  button: one row each, with "cancel — I closed the bank window" (the
 *  server only learns about an exit Plaid reports, and a closed TAB
 *  reports nothing). */
export function PendingLinkRows() {
  const { pending, outcomes } = usePlaidLinks();
  if (!pending.length && !outcomes.length) return null;
  return (
    <div style={{ marginTop: ".4rem" }}>
      {pending.map((p) => (
        <p key={p.token} style={{ fontSize: 13, margin: ".2rem 0",
                                  color: "var(--green, #4a4)" }}>
          {pendingLine(p)}
          {!p.cancelled && (
            <button type="button" className="linklike mut"
              style={{ marginLeft: ".7rem", fontSize: 13 }}
              onClick={() => cancelPending(p.token)}>
              cancel — I closed the bank window
            </button>)}
        </p>
      ))}
      {outcomes.map((o) => (
        <p key={o.id} style={{ fontSize: 13, margin: ".2rem 0",
                               color: o.ok ? "var(--green, #4a4)" : "var(--red)" }}>
          {o.text}</p>
      ))}
    </div>
  );
}

/** Stop an add session holding a place under the institution cap and
 *  stop it being resumed. Best-effort: an older server simply 404s/405s. */
export function setPlaidLinkAside(linkToken: string): Promise<void> {
  return req(`/accounts/plaid/link/${encodeURIComponent(linkToken)}`,
             { method: "DELETE" })
    .then(() => undefined, () => undefined);
}

/** How long a client waits on one link session: the Hosted Link URL's
 *  30 minutes, plus a few for a person who opened it near the end and is
 *  still inside Link. Giving up sooner told the person the link timed out
 *  while the URL still worked. The server finishes the session either way
 *  (the LINK webhook and the worker's sweep); this is only how long the
 *  page keeps watching for the answer. */
export const PLAID_LINK_WAIT_MS = 35 * 60 * 1000;

/** The link sessions this tab is already watching, so the resume on
 *  mount (usePlaidLinkResume) never polls one twice. */
const watching = new Set<string>();

/** Poll one session until the server reports it finished (exchange + first
 *  sync). The server resolves kind:"exited" when Plaid stamps the session
 *  finished with nothing added (the in-Link exit button); the cancelled
 *  callback covers the case Plaid never hears about — a closed tab.
 *
 *  One failed request must not end the wait: a dropped connection, a
 *  restart or an edge timeout while the first sync runs would otherwise
 *  lose sight of a link the person just finished. Transient failures back
 *  off and keep polling until the deadline; only a definite refusal (a 4xx
 *  such as an unknown session) or a sign-out ends the wait early. */
export async function pollPlaidLink(linkToken: string,
                                    cancelled?: () => boolean,
                                    resume = false
): Promise<PlaidLinkResult> {
  watching.add(linkToken);
  try {
    const deadline = Date.now() + PLAID_LINK_WAIT_MS;
    // the normal tick, or the fast one for a while after a wake-up (the
    // tab regaining focus, the completion page calling over); a failed
    // request backs off from there
    let backoff = 0;
    while (Date.now() < deadline) {
      await sleepOrWake(backoff || pollWait());
      if (cancelled?.()) return { kind: "cancelled" };
      try {
        // a resumed session is watched for its answer only: the server
        // does not keep it counted under the institution cap for us
        const s = resume
          ? await req<PlaidLinkResult & { done: boolean }>(
              `/accounts/plaid/link/${encodeURIComponent(linkToken)}`
              + "/status?resume=1")
          : await api.plaidLinkStatus(linkToken);
        backoff = 0;
        if (s.done) return s;
      } catch (e) {
        if (!pollMayRetry(e)) throw e;
        backoff = Math.min((backoff || 2500) * 2, 30_000);
      }
    }
    throw new Error("Timed out waiting for the bank connection");
  } finally {
    watching.delete(linkToken);
  }
}

/** Pick up the household's bank links still able to finish — ones a
 *  closed or reloaded tab stopped watching while the person was in the
 *  bank's window — and say how each ended. Owner-only on the server; any
 *  refusal (a member, an older server) just means nothing to resume. */
export function usePlaidLinkResume(enabled: boolean): void {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    req<{ sessions: { link_token: string; relink_of?: string | null }[] }>(
      "/accounts/plaid/links/open")
      .then(({ sessions }) => {
        // an unmount before the list arrives starts nothing; the next
        // mount asks again
        if (!live) return;
        for (const { link_token, relink_of } of sessions ?? []) {
          if (watching.has(link_token) || isPending(link_token)) continue;
          // a row like a link started here, so a reloaded tab shows what
          // it is still waiting on (its cancel sets the session aside).
          // The poll outlives this mount on purpose: the store is
          // module-level, and a remount skips a token still being
          // watched, so a poll stopped on unmount would leave the session
          // with nobody watching it until the next full reload.
          // a relink comes back as the relink it is, so that bank's
          // "more history" door stays shut while it is open
          markPending(link_token, relink_of ? "relink" : "add",
                      relink_of ?? undefined);
          void pollPlaidLink(link_token, () => isCancelled(link_token), true)
            .then((s) => {
              if (s.kind === "cancelled") {
                void setPlaidLinkAside(link_token);
                finishPending(link_token);
                return;
              }
              finishPending(link_token);
              afterFirstPull(qc);
              toast(plaidLinkOutcome(s, "add").text);
            }).catch(() => finishPending(link_token));
        }
      })
      .catch(() => { /* nothing to resume */ });
    return () => { live = false; };
  }, [enabled, qc]);
}

/** A status poll failure worth trying again: no answer at all (offline,
 *  connection dropped), a server or gateway error, a request timeout or a
 *  rate limit. A sign-out and any other 4xx are final. */
function pollMayRetry(e: unknown): boolean {
  if (e instanceof AuthError) return false;
  const m = /^(\d{3}):/.exec(e instanceof Error ? e.message : String(e));
  if (!m) return true;
  const code = Number(m[1]);
  return code >= 500 || code === 408 || code === 429;
}

const ROWS: { key: ProviderKey; name: string; cost: string; effort: string;
              richness: string; hostedCost?: string;
              hostedEffort?: string }[] = [
  { key: "plaid", name: "Plaid", cost: "Free tier",
    effort: "Hard", richness: "Best",
    hostedCost: "Included", hostedEffort: "1 click" },
  { key: "mx", name: "MX", cost: "Free sandbox",
    effort: "Medium", richness: "Better",
    hostedCost: "Included", hostedEffort: "1 click" },
  { key: "simplefin", name: "SimpleFIN", cost: "$1.50/mo",
    effort: "Easy", richness: "Good" },
  { key: "scripts", name: "API scripts", cost: "Free",
    effort: "Technical", richness: "Varies" },
  { key: "files", name: "File imports", cost: "Free",
    effort: "None", richness: "Full history" },
];

const field = (v: string, set: (s: string) => void, ph: string,
               type = "text") => (
  <input type={type} value={v} placeholder={ph}
    onChange={(e) => set(e.target.value)}
    style={{ width: "100%", padding: ".5rem", background: "var(--hover)",
             border: "1px solid var(--line)", borderRadius: "var(--rs)",
             color: "var(--ink)" }} />
);

// A first pull lands accounts and transactions: the connection list, the
// accounts, the verdict and the wizard's progress move, and the settings
// view carries the provider flags. Nothing else on the client does —
// invalidating the whole cache here refetched every mounted query on the
// page for no reason.
// A first pull lands ledger rows, so everything derived from the ledger
// (lenses, reports, bills, merchants, reimbursements, business books,
// budget snapshots) is stale too — and these flows also run from Settings
// on a tenant that already has data on those pages. Prefix invalidates
// are free while the pages are unmounted.
function afterFirstPull(qc: ReturnType<typeof useQueryClient>) {
  for (const k of ["connections", "accounts", "settings", "today",
                   "onboarding", "txns", "lens-month", "lens-year", "report",
                   "bills", "merchants", "reimb", "biz", "budget-snapshots"])
    qc.invalidateQueries({ queryKey: [k] });
}

function Steps({ items }: { items: React.ReactNode[] }) {
  return (
    <ol style={{ margin: ".4rem 0 .8rem", paddingLeft: "1.3rem",
                 display: "flex", flexDirection: "column", gap: ".45rem" }}>
      {items.map((x, i) => <li key={i}>{x}</li>)}
    </ol>
  );
}

export function PlaidFlow({ onDone }: { onDone: () => void }) {
  void onDone;   // linking happens in-flow now; the grid's close button exits
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [env, setEnv] = useState("production");
  const [validated, setValidated] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const validate = useMutation({
    mutationFn: () => api.plaidValidate(clientId.trim(), secret.trim(), env),
    onSuccess: () => { setErr(null); setValidated(true); },
    onError: (e) => { setValidated(false); setErr(errText(e)); },
  });
  const save = useMutation({
    // keys saved ≠ done — the panel stays open and moves to the LINK step.
    // Closing it here and sending the user to the Accounts page (which the
    // strict wizard does not show) leaves keys saved, no bank linked, and a
    // sync step that looks like it is "missing" Plaid.
    mutationFn: () => api.plaidKeys(clientId.trim(), secret.trim(), env),
    onSuccess: (r) => {
      setErr(null);
      // the Link step below is gated on the cached flag — set it from what
      // was just saved so the step opens now, not after the refetch
      patchQuery<Settings>(qc, ["settings"], {
        plaid_secret_set: true, plaid_client_id: clientId.trim(),
        plaid_env: r.env ?? env });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  const keysReady = !!s.data?.plaid_secret_set;
  return (
    <>
      <Steps items={[
        <>Create a free Plaid developer account at{" "}
          <a href="https://dashboard.plaid.com/signup" target="_blank"
             rel="noreferrer">dashboard.plaid.com/signup</a>.</>,
        <>In the dashboard, open <b>Developer → Keys</b> and copy your{" "}
          <b>client_id</b> and the <b>secret</b> for your environment.
          Sandbox works right away with test banks; real banks need
          Production access (request it from the dashboard — approval can
          take a few days and isn't guaranteed).</>,
        <>Paste the keys here — they're validated against Plaid's API before
          anything is saved:</>,
      ]} />
      <div className="field-grid">
        <label>client_id{field(clientId, (v) => { setClientId(v); setValidated(false); }, "5f3…")}</label>
        <label>secret{field(secret, (v) => { setSecret(v); setValidated(false); }, "", "password")}</label>
        <label>Environment
          <select value={env}
            onChange={(e) => { setEnv(e.target.value); setValidated(false); }}>
            <option value="production">production (real banks)</option>
            <option value="sandbox">sandbox (test banks)</option>
          </select>
        </label>
      </div>
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem" }}>
        <button disabled={validate.isPending || !clientId || !secret}
          onClick={() => validate.mutate()}>
          {validate.isPending ? "checking with Plaid…" : "Validate keys"}
        </button>
        <button className="pri" disabled={!validated || save.isPending}
          title={validated ? "" : "validate first"}
          onClick={() => save.mutate()}>
          {save.isPending ? "saving…" : "Save keys"}
        </button>
        {validated && <span className="pill g" style={{ alignSelf: "center" }}>keys valid ✓</span>}
      </div>
      {keysReady && (
        <div style={{ marginTop: ".9rem", paddingTop: ".8rem",
                      borderTop: "1px solid var(--line)" }}>
          <p style={{ margin: "0 0 .5rem" }}>
            <b>Keys saved ✓ — now link your bank(s):</b></p>
          <PlaidConnectButton label="+ Link a bank via Plaid ↗"
            hint="Opens your bank's sign-in (Plaid) in a new tab. When you
              finish, the bank shows up above — link as many as you like,
              then hit Done connecting." />
        </div>
      )}
    </>
  );
}

/** Shared "+ Connect an account" control: opens Plaid only, polls from this page.
 *  `full` = the plan's institutions are all spent; the door is closed here
 *  rather than at the server's refusal, which on the hosted link path can
 *  land AFTER the token exchange — the household watches a bank it just
 *  authorized get removed again. */
function PlaidConnectButton({ label, hint, full = false, closed }: {
  label: string; hint: string; full?: boolean;
  // a sentence saying why the door is shut ahead of the server (an
  // address still to confirm); the button stays visible, disabled
  closed?: string | null;
}) {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const held = bankLinkHoldNote(st.data, "plaid");
  // the link token asks Plaid for the stored history window, which can
  // never be narrowed afterwards — a history choice still saving waits
  const saving = useHistorySaving();
  // busy only while the bank tab is on its way: from then on the session
  // is a pending row below, and the button is free for the next bank
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => {
    setStarting(true); setErr(null);
    try {
      await startPlaidLink(qc, { mode: "add" });
    } catch (e) {
      setErr(errText(e));
    } finally {
      setStarting(false);
    }
  };
  return (
    <div>
      <button className="pri" style={{ fontSize: 15 }}
        disabled={starting || full || !!held || !!closed || saving}
        title={closed ?? (saving ? HISTORY_SAVING
          : full ? "you are at this account's institution limit" : undefined)}
        onClick={() => void run()}>
        {starting ? "Opening your bank…" : label}
      </button>
      {held && <p style={{ fontSize: 13, color: "var(--amber)" }}>{held}</p>}
      {saving && !starting && <p className="mut" style={{ fontSize: 13 }}>
        {HISTORY_SAVING}</p>}
      <p className="mut" style={{ marginTop: ".5rem", fontSize: 13 }}>{hint}</p>
      <PendingLinkRows />
      {err && <p style={{ fontSize: 13, color: "var(--red)" }}>{err}</p>}
    </div>
  );
}

/** True while a relink of this bank for more history is still open in
 *  this tab. The old bank no longer leaves when the session starts, so
 *  its "more history" door would otherwise stay live beside the pending
 *  row (and the server refuses a second relink of the same bank). */
export function useRelinkPending(itemId: string | undefined): boolean {
  const { pending } = usePlaidLinks();
  return !!itemId
    && pending.some((p) => p.kind === "relink" && p.itemId === itemId);
}

/** "pull more history ↗" on a hosted-hub row whose bank was linked with a
 *  narrower window than the household's choice. Plaid fixes the window
 *  when an Item is created, so widening it means a fresh Item: the same
 *  bank is linked again while the old one stays live and syncing, and the
 *  old one is released (under a reason the institution cap ignores) only
 *  once the new link of the same bank is saved; the history already in
 *  is kept. Mobile: welcome.tsx LinkedBanks / accounts.tsx keep the same
 *  door. */
function HubMoreHistory({ c }: { c: Connection }) {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const held = bankLinkHoldNote(st.data, "plaid");
  const saving = useHistorySaving();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const relinking = useRelinkPending(c.id);
  const name = c.institution_name || c.id;
  const run = async () => {
    if (!window.confirm(`Pull more history from ${name}? You sign in at `
                        + "the bank again; what is already in is kept."))
      return;
    setBusy(true); setMsg(null);
    try {
      // the old bank stays listed and syncing until the new link is in;
      // this is only a refetch of what the session start may have moved
      await startPlaidLink(qc, { mode: "relink", relinkItemId: c.id });
      for (const k of ["connections", "me", "accounts"])
        qc.invalidateQueries({ queryKey: [k] });
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };
  if (relinking)
    return <small className="mut" style={{ marginLeft: ".4rem" }}>relinking…</small>;
  return (
    <>
      <button className="linked-x" disabled={busy || !!held || saving}
        title={held ?? (saving ? HISTORY_SAVING
          : "relink this bank with the wider history window — the window is fixed when a bank is linked")}
        onClick={() => void run()}>
        {busy ? "opening…" : "pull more history ↗"}</button>
      {msg && <small className="mut" style={{ marginLeft: ".4rem" }}>{msg}</small>}
    </>
  );
}

/** The repair door on a hosted-hub row. A red "needs attention" with only
 *  Disconnect beside it left the one destructive action as the answer to
 *  an expired sign-in. A broken login is re-authenticated in place (Link's
 *  update mode, same Item); a connection that was removed ("gone") has no
 *  token left, so it is linked afresh. A bank outage gets no button —
 *  signing in again cannot fix the bank's side (the Accounts page makes
 *  the same call). */
function HubFixButton({ c }: { c: Connection }) {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  // a re-auth hands the bank login to the same client a new link does
  const held = bankLinkHoldNote(st.data, "plaid");
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // linking a gone bank afresh mints a new token from the stored window
  const saving = useHistorySaving();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  if (c.aggregator !== "plaid") return null;
  const kind = c.status_kind
    ?? (c.status === "reaped" ? "gone"
        : (c.status || "").startsWith("error") ? "attention" : "ok");
  const gone = kind === "gone" || c.status === "reaped";
  if (!gone && kind !== "reauth" && kind !== "attention") return null;
  // linking a gone bank afresh is a new connection, which the allowance of
  // a trial still waiting on confirmation refuses; signing in again to a
  // live one adds nothing and stays open
  const closed = gone && bankAwaitsConfirm(me.data) ? CONFIRM_TO_CONNECT
                                                     : null;
  const why = held ?? closed ?? (gone && saving ? HISTORY_SAVING : null);
  const run = () => {
    setBusy(true); setNote(null);
    // new accounts at the bank are offered only with Link's account
    // checklist switched on
    void openPlaidHostedLink(gone ? "" : c.id,
      { manageAccounts: !gone && c.status === "new_accounts" })
      .then((s) => {
        const out = plaidLinkOutcome(s, gone ? "add" : "fix");
        setNote(out.text);
        if (out.ok)
          for (const k of ["connections", "accounts", "me"])
            qc.invalidateQueries({ queryKey: [k] });
      })
      .catch((e) => setNote(errText(e)))
      .finally(() => setBusy(false));
  };
  return (
    <>
      <button className="linked-x" disabled={busy || !!why}
        title={why ?? (gone ? "link this bank again"
                             : "sign in to the bank again")}
        onClick={run}>
        {busy ? "waiting…" : gone ? "reconnect ↗" : "fix ↗"}</button>
      {(note ?? why) && <small className="sub">{note ?? why}</small>}
    </>
  );
}

export function MxFlow({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [clientId, setClientId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [env, setEnv] = useState("sandbox");
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.mxKeys(clientId.trim(), apiKey.trim(), env),
    onSuccess: (r) => { setErr(null);
      // connect / sync below are gated on the cached flag
      patchQuery<Settings>(qc, ["settings"], {
        mx_api_key_set: true, mx_client_id: clientId.trim(),
        mx_env: r.env ?? env });
      qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e) => setErr(errText(e)),
  });
  // Keys already stored count, like PlaidFlow's keysReady: the credentials
  // are write-only, so re-opening this panel can never retype them, and
  // connect/sync take no body — they work straight off the stored key. In
  // session state alone, an owner returning to link another bank would find
  // both actions dead with no way to revive them.
  const keysReady = !!s.data?.mx_api_key_set;
  const held = bankLinkHoldNote(s.data, "mx");
  // a pull's start date is bounded by the stored history window
  const saving = useHistorySaving();
  const connect = useMutation({
    // the widget URL comes from the server response; openTabAt opens the
    // tab inside the click (a tab opened after the round-trip is blocked),
    // follows only https, and cuts the opener handle back into the app
    mutationFn: () => openTabAt(api.mxConnect, (r) => r.url, "MX"),
    onError: (e) => setErr(errText(e)),
  });
  const sync = useMutation({
    mutationFn: api.mxSyncNow,
    onSuccess: () => { afterFirstPull(qc); onDone(); },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <>
      <Steps items={[
        <>Create a developer account at{" "}
          <a href="https://dashboard.mx.com" target="_blank"
             rel="noreferrer">dashboard.mx.com</a> — the sandbox is free
          (test banks like "MX Bank"); production needs an MX agreement.</>,
        <>In the dashboard copy your <b>client_id</b> and <b>API key</b>.</>,
        <>Paste them here — validated live against MX before saving:</>,
      ]} />
      <div className="field-grid">
        <label>client_id{field(clientId, setClientId, "")}</label>
        <label>API key{field(apiKey, setApiKey, "", "password")}</label>
        <label>Environment
          <select value={env} onChange={(e) => setEnv(e.target.value)}>
            <option value="sandbox">sandbox (free, test banks)</option>
            <option value="production">production</option>
          </select>
        </label>
      </div>
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem",
                    flexWrap: "wrap" }}>
        <button className="pri" disabled={save.isPending || !clientId || !apiKey}
          onClick={() => save.mutate()}>
          {save.isPending ? "checking with MX…" : keysReady ? "Keys saved ✓" : "Validate & save keys"}
        </button>
        <button disabled={!keysReady || connect.isPending || !!held}
          onClick={() => connect.mutate()}>
          {connect.isPending ? "…" : "Open MX Connect (link a bank)"}
        </button>
        <button disabled={!keysReady || sync.isPending || saving}
          title={saving ? HISTORY_SAVING : undefined}
          onClick={() => sync.mutate()}>
          {sync.isPending ? "syncing…" : "Sync now"}
        </button>
      </div>
      {held && <p style={{ color: "var(--amber)" }}>{held}</p>}
      <p className="mut" style={{ marginTop: ".6rem" }}>Link banks in the MX
        window, then hit <b>Sync now</b> — accounts and transactions land on
        the Accounts page; the background sync keeps them fresh after that.</p>
    </>
  );
}

export function SimplefinFlow({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  // the claim runs the first pull at once, bounded by the stored window
  const saving = useHistorySaving();
  const connect = useMutation({
    mutationFn: () => api.simplefinConnect(token.trim()),
    onSuccess: () => { afterFirstPull(qc); onDone(); },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <>
      <Steps items={[
        <>Sign up at <a href="https://bridge.simplefin.org" target="_blank"
          rel="noreferrer">bridge.simplefin.org</a> ($1.50/mo, paid to them
          directly) and connect your banks there.</>,
        <>Create a <b>setup token</b> (New App → copy the long code).</>,
        <>Paste it here — it's claimed and your first sync runs
          immediately:</>,
      ]} />
      <textarea rows={3} value={token} onChange={(e) => setToken(e.target.value)}
        placeholder="paste the SimpleFIN setup token"
        style={{ width: "100%", padding: ".5rem", background: "var(--hover)",
                 border: "1px solid var(--line)", borderRadius: "var(--rs)",
                 color: "var(--ink)" }} />
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      <button className="pri" style={{ marginTop: ".5rem" }}
        disabled={connect.isPending || !token.trim() || saving}
        title={saving ? HISTORY_SAVING : undefined}
        onClick={() => connect.mutate()}>
        {connect.isPending ? "claiming + first sync…" : "Connect SimpleFIN"}
      </button>
    </>
  );
}

export default function ConnectHub({ onDone }: { onDone?: () => void }) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // the grid shows LIVE connected state per provider, so a linked
  // SimpleFIN does not look like an untouched one
  // poll while the grid is on screen: Plaid Link finishes in ANOTHER tab,
  // and the new bank should appear here without a manual refresh
  const conns = useQuery({ queryKey: ["connections"],
                           queryFn: api.connections,
                           refetchInterval: 5000 });
  // a first sync landing refreshes the accounts and the wizard it fills
  useFirstSyncLanding(conns.data?.connections);
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [open, setOpen] = useState<ProviderKey | null>(null);
  const hosted = !!me.data?.hosted;
  const done = () => { setOpen(null); onDone?.(); };
  const qc2 = useQueryClient();
  // the household's window, to tell which linked bank was linked narrower
  const hubSettings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const disc = useMutation({
    mutationFn: (id: string) => api.connectionDisconnect(id),
    onSuccess: (r, id) => {
      // the ✓ rows read the cached connection list, which no longer
      // serves a disconnected bank — drop it now; its accounts are archived
      // too, and the wizard's progress reads both
      removeFromList<{ connections: Connection[] }, Connection>(
        qc2, ["connections"], "connections", (c) => c.id === id);
      qc2.invalidateQueries({ queryKey: ["connections"] });
      // the institutions-used figure rides /api/me
      qc2.invalidateQueries({ queryKey: ["me"] });
      qc2.invalidateQueries({ queryKey: ["accounts"] });
      qc2.invalidateQueries({ queryKey: ["onboarding"] });
      if (r.note) window.alert(r.note);
    },
    onError: (e) => window.alert(errText(e)),
  });
  const connsFor = (key: ProviderKey): Connection[] => {
    const all = conns.data?.connections ?? [];
    if (key === "plaid") return all.filter((c) => c.aggregator === "plaid");
    if (key === "mx") return all.filter((c) => c.aggregator === "mx");
    if (key === "simplefin") {
      // per-bank child items carry the institutions; the bridge (token
      // holder) only stands in until the first sync creates children
      const kids = all.filter((c) => c.aggregator === "simplefin-org");
      return kids.length ? kids
        : all.filter((c) => c.aggregator === "simplefin");
    }
    return [];
  };
  // demo instances never connect external data — the whole hub
  // shows disabled wherever it renders (Accounts, wizard); server 403s back
  // this up
  if (me.data?.demo)
    return (
      <div className="card" style={{ marginBottom: "1rem" }}>
        <h2>Where should your data come from?</h2>
        <p className="sub" style={{ color: "var(--amber)" }}>
          External data connections are disabled on the demo instance —
          everything here is synthetic. Install your own instance to
          connect real accounts.</p>
      </div>
    );
  // the shared demonstration login is refused every door in here (each
  // one plants a credential that outlives the login), so the hub says so
  // once instead of offering buttons that can only answer 403
  const credentialLock = sharedLock(me.data, "credential");
  if (credentialLock)
    return (
      <div className="card" style={{ marginBottom: "1rem" }}>
        <h2>Where should your data come from?</h2>
        <p className="sub" style={{ color: "var(--amber)" }}>{credentialLock}</p>
      </div>
    );
  // hosted never shows the raw provider grid — the platform runs
  // the aggregators under its own credentials (best live source serves,
  // rest shadow — so which one answered is not the user's
  // problem). One "Connect your accounts" door, plus the bring-your-own
  // lane (scripts + file imports). Self-host keeps the grid: BYO keys
  // legitimately need the comparison.
  if (hosted) {
    const all = conns.data?.connections ?? [];
    const linked = all.filter((c) => c.aggregator !== "simplefin");
    // What the cap allows: the add door refuses past it, so the screen
    // shows the same limit. Both figures come from the server: the cap has
    // a per-tenant override an operator can raise, so a number written
    // into this copy would be wrong for exactly the accounts that were
    // given more. Wording and disabled anatomy are the Accounts page's
    // ConnectCard.
    const cap = me.data?.features?.institution_cap;
    const used = me.data?.features?.institutions_used;
    const capKnown = typeof cap === "number" && typeof used === "number";
    const full = capKnown && used >= cap;
    // a free trial whose owner has not confirmed has an allowance of
    // zero: the door is closed here, with the reason, before the tap the
    // server refuses
    const confirmFirst = bankAwaitsConfirm(me.data) ? CONFIRM_TO_CONNECT
                                                    : null;
    return (
      <div className="card" style={{ marginBottom: "1rem" }}>
        <h2>Connect your accounts</h2>
        {me.data?.bank_link ? (
          <>
            <p className="mut">Pick your bank and sign in — that's it.
              Transactions, balances and cards flow in automatically.</p>
            {confirmFirst && (
              <VerifyEmailCard me={me.data} inline>
                {confirmFirst}
              </VerifyEmailCard>)}
            {/* "account", not "bank" — a card, a brokerage and a bank are
                all accounts; and once one is in, the button invites the
                next */}
            <PlaidConnectButton full={full} closed={confirmFirst} label={linked.length
                ? "+ Connect another account ↗" : "+ Connect an account ↗"}
              hint="Opens your bank's sign-in (Plaid) in a new tab. This page
                stays put — when you finish, the connection appears below so
                you can link another or continue the wizard." />
            {/* "0 of 0" while the address waits says nothing true */}
            {capKnown && !confirmFirst && (
              <p className="sub" style={{ marginTop: ".2rem", marginBottom: 0 }}>
                {ext.allowanceNote(used, cap, full)}
              </p>)}
          </>
        ) : (
          <p className="sub" style={{ color: "var(--amber)" }}>
            Automatic bank connections aren't enabled on this instance yet —
            they'll appear right here when they are. Meanwhile your data can
            come in through file imports or an API script below.</p>
        )}
        {linked.length > 0 && (
          <div className="linked">
            <div className="linked-hdr">
              <b>Connected</b>
              <span className="pill g">
                {linked.length} {linked.length === 1 ? "bank" : "banks"}
                {linked.some((c) => typeof c.accounts === "number") && (
                  <> · {linked.reduce((t, c) => t + (c.accounts ?? 0), 0)} accounts</>)}
              </span>
            </div>
            {/* one row per bank with its status dot, account count and last
                pull — a flat "✓ Name ×" run stops reading as a list once a
                household has five or six banks */}
            <div className="linked-rows">
              {linked.map((c) => {
                const bad = (c.status || "").startsWith("error")
                  || c.status === "reaped";
                return (
                  <div key={c.id} className="linked-row">
                    <span className={"linked-dot" + (bad ? " bad" : "")} />
                    <span className="linked-name">
                      {c.institution_name || c.id}
                      <small>
                        {typeof c.accounts === "number" && (
                          <>{c.accounts} {c.accounts === 1 ? "account" : "accounts"}</>)}
                        {bad
                          ? <> · <span style={{ color: "var(--red)" }}>needs attention</span></>
                          : c.last_ok
                            ? <> · synced {ago(c.last_ok)}</>
                            : <> · {FIRST_SYNC_RUNNING}</>}
                        {/* the bank's own clock. Sync reads Plaid's copy;
                            the bank refreshes that copy on its own
                            schedule, so this is the number that explains a
                            sync which found nothing. */}
                        {!bad && c.bank_updated_at && (
                          <> · bank sent data {ago(c.bank_updated_at)}</>)}
                        {/* the window this bank was linked with, when it
                            is less than everything: a choice widened
                            later does not reach it by itself */}
                        {c.history_months != null && (
                          <> · {historyRowNote(c.history_months)}</>)}
                      </small>
                    </span>
                    {bad && <HubFixButton c={c} />}
                    {!bad && windowNarrower(c.history_months,
                                            hubSettings.data?.history_months) && (
                      <HubMoreHistory c={c} />)}
                    <button className="linked-x"
                      title="disconnect — stops syncing, keeps history"
                      disabled={disc.isPending}
                      onClick={() => window.confirm(
                        `Disconnect ${c.institution_name || c.id}? It stops `
                        + "syncing (history is kept).") && disc.mutate(c.id)}>
                      Disconnect</button>
                  </div>
                );
              })}
            </div>
          </div>)}
        <h2 style={{ marginTop: "1.2rem" }}>Bring your own data</h2>
        <p className="mut">For history no aggregator carries, exports from
          another tool, or accounts you'd rather feed yourself.</p>
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
          <button className={open === "files" ? "" : "pri"}
            onClick={() => setOpen(open === "files" ? null : "files")}>
            {open === "files" ? "close file imports" : "Import files"}
          </button>
          <a className="btn" href="https://github.com/oikonome/oikonome"
             target="_blank" rel="noreferrer">API scripts docs ↗</a>
        </div>
        {open === "files" && <div style={{ marginTop: "1rem" }}><FileImport /></div>}
      </div>
    );
  }
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <h2>Where should your data come from?</h2>
      <p className="mut">Pick one to start — add more later. Two sources for
        one account link automatically (best serves, rest back up).{" "}
        <b>Plaid is the recommended path</b>: it returns the most data
        (precise categories, card due dates, statement balances, investment
        holdings), which means the best out-of-box experience — smart
        categorization only has to fill in what your source doesn't
        provide.</p>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontSize: 14 }}>
          <thead><tr><th></th><th>Cost</th><th>Setup</th>
            <th>Data detail</th><th></th></tr></thead>
          <tbody>
            {ROWS.map((r) => {
              const linked = connsFor(r.key);
              const broken = linked.filter(
                (c) => (c.status || "").startsWith("error")).length;
              return (
              <tr key={r.key}
                  style={open === r.key ? { background: "var(--hover)" } : undefined}>
                <td><b>{r.name}</b>
                  {r.key === "plaid" &&
                    <>{" "}<span className="pill g">recommended</span></>}
                  {r.key === "plaid" && linked.length === 0
                    && !!st.data?.plaid_secret_set && (
                    <div style={{ fontSize: 12,
                                  color: "var(--amber, #e6b159)" }}>
                      keys saved — no bank linked yet (open set up)</div>)}
                  {linked.length > 0 && (
                    <div style={{ fontSize: 12, color: "var(--green, #4a4)" }}>
                      {linked.map((c) => (
                        <span key={c.id} style={{ marginRight: ".6rem",
                                                  whiteSpace: "nowrap" }}>
                          ✓ {c.institution_name || c.id}
                          <button title={"disconnect — stops syncing, keeps "
                                         + "history"
                                         + (r.key === "plaid"
                                            ? "; frees the Plaid slot" : "")}
                            style={{ background: "none", border: "none",
                                     cursor: "pointer", padding: "0 .15rem",
                                     color: "var(--mut, #888)", fontSize: 12 }}
                            disabled={disc.isPending}
                            onClick={() => window.confirm(
                              `Disconnect ${c.institution_name || c.id}? `
                              + "It stops syncing (history is kept)"
                              // a released Item keeps its place under the
                              // allowance for 30 days, so only say so where
                              // there is an allowance to hold it
                              + (r.key === "plaid"
                                   && typeof me.data?.features?.institution_cap === "number"
                                 ? ". It keeps its slot for 30 days." : "."))
                              && disc.mutate(c.id)}>✕</button>
                        </span>
                      ))}
                      {broken > 0 && (
                        <span style={{ color: "var(--red)" }}>
                          {" "}({broken} needs attention)</span>)}
                    </div>)}
                </td>
                <td>{hosted && r.hostedCost ? r.hostedCost : r.cost}</td>
                <td>{hosted && r.hostedEffort ? r.hostedEffort : r.effort}</td>
                <td className="mut">{r.richness}</td>
                <td style={{ textAlign: "right" }}>
                  {r.key === "files"
                    // inline, not a nav link — leaving the page mid-wizard
                    // dumped the user out of the flow
                    ? <button className={open === "files" ? "" : "pri"}
                        onClick={() =>
                          setOpen(open === "files" ? null : "files")}>
                        {open === "files" ? "close" : "use imports"}
                      </button>
                    : r.key === "scripts"
                      ? <a className="btn"
                           href="https://github.com/oikonome/oikonome"
                           target="_blank" rel="noreferrer">docs</a>
                      : hosted && (r.key === "plaid" || r.key === "mx")
                        ? <span className="pill g">included</span>
                        : <button className={open === r.key || linked.length
                                             ? "" : "pri"}
                            onClick={() =>
                              setOpen(open === r.key ? null : r.key)}>
                            {open === r.key ? "close"
                              : linked.length ? "manage"
                                : r.key === "plaid"
                                  && st.data?.plaid_secret_set
                                  ? "finish setup" : "set up"}
                          </button>}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {open === "plaid" && <div style={{ marginTop: "1rem" }}><PlaidFlow onDone={done} /></div>}
      {open === "mx" && <div style={{ marginTop: "1rem" }}><MxFlow onDone={done} /></div>}
      {open === "simplefin" && <div style={{ marginTop: "1rem" }}><SimplefinFlow onDone={done} /></div>}
      {open === "files" && <div style={{ marginTop: "1rem" }}><FileImport /></div>}
    </div>
  );
}
