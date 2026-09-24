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
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { api, errText, mmdd, money as money$, catLabel } from "../api/client";
import type { MerchantDetail, MergeProposal, MergeSideFacts } from "../api/client";
import { removeFromList } from "../api/cache";
import { canEdit } from "../role";
import MerchantAvatar from "../components/MerchantAvatar";
const money = (v: number | null | undefined) => money$(v, false);

// the server caps the catalog at its 200 most frequent merchants;
// the long tail is reachable only through search
const CATALOG_LIMIT = 200;

// the server caps the `variants` array it returns, so the array's length
// is NOT the number of source names — a merchant with 3,000 spellings
// would claim the cap. `variant_count` is the true count; fall back to
// the array only for an older server that doesn't send it.
const variantCount = (m: { variants: string[]; variant_count?: number }) =>
  m.variant_count ?? m.variants.length;

type Catalog = Awaited<ReturnType<typeof api.merchantCatalog>>;
type Row = Catalog["merchants"][number];

// the server orders by count or name; the two spend/recency orders are
// cut from the loaded page here
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
// a name the aggregator never resolved: digits run, a terminal's * or #,
// or an all-caps string that no one would call a name
export const looksUnnamed = (name: string) =>
  /\d{3,}|[*#]/.test(name) || (/[A-Z]{4,}/.test(name) && name === name.toUpperCase());

const toast = (msg: string) =>
  window.dispatchEvent(new CustomEvent("oiko-toast", { detail: msg }));

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
  const key = ["merchants", q, serverOrder(order)];
  const list = useQuery({ queryKey: key,
                          queryFn: () => api.merchantCatalog(q, serverOrder(order)),
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
    mutationFn: ({ pid, action }: { pid: string; action: "approve" | "reject" }) =>
      api.merchantMerge(pid, action),
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
    onError: (e) => toast(`Couldn't decide — ${errText(e)}`),
  });
  // "merge all" walks the queue one decision at a time: each is its own
  // journal entry, so a wrong one is undone alone
  const decideAll = useMutation({
    mutationFn: async (pids: string[]) => {
      let n = 0;
      for (const pid of pids) { await api.merchantMerge(pid, "approve"); n += 1; }
      return n;
    },
    onSuccess: (n) => done(`Merged ${n} pair${n === 1 ? "" : "s"}.`),
    onError: (e) => { done(`Stopped — ${errText(e)}`); },
  });

  // the server owns count/alpha; spend and recency are cut here, and the
  // filters too. Memoised: the page re-renders on every keystroke and
  // re-sorting up to five hundred rows each time is work it does not need.
  const rows = useMemo(() => {
    let r = list.data?.merchants ?? [];
    if (flt === "oneoff") r = r.filter((m) => m.rows === 1);
    else if (flt === "unnamed") r = r.filter((m) => looksUnnamed(m.display));
    else if (flt === "month") {
      const m0 = new Date().toISOString().slice(0, 7);
      r = r.filter((m) => (m.last ?? "").slice(0, 7) >= m0);
    }
    if (order === "alpha") r = [...r].sort((a, b) => a.display.localeCompare(b.display));
    else if (order === "spend") r = [...r].sort((a, b) => b.total - a.total);
    else if (order === "recent") r = [...r].sort((a, b) => (b.last ?? "").localeCompare(a.last ?? ""));
    return r;
  }, [list.data, order, flt]);
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
          <div style={{ display: "flex", gap: ".35rem", flexWrap: "wrap" }}>
            {FILTERS.map(([v, label]) => (
              <button key={v} type="button" className={"chip" + (flt === v ? " on" : "")}
                      onClick={() => setFlt(v)}>{label}</button>
            ))}
          </div>
        </div>
      </div>

      {merges.length > 0 && (
        <NeedsALook merges={merges} mayEdit={mayEdit} demo={demo}
                    busy={decide.isPending || decideAll.isPending}
                    onDecide={(pid, action) => decide.mutate({ pid, action })}
                    onMergeAll={() => decideAll.mutate(merges.map((p) => p.id))}
                    onOpen={setOpen} />
      )}

      {recent.length > 0 && (
        <details className="card fold">
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
                    <span className="mut">{r.raw_merchant}</span>
                    {" → "}<b>{r.to_canonical}</b>
                    {r.from_canonical && (
                      <span className="mut" style={{ fontSize: 12 }}>
                        {" "}(was {r.from_canonical})</span>
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
        {list.isError && (
          <div className="note bad">Couldn't load: {String(list.error)}</div>
        )}
        {rows.length === 0 && !list.isPending && (
          <p className="sub">No merchants match that.</p>
        )}
        {rows.map((m) => (
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
                {m.rows} row{m.rows === 1 ? "" : "s"} · {money(m.total)} ·{" "}
                {mmdd(m.first)} – {mmdd(m.last)}
                {variantCount(m) > 1 && ` · ${variantCount(m)} source names`}
                {(m.business_rows ?? 0) > 0 &&
                  ` · ${m.business_rows} business row${m.business_rows === 1 ? "" : "s"}`}
              </div>
            </div>
            <span className="mut" aria-hidden="true">›</span>
          </div>
        ))}
        {rows.length >= CATALOG_LIMIT && !q && flt === "all" && (
          <p className="sub" style={{ marginTop: ".6rem" }}>
            Showing the {CATALOG_LIMIT} most frequent — search to find any merchant.
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
  return (
    <details className="card fold" open>
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
        {mayEdit && n > 1 && (
          <button disabled={demo || busy} onClick={onMergeAll}>Merge all {n}</button>
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
            {facts.rows} row{facts.rows === 1 ? "" : "s"} · {money(facts.total)} ·{" "}
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
 *  both sides' rows. The sums are the two boxes beside it, added. */
function OfferMerged({ p }: { p: MergeProposal }) {
  const f = p.from_facts, i = p.into_facts;
  const dates = [f?.first, f?.last, i?.first, i?.last].filter(Boolean).sort() as string[];
  return (
    <div className="offer-box merged">
      <div className="offer-tag">After merge</div>
      <div className="nm">
        <MerchantAvatar name={p.into} logo={p.into_logo} size={18} /> {p.into}
      </div>
      {f && i && (
        <>
          <div className="sb">
            {f.rows + i.rows} rows <span className="delta">+{f.rows}</span> ·{" "}
            {money(f.total + i.total)} <span className="delta">+{money(f.total)}</span>
          </div>
          <div className="sb">
            {mmdd(dates[0])} – {mmdd(dates[dates.length - 1])}
            {i.category && ` · ${catLabel(i.category)}`}
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
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
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
  const total = m?.total ?? row?.total;
  const first = m?.first ?? row?.first;
  const last = m?.last ?? row?.last;
  return (
    <aside className="side-panel" aria-label={`${display} details`}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <MerchantAvatar name={display} logo={m?.logo ?? row?.logo} size={40} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 17, overflowWrap: "anywhere" }}>{display}</h2>
          <div className="mut" style={{ fontSize: 12 }}>
            {m?.parent && <>outlet of {m.parent} · </>}
            {m?.kind && m.kind !== "merchant" && <>{catLabel(m.kind)} · </>}
            {rows != null && <>{rows} row{rows === 1 ? "" : "s"} · {money(total)}</>}
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
                {pickQ && !cands.isPending && options.length === 0 && (
                  <span className="sub">No merchant matches that.</span>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {d.isPending && <div className="sub" style={{ marginTop: "1rem" }}>loading…</div>}
      {d.isError && <div className="note bad" style={{ marginTop: "1rem" }}>
        Couldn't load: {String(d.error)}</div>}
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
              {m.rule && <span className="mut" style={{ fontSize: 12 }}>
                {" "}· {m.rule.source === "user" ? "your rule" : `${m.rule.source} rule`}
                {" "}→ {catLabel(m.rule.category_primary)}</span>}
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
