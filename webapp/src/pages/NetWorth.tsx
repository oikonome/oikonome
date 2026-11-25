// Net Worth — 3 stat tiles, a trend line with estimated shading,
// class/institution donuts, retirement & Coinbase P/L tables, and the
// property table, which is editable here by the owner (Settings does not
// own manual_assets).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { Link } from "react-router";
import { SETTINGS_SAVES, saveSettings } from "../api/cache";
import { changeWords, nameKeys, parseMoneyDraft, rebaseNamedSave, signedPct }
  from "../moneymath";
import { api, errText,  money, type FeesReport, type ManualAsset, type NetWorthReport,
         type InvestedGroup, type TrendWindow, catLabel } from "../api/client";
import AreaChart from "../components/AreaChart";
import Bars from "../components/Bars";
import { FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../components/Loaded";
import Donut from "../components/Donut";
import { DEFAULT_RANGE, RangePicker, overCaption, type Range }
  from "../components/RangePicker";
import { canEdit, isViewer } from "../role";
import PageSkeleton from "../components/PageSkeleton";
import Field from "../components/Field";
import Note from "../components/Note";

// For example '2001-02-03T04:05:06' → '02/03/01 04:05' — the cache stamp, in the app's
// date order instead of the raw timestamp
const builtAt = (s: string) =>
  /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(s)
    ? `${s.slice(5, 7)}/${s.slice(8, 10)}/${s.slice(2, 4)} ${s.slice(11, 16)}`
    : s;

// The over-time chart's timeframes are the site-wide This month … All
// (components/RangePicker); the windows themselves are the server's.
export type TrendRange = Range;

/** The Over-time card's window for a timeframe: the server's
 *  (`trend_windows`, over `full_trend` — the same total as the headline).
 *  Without one the whole series is drawn and no gain printed; the gain is
 *  never worked out here. Mobile twin: overTimeWindow in
 *  lib/networth-pure.ts. */
export function overTimeWindow(nw: NetWorthReport, range: TrendRange,
                               points: number): TrendWindow {
  return nw.trend_windows?.[range]
    ?? { start: 0, end: Math.max(points - 1, 0), gain: null, pct: null,
         full: true };
}

const pl = (v: number) => (
  <td className={`num c-pl ${v >= 0 ? "pos" : "neg"}`}>
    {v >= 0 ? "+" : "−"}{money(Math.abs(v))}
  </td>
);
const pct = (v: number, p: number | null) => (
  <td className={`num c-ret ${v >= 0 ? "pos" : "neg"}`}>
    {p !== null && p !== undefined ? signedPct(p) : "—"}
  </td>
);

export default function NetWorth() {
  const q = useQuery({ queryKey: ["report", "networth"], queryFn: api.reportNetworth });
  // the fees section renders after the report, and its request waits for
  // the report too: both handlers are CPU-bound, and in parallel they
  // slow each other — the report (what the page paints from) landed
  // ~70 ms later when fees ran beside it
  useQuery({ queryKey: ["report", "fees"], queryFn: api.reportFees,
             enabled: !!q.data });
  // the site-wide default window, as on Cash Flow
  const [range, setRange] = useState<TrendRange>(DEFAULT_RANGE);
  const noAccounts = useNoAccounts();
  if (q.isPending) return <PageSkeleton />;
  // a failed read keeps the page's name over the line: untitled, "the
  // server is busy" said nothing about what had not loaded
  if (q.data === undefined)
    return <><h1>Net Worth</h1><FailedLine q={q} what="net worth" /></>;
  const nw: NetWorthReport = q.data;
  // nothing linked and nothing entered by hand: the way to start, not a
  // "$0" total
  if (noAccounts && nw.property_items.length === 0)
    return <><h1>Net Worth</h1><div className="card"><NoAccountsLine /></div></>;
  const rc = nw.retirement_combined;
  const rtot = rc?.total;
  // no payoff figure is no line (the phone's rule), never "$0 payoff"
  const mortgage = nw.mortgage_payoff ?? null;
  // the headline's and the tiles' changes this month, server-decided:
  // the headline on the total it sits beside (property included), the
  // financial tile on the financial-only trend, the property tile the
  // difference (reporting._networth_figures)
  const delta = nw.month_change ?? null;
  const finDelta = nw.financial_month_change ?? null;
  const propDelta = nw.property_month_change ?? null;

  return (
    <>
      <h1>Net Worth</h1>
      {/* a failed refetch says the figures below are from before it (the
          phone's line) */}
      <StaleLine query={q} />
      {/* HERO: total net worth is THE number; financial and
          property are its parts, so they are tiles beneath rather than two
          equal cards beside it — the tiles ARE the breakdown. */}
      <div className="card" style={{ marginTop: 0 }}>
        <div className="sub">Total net worth</div>
        <div style={{ display: "flex", gap: ".6rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          <span style={{ fontSize: "2rem", fontWeight: 700,
                         letterSpacing: "-.02em" }}>{money(nw.full_total)}</span>
          {delta !== null && (
            <span className={changeWords(delta, money).tone}>
              {changeWords(delta, money).text} this month</span>
          )}
        </div>
        {/* what that change is made of, and money withdrawn that no linked
            account has received — the server's two lines, as sent */}
        {nw.month_change_text && (
          <div className="sub">{nw.month_change_text}</div>)}
        {nw.month_withdrawn_text && (
          <div className="sub">{nw.month_withdrawn_text}</div>)}
        <div className="grid cols2" style={{ gap: ".6rem", marginTop: ".7rem" }}>
          <div className="tile">
            <div className="sub">Financial accounts</div>
            <div className="kpi sm">{money(nw.current_total)}</div>
            {finDelta !== null && (
              <div className={`sub ${changeWords(finDelta, money).tone}`}>
                {changeWords(finDelta, money).text} this month
              </div>)}
            <div className="sub">cash + investments − card debt · live
              {" · "}<Link to="/debt">payoff plan</Link></div>
          </div>
          <div className="tile">
            <div className="sub">Property &amp; vehicles, net</div>
            <div className="kpi sm">{money(nw.property_net)}</div>
            {propDelta !== null && Math.abs(propDelta) >= 0.5 && (
              <div className={`sub ${changeWords(propDelta, money).tone}`}>
                {changeWords(propDelta, money).text} this month
              </div>)}
            {mortgage !== null && (
              <div className="sub">after {money(mortgage)} mortgage payoff
                {/* a typed-in balance says how old it is (the server's
                    words; "— update" once a quarter has passed) */}
                {nw.mortgage_entered_text && <> · {nw.mortgage_entered_text}</>}
              </div>)}
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Over time</h2>
        {/* the same total as the headline: full_trend, property in */}
        <TrendCard nw={nw} trend={nw.full_trend ?? []}
                   estimatedUntil={nw.full_trend_estimated_until}
                   range={range} setRange={setRange} />
        {/* no legend: the dashed segment labels itself at the boundary,
            and the caveat about reconstruction is said once, in the cache
            footnote at the foot of the page */}
      </div>

      <div className="grid cols2">
        <div className="card"><h2>By asset class</h2>
          <Donut segments={nw.by_asset_class} figures={nw.rings.by_asset_class} /></div>
        <div className="card"><h2>By institution</h2>
          <Donut segments={nw.by_institution} figures={nw.rings.by_institution} /></div>
      </div>

      {rtot && rc.groups && (
        <div className="card">
          {/* the headline rides the title row, so it neither competes
              with the page's own hero nor repeats the table's Total row */}
          <div style={{ display: "flex", gap: "1rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <h2 style={{ margin: 0 }}>Retirement &amp; investments</h2>
            <span className="mut" style={{ fontSize: 13 }}>
              {rc.groups.map((g) => g.provider).join(" + ")}, by account</span>
            <span style={{ marginLeft: "auto" }}
                  className={rtot.pl >= 0 ? "pos" : "neg"}>
              <b>{rtot.pl >= 0 ? "+" : "−"}{money(Math.abs(rtot.pl))}</b>
              {rtot.pct !== null && <> · <b>{signedPct(rtot.pct)}</b></>}
            </span>
          </div>
          {/* .pl-table: one account a two-line row at phone width (index.css) */}
          <table className="pl-table">
            <thead><tr><th>Account</th><th className="num hide-m">Invested</th>
              <th className="num">Value now</th><th className="num">P/L</th>
              <th className="num">Return</th></tr></thead>
            <tbody>
              {rc.groups.map((g) => (
                <FragmentRows key={g.provider} g={g} />
              ))}
              <tr style={{ borderTop: "2px solid var(--line)", fontWeight: 600 }}>
                <td>Total</td>
                <td className="num hide-m">{money(rtot.invested)}</td>
                <td className="num c-val">{money(rtot.value)}</td>
                {pl(rtot.pl)}{pct(rtot.pl, rtot.pct)}
              </tr>
            </tbody>
          </table>
          {rc.pl_note && (
            <div className="mut" style={{ fontSize: 12, marginTop: ".4rem" }}>
              {rc.pl_note}</div>
          )}
        </div>
      )}

      {nw.coinbase_pl.length > 0 && (
        <div className="card">
          <div style={{ display: "flex", gap: "1rem", alignItems: "baseline",
                        flexWrap: "wrap" }}>
            <h2 style={{ margin: 0 }}>Coinbase</h2>
            <span className="mut" style={{ fontSize: 13 }}>
              anchored to bank-side flows</span>
          </div>
          <table className="pl-table">
            <thead><tr><th>Account</th><th className="num hide-m">Cash in (bank)</th>
              <th className="num hide-m">Cash out</th><th className="num">Value now</th>
              <th className="num">P/L</th><th className="num">Return</th></tr></thead>
            <tbody>
              {nw.coinbase_pl.map((c) => (
                <tr key={c.name}
                    style={c.name === "Combined"
                      ? { borderTop: "2px solid var(--line)", fontWeight: 600 } : undefined}>
                  <td>{c.name}</td>
                  <td className="num hide-m">{money(c.cash_in)}</td>
                  <td className="num hide-m">{money(c.cash_out)}</td>
                  <td className="num c-val">{money(c.value)}</td>
                  {/* the return to one decimal, as the table above it */}
                  {pl(c.pl)}{pct(c.pl, c.pct)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <InvestmentFees />

      <div className="card">
        <h2>Property, vehicles &amp; mortgage</h2>
        <table>
          <thead><tr><th>Item</th><th>Kind</th><th className="num">Value</th>
            <th className="mut">Source</th></tr></thead>
          <tbody>
            {nw.property_items.map((p) => (
              <tr key={p.name}>
                <td>{p.name}</td>
                <td className="mut">{catLabel(p.kind)}</td>
                <td className={`num ${p.value < 0 ? "neg" : ""}`}>{money(p.value)}
                  {/* a typed-in loan balance carries its age, toned as a
                      warning once it asks for an update */}
                  {p.entered_text && (
                    <div className={p.update_due ? "sub warn" : "sub"}>
                      {p.entered_text}</div>)}</td>
                <td className="mut" style={{ fontSize: 12 }}>{p.source}</td>
              </tr>
            ))}

          </tbody>
        </table>
        <ManualAssetsEditor />
      </div>
      {nw._built_at && (
        <div className="sub" style={{ marginTop: ".6rem" }}>
          {/* built on this request — nothing is cached */}
          Figures as of {builtAt(nw._built_at)} · history before the first recorded
          balance is reconstructed from holdings × historical prices, with
          transfers netted out</div>
      )}
    </>
  );
}

// Timeframe toggles + the stats they imply. The chart is keyed by range
// so its internal drag/zoom window resets when the timeframe changes —
// a pan carried across windows would show a slice of the wrong slice.
function TrendCard({ nw, trend, estimatedUntil, range, setRange }: {
  nw: NetWorthReport;
  trend: [string, number][];
  estimatedUntil?: number;
  range: TrendRange;
  setRange: (r: TrendRange) => void;
}) {
  const w = overTimeWindow(nw, range, trend.length);
  const sliced = trend.slice(w.start, w.end + 1);
  // the estimate boundary is an index into the FULL series — rebase it,
  // dropping it when the whole window is measured data
  const eu = estimatedUntil !== undefined && estimatedUntil - w.start > 0
    ? estimatedUntil - w.start : undefined;
  return (
    <>
      <div style={{ display: "flex", gap: ".35rem", alignItems: "baseline",
                    flexWrap: "wrap", marginBottom: ".5rem" }}>
        <RangePicker value={range} onChange={setRange}
                     dead={(r) => overTimeWindow(nw, r, trend.length).full} />
        {w.gain !== null && (
          <span className={w.gain >= 0 ? "pos" : "neg"}
                style={{ marginLeft: "auto", fontSize: 13 }}>
            {w.gain >= 0 ? "+" : "−"}{money(Math.abs(w.gain))}
            {w.pct !== null && <> · {signedPct(w.pct)}</>}
            <span className="mut"> {overCaption(range)}
              {/* no percent to give the base: the server says where the
                  gain is measured from */}
              {w.from_text && <> · {w.from_text}</>}
            </span>
          </span>
        )}
      </div>
      {/* the server's words when the window crosses a typed-in property:
          the gain above is then the financial accounts' alone */}
      {w.note && (
        <p className="mut" style={{ margin: "0 0 .5rem", fontSize: 12,
                                    textAlign: "right" }}>{w.note}</p>
      )}
      {sliced.length < 2 ? (
        // a household linked today has one balance and no history yet
        <p className="mut" style={{ margin: ".4rem 0 0" }}>
          {sliced.length ? <>{money(sliced[0][1])} today. </> : null}
          The line starts once a second day's balance is in.</p>
      ) : (
        // a year label per year only when the window spans several: over
        // a year or less the two it touches both sat at the left edge, and
        // the dates themselves are the better axis
        <AreaChart key={range} points={sliced} estimatedUntil={eu}
                   subject="Net worth"
                   xticks={sliced.length > 24 ? "years" : undefined} />)}
    </>
  );
}

// Investment fees as a section of this page — fee drag only means
// something next to the balances it erodes. The report is investment-only
// (bank/card/ATM fees are ordinary spending), so the section stays hidden
// when nothing matched.
function InvestmentFees() {
  const q = useQuery({ queryKey: ["report", "fees"], queryFn: api.reportFees });
  if (q.isPending) return null;
  // a failed read keeps the card's place: gone, it read as never charged
  if (q.data === undefined)
    return (
      <div className="card"><h2>Investment fees</h2>
        <FailedLine q={q} what="the investment fees" /></div>);
  const fe: FeesReport = q.data;
  if (!fe.by_platform.length) return null;

  return (
    <div className="grid cols2">
      <div className="card">
        <h2>Investment fees{" "}
          <span className="mut" style={{ fontSize: 13, fontWeight: 400 }}>
            (advisory / admin / participant / account, all time)</span></h2>
        <div className="kpi sm neg" style={{ marginBottom: ".4rem" }}>
          {money(fe.total)}</div>
        <table>
          <thead><tr><th>Platform</th><th className="num">Fees</th>
            <th className="num">Counted</th></tr></thead>
          <tbody>
            {fe.by_platform.map(([inst, amt], i) => (
              <tr key={inst}><td>{inst}</td>
                <td className="num">{money(amt)}</td>
                {/* the server's words: charges for fee rows, years for
                    an estimated advisory fee */}
                <td className="num mut">{fe.platform_counts[i]}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card"><h2>Fees by year</h2>
        <Bars rows={[...fe.by_year].reverse()} /></div>
    </div>
  );
}

// manual_assets editor — add/edit/delete. Auto-valuation labels are
// preserved untouched; editing a value clears as_of so the server stamps
// today. "Owned since" (acquired_on) is the month the item enters the
// trend, and only this field changes it.
function ManualAssetsEditor() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings,
                       enabled: canEdit(me) });
  // `draft` is the value TEXT while it is typed; it is parsed only at
  // save, so "-", "-0." and "1200." can be typed on the way to a number.
  // `orig` is the row's key in `opened`, the list as it was when Edit was
  // pressed (absent: added here). `sent` is the row's place in the list
  // the last Save wrote, and `lastSent` that list: when its reply is lost
  // the next Save checks whether it landed, so nothing goes in twice.
  type Row = ManualAsset & { draft?: string; orig?: string; sent?: number };
  type Edited = ManualAsset & { orig?: string; sent?: number };
  const [rows, setRows] = useState<Row[] | null>(null);
  const [opened, setOpened] = useState<ManualAsset[]>([]);
  const lastSent = useRef<ManualAsset[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    scope: SETTINGS_SAVES,
    // The server replaces the whole list, and this one is a copy from
    // when Edit was pressed: the nightly valuation or another device may
    // have saved since. Read the stored list now and lay only this
    // editor's changes over it.
    // The save returns the full settings view, so the editor reopens on
    // the saved rows at once; the report is recomputed server-side from
    // them, so it is the one thing left to refetch.
    mutationFn: async ({ edited, at }: { edited: Edited[]; at: number[] }) => {
      // the read the merge is built on is also the basis the save names
      // the list's version from: a save another device lands after it is
      // refused and merged again, never overwritten
      const view = await api.settings();
      const live = view.manual_assets ?? [];
      const r = rebaseNamedSave(edited, opened, live, lastSent.current);
      lastSent.current = r.list;
      setOpened(r.baseline);
      setRows((cur) => cur && cur.flatMap((row, j) => {
        const n = at.indexOf(j);
        if (n < 0) return [row];
        const k = r.rows[n];
        return k ? [{ ...row, ...k }] : [];
      }));
      return saveSettings(qc, { manual_assets: r.list }, view);
    },
    onSuccess: () => {
      setErr(null); setRows(null);
      qc.invalidateQueries({ queryKey: ["report", "networth"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  // every hook sits above the first early return: one declared below it
  // changes the hook count when the editor opens, and React drops the page.
  //
  // The row whose value is not a number: its field is marked invalid and
  // reads the error banner as its description
  const [errAt, setErrAt] = useState<number | null>(null);
  const errId = useId();
  if (isViewer(me) || !s.data) return null;
  const saved = s.data.manual_assets ?? [];
  if (rows === null) {
    return (
      <button style={{ marginTop: ".6rem" }} onClick={() => {
        const keys = nameKeys(saved);
        setOpened(saved);
        lastSent.current = null;
        setRows(saved.map((a, i) => ({ ...a, orig: keys[i] })));
      }}>
        {saved.length ? "Edit assets" : "Add property, vehicle or mortgage"}
      </button>
    );
  }
  const upd = (i: number, patch: Partial<Row>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  // settle every typed value; one that is not a number stops the save
  // and says which row, rather than going out as NaN (sent as null → $0)
  const submit = () => {
    setErrAt(null);
    const out: Edited[] = [];
    const at: number[] = [];
    for (const [j, { draft, ...r }] of rows.entries()) {
      if (!r.name.trim()) continue;
      at.push(j);
      if (draft === undefined) { out.push(r); continue; }
      const v = draft.trim() === "" ? 0 : parseMoneyDraft(draft);
      if (v === null) {
        setErr(`${r.name.trim()}: the value is not a number`);
        setErrAt(j);
        return;
      }
      out.push({ ...r, value: v });
    }
    save.mutate({ edited: out, at });
  };
  return (
    <div style={{ marginTop: ".8rem" }}>
      {err && <Note tone="bad" id={errId} onDismiss={() => setErr(null)}>{err}</Note>}
      {rows.map((r, i) => (
        <div key={i} className="card" style={{ margin: ".5rem 0",
              padding: "12px 14px" }}>
          <div className="field-grid">
            <label>Name
              <input value={r.name} placeholder="Home"
                     onChange={(e) => upd(i, { name: e.target.value })} /></label>
            <label>Kind
              <select value={r.kind ?? "other"}
                      onChange={(e) => upd(i, { kind: e.target.value })}>
                <option value="property">property</option>
                <option value="vehicle">vehicle</option>
                <option value="mortgage">mortgage (negative)</option>
                <option value="other">other</option>
              </select></label>
            <Field label="Value $" error={!!err && errAt === i} errorId={errId}>
              <input value={r.draft ?? String(r.value)}
                     onChange={(e) => upd(i, { draft: e.target.value,
                                               as_of: null })} /></Field>
            {/* the month the item enters the trend; a value edit never
                moves it, and left blank the server keeps the stored date
                (a new item: today) */}
            <label>Owned since
              <input type="date" value={r.acquired_on ?? ""}
                     onChange={(e) => upd(i, { acquired_on:
                                               e.target.value || null })} />
            </label>
          </div>
          <button type="button" style={{ marginTop: ".4rem" }}
                  onClick={() => setRows(rows.filter((_, j) => j !== i))}>
            Remove</button>
        </div>
      ))}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem" }}>
        <button type="button" onClick={() => setRows([...rows,
          { name: "", kind: "property", value: 0 }])}>Add asset</button>
        <button className="pri" disabled={save.isPending}
                onClick={submit}>
          {save.isPending ? "saving…" : "Save assets"}</button>
        <button type="button" onClick={() => { setRows(null); setErr(null); }}>
          Cancel</button>
      </div>
    </div>
  );
}

function FragmentRows({ g }: { g: InvestedGroup }) {
  return (
    <>
      <tr style={{ fontWeight: 600 }}>
        <td style={{ paddingTop: ".7rem" }}>{g.provider}</td>
        <td className="num hide-m" style={{ paddingTop: ".7rem" }}>{money(g.invested)}</td>
        <td className="num c-val" style={{ paddingTop: ".7rem" }}>{money(g.value)}</td>
        {pl(g.pl)}{pct(g.pl, g.pct)}
      </tr>
      {g.accounts.map((c) => (
        <tr key={c.name}>
          <td className="mut c-sub" style={{ paddingLeft: "1.6rem" }}>{c.name}</td>
          <td className="num hide-m">{money(c.invested)}</td>
          <td className="num c-val">{money(c.value)}</td>
          {pl(c.pl)}{pct(c.pl, c.pct)}
        </tr>
      ))}
    </>
  );
}
