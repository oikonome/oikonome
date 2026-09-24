// The ledger table — ONE renderer for every surface that shows
// transaction rows (the Transactions page, Today's recent pane), so the
// look, pills, and affordances cannot drift — the same doctrine as
// PlanBars/MoneyMap. Self-contained: recategorize, reimb
// flag/unflag, biz toggle, and the receipt panel all live here.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { flushSync } from "react-dom";
import { Link, useNavigate } from "react-router";
import ReceiptPanel from "./ReceiptPanel";
import SplitPanel from "./SplitPanel";
import { api, errText, mmdd, money as money$, type Entity, type Txn, catLabel } from "../api/client";
import { patchQueries } from "../api/cache";
import MerchantAvatar from "./MerchantAvatar";
import { isViewer } from "../role";
const money = (v: number | null | undefined) => money$(v, true);  // cents on transaction/recurring surfaces

// ---- day dividers -------------------------------------------------------
// A ledger reads as days, not as three hundred undifferentiated rows, so
// the Transactions page draws a divider wherever the date changes. The
// divider carries that day's spend, which is what makes it worth the row
// it costs: a bare rule only restates what the date column already says,
// while the total answers "what did that day cost me" without arithmetic.
//
// Spend follows one rule, not a sum of the amount column. Transfers and
// card payments move money between the user's own accounts and money in is
// not spending, so summing the column would print a number that disagrees
// with the verdict, the daily email and the Business worksheet — three
// surfaces that all already exclude exactly these.
//
// The server decides which rows count (counts_as_spend, computed from the
// same SQL the verdict uses) — the client must not re-derive spend
// semantics: the display category can't see overrides collapsing to
// transfers or loan-account rows, so any local rule diverges. The category
// fallback below exists only for a NEW client against an OLD server that
// doesn't send the field yet — without it every day would total $0.
const daySpend = (group: Txn[]) => group.reduce((sum, t) =>
  sum + ((typeof t.counts_as_spend === "boolean"
          ? t.counts_as_spend
          : t.amount > 0 && !(t.category || "").includes("TRANSFER"))
         ? t.amount : 0), 0);

// "Thu · Aug 13". The weekday is the reason this exists — MM/DD never says
// whether an expensive day was a Saturday, and the row's own date column
// already covers the digits. Parsed field by field on purpose:
// new Date("2026-08-13") is parsed as UTC and renders as the 12th
// everywhere west of Greenwich, which would mislabel every divider.
const dayLabel = (iso: string) => {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  const dt = new Date(y, m - 1, d);
  return `${dt.toLocaleDateString(undefined, { weekday: "short" })} · `
    + dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

// A pending authorization the bank never posted and never released. Card
// holds clear within days, so a row still pending after two weeks is
// stuck, not in flight — that is the line the retire door opens at. This
// only decides whether to OFFER the control; the server enforces the same
// rule and is the authority. Parsed field by field for the same reason
// dayLabel is: new Date("2026-08-13") is UTC and reads a day early west
// of Greenwich, which would offer the button a day too soon.
const STUCK_PENDING_DAYS = 14;
const stuckPending = (t: Txn) => {
  if (!t.pending) return false;
  const [y, m, d] = t.date.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return false;
  return Date.now() - new Date(y, m - 1, d).getTime()
    > STUCK_PENDING_DAYS * 86400000;
};

// inline free-text note editor for one transaction (expands under the row).
// Exported so the Business worksheet reuses the exact same editor.
// A save hands the stored note to onDone so the caller can show it without
// a refetch; cancel calls onDone with nothing.
export function NoteEditor({ txnId, initial, onDone }:
  { txnId: string; initial: string; onDone: (note?: string) => void }) {
  const [text, setText] = useState(initial);
  const [busy, setBusy] = useState(false);
  const save = () => {
    setBusy(true);
    api.setTxnNote(txnId, text)
      .then((r) => onDone(r.note))
      .catch((e) => window.dispatchEvent(new CustomEvent("oiko-toast",
        { detail: `Couldn't save the note — ${errText(e)}` })))
      .finally(() => setBusy(false));
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: ".4rem",
      padding: ".2rem 0" }}>
      <textarea value={text} autoFocus rows={2}
        placeholder="Add a note — anything you want to remember about this transaction"
        style={{ width: "100%", resize: "vertical", fontSize: 13 }}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save();
        }} />
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center" }}>
        <button className="pri" disabled={busy} onClick={save}>Save note</button>
        <button disabled={busy} onClick={() => onDone()}>Cancel</button>
        <span className="mut" style={{ fontSize: 11 }}>
          {initial ? "Clear the box and save to remove." : "⌘/Ctrl+Enter saves."}</span>
      </div>
    </div>
  );
}


// One ledger row (plus its expand rows), memoized: the table sits under a
// page that re-renders every time any of its half-dozen queries resolves,
// and under its own cats/me/business queries besides — without the memo
// every one of those passes re-rendered every row. With it, a row renders
// when ITS data or ITS open/selected/armed state changes, and the rest of
// the pass is prop comparison.
type RowProps = {
  t: Txn;
  viewer: boolean;
  showAccount: boolean;
  picked: boolean;
  armed: boolean;
  saving: boolean;
  bizPending: boolean;
  // decided by the table (it knows the role and the instance), so the
  // row only has to render the control
  canRetire: boolean;
  retiring: boolean;
  menuOpen: boolean;
  noteOpen: boolean;
  receiptOpen: boolean;
  splitOpen: boolean;
  activeEntities: Entity[];
  catGroups: ReactNode;
  // every category the pickers offer, so a split's custom name is known
  knownCats: Set<string>;
  onArm: (id: string) => void;
  onPick: (id: string) => void;
  onRecat: (id: string, category: string) => void;
  onMenu: (id: string) => void;
  onNote: (id: string) => void;
  onReceipt: (id: string) => void;
  onSplit: (id: string) => void;
  onBiz: (id: string, on: boolean) => void;
  onRetire: (id: string) => void;
  onNoteDone: (id: string, note?: string) => void;
};

/** Everything the aggregator knows about the row, plus WHY it has the
 *  category it has — under the row when its ⋯ opens. Facts only where a
 *  source supplied them; an imported row shows the bank text and the why. */
function TxnDetail({ t }: { t: Txn }) {
  const loc = [t.location_city, t.location_region].filter(Boolean).join(", ");
  const map = (t.location_lat != null && t.location_lon != null)
    ? `https://www.openstreetmap.org/?mlat=${t.location_lat}&mlon=${t.location_lon}#map=17/${t.location_lat}/${t.location_lon}`
    : null;
  const plaid = t.category_plaid
    ? catLabel(t.category_plaid).toLowerCase()
      + (t.category_plaid_detailed
         ? " · " + catLabel(t.category_plaid_detailed.replace(t.category_plaid + "_", "")).toLowerCase()
         : "")
    : null;
  const facts: [string, ReactNode][] = [];
  if (t.bank_text) facts.push(["bank text", <span style={{ fontFamily: "ui-monospace, monospace" }}>{t.bank_text}</span>]);
  if (loc || t.location_store) facts.push(["location", <>
    {loc}{map && <> · <a href={map} target="_blank" rel="noopener noreferrer">map</a></>}
    {t.location_store && <span className="mut"> · store {t.location_store}</span>}</>]);
  if (t.payment_channel || t.payment_processor) facts.push(["paid", <>
    {t.payment_channel}{t.payment_processor && <> · via {t.payment_processor}</>}</>]);
  if (t.check_number) facts.push(["check", `#${t.check_number}`]);
  if (t.authorized_date && t.authorized_date !== t.date)
    facts.push(["authorized", mmdd(t.authorized_date)]);
  if (t.mcc) facts.push(["merchant code", `MCC ${t.mcc}`]);
  if (plaid) facts.push(["Plaid says", <>
    {plaid}{t.category_plaid_confidence && <span className="pill m" style={{ marginLeft: 6 }}>
      {catLabel(t.category_plaid_confidence).toLowerCase()}</span>}</>]);
  if (!facts.length && !t.category_why) return null;
  return (
    <div className="txn-detail">
      {facts.length > 0 && (
        <div className="txn-facts">
          {facts.map(([k, v]) => (
            <div key={k}><span className="k">{k}</span><span className="v">{v}</span></div>
          ))}
        </div>
      )}
      {t.category_why && (
        <div className="txn-why">
          <b>{catLabel(t.category)}</b> — {t.category_why}
          {t.override_is_fee && t.override_bill ? (
            <> · a fee that rides with <b>{t.override_bill}</b>, counted
              with that bill</>) : null}
        </div>
      )}
    </div>
  );
}


const Row = memo(function Row({ t, viewer, showAccount, picked, armed,
  saving, bizPending, canRetire, retiring, menuOpen, noteOpen, receiptOpen,
  splitOpen, activeEntities, catGroups, knownCats, onArm, onPick, onRecat,
  onMenu, onNote, onReceipt, onSplit, onBiz, onRetire, onNoteDone }: RowProps) {
  const tcat = catLabel(t.category || "");
  const xfer = tcat.includes("TRANSFER") ||
    (tcat.includes("LOAN PAYMENTS") && t.amount < 0);
  const expandCols = (showAccount ? 6 : 5) + (viewer ? 0 : 1);
  return (
    <>
    <tr className={picked ? "picked" : undefined}>
      {!viewer && (
      <td className="c-pick" style={{ width: "1%" }}>
        <input type="checkbox" checked={picked}
               aria-label={`select ${t.payee}`}
               onChange={() => onPick(t.id)} />
      </td>
      )}
      <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(t.date)}</td>
      <td className={`num c-amt ${xfer ? "mut" : t.amount < 0 ? "pos" : ""}`}
          style={{ whiteSpace: "nowrap", width: "1%" }}>{money(-t.amount)}</td>
      <td className="c-merch">
        <MerchantAvatar name={t.payee} logo={t.merchant_logo}
                        style={{ marginRight: 7 }} />
        <Link to={`/bills/history?payee=${encodeURIComponent(t.payee)}`}
              title="full history for this merchant">{t.payee}</Link>
        {/* "AMAZON.COM" ×40 in a month tells you nothing; the order
            summary does. Same render as the daily email's recent
            table, so the two read alike. */}
        {t.item_summary && (
          <span className="mut"> — <i>{t.item_summary}</i></span>
        )}
        {/* a bill's matcher counts this row. A bare ⟳ glyph is too easy
            to miss next to the pill-styled pend/reimb/biz badges, so this
            is a matching blue pill. Unmatched spend rows keep the quiet
            add-as-bill shortcut into the prefilled history form. */}
        {t.recurring_bill ? (
          <Link to={`/bills/history?payee=${encodeURIComponent(t.recurring_bill)}`}
                className="pill b"
                title={`Counts toward the recurring bill "${t.recurring_bill}"`}
                aria-label={`Counts toward the recurring bill "${t.recurring_bill}"`}
                style={{ marginLeft: 6, textDecoration: "none" }}>⟳ recurring</Link>
        ) : null}
        {t.pending ? <span className="pill m" style={{ marginLeft: 6 }}>pend</span> : null}
        {t.reimb ? <span className="pill g" style={{ marginLeft: 6 }}>reimb</span> : null}
        {t.reimb_flag ? <span className="pill a"
            style={{ marginLeft: 6 }}
            title="flagged as reimbursement expected — the category is unchanged and it still counts as spend until the deposit is matched">
            awaiting reimbursement</span> : null}
        {/* biz is STATE on the row for everyone; the toggle lives in the
            ⋯ strip. A pill only owners could see would be information
            hidden behind a permission. */}
        {t.biz_flag ? <span className="pill b"
            style={{ marginLeft: 6 }}
            title="business expense (Schedule C tag)">biz</span> : null}
        {/* the row IS a business's money — its account belongs to the
            entity — as opposed to biz above, a household charge tagged
            as a business expense. Named, so two businesses read apart. */}
        {/* what the bank knows: the check number, the payment app in
            front of the merchant, and an online channel (in-store is the
            default and shows nothing) — read from the row's own columns */}
        {t.check_number ? <span className="pill m" style={{ marginLeft: 6 }}
            title="check number">#{t.check_number}</span> : null}
        {t.payment_processor ? <span className="pill m" style={{ marginLeft: 6 }}
            title={`paid through ${t.payment_processor}`}>via {t.payment_processor}</span> : null}
        {t.payment_channel === "online" ? <span className="pill m" style={{ marginLeft: 6 }}
            title="an online purchase">online</span> : null}
        {t.entity ? <Link to="/business" className="pill e"
            style={{ marginLeft: 6, textDecoration: "none" }}
            title={`${t.entity}'s account${t.account ? ` — ${t.account}` : ""}; managed on Business`}
            aria-label={`${t.entity} business account`}>{t.entity}</Link> : null}
        {t.note && !noteOpen && (
          <div className="mut" title="note — click 📝 to edit"
            style={{ fontSize: 11, fontStyle: "italic",
              whiteSpace: "normal", marginTop: 2 }}>
            📝 {t.note}</div>
        )}
      </td>
      <td className="sub c-cat" style={{ whiteSpace: "nowrap" }}>
        {/* a companion charge — the fee that rides with a bill's payment
            — says whose it is, so a $1 row is not a stray purchase */}
        {t.override_is_fee && t.override_bill ? (
          <span className="pill m" style={{ marginRight: ".35rem" }}
                title={`a fee that rides with ${t.override_bill}'s payment — counted with that bill`}>
            fee of {t.override_bill}</span>) : null}
        {/* .cat-chip: a dotted underline and a caret, so "you can
            change this" is visible instead of living in a hover
            tooltip a touch device never shows. Still a native select —
            the lazy arming below and every accessibility affordance
            come free with it. The reimbursement flag lives in the ⋯
            strip instead; two doors to one action is how they drift
            apart. */}
        {/* a hand-split row shows its parts where the category goes —
            the rollups count those, not the row's own category — and
            the picker steps aside: recategorizing a split is editing the
            split. Viewers read the same parts, plain. */}
        {t.split?.length ? (
          <span className="txn-split" role={viewer ? undefined : "button"}
                tabIndex={viewer ? undefined : 0}
                title={viewer ? "split across categories" : "edit the split"}
                aria-label={`split across ${t.split.length} categories${viewer ? "" : " — edit"}`}
                style={{ cursor: viewer ? undefined : "pointer",
                         display: "inline-flex", flexDirection: "column",
                         gap: 1, fontSize: 12, lineHeight: 1.3 }}
                onClick={viewer ? undefined : () => onSplit(t.id)}
                onKeyDown={viewer ? undefined : (e) => {
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSplit(t.id); }
                }}>
            {t.split.map((p, i) => (
              <span key={i}>
                <span className="mut">{money(Math.abs(p.amount))}</span>
                {" "}{catLabel(p.category)}
              </span>
            ))}
            <span className="pill b" style={{ alignSelf: "flex-start" }}>✂ split</span>
          </span>
        ) : viewer ? (
          <span style={{ fontSize: 12, color: "var(--mut)" }}>
            {catLabel(t.category)}</span>
        ) : (<>
        <select value="" title="recategorize (permanent override)"
                className="cat-chip"
                aria-label={`category: ${catLabel(t.category)} — change`}
                onPointerDown={() => onArm(t.id)}
                onFocus={() => onArm(t.id)}
                disabled={saving}
                onChange={(e) => e.target.value && onRecat(t.id, e.target.value)}>
          <option value="">
            {saving ? "saving…" : catLabel(t.category)}</option>
          {/* everything below the visible label mounts on first contact
              (the arming pattern) — a collapsed select needs only the
              option that labels it, and a page can carry hundreds of
              these at once */}
          {armed && (<>
          <option value="__reimburse__">↔ mark reimbursed…</option>
          <option value="__clear__">↺ reset to source category</option>
          <option value="__customcat__">✎ custom category…</option>
          {t.amount > 0 && activeEntities.map((e) => (
            <optgroup key={e.id} label={`🏢 ${e.name}`}>
              <option value={`__bizc__${e.id}`}>capitalize (owner contribution)</option>
              <option value={`__bizr__${e.id}`}>reimburse from business</option>
            </optgroup>
          ))}
          {/* the standard Plaid primaries, not just what this tenant
              already uses: offering only in-use categories leaves a
              brand-new one reachable ONLY through the custom prompt,
              which is how one-off spellings get into the data. The
              merchant-wide picker offers the same list, so the two
              pickers on this page cannot disagree. */}
          {catGroups}
          </>)}
        </select>
        {/* a category you set yourself can be handed back to the automatic
            layer in one tap, without opening the picker to find the reset
            entry; a bill's or a store match's override is not yours to
            drop here and keeps only the picker's reset */}
        {t.override_manual && !saving ? (
          <button type="button" className="chip-x"
                  style={{ marginLeft: ".3rem" }}
                  title="use automatic — drop your category and let the row take its source category again"
                  aria-label={`use the automatic category for ${t.payee}`}
                  onClick={() => onRecat(t.id, "__clear__")}>↺</button>
        ) : null}
        </>)}
      </td>
      {showAccount && (
        <td className="mut hide-m hide-t c-acct" title={t.account ?? ""}
            style={{ whiteSpace: "nowrap", textAlign: "right", maxWidth: "15rem",
                     overflow: "hidden", textOverflow: "ellipsis" }}>{t.account}</td>
      )}
      <td className="c-clip" style={{ textAlign: "center", width: "1%",
        whiteSpace: "nowrap" }}>
        {/* ONE control per row. Blue when the row already carries
            something (note or receipt), so what a pair of ghost buttons
            would convey by opacity survives in a single glyph instead of
            two permanent ones. Viewers get it
            only when there is a receipt to look at, since every other
            action in the strip is a write. */}
        {(!viewer || t.has_receipt) && (
        <button className="row-menu"
          aria-label={`actions for ${t.payee}`}
          aria-expanded={menuOpen}
          title={[t.note ? "note" : null,
                  t.has_receipt ? "receipt" : null]
                 .filter(Boolean).join(" · ") || "row actions"}
          style={(t.note || t.has_receipt)
            ? { color: "var(--blue)" } : undefined}
          onClick={() => onMenu(t.id)}>⋯</button>
        )}
      </td>
    </tr>
    {menuOpen && (
      <tr className="tr-expand"><td className="c-expand" colSpan={expandCols}>
        <TxnDetail t={t} />
        <div className="row-actions">
          {!viewer && (
          <button disabled={bizPending}
            title={t.biz_flag
              ? "unmark business expense"
              : "Schedule-C tag — budget math is unchanged"}
            onClick={() => onBiz(t.id, !t.biz_flag)}>
            {t.biz_flag ? "✓ business expense" : "mark business expense"}
          </button>
          )}
          {!viewer && (
          <button onClick={() => onNote(t.id)}>
            📝 {t.note ? "edit note" : "add a note"}</button>
          )}
          {(!viewer || t.has_receipt) && (
          <button onClick={() => onReceipt(t.id)}>
            📎 {t.has_receipt ? "receipt" : "attach a receipt"}</button>
          )}
          {/* the server's own answer: Split is offered exactly where
              the split door will take the row */}
          {!viewer && t.splittable && (
          <button onClick={() => onSplit(t.id)}
            title="one charge, several categories — the parts count where the row's category would">
            ✂ {t.split?.length ? "edit split" : "split across categories"}</button>
          )}
          {!viewer && !xfer && t.amount > 0 && !t.recurring_bill && (
          <Link className="btn" style={{ textDecoration: "none" }}
            to={`/bills/history?payee=${encodeURIComponent(t.payee)}`}
            title="opens this merchant's history with a prefilled form">
            ⟳ make recurring</Link>
          )}
          {!viewer && (
          <button onClick={() => onRecat(t.id,
            t.reimb_flag ? "__unflag__" : "__flag__")}>
            ⚑ {t.reimb_flag
              ? "not awaiting reimbursement"
              : "expect reimbursement"}</button>
          )}
          {!viewer && !t.reimb_flag && (
          <button
            title="a fraction comes back (co-pay, insurance claim); the rest stays real spend"
            onClick={() => onRecat(t.id, "__flag_partial__")}>
            ⚑ only part comes back</button>
          )}
          {/* offered only where the row is old enough to be stuck — a
              hold from yesterday is still in flight and dropping it
              would delete a charge that is about to post */}
          {canRetire && (
          <button disabled={retiring}
            title="the bank has left this authorization pending for over two weeks — drop the row from the ledger"
            onClick={() => onRetire(t.id)}>
            Remove stuck pending</button>
          )}
          {!viewer && (
          <span className="sub" style={{ marginLeft: "auto" }}>
            the category stays; it counts as spend until matched</span>
          )}
        </div>
      </td></tr>
    )}
    {noteOpen && (
      <tr className="tr-expand"><td className="c-expand" colSpan={expandCols}>
        <NoteEditor txnId={t.id} initial={t.note || ""}
          onDone={(note) => onNoteDone(t.id, note)} /></td></tr>
    )}
    {receiptOpen && (
      // tr-expand: opts this row OUT of the mobile card grid so the
      // receipt panel spans the full width on phones
      <tr className="tr-expand"><td className="c-expand" colSpan={expandCols}>
        <ReceiptPanel txnId={t.id} /></td></tr>
    )}
    {splitOpen && (
      <tr className="tr-expand"><td className="c-expand" colSpan={expandCols}>
        <SplitPanel t={t} catGroups={catGroups} known={knownCats}
                    onDone={() => onSplit(t.id)} /></td></tr>
    )}
    </>
  );
});


// memo on the table itself: the Transactions page re-renders for its own
// month/search/filter queries resolving; when the rows prop hasn't
// changed, none of that needs to touch this subtree.
export default memo(function TxnTable({ rows, showAccount = true,
  dayGroups = false }: {
  rows: Txn[]; showAccount?: boolean;
  // Day dividers are OFF unless asked for. This table also draws Today's
  // recent pane, a merchant's bill history and the Business worksheet —
  // short lists where a header per day is chrome, not structure. It also
  // assumes date order, which every caller currently is; a future
  // sort-by-amount has to turn this off with it.
  dayGroups?: boolean;
}) {
  const qc = useQueryClient();
  // The category taxonomy is ~38 <option>s and every row carries its own
  // picker, so a page of a few hundred rows is mostly dropdown contents
  // behind collapsed selects — tens of thousands of option nodes, and
  // hundreds of milliseconds of blocked main thread to build them. A select
  // only needs its list when someone is actually about to use it, so rows
  // are armed on first contact and stay armed.
  //
  // flushSync, not a plain setState, and this is the whole correctness of
  // the change: pointerdown and focus both fire before the select's popup
  // opens, but a scheduled render is not guaranteed to land before the
  // browser runs that default action — and losing that race means the user
  // opens a picker containing one item. flushSync makes the list part of
  // the same synchronous step as the gesture that asked for it. The cost is
  // one render of already-light rows, once per row, ever.
  const [armed, setArmed] = useState<Set<string>>(() => new Set());
  const arm = useCallback((id: string) => {
    // returning the same Set when already armed lets React bail out, so
    // repeat contacts cost nothing
    flushSync(() => setArmed((s) => s.has(id) ? s : new Set(s).add(id)));
  }, []);
  const navigate = useNavigate();
  const cats = useQuery({ queryKey: ["cats"], queryFn: api.categories });
  // anything in use that isn't a standard Plaid primary — i.e. what this
  // tenant named itself via the custom prompt
  const customCats = useMemo(() => {
    const std = new Set([...(cats.data?.plaid_spend ?? []),
                         ...(cats.data?.plaid_flow ?? [])]);
    return (cats.data?.categories ?? []).filter((c) => !std.has(c));
  }, [cats.data]);
  const knownCats = useMemo(() => new Set([
    ...(cats.data?.plaid_spend ?? []), ...(cats.data?.plaid_flow ?? []),
    ...customCats]), [cats.data, customCats]);
  // one shared element for the full taxonomy — the bulk bar and every
  // armed row picker offer the identical list, built once per cats fetch
  const catGroups = useMemo(() => (<>
    <optgroup label="Spending">
      {cats.data?.plaid_spend.map((c) => (
        <option key={c} value={c}>{catLabel(c)}</option>))}
    </optgroup>
    <optgroup label="Transfers &amp; income">
      {cats.data?.plaid_flow.map((c) => (
        <option key={c} value={c}>{catLabel(c)}</option>))}
    </optgroup>
    {/* categories this tenant made itself (the custom prompt) stay
        selectable — the list adds, never removes */}
    {customCats.length > 0 && (
      <optgroup label="Your own categories">
        {customCats.map((c) => (
          <option key={c} value={c}>{catLabel(c)}</option>))}
      </optgroup>
    )}
  </>), [cats.data, customCats]);
  // recategorize / reimb flag / biz toggle / receipt upload are owner
  // writes — viewers get the same table with plain text instead of controls
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const viewer = isViewer(me);
  // the demo instance refuses merchant-wide category writes (its login is
  // shared) — disabled-in-place beats a 403 after the scope question, the
  // same gate Merchants applies. Single-row writes still go through.
  const demo = !!me.data?.demo;
  // Keyed by the id of each day's FIRST row so the header renders inline
  // with the row map below. Computed over the rows actually handed to us,
  // which is what keeps "show more" correct: a day straddling a chunk
  // boundary regroups on the next render instead of stranding a header.
  const dayHeads = useMemo(() => {
    if (!dayGroups) return null;
    const heads = new Map<string,
      { label: string; count: number; spent: number }>();
    let start = 0;
    for (let i = 0; i <= rows.length; i++) {
      if (i < rows.length
          && rows[i].date.slice(0, 10) === rows[start].date.slice(0, 10))
        continue;
      const group = rows.slice(start, i);
      if (group.length)
        heads.set(group[0].id, { label: dayLabel(group[0].date),
                                 count: group.length, spent: daySpend(group) });
      start = i;
    }
    return heads;
  }, [dayGroups, rows]);
  const [receiptFor, setReceiptFor] = useState<string | null>(null);
  const [splitFor, setSplitFor] = useState<string | null>(null);
  const [noteFor, setNoteFor] = useState<string | null>(null);
  // Three always-rendered controls per row — a biz toggle plus a 📝 and a
  // 📎 at 30% opacity — is 300 buttons on a 100-row page, most of them
  // meaning "nothing here yet". One ⋯ opens a strip with all of them. It is also the named touch equivalent for the
  // affordances that only explained themselves in hover tooltips.
  const [menuFor, setMenuFor] = useState<string | null>(null);
  // Bulk selection. Lives here rather than on a page so
  // the Transactions ledger, a merchant's history and Today's recent pane
  // all get it from one implementation — the same reason this component
  // exists at all.
  const [sel, setSel] = useState<Set<string>>(() => new Set());
  const allShown = rows.length > 0 && rows.every((r) => sel.has(r.id));
  const toggleOne = useCallback((id: string) => setSel((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  }), []);
  const bulk = useMutation({
    mutationFn: ({ action, category }: { action: string; category?: string }) =>
      api.txnBulk([...sel], action, category),
    // the ids the write covered, captured at send time — the selection is
    // cleared on success, after the rows it named have been patched
    onMutate: () => ({ ids: new Set(sel) }),
    onSuccess: (r, v, ctx) => {
      const patch: Partial<Txn> | null =
        v.action === "category" && v.category ? { category: catLabel(v.category) }
        : v.action === "biz_on" ? { biz_flag: true }
        : v.action === "biz_off" ? { biz_flag: false }
        : v.action === "reimb_flag" ? { reimb_flag: true }
        : v.action === "reimb_unflag" ? { reimb_flag: false }
        : null;
      if (patch) patchRows((t) => ctx.ids.has(t.id), patch);
      setSel(new Set());
      invalidate();
      qc.invalidateQueries({ queryKey: ["biz"] });
      // say what happened, including the consequence they may not have
      // intended: a transfer that is now spending
      const bits = [`${r.applied} updated`];
      if (r.flow) bits.push(`${r.flow} of them were transfers or income and `
        + `now count as spending`);
      if (r.missing) bits.push(`${r.missing} no longer exist`);
      window.dispatchEvent(new CustomEvent("oiko-toast",
        { detail: bits.join(" · ") }));
    },
    onError: (e) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: `Bulk update failed — ${errText(e)}` })),
  });
  // a business cost paid on a personal card can be capitalized
  // (contribution) or marked reimbursable, assigning it to an entity. Only
  // fetch when a business is set up.
  const bizsum = useQuery({ queryKey: ["biz-summary"],
    queryFn: api.businessSummary, retry: false,
    // no business set up → nothing to count, and no business column to show
    enabled: !viewer && !!me.data?.has_business });
  const activeEntities = useMemo(() =>
    (bizsum.data?.entities || []).filter(e => e.status === "active"),
    [bizsum.data]);

  // Write the changed row into every cached ledger list that could be
  // showing it — the Transactions page's ["txns", params] (this table
  // doesn't know which params it sits in) and a merchant's history. The
  // server has already committed; this is what lets the row change in the
  // same frame the response lands, instead of after the list refetches.
  const patchRows = useCallback(
    (pred: (t: Txn) => boolean, patch: Partial<Txn>) => {
      const fn = (t: Txn) => pred(t) ? { ...t, ...patch } : t;
      patchQueries<{ rows: Txn[] }>(qc, ["txns"],
        (d) => d.rows ? { ...d, rows: d.rows.map(fn) } : d);
      patchQueries<{ txns: Txn[] }>(qc, ["bills-history"],
        (d) => d.txns ? { ...d, txns: d.txns.map(fn) } : d);
    }, [qc]);
  const invalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["txns"] });
    qc.invalidateQueries({ queryKey: ["reimb"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    // the merchant-history page renders this same table — its rows must
    // refetch after a write too
    qc.invalidateQueries({ queryKey: ["bills-history"] });
    // the Merchants detail panel shows the merchant's category and rule,
    // which an "all" recategorize just changed
    qc.invalidateQueries({ queryKey: ["merchant-detail"] });
  }, [qc]);
  // the row is gone, not changed — take it out of every cached list
  // showing it so it leaves the table in the same frame the response
  // lands, the way patchRows does for an edit
  const dropRow = useCallback((id: string) => {
    patchQueries<{ rows: Txn[] }>(qc, ["txns"],
      (d) => d.rows ? { ...d, rows: d.rows.filter((t) => t.id !== id) } : d);
    patchQueries<{ txns: Txn[] }>(qc, ["bills-history"],
      (d) => d.txns ? { ...d, txns: d.txns.filter((t) => t.id !== id) } : d);
  }, [qc]);
  // Retire a pending row the bank never resolved. Sync cannot do this on
  // its own — a row missing from one feed response is not proof it was
  // cancelled — so it is a hand action, and a deliberate one: the server
  // refuses anything not pending or younger than two weeks, and its
  // refusal text is what the toast says. Not offered on the demo: one
  // visitor deleting rows from the shared synthetic ledger degrades it
  // for the next one, and it cannot be undone.
  // ids with a retire in flight — a shared mutation's `variables` names
  // only the LAST click, so a second row's click would re-enable the first
  // row's button while its request is still out
  const [retiring, setRetiring] = useState<Set<string>>(() => new Set());
  const retire = useMutation({
    mutationFn: (id: string) => api.retirePending(id),
    onMutate: (id) => setRetiring((s) => new Set(s).add(id)),
    onSettled: (_r, _e, id) => setRetiring((s) => { const n = new Set(s); n.delete(id); return n; }),
    onSuccess: (_r, id) => {
      dropRow(id);
      // the totals and the verdict counted this row; both recompute
      qc.invalidateQueries({ queryKey: ["txns"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["bills-history"] });
      window.dispatchEvent(new CustomEvent("oiko-toast",
        { detail: "Pending transaction removed." }));
    },
    onError: (e) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: errText(e) })),
  });
  const retireMutate = retire.mutate;
  const onRetire = useCallback((id: string) => retireMutate(id),
    [retireMutate]);
  // a bare await here makes failures (network, 403) look like success
  // until reload — a mutation invalidates only on success, and the
  // global MutationCache toasts 401/403; non-auth failures get their own
  // toast below. The select itself is always value="" (the row shows the
  // server category), so there's no optimistic state to roll back.
  const recatM = useMutation({
    mutationFn: ({ id, category, scope }:
                 { id: string; category: string; scope?: "one" | "all" }) => {
      if (category === "__flag__") return api.reimbFlag(id);
      // a co-pay or insurance claim is partial from the moment it's
      // flagged, not after a trip to the Reimburse page
      if (category === "__flag_partial__")
        return api.reimbFlag(id, { partial: true });
      if (category === "__unflag__") return api.reimbUnflag(id);
      return api.setCategory(id, category === "__clear__" ? "" : category,
                             scope ?? "one");
    },
    onSuccess: (_r, v) => {
      // the row shows the new state as `saving…` clears — the server has
      // it; the refetch below only fills in what it derived (provenance,
      // the bill matcher, the verdict's spend rule)
      const flagging = v.category === "__flag__" || v.category === "__flag_partial__";
      if (flagging) patchRows((t) => t.id === v.id, { reimb_flag: true });
      else if (v.category === "__unflag__")
        patchRows((t) => t.id === v.id, { reimb_flag: false });
      // a reset goes back to whatever the sources say, which only the
      // server knows — that one waits for the refetch
      else if (v.category !== "__clear__")
        patchRows((t) => t.id === v.id, { category: catLabel(v.category) });
      qc.invalidateQueries({ queryKey: ["txns"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["bills-history"] });
      if (flagging || v.category === "__unflag__")
        qc.invalidateQueries({ queryKey: ["reimb"] });
      // only a merchant-wide write changes the merchant's rule
      if (v.scope === "all")
        qc.invalidateQueries({ queryKey: ["merchant-detail"] });
    },
    onError: (e) => {
      if (!/\b40[13]\b/.test(String(e)))
        window.dispatchEvent(new CustomEvent("oiko-toast", {
          detail: `Couldn't save the category — ${errText(e)}` }));
    },
  });
  // Ask which rows the correction covers. Choosing "just this one" writes
  // NO rule, so it never appears on the Rules page — which is the whole
  // point: "Check Paid" and a named person's transfer are not one category,
  // and a single edit must not pin either of them merchant-wide.
  const [scopeAsk, setScopeAsk] = useState<
    { id: string; category: string; payee: string } | null>(null);

  // react-query's mutate is identity-stable; depending on it (not the
  // mutation result object, which is new every render) keeps these
  // callbacks stable so the memoized rows actually skip
  const recatMutate = recatM.mutate;
  // "All" is the same merchant-wide write as Bills → "Apply to whole
  // merchant", and since a user rule outranks the bank's transfer label it
  // moves bank-labelled transfers/income into spend. That changes the budget
  // math, so it gets the same preview-then-consent step here instead of
  // happening quietly from a row menu. The preview is advisory: when it
  // can't be read (it is owner-only; a member's write still goes through as
  // it always did) the write proceeds without the warning.
  const [flowAsk, setFlowAsk] = useState<
    { flow_rows: number; skipped_override: number } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const applyAll = useCallback(async () => {
    if (!scopeAsk) return;
    const { id, category, payee } = scopeAsk;
    let flow_rows = 0, skipped_override = 0;
    if (payee) {
      setPreviewing(true);
      try {
        // family: false — this row's "All" writes scope "all" on ONE
        // canonical merchant, so the preview counts the same single-
        // canonical scope (the bill-history page keeps family scope)
        const p = await api.merchantCategoryPreview(payee, category,
                                                    { family: false });
        flow_rows = p.flow_rows ?? 0;
        skipped_override = p.skipped_override ?? 0;
      } catch {
        // advisory off the demo — but on the demo instance a refusal here
        // IS the write guard, and proceeding would only 403 after it
        if (demo) {
          window.dispatchEvent(new CustomEvent("oiko-toast", { detail:
            "Not available on the demo instance — synthetic data only." }));
          setScopeAsk(null);
          return;
        }
      }
      finally { setPreviewing(false); }
    }
    if (flow_rows > 0) { setFlowAsk({ flow_rows, skipped_override }); return; }
    recatMutate({ id, category, scope: "all" });
    setScopeAsk(null);
  }, [scopeAsk, recatMutate, demo]);
  // capitalize / reimburse a personally-paid business expense — assign it
  // to an entity and record the equity movement. A mutation rather than a
  // bare promise so the row reads as saving and a second pick can't record
  // the movement twice.
  const equityM = useMutation({
    mutationFn: ({ id, entity, mode }:
                 { id: string; entity: string; mode: "contribute" | "reimburse" }) =>
      api.equityReimburse(entity, { txn_id: id, mode }),
    onSuccess: (_r, v) => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["biz-summary"] });
      window.dispatchEvent(new CustomEvent("oiko-toast", { detail:
        v.mode === "contribute"
          ? "Capitalized as a business contribution."
          : "Assigned to the business, reimbursement recorded." }));
    },
    onError: (e) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: errText(e) })),
  });
  const equityMutate = equityM.mutate;
  const recat = useCallback((id: string, category: string) => {
    if (category === "__reimburse__") { navigate("/reimburse"); return; }
    if (category.startsWith("__bizc__") || category.startsWith("__bizr__")) {
      equityMutate({ id, entity: category.slice(8),
        mode: category.startsWith("__bizc__") ? "contribute" : "reimburse" });
      return;
    }
    if (category === "__customcat__") {
      const name = window.prompt("Custom category name:")?.trim();
      if (!name) return;
      category = name;
    }
    // clearing an override is inherently this-row-only — nothing to teach
    if (category === "__clear__") {
      recatMutate({ id, category, scope: "one" });
      return;
    }
    const row = rows.find((r) => r.id === id);
    setFlowAsk(null);
    setScopeAsk({ id, category, payee: row?.payee || "" });
  }, [navigate, equityMutate, recatMutate, rows]);
  const biz = useMutation({
    mutationFn: ({ id, on }: { id: string; on: boolean }) =>
      api.bizFlag(id, on),
    onSuccess: (_r, v) => {
      // the pill and the strip's button both read t.biz_flag from the list
      patchRows((t) => t.id === v.id, { biz_flag: v.on });
      qc.invalidateQueries({ queryKey: ["txns"] });
      qc.invalidateQueries({ queryKey: ["biz"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["bills-history"] });
    },
  });
  const bizMutate = biz.mutate;
  const onBiz = useCallback((id: string, on: boolean) =>
    bizMutate({ id, on }), [bizMutate]);
  // ⋯ opens one strip at a time; note, receipt and split panels close with it
  const onMenu = useCallback((id: string) => {
    setMenuFor((cur) => (cur === id ? null : id));
    setNoteFor(null); setReceiptFor(null); setSplitFor(null);
  }, []);
  const onSplit = useCallback((id: string) =>
    setSplitFor((cur) => (cur === id ? null : id)), []);
  const onNote = useCallback((id: string) =>
    setNoteFor((cur) => (cur === id ? null : id)), []);
  const onReceipt = useCallback((id: string) =>
    setReceiptFor((cur) => (cur === id ? null : id)), []);
  // a note changes no money: the saved text goes onto the cached row and
  // nothing is refetched (cancel passes no note and patches nothing)
  const onNoteDone = useCallback((id: string, note?: string) => {
    setNoteFor(null);
    if (note !== undefined) patchRows((t) => t.id === id, { note });
  }, [patchRows]);

  // The prompt renders UNDER the row it concerns, not above the table: a
  // note pinned to the table top is off screen from row 300 of a long
  // ledger (and on desktop it would slide under the fixed top bar), so
  // choosing a category deep in the list would look like it did nothing. It scrolls
  // itself into view when it lands below the fold.
  const askRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    askRef.current?.scrollIntoView({ block: "nearest" });
  }, [scopeAsk?.id, flowAsk]);
  const scopePrompt = scopeAsk && (
      <div className="note" ref={askRef}
           style={{ display: "block", scrollMarginTop: "var(--bar-h, 0px)",
                    scrollMarginBottom: ".5rem" }}>
        <div style={{ marginBottom: ".5rem" }}>
          Apply <b>{catLabel(scopeAsk.category)}</b> to…
        </div>
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
          <button className="pri" onClick={() => {
            recatM.mutate({ id: scopeAsk.id, category: scopeAsk.category,
                            scope: "one" });
            setScopeAsk(null); setFlowAsk(null);
          }}>Just this one</button>
          {!flowAsk && (
            <button disabled={previewing || demo} onClick={applyAll}
              title={demo
                ? "Not available on the demo instance — synthetic data only."
                : undefined}>
              {previewing ? "Checking…"
                : `All “${scopeAsk.payee || "this merchant"}” transactions`}
            </button>
          )}
          <button className="linklike mut" style={{ background: "none",
                    border: "none", cursor: "pointer" }}
                  onClick={() => { setScopeAsk(null); setFlowAsk(null); }}>
            Cancel</button>
        </div>
        {flowAsk ? (
          // same copy as Bills → "Apply to whole merchant": name the
          // consequence before the write, never after
          <div className="note" style={{ marginTop: ".5rem", display: "block" }}>
            <div><b>{flowAsk.flow_rows}</b> of these row
              {flowAsk.flow_rows === 1 ? " is" : "s are"} labelled by your
              bank as a transfer, income or loan payment and <b>will</b> move
              too — your merchant rule outranks that label (a Venmo or Zelle
              payment to a person is a transfer to the bank and whatever you
              say it is to you). They start counting as spending.</div>
            {!!flowAsk.skipped_override && (
              <div style={{ marginTop: ".3rem" }}>
                <b>{flowAsk.skipped_override}</b> row
                {flowAsk.skipped_override === 1 ? " has" : "s have"} a
                category you set by hand and will be left alone.</div>
            )}
            <div style={{ marginTop: ".6rem", display: "flex", gap: ".5rem" }}>
              <button className="pri" onClick={() => {
                recatM.mutate({ id: scopeAsk.id, category: scopeAsk.category,
                                scope: "all" });
                setScopeAsk(null); setFlowAsk(null);
              }}>Yes, recategorize</button>
              <button onClick={() => setFlowAsk(null)}>Back</button>
            </div>
          </div>
        ) : (
          <div className="sub" style={{ marginTop: ".4rem" }}>
            “Just this one” changes only this row and creates no rule.
            {demo
              ? " “All” is not available on the demo instance — synthetic data only."
              : " “All” teaches the merchant and shows up under Settings → Categorization."}
          </div>
        )}
      </div>
    );

  return (
    <>
    {/* the bar appears only with a selection, and states the count it will
        act on — a bulk action that does not say how many rows it covers is
        how "apply to all" can move six of thirty-six and look like a
        no-op */}
    {sel.size > 0 && (
      <div className="bulkbar">
        <b>{sel.size} selected</b>
        <select value="" aria-label="set a category for the selected rows"
                disabled={bulk.isPending}
                onChange={(e) => {
                  if (e.target.value) bulk.mutate({ action: "category",
                                                    category: e.target.value });
                  e.target.value = "";
                }}>
          <option value="">recategorize…</option>
          {catGroups}
        </select>
        <button disabled={bulk.isPending}
                onClick={() => bulk.mutate({ action: "biz_on" })}>
          mark business</button>
        <button disabled={bulk.isPending}
                onClick={() => bulk.mutate({ action: "biz_off" })}>
          unmark business</button>
        <button disabled={bulk.isPending}
                onClick={() => bulk.mutate({ action: "reimb_flag" })}>
          ⚑ expect reimbursement</button>
        <button disabled={bulk.isPending}
                onClick={() => bulk.mutate({ action: "reimb_unflag" })}>
          unflag</button>
        <button style={{ marginLeft: "auto" }}
                onClick={() => setSel(new Set())}>clear</button>
      </div>
    )}
    {/*.txn-table + per-td c-* classes let index.css restack each
        row into a two-line card grid at <=620px (desktop unchanged) */}
    <table className="txn-table">
      <thead><tr>
        {!viewer && (
        <th className="c-pick" style={{ width: "1%" }}>
          <input type="checkbox" checked={allShown}
                 aria-label={allShown ? "clear selection" : "select all shown"}
                 title={allShown ? "clear selection" : "select all shown"}
                 onChange={() => setSel(allShown ? new Set()
                                                 : new Set(rows.map((r) => r.id)))} />
        </th>
        )}
        <th style={{ width: "1%", whiteSpace: "nowrap" }}>Date</th>
        <th className="num" style={{ width: "1%", whiteSpace: "nowrap" }}>Amount</th>
        {/* Merchant is the ONE flexible column — nowrap everywhere
            pushes the table's min width past the card and silently
            scrolls the last (📎) column out of view */}
        <th>Merchant</th>
        <th style={{ whiteSpace: "nowrap" }}>Category</th>
        {showAccount && (
          <th className="hide-m hide-t" style={{ width: "1%", whiteSpace: "nowrap", textAlign: "right" }}>Account</th>
        )}
        <th style={{ width: "1%", textAlign: "center" }} title="receipts">📎</th>
      </tr></thead>
      <tbody>
        {rows.map((t: Txn) => {
          const head = dayHeads?.get(t.id);
          return (
          <Fragment key={t.id}>
          {head && (
            <tr className="dayhead">
              <td colSpan={(showAccount ? 6 : 5) + (viewer ? 0 : 1)}>
                <div className="dayhead-in">
                  <span className="dayhead-l">{head.label}</span>
                  {/* a day of nothing but transfers or income has a real
                      count and no spend — printing "$0.00 spent" there
                      reads as a claim about the day rather than about
                      what this table excludes */}
                  <span className="dayhead-r">
                    {head.count} transaction{head.count === 1 ? "" : "s"}
                    {head.spent > 0 && (
                      <> · <b>{money(-head.spent)}</b> spent</>)}
                  </span>
                </div>
              </td>
            </tr>
          )}
          <Row t={t} viewer={!!viewer} showAccount={showAccount}
               picked={sel.has(t.id)} armed={armed.has(t.id)}
               saving={(recatM.isPending && recatM.variables?.id === t.id)
                       || (equityM.isPending && equityM.variables?.id === t.id)}
               bizPending={biz.isPending && biz.variables?.id === t.id}
               canRetire={!viewer && !demo && stuckPending(t)}
               retiring={retiring.has(t.id)}
               menuOpen={menuFor === t.id} noteOpen={noteFor === t.id}
               receiptOpen={receiptFor === t.id} splitOpen={splitFor === t.id}
               activeEntities={activeEntities} catGroups={catGroups}
               knownCats={knownCats}
               onArm={arm} onPick={toggleOne} onRecat={recat}
               onMenu={onMenu} onNote={onNote} onReceipt={onReceipt}
               onSplit={onSplit}
               onBiz={onBiz} onRetire={onRetire} onNoteDone={onNoteDone} />
          {scopeAsk?.id === t.id && (
            <tr className="tr-expand" data-scope-ask>
              <td className="c-expand"
                  colSpan={(showAccount ? 6 : 5) + (viewer ? 0 : 1)}>
                {scopePrompt}
              </td>
            </tr>
          )}
          </Fragment>);
        })}
      </tbody>
    </table>
    </>
  );
});
