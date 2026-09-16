// Transactions: month browse, universal
// search with account/category/date-range filters, result summary line,
// pagination (100/page), merchant → recurring-history links, inline
// recategorize + biz toggle.
import { useQuery } from "@tanstack/react-query";
import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import TxnTable from "../components/TxnTable";
import { api, mmdd, money as money$, catLabel } from "../api/client";
import { RANGES, ledgerRangeFrom } from "../components/RangePicker";
const money = (v: number | null | undefined) => money$(v, true);  // cents on transaction/recurring surfaces


const PER_PAGE = 100;


export default function Transactions() {
  const now = new Date();
  const [sp] = useSearchParams();         // /transactions?… deep links
  // honor the full query the /transactions→/app redirect forwards
  // (q, cat, y, m, date range) — not just acct
  const [y, setY] = useState(Number(sp.get("y")) || now.getFullYear());
  const [m, setM] = useState(Number(sp.get("m")) || now.getMonth() + 1);
  const [q, setQ] = useState(sp.get("q") ?? "");     // text box
  const [live, setLive] = useState(sp.get("q") ?? "");  // submitted search
  const [acct, setAcct] = useState(sp.get("acct") ?? "");
  const [cat, setCat] = useState(sp.get("cat") ?? "");
  // A verdict BUCKET — Food, Everything else, a custom bucket — is a
  // grouping of the month the ledger stores on no transaction (Food counts
  // food-override rows of merchants filed elsewhere; a custom bucket is a
  // plan label), so the server lists it by name instead of by category.
  // It only ever arrives as a deep link from a plan label.
  const [bucket, setBucket] = useState(sp.get("bucket") ?? "");
  // the day a past Today page was read on: its bucket's rows stop there
  const [asOf, setAsOf] = useState(sp.get("as_of") ?? "");
  // a lens category label's link: the rows that label summed (personal
  // spend, reimbursements netted), not every row filed under the category
  const [counted, setCounted] = useState(sp.get("counted") === "1");
  // whether the view is one MONTH's slice. A bucket always is; a category
  // deep-linked with y&m is that month's category. A category picked in
  // the toolbar is not — it searches all history, as it always has — so
  // this can't be derived from `cat` alone.
  const [monthScope, setMonthScope] = useState(
    !!(sp.get("bucket") || (sp.get("cat") && sp.get("y") && sp.get("m"))));
  const [df, setDf] = useState(sp.get("date_from") ?? "");
  const [dt_, setDt] = useState(sp.get("date_to") ?? "");
  // reimbursement quick filter: linked pairs + awaiting
  // flags only — works in both the month view and searches
  const [reimbF, setReimbF] = useState(sp.get("reimb") === "1");
  const [ownerF, setOwnerF] = useState(sp.get("owner") ?? "");
  // whose money the list shows: household + business (default — business
  // rows carry a named pill), household only, or business only. Business
  // rows never count toward the figures either way; this only decides
  // whether they are LISTED.
  const [scopeF, setScopeF] = useState(sp.get("scope") ?? "");
  // how it was paid (Plaid's payment channel) and checks only — both are
  // searches server-side, like a category or an account
  const [channelF, setChannelF] = useState(sp.get("channel") ?? "");
  const [checksF, setChecksF] = useState(sp.get("checks") === "1");
  const [page, setPage] = useState(1);
  // The three controls people touch constantly stay in the toolbar row;
  // date range, owner and the reimbursement
  // lens live behind "more", which opens automatically when a deep link
  // arrives with one of them already set — otherwise the page would hide the
  // filter that is shaping what you are looking at.
  const [more, setMore] = useState(
    !!(sp.get("date_from") || sp.get("date_to") || sp.get("owner")
       || sp.get("scope") || sp.get("channel") || sp.get("checks")
       || sp.get("reimb") === "1"));
  // month view renders a slice until asked for the rest: a few hundred
  // rows mounted in one pass is this page's own main-thread stall, and the
  // layout simply stops inviting it.
  const MONTH_CHUNK = 60;
  const [shown, setShown] = useState(MONTH_CHUNK);
  // "show all" on a big month mounts a few thousand table cells; grown a
  // chunk at a time — each next chunk waiting for the previous one's rows
  // to actually reach the table — every commit stays near the frame budget
  // instead of landing as one long blocking task
  const [shownTarget, setShownTarget] = useState<number | null>(null);

  // The toolbar's own filters search ALL history. Reaching for one
  // therefore leaves a bucket or a month's category behind rather than
  // silently narrowing the new filter to the month that was on screen —
  // and a bucket listing ignores the toolbar entirely, so a search typed
  // inside one would otherwise look broken.
  const leaveScope = () => {
    setBucket(""); setAsOf(""); setCounted(false); setMonthScope(false);
  };

  // A deep link arriving while this page is already open RETARGETS it:
  // every filter a link can carry is read from the URL and the ones it
  // omits are cleared. Without this the state initialisers above only run
  // on mount, so a second tap — another category label, another month's
  // bucket — would inherit the filter the first one left behind and list
  // the wrong rows under the right heading.
  const spKey = sp.toString();
  useEffect(() => {
    const n = new Date();
    setY(Number(sp.get("y")) || n.getFullYear());
    setM(Number(sp.get("m")) || n.getMonth() + 1);
    setQ(sp.get("q") ?? ""); setLive(sp.get("q") ?? "");
    setAcct(sp.get("acct") ?? ""); setCat(sp.get("cat") ?? "");
    setBucket(sp.get("bucket") ?? ""); setAsOf(sp.get("as_of") ?? "");
    setCounted(sp.get("counted") === "1");
    setMonthScope(!!(sp.get("bucket")
                     || (sp.get("cat") && sp.get("y") && sp.get("m"))));
    setDf(sp.get("date_from") ?? ""); setDt(sp.get("date_to") ?? "");
    setChannelF(sp.get("channel") ?? "");
    setChecksF(sp.get("checks") === "1");
    setReimbF(sp.get("reimb") === "1");
    setOwnerF(sp.get("owner") ?? ""); setScopeF(sp.get("scope") ?? "");
    // same rule as the mount state: a collapsed "more" panel must never
    // silently shape the results, so a link carrying one of its filters
    // opens it
    setMore(!!(sp.get("date_from") || sp.get("date_to") || sp.get("owner")
               || sp.get("scope") || sp.get("channel") || sp.get("checks")
               || sp.get("reimb") === "1"));
    setPage(1); setShown(MONTH_CHUNK);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spKey]);

  // as-you-type: the typed text becomes the live search a beat after the
  // last keystroke (Enter still fires at once). Two characters minimum for
  // the automatic path — one letter matches most of the ledger and the
  // result is noise; placeholderData keeps the previous rows on screen while
  // the next page loads, so the table filters instead of flashing.
  useEffect(() => {
    const want = q.trim();
    if (want === live) return;
    if (want.length > 0 && want.length < 2) return;
    const t = setTimeout(() => {
      setLive(want); setPage(1);
      // typing is an all-history search — leave any bucket / month scope
      // (setters inline rather than the helper: a stable effect body)
      if (want) { setBucket(""); setMonthScope(false); }
    }, 250);
    return () => clearTimeout(t);
  }, [q, live]);

  // a bucket listing is a search-mode result like any other filter: a
  // paged, totalled list rather than the month browse
  const searching = !!(live || acct || cat || bucket || df || dt_
                       || channelF || checksF);
  const params: Record<string, string> = {
    ...(bucket
      // the bucket IS the query — it already names its month, and mixing
      // it with the toolbar's filters would list something the verdict
      // never counted
      ? { bucket, y: String(y), m: String(m), page: String(page),
          ...(asOf ? { as_of: asOf } : {}) }
      : searching
      ? { q: live, acct, cat, date_from: df, date_to: dt_, page: String(page),
          // a month's category: no date range, so the server reads y&m and
          // scopes to that month — which is what a link from a month's
          // lens means, and what the month arrows then re-query
          ...(monthScope ? { y: String(y), m: String(m) } : {}),
          ...(counted && cat ? { counted: "1" } : {}),
          ...(channelF ? { channel: channelF } : {}),
          ...(checksF ? { checks: "1" } : {}) }
      : { y: String(y), m: String(m) }),
    // a bucket or a counted category is the engine's own row set — the
    // lenses below would only empty it under a non-zero chip, so none
    // rides along
    ...(bucket || (counted && cat) ? {} : {
      ...(reimbF ? { reimb: "1" } : {}),
      // owner filter is a month-view lens (search path is owner-agnostic)
      ...(ownerF && !searching ? { owner: ownerF } : {}),
      ...(scopeF ? { scope: scopeF } : {}),
    }),
  };
  const query = useQuery({
    queryKey: ["txns", params],
    queryFn: () => api.transactions(params),
    // keep the last page on screen while the next loads — but never
    // across the month ⇄ search shape switch: a browse answer has no
    // total and a search answer no month sums, so a stale one renders
    // "0 results" over a full table. Only the search path is paged, so
    // that is the honest mode marker (mobile reads the same way).
    placeholderData: (prev, prevQuery) => {
      const prevParams = prevQuery?.queryKey[1] as Record<string, string> | undefined;
      if (!prevParams) return undefined;
      return ("page" in prevParams) === ("page" in params) ? prev : undefined;
    },
  });
  const cats = useQuery({ queryKey: ["cats"], queryFn: api.categories });
  const accts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const owners = useQuery({ queryKey: ["owners"], queryFn: api.owners });
  // count pill on the awaiting-reimbursement door. The business count that
  // sat beside it is gone: the Business tab is where that number lives,
  // and here it read as a claim about the month on screen.
  const reimb = useQuery({ queryKey: ["reimb"], queryFn: api.reimbPending });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // The business link follows the Business TAB: hidden until a business
  // exists (has_business on /api/me), so a household that never runs one
  // is not pointed at a feature built for someone else.
  const bizGated = !me.data?.has_business;
  const awaitingN = reimb.data?.pending.length ?? 0;

  // the arrows move the month a bucket or a month's category is scoped to,
  // not just the browse — so page 1 of the new month, every time
  // (a past day's as-of belongs to its own month, so it does not travel)
  const prev = () => { setPage(1); setAsOf("");
    if (m === 1) { setY(y - 1); setM(12); } else setM(m - 1); };
  const next = () => { setPage(1); setAsOf("");
    if (m === 12) { setY(y + 1); setM(1); } else setM(m + 1); };
  const label = new Date(y, m - 1, 1).toLocaleString("en", { month: "long", year: "numeric" });
  const monthShort = new Date(y, m - 1, 1)
    .toLocaleString("en", { month: "short", year: "numeric" });
  const isCurrent = y === now.getFullYear() && m === now.getMonth() + 1;

  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const acctLabel = acct
    ? accts.data?.accounts.find((a) => a.id === acct)?.name : null;
  const clear = () => {
    setLive(""); setQ(""); setAcct(""); setCat(""); setDf(""); setDt("");
    setChannelF(""); setChecksF(false); setBucket(""); setAsOf("");
    setCounted(false); setMonthScope(false); setPage(1);
  };
  // What the list is narrowed to, said in one chip with its month: a deep
  // link's scope is the part that otherwise goes unsaid, and neither a
  // bucket nor a month's category is shown by any toolbar control.
  // The server's label is used only once it is the label OF THIS bucket —
  // the previous result stays on screen while the next loads — so until
  // then the link's own word stands in: the plan's name for the two
  // standard buckets, and a custom bucket is already its own name.
  const bucketLabel =
    (query.data?.bucket === bucket && query.data?.bucket_label)
    || (bucket === "food" ? "Food"
        : bucket === "other" ? "Everything else" : bucket);
  const scopeChip = bucket
    ? `${bucketLabel} · ${monthShort}`
    : cat && monthScope ? `${catLabel(cat)} · ${monthShort}` : null;
  // ✕ on the chip drops the grouping and keeps the month it named
  const clearScope = () => {
    setBucket(""); setAsOf(""); setCounted(false); setCat("");
    setMonthScope(false); setPage(1);
  };
  // any filter change starts back at page 1
  const set = <T,>(fn: (v: T) => void) => (v: T) => {
    fn(v); setPage(1); leaveScope();
  };
  // count of the filters hiding behind "more", so a collapsed panel can
  // never silently shape the results
  const moreN = [df, dt_, ownerF, scopeF, channelF].filter(Boolean).length
    + (reimbF ? 1 : 0) + (checksF ? 1 : 0);
  const rows = useMemo(() => query.data?.rows ?? [], [query.data]);
  // a stable slice identity lets the memoized table skip re-rendering when
  // this page re-renders for one of its other queries (owners, accounts,
  // reimb counts, /me) resolving — the rows themselves haven't changed
  const monthRows = useMemo(
    () => (searching ? rows : rows.slice(0, shown)),
    [rows, searching, shown]);
  // rendering a few hundred ledger rows in one synchronous pass is this
  // page's measured main-thread stall — deferring the table's copy of the
  // rows lets React time-slice that work (and keep showing the previous
  // rows meanwhile) instead of blocking the frame; everything above the
  // table still updates immediately
  const tableRows = useDeferredValue(monthRows);
  useEffect(() => {
    if (shownTarget == null) return;
    if (searching || shown >= shownTarget) { setShownTarget(null); return; }
    // the table hasn't committed the last chunk yet — this effect runs
    // again when it has (tableRows is what the table actually shows)
    if (tableRows.length < Math.min(shown, shownTarget)) return;
    const id = requestAnimationFrame(
      () => setShown(Math.min(shown + MONTH_CHUNK, shownTarget)));
    return () => cancelAnimationFrame(id);
  }, [shownTarget, shown, searching, tableRows]);

  return (
    <>
      <h1>Transactions</h1>
      {/* placeholderData keeps the last page on screen while refetching —
          which also means a FAILED background refetch (server mid-deploy,
          network blip) would silently show stale rows. Say so instead. */}
      {query.isError && (
        <div className="note bad" style={{ marginTop: 0 }}>
          <span style={{ flex: 1 }}>
            Couldn't refresh — {query.data ? "showing possibly stale data"
              : "nothing loaded"}: {String(query.error)}
          </span>
          <button onClick={() => query.refetch()}>Retry</button>
        </div>
      )}
      {/* HERO. Without this the month view states no total — only searches
          do — and the DEFAULT state of the ledger answers nothing about the
          month on screen. Money out / money in, the count, and the two
          counts that are actually destinations rather than filters. */}
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", gap: "1rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          {/* the month arrows belong to any view that IS a month — the
              browse, a bucket, a month's category — so a plan label's
              rows can be walked back through the year in place */}
          {(!searching || monthScope) && (
            <span style={{ display: "inline-flex", gap: ".35rem", alignItems: "center" }}>
              <button onClick={prev} title="previous month">‹</button>
              <b style={{ minWidth: "9.5rem", textAlign: "center" }}>{label}</b>
              {!isCurrent && <button onClick={next} title="next month">›</button>}
              {!isCurrent && <button title="jump to current month"
                onClick={() => { setPage(1); setY(now.getFullYear());
                                 setM(now.getMonth() + 1); }}>today</button>}
            </span>
          )}
          {!searching ? (
            <>
              {/* the month's cash flow (income / spend / net, the Cash Flow
                  page's definitions) — transfers and card payments are money
                  moved, not money made or spent, so they are not in these */}
              {query.data?.out_sum !== undefined && (
                <span style={{ fontSize: "1.35rem", fontWeight: 700,
                               letterSpacing: "-.01em" }}>
                  <span className="pos">{money(query.data.in_sum ?? 0)}</span>
                  <span className="sub" style={{ fontWeight: 400 }}> in</span>
                  {" · "}
                  {money(query.data.out_sum)}
                  <span className="sub" style={{ fontWeight: 400 }}> out</span>
                  {" · "}
                  <span className={(query.data.net_sum ?? 0) < 0 ? "neg" : "pos"}>
                    {(query.data.net_sum ?? 0) < 0 ? "" : "+"}
                    {money(query.data.net_sum ?? 0)}
                  </span>
                  <span className="sub" style={{ fontWeight: 400 }}> net</span>
                  {query.data.per_day != null && (
                    <>
                      {" · "}
                      <b>{money(query.data.per_day)}</b>
                      <span className="sub" style={{ fontWeight: 400 }}
                            title={`spending over ${query.data.per_day_days} day${
                              query.data.per_day_days === 1 ? "" : "s"}`}>
                        /day</span>
                    </>
                  )}
                </span>
              )}
            </>
          ) : (
            <span style={{ fontSize: "1.35rem", fontWeight: 700,
                           letterSpacing: "-.01em" }}>
              {total}<span className="sub" style={{ fontWeight: 400 }}>
                {" "}result{total === 1 ? "" : "s"}</span>
              {" · "}
              <b className={(query.data?.amount_sum ?? 0) < 0 ? "pos" : ""}>
                {money(-(query.data?.amount_sum ?? 0))}</b>
              <span className="sub" style={{ fontWeight: 400 }}> total</span>
              {/* the total says how much has gone to this merchant; the
                  average says what one visit costs, which is usually the
                  question a merchant search is really asking. Server-side
                  over the WHOLE result set (averaging the visible page would
                  change as you page), and over spending only, so a transfer
                  to the same payee cannot dilute it. Absent when nothing
                  matched counts as spending — $0.00 would read as an
                  average rather than as "no purchases here". */}
              {query.data?.spend_avg != null && (
                <>
                  {" · "}
                  <b>{money(query.data.spend_avg)}</b>
                  <span className="sub" style={{ fontWeight: 400 }}>
                    {" "}avg of {query.data.spend_count}
                  </span>
                </>
              )}
              {/* per DAY over the window asked for — a month's category, a
                  bucket, a date range — beside the per-visit average */}
              {query.data?.per_day != null && (
                <>
                  {" · "}
                  <b>{money(query.data.per_day)}</b>
                  <span className="sub" style={{ fontWeight: 400 }}
                        title={`spending over ${query.data.per_day_days} day${
                          query.data.per_day_days === 1 ? "" : "s"}`}>
                    /day</span>
                </>
              )}
              {/* transaction totals overstate an item search — a mixed box
                  counts the whole charge — so when the term matched Amazon
                  order items, also state the true per-item spend */}
              {query.data?.amazon_items && (
                <>
                  {" · "}
                  <b>{money(query.data.amazon_items.sum)}</b>
                  <span className="sub" style={{ fontWeight: 400 }}>
                    {" "}on {query.data.amazon_items.count} Amazon item
                    {query.data.amazon_items.count === 1 ? "" : "s"}
                    {query.data.amazon_items.unpriced > 0 &&
                      ` (${query.data.amazon_items.unpriced} without a price)`}
                  </span>
                </>
              )}
            </span>
          )}
        </div>
        <div className="sub" style={{ marginTop: ".2rem", display: "flex",
                                      gap: ".9rem", flexWrap: "wrap",
                                      alignItems: "center" }}>
          {!searching ? (
            <span>{rows.length} transaction{rows.length === 1 ? "" : "s"}
              {/* the out/in figures above are household money; say how
                  many listed rows are a business's so the two agree */}
              {(query.data?.biz_count ?? 0) > 0 && (
                <span className="mut"> · {query.data!.biz_count} business
                  (not in the totals)</span>
              )}</span>
          ) : (
            <span>
              in <b style={{ color: "var(--ink)" }}>{acctLabel || "all accounts"}</b>
              {/* a month-scoped category is already named by the chip
                  below, with its month — saying it twice reads as two
                  different filters */}
              {cat && !monthScope && <> · {catLabel(cat)}</>}
              {(df || dt_) && <> · {df ? mmdd(df) : "start"} – {dt_ ? mmdd(dt_) : "now"}</>}
              {" · newest first"}
              {(query.data?.biz_count ?? 0) > 0 && scopeF !== "business" && (
                <span className="mut"> · {query.data!.biz_count} business
                  (not in the total)</span>
              )}
              {/* the term matched an account by name, so that account's
                  rows are in the result. A renamed account found through
                  the BANK'S name states both, or a "CREDIT CARD" search
                  lists rows that show neither word */}
              {(query.data?.account_hits ?? []).map((h) => (
                <span key={h.id} className="mut">
                  {" · includes "}<b style={{ color: "var(--ink)" }}>{h.label}</b>
                  {h.via_bank && <> (the bank calls it “{h.bank_name}”)</>}
                </span>
              ))}
            </span>
          )}
          {/* what a deep link narrowed the list to, and the way out of it:
              a plan label sends a bucket or a month's category, neither of
              which any toolbar control shows */}
          {scopeChip && (
            <span className="pill b">{scopeChip}{" "}
              <button aria-label={`clear ${scopeChip}`} className="chip-x"
                      title="back to the whole month"
                      onClick={clearScope}>✕</button>
            </span>
          )}
          {/* destinations, not filters: inside the filter form they read
              as ways to narrow the list */}
          {awaitingN > 0 && (
            <Link to="/reimburse" style={{ textDecoration: "none" }}
                  title="charges flagged as reimbursement-expected, waiting to be matched">
              <span className="pill a" style={{ marginRight: ".3rem" }}>{awaitingN}</span>
              awaiting reimbursement</Link>
          )}
          {searching && (
            <button type="button" onClick={clear} style={{ marginLeft: "auto" }}
                    title="back to the monthly view">✕ clear</button>
          )}
        </div>
      </div>

      <div className="card" style={{ display: "flex", gap: ".6rem", alignItems: "center", flexWrap: "wrap" }}>
        <form style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                       alignItems: "center", width: "100%" }}
              onSubmit={(e) => { e.preventDefault(); setLive(q.trim());
                                 setPage(1); if (q.trim()) leaveScope(); }}>
          <input placeholder="search all accounts & history…" size={20} value={q}
                 onChange={(e) => setQ(e.target.value)} />
          <select value={acct} style={{ maxWidth: "13rem" }}
                  onChange={(e) => set(setAcct)(e.target.value)}>
            <option value="">all accounts</option>
            {accts.data?.accounts.filter((a) => a.balance_current !== null).map((a) => (
              <option key={a.id} value={a.id}>
                {a.institution_name} {a.name}{a.mask ? ` ${a.mask}` : ""}
              </option>
            ))}
            {(accts.data?.accounts.some((a) => a.balance_current === null)) && (
              <optgroup label="── Archived / closed ──">
                {accts.data!.accounts.filter((a) => a.balance_current === null).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.institution_name} {a.name}{a.mask ? ` ${a.mask}` : ""}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <select value={cat} style={{ maxWidth: "11rem" }}
                  onChange={(e) => set(setCat)(e.target.value)}>
            <option value="">all categories</option>
            {cats.data?.categories.map((c) => (
              <option key={c} value={c}>{catLabel(c)}</option>
            ))}
          </select>
          <button className="pri">🔎 Search</button>
          <button type="button" aria-expanded={more}
                  style={moreN ? { borderColor: "var(--blue)",
                                   color: "var(--blue)" } : undefined}
                  title="date range, owner, business rows, reimbursement-only"
                  onClick={() => setMore(!more)}>
            more{moreN ? ` (${moreN})` : ""} {more ? "▴" : "▾"}</button>

          {/* the rarely-touched filters. The count on the button above is
              load-bearing: a collapsed panel must never silently shape the
              results. */}
          {more && (
          <span className="daterange"
                style={{ flexBasis: "100%", display: "flex", gap: ".7rem",
                         alignItems: "center", flexWrap: "wrap",
                         marginTop: ".2rem", paddingTop: ".55rem",
                         borderTop: "1px dashed var(--line)" }}>
            {/* the site-wide 3m … All vocabulary — one click instead of
                two typed dates; a typed pair matching none of them lights
                nothing, and All is the no-date-filter state */}
            <span style={{ display: "inline-flex", gap: ".35rem", flexWrap: "wrap" }}>
              {RANGES.map((r) => {
                const from = ledgerRangeFrom(r, now);
                const on = df === from && !dt_;
                return (
                  <button key={r} type="button"
                          className={`pill m${on ? " pri" : ""}`}
                          title={r === "all" ? "no date filter"
                                 : `${r} — from the first of the month`}
                          onClick={() => { setDf(from); setDt(""); setPage(1);
                                           leaveScope(); }}>
                    {r === "all" ? "All" : r}</button>
                );
              })}
            </span>
            <label className="mut" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
              from <input type="date" value={df}
                          onChange={(e) => set(setDf)(e.target.value)} /></label>
            <label className="mut" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
              to <input type="date" value={dt_}
                        onChange={(e) => set(setDt)(e.target.value)} /></label>
            {(owners.data?.owners.length ?? 0) > 0 && (
              <select value={ownerF} style={{ maxWidth: "9rem", fontSize: 12 }}
                      title="owner lens — whose money (month view)"
                      onChange={(e) => { leaveScope(); setOwnerF(e.target.value); setPage(1); }}>
                <option value="">all owners</option>
                {owners.data!.owners.map((o) => (
                  <option key={o} value={o}>{o}</option>))}
              </select>
            )}
            {!bizGated && (
              <select value={scopeF} style={{ maxWidth: "12rem", fontSize: 12 }}
                      title="business-entity accounts are listed with a named pill; narrow to household only or business only"
                      onChange={(e) => { setScopeF(e.target.value); setPage(1);
                                         leaveScope(); }}>
                <option value="">household + business</option>
                <option value="personal">household only</option>
                <option value="business">business only</option>
              </select>
            )}
            <select value={channelF} style={{ maxWidth: "9rem", fontSize: 12 }}
                    title="how it was paid — the bank's payment channel"
                    onChange={(e) => { setChannelF(e.target.value); setPage(1);
                                       leaveScope(); }}>
              <option value="">any channel</option>
              <option value="in store">in store</option>
              <option value="online">online</option>
              <option value="other">other</option>
            </select>
            <button type="button"
                    className={checksF ? "pri" : ""}
                    style={{ fontSize: 12 }}
                    title="only transactions the bank reports as checks"
                    onClick={() => { setChecksF(!checksF); setPage(1);
                                     leaveScope(); }}>
              checks only{checksF ? " ✓" : ""}</button>
            <button type="button"
                    className={reimbF ? "pri" : ""}
                    style={{ fontSize: 12 }}
                    title="show only reimbursement activity — matched pairs and awaiting flags"
                    onClick={() => { setReimbF(!reimbF); setPage(1);
                                     leaveScope(); }}>
              reimbursement activity only{reimbF ? " ✓" : ""}</button>
          </span>
          )}
        </form>
      </div>

      {/* the result summary is the hero above — a second one here would
          repeat the same counts, the same total and "page N of M" */}

      <div className="card">
        {query.isPending && <p className="mut">loading…</p>}
        {query.data && rows.length === 0 && (
          <p className="sub">{searching
            ? "No matches anywhere — try a different search."
            : "No transactions for this month/filter."}</p>
        )}
        {/* the ledger is the one long, date-ordered list in the app, so it
            is the one surface where a day divider is structure */}
        {query.data && rows.length > 0 && (
          <TxnTable rows={tableRows} showAccount={!acct} dayGroups />
        )}
        {!searching && rows.length > monthRows.length && (
          <div className="sub" style={{ marginTop: ".6rem", display: "flex",
                                        gap: ".8rem", alignItems: "center" }}>
            <span>{monthRows.length} of {rows.length} shown</span>
            <button onClick={() => setShown(shown + MONTH_CHUNK)}>show more</button>
            <button onClick={() => setShownTarget(rows.length)}>show all</button>
          </div>
        )}
      </div>

      {searching && totalPages > 1 && (
        <div className="card" style={{ marginTop: ".6rem", display: "flex", gap: ".8rem",
                                       alignItems: "center", justifyContent: "center" }}>
          {page > 1 && (
            <>
              <button onClick={() => setPage(1)}>« first</button>
              <button onClick={() => setPage(page - 1)}>‹ prev</button>
            </>
          )}
          <span className="mut">page {page} of {totalPages}</span>
          {page < totalPages && (
            <>
              <button onClick={() => setPage(page + 1)}>next ›</button>
              <button onClick={() => setPage(totalPages)}>last »</button>
            </>
          )}
        </div>
      )}
    </>
  );
}
