// Accounts — legacy accounts.html parity: institution-grouped table with
// status dots, per-connection sync (↻) / fix (↗) affordances, connection-type
// badges, pencil rename + classify (product feature, tucked into the ✎ edit
// row), an Archived collapsible for closed/historical accounts, Top holdings.
// Product-only kept: SimpleFIN connect, Plaid hosted link, manual accounts.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { openPlaidHostedLink } from "../components/ConnectHub";
import ImportPage from "./Import";
import { Fragment, useEffect, useState } from "react";
import { ext } from "../ext";
import { Link, useSearchParams } from "react-router";
import { ago, api, errText, linkCardVisible, mmdd, money, type Account,
         type Connection, type Settings } from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { canEdit, isOwner, isViewer } from "../role";
import { hueOf, monogram, useLogosOn } from "../components/MerchantAvatar";
import { KINDS, RemoveAccountPanel, type DoneFn, type Moved }
  from "../components/RemoveAccountPanel";

type AccountsData = { accounts: Account[] };

interface Group {
  itemId: string;
  institution: string;
  conn: Connection | null;
  accounts: Account[];
  total: number;
}

// top-level account categories with per-group totals. Coinbase is
// investment/crypto so it sits in Investments.
type Category = "Bank" | "Credit Cards" | "Investments" | "Loans";
const CATEGORY_ORDER: Category[] = ["Bank", "Credit Cards", "Investments", "Loans"];

const badgeLabel = (c: Connection | null,
                    script?: Account["script"]) =>
  // a collector-script-fed item is not a one-time "Import" —
  // say what it is (community script, api or scrape collection half)
  script ? `Script${script.kind ? ` · ${script.kind}` : ""}`
  : c === null ? "Import" : c.aggregator === "plaid" ? "Plaid"
  : c.aggregator === "mx" ? "MX"
  // simplefin-org = a per-institution SimpleFIN child item
  : c.aggregator.startsWith("simplefin") ? "SimpleFIN" : c.aggregator;

// legacy status dot: the whole ok/error/stale signal, left of the name
function Dot({ conn }: { conn: Connection | null }) {
  if (!conn) return null;
  const style = { fontSize: 16, verticalAlign: "middle" } as const;
  // red is reserved for something the person has to DO. A bank that is
  // down is amber like any other "not right now" state — it resolves
  // itself, and red for it is what made red stop meaning anything.
  const kind = conn.status_kind
    ?? (conn.status && conn.status !== "ok" ? "attention" : "ok");
  if (kind === "ok") {
    // the item is fine, but the BANK's own login rail is DOWN fleet-wide —
    // amber with context, so a reconnect that dies on the bank's error
    // page doesn't look like our bug (nothing to fix here). Only DOWN:
    // Plaid's DEGRADED measures fleet-wide NEW-LOGIN success and major
    // banks sit there for weeks at a time, which painted whole pages of
    // healthy, syncing accounts amber at once and made amber mean
    // nothing. An item that is ok and syncing stays green through it.
    if (conn.institution_health === "DOWN")
      return <span title={`${conn.institution_name ?? "the bank"} is having `
                          + "trouble on the bank's side — syncing and "
                          + "reconnects may fail until it clears; nothing "
                          + "to fix here"}
                   style={{ ...style, color: "var(--amber)" }}>●</span>;
    return <span title="ok" style={{ ...style, color: "var(--green)" }}>●</span>;
  }
  if (kind === "retrying")
    return <span title={conn.status === "restored" || conn.status === "stale"
                        ? "not synced recently"
                        : "the bank is down or slow — retrying automatically"}
                 style={{ ...style, color: "var(--amber)" }}>●</span>;
  return <span title={conn.status ?? "error"}
               style={{ ...style, color: "var(--red)" }}>●</span>;
}

// script-fed items get the same live dot as aggregator connections — green = a successful push within 26h (server-computed,
// Doctor's freshness threshold), red = the collector has gone quiet.
// Heartbeats only stamp successes, so the tooltip carries the last good
// run; a silent collector's error lives in ITS logs, host-side.
function ScriptDot({ script }: { script: NonNullable<Account["script"]> }) {
  const style = { fontSize: 16, verticalAlign: "middle" } as const;
  const when = new Date(script.last_push).toLocaleString();
  if (script.ok)
    return <span title={`last run ${when}`}
                 style={{ ...style, color: "var(--green)" }}>●</span>;
  // The one silence the server can explain: its token was revoked after
  // the last good push (a password change or reset revokes every script
  // token), so the collector is probably running fine and being refused.
  const why = script.token_revoked_at
    ? `its script token was revoked ${new Date(script.token_revoked_at)
         .toLocaleString()} (a password change or reset revokes them all) — `
      + "mint a new one under Settings → Connections and put it in the "
      + "collector's config.json"
    : "check the collector on your machine";
  return <span title={`no successful run since ${when} — ${why}`}
               style={{ ...style, color: "var(--red)" }}>●</span>;
}

// the "run" affordance for a script-fed item: collectors are host-side
// systemd user timers — the product CANNOT execute them (that's the whole
// credentials-stay-on-your-machine design). So "run" opens a small guide
// with the exact host command and a copy button, honest about where it runs.
function ScriptRunHint({ source, script }:
                       { source: string; script?: Account["script"] }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const cmd = `systemctl --user start oikonome-${source}.service`;
  const revoked = script && !script.ok && script.token_revoked_at
    ? new Date(script.token_revoked_at).toLocaleString() : null;
  return (
    <>
      {" "}<button type="button" style={{ padding: ".05rem .45rem", fontSize: 12 }}
              title="how to run this collector now"
              onClick={() => { setOpen(!open); setCopied(false); }}>run</button>
      {open && (
        <span style={{ display: "block", margin: ".4rem 0 .2rem",
                       padding: ".5rem .7rem", fontSize: 12, fontWeight: 400,
                       border: "1px solid var(--line)",
                       borderRadius: "var(--rs)", maxWidth: "34rem" }}>
          {revoked && (
            <span style={{ display: "block", marginBottom: ".45rem",
                           color: "var(--red)" }}>
              Its script token was revoked {revoked} — a password change
              or reset revokes every script token, so the collector is
              likely still running and being refused. Mint a new token
              under <Link to="/settings/connections">Settings → Connections</Link>{" "}
              and put it in the collector&apos;s <code>config.json</code>,
              then run it once by hand:
            </span>
          )}
          This collector runs on <b>the machine where you installed it</b>{" "}
          (a systemd user timer), not in the app — it holds your source
          credentials there by design. To run it now, on that host:
          <span style={{ display: "flex", gap: ".5rem", alignItems: "center",
                         marginTop: ".35rem" }}>
            <code style={{ userSelect: "all", overflowX: "auto" }}>{cmd}</code>
            <button type="button" style={{ padding: ".05rem .45rem", fontSize: 12 }}
              onClick={() => navigator.clipboard.writeText(cmd)
                .then(() => setCopied(true)).catch(() => {})}>
              {copied ? "copied ✓" : "copy"}</button>
          </span>
          <span className="mut" style={{ display: "block", marginTop: ".35rem" }}>
            Or run the collector script directly — see its README under{" "}
            <code>~/.local/share/oikonome/scripts/{source}/</code>.
          </span>
        </span>
      )}
    </>
  );
}

export default function Accounts({ embedded }: { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  // Two different gates on this page. Editing an account — its name,
  // kind, whether it anchors the forecast, a manual balance — is ordinary
  // household work, so a member does it. The CONNECTION is the owner's:
  // linking a bank, repairing one, choosing which accounts it shares,
  // hiding or removing an account. Those spend credentials or money and
  // are felt by everyone in the house, and the server refuses them, so a
  // member must not be shown the button.
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const owner = isOwner(me);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const conns = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const holds = useQuery({ queryKey: ["holdings"], queryFn: api.holdings });
  const [notice, setNotice] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null>(null);

  // ?msg= landing banner (the setup wizard's Plaid nudge and the
  // transitional Jinja POST routes both redirect here with one)
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    const m = params.get("msg");
    // the query string is attacker-craftable (a mailed link lands
    // here) — React escapes it, but a free-form banner is still a content
    // spoof ("…sign in at evil.com"). Our own producers are short and
    // never contain a URL, so anything long or link-shaped is dropped.
    if (m) {
      if (m.length <= 160 && !/https?:|www\.|\/\//i.test(m)) setNotice(m);
      const next = new URLSearchParams(params);
      next.delete("msg");
      setParams(next, { replace: true });
    }
  }, [params, setParams]);

  // ["settings"] and ["connections"] are mounted app-wide, so refetching
  // them here is a real request every time, not a free stale mark — and a
  // rename or a hide moves neither. Callers say what they changed; the
  // editors patch the cached rows themselves before calling back, so the
  // refetch only confirms what the screen already shows.
  const refresh = (moved?: Moved[]) => {
    // "me" is in the everything-list because the paths that omit `moved`
    // are the link and relink paths, and a new institution spends one of
    // the plan's — the allowance under the Connect button reads /api/me.
    const keys: Moved[] = moved
      ?? ["accounts", "settings", "connections", "today", "calendar", "me"];
    for (const k of keys) qc.invalidateQueries({ queryKey: [k] });
  };
  const done: DoneFn = (m, moved) => {
    setNotice(m); setEditId(null); refresh(moved);
  };

  const sync = useMutation({
    mutationFn: (itemId: string) => api.accountSync(itemId),
    // a pull changes balances, the connection's last-sync and the verdict;
    // the settings and the bill calendar it cannot touch
    onSuccess: (r) => done(r.message, ["accounts", "connections", "today"]),
    onError: (e) => setNotice(errText(e)),
  });

  if (accounts.isPending || settings.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (accounts.isError)
    return <div className="card">Couldn't load accounts: {String(accounts.error)}</div>;

  const primary = settings.data?.checking_account_id ?? null;
  const excluded = new Set(settings.data?.excluded_accounts ?? []);
  // A linked pair is ONE real account fed by two sources: the serving row
  // carries the balance and the backup shadows it. Every server-side money
  // aggregate excludes the shadow for that reason, so every total on this
  // page does too — the backup rows still render (with their pill saying
  // they don't count), they just never add twice.
  const counts = (a: Account) => !a.link || a.link.primary;
  const connById = new Map((conns.data?.connections ?? []).map((c) => [c.id, c]));

  // group by item (institution); balance-less OR user-removed accounts →
  // archived (closed lineages + soft-removed under a live connection)
  const byItem = new Map<string, Account[]>();
  for (const a of accounts.data.accounts) {
    if (!byItem.has(a.item_id)) byItem.set(a.item_id, []);
    byItem.get(a.item_id)!.push(a);
  }
  const groups: Group[] = [];
  const archGroups: Group[] = [];
  const hiddenGroups: Group[] = [];
  for (const [itemId, accs] of byItem) {
    const conn = connById.get(itemId) ?? null;
    const inst = accs[0].institution_name || itemId;
    const live = accs.filter((a) =>
      a.balance_current !== null && !a.user_removed_at);
    // HIDDEN is its own state, not a flavour of archived: the connection is
    // alive and the user chose this, so it gets a section that says so and
    // an obvious way back. Archived stays what it was — closed lineages.
    const hidden = accs.filter((a) => !!a.user_removed_at);
    const closed = accs.filter((a) =>
      a.balance_current === null && !a.user_removed_at);
    if (live.length)
      groups.push({ itemId, institution: inst, conn, accounts: live,
                    total: live.filter(counts)
                      .reduce((s, a) => s + (a.balance_current ?? 0), 0) });
    if (closed.length)
      archGroups.push({ itemId, institution: inst, conn, accounts: closed, total: 0 });
    if (hidden.length)
      hiddenGroups.push({ itemId, institution: inst, conn, accounts: hidden, total: 0 });
  }
  const byInst = (a: Group, b: Group) =>
    a.institution.toLowerCase().localeCompare(b.institution.toLowerCase());
  groups.sort(byInst);
  archGroups.sort(byInst);
  hiddenGroups.sort(byInst);
  const archCount = archGroups.reduce((s, g) => s + g.accounts.length, 0);
  const hiddenCount = hiddenGroups.reduce((s, g) => s + g.accounts.length, 0);

  // bucket the institution groups into account categories with a
  // per-category total. An item can in principle span
  // categories (a bank with a card + brokerage under one Plaid item), so
  // categorize per ACCOUNT and re-nest by institution inside each bucket.
  // Coinbase accounts are typed investment/crypto → they land in
  // Investments for free. Anything unrecognized falls to Bank.
  const catOf = (kind: string): Category => {
    const t = (kind || "").split("/")[0];
    return t === "credit" ? "Credit Cards"
      : t === "investment" ? "Investments"
      : t === "loan" ? "Loans" : "Bank";
  };
  // personal and business money are separate sections of the
  // product, so they get separate PANES here too — mixed in one list, an
  // assigned account looked like any other and its balance read as the
  // user's own. Built by the same bucketing, run over each side.
  const buildCategories = (keep: (a: Account) => boolean) => {
    const buckets = new Map<Category, Map<string, Account[]>>();
    for (const g of groups) {
      for (const a of g.accounts) {
        if (!keep(a)) continue;
        const cat = catOf(a.kind);
        if (!buckets.has(cat)) buckets.set(cat, new Map());
        const byInst = buckets.get(cat)!;
        if (!byInst.has(g.itemId)) byInst.set(g.itemId, []);
        byInst.get(g.itemId)!.push(a);
      }
    }
    return CATEGORY_ORDER
      .filter((cat) => buckets.has(cat))
      .map((cat) => {
        const byInst = buckets.get(cat)!;
        const catGroups: Group[] = [];
        for (const [itemId, accs] of byInst) {
          const src = groups.find((g) => g.itemId === itemId)!;
          catGroups.push({ itemId, institution: src.institution, conn: src.conn,
            accounts: accs,
            total: accs.filter(counts)
              .reduce((s, a) => s + (a.balance_current ?? 0), 0) });
        }
        catGroups.sort((a, b) => a.institution.toLowerCase()
          .localeCompare(b.institution.toLowerCase()));
        const total = catGroups.reduce((s, g) => s + g.total, 0);
        return { cat, total, groups: catGroups };
      });
  };
  const categories = buildCategories((a) => !a.entity_id);
  const bizCategories = buildCategories((a) => !!a.entity_id);

  const rowProps = { mayEdit, owner, primary, excluded, editId, setEditId,
                     onDone: done };
  // every hero figure derives from payloads this page already loads
  const liveAccts = groups.flatMap((g) => g.accounts)
    .filter((a) => !a.entity_id && counts(a));
  const liveTotal = liveAccts.reduce((t, a) => t + (a.balance_current ?? 0), 0);
  const liveCount = liveAccts.length;
  // same scope as the total beside it — a business-only institution is
  // not in the figure, so it is not in the count
  const instCount = new Set(liveAccts.map(
    (a) => a.institution_name || "Other")).size;
  // only connections a PERSON can act on — a bank outage retrying by
  // itself is not something to count in a "needs attention" badge
  const needsAttention = (conns.data?.connections ?? [])
    .filter((c) => (c.status_kind
                    ?? (c.status && c.status !== "ok" ? "attention" : "ok"))
                   !== "ok"
                   && c.status_kind !== "retrying").length;
  // the payload carries ONE last_sync for the tenant — not per connection.
  // It arrives already phrased ("synced 12m ago"), not as a timestamp —
  // parsing it as a date silently blanked this line for as long as it
  // existed.
  const lastSync = conns.data?.last_sync
    ? String(conns.data.last_sync) : null;

  return (
    <>
      {!embedded && <h1>Accounts</h1>}
      {/* HERO: the page answers "what do I have, and is it all
          syncing?". Add-a-connection has one home — the embedded import
          section at the foot — and the hero points at it rather than
          opening a second. */}
      {!embedded && (
        <div className="card" style={{ marginTop: 0 }}>
          <div style={{ display: "flex", gap: "1rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <span style={{ fontSize: "1.5rem", fontWeight: 700,
                           letterSpacing: "-.01em" }}>
              {money(liveTotal)}
              <span className="sub" style={{ fontWeight: 400 }}>
                {" "}across {liveCount} account{liveCount === 1 ? "" : "s"} ·{" "}
                {instCount} institution{instCount === 1 ? "" : "s"}</span>
            </span>
            {mayEdit && (
              <a className="btn" href="#connect" style={{ marginLeft: "auto",
                    textDecoration: "none" }}>＋ connect or import</a>
            )}
          </div>
          <div className="sub" style={{ marginTop: ".2rem", display: "flex",
                                        gap: ".9rem", flexWrap: "wrap" }}>
            {needsAttention > 0 && (
              <span><span className="pill a" style={{ marginRight: ".3rem" }}>
                {needsAttention}</span>
                connection{needsAttention === 1 ? "" : "s"} need attention</span>
            )}
            {lastSync && <span>{lastSync}</span>}
          </div>
        </div>
      )}
      {/* linking two sources into one account is bookkeeping, not a
          connection change — a member decides it; the serving/backup
          pills in the table below carry the read view for viewers */}
      {mayEdit && <LinkSuggestions />}
      {notice && (
        <div className="card" style={{ borderColor: "var(--green)", cursor: "pointer" }}
             onClick={() => setNotice(null)}>
          <span className="pill g">action</span> {notice}
        </div>
      )}

      {/* Account management lives below the Accounts pane, in the
          connect-and-import section */}
      <div className="card">
        <h2>Accounts</h2>
        {/*.accts-table + c-* cell classes drive the ≤620px
            row-restack in index.css; desktop rendering is untouched */}
        <table className="accts-table">
          <thead>
            <tr><th>Account</th><th className="hide-m">Kind</th>
                <th className="num">Balance</th><th className="num hide-m">Txns</th></tr>
          </thead>
          <tbody>
            {categories.map(({ cat, total, groups: catGroups }) => (
              <Fragment key={cat}>
                <tr className="cat-head">
                  <td colSpan={2} className="c-acct" style={{ paddingTop: "1.1rem",
                        fontSize: 12, textTransform: "uppercase",
                        letterSpacing: ".05em", color: "var(--mut)" }}>
                    {cat}</td>
                  <td className="num c-bal" style={{ paddingTop: "1.1rem",
                        fontWeight: 700 }}>{money(total)}</td>
                  <td className="c-pad" style={{ paddingTop: "1.1rem" }}></td>
                </tr>
                {catGroups.map((g) => (
                  <GroupRows key={g.itemId} g={g} {...rowProps}
                             onSync={(id) => sync.mutate(id)}
                             syncing={sync.isPending
                                      && sync.variables === g.itemId} />
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
        {accounts.data.accounts.length === 0 && (
          <p className="sub">No accounts yet — connect your bank or add a
            manual account above, then import files into it.</p>
        )}

        {/* Business money is its own section of the product. Shown here only
            so the accounts ARE visible and manageable; their balances and
            transactions stay out of the personal totals everywhere else. */}
        {bizCategories.length > 0 && (
          <>
            <h2 style={{ marginTop: "1.6rem" }}>Business accounts</h2>
            <p className="sub" style={{ marginTop: 0 }}>
              Excluded from personal totals; managed on{" "}
              <Link to="/business">Business</Link>.
            </p>
            <table className="txn-table">
              <thead>
                <tr><th>Account</th><th className="hide-m">Kind</th>
                    <th className="num">Balance</th>
                    <th className="num hide-m">Txns</th></tr>
              </thead>
              <tbody>
                {bizCategories.map(({ cat, total, groups: catGroups }) => (
                  <Fragment key={`biz-${cat}`}>
                    <tr className="cat-head">
                      <td colSpan={2} className="c-acct"
                          style={{ paddingTop: "1.1rem", fontSize: 12,
                                   textTransform: "uppercase",
                                   letterSpacing: ".05em",
                                   color: "var(--mut)" }}>{cat}</td>
                      <td className="num c-amt" style={{ paddingTop: "1.1rem",
                            fontWeight: 700 }}>{money(total)}</td>
                      <td className="c-pad" style={{ paddingTop: "1.1rem" }}></td>
                    </tr>
                    {catGroups.map((g) => (
                      <GroupRows key={g.itemId} g={g} {...rowProps}
                                 onSync={(id) => sync.mutate(id)}
                                 syncing={sync.isPending
                                          && sync.variables === g.itemId} />
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </>
        )}

        {hiddenGroups.length > 0 && (
          <details style={{ marginTop: "1rem" }}>
            <summary style={{ cursor: "pointer", color: "var(--mut)" }}>
              Hidden ({hiddenCount}) — excluded from every total; the
              connection is still live and the history is kept
            </summary>
            <table className="accts-table" style={{ marginTop: ".6rem" }}>
              <tbody>
                {hiddenGroups.map((g) => (
                  <GroupRows key={g.itemId} g={g} hidden {...rowProps} />
                ))}
              </tbody>
            </table>
          </details>
        )}

        {archGroups.length > 0 && (
          <details style={{ marginTop: "1rem" }}>
            <summary style={{ cursor: "pointer", color: "var(--mut)" }}>
              Archived ({archCount}) — closed / historical, not syncing
            </summary>
            <table className="accts-table" style={{ marginTop: ".6rem" }}>
              <tbody>
                {archGroups.map((g) => (
                  <GroupRows key={g.itemId} g={g} archived {...rowProps} />
                ))}
              </tbody>
            </table>
          </details>
        )}
      </div>

      {(holds.data?.holdings.length ?? 0) > 0 && (
        <div className="card">
          <h2>Top holdings</h2>
          <table className="holds-table">
            <thead><tr><th>Account</th><th>Symbol</th><th className="hide-m">Fund</th>
              <th className="num hide-m">Qty</th><th className="num hide-m">Price</th>
              <th className="num">Value</th></tr></thead>
            <tbody>
              {holds.data!.holdings.map((h, i) => (
                <tr key={i}>
                  <td className="c-hacct">{h.acct}</td>
                  <td className="c-hsym">{h.symbol}</td>
                  <td className="mut hide-m">{(h.fund || "").slice(0, 34)}</td>
                  <td className="num hide-m">{(h.qty ?? 0).toFixed(3)}</td>
                  <td className="num hide-m">{money(h.price)}</td>
                  <td className="num c-hval">{money(h.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!embedded && <OwnerImport onDone={done} />}
    </>
  );
}

// the Import page lives INSIDE Accounts — the /import route stays for
// deep links, but there is no nav item. The section is "Connect accounts
// and import transactions", with the Account-management card on top.
function OwnerImport({ onDone }: { onDone: DoneFn }) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  if (isViewer(me)) return null;
  return (
    <div id="connect" style={{ marginTop: "1.4rem" }}>
      {/* Two peer actions, two equal cards. One heading over one card
          would carry three unrelated things — the bank door, a SimpleFIN
          token box and a manual-account form — with the file importer
          trailing underneath, so the page would read as one long funnel
          rather than a choice between getting a live feed and uploading
          history. */}
      {/* the bank door is the owner's — a member imports files and adds
          manual accounts here, and the card that spends a connection
          slot simply is not part of their page */}
      <ImportPage heading={isOwner(me)
                    ? "Connect accounts and import transactions"
                    : "Import transactions"}
                  connect={isOwner(me)
                    ? <ConnectCard onDone={onDone}
                        hosted={!!me.data?.hosted}
                        used={me.data?.features?.institutions_used}
                        cap={me.data?.features?.institution_cap} />
                    : null}
                  manual={<ManualAccountRow onDone={onDone} />} />
    </div>
  );
}

function GroupRows({ g, archived, hidden, mayEdit, owner, primary, excluded,
                     editId, setEditId,
                     onDone, onSync, syncing }: {
  g: Group; archived?: boolean; hidden?: boolean;
  mayEdit: boolean; owner: boolean; primary: string | null;
  excluded: Set<string>;
  editId: string | null; setEditId: (id: string | null) => void;
  onDone: DoneFn;
  onSync?: (itemId: string) => void; syncing?: boolean;
}) {
  // one Link session per click: the bank tab takes a while to come back,
  // and a second click in the meantime opened a second session
  const [linking, setLinking] = useState(false);
  const openLink = (itemId?: string) => {
    setLinking(true);
    void openPlaidHostedLink(itemId)
      .then(() => onDone("reconnected"))
      .catch((e) => onDone(errText(e)))
      .finally(() => setLinking(false));
  };
  // 'reaped' = the connection was auto-removed (30+ days dead, or
  // gone at Plaid's side) — its token no longer exists, so no sync and no
  // update-mode "fix"; the only way forward is a fresh link (reconnect).
  // "gone" is the same door as reaped: a restored or orphaned item with no
  // token can only be linked afresh (mobile already treats it so)
  const reaped = g.conn?.status === "reaped" || g.conn?.status_kind === "gone";
  // Hidden is NOT a flavour of archived. Both sections are shown
  // quietly — no dot, no ↻, no balance — but the reasons are opposite:
  // archived means the connection is gone, hidden means the owner switched
  // one account off while the connection stays live. Reusing `archived` for
  // both put a "closed" pill on a working connection and offered a "fix ↗"
  // for a break that isn't there.
  const quiet = archived || hidden;
  const canSync = !quiet && g.conn !== null && mayEdit && !reaped;
  // A failing connection is not one thing. The server classifies it
  // (status_kind): 'reauth' is the only state a person can act on, and
  // 'retrying' is a bank outage that the next poll clears — offering
  // update mode for THAT is a button that appears to work and teaches
  // people the warning means nothing. Fall back to the old test only for
  // a server too old to send the field.
  const kind = g.conn?.status_kind
    ?? (g.conn && g.conn.status !== "ok" ? "attention" : "ok");
  const needsFix = owner && !reaped && !hidden &&
    g.conn?.aggregator === "plaid" && (kind === "reauth"
                                       || kind === "attention");
  const retrying = !reaped && !hidden && kind === "retrying"
    && g.conn?.status !== "restored" && g.conn?.status !== "stale";
  // NEW_ACCOUNTS_AVAILABLE: the bank is telling us this household opened
  // an account it has not shared. It is a 'reauth' state, but the ask is
  // not "your sign-in broke" — saying so would be alarming and wrong.
  const newAccounts = g.conn?.status === "new_accounts";
  const script = g.accounts[0]?.script ?? null;
  // fixed-width dot+sync box so institution names line up (legacy 3.4em);
  // acct-left lets the phone layout relax the width for a touch-size ↻
  const left = !quiet && (
    <span className="acct-left"
          style={{ display: "inline-block", width: "3.4em", whiteSpace: "nowrap" }}>
      {script ? <ScriptDot script={script} /> : <Dot conn={g.conn} />}{" "}
      {canSync && onSync && (
        <button style={{ padding: ".05rem .45rem", fontSize: 12 }} disabled={syncing}
                title={"pull anything your bank has already sent to Plaid"
                       + (g.conn?.bank_updated_at
                          ? ` — it last sent data ${ago(g.conn.bank_updated_at)}`
                          : "")
                       + ". Banks refresh on their own schedule, so a sync "
                       + "that finds nothing usually means nothing new."}
                onClick={() => onSync(g.itemId)}>↻</button>
      )}
    </span>
  );
  const badge = (
    <>
      {/* the aggregator badge (plaid/simplefin) said nothing
          actionable on a healthy row — it is provenance, and provenance that
          MATTERS (a community script, a manual account) keeps its pill while
          the rest moves into the ✎ editor. */}
      {(script || !g.conn) && (
      <>{" "}<span className="pill m" title={script
        ? `community script (${script.kind ?? "?"} collection) — pushes via `
          + (script.via === "token" ? "script token over HTTP"
             : script.via === "exec" ? "container exec" : "the app")
        : "manual account — file imports"}>{badgeLabel(g.conn, script)}</span></>
      )}
      {script && !quiet && <ScriptRunHint source={script.source} script={script} />}
      {/* WHEN THE BANK LAST SENT DATA — a different clock from "synced
          Xm ago", which is only when we last read Plaid's copy. Without
          it, a sync that correctly finds nothing is indistinguishable
          from one that is broken. */}
      {!quiet && !needsFix && !reaped && g.conn?.bank_updated_at && (
        <>{" "}<span className="pill m"
          title="Plaid last received data from this bank then. Sync reads that copy; it does not make the bank send more.">
          bank sent data {ago(g.conn.bank_updated_at)}</span></>
      )}
      {hidden && <>{" "}<span className="pill m"
        title="excluded from every total — the connection is still live">
        hidden</span></>}
      {archived && <>{" "}<span className="pill m">{g.conn ? "closed" : "archived"}</span></>}
      {/* a transient bank outage: amber, no button, and it says who is
          at fault and that nothing is required — Plaid documents these
          as clearing on their own. */}
      {retrying && (
        <>{" "}<span className="pill a"
          title="Plaid reports this bank as down or slow. The next sync picks up where it left off — there is nothing to fix.">
          bank having trouble — retrying</span></>
      )}
      {needsFix && (
        <>
          {" "}<span className={newAccounts ? "pill a" : "pill r"}>
            {newAccounts ? "new accounts to share"
              : kind === "reauth" ? "sign-in expired"
              : "needs attention"}</span>
          {" "}<button className="pri" style={{ padding: ".05rem .45rem", fontSize: 12 }}
                disabled={linking}
                title={newAccounts
                  ? "choose which new accounts to share with Oikonome"
                  : "re-authenticate this connection"}
                onClick={() => openLink(g.itemId)}>
            {newAccounts ? "share ↗" : "fix ↗"}</button>
        </>
      )}
      {/* The BANK's own login rail is degraded fleet-wide (Plaid
          institution health). Web said this only in the status dot's
          tooltip, which a touchscreen never reveals and a mouse only
          finds by accident — mobile has always said it in words, and the
          words belong in the row here too. It matters MOST on the row
          whose "fix ↗" is about to open the bank's own error page, so it
          renders there as well, after the red pill and its button:
          context for a fix that may bounce, never a reason to skip it. A
          'retrying' row already carries the outage sentence, and a
          reaped one is past caring. DEGRADED only earns the pill on a
          needs-fix row — it is Plaid's chronic fleet-wide new-login
          metric (weeks at a time on major banks), so on a healthy,
          syncing row it was a standing false alarm; DOWN warrants it
          everywhere. */}
      {!quiet && !retrying && !reaped && g.conn?.institution_health
        && g.conn.institution_health !== "HEALTHY"
        && (g.conn.institution_health === "DOWN" || needsFix) && (
        <>{" "}<span className="pill m"
          title={`${g.conn.institution_name ?? "This bank"} is having `
                 + "trouble on the bank's side, fleet-wide. "
                 + (needsFix
                    ? "Reconnecting may fail on the bank's own error page "
                      + "until it clears — worth trying again a little "
                      + "later if it does."
                    : "Syncing and reconnects may fail until it clears; "
                      + "there is nothing to fix here.")}>
          {needsFix ? "bank-side trouble — the fix may not go through yet"
            : "bank-side trouble — may clear on its own"}</span></>
      )}
      {/* A billed product this connection cannot answer for — the reason
          a card here has no autopay line on the cash forecast. The sync
          knows two very different causes and the pill says which:
          'consent' is fixable (an update-mode re-link lets the bank
          grant the product), 'not_supported' is the bank's permanent
          answer, said once in a quiet pill instead of silence. */}
      {!quiet && !reaped && g.conn?.product_issues &&
        Object.entries(g.conn.product_issues).map(([prod, iss]) => {
          const what = prod === "liabilities"
            ? "card due dates" : prod === "investments"
            ? "investment holdings" : prod;
          return iss.cause === "consent" ? (
            <span key={prod}>
              {" "}<span className="pill a"
                title={`This connection was linked without ${what}. `
                       + "Reconnecting lets the bank share them"
                       + (prod === "liabilities"
                          ? " — that is what puts the real due-date and "
                            + "autopay lines on the cash forecast." : ".")}>
                {what} not shared</span>
              {owner && !needsFix && (
                <>{" "}<button className="pri"
                    style={{ padding: ".05rem .45rem", fontSize: 12 }}
                    disabled={linking}
                    title={`re-link this connection to grant ${what}`}
                    onClick={() => openLink(g.itemId)}>grant ↗</button></>
              )}
            </span>
          ) : null;
          // not_supported stays silent: "unavailable at this bank" is
          // permanent and unactionable, so a standing pill for it is
          // noise. The undated-card behavior on the cash forecast
          // already carries the consequence.
        })}
      {/* the shared-accounts door lives in the ✎ editor — an every-row
          inline link is chrome the row didn't earn, and the editor is
          where account-shape controls go. */}
      {reaped && (
        <>
          {" "}<span className="pill m" style={{ color: "var(--red)" }}
            title="the connection kept failing for 30+ days, so it was removed to stop it billing — history is kept">
            connection removed after 30 days of failure — reconnect</span>
          {owner && (
            /* the removed Item's token is gone — reconnect = a FRESH link
               (no item_id), not update mode */
            <>{" "}<button className="pri" style={{ padding: ".05rem .45rem", fontSize: 12 }}
                    disabled={linking}
                    title="link this bank again"
                    onClick={() => openLink()}>
              reconnect ↗</button>
            </>
          )}
        </>
      )}
    </>
  );

  if (g.accounts.length === 1) {
    const a = g.accounts[0];
    return (
      <AcctRow a={a} institution={g.institution} leftBox={left} badge={badge}
               mayEdit={mayEdit} owner={owner}
               primary={primary} excluded={excluded.has(a.id)}
               editId={editId} setEditId={setEditId} onDone={onDone} />
    );
  }
  return (
    <>
      <tr>
        {/* .acct-pills is an inert inline span on desktop; on phones it
            becomes the row's second line (pills under the name) */}
        <td colSpan={2} className="c-acct" style={{ paddingTop: ".85rem" }}>
          {left} <InstMark a={g.accounts[0]} /><strong>{g.institution}</strong>
          <span className="acct-pills">{badge}</span>
        </td>
        <td className="num c-bal" style={{ paddingTop: ".85rem" }}>
          {quiet ? "" : <strong>{money(g.total)}</strong>}</td>
        <td className="c-pad"></td>
      </tr>
      {g.accounts.map((a) => (
        <AcctRow key={a.id} a={a} indent mayEdit={mayEdit} owner={owner}
                 primary={primary}
                 excluded={excluded.has(a.id)}
                 editId={editId} setEditId={setEditId} onDone={onDone} />
      ))}
    </>
  );
}

/** the bank's mark beside its name: Plaid's institution logo when the
 *  item has one, else a monogram in the brand colour; hidden entirely when
 *  the household turned merchant logos off */
function InstMark({ a }: { a: Account }) {
  const on = useLogosOn();
  if (!on) return null;
  const name = a.institution_name || "?";
  if (a.inst_logo_url) {
    return <img src={a.inst_logo_url} alt="" width={18} height={18}
                style={{ width: 18, height: 18, borderRadius: 4, verticalAlign: -4,
                         marginRight: 6, background: "#fff", objectFit: "contain" }} />;
  }
  return <span aria-hidden="true" style={{ display: "inline-flex", alignItems: "center",
      justifyContent: "center", width: 18, height: 18, borderRadius: 4,
      marginRight: 6, verticalAlign: -4, color: "#fff", fontSize: 9, fontWeight: 700,
      background: a.inst_color || `hsl(${hueOf(name)} 40% 30%)` }}>{monogram(name)}</span>;
}

function AcctRow({ a, institution, indent, leftBox, badge, mayEdit, owner,
                   primary, excluded, editId, setEditId, onDone }: {
  a: Account; institution?: string; indent?: boolean;
  leftBox?: React.ReactNode; badge?: React.ReactNode;
  mayEdit: boolean; owner: boolean; primary: string | null; excluded: boolean;
  editId: string | null;
  setEditId: (id: string | null) => void; onDone: DoneFn;
}) {
  const editing = mayEdit && editId === a.id;
  return (
    <>
      <tr>
        {/* the indent moved from an inline style to .indent so the phone
            layout can shrink it (index.css keeps 4.4rem on desktop) */}
        <td className={indent ? "c-acct indent" : "c-acct"}>
          {leftBox}{leftBox ? " " : null}
          {institution && <InstMark a={a} />}
          <Link to={`/transactions?acct=${encodeURIComponent(a.id)}`}
                title="view all transactions for this account">
            {institution && <><strong>{institution}</strong>{" "}</>}
            {a.name}{a.mask ? ` ${a.mask}` : ""}
          </Link>{" "}
          {mayEdit && (
          <a className="mut acct-edit" style={{ textDecoration: "none", fontSize: 12, cursor: "pointer" }}
             title="rename / classify account"
             onClick={() => setEditId(editing ? null : a.id)}>✎</a>
          )}
          {/* inert inline span on desktop; the phone layout turns it into
              the row's second line so pills never wrap under the buttons */}
          <span className="acct-pills">
          {a.id === primary && <>{" "}<span className="pill b"
            title="cash forecast + solvency anchor">primary</span></>}
          {excluded && <>{" "}<span className="pill m"
            title="excluded from budget math — its spending never counts">excl</span></>}
          {a.user_removed_at && <>{" "}<span className="pill m"
            title="you removed this account from the active list — history is kept">removed</span></>}
          {a.link && (a.link.primary
            ? <>{" "}<span className="pill g" title="linked account — this source is serving the data">serving</span></>
            : <>{" "}<span className="pill m" title="linked account — hidden from totals, standing by as backup">backup</span></>)}
          {a.link && !a.link.healthy && <>{" "}<span className="pill r"
            title="this source is stale or erroring">source down</span></>}
          {badge}
          </span>
        </td>
        <td className="mut hide-m">{a.kind}</td>
        <td className="num c-bal">
          {a.balance_current !== null ? money(a.balance_current) : "—"}
          {/* a card's next due date, right where the card is — from the
              liabilities pull, so a bank that shares nothing shows
              nothing (the forecast handles the guess) */}
          {a.card_due && (a.balance_current ?? 0) > 0.5 && (
            <div className="sub" title={a.card_statement != null
              ? `statement balance ${money(a.card_statement)}, due ${mmdd(a.card_due)}`
              : undefined}>
              due {mmdd(a.card_due)}
              {a.card_statement != null && <> · {money(a.card_statement)}</>}
            </div>
          )}</td>
        <td className="num mut hide-m">{a.txns}</td>
      </tr>
      {editing && <EditRow a={a} owner={owner} primary={primary}
                           excluded={excluded}
                           onDone={onDone} onCancel={() => setEditId(null)} />}
    </>
  );
}


// multi-source accounts — auto-suggested "same account?" pairs (owner
// confirms; never silently merged) and the standing status of existing
// links. Reordering and unlinking live in each account's editor (✎), so
// this card carries nothing the owner loses by putting it away — and it
// comes back by itself when a pair needs deciding or a source goes down.
function LinkSuggestions() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["account-links"],
                       queryFn: api.accountLinks });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [linkErr, setLinkErr] = useState<string | null>(null);
  const refresh = () => {
    setLinkErr(null);
    qc.invalidateQueries({ queryKey: ["account-links"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  // overlapping suggestions (A-B, A-C, B-C) go stale after one is
  // linked — clicking a now-invalid pair 400'd SILENTLY. Surface the error
  // AND re-fetch so the stale suggestions drop out of the list.
  const onError = (e: unknown) => {
    setLinkErr(errText(e));
    qc.invalidateQueries({ queryKey: ["account-links"] });
  };
  // The decided row leaves the card as the response lands; the refetch
  // behind it only confirms. A suggestion names two accounts, so linking
  // either of them also retires every other pair that mentions them.
  type Links = NonNullable<typeof q.data>;
  type Sugg = Links["suggestions"][number];
  const link = useMutation({
    mutationFn: (ids: string[]) => api.accountLinkCreate(ids),
    onSuccess: (_r, ids) => {
      removeFromList<Links, Sugg>(qc, ["account-links"], "suggestions",
        (x) => ids.includes(x.a.id) || ids.includes(x.b.id));
      refresh();
    }, onError });
  const dismiss = useMutation({
    mutationFn: (key: string) => api.accountLinkDismiss(key),
    onSuccess: (_r, key) => {
      removeFromList<Links, Sugg>(qc, ["account-links"], "suggestions",
        (x) => x.key === key);
      refresh();
    }, onError });
  const putAway = useMutation({
    mutationFn: () => api.accountLinkCard(true),
    onSuccess: () => qc.setQueryData<Links>(["account-links"],
      (old) => old && { ...old, card_dismissed: true }),
    onError });
  if (q.isPending || q.isError) return null;
  const names = new Map(
    (accounts.data?.accounts ?? []).map((a) => [a.id, a.name]));
  const label = (s: { name: string; mask: string;
                      institution_name: string | null }) =>
    `${s.institution_name ? s.institution_name + " — " : ""}${s.name} (${s.mask})`;
  // pending per ROW, not per card — one pair deciding must not grey the rest
  const suggBusy = (x: Sugg) =>
    (link.isPending && (link.variables ?? []).includes(x.a.id))
    || (dismiss.isPending && dismiss.variables === x.key);
  if (!linkCardVisible(q.data)) return null;
  // put away, the card lists only the groups that need attention
  const shown = q.data.card_dismissed
    ? q.data.groups.filter((g) => g.members.some((m) => !m.healthy))
    : q.data.groups;
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <h2 style={{ display: "flex", alignItems: "baseline", gap: ".6rem" }}>
        Linked sources
        {!q.data.card_dismissed && q.data.groups.length > 0 && (
          <button type="button" className="btn" disabled={putAway.isPending}
                  style={{ padding: "0 .5rem", fontSize: 12, fontWeight: 400,
                           marginLeft: "auto" }}
                  title="hide this status until a source goes down or a new pair needs deciding — change or unlink a link from the account's ✎ editor"
                  onClick={() => putAway.mutate()}>dismiss</button>
        )}
      </h2>
      <p className="mut">Two sources feeding the same real-world account get
        linked: the best live source serves the data, the others stay in
        sync as backups — never double-counted, and they take over
        automatically (with an email) if the primary breaks. Reorder or
        unlink from the account's ✎ editor.</p>
      {linkErr && <p className="note bad" onClick={() => setLinkErr(null)}
        style={{ cursor: "pointer" }}>{linkErr}</p>}
      {q.data.suggestions.map((s) => (
        <div key={s.key} className="note" style={{ margin: ".4rem 0" }}>
          These look like the same account:{" "}
          <b>{label(s.a)}</b> and <b>{label(s.b)}</b>
          <span style={{ marginLeft: ".7rem", whiteSpace: "nowrap" }}>
            <button className="pri" disabled={suggBusy(s)}
              style={{ padding: ".1rem .6rem" }}
              onClick={() => link.mutate([s.a.id, s.b.id])}>Link them</button>
            {" "}
            <button disabled={suggBusy(s)} style={{ padding: ".1rem .6rem" }}
              onClick={() => dismiss.mutate(s.key)}>Not the same</button>
          </span>
        </div>
      ))}
      {shown.map((g) => (
        <p key={g.group_id} style={{ margin: ".4rem 0" }}>
          {g.members.map((m, i) => (
            <span key={m.account_id}>
              {i > 0 && <span className="mut"> → </span>}
              {names.get(m.account_id) ?? m.account_id}
              <span className="mut">
                {m.primary ? " (serving)" : " (backup)"}
                {!m.healthy && " ⚠"}</span>
            </span>
          ))}
        </p>
      ))}
    </div>
  );
}

// the account's place in a multi-source link, inside its editor: who it
// is linked with, whether it serves or backs up, and the two decisions
// — prefer this source, or unlink the group. Lives here rather than on
// the status card so the card can be put away without losing them.
function LinkControls({ a, onDone }: { a: Account; onDone: DoneFn }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["account-links"],
                       queryFn: api.accountLinks });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const group = q.data?.groups.find(
    (g) => g.members.some((m) => m.account_id === a.id));
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["account-links"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  const promote = useMutation({
    mutationFn: (gid: string) => api.accountLinkOrder(gid,
      [a.id, ...(group?.members ?? []).filter((m) => m.account_id !== a.id)
                .map((m) => m.account_id)]),
    onSuccess: () => { refresh(); onDone(`${a.name} now serves the linked account.`,
                                         ["accounts", "today"]); },
    onError: (e) => onDone(errText(e)),
  });
  const unlink = useMutation({
    mutationFn: (gid: string) => api.accountLinkDelete(gid),
    onSuccess: () => { refresh(); onDone(`${a.name} unlinked — each source counts on its own again.`,
                                         ["accounts", "today"]); },
    onError: (e) => onDone(errText(e)),
  });
  if (!group) return null;
  const me = group.members.find((m) => m.account_id === a.id)!;
  const names = new Map(
    (accounts.data?.accounts ?? []).map((x) => [x.id, x.name]));
  const others = group.members.filter((m) => m.account_id !== a.id)
    .map((m) => `${names.get(m.account_id) ?? m.account_id}`
                + ` (${m.primary ? "serving" : "backup"}${m.healthy ? "" : " ⚠"})`);
  const busy = promote.isPending || unlink.isPending;
  return (
    <div className="mut" style={{ display: "flex", gap: ".5rem", alignItems: "center",
                                  flexWrap: "wrap", fontSize: 12, padding: ".3rem 0 .1rem" }}>
      <span>
        {me.primary ? "Serving" : "Backup"}{!me.healthy && " ⚠ source down"}
        {" · linked with "}{others.join(", ")}
      </span>
      {!me.primary && (
        <button type="button" className="btn" disabled={busy || !me.healthy}
                style={{ padding: "0 .5rem", fontSize: 12 }}
                title={me.healthy ? "prefer this source — it serves the data while it is healthy"
                                  : "this source is down; it cannot serve"}
                onClick={() => promote.mutate(group.group_id)}>make primary</button>
      )}
      <button type="button" className="btn" disabled={busy}
              style={{ padding: "0 .5rem", fontSize: 12 }}
              title="separate the sources — each counts on its own again"
              onClick={() => {
                if (window.confirm(`Unlink ${a.name}? Each source will count `
                    + `separately, so a shared balance shows twice until you `
                    + `hide or remove one.`))
                  unlink.mutate(group.group_id);
              }}>unlink</button>
    </div>
  );
}

function EditRow({ a, owner, primary, excluded, onDone, onCancel }: {
  a: Account; owner: boolean; primary: string | null; excluded: boolean;
  onDone: DoneFn; onCancel: () => void;
}) {
  const qc = useQueryClient();
  // The fields below are the account as DATA — name, kind, forecast
  // anchor, whose money it is, a manual balance — and a member edits
  // them. The three controls at the end of the form act on the
  // CONNECTION (hide it from every total, change what the bank shares,
  // remove it), which is the owner's; the server refuses them for a
  // member, so they are not rendered.
  const [name, setName] = useState(a.name);
  const [kind, setKind] = useState(a.kind);
  const [isPrimary, setIsPrimary] = useState(a.id === primary);
  useEffect(() => setIsPrimary(a.id === primary), [a.id, primary]);
  const [isExcl, setIsExcl] = useState(excluded);
  useEffect(() => setIsExcl(excluded), [a.id, excluded]);
  const [bal, setBal] = useState(
    a.balance_current === null ? "" : String(a.balance_current));
  // the account's OWNER LABEL (whose money — yours / mine / ours), which
  // is data, not the role prop above
  const [ownerLbl, setOwnerLbl] = useState(a.owner ?? "");
  const [removing, setRemoving] = useState(false);
  const manual = a.institution_name === "Manual"
    || a.aggregator === "manual";
  // institution disconnect (incl. Plaid /item/remove) only for live pull
  // aggregators; manual/import/script use purge/hide instead
  const pullAgg = ["plaid", "mx", "simplefin", "simplefin-org"]
    .includes(a.aggregator || "");
  const liveConn = pullAgg && (a.status || "ok") !== "archived";

  // An empty name — or retyping the bank's own — CLEARS the override: the
  // server then serves the bank's name again and follows the bank's future
  // renames. The old `name.trim() &&` guard swallowed that intentional
  // clear, which made the server's revert path unreachable from here.
  const trimmed = name.trim();
  const wantsRevert = !trimmed || trimmed === a.bank_name;
  const nextName = wantsRevert ? (a.bank_name || a.name) : trimmed;

  const save = useMutation({
    mutationFn: async () => {
      if (nextName !== a.name)
        await api.accountRename(a.id, wantsRevert ? "" : trimmed);
      // the classify door always rewrites the kind and the anchor — only
      // knock when one of them actually changed
      if (kind !== a.kind || isPrimary !== (a.id === primary))
        await api.accountClassify(a.id, kind, isPrimary);
      if (isExcl !== excluded) await api.accountExclude(a.id, isExcl);
      if (ownerLbl.trim() !== (a.owner ?? ""))
        await api.setAccountOwner(a.id, ownerLbl.trim());
      if (manual && bal.trim() !== "" &&
          Number(bal.replace(/[$,]/g, "")) !== a.balance_current)
        await api.accountBalance(a.id, bal);
    },
    onSuccess: () => {
      // The row reads its name, kind and pills straight from the cache, so
      // write what was just saved into it — the editor closes on the new
      // values instead of the old ones. Each door is a plain field write
      // with no server-side derivation, so the prediction is the truth.
      const newBal = Number(bal.replace(/[$,]/g, ""));
      patchList<AccountsData, Account>(qc, ["accounts"], "accounts",
        (x) => x.id === a.id,
        { name: nextName, kind, owner: ownerLbl.trim() || null,
          ...(manual && bal.trim() !== "" && Number.isFinite(newBal)
              ? { balance_current: newBal } : {}) });
      const anchorMoved = isPrimary !== (a.id === primary);
      const exclMoved = isExcl !== excluded;
      if (anchorMoved || exclMoved)
        qc.setQueryData<Settings>(["settings"], (old) => old && {
          ...old,
          checking_account_id: isPrimary ? a.id
            : old.checking_account_id === a.id ? null
            : old.checking_account_id,
          excluded_accounts: isExcl
            ? [...new Set([...(old.excluded_accounts ?? []), a.id])]
            : (old.excluded_accounts ?? []).filter((id) => id !== a.id),
        });
      // the anchor, the exclusion and the kind move the verdict (budget
      // logic keys on account type); a rename does not
      const kindMoved = kind !== a.kind;
      onDone(`Saved ${nextName}.`,
             anchorMoved || exclMoved || kindMoved
               ? ["accounts", "today"] : ["accounts"]);
    },
    onError: (e) => onDone(errText(e)),
  });

  const setHidden = useMutation({
    mutationFn: (hidden: boolean) => api.accountSetHidden(a.id, hidden),
    onSuccess: (r, hidden) => {
      // the row changes section the moment the server agrees — hidden
      // rows group by user_removed_at, and a hidden account drops its
      // balance, which is what keeps it out of every total
      patchList<AccountsData, Account>(qc, ["accounts"], "accounts",
        (x) => x.id === a.id,
        hidden ? { user_removed_at: new Date().toISOString(),
                   balance_current: null }
               : { user_removed_at: null });
      onDone(r.note, ["accounts", "today"]);
    },
    onError: (e) => onDone(errText(e)),
  });
  const [linking, setLinking] = useState(false);

  return (
    <tr className="editrow">
      <td colSpan={4} style={{ background: "var(--hover)" }}>
        <form style={{ display: "flex", gap: ".5rem", alignItems: "center",
                       flexWrap: "wrap", padding: ".2rem 0" }}
              onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
          <input type="text" value={name} style={{ width: "14rem" }}
                 title="display name — sync never overwrites it; empty
                        reverts to the bank's own name"
                 placeholder={a.bank_name || undefined}
                 onChange={(e) => setName(e.target.value)} />
          {a.bank_name && a.name !== a.bank_name && (
            <span className="mut" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
              bank name “{a.bank_name}”
              <button type="button" className="btn"
                      style={{ marginLeft: 6, padding: "0 .4rem", fontSize: 12 }}
                      title="drop the custom name and go back to what the bank calls it"
                      onClick={() => setName("")}>revert</button>
            </span>
          )}
          <input type="text" value={ownerLbl} style={{ width: "8rem" }}
                 title="owner — whose money (yours / mine / ours). A reporting
                        filter; never affects the budget."
                 placeholder="owner"
                 onChange={(e) => setOwnerLbl(e.target.value)} />
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            {!KINDS.some(([v]) => v === a.kind) &&
              <option value={a.kind}>{a.kind}</option>}
          </select>
          <label className="mut" title="anchor the cash forecast on this account"
                 style={{ cursor: "pointer", whiteSpace: "nowrap" }}>
            <input type="checkbox" checked={isPrimary}
                   onChange={(e) => setIsPrimary(e.target.checked)} /> primary checking
          </label>
          <label className="mut"
                 title="exclude from budget math — its spending never counts"
                 style={{ cursor: "pointer", whiteSpace: "nowrap" }}>
            <input type="checkbox" checked={isExcl}
                   onChange={(e) => setIsExcl(e.target.checked)} /> excl. from budget
          </label>
          {manual && (
            <label className="mut">$ <input value={bal} size={9}
                   onChange={(e) => setBal(e.target.value)} placeholder="balance"
                   style={{ textAlign: "right" }} /></label>
          )}
          <button className="pri" disabled={save.isPending}>save</button>
          <button type="button" className="btn" onClick={onCancel}>cancel</button>
          {/* Hide is the middle ground between "exclude from budget" (which
              still shows the account and its balance) and disconnecting the
              whole institution. One account off a shared login, gone from
              everything, connection untouched. */}
          {owner && (
          <button type="button" className="btn" disabled={setHidden.isPending}
                  title={a.user_removed_at
                    ? "bring this account back into your totals and lists"
                    : "exclude this account from every total, list and sync — "
                      + "keeps the connection and the history"}
                  onClick={() => {
                    const to = !a.user_removed_at;
                    if (!to || window.confirm(
                        `Hide ${a.name}? It stops pulling new data and will `
                        + `not appear in or count toward anything. The `
                        + `${a.institution_name || "bank"} connection and the `
                        + `existing history stay.`))
                      setHidden.mutate(to);
                  }}>
            {setHidden.isPending ? "…"
              : a.user_removed_at ? "unhide" : "hide account"}</button>
          )}
          {/* Link's update-mode checklist is the ONLY place a
              per-account Plaid charge can be switched off — hide changes
              display, never what the institution shares (or what Plaid
              bills). Deselected accounts are auto-hidden when the session
              completes. Lives here in the editor, not inline on the
              row. */}
          {owner && a.aggregator === "plaid" && liveConn && (
            <button type="button" className="btn" disabled={linking}
                    title={"choose which accounts "
                           + (a.institution_name || "this bank")
                           + " shares with Oikonome — unshared accounts "
                           + "stop syncing and stop counting toward Plaid "
                           + "usage"}
                    onClick={() => {
                      setLinking(true);
                      void openPlaidHostedLink(a.item_id,
                        { manageAccounts: true })
                      .then((s) => onDone(
                        s.kind === "update_failed"
                          ? "that didn't complete — the connection still "
                            + "reports an error"
                            + (s.error ? ` (${s.error})` : "")
                          : s.kind === "exited"
                            ? "no change — the window was closed"
                            : "shared accounts updated"))
                      .catch((e) => onDone(errText(e)))
                      .finally(() => setLinking(false));
                    }}>
              edit connected accounts ↗</button>
          )}
          {owner && (
          <button type="button" className="btn"
                  style={{ color: "var(--red, #c44)" }}
                  onClick={() => setRemoving((v) => !v)}>
            {removing ? "close remove" : "remove…"}</button>
          )}
        </form>
        <LinkControls a={a} onDone={onDone} />
        {removing && (
          <RemoveAccountPanel a={a} liveConn={!!liveConn}
            onDone={onDone} onCancel={() => setRemoving(false)} />
        )}
      </td>
    </tr>
  );
}

function ConnectCard({ onDone, hosted = false, used, cap }: {
  onDone: DoneFn; hosted?: boolean;
  used?: number; cap?: number | null;
}) {
  const [token, setToken] = useState("");
  const [more, setMore] = useState(false);
  // the bank tab is slow to come back; a second click opened a second
  // Link session in the meantime
  const [linking, setLinking] = useState(false);
  const connect = useMutation({
    mutationFn: () => api.simplefinConnect(token),
    onSuccess: (r) => {
      setToken("");
      onDone(`Bank connected — ${r.accounts} accounts, ${r.transactions} transactions pulled.`);
    },
    onError: (e) => onDone(errText(e)),
  });
  const full = typeof cap === "number" && typeof used === "number"
    && used >= cap;
  return (
    <div className="card">
      <h2>Connect a bank</h2>
      {/* Hosted runs ONE aggregator under the platform's own credentials,
          so naming it here asks the household to care about a supplier
          they cannot choose, and the SimpleFIN box would offer a door the
          hosted API refuses. Self-host keeps both:
          there, which provider to use is genuinely the owner's call. */}
      <p className="sub" style={{ marginTop: 0 }}>
        {hosted
          ? <>A live feed. Opens your bank's sign-in in a new tab;
              balances and transactions arrive on their own after that.</>
          : <>A live feed via Plaid — opens Plaid's hosted login in a
              new tab. Balances and transactions arrive on their own after
              that.</>}
      </p>
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                    alignItems: "center" }}>
        <button className="pri" disabled={full || linking}
          title={full ? "you are at this account's institution limit" : undefined}
          onClick={() => {
            setLinking(true);
            void openPlaidHostedLink()
            .then((s) => onDone(s.institution
              ? `Linked ${s.institution}` : "Linked bank"))
            .catch((e) => onDone(errText(e)))
            .finally(() => setLinking(false));
          }}>
          {linking ? "connecting…"
            : hosted ? "+ Connect an account ↗" : "+ Add via Plaid ↗"}</button>
      </div>
      {/* The two cards are peers, so they carry the same SHAPE: a
          heading, a line saying what this is, the action, then the
          detail you need to choose it. Without this list the bank card
          was a button in a field of empty space beside a dense importer
          — equal height, nothing like equal weight. */}
      <ul className="sub" style={{ margin: ".8rem 0 0", paddingLeft: "1.1rem" }}>
        <li>Balances and transactions, refreshed hourly</li>
        <li>Investment holdings and loan details where the bank offers them</li>
        <li>Recurring bills and income found for you as the feed fills</li>
      </ul>
      {/* HOW MANY OF THE CAP'S INSTITUTIONS ARE SPENT — the number a
          household wants ("how many left?"). Both figures come from the
          server: the cap has a per-tenant override an operator can raise,
          so no number is hardcoded here; the wording around them is the
          installed add-on's. */}
      {typeof cap === "number" && typeof used === "number" && (
        <p className="sub" style={{ marginTop: ".6rem", marginBottom: 0 }}>
          {ext.allowanceNote(used, cap, full)}
        </p>
      )}
      {/* Self-host only, and behind a disclosure: SimpleFIN is a real
          door there (the owner picks their own aggregator) but it is a
          token-paste flow that should not be the first thing next to the
          one-click one. */}
      {!hosted && (
        <div style={{ marginTop: ".7rem" }}>
          <a className="mut" style={{ cursor: "pointer", fontSize: 13 }}
             onClick={() => setMore((v) => !v)}>
            {more ? "▾" : "▸"} more ways to connect</a>
          {more && (
            <div style={{ marginTop: ".45rem" }}>
              <p className="sub" style={{ marginTop: 0 }}>
                Paste a SimpleFIN setup token from{" "}
                <a href="https://bridge.simplefin.org" rel="noopener">
                  bridge.simplefin.org</a>.</p>
              <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                            alignItems: "center" }}>
                <input value={token} placeholder="SimpleFIN setup token"
                       style={{ flex: "1 1 14rem" }}
                       onChange={(e) => setToken(e.target.value)} />
                <button className="pri"
                        disabled={!token.trim() || connect.isPending}
                        onClick={() => connect.mutate()}>
                  {connect.isPending ? "connecting…" : "connect"}</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** A manual account is a CONTAINER FOR A FILE, not a peer of a bank
 *  connection — it pulls nothing on its own. It sat beside the Plaid
 *  button, which made "connect" and "type a name in a box" look like two
 *  ways to do the same thing; it belongs with the importer that fills
 *  it. */
export function ManualAccountRow({ onDone }: { onDone: DoneFn }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState(KINDS[0][0]);
  const [bal, setBal] = useState("");
  const add = useMutation({
    mutationFn: () => api.accountAdd(name, kind, bal),
    // a new empty container is a new row and nothing else
    onSuccess: () => { setName(""); onDone(`Added ${name}.`, ["accounts"]); },
    onError: (e) => onDone(errText(e)),
  });
  return (
    <>
      <p className="sub" style={{ margin: ".9rem 0 .35rem" }}>
        No connection for it? Make an account to import into.
      </p>
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                    alignItems: "center" }}>
        <input value={name} placeholder="e.g. Everyday Checking"
               style={{ flex: "1 1 11rem" }}
               onChange={(e) => setName(e.target.value)} />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <input value={bal} placeholder="opening balance (0)"
               inputMode="decimal" style={{ width: "9rem" }}
               onChange={(e) => setBal(e.target.value)} />
        <button className="pri" disabled={!name.trim() || add.isPending}
                onClick={() => add.mutate()}>+ add account</button>
      </div>
    </>
  );
}
