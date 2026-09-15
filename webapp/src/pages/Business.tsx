import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

// double-submit guard: wrap an async action, expose a pending flag to disable
// its trigger while in flight
function useBusy() {
  const [busy, setBusy] = useState(false);
  const run = (p: Promise<unknown>) => {
    setBusy(true);
    return Promise.resolve(p).finally(() => setBusy(false));
  };
  return [busy, run] as const;
}
import { Link, useSearchParams } from "react-router";
import BusinessWizard from "../components/BusinessWizard";
import ReceiptPanel from "../components/ReceiptPanel";
import { NoteEditor } from "../components/TxnTable";
import { api, errText, kindIs, mmdd, money, type Account, type BizData,
         type BizTxn, type BusinessSummary, type Entity, type EquityMovement,
         type MileageTrip, type Obligation, type Vendor,
         catLabel } from "../api/client";
import { patchList, patchQuery, removeFromList } from "../api/cache";
import { canEdit, isOwner, isViewer } from "../role";

// Assigning an account moves its money between the personal and business
// sides, so everything derived from that entity's ledger shifts with it.
// The keys are entity-scoped; the page only ever has one open.
const afterAccountsMoved = (qc: ReturnType<typeof useQueryClient>,
                            entityId: string) => {
  for (const k of ["pnl", "biz-txns", "balancesheet", "esttax",
                   "biz-suggestions", "vendors"])
    qc.invalidateQueries({ queryKey: [k, entityId] });
  qc.invalidateQueries({ queryKey: ["biz-summary"] });
  qc.invalidateQueries({ queryKey: ["today"] });
};

// business-entity management ----------------------------------------

const STRUCTURES: [string, string][] = [
  ["single_member_llc", "Single-member LLC"],
  ["multi_member_llc", "Multi-member LLC"],
  ["sole_prop", "Sole proprietorship"],
  ["s_corp", "S-corp"],
];
const structLabel = (s: string) =>
  STRUCTURES.find(([v]) => v === s)?.[1] || s;

const toastErr = (e: unknown) => window.dispatchEvent(
  new CustomEvent("oiko-toast", { detail: errText(e) }));
// confirm before a destructive/irreversible action
const confirmThen = (msg: string, run: () => void) => {
  if (window.confirm(msg)) run();
};

// Business is its own section with its own sub-menu, a second row under
// the main nav. On one page everything is cluttered together and nothing
// is findable; the section COMPONENTS (EquitySection, BooksSection,
// TaxSection...) exist either way — what the sub-menu adds is a way to
// see one at a time.
export type BizTab = "overview" | "transactions" | "books" | "tax"
  | "log" | "settings";

const BIZ_TABS: { key: BizTab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "transactions", label: "Review" },
  { key: "books", label: "Books" },
  { key: "tax", label: "Tax" },
  { key: "log", label: "Receipts" },
];

// BIZ_TABS is the NAV list — "settings" is missing from it on purpose (it
// renders apart, pushed right). Validating the ?tab= param against that list
// would reject "settings" as unknown and bounce every click back to
// overview, leaving the tab unreachable. Validate against every real tab.
const BIZ_TAB_KEYS: readonly BizTab[] = [...BIZ_TABS.map((x) => x.key),
                                         "settings"];

function BizNav({ tab, onTab }: { tab: BizTab; onTab: (t: BizTab) => void }) {
  return (
    <div style={{ display: "flex", gap: ".3rem", flexWrap: "wrap",
                  margin: ".2rem 0 1rem", overflowX: "auto" }}>
      {BIZ_TABS.map((x) => (
        <button key={x.key} className={tab === x.key ? "pri" : ""}
                onClick={() => onTab(x.key)}>{x.label}</button>
      ))}
      {/* Pushed right and quieter than its siblings, because setup is
          something you finish rather than a place you work — but still a
          BUTTON. Stripped of background and border it reads as a caption
          nobody would think to press, and then turns into a solid blue pill
          on click: the one control that looks inert is the one that moves.
          Same chrome as the other tabs; only the colour is softer while it
          is unselected. */}
      <button className={tab === "settings" ? "pri" : ""}
              style={{ marginLeft: "auto",
                       ...(tab === "settings" ? {}
                         : { color: "var(--mut)" }) }}
              onClick={() => onTab("settings")}>Settings</button>
    </div>
  );
}

function AddEntity({ onDone, onCancel }:
  { onDone: (id?: string) => void; onCancel: () => void }) {
  const [f, setF] = useState({ name: "", structure: "single_member_llc",
    state: "", ein: "", formation_date: "", business_start_date: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k: string) => (e: { target: { value: string } }) =>
    setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    setBusy(true); setErr("");
    try {
      const created = await api.entityCreate({
        name: f.name.trim(), structure: f.structure,
        state: f.state || undefined, ein: f.ein || undefined,
        formation_date: f.formation_date || undefined,
        business_start_date: f.business_start_date || undefined });
      onDone(created.id);
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  return (
    <div className="card" style={{ marginTop: ".8rem" }}>
      <h3 style={{ marginTop: 0 }}>Add a business</h3>
      <div style={{ display: "grid", gap: ".6rem",
        gridTemplateColumns: "repeat(auto-fit,minmax(12rem,1fr))" }}>
        <label>Name<input value={f.name} onChange={set("name")}
          placeholder="Acme LLC" /></label>
        <label>Structure<select value={f.structure} onChange={set("structure")}>
          {STRUCTURES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select></label>
        <label>State<input value={f.state} onChange={set("state")}
          placeholder="CA" maxLength={2} /></label>
        <label>EIN<input value={f.ein} onChange={set("ein")}
          placeholder="XX-XXXXXXX" /></label>
        <label>Formation date<input type="date" value={f.formation_date}
          onChange={set("formation_date")} /></label>
        <label>Business start<input type="date" value={f.business_start_date}
          onChange={set("business_start_date")} /></label>
      </div>
      <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem" }}>
        <button className="pri" disabled={busy || !f.name.trim()}
          onClick={submit}>Create</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      {err && <p className="bad" style={{ marginBottom: 0 }}>{err}</p>}
      <p className="mut" style={{ marginBottom: 0, fontSize: ".85rem" }}>
        The EIN is encrypted at rest — only its last 4 digits are shown back.</p>
    </div>
  );
}

function EditEntityForm({ entity, onDone, onCancel }:
  { entity: Entity; onDone: () => void; onCancel: () => void }) {
  const [f, setF] = useState({
    name: entity.name, structure: entity.structure, state: entity.state || "",
    ein: "", formation_date: entity.formation_date || "",
    business_start_date: entity.business_start_date || "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k: string) => (e: { target: { value: string } }) =>
    setF({ ...f, [k]: e.target.value });
  const save = async () => {
    setBusy(true); setErr("");
    try {
      const body: Record<string, unknown> = {
        name: f.name.trim(), structure: f.structure, state: f.state || null,
        formation_date: f.formation_date || null,
        business_start_date: f.business_start_date || null };
      if (f.ein.trim()) body.ein = f.ein.trim();   // EIN is write-only; only send if changing
      await api.entityUpdate(entity.id, body);
      onDone();
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  return (
    <div className="card" style={{ marginTop: ".6rem" }}>
      <h4 style={{ marginTop: 0 }}>Edit details</h4>
      <div style={{ display: "grid", gap: ".6rem",
        gridTemplateColumns: "repeat(auto-fit,minmax(12rem,1fr))" }}>
        <label>Name<input value={f.name} onChange={set("name")} /></label>
        <label>Structure<select value={f.structure} onChange={set("structure")}>
          {STRUCTURES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select></label>
        <label>State<input value={f.state} onChange={set("state")}
          maxLength={2} /></label>
        <label>EIN<input value={f.ein} onChange={set("ein")}
          placeholder={entity.ein_last4 ? `•••••${entity.ein_last4} (unchanged)` : "XX-XXXXXXX"} /></label>
        <label>Formation date<input type="date" value={f.formation_date}
          onChange={set("formation_date")} /></label>
        <label>Business start<input type="date" value={f.business_start_date}
          onChange={set("business_start_date")} /></label>
      </div>
      <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem" }}>
        <button className="pri" disabled={busy || !f.name.trim()}
          onClick={save}>Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      {err && <p className="bad" style={{ marginBottom: 0 }}>{err}</p>}
    </div>
  );
}

/** Overview: the four numbers an owner actually looks at. Not a redirect
 *  and not an empty-when-clean inbox — a real dashboard, because "how is
 *  the business doing" is one of the four jobs people come here for.
 *
 *  Every figure is sourced from an endpoint that already exists; nothing is
 *  recomputed here beyond summing this month's rows, which the P&L does not
 *  break out by month.
 */
/** Schedule C review queue: uncategorized only, accept or override, until
 *  empty — a dedicated review queue, not a filter over the ledger.
 *
 *  Suggest-and-confirm, NOT auto-apply. The personal side auto-applies a
 *  learned merchant category because a wrong one is cosmetic; a wrong
 *  Schedule C line is a wrong number on a filed return. So every row is
 *  committed by a human, and the queue says WHERE each suggestion came
 *  from — "merchant" is your own prior decision for that vendor, "category"
 *  is a weak guess from the ledger, and no badge means it had no basis and
 *  declined to guess rather than inventing one.
 */
/** Tax set-aside: what you owe versus what is actually sitting in the
 *  business — put aside a share of profit, and keep a running balance of
 *  owed versus saved.
 *
 *  The RATE is not a flat 25% — estimated tax already computes the real
 *  figure from profit, SE tax and deductions, so a hardcoded percentage
 *  would be less accurate than what the app already knows. The effective
 *  percentage is shown instead, which answers the same question honestly.
 *
 *  "Saved" is the NOMINATED tax-reserve account when the entity has one —
 *  the ring-fenced answer. Until then it falls back to all business cash,
 *  which answers the weaker question "is the money there at all", and the
 *  card says which of the two it is showing rather than letting the number
 *  imply the stronger one.
 */
function TaxSetAside({ entity }: { entity: Entity }) {
  const year = new Date().getFullYear();
  const tax = useQuery({ queryKey: ["esttax", entity.id, year],
    queryFn: () => api.estTax(entity.id, year) });
  const accts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });

  if (tax.isPending) return null;
  const owed = tax.data?.total_estimated ?? 0;
  const profit = tax.data?.net_profit ?? 0;
  // Shadow exclusion, the same rule the server applies to every money sum:
  // an account linked through two aggregators leaves two rows sharing one
  // group, and both can carry entity_id, so the fallback "all business
  // cash" counted one real balance twice.
  const mine = (accts.data?.accounts || [])
    .filter(a => a.entity_id === entity.id && kindIs(a, "depository"))
    .filter(a => !a.link || a.link.primary);
  // A nomination pointing at an account that has since been purged or
  // reassigned must not read as $0 saved — that would be a scarier number
  // than the truth. Fall back, and say so.
  const reserve = entity.tax_reserve_account_id
    ? mine.find(a => a.id === entity.tax_reserve_account_id) : undefined;
  const ringFenced = !!reserve;
  const cash = ringFenced
    ? (reserve!.balance_current ?? 0)
    : mine.reduce((s, a) => s + (a.balance_current ?? 0), 0);
  const stale = !!entity.tax_reserve_account_id && !reserve;
  const gap = cash - owed;
  const pct = profit > 0 ? Math.round((owed / profit) * 100) : null;

  return (
    <div className="card">
      <h4 style={{ marginTop: 0 }}>Set aside for tax</h4>
      <div className="grid cols3" style={{ gap: ".6rem" }}>
        <div>
          <div className="sub">Estimated for {year}</div>
          <div className="kpi sm">{money(owed)}</div>
          {pct !== null && <div className="sub">{pct}% of profit</div>}
        </div>
        <div>
          <div className="sub">{ringFenced ? "In the tax account"
                                           : "Business cash"}</div>
          <div className="kpi sm">{money(cash)}</div>
          {ringFenced && <div className="sub">{reserve!.name}
            {reserve!.mask ? ` ····${reserve!.mask}` : ""}</div>}
        </div>
        <div>
          <div className="sub">{gap >= 0 ? "Covered by" : "Short by"}</div>
          <div className="kpi sm" style={{ color: gap >= 0
            ? "var(--green, #4a4)" : "var(--amber, #e6b159)" }}>
            {money(Math.abs(gap))}</div>
        </div>
      </div>
      {stale && (
        <div className="note" style={{ marginTop: ".5rem" }}>
          The account nominated as this business's tax reserve is no longer
          assigned to it, so this is showing all business cash instead. Pick
          one again under Settings → Tax reserve.
        </div>
      )}
      <p className="sub" style={{ marginBottom: 0 }}>
        {ringFenced
          ? <>“Saved” is the balance of the account you nominated as this
              business's tax reserve, so this answers “is it ring-fenced”.</>
          : <>“Saved” here is all business cash, so this answers “is the money
              there”, not “is it ring-fenced”. Nominate a tax account under
              Settings to make it the stronger question.</>}
        {" "}The percentage is computed from your actual profit and SE tax,
        not a flat rule of thumb. Not tax advice.
      </p>
    </div>
  );
}

/** Which account holds this entity's tax money.
 *
 *  Measured against ALL business cash, "Set aside for tax" answers "is the
 *  money there", not "is it ring-fenced". Setting estimated tax aside in a
 *  separate account is ordinary practice for a self-employed filer, and the
 *  app already computes the figure — so the entity records where that money
 *  lives.
 *
 *  Only accounts already ASSIGNED to this business are offered — the server
 *  refuses anything else, because nominating a personal account (or another
 *  entity's) would misreport the set-aside for both.
 */
function TaxReservePicker({ entity, accounts, ro }: {
  entity: Entity;
  accounts: { id: string; name: string; mask: string | null; kind: string;
              balance_current: number | null }[];
  ro: boolean;
}) {
  const qc = useQueryClient();
  const [busy, run] = useBusy();
  const eligible = accounts.filter(a => kindIs(a, "depository"));
  const current = entity.tax_reserve_account_id || "";
  // nominated, but that account is no longer one of this entity's
  const dangling = !!current && !eligible.some(a => a.id === current);
  // The select is bound to the entity in the cached summary, so write the
  // nomination there as the response lands, or it snaps back to the old
  // value until the summary refetches. A nomination moves no money:
  // only the summary and the entity itself change.
  const save = (id: string) => run(
    api.entityUpdate(entity.id, { tax_reserve_account_id: id })
      .then((e) => {
        patchList<BusinessSummary, Entity>(qc, ["biz-summary"], "entities",
          (x) => x.id === entity.id,
          { tax_reserve_account_id: e.tax_reserve_account_id ?? (id || null) });
        qc.setQueryData<Entity>(["entity", entity.id], (old) =>
          old && { ...old, ...e });
        qc.invalidateQueries({ queryKey: ["biz-summary"] });
        qc.invalidateQueries({ queryKey: ["entity", entity.id] });
      })
      .catch(toastErr));

  if (!eligible.length)
    return (
      <p className="mut" style={{ marginTop: 0, fontSize: ".85rem" }}>
        Assign a bank account to this business first — then you can nominate
        one of them as the account its tax money sits in.
      </p>
    );
  return (
    <>
      <p className="mut" style={{ marginTop: 0, fontSize: ".85rem" }}>
        Nominate the account you keep this business's tax money in. The
        set-aside card then measures THAT balance against what you owe,
        instead of all business cash — the difference between "is it
        ring-fenced" and "is the money there somewhere".
      </p>
      {/* A nomination whose account has since been unassigned reads as
          "none nominated" here, while the set-aside card correctly says a
          nominated account is missing. Two surfaces disagreeing about the
          same fact is worse than either message alone — say it here too,
          and keep the value so a re-assign restores it rather than
          silently resetting to none. */}
      {dangling && (
        <div className="note" style={{ marginTop: 0 }}>
          The account nominated as this business's tax reserve is no longer
          assigned to it, so the set-aside card is measuring all business
          cash. Re-assign that account, or pick another below.
        </div>
      )}
      <select value={dangling ? "" : current} disabled={ro || busy}
              aria-label="Tax reserve account"
              onChange={(e) => save(e.target.value)}>
        <option value="">{dangling
          ? "— the nominated account is missing — pick another —"
          : "— none nominated (measure all business cash) —"}</option>
        {eligible.map(a => (
          <option key={a.id} value={a.id}>
            {a.name}{a.mask ? ` ····${a.mask}` : ""}
            {a.balance_current != null ? ` — ${money(a.balance_current)}` : ""}
          </option>
        ))}
      </select>
    </>
  );
}

function ReviewQueue({ entity, ro }: { entity: Entity; ro?: boolean }) {
  const qc = useQueryClient();
  // pending per ROW — one shared flag would grey every Accept in the queue
  // while a single decision is in flight
  const [pending, setPending] = useState<string | null>(null);
  const [override, setOverride] = useState<Record<string, string>>({});
  const q = useQuery({ queryKey: ["biz-suggestions", entity.id],
    queryFn: () => api.bizSuggestions(entity.id) });
  type Sugg = NonNullable<typeof q.data>;

  const rows = q.data?.suggestions || [];
  const lines = q.data?.lines || [];
  const commit = (txnId: string, bucket: string, line: string) => {
    setPending(txnId);
    api.bizClassify(entity.id, txnId, { bucket, sched_c_line: line })
      .then(() => {
        // the decided row leaves the queue now — the server has committed
        // it, and waiting for the refetch to prove that left it sitting
        // there; the count on the Overview tile follows the same payload
        qc.setQueryData<Sugg>(["biz-suggestions", entity.id], (old) =>
          old && { ...old,
                   suggestions: old.suggestions.filter((x) => x.txn_id !== txnId),
                   unclassified: Math.max(0, old.unclassified - 1) });
        qc.invalidateQueries({ queryKey: ["biz-suggestions", entity.id] });
        qc.invalidateQueries({ queryKey: ["pnl", entity.id] });
        qc.invalidateQueries({ queryKey: ["biz-txns", entity.id] });
      })
      .catch((e) => window.dispatchEvent(new CustomEvent("oiko-toast",
        { detail: errText(e) })))
      .finally(() => setPending((p) => (p === txnId ? null : p)));
  };

  if (q.isPending) return <p className="mut">loading…</p>;
  if (!rows.length)
    return (
      <div className="card">
        <h4 style={{ marginTop: 0 }}>Schedule C review</h4>
        <p className="mut" style={{ marginBottom: 0 }}>
          Nothing to review — every business transaction has a line.</p>
      </div>
    );

  return (
    <div className="card">
      <h4 style={{ marginTop: 0 }}>Schedule C review
        <span className="mut" style={{ fontWeight: 400 }}> — {rows.length} to go</span>
      </h4>
      <p className="sub" style={{ marginTop: 0 }}>
        Nothing is filed automatically. Accept a suggestion or pick a
        different line; a wrong line is a wrong number on your return.
      </p>
      <table className="txn-table"><tbody>
        {rows.slice(0, 25).map((s) => {
          const chosen = override[s.txn_id] ?? s.suggested_line ?? "";
          return (
            <tr key={s.txn_id}>
              <td style={{ whiteSpace: "nowrap" }} className="mut">
                {s.date ? mmdd(s.date) : ""}</td>
              <td>{s.payee}
                {s.source === "merchant" && (
                  <span className="pill g" style={{ marginLeft: 6 }}
                        title="you classified this merchant before">learned</span>)}
                {s.source === "category" && (
                  <span className="pill m" style={{ marginLeft: 6 }}
                        title="guessed from the ledger category — check it">guess</span>)}
              </td>
              <td className="num">{money(s.amount)}</td>
              <td>
                {/* viewer-gated like every other Business write — these
                    controls 403 server-side, so they must not render
                    enabled */}
                <select value={chosen} disabled={ro}
                        onChange={(e) => setOverride(
                          { ...override, [s.txn_id]: e.target.value })}>
                  <option value="">choose a line…</option>
                  {lines.map((l) => <option key={l} value={l}>{l}</option>)}
                </select>
              </td>
              <td style={{ width: "1%" }}>
                <button className="pri"
                        disabled={pending === s.txn_id || !chosen || ro}
                        onClick={() => commit(s.txn_id, s.suggested_bucket,
                                              chosen)}>
                  {s.suggested_line && chosen === s.suggested_line
                    ? "Accept" : "Save"}</button>
              </td>
            </tr>
          );
        })}
      </tbody></table>
      {rows.length > 25 && (
        <p className="sub">Showing 25 of {rows.length} — the list refills as
          you go.</p>
      )}
    </div>
  );
}

function OverviewSection({ entity }: { entity: Entity }) {
  const year = new Date().getFullYear();
  const pnl = useQuery({ queryKey: ["pnl", entity.id, year],
    queryFn: () => api.entityPnl(entity.id, year) });
  const txns = useQuery({ queryKey: ["biz-txns", entity.id, year],
    queryFn: () => api.bizTxns(entity.id, year) });
  const accts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const comp = useQuery({ queryKey: ["compliance", entity.id],
    queryFn: () => api.entityCompliance(entity.id) });
  const tax = useQuery({ queryKey: ["esttax", entity.id, year],
    queryFn: () => api.estTax(entity.id, year) });
  const sugg = useQuery({ queryKey: ["biz-suggestions", entity.id],
    queryFn: () => api.bizSuggestions(entity.id) });

  // LOCAL date, not toISOString() — that is UTC, so west of Greenwich it
  // reports the previous day/month for most of the evening
  const ymd = (d: Date) => `${d.getFullYear()}-`
    + `${String(d.getMonth() + 1).padStart(2, "0")}-`
    + `${String(d.getDate()).padStart(2, "0")}`;
  const ym = ymd(new Date()).slice(0, 7);
  // Mirror books._pnl_core's exclusions exactly. The annual figure directly
  // above this one skips transfers and card payments per the house rule
  // ("Spend excludes LOAN_PAYMENTS_CREDIT_CARD_PAYMENT and TRANSFER_*"), so
  // summing the raw ledger here would put a different methodology on the
  // same tile — an owner draw reading as a loss against a profitable month.
  const inPnl = (x: { category?: string | null; amount?: number;
                      cat_detailed?: string | null;
                      cat_original?: string | null }) =>
    !(x.category || "").includes("TRANSFER")
    && (x.cat_detailed || "") !== "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    // money in whose ORIGINAL category was a transfer is a capital move,
    // not revenue, however its display category was later renamed — the
    // server's books._pnl_core applies the same was_transfer rule
    && !((x.amount ?? 0) < 0
         && (x.cat_original || "").toUpperCase().includes("TRANSFER")
         && (x.category || "") !== "INCOME");
  // ...including its bucket split. Organizational and §195 start-up costs
  // are capitalized, not ordinary deductions, so they stay out of
  // net_operating — the figure this sub-label sits under. An unclassified
  // cost dated before the business opened defaults to the start-up bucket
  // (books.default_bucket), so a brand-new LLC's filing fee would otherwise
  // read as a loss for the month beneath a $0 profit for the year.
  const start = entity.business_start_date || null;
  const operating = (x: { bucket?: string | null; date?: string | null }) => {
    const bucket = x.bucket
      || (start && x.date && x.date < start ? "startup_195" : "operating");
    return bucket !== "organizational" && bucket !== "startup_195";
  };
  // house sign convention: positive = money out. Revenue (money in) always
  // counts; an expense counts only in the operating bucket.
  const monthProfit = (txns.data?.transactions || [])
    .filter(x => (x.date || "").startsWith(ym) && inPnl(x))
    .reduce((s, x) => (x.amount < 0 || operating(x)) ? s - x.amount : s, 0);

  // Shadow exclusion, same as TaxSetAside below: an account linked through
  // two aggregators leaves two rows sharing one group, and both can carry
  // entity_id — summing every row counts one real balance twice.
  const cash = (accts.data?.accounts || [])
    .filter(a => a.entity_id === entity.id && kindIs(a, "depository"))
    .filter(a => !a.link || a.link.primary)
    .reduce((s, a) => s + (a.balance_current ?? 0), 0);

  const today = ymd(new Date());        // local, per ymd() above
  const next = (comp.data?.obligations || [])
    .filter(o => (o.due || "") >= today)
    .sort((a, b) => (a.due || "").localeCompare(b.due || ""))[0];

  // the endpoint's real COUNT, not the length of the capped page it
  // returned — that would stick the tile at the page size
  const attention = sugg.data?.unclassified ?? 0;

  const Tile = ({ label, value, sub }: {
    label: string; value: string; sub?: string }) => (
    <div className="tile">
      <div className="sub">{label}</div>
      <div className="kpi sm">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );

  return (
    <div className="grid cols4" style={{ gap: ".6rem", marginTop: ".7rem" }}>
      <Tile label="Profit this year"
            value={pnl.data ? money(pnl.data.net_operating) : "—"}
            sub={`${money(monthProfit)} this month`} />
      <Tile label="Cash in the business" value={money(cash)}
            sub={`${(accts.data?.accounts || [])
              .filter(a => a.entity_id === entity.id && (!a.link || a.link.primary))
              .length} account(s)`} />
      <Tile label="Next deadline"
            value={next ? mmdd(next.due) : "none"}
            sub={tax.data ? `${money(tax.data.quarterly)} est. quarterly` : undefined} />
      <Tile label="Needs attention"
            value={sugg.isPending ? "…" : String(attention)}
            sub={attention ? "unclassified transactions" : "nothing outstanding"} />
    </div>
  );
}

function EntityDetail({ entity, flaggedUnassigned, onChange, tab,
                        combined, onCombine, onAdd }:
  { entity: Entity; flaggedUnassigned: number; onChange: () => void;
    tab: BizTab; combined?: boolean; onCombine?: (v: boolean) => void;
    onAdd?: () => void }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  // delete-forever confirm open (typed-name gate lives in the card)
  const [nuke, setNuke] = useState(false);
  const accts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const full = useQuery({ queryKey: ["entity", entity.id],
    queryFn: () => api.entityGet(entity.id) });
  const [busy, run] = useBusy();
  // same reason as the wizard: on a ledger with dozens of accounts an
  // unfiltered list is useless for picking the two this business owns
  const [acctQ, setAcctQ] = useState("");
  // the assignment picker is an ACTION, not the default view
  const [picking, setPicking] = useState(false);
  const [showAllAccts, setShowAllAccts] = useState(false);
  const act = (p: Promise<unknown>) => run(p.then(onChange).catch((e) =>
    window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: errText(e) }))));
  // The assigned list and the picker both read entity_id off the cached
  // accounts, so write it there as the response lands — the row moves
  // between "assigned" and the picker at once. Then refetch what an
  // account changing sides really moves (accounts, the entity's books,
  // the verdict) — not the flagged worksheet, which this never touches.
  const assign = (accountId: string, to: string | null) => run(
    api.accountAssignEntity(accountId, to).then(() => {
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"], "accounts",
        (a) => a.id === accountId, { entity_id: to });
      qc.invalidateQueries({ queryKey: ["accounts"] });
      afterAccountsMoved(qc, entity.id);
    }).catch(toastErr));
  const assigned = (accts.data?.accounts || []).filter(a => a.entity_id === entity.id);
  // Read-only when the entity is archived OR the person is a viewer.
  // Checking only the entity would show an invited household member every
  // control on this page fully enabled — rename, edit, archive, assign
  // accounts, add members — each of which 403s server-side, making the whole
  // page an invitation to a wall. Every other page makes the same check
  // (canEdit).
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const ro = entity.status !== "active" || isViewer(meQ);
  return (
    <div className="card" style={{ marginTop: 0 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: ".6rem",
        flexWrap: "wrap" }}>
        <b style={{ fontSize: "1.05rem" }}>{entity.name}</b>
        <span className="sub">{structLabel(entity.structure)}
          {entity.state ? ` · ${entity.state}` : ""}
          {entity.ein_last4 ? ` · EIN •••••${entity.ein_last4}` : ""}
          {entity.business_start_date
            ? ` · operating since ${entity.business_start_date}` : ""}</span>
        {ro && <span className="pill m">archived · read-only</span>}
        <label className="sub" style={{ marginLeft: "auto", display: "flex",
              gap: ".35rem", alignItems: "center" }}
              title="show business + personal together in every view">
          <input type="checkbox" checked={!!combined}
                 onChange={(e) => onCombine?.(e.target.checked)} />
          combined view</label>
      </div>
      {/* the four tiles are the page's headline, not something below the
          management furniture */}
      {tab === "overview" && <OverviewSection entity={entity} />}
      {/* settings owns the lifecycle: edit, archive/restore, delete
          forever, add another */}
      {tab === "settings" && (
        <>
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                      marginTop: ".7rem" }}>
          {!ro && <button onClick={() => setEditing(v => !v)}>
            {editing ? "Close" : "Edit details"}</button>}
          {/* the lifecycle is a write too — viewer-gated like Edit right
              above it (ro also covers archived, where Restore must
              stay reachable for owners: gate on ROLE, not ro) */}
          {canEdit(meQ) && (
            entity.status === "active" ? (
              /* reversible, but say what it means before it happens —
                 the point is that nothing is lost. Mobile asks the same
                 question in its own confirm. */
              <button disabled={busy}
                title="hides the business; every record is kept"
                onClick={() => confirmThen(
                  `Archive ${entity.name}? Every record is kept — books, `
                  + "equity, mileage, compliance stay readable, and you can "
                  + "restore any time.",
                  () => act(api.entityArchive(entity.id)))}>
                Archive</button>
            ) : (
              <button disabled={busy}
                onClick={() => act(api.entityRestore(entity.id))}>
                Restore</button>
            )
          )}
          {isOwner(meQ) && (
            <button disabled={busy} style={{ color: "var(--red)" }}
              onClick={() => setNuke(v => !v)}>Delete forever…</button>
          )}
          {onAdd && <button onClick={onAdd}>+ Add business</button>}
        </div>
        {canEdit(meQ) && entity.status === "active" && (
          <p className="mut" style={{ margin: ".25rem 0 0",
                                      fontSize: ".8rem" }}>
            Archive keeps every record — books, equity, mileage, compliance
            stay readable, and you can restore any time. Delete forever is
            only for a business created by mistake.</p>
        )}
        {nuke && isOwner(meQ) && (
          <DeleteForeverCard entity={entity}
            onDone={() => { setNuke(false); onChange(); }}
            onCancel={() => setNuke(false)} />
        )}
        </>
      )}
      {tab === "settings" && editing && !ro && <EditEntityForm entity={entity}
        onDone={() => { setEditing(false); onChange();
          qc.invalidateQueries({ queryKey: ["entity", entity.id] }); }}
        onCancel={() => setEditing(false)} />}

      {flaggedUnassigned > 0 && !ro && (
        <div className="note" style={{ marginTop: ".4rem" }}>
          <span style={{ flex: 1 }}>{flaggedUnassigned} transaction
            {flaggedUnassigned === 1 ? "" : "s"} carry the old business flag.
            Import them into this entity?</span>
          <button disabled={busy}
            onClick={() => act(api.entityImportFlags(entity.id).then((r) => {
              // the flagged rows are now this entity's books — and gone
              // from the personal verdict, ledger, reports and lenses
              for (const k of ["pnl", "biz-txns", "biz-suggestions"])
                qc.invalidateQueries({ queryKey: [k, entity.id] });
              for (const k of ["today", "txns", "report", "lens-month",
                               "lens-year"])
                qc.invalidateQueries({ queryKey: [k] });
              return r;
            }))}>
            Import {flaggedUnassigned}</button>
        </div>
      )}


      {/* Setup, not workspace. Entity details, account assignment and
          members on the main page leave it looking half-configured long
          after setup is done, so they live behind a de-emphasized Settings
          tab — reachable, not occupying the surface you work on every
          day. */}
      {tab === "settings" && <>
      <h4 style={{ marginBottom: ".3rem" }}>Accounts</h4>
      {/* Listing EVERY account in the tenant here makes an assignment
          PICKER do duty as a status DISPLAY, so a page about one business
          shows dozens of unrelated, non-business accounts. Default to this
          entity's own accounts; the picker is a deliberate action behind a
          button. */}
      {!picking ? (
        <>
          <p className="mut" style={{ marginTop: 0, fontSize: ".85rem" }}>
            {assigned.length
              ? "Everything in these accounts is this business's money — out of your personal budget."
              : "No accounts yet. Assign one and all its transactions become this business's money."}
          </p>
          <table><tbody>
            {assigned.map(a => (
              <tr key={a.id}>
                <td>{a.name} <span className="mut">{a.kind}</span></td>
                <td className="num mut">{a.balance_current != null
                  ? money(a.balance_current) : ""}</td>
                <td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  {ro ? "assigned" : (
                    <button disabled={busy}
                      onClick={() => confirmThen(
                        `Unassign ${a.name}? Its transactions return to personal.`,
                        () => assign(a.id, null))}>
                      unassign</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody></table>
          {!ro && (
            <button style={{ marginTop: ".5rem" }}
                    onClick={() => setPicking(true)}>
              Assign an account…</button>
          )}
        </>
      ) : (
      <>
      <p className="mut" style={{ marginTop: 0, fontSize: ".85rem" }}>
        Pick an account the business owns. All of its transactions — past and
        future — become business money and leave your personal budget.</p>
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center",
                    flexWrap: "wrap", margin: ".3rem 0 .5rem" }}>
        <input value={acctQ} placeholder="search accounts"
               onChange={(e) => setAcctQ(e.target.value)}
               style={{ flex: "1 1 12rem" }} />
        <button className="linklike mut"
                style={{ background: "none", border: "none", cursor: "pointer",
                         color: "var(--blue)", textDecoration: "underline" }}
                onClick={() => setShowAllAccts(!showAllAccts)}>
          {showAllAccts ? "hide closed/archived" : "show closed/archived"}
        </button>
      </div>
      {(accts.data?.accounts || []).length === 0
        ? <p className="mut">No accounts yet.</p>
        : (
        <table><tbody>
          {(accts.data?.accounts || [])
            .filter(a => showAllAccts
              || (a.status !== "archived" && a.balance_current != null))
            .filter(a => { const q = acctQ.trim().toLowerCase();
              return !q || `${a.name} ${a.institution_name || ""}`
                .toLowerCase().includes(q); })
            .sort((x, y) => `${x.institution_name || ""}${x.name}`
              .localeCompare(`${y.institution_name || ""}${y.name}`))
            .map(a => {
            const mine = a.entity_id === entity.id;
            const other = a.entity_id && a.entity_id !== entity.id;
            return (
              <tr key={a.id}>
                <td>{a.name} <span className="mut">{a.kind}</span></td>
                <td className="num mut">{a.balance_current != null
                  ? money(a.balance_current) : ""}</td>
                <td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  {other ? <span className="mut">other business</span>
                    : ro ? (mine ? "assigned" : "")
                    : (
                    <button className={mine ? "pri" : ""} disabled={busy}
                      onClick={() => confirmThen(mine
                        ? `Unassign ${a.name}? Its transactions return to personal.`
                        : `Assign ${a.name} to ${entity.name}? All its transactions become business money and leave your personal budget.`,
                        () => assign(a.id, mine ? null : entity.id))}>
                      {mine ? "✓ assigned" : "assign"}</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody></table>
      )}
      <button style={{ marginTop: ".5rem" }} onClick={() => setPicking(false)}>
        Done</button>
      </>
      )}
      <p className="mut" style={{ fontSize: ".85rem" }}>
        {assigned.length} account{assigned.length === 1 ? "" : "s"} assigned.
        For a one-off business cost on a personal card, use the
        <b> business</b> button on the Transactions page.</p>

      <h4 style={{ marginBottom: ".3rem" }}>Tax reserve</h4>
      <TaxReservePicker entity={entity} accounts={assigned} ro={ro} />

      <h4 style={{ marginBottom: ".3rem" }}>Members</h4>
      {(full.data?.members || []).map(m => (
        <div key={m.id} className="mut" style={{ fontSize: ".9rem" }}>
          {m.member_name}{m.ownership_pct != null
            ? ` — ${m.ownership_pct}%` : ""}{m.is_manager ? " · manager" : ""}
        </div>
      ))}
      {!ro && <AddMember entityId={entity.id} onDone={() =>
        qc.invalidateQueries({ queryKey: ["entity", entity.id] })} />}
      </>}

      {/* one task per tab. Books is the money picture, Tax is what the
          government wants, Log is capture. Grouping follows the job being
          done, not the order these components happened to be written
          in. */}
      {tab === "books" && <>
        <BooksSection entityId={entity.id} readOnly={ro} />
        <BalanceSheetSection entityId={entity.id} />
        <EquitySection entityId={entity.id} readOnly={ro} />
      </>}
      {tab === "tax" && <>
        <TaxSetAside entity={entity} />
        {/* the whole point is ONE action — the failure mode is exporting
            four reports in March and forgetting the fifth */}
        <div className="card">
          <h4 style={{ marginTop: 0 }}>Year-end package</h4>
          <p className="sub" style={{ marginTop: 0 }}>
            P&amp;L, ledger with Schedule C lines, balance sheet, 1099 vendor
            totals and the mileage log — one zip for your accountant.
          </p>
          <a className="btn pri"
             href={`/api/business/entities/${entity.id}/package.zip` +
                   `?year=${new Date().getFullYear()}`}>
            Download {new Date().getFullYear()} package</a>
        </div>
        <TaxSection entity={entity} readOnly={ro} />
        <VendorSection entityId={entity.id} readOnly={ro} />
        <ComplianceSection entityId={entity.id} readOnly={ro} />
      </>}
      {tab === "transactions" && <ReviewQueue entity={entity} ro={ro} />}
      {tab === "log" && <MileageSection entityId={entity.id} readOnly={ro} />}
    </div>
  );
}

const EQUITY_KINDS: [string, string][] = [
  ["contribution", "Capital contribution"],
  ["draw", "Owner's draw"],
  ["distribution", "Distribution"],
  ["reimbursement", "Reimbursement"],
];

function EquitySection({ entityId, readOnly }:
  { entityId: string; readOnly: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["equity", entityId],
    queryFn: () => api.entityEquity(entityId) });
  const [f, setF] = useState({ kind: "contribution", amount: "",
    date: new Date().toISOString().slice(0, 10), form: "", note: "" });
  // a movement changes the capital account, which the balance sheet
  // beside this section also carries
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["equity", entityId] });
    qc.invalidateQueries({ queryKey: ["entity", entityId] });
    qc.invalidateQueries({ queryKey: ["balancesheet", entityId] });
  };
  const toast = (e: unknown) => window.dispatchEvent(
    new CustomEvent("oiko-toast", { detail: errText(e) }));
  const [busy, run] = useBusy();
  const add = () => run(api.equityRecord(entityId, { kind: f.kind,
    amount: Number(f.amount), date: f.date, form: f.form || undefined,
    note: f.note || undefined }).then(() => {
      setF({ ...f, amount: "", form: "", note: "" }); refresh();
    }).catch(toast));
  // one DELETE per click, and the row leaves the table as the server
  // agrees; a second click while the first is in flight would delete twice
  const [deleting, setDeleting] = useState<string | null>(null);
  const del = (id: string) => {
    setDeleting(id);
    api.equityDelete(entityId, id).then(() => {
      removeFromList<{ movements: EquityMovement[] }, EquityMovement>(
        qc, ["equity", entityId], "movements", (m) => m.id === id);
      refresh();
    }).catch(toast).finally(() => setDeleting(null));
  };
  const cap = q.data?.capital;
  return (
    <>
      <h4 style={{ marginBottom: ".3rem" }}>Owner equity</h4>
      {cap && (
        <p className="mut" style={{ marginTop: 0, fontSize: ".9rem" }}>
          Capital account:{" "}
          <b className={cap.capital_balance < 0 ? "neg" : "pos"}>
            {money(cap.capital_balance)}</b>
          {" "}(contributions {money(cap.contributions)}
          {" "}+ retained earnings {money(cap.net_income ?? 0)} − draws{" "}
          {money(cap.draws + cap.distributions)})
          {cap.reimbursements > 0 &&
            <> · reimbursements {money(cap.reimbursements)}</>}
        </p>
      )}
      {(q.data?.movements || []).length > 0 && (
        <table style={{ marginBottom: ".5rem" }}><tbody>
          {(q.data?.movements || []).map(m => (
            <tr key={m.id}>
              <td className="mut" style={{ whiteSpace: "nowrap" }}>
                {m.date ? mmdd(m.date) : ""}</td>
              <td>{EQUITY_KINDS.find(([v]) => v === m.kind)?.[1] || m.kind}
                {m.form ? <span className="mut"> · {m.form}</span> : ""}</td>
              <td className="num">{money(m.amount)}</td>
              <td style={{ width: "1%" }}>{!readOnly &&
                <button title="delete" disabled={deleting === m.id}
                  onClick={() => confirmThen(
                  "Delete this equity movement? It changes the capital-account balance.",
                  () => del(m.id))}>
                  ✕</button>}</td>
            </tr>
          ))}
        </tbody></table>
      )}
      {!readOnly && (
        <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap",
          alignItems: "center" }}>
          <select value={f.kind}
            onChange={(e) => setF({ ...f, kind: e.target.value })}>
            {EQUITY_KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <input type="number" value={f.amount} placeholder="amount"
            style={{ maxWidth: "7rem" }}
            onChange={(e) => setF({ ...f, amount: e.target.value })} />
          <input type="date" value={f.date}
            onChange={(e) => setF({ ...f, date: e.target.value })} />
          <input value={f.form} placeholder="form (Cash, ACH…)"
            style={{ maxWidth: "9rem" }}
            onChange={(e) => setF({ ...f, form: e.target.value })} />
          <button disabled={busy || !f.amount} onClick={add}>record</button>
        </div>
      )}
    </>
  );
}

const BUCKETS: [string, string][] = [
  ["operating", "Operating"],
  ["organizational", "Organizational (§248)"],
  ["startup_195", "Start-up (§195)"],
];

function BooksSection({ entityId, readOnly }:
  { entityId: string; readOnly: boolean }) {
  const qc = useQueryClient();
  const now = new Date();
  const [year, setYear] = useState<number | undefined>(now.getFullYear());
  const pnl = useQuery({ queryKey: ["pnl", entityId, year ?? "all"],
    queryFn: () => api.entityPnl(entityId, year) });
  // the same key the Overview and the review queue use, so a decision made
  // on either tab refreshes this list too — spelled differently it never
  // would
  const txns = useQuery({ queryKey: ["biz-txns", entityId, year ?? "all"],
    queryFn: () => api.bizTxns(entityId, year) });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["pnl", entityId] });
    qc.invalidateQueries({ queryKey: ["biz-txns", entityId] });
  };
  // the select is bound to the cached row, so it snaps back to the old
  // bucket until the list refetches — write the choice into the row first;
  // a failure refetches the truth back
  const setBucket = (txnId: string, bucket: string) => {
    patchList<{ transactions: BizTxn[] }, BizTxn>(
      qc, ["biz-txns", entityId, year ?? "all"], "transactions",
      (t) => t.id === txnId, { bucket });
    api.bizClassify(entityId, txnId, { bucket }).then(refresh).catch((e) => {
      qc.invalidateQueries({ queryKey: ["biz-txns", entityId] });
      window.dispatchEvent(new CustomEvent("oiko-toast", { detail: errText(e) }));
    });
  };
  const p = pnl.data;
  const years = [now.getFullYear(), now.getFullYear() - 1,
                 now.getFullYear() - 2];
  const csv = `/api/business/entities/${entityId}/pnl.csv` +
    (year ? `?year=${year}` : "");
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: ".5rem",
        marginTop: ".6rem", flexWrap: "wrap" }}>
        <h4 style={{ margin: 0 }}>Books &amp; P&amp;L</h4>
        <select value={year ?? ""} onChange={(e) =>
          setYear(e.target.value ? Number(e.target.value) : undefined)}>
          <option value="">all years</option>
          {years.map(y => <option key={y} value={y}>{y}</option>)}
        </select>
        <a className="btn" href={csv} style={{ marginLeft: "auto" }}>
          download CSV</a>
      </div>
      {p && (
        <div className="mut" style={{ fontSize: ".9rem", margin: ".3rem 0" }}>
          <span>Revenue <b className="pos">{money(p.revenue)}</b></span>
          {"  −  "}
          <span>Operating <b>{money(p.operating_expenses)}</b></span>
          {"  =  "}
          <span>Net <b className={p.net_operating >= 0 ? "pos" : "bad"}>
            {money(p.net_operating)}</b></span>
          {(p.organizational > 0 || p.startup_195 > 0) && (
            <div style={{ marginTop: ".25rem" }}>
              {p.organizational > 0 && <>Organizational (§248){" "}
                {money(p.organizational)} · {money(
                  p.organizational_deduction.immediate)} deductible now. </>}
              {p.startup_195 > 0 && <>Start-up (§195) {money(p.startup_195)} ·{" "}
                {money(p.startup_deduction.immediate)} now,{" "}
                {money(p.startup_deduction.amortizable)} over 180 mo.</>}
              <div style={{ fontSize: ".82rem" }}>
                Estimates — confirm the tax treatment with your CPA.</div>
            </div>
          )}
        </div>
      )}
      {(txns.data?.transactions || []).some(t => t.amount > 0) && (
        <>
        <p className="mut" style={{ fontSize: ".82rem", margin: ".2rem 0" }}>
          Expenses only — classify each into a bucket. Revenue is summed into
          the figure above.</p>
        <table style={{ fontSize: ".9rem" }}><thead>
          <tr><th style={{ width: "1%" }}>Date</th><th>Payee</th>
            <th className="num" style={{ width: "1%" }}>Amount</th>
            <th style={{ width: "1%" }}>Bucket</th></tr>
        </thead><tbody>
          {(txns.data?.transactions || []).filter(t => t.amount > 0).map(t => (
            <tr key={t.id}>
              <td className="mut" style={{ whiteSpace: "nowrap" }}>
                {t.date ? mmdd(t.date) : ""}</td>
              <td>{t.payee}{t.note && <div className="mut" style={{ fontSize: 11,
                fontStyle: "italic" }}>📝 {t.note}</div>}</td>
              <td className="num">{money(t.amount)}</td>
              <td>{readOnly ? (t.bucket || "operating") : (
                <select value={t.bucket || "operating"}
                  onChange={(e) => setBucket(t.id, e.target.value)}>
                  {BUCKETS.map(([v, l]) =>
                    <option key={v} value={v}>{l}</option>)}
                </select>)}</td>
            </tr>
          ))}
        </tbody></table>
        </>
      )}
    </>
  );
}

function TaxSection({ entity, readOnly }:
  { entity: Entity; readOnly: boolean }) {
  const qc = useQueryClient();
  const est = useQuery({ queryKey: ["esttax", entity.id],
    queryFn: () => api.estTax(entity.id) });
  const [rate, setRate] = useState(
    entity.income_tax_rate != null ? String(entity.income_tax_rate) : "");
  const [sqft, setSqft] = useState(
    entity.home_office_sqft != null ? String(entity.home_office_sqft) : "");
  const [busy, run] = useBusy();
  // the rate lives on the entity, which the summary serves — write the
  // saved entity into both caches so nothing reads the old rate back
  const save = () => run(api.entityUpdate(entity.id, {
    income_tax_rate: rate ? Number(rate) : null,
    home_office_sqft: sqft ? Number(sqft) : null }).then((saved) => {
      patchList<BusinessSummary, Entity>(qc, ["biz-summary"], "entities",
        (x) => x.id === entity.id,
        { income_tax_rate: saved.income_tax_rate,
          home_office_sqft: saved.home_office_sqft });
      qc.setQueryData<Entity>(["entity", entity.id], (old) =>
        old && { ...old, ...saved });
      qc.invalidateQueries({ queryKey: ["esttax", entity.id] });
      qc.invalidateQueries({ queryKey: ["biz-summary"] });
    }).catch((e) => window.dispatchEvent(
      new CustomEvent("oiko-toast", { detail: errText(e) }))));
  const e = est.data;
  return (
    <>
      <h4 style={{ marginBottom: ".3rem" }}>Estimated taxes</h4>
      {e && (
        <p className="mut" style={{ marginTop: 0, fontSize: ".9rem" }}>
          On {money(e.net_profit)} net{e.mileage_deduction > 0 &&
            <> − {money(e.mileage_deduction)} mileage</>}
          {e.home_office_deduction > 0 &&
            <> − {money(e.home_office_deduction)} home office</>}
          {" = "}{money(e.taxable_profit)} taxable ·{" "}
          SE tax <b>{money(e.se_tax)}</b>
          {e.income_tax != null && <> + income tax <b>{money(e.income_tax)}</b></>}
          {" → "}<b className="pos">{money(e.total_estimated)}</b>/yr,{" "}
          <b>{money(e.quarterly)}</b> per quarter.
          <span style={{ display: "block", fontSize: ".82rem" }}>
            Estimate — set aside quarterly; confirm with your CPA.
            {e.income_tax == null && " Set an income-tax rate below to include income tax."}
          </span>
        </p>
      )}
      {!readOnly && (
        <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap",
          alignItems: "center", fontSize: ".9rem" }}>
          <label className="mut">income-tax rate %
            <input type="number" value={rate} style={{ width: "5rem" }}
              onChange={(ev) => setRate(ev.target.value)} /></label>
          <label className="mut">home-office sq ft
            <input type="number" value={sqft} style={{ width: "5rem" }}
              onChange={(ev) => setSqft(ev.target.value)} /></label>
          <button onClick={save} disabled={busy}>save</button>
        </div>
      )}
    </>
  );
}


// IRS mileage rates are quoted in cents (72.5¢), and a year can carry two of
// them (2026 changed on Jul 1), so the summary names each rate that applied
// rather than a rounded blend that matches no trip.
const cents = (r: number) =>
  `${(r * 100).toFixed(1).replace(/\.0$/, "")}¢`;
function mileageRateLabel(s: { rate: number;
                               rates?: { rate: number; miles: number }[] }) {
  const rs = s.rates ?? [];
  if (rs.length > 1)
    return `(${rs.map((x) => `${x.miles} mi at ${cents(x.rate)}`).join(", ")})`;
  return `× ${cents(s.rate)}/mi`;
}

function MileageSection({ entityId, readOnly }:
  { entityId: string; readOnly: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["mileage", entityId],
    queryFn: () => api.mileageList(entityId) });
  const [f, setF] = useState({ date: new Date().toISOString().slice(0, 10),
    miles: "", purpose: "" });
  const refresh = () => qc.invalidateQueries({ queryKey: ["mileage", entityId] });
  const toast = (e: unknown) => window.dispatchEvent(
    new CustomEvent("oiko-toast", { detail: errText(e) }));
  const [busy, run] = useBusy();
  const add = () => run(api.mileageAdd(entityId, { date: f.date,
    miles: Number(f.miles), purpose: f.purpose || undefined }).then(() => {
      setF({ ...f, miles: "", purpose: "" }); refresh();
    }).catch(toast));
  // one DELETE per click; the trip leaves the list as the server agrees
  const [deleting, setDeleting] = useState<string | null>(null);
  const del = (id: string) => {
    setDeleting(id);
    api.mileageDelete(entityId, id).then(() => {
      removeFromList<{ trips: MileageTrip[] }, MileageTrip>(
        qc, ["mileage", entityId], "trips", (t) => t.id === id);
      refresh();
    }).catch(toast).finally(() => setDeleting(null));
  };
  const s = q.data?.summary;
  return (
    <>
      <h4 style={{ marginBottom: ".3rem" }}>Mileage</h4>
      {s && <p className="mut" style={{ marginTop: 0, fontSize: ".9rem" }}>
        {s.miles} mi {mileageRateLabel(s)} ={" "}
        <b className="pos">{money(s.deduction)}</b> deduction
        {s.year ? ` (${s.year})` : ""}.</p>}
      {(q.data?.trips || []).slice(0, 6).map(t => (
        <div key={t.id} className="mut" style={{ fontSize: ".85rem",
          display: "flex", gap: ".5rem" }}>
          <span>{t.date ? mmdd(t.date) : ""}</span>
          <span>{t.miles} mi</span>
          <span style={{ flex: 1 }}>{t.purpose || ""}</span>
          {!readOnly && <button title="delete" disabled={deleting === t.id}
            onClick={() => confirmThen(
            "Delete this mileage trip?", () => del(t.id))}>✕
          </button>}
        </div>
      ))}
      {(q.data?.trips || []).length > 6 && (
        <div className="mut" style={{ fontSize: ".8rem" }}>
          showing the 6 most recent of {(q.data?.trips || []).length} trips
          (the deduction total covers all of this year's).</div>
      )}
      {!readOnly && (
        <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap",
          marginTop: ".3rem" }}>
          <input type="date" value={f.date}
            onChange={(e) => setF({ ...f, date: e.target.value })} />
          <input type="number" value={f.miles} placeholder="miles"
            style={{ maxWidth: "6rem" }}
            onChange={(e) => setF({ ...f, miles: e.target.value })} />
          <input value={f.purpose} placeholder="purpose"
            style={{ maxWidth: "10rem" }}
            onChange={(e) => setF({ ...f, purpose: e.target.value })} />
          <button disabled={busy || !f.miles} onClick={add}>log trip</button>
        </div>
      )}
    </>
  );
}

function BalanceSheetSection({ entityId }: { entityId: string }) {
  const q = useQuery({ queryKey: ["balancesheet", entityId],
    queryFn: () => api.balanceSheet(entityId) });
  const b = q.data;
  if (!b) return null;
  const empty = b.assets.length === 0 && b.liabilities.length === 0;
  return (
    <>
      <h4 style={{ marginBottom: ".3rem" }}>Balance sheet</h4>
      {empty ? (
        <p className="mut" style={{ marginTop: 0, fontSize: ".9rem" }}>
          Assign a business bank or credit account above and its balances show
          up here as assets and liabilities.</p>
      ) : (
      <p className="mut" style={{ marginTop: 0, fontSize: ".9rem" }}>
        Assets <b>{money(b.total_assets)}</b> − Liabilities{" "}
        <b>{money(b.total_liabilities)}</b> = Net{" "}
        <b className={b.net >= 0 ? "pos" : "bad"}>{money(b.net)}</b>
        {"  ·  "}Capital account {money(b.capital_account)}
        <span style={{ display: "block", fontSize: ".82rem" }}>
          Snapshot from business account balances — not double-entry books.</span>
      </p>
      )}
    </>
  );
}

function VendorSection({ entityId, readOnly }:
  { entityId: string; readOnly: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["vendors", entityId],
    queryFn: () => api.vendorList(entityId) });
  // the checkbox is bound to the cached row, so it snaps back until the
  // list refetches — flip it first; the refetch settles needs_1099, and a
  // failure refetches the truth back
  const mark = (merchant: string, reportable: boolean) => {
    patchList<{ vendors: Vendor[] }, Vendor>(qc, ["vendors", entityId],
      "vendors", (v) => v.merchant === merchant, { reportable });
    api.vendorMark(entityId, { merchant, reportable }).then(() =>
      qc.invalidateQueries({ queryKey: ["vendors", entityId] })).catch((e) => {
      qc.invalidateQueries({ queryKey: ["vendors", entityId] });
      window.dispatchEvent(new CustomEvent("oiko-toast", { detail: errText(e) }));
    });
  };
  const all = q.data?.vendors || [];
  const vendors = all.slice(0, 12);
  return (
    <>
      <h4 style={{ marginBottom: ".3rem" }}>1099 contractors</h4>
      <p className="mut" style={{ marginTop: 0, fontSize: ".85rem" }}>
        Mark who's a contractor — those paid $600+ this year need a 1099-NEC.
        {all.length === 0 && " No business payees yet — assign an account or "
          + "transactions to this entity first."}</p>
      <table style={{ fontSize: ".9rem" }}><tbody>
        {vendors.map(v => (
          <tr key={v.merchant}>
            <td>{v.merchant}</td>
            <td className="num">{money(v.paid)}</td>
            <td style={{ width: "1%", whiteSpace: "nowrap" }}>
              {v.needs_1099 && <span className="pill b">1099</span>}</td>
            <td style={{ width: "1%" }}>
              <label className="mut" style={{ fontSize: ".82rem" }}>
                <input type="checkbox" checked={v.reportable} disabled={readOnly}
                  onChange={(e) => mark(v.merchant, e.target.checked)} />{" "}
                contractor</label></td>
          </tr>
        ))}
      </tbody></table>
      {all.length > 12 && (
        <div className="mut" style={{ fontSize: ".8rem" }}>
          showing the top 12 of {all.length} payees by amount — the books CSV
          export lists every vendor.</div>
      )}
    </>
  );
}

function ComplianceSection({ entityId, readOnly }:
  { entityId: string; readOnly: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["compliance", entityId],
    queryFn: () => api.entityCompliance(entityId) });
  const [f, setF] = useState({ title: "", due_date: "", recurrence: "yearly" });
  const refresh = () =>
    qc.invalidateQueries({ queryKey: ["compliance", entityId] });
  const toast = (e: unknown) => window.dispatchEvent(
    new CustomEvent("oiko-toast", { detail: errText(e) }));
  const [busy, run] = useBusy();
  const add = () => run(api.complianceAdd(entityId, { title: f.title.trim(),
    due_date: f.due_date, recurrence: f.recurrence }).then(() => {
      setF({ title: "", due_date: "", recurrence: f.recurrence }); refresh();
    }).catch(toast));
  // one DELETE per click; the row leaves the calendar as the server agrees
  const [deleting, setDeleting] = useState<string | null>(null);
  const del = (id: string) => {
    setDeleting(id);
    api.complianceDelete(entityId, id).then(() => {
      removeFromList<{ obligations: Obligation[] }, Obligation>(
        qc, ["compliance", entityId], "obligations", (o) => o.id === id);
      refresh();
    }).catch(toast).finally(() => setDeleting(null));
  };
  const obls = q.data?.obligations || [];
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: ".5rem",
        marginTop: ".6rem" }}>
        <h4 style={{ margin: 0 }}>Compliance calendar</h4>
        {obls.length > 0 && <a className="btn"
          href={`/api/business/entities/${entityId}/compliance.ics`}
          style={{ marginLeft: "auto" }}>add to calendar (.ics)</a>}
      </div>
      {obls.length === 0
        ? <p className="mut" style={{ marginTop: ".2rem" }}>No obligations
          tracked{" "}— add a filing deadline below.</p>
        : (
        <table style={{ fontSize: ".9rem" }}><tbody>
          {obls.map((o, i) => (
            <tr key={o.id || i}>
              <td className="mut" style={{ whiteSpace: "nowrap" }}>
                {mmdd(o.due)}</td>
              {/* the server refuses anything but http/https on write; this
                  is the second half of the same guard, for rows written
                  before it existed */}
              <td>{/^https?:\/\//i.test(o.url ?? "")
                ? <a href={o.url!} target="_blank"
                rel="noopener noreferrer">{o.title}</a> : o.title}
                {o.fee === 0 ? <span className="mut"> · free</span>
                  : o.fee ? <span className="mut"> · {money(o.fee)}</span> : ""}
              </td>
              <td style={{ width: "1%" }}>{!readOnly && o.id &&
                <button title="remove" disabled={deleting === o.id}
                  onClick={() => confirmThen(
                  `Remove "${o.title}" from the compliance calendar?`,
                  () => del(o.id!))}>✕</button>}</td>
            </tr>
          ))}
        </tbody></table>
      )}
      {!readOnly && (
        <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap",
          marginTop: ".3rem" }}>
          <input value={f.title} placeholder="obligation (e.g. franchise tax)"
            style={{ maxWidth: "16rem" }}
            onChange={(e) => setF({ ...f, title: e.target.value })} />
          <input type="date" value={f.due_date}
            onChange={(e) => setF({ ...f, due_date: e.target.value })} />
          <select value={f.recurrence}
            onChange={(e) => setF({ ...f, recurrence: e.target.value })}>
            <option value="yearly">every year</option>
            <option value="once">one-time</option>
          </select>
          <button disabled={busy || !f.title.trim() || !f.due_date}
            onClick={add}>add</button>
        </div>
      )}
    </>
  );
}

function AddMember({ entityId, onDone }:
  { entityId: string; onDone: () => void }) {
  const [name, setName] = useState("");
  const [pct, setPct] = useState("");
  const [busy, run] = useBusy();
  const add = () => run(api.entityAddMember(entityId, { member_name: name.trim(),
    ownership_pct: pct ? Number(pct) : undefined }).then(() => {
      setName(""); setPct(""); onDone();
    }).catch((e) => window.dispatchEvent(
      new CustomEvent("oiko-toast", { detail: errText(e) }))));
  return (
    <div style={{ display: "flex", gap: ".4rem", marginTop: ".4rem",
      flexWrap: "wrap" }}>
      <input value={name} onChange={(e) => setName(e.target.value)}
        placeholder="member name" style={{ maxWidth: "12rem" }} />
      <input value={pct} onChange={(e) => setPct(e.target.value)}
        placeholder="%" type="number" style={{ maxWidth: "5rem" }} />
      <button disabled={busy || !name.trim()} onClick={add}>add member</button>
    </div>
  );
}

// Permanent destruction — the made-this-by-mistake case only; archiving is
// the normal way to remove a business. The typed exact name mirrors the
// account-erasure confirm, and the server enforces the same match, so this
// gate can't be skipped by a hand-rolled request. The copy states exactly
// what dies (per-table counts from the server) and what merely detaches.
function DeleteForeverCard({ entity, onDone, onCancel }:
  { entity: Entity; onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [busy, run] = useBusy();
  const impact = useQuery({ queryKey: ["entity-impact", entity.id],
    queryFn: () => api.entityDeleteImpact(entity.id) });
  const d = impact.data;
  const n = (x: number, w: string) => `${x} ${w}${x === 1 ? "" : "s"}`;
  const destroyed = d ? [
    n(d.destroyed.equity_movements, "equity movement"),
    n(d.destroyed.mileage_trips, "mileage trip"),
    n(d.destroyed.compliance_obligations, "compliance obligation"),
    n(d.destroyed.vendors_1099, "1099 vendor"),
    n(d.destroyed.members, "member"),
  ].join(", ") : "…";
  // Nobody deletes irreversibly on a placeholder. The whole feature is
  // "see what this destroys BEFORE you type the name", so a counts request
  // that is still in flight — or that failed — blocks the button instead
  // of quietly leaving a "…" in the sentence.
  const ready = impact.isSuccess;
  return (
    <div className="card" style={{ borderColor: "rgba(224,105,93,.45)" }}>
      <h4 style={{ color: "var(--red)", marginTop: 0 }}>
        Delete {entity.name} forever</h4>
      <p className="mut" style={{ marginTop: 0 }}>
        This permanently destroys the entity and its records — {destroyed}.
        {d ? ` ${n(d.detached.accounts, "account")} and
          ${n(d.detached.transactions, "transaction")} return to personal —
          no transaction is deleted.` : ""} There is no undo. If you might
        ever need these books again, archive instead: archiving keeps
        everything.</p>
      {impact.isError && (
        <p className="bad" style={{ marginTop: 0 }}>
          Couldn't load what this would destroy — {String(impact.error)}.
          Deleting is blocked until it loads.{" "}
          <button onClick={() => impact.refetch()}>try again</button></p>
      )}
      <label className="mut" style={{ display: "block",
                                      maxWidth: "22rem" }}>
        Type <b>{entity.name}</b> to confirm
        <input value={name} placeholder={entity.name}
               style={{ display: "block", width: "100%",
                        marginTop: ".2rem" }}
               onChange={(e) => setName(e.target.value)} /></label>
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".7rem" }}>
        <button className="pri" style={{ background: "var(--red)" }}
                disabled={busy || !ready || name !== entity.name}
                title={ready ? "" : "waiting for what this would destroy"}
                onClick={() => run(api.entityDeleteForever(entity.id, name)
                  .then(onDone).catch(toastErr))}>
          {busy ? "deleting…" : "Delete forever"}</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function EntitiesSection({ tab }: { tab: BizTab }) {
  const qc = useQueryClient();
  const [sel, setSel] = useState<string | null>(null);
  // which entity has the delete-forever confirm open (archived list)
  const [nuking, setNuking] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  // guided setup — what the empty state opens, instead of the flat
  // six-field form (which stays for adding a SECOND entity)
  const [wiz, setWiz] = useState(
    new URLSearchParams(window.location.search).get("wizard") === "1");
  // needed to tell whether guided setup is FINISHED (an entity with at
  // least one assigned account), or the prompt shows forever
  const acctsQ = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const sum = useQuery({ queryKey: ["biz-summary"],
    queryFn: api.businessSummary });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["biz-summary"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["biz"] });
  };
  if (sum.isPending) return null;
  if (sum.isError)
    return <div className="card">Couldn't load: {String(sum.error)}</div>;
  const s = sum.data;
  // an archived business is out of active use but never out of reach: it
  // keeps its books readable and restorable, so it lives in a collapsed
  // Archived list (the Bills pattern), not in the working switcher
  const actives = s.entities.filter(e => e.status === "active");
  const archived = s.entities.filter(e => e.status !== "active");
  const entity = s.entities.find(e => e.id === sel)
    || (actives.length === 1 ? actives[0] : null)
    || (s.entities.length === 1 ? s.entities[0] : null);

  return (
    <>
      {/* for the single-entity household — the overwhelming default
          — the hero IS the business: who it is, then its numbers, then the
          tabs. The management chrome (heading, add button, a permanent
          explainer paragraph, setup links) lives in Settings rather than
          above every tab, because setup is something you finish, not a place
          you work. That is this page's own doctrine, applied to its own
          furniture. */}
      {actives.length > 1 && (
        <div style={{ display: "flex", gap: ".4rem", marginBottom: ".5rem",
                      flexWrap: "wrap" }}>
          {actives.map(e => (
            <button key={e.id} className={entity?.id === e.id ? "pri" : ""}
              onClick={() => setSel(e.id)}>{e.name}</button>
          ))}
        </div>
      )}

      {adding && <AddEntity
        onDone={(id) => { setAdding(false); if (id) setSel(id); refresh(); }}
        onCancel={() => setAdding(false)} />}

      {s.entities.length === 0 && !adding && !wiz && (
        <div className="card" style={{ marginTop: 0 }}>
          <p className="sub" style={{ marginTop: 0 }}>No businesses yet — set
            one up and its money stays out of your personal budget, cash flow
            and net worth.</p>
          {/* creating an entity is an editor's act — mobile gates this the
              same way; a viewer got a wizard whose first write 403s */}
          {canEdit(meQ) && (
            <Link to="/business/setup"><button className="pri">
              Set up a business</button></Link>
          )}
        </div>
      )}
      {wiz && (
        <BusinessWizard
          resume={entity ? { id: entity.id, name: entity.name,
                             business_start_date: entity.business_start_date }
                         : undefined}
          onDone={(id: string) => { setWiz(false); setSel(id); refresh(); }}
          onCancel={() => setWiz(false)} />
      )}

      {entity && !wiz && !(acctsQ.data?.accounts || [])
        .some((a) => a.entity_id === entity.id) && (
        <p className="sub" style={{ marginTop: ".6rem" }}>
          <Link to="/business/setup">Continue guided setup</Link>{" "}
          — assign accounts and scan for startup costs.
        </p>
      )}

      {entity && <EntityDetail entity={entity} tab={tab}
        combined={s.combined} onCombine={(v) => {
          // the checkbox is bound to the cached summary — flip it first
          // (a failure refetches the truth back). Combined view changes
          // what every money page shows, so those refetch too; the
          // summary and the worksheet merely confirm.
          patchQuery<BusinessSummary>(qc, ["biz-summary"], { combined: v });
          api.businessCombine(v).then(() => {
            refresh();
            for (const k of ["today", "txns", "report", "lens-month",
                             "lens-year"])
              qc.invalidateQueries({ queryKey: [k] });
          }).catch((e) => {
            qc.invalidateQueries({ queryKey: ["biz-summary"] });
            toastErr(e);
          });
        }}
        onAdd={() => setAdding(true)}
        flaggedUnassigned={s.flagged_unassigned} onChange={refresh} />}

      {archived.length > 0 && (
        <details className="card" style={{ marginTop: "1rem" }}>
          <summary style={{ cursor: "pointer", color: "var(--mut)" }}>
            Archived — {archived.length} business
            {archived.length > 1 ? "es" : ""} (records kept)
          </summary>
          <table style={{ marginTop: ".5rem" }}><tbody>
            {archived.map(e => (
              <tr key={e.id}>
                <td>{e.name} <span className="mut">
                  {structLabel(e.structure)}
                  {e.archived_at
                    ? ` · archived ${mmdd(e.archived_at)}` : ""}</span></td>
                <td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <button onClick={() => setSel(e.id)}>view</button>{" "}
                  {canEdit(meQ) && (
                    <button onClick={() => api.entityRestore(e.id)
                      .then(() => { setSel(e.id); refresh(); })
                      .catch(toastErr)}>restore</button>
                  )}{" "}
                  {/* delete forever is owner-only — it irreversibly
                      destroys the entity's tax records; the server gates it
                      with _owner_only, so a member never sees the control */}
                  {isOwner(meQ) && (
                    <button title="permanent — asks for the exact name"
                      onClick={() => setNuking(
                        nuking === e.id ? null : e.id)}>
                      delete forever…</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody></table>
          {nuking && isOwner(meQ) && archived.some(e => e.id === nuking) && (
            <DeleteForeverCard
              entity={archived.find(e => e.id === nuking)!}
              onDone={() => { setNuking(null);
                if (sel === nuking) setSel(null); refresh(); }}
              onCancel={() => setNuking(null)} />
          )}
        </details>
      )}
    </>
  );
}


// tagged receipt line items → the itemized expense report
function ExpenseReportCard() {
  const now = new Date();
  const [tag, setTag] = useState("business");
  const [y, setY] = useState(now.getFullYear());
  const [m, setM] = useState(now.getMonth() + 1);
  const q = useQuery({
    queryKey: ["receipt-report", tag, y, m],
    queryFn: () => api.receiptReport(tag, y, m),
  });
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Itemized expenses (receipt line items)</h2>
      <p className="mut">Line items tagged on receipts (🧾 on any
        transaction). Tag lines “business” and this becomes the itemized
        Schedule-C companion to the transaction-level list above.</p>
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center",
                    flexWrap: "wrap" }}>
        <input value={tag} onChange={(e) => setTag(e.target.value)}
          style={{ width: "9rem" }} placeholder="tag" />
        <input type="number" value={y} style={{ width: "6rem" }}
          onChange={(e) => setY(Number(e.target.value))} />
        <select value={m} onChange={(e) => setM(Number(e.target.value))}>
          {Array.from({ length: 12 }, (_, i) => (
            <option key={i + 1} value={i + 1}>{i + 1}</option>))}
        </select>
        <a className="btn"
           href={`/api/receipts/report?tag=${encodeURIComponent(tag)}&y=${y}&m=${m}&format=csv`}>
          download CSV</a>
      </div>
      {q.data && (
        q.data.count === 0
          ? <p className="mut" style={{ marginBottom: 0 }}>No “{tag}” line
              items in {m}/{y}.</p>
          : <>
              <table style={{ fontSize: 13, marginTop: ".6rem" }}>
                <thead><tr><th>Date</th><th>Payee</th><th>Item</th>
                  <th className="num">Amount</th><th></th></tr></thead>
                <tbody>
                  {q.data.rows.map((r, i) => (
                    <tr key={i}>
                      <td>{mmdd(r.date)}</td><td>{r.payee}</td>
                      <td>{r.description}</td>
                      <td className="num">${r.amount.toFixed(2)}</td>
                      <td><a href={`/api/receipts/${r.receipt_id}/image`}
                             target="_blank" rel="noreferrer">receipt</a></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p style={{ marginBottom: 0 }}><b>Total:
                ${q.data.total.toFixed(2)}</b> across {q.data.count} items</p>
            </>
      )}
    </div>
  );
}

export default function Business() {
  const qc = useQueryClient();
  const [receiptFor, setReceiptFor] = useState<string | null>(null);
  const [noteFor, setNoteFor] = useState<string | null>(null);
  const [year, setYear] = useState<number | undefined>(undefined);
  // the server caps /api/business at 500 rows by default (1000 max) while
  // `count` spans the whole scope — the worksheet must say so and offer
  // the rest rather than silently dropping the oldest rows
  const [limit, setLimit] = useState(500);
  // in the URL so a tab is linkable and survives a reload — the same reason
  // the wizards stopped keeping their position in React state
  const [sp, setSp] = useSearchParams();
  const tab = (BIZ_TAB_KEYS.includes(sp.get("tab") as BizTab)
               ? sp.get("tab") : "overview") as BizTab;
  const setTab = (v: BizTab) => {
    if (v === "overview") sp.delete("tab"); else sp.set("tab", v);
    setSp(sp, { replace: true });
  };
  const q = useQuery({
    queryKey: ["biz", year ?? "all", limit],
    queryFn: () => api.bizList(year, limit),
  });
  // Unflagging is an editing write — viewers keep the worksheet view
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  // one request per click, and the row leaves the worksheet as the server
  // agrees — the list is up to a thousand rows, and waiting for it to
  // refetch leaves the unflagged row sitting there with a live button
  const [unflagging, setUnflagging] = useState<string | null>(null);
  const unflag = (id: string) => {
    setUnflagging(id);
    api.bizFlag(id, false).then(() => {
      qc.setQueryData<BizData>(["biz", year ?? "all", limit], (old) => {
        if (!old) return old;
        const gone = old.rows.find((r) => r.id === id);
        return { ...old, rows: old.rows.filter((r) => r.id !== id),
                 count: Math.max(0, old.count - (gone ? 1 : 0)),
                 total: gone ? old.total - gone.amount : old.total };
      });
      qc.invalidateQueries({ queryKey: ["biz"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
      // the flag keeps the row out of personal spend, so the verdict moves
      qc.invalidateQueries({ queryKey: ["today"] });
    }).catch((e) => window.dispatchEvent(
      new CustomEvent("oiko-toast", { detail: `Unflag failed: ${errText(e)}` })))
      .finally(() => setUnflagging(null));
  };

  // No entitlement check here: the page's own "No businesses yet" empty
  // state below is what a tenant without one sees, and it points at the
  // wizard.

  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't load: {String(q.error)}</div>;

  const d = q.data;
  const exportHref = "/api/business/export.csv" + (year ? `?year=${year}` : "");

  return (
    <>
      <h1>Business</h1>
      <BizNav tab={tab} onTab={setTab} />
      <EntitiesSection tab={tab} />
      {tab === "transactions" && <div className="card" style={{ marginTop: "1rem" }}>
        <div style={{ display: "flex", gap: ".8rem", alignItems: "baseline", flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Flagged on personal cards</h2>
          <span className="sub">the biz tag from Transactions — separate from
            the entity's own books; import them to merge</span>
          <select style={{ marginLeft: "auto" }} value={year ?? ""} onChange={(e) =>
            setYear(e.target.value ? Number(e.target.value) : undefined)}>
            <option value="">all years</option>
            {d.years.map((y) => <option key={y} value={y}>{y}</option>)}
          </select>
          <a className="btn" href={exportHref}>download CSV</a>
          <Link className="btn" to="/transactions">‹ transactions</Link>
        </div>
        <div style={{ marginTop: ".6rem", fontSize: "1.15rem" }}>
          <b>{money(d.total)}</b>
          <span className="mut"> across {d.count} transaction{d.count === 1 ? "" : "s"}
            {year ? ` in ${year}` : ""}</span>
        </div>
        {d.rows.length === 0 && (
          <p className="sub" style={{ marginBottom: 0 }}>Nothing flagged
            {year ? ` in ${year}` : ""} — hit the <b>biz</b> button on any row
            of the Transactions page.</p>
        )}
        {d.rows.length > 0 && (
          // txn-table + per-td c-* classes let index.css restack
          // each row into a two-line card grid at <=620px, matching the main
          // Transactions ledger — desktop rendering unchanged
          <table className="txn-table" style={{ marginTop: ".5rem" }}>
            <thead>
              <tr><th style={{ width: "1%", whiteSpace: "nowrap" }}>Date</th>
                  <th className="num" style={{ width: "1%" }}>Amount</th>
                  <th>Merchant</th><th>Category</th>
                  <th className="hide-m">Account</th>
                  <th style={{ width: "1%" }}></th></tr>
            </thead>
            <tbody>
              {d.rows.flatMap((r) => {
                const row = (
                <tr key={r.id}>
                  <td className="mut c-date" style={{ whiteSpace: "nowrap" }}>{mmdd(r.date)}</td>
                  {/* Schedule C worksheet framing: expenses positive, refunds
                      negative (net against the total) — same sign as the CSV */}
                  <td className={`num c-amt ${r.amount < 0 ? "pos" : ""}`}
                      style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                  <td className="c-merch">{r.payee}
                    {r.pending ? <span className="pill m" style={{ marginLeft: 6 }}>pend</span> : null}
                    {r.note && noteFor !== r.id ? <div className="mut" style={{ fontSize: 11,
                      fontStyle: "italic", whiteSpace: "normal" }}>📝 {r.note}</div> : null}</td>
                  <td className="mut c-cat" style={{ whiteSpace: "nowrap" }}>
                    {catLabel(r.category || "")}</td>
                  <td className="mut hide-m c-acct" style={{ whiteSpace: "nowrap", maxWidth: "15rem",
                      overflow: "hidden", textOverflow: "ellipsis" }}>{r.account ?? ""}</td>
                  <td className="c-clip" style={{ whiteSpace: "nowrap" }}>
                    {/* free-text note — owners edit, filled 📝 = a note exists */}
                    {mayEdit && (
                    <button className="clip-btn" aria-label="add or edit a note"
                            title={r.note ? `note: ${r.note}` : "add a note"}
                            style={{ opacity: r.note ? 1 : 0.3,
                                     color: r.note ? "var(--blue)" : undefined }}
                            onClick={() => setNoteFor(
                              noteFor === r.id ? null : r.id)}>
                      📝</button>
                    )}{" "}
                    <button className="clip-btn" title="receipts — attach or view"
                            style={receiptFor === r.id
                              ? {} : { opacity: 0.6 }}
                            onClick={() => setReceiptFor(
                              receiptFor === r.id ? null : r.id)}>
                      📎</button>{" "}
                    {mayEdit && (
                    <button title="remove the business flag"
                            disabled={unflagging === r.id}
                            onClick={() => unflag(r.id)}>
                      unflag</button>
                    )}
                  </td>
                </tr>
                );
                const extras = [];
                // tr-expand: opts the row OUT of the mobile card grid so the
                // editor / receipt panel spans full width on phones
                if (mayEdit && noteFor === r.id)
                  extras.push(
                    <tr className="tr-expand" key={r.id + "-note"}>
                      <td className="c-expand" colSpan={6}>
                        <NoteEditor txnId={r.id} initial={r.note || ""}
                          onDone={(note) => { setNoteFor(null);
                            // a note moves no money: the saved text goes
                            // onto the cached row instead of refetching
                            // the whole worksheet (cancel hands back no
                            // note and patches nothing); the ledger's
                            // copy of the row is only marked stale
                            if (note !== undefined)
                              qc.setQueryData<BizData>(
                                ["biz", year ?? "all", limit], (old) =>
                                  old && { ...old, rows: old.rows.map((x) =>
                                    x.id === r.id ? { ...x, note } : x) });
                            qc.invalidateQueries({ queryKey: ["txns"] }); }} />
                      </td></tr>);
                if (receiptFor === r.id)
                  extras.push(
                    <tr className="tr-expand" key={r.id + "-rcpt"}>
                      <td className="c-expand" colSpan={6}>
                        <ReceiptPanel txnId={r.id} /></td></tr>);
                return [row, ...extras];
              })}
            </tbody>
          </table>
        )}
        {d.count > d.rows.length && (
          limit < 1000
            ? <button className="btn" style={{ marginTop: ".5rem" }}
                onClick={() => setLimit(1000)}>
                show more ({d.rows.length} of {d.count})
              </button>
            : <p className="sub" style={{ marginTop: ".5rem", marginBottom: 0 }}>
                Showing the newest {d.rows.length} of {d.count} — the CSV
                download lists the whole worksheet.
              </p>
        )}
      </div>}
      {tab === "log" && <ExpenseReportCard />}
    </>
  );
}
