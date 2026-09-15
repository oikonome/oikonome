import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useRef, useState } from "react";
import { ago, api, errText, type Connection, type Settings } from "../api/client";
import { ext } from "../ext";
import { patchQuery, removeFromList } from "../api/cache";
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

/** Open Plaid Hosted Link only (no oikonome waiting tab). Caller stays put
 *  and this promise resolves when the bank tab finishes (or fails). */
export async function openPlaidHostedLink(itemId = "", opts?: {
  /** polled between status checks — return true to stop waiting (the user
   *  told US they closed the bank tab; the server can't always know) */
  cancelled?: () => boolean;
  /** update mode only: open Link's shared-accounts checklist so an
   *  account can be deselected — the only way to stop Plaid billing it */
  manageAccounts?: boolean;
}): Promise<{
  kind?: string; institution?: string; error?: string;
}> {
  const started = await api.plaidLinkStart(itemId, !!opts?.manageAccounts);
  if (!/^https:/i.test(started.hosted_link_url))
    throw new Error("Plaid returned a non-https link URL");
  window.open(started.hosted_link_url, "_blank", "noopener,noreferrer");
  // Poll until the session is finished server-side (exchange + first sync).
  // Cap ~15 min — Hosted Link URL lifetime is 30 min; stop sooner for UX.
  // The server resolves kind:"exited" when Plaid stamps the session
  // finished with nothing added (the in-Link exit button); the cancelled
  // callback covers the case Plaid never hears about — a closed tab.
  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2500));
    if (opts?.cancelled?.()) return { kind: "cancelled" };
    const s = await api.plaidLinkStatus(started.link_token);
    if (s.done) return s;
  }
  throw new Error("Timed out waiting for the bank connection");
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
function PlaidConnectButton({ label, hint, itemId = "", full = false }: {
  label: string; hint: string; itemId?: string; full?: boolean;
}) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // backing out of the bank tab is normal, not an error — it gets its own
  // muted line so it never renders red
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const cancelRef = useRef(false);
  const run = async () => {
    cancelRef.current = false;
    setBusy(true); setErr(null); setNote(null);
    setMsg("Finish signing in at your bank…");
    try {
      const s = await openPlaidHostedLink(itemId,
        { cancelled: () => cancelRef.current });
      if (s.kind === "cancelled") {
        setMsg(null);
      } else if (s.kind === "exited") {
        setMsg(null);
        setNote("No bank was linked — the sign-in window was closed. "
                + "Connect again whenever you're ready.");
      } else if (s.kind === "add_failed" || s.kind === "update_failed") {
        setErr(s.error || "Link failed");
        setMsg(null);
      } else {
        // the confirmation is the bank appearing in the linked list
        // below; the message only covers the moment before that list
        // has refetched, then goes — two green lines naming the same
        // bank read as two connections
        setMsg(`Linked ${s.institution || "bank"} ✓`);
        await Promise.all([
          qc.invalidateQueries({ queryKey: ["connections"] }),
          // the institutions-used figure rides /api/me
          qc.invalidateQueries({ queryKey: ["me"] }),
          qc.invalidateQueries({ queryKey: ["accounts"] }),
        ]);
        setMsg(null);
      }
    } catch (e) {
      setErr(errText(e)); setMsg(null);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <button className="pri" style={{ fontSize: 15 }} disabled={busy || full}
        title={full ? "you are at this account's institution limit" : undefined}
        onClick={() => void run()}>
        {busy ? "Connecting…" : label}
      </button>
      {/* the escape hatch: the server only learns about an exit Plaid
          reports, and a closed TAB reports nothing — without this the
          button sat on "Connecting…" for 15 minutes after a back-out */}
      {busy && (
        <button type="button" className="linklike mut"
          style={{ marginLeft: ".7rem", fontSize: 13 }}
          onClick={() => { cancelRef.current = true; }}>
          cancel — I closed the bank window
        </button>
      )}
      <p className="mut" style={{ marginTop: ".5rem", fontSize: 13 }}>{hint}</p>
      {msg && <p style={{ fontSize: 13, color: "var(--green, #4a4)" }}>{msg}</p>}
      {note && <p className="mut" style={{ fontSize: 13 }}>{note}</p>}
      {err && <p style={{ fontSize: 13, color: "var(--red)" }}>{err}</p>}
    </div>
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
  const connect = useMutation({
    mutationFn: api.mxConnect,
    // the widget URL comes from the server response — open only https,
    // and without an opener handle back into the app
    onSuccess: (r) => { if (/^https:/i.test(r.url))
      window.open(r.url, "_blank", "noopener,noreferrer"); },
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
        <button disabled={!keysReady || connect.isPending}
          onClick={() => connect.mutate()}>
          {connect.isPending ? "…" : "Open MX Connect (link a bank)"}
        </button>
        <button disabled={!keysReady || sync.isPending}
          onClick={() => sync.mutate()}>
          {sync.isPending ? "syncing…" : "Sync now"}
        </button>
      </div>
      <p className="mut" style={{ marginTop: ".6rem" }}>Link banks in the MX
        window, then hit <b>Sync now</b> — accounts and transactions land on
        the Accounts page; the hourly worker keeps them fresh after that.</p>
    </>
  );
}

export function SimplefinFlow({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
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
        disabled={connect.isPending || !token.trim()}
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
  const st = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [open, setOpen] = useState<ProviderKey | null>(null);
  const hosted = !!me.data?.hosted;
  const done = () => { setOpen(null); onDone?.(); };
  const qc2 = useQueryClient();
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
    return (
      <div className="card" style={{ marginBottom: "1rem" }}>
        <h2>Connect your accounts</h2>
        {me.data?.bank_link ? (
          <>
            <p className="mut">Pick your bank and sign in — that's it.
              Transactions, balances and cards flow in automatically.</p>
            {/* "account", not "bank" — a card, a brokerage and a bank are
                all accounts; and once one is in, the button invites the
                next */}
            <PlaidConnectButton full={full} label={linked.length
                ? "+ Connect another account ↗" : "+ Connect an account ↗"}
              hint="Opens your bank's sign-in (Plaid) in a new tab. This page
                stays put — when you finish, the connection appears below so
                you can link another or continue the wizard." />
            {capKnown && (
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
                            : <> · first sync pending</>}
                        {/* the bank's own clock. Sync reads Plaid's copy;
                            the bank refreshes that copy on its own
                            schedule, so this is the number that explains a
                            sync which found nothing. */}
                        {!bad && c.bank_updated_at && (
                          <> · bank sent data {ago(c.bank_updated_at)}</>)}
                      </small>
                    </span>
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
                              + (r.key === "plaid"
                                 ? " and the Plaid slot is freed." : "."))
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
