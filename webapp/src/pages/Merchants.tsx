// Merchants — the one place a person can correct who a payee is.
//
// Every automatic naming layer in this app is deliberately conservative,
// and the reason was always the same: a wrong answer was unfixable by the
// person looking at it. An aggregator that cannot resolve a small merchant
// echoes whatever the bank's descriptor said, so one clinic arrives as
// three names over five months; a chain that runs a fuel pump arrives as
// one name covering two businesses. Both are corrected here.
//
// The page is one list. Search, sort and a few filters stay pinned at the
// top; a row is a logo, a name and one line of facts; everything else —
// source strings, places, the category rule, and the two actions — lives
// in a panel that opens beside the list, so the list never reflows.
// Rename and Merge are two actions on the same door (the journalled
// rename): Rename takes a new name, Merge picks an EXISTING merchant, so a
// typo cannot fork a merchant. The proposer's offers are a queue above the
// list ("Needs a look"), one line each, the evidence behind a click.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api, errText, mmdd, money as money$, catLabel } from "../api/client";
import type { MerchantDetail, MergeProposal, MergeSideFacts } from "../api/client";
import { removeFromList } from "../api/cache";
import { canEdit } from "../role";
import { EMPTY_LINES, failureStatus } from "../moneymath";
import MerchantAvatar from "../components/MerchantAvatar";
import { useDialog } from "../components/useDialog";
import { FailedLine, StaleLine } from "../components/Loaded";
import SegmentedControl from "../components/SegmentedControl";
import { ShowAllButton, useShowMore } from "../components/ShowMore";
const money = (v: number | null | undefined) => money$(v, false);

// the server caps the catalog at its 200 most frequent merchants;
// the long tail is reachable only through search
const CATALOG_LIMIT = 200;
// a filter or a spend/recency sort is cut from whatever came back, so ask
// for the widest window the server allows before cutting it
const CATALOG_WIDE = 500;

// the server caps the `variants` array it returns, so the array's length
// is NOT the number of source names — a merchant with 3,000 spellings
// would claim the cap. `variant_count` is the true count; fall back to
// the array only for an older server that doesn't send it.
const variantCount = (m: { variants: string[]; variant_count?: number }) =>
  m.variant_count ?? m.variants.length;

type Catalog = Awaited<ReturnType<typeof api.merchantCatalog>>;
type Row = Catalog["merchants"][number];

// the orders the server sorts by (count and name through `order`, spend
// and recency through `sort`)
export const ORDERS = [
  ["count", "most frequent"], ["alpha", "a → z"],
  ["spend", "biggest spend"], ["recent", "recently seen"],
] as const;
type Order = typeof ORDERS[number][0];
const serverOrder = (o: Order): "count" | "alpha" => (o === "alpha" ? "alpha" : "count");

// filters that answer the questions people bring to this page: which of
// these are one-offs, which still wear a bank string as their name, what
// has been seen lately
export const FILTERS = [
  ["all", "all"], ["oneoff", "one-off"], ["unnamed", "unnamed"], ["month", "seen this month"],
] as const;
type Filter = typeof FILTERS[number][0];

const toast = (msg: string) =>
  window.dispatchEvent(new CustomEvent("oiko-toast", { detail: msg }));

// merchants the catalog shows before "Show all"
const MERCHANTS_SHORT = 25;

export default function Merchants({ embedded = false }:
                                  { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  const [filterText, setFilterText] = useState("");
  const [q, setQ] = useState("");   // debounced — server-side search
  useEffect(() => {
    const t = window.setTimeout(() => setQ(filterText.trim()), 300);
    return () => window.clearTimeout(t);
  }, [filterText]);
  const [order, setOrder] = useState<Order>("count");
  const [flt, setFlt] = useState<Filter>("all");
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // one merchant open at a time — click the row to open its panel
  const [open, setOpen] = useState<string | null>(null);
  // fail CLOSED while /api/me loads — an editor briefly missing a control
  // beats a viewer being shown one that 403s
  const mayEdit = canEdit(me);
  // the demo instance 403s every write; disabled-in-place beats a 403
  const demo = !!me.data?.demo;
  const narrowed = flt !== "all" || order === "spend" || order === "recent";
  const limit = narrowed ? CATALOG_WIDE : CATALOG_LIMIT;
  const key = ["merchants", q, serverOrder(order), order, flt];
  const list = useQuery({ queryKey: key,
                          queryFn: () => api.merchantCatalog(q, serverOrder(order),
                                                             { sort: order, filter: flt, limit }),
                          // keep the last page on screen while refetching —
                          // a debounce fire shouldn't flash "loading…"
                          placeholderData: (prev) => prev });

  const done = (msg: string) => {
    qc.invalidateQueries({ queryKey: ["merchants"] });
    // the open panel is keyed by display name and would otherwise keep
    // showing the pre-rename/merge facts until it was closed and reopened
    qc.invalidateQueries({ queryKey: ["merchant-detail"] });
    // every surface that prints a payee is now stale
    qc.invalidateQueries({ queryKey: ["txns"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["bills-history"] });
    qc.invalidateQueries({ queryKey: ["alertsHistory"] });
    toast(msg);
  };
  const rename = useMutation({
    mutationFn: (v: { display: string; to: string }) => api.merchantRename(v),
    onSuccess: (r, v) => {
      // the catalog on screen shows the result now — a rename relabels the
      // row, a merge folds it into the target's — instead of the old name
      // sitting there until the 200-row catalog comes back
      qc.setQueryData<Catalog>(key, (old) => {
        if (!old) return old;
        const src = old.merchants.find((m) => m.display === v.display);
        if (!src) return old;
        const rest = old.merchants.filter((m) => m.display !== v.display);
        const target = rest.find((m) => m.display === v.to);
        return { ...old, merchants: target
          ? rest.map((m) => m !== target ? m : {
              ...m, rows: m.rows + src.rows, total: m.total + src.total,
              first: m.first < src.first ? m.first : src.first,
              last: m.last > src.last ? m.last : src.last,
              variants: [...m.variants, ...src.variants],
              variant_count: variantCount(m) + variantCount(src) })
          : [...rest, { ...src, display: v.to }] };
      });
      if (open === v.display) setOpen(v.to);
      done(`${v.display} → ${v.to} · ${r.rows} transaction${r.rows === 1 ? "" : "s"}`);
    },
    onError: (e) => toast(`Couldn't rename — ${errText(e)}`),
  });
  const undo = useMutation({
    mutationFn: (id: number) => api.merchantUndo(id),
    onSuccess: (_r, id) => {
      // the undone entry leaves the journal at once; the catalog rows it
      // moved come back with the refetch
      removeFromList<Catalog, Catalog["recent"][number]>(qc, key, "recent", (x) => x.id === id);
      done("Change undone.");
    },
    onError: (e) => toast(`Couldn't undo — ${errText(e)}`),
  });
  const decide = useMutation({
    mutationFn: ({ pid, action, into_id }:
        { pid: string; action: "approve" | "reject"; into_id: string }) =>
      api.merchantMerge(pid, action, into_id),
    onSuccess: (_r, v) => {
      removeFromList<Catalog, MergeProposal>(qc, key, "merges", (x) => x.id === v.pid);
      // a merge moved rows and wrote a journal entry the page shows
      if (v.action === "approve") {
        qc.invalidateQueries({ queryKey: ["merchants"] });
        qc.invalidateQueries({ queryKey: ["merchant-detail"] });
        qc.invalidateQueries({ queryKey: ["txns"] });
      }
      // the "merges" alert retires on the server the moment the queue
      // empties — by approving OR rejecting the last offer — so the
      // strip and the Alerts page refetch either way
      qc.invalidateQueries({ queryKey: ["alertsHistory"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      toast(v.action === "approve" ? "Merged." : "Kept apart.");
    },
    onError: (e) => {
      // a sync turned the offer since it was listed: nothing merged, and
      // the list comes back with the offer as it now stands
      if (failureStatus(e) === 409) {
        qc.invalidateQueries({ queryKey: ["merchants"] });
        toast("This suggestion changed — showing it again.");
        return;
      }
      toast(`Couldn't decide — ${errText(e)}`);
    },
  });
  // "merge all" walks the queue one decision at a time: each is its own
  // journal entry, so a wrong one is undone alone. An earlier approve can
  // settle a later offer or re-point it into a pair that only shares a
  // word; the server answers that 409 and the batch skips it, leaving it
  // for the refetched list to show on its own.
  const decideAll = useMutation({
    mutationFn: async (offers: { id: string; into_id: string }[]) => {
      let n = 0, skipped = 0;
      for (const o of offers) {
        try { await api.merchantMerge(o.id, "approve", o.into_id, true); n += 1; }
        catch (e) {
          if (failureStatus(e) !== 409) throw e;
          skipped += 1;
        }
      }
      return { n, skipped };
    },
    onSuccess: ({ n, skipped }) => done(`Merged ${n} pair${n === 1 ? "" : "s"}.`
      + (skipped ? ` ${skipped} changed on the way — check ${skipped === 1 ? "it" : "them"} below.` : "")),
    onError: (e) => { done(`Stopped — ${errText(e)}`); },
  });

  // the server filters and sorts — "seen this month" by the household's
  // own month, "unnamed" by its own test — and the list is printed in the
  // order it came (the phone does the same)
  const rows = list.data?.merchants ?? [];
  // the catalog opens on its first few; the rest is one tap away (a
  // search narrows the list itself, and every hit stays within reach)
  const more = useShowMore(rows, MERCHANTS_SHORT);
  const recent = list.data?.recent ?? [];
  const merges = list.data?.merges ?? [];
  const openRow = rows.find((m) => m.display === open)
    ?? (list.data?.merchants ?? []).find((m) => m.display === open);

  return (
    <>
      {!embedded && (
        <div className="sub" style={{ marginBottom: ".2rem" }}>
          <Link to="/settings/merchants">← Settings · Merchants</Link>
        </div>
      )}
      {!embedded && <h1>Merchants</h1>}
      <div className={"card merchants-head" + (embedded ? "" : " pinned")}
           style={{ marginTop: embedded ? "1rem" : 0 }}>
        {!embedded && (
          <div className="sub">
            Every name your ledger shows. Open one to see its source strings,
            where it has been seen, and to rename or merge it.
          </div>
        )}
        {mayEdit && demo && (
          <div className="sub" style={{ marginTop: ".4rem", color: "var(--amber)" }}>
            Not available on the demo instance — synthetic data only.
          </div>
        )}
        <div style={{ display: "flex", gap: ".6rem", alignItems: "center",
                      flexWrap: "wrap", marginTop: ".6rem" }}>
          <input value={filterText} onChange={(e) => setFilterText(e.target.value)}
                 placeholder="search merchants…" aria-label="search merchants"
                 style={{ width: "min(22rem, 100%)" }} />
          <select value={order} title="sort" aria-label="sort"
                  onChange={(e) => setOrder(e.target.value as Order)}>
            {ORDERS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
          <SegmentedControl label="Show" size="sm" value={flt} onChange={setFlt}
            options={FILTERS.map(([v, label]) => ({
              value: v, label: label[0].toUpperCase() + label.slice(1) }))} />
        </div>
      </div>

      {merges.length > 0 && (
        <NeedsALook merges={merges} mayEdit={mayEdit} demo={demo}
                    busy={decide.isPending || decideAll.isPending}
                    onDecide={(pid, action) => decide.mutate({
                      pid, action,
                      into_id: merges.find((p) => p.id === pid)?.into_id ?? "" })}
                    onMergeAll={() => decideAll.mutate(
                      merges.filter((p) => p.bulk)
                        .map((p) => ({ id: p.id, into_id: p.into_id })))}
                    onOpen={setOpen} />
      )}

      {recent.length > 0 && (
        <details className="card disclosure">
          <summary>
            <b style={{ fontSize: 13 }}>Recent changes</b>
            <span className="mut" style={{ fontSize: 12, marginLeft: 8 }}>
              {recent.length} change{recent.length === 1 ? "" : "s"} · undo here</span>
          </summary>
          <table style={{ marginTop: ".4rem" }}>
            <tbody>
              {recent.map((r) => (
                <tr key={r.id}>
                  <td className="mut" style={{ width: "1%", whiteSpace: "nowrap" }}>
                    {mmdd(r.at)}</td>
                  <td>
                    {/* the change in the server's words; the bank line
                        only when it is neither name */}
                    <b>{r.label}</b>
                    {r.line && (
                      <span className="mut" style={{ fontSize: 12 }}>
                        {" "}· {r.line}</span>
                    )}
                  </td>
                  <td style={{ width: "1%" }}>
                    {mayEdit && (
                      <button disabled={demo || (undo.isPending && undo.variables === r.id)}
                              onClick={() => undo.mutate(r.id)}>undo</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      <div className="card">
        {list.isPending && <p className="mut">loading…</p>}
        <StaleLine query={list} />
        {list.data === undefined && list.isError && (
          <FailedLine q={list} what="merchants" />)}
        {/* "no match" only from a list that answered */}
        {rows.length === 0 && list.data !== undefined && (
          <p className="sub">{q || flt !== "all" ? EMPTY_LINES.filtered
                                                 : EMPTY_LINES.merchants}</p>
        )}
        {(q ? rows : more.shown).map((m) => (
          <div key={m.display} role="button" tabIndex={0}
               className={"mrow" + (open === m.display ? " on" : "")}
               onClick={() => setOpen(open === m.display ? null : m.display)}
               onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") {
                 e.preventDefault(); setOpen(open === m.display ? null : m.display); } }}>
            <MerchantAvatar name={m.display} logo={m.logo} size={28} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="nm">
                {m.display}
                {/* the merchant row's own facts: kind (a marketplace or
                    payment app is not a shop), outlet of a brand */}
                {m.kind && m.kind !== "merchant" && (
                  <span className="pill m" style={{ marginLeft: 6 }}>{catLabel(m.kind)}</span>
                )}
                {m.parent && (
                  <span className="mut" style={{ marginLeft: 6, fontSize: 12 }}
                        title="a fuel-arm outlet of this brand">outlet of {m.parent}</span>
                )}
              </div>
              <div className="sb">
                {/* the total with its direction, in the server's words */}
                {m.rows.toLocaleString()} row{m.rows === 1 ? "" : "s"} · {m.total_label} ·{" "}
                {mmdd(m.first)} – {mmdd(m.last)}
                {variantCount(m) > 1 && ` · ${variantCount(m)} source names`}
                {(m.business_rows ?? 0) > 0 &&
                  ` · ${m.business_rows} business row${m.business_rows === 1 ? "" : "s"}`}
              </div>
            </div>
            <span className="mut" aria-hidden="true">›</span>
          </div>
        ))}
        {!q && <ShowAllButton {...more} noun="merchants" />}
        {/* capped is judged on what the server sent, before the filter:
            a filter over a capped window can come back short or empty
            while the merchants it wants sit past the cap */}
        {(list.data?.merchants.length ?? 0) >= limit && !q && more.all && (
          <p className="sub" style={{ marginTop: ".6rem" }}>
            Showing the {limit} most frequent — search to find any merchant.
          </p>
        )}
      </div>

      {open && (
        <MerchantPanel display={open} row={openRow} mayEdit={mayEdit} demo={demo}
                       busy={rename.isPending}
                       onClose={() => setOpen(null)}
                       onRename={(to) => rename.mutate({ display: open, to })} />
      )}
    </>
  );
}

/** The proposer's queue: pairs the ledger thinks are one business, one
 *  line each. The reasons and each side's own facts, and what joining them makes, sit behind "details" — the
 *  decision is usually made on the two names alone. */
function NeedsALook({ merges, mayEdit, demo, busy, onDecide, onMergeAll, onOpen }: {
  merges: MergeProposal[]; mayEdit: boolean; demo: boolean; busy: boolean;
  onDecide: (pid: string, action: "approve" | "reject") => void;
  onMergeAll: () => void;
  onOpen: (display: string) => void;
}) {
  const [why, setWhy] = useState<string | null>(null);
  const n = merges.length;
  // "Merge all" covers only the offers where one name is the other cut
  // short (the server's `bulk`); two names that merely share a word are
  // merged one at a time
  const nBulk = merges.filter((p) => p.bulk).length;
  return (
    <details className="card disclosure" open>
      <summary>
        <b style={{ fontSize: 13 }}>Needs a look</b>
        <span className="mut" style={{ fontSize: 12, marginLeft: 8 }}>
          {n} pair{n === 1 ? "" : "s"} look{n === 1 ? "s" : ""} like one merchant</span>
      </summary>
      <div className="sub" style={{ margin: ".3rem 0 .4rem", display: "flex",
                                    gap: ".8rem", alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ flex: 1 }}>
          Merge joins them (undo under Recent changes); Not the same keeps
          them apart for good. Nothing is merged unless you say so.</span>
        {mayEdit && nBulk > 1 && (
          <button disabled={demo || busy} onClick={onMergeAll}
                  title="the offers where one name is the other cut short">
            Merge all {nBulk} cut-off names</button>
        )}
      </div>
      {merges.map((p) => (
        <div key={p.id} className="offer">
          <div style={{ flex: "1 1 16rem", minWidth: 0 }}>
            <div>
              <MerchantAvatar name={p.from} logo={p.from_logo} size={18} />{" "}
              <b>{p.from}</b>
              <span className="mut" style={{ fontSize: 12 }}> · {p.from_facts?.rows ?? p.from_rows ?? "?"}</span>
              <span className="mut"> → </span>
              <MerchantAvatar name={p.into} logo={p.into_logo} size={18} />{" "}
              <b>{p.into}</b>
              <span className="mut" style={{ fontSize: 12 }}> · {p.into_facts?.rows ?? p.into_rows ?? "?"}</span>
            </div>
            <div className="sb">
              {p.caution && (
                <span style={{ color: "var(--amber)" }}>{p.caution} · </span>)}
              {p.signals[0]}
              {" "}<button type="button" className="linkish"
                           onClick={() => setWhy(why === p.id ? null : p.id)}>
                {why === p.id ? "less" : "details"}</button>
            </div>
          </div>
          {mayEdit && (
            <div className="row-actions" style={{ whiteSpace: "nowrap" }}>
              <button className="pri" disabled={demo || busy}
                      onClick={() => onDecide(p.id, "approve")}>Merge</button>
              <button disabled={demo || busy}
                      onClick={() => onDecide(p.id, "reject")}>Not the same</button>
            </div>
          )}
          {why === p.id && (
            <div style={{ fontSize: 12.5, flex: "1 1 100%", minWidth: 0 }}>
              <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
                {p.signals.slice(1).map((sg) => <li key={sg}>{sg}</li>)}
                {p.supports.map((sp) => <li key={sp} className="mut">{sp}</li>)}
              </ul>
              <div className="offer-sum">
                {/* the side being folded in is usually the newcomer, but a
                    long-lived spelling with fewer rows can lose too */}
                <OfferSide tag={(p.from_facts?.first ?? "") > (p.into_facts?.first ?? "")
                                  ? "New detection" : "Other spelling"}
                           tone="new" name={p.from}
                           facts={p.from_facts} samples={p.from_samples}
                           onOpen={() => onOpen(p.from)} />
                <div className="offer-op" aria-hidden="true">+</div>
                <OfferSide tag="History" tone="hist" name={p.into_name ?? p.into}
                           facts={p.into_facts} samples={p.into_samples}
                           onOpen={() => onOpen(p.into_name ?? p.into)} />
                <div className="offer-op" aria-hidden="true">=</div>
                <OfferMerged p={p} />
              </div>
            </div>
          )}
        </div>
      ))}
    </details>
  );
}

/** One side of an offer: what the ledger holds under that name today, so
 *  the two can be told apart before they are joined. The name opens the
 *  merchant's panel for everything else. */
function OfferSide({ tag, tone, name, facts, samples, onOpen }: {
  tag: string; tone: "new" | "hist"; name: string;
  facts?: MergeSideFacts | null; samples: string[]; onOpen: () => void;
}) {
  return (
    <div className={"offer-box " + tone}>
      <div className="offer-tag">{tag}</div>
      <div className="nm">
        <button type="button" className="linkish" onClick={onOpen}><b>{name}</b></button>
      </div>
      {facts ? (
        <>
          <div className="sb">
            {facts.rows.toLocaleString()} row{facts.rows === 1 ? "" : "s"} · {money(facts.total)} ·{" "}
            {facts.first === facts.last
              ? mmdd(facts.first) : `${mmdd(facts.first)} – ${mmdd(facts.last)}`}
          </div>
          <div className="sb">
            {[facts.rows > 1 && facts.typical != null && `usually ${money(facts.typical)}`,
              facts.category && catLabel(facts.category), facts.city,
              facts.accounts.join(", ")].filter(Boolean).join(" · ")}
          </div>
          <table style={{ marginTop: ".4rem" }}>
            <tbody>
              {facts.recent.map((r, k) => (
                <tr key={k}>
                  <td className="mut" style={{ width: "1%", whiteSpace: "nowrap" }}>
                    {mmdd(r.date)}</td>
                  <td style={{ fontFamily: "ui-monospace, monospace", fontSize: 11.5 }}>
                    {r.line}</td>
                  <td className="num" style={{ width: "1%", whiteSpace: "nowrap" }}>
                    {money$(r.amount, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <div className="sb" style={{ fontFamily: "ui-monospace, monospace" }}>
          {samples.join("  ·  ")}
        </div>
      )}
    </div>
  );
}

/** What approving makes: one merchant under the surviving name, holding
 *  both sides' rows. The figures are the server's (`merged`): it adds the
 *  two boxes beside it and signs the change, so nothing is summed here
 *  (the phone prints the same). */
function OfferMerged({ p }: { p: MergeProposal }) {
  const m = p.merged, i = p.into_facts;
  return (
    <div className="offer-box merged">
      <div className="offer-tag">After merge</div>
      <div className="nm">
        <MerchantAvatar name={p.into} logo={p.into_logo} size={18} /> {p.into}
      </div>
      {m && (
        <>
          <div className="sb">
            {m.rows.toLocaleString()} rows <span className="delta">{m.rows_delta}</span> ·{" "}
            {money(m.total)} <span className="delta">{m.total_delta}</span>
          </div>
          <div className="sb">
            {m.first && m.last && `${mmdd(m.first)} – ${mmdd(m.last)}`}
            {i?.category && ` · ${catLabel(i.category)}`}
          </div>
        </>
      )}
      <div className="sb" style={{ marginTop: ".4rem" }}>
        “{p.from}” stops being its own merchant: its rows, and any later
        charge under that name, file here.
      </div>
      <div className="sb" style={{ marginTop: ".35rem" }}>
        Undo any time under Recent changes.</div>
    </div>
  );
}

/** One merchant, opened beside the list: facts, category, source names,
 *  where it has been seen — and the two actions. A chain lists several
 *  places; an online merchant lists none and says so. */
function MerchantPanel({ display, row, mayEdit, demo, busy, onClose, onRename }: {
  display: string; row?: Row; mayEdit: boolean; demo: boolean; busy: boolean;
  onClose: () => void; onRename: (to: string) => void;
}) {
  const [mode, setMode] = useState<"view" | "rename" | "merge">("view");
  const [draft, setDraft] = useState(display);
  const [pick, setPick] = useState("");
  useEffect(() => { setMode("view"); setDraft(display); setPick(""); }, [display]);
  // Focus moves into the panel and stays there until Escape or × closes
  // it, then returns to the merchant that opened it. Not modal: the list
  // beside it still takes a click, which opens another merchant here.
  const panel = useRef<HTMLElement | null>(null);
  useDialog(panel, { onClose });
  const d = useQuery({ queryKey: ["merchant-detail", display],
                       queryFn: () => api.merchantDetail(display) });
  // the merge picker searches the catalog itself, so the target is always
  // a merchant that exists — the long tail included
  const [pickQ, setPickQ] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setPickQ(pick.trim()), 250);
    return () => window.clearTimeout(t);
  }, [pick]);
  const cands = useQuery({ queryKey: ["merchants", pickQ, "count"],
                           queryFn: () => api.merchantCatalog(pickQ, "count"),
                           enabled: mode === "merge" && pickQ.length > 0,
                           placeholderData: (prev) => prev });
  const options = (cands.data?.merchants ?? [])
    .filter((m) => m.display !== display).slice(0, 8);
  const m: MerchantDetail | undefined = d.data ?? undefined;
  // Without coordinates the link searches for the PLACE, never the name:
  // OpenStreetMap's search matches a business only under the exact name
  // its mappers gave it, so a ledger name in the query finds nothing —
  // while a street address, or the town alone, always lands somewhere.
  const mapHref = (l: MerchantDetail["locations"][number]) =>
    l.lat != null && l.lon != null
      ? `https://www.openstreetmap.org/?mlat=${l.lat}&mlon=${l.lon}#map=17/${l.lat}/${l.lon}`
      : `https://www.openstreetmap.org/search?query=${encodeURIComponent(
          [l.address, l.city, l.region, l.postal].filter(Boolean).join(", "))}`;
  const place = (l: MerchantDetail["locations"][number]) =>
    [l.address, [l.city, l.region].filter(Boolean).join(", "), l.postal]
      .filter(Boolean).join(" · ");
  const txnHref = "/transactions?" + new URLSearchParams({ q: display });
  const rows = m?.rows ?? row?.rows;
  const totalLabel = m?.total_label ?? row?.total_label;
  const first = m?.first ?? row?.first;
  const last = m?.last ?? row?.last;
  return (
    <aside className="side-panel" role="dialog" aria-label={`${display} details`}
           ref={panel} tabIndex={-1}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <MerchantAvatar name={display} logo={m?.logo ?? row?.logo} size={40} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 17, overflowWrap: "anywhere" }}>{display}</h2>
          <div className="mut" style={{ fontSize: 12 }}>
            {m?.parent && <>outlet of {m.parent} · </>}
            {m?.kind && m.kind !== "merchant" && <>{catLabel(m.kind)} · </>}
            {rows != null && <>{rows.toLocaleString()} row{rows === 1 ? "" : "s"} · {totalLabel}</>}
            {first && last && <> · {mmdd(first)} – {mmdd(last)}</>}
          </div>
        </div>
        <button type="button" className="close" aria-label="close" onClick={onClose}>×</button>
      </div>

      {mayEdit && (
        <div className="row-actions" style={{ marginTop: ".8rem" }}>
          {mode === "view" && (
            <>
              <button disabled={demo} onClick={() => setMode("rename")}>Rename</button>
              <button disabled={demo} onClick={() => setMode("merge")}>Merge into…</button>
              <Link className="btn-link" to={txnHref}>open in Transactions →</Link>
            </>
          )}
          {mode === "rename" && (
            <>
              <input autoFocus value={draft} aria-label="new name"
                     onChange={(e) => setDraft(e.target.value)}
                     onKeyDown={(e) => {
                       // a held or repeated Enter must not send the rename twice
                       if (e.key === "Enter" && draft.trim() && draft.trim() !== display
                           && !demo && !busy) onRename(draft.trim());
                       if (e.key === "Escape") { e.stopPropagation(); setMode("view"); }
                     }}
                     style={{ width: "min(18rem, 100%)" }} />
              <button className="pri"
                      disabled={demo || busy || !draft.trim() || draft.trim() === display}
                      onClick={() => onRename(draft.trim())}>Save</button>
              <button onClick={() => setMode("view")}>Cancel</button>
            </>
          )}
          {mode === "merge" && (
            <div style={{ width: "100%" }}>
              <div style={{ display: "flex", gap: ".4rem", alignItems: "center", flexWrap: "wrap" }}>
                <input autoFocus value={pick} placeholder="merge into which merchant?"
                       aria-label="merge into" onChange={(e) => setPick(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setMode("view"); } }}
                       style={{ width: "min(18rem, 100%)" }} />
                <button onClick={() => setMode("view")}>Cancel</button>
              </div>
              <div className="sub" style={{ marginTop: ".3rem" }}>
                Every row now shown as {display} moves under the merchant you pick. Undoable.
              </div>
              <div style={{ display: "flex", gap: ".35rem", flexWrap: "wrap", marginTop: ".4rem" }}>
                {options.map((o) => (
                  <button key={o.display} type="button" className="chip" disabled={demo || busy}
                          onClick={() => onRename(o.display)}>
                    <MerchantAvatar name={o.display} logo={o.logo} size={14} />{" "}
                    {o.display}
                    <span className="mut" style={{ fontSize: 11 }}> · {o.rows}</span>
                  </button>
                ))}
                {/* "no match" only from a search that answered */}
                {pickQ && cands.isError && (
                  <FailedLine q={cands} what="the merchants" />)}
                {pickQ && cands.isSuccess && options.length === 0 && (
                  <span className="sub">No merchant matches that.</span>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {d.isPending && <div className="sub" style={{ marginTop: "1rem" }}>loading…</div>}
      {d.isError && <FailedLine q={d} what="this merchant" />}
      {m && (
        <div className="txn-detail" style={{ marginTop: "1rem" }}>
          <div className="mut" style={{ fontSize: 11, textTransform: "uppercase" }}>About</div>
          <dl className="kv" style={{ marginTop: 6 }}>
            {m.website && <><dt>site</dt><dd>
              <a href={/^https?:/.test(m.website) ? m.website : "https://" + m.website}
                 target="_blank" rel="noopener noreferrer">{m.website}</a></dd></>}
            {m.phone && <><dt>phone</dt><dd><a href={"tel:" + m.phone}>{m.phone}</a></dd></>}
            {m.mcc && <><dt>merchant code</dt><dd>MCC {m.mcc}</dd></>}
            <dt>category</dt><dd>
              {m.category ? catLabel(m.category) : <span className="mut">—</span>}
              {/* what the rule does, in the server's words */}
              {m.rule?.label && <span className="mut" style={{ fontSize: 12 }}>
                {" "}· {m.rule.label}</span>}
            </dd>
            <dt>source names</dt><dd className="mut" style={{ fontSize: 12 }}>
              {m.variants.join(" · ")}</dd>
          </dl>
          <div className="mut" style={{ fontSize: 11, textTransform: "uppercase",
                                        marginTop: ".9rem" }}>Where</div>
          {m.locations.length === 0 ? (
            <div className="sub" style={{ marginTop: 4 }}>
              No location came through for this merchant — online, or the bank
              didn't say.</div>
          ) : (
            <table style={{ marginTop: 4 }}><tbody>
              {m.locations.map((l, i) => (
                <tr key={i}>
                  <td>{place(l) || <span className="mut">unnamed place</span>}</td>
                  <td className="num mut" style={{ whiteSpace: "nowrap" }}>
                    {l.n} visit{l.n === 1 ? "" : "s"} · last {mmdd(l.last)}</td>
                  <td style={{ width: "1%" }}>
                    {(l.lat != null || l.address || l.city || l.postal) && (
                      <a href={mapHref(l)} target="_blank" rel="noopener noreferrer">map</a>
                    )}</td>
                </tr>
              ))}
            </tbody></table>
          )}
        </div>
      )}
    </aside>
  );
}
