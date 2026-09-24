// Net Worth — 3 stat tiles, a trend line with estimated shading,
// class/institution donuts, retirement & Coinbase P/L tables, and the
// property table, which is editable here by the owner (Settings does not
// own manual_assets).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { saveSettings } from "../api/cache";
import { api, errText,  money, type FeesReport, type ManualAsset, type NetWorthReport,
         type PLRow, catLabel } from "../api/client";
import AreaChart from "../components/AreaChart";
import Bars from "../components/Bars";
import Donut from "../components/Donut";
import { RANGE_MONTHS, RangePicker, rangeCaption, shiftMonthsUtc, type Range }
  from "../components/RangePicker";
import { canEdit, isViewer } from "../role";

// The over-time chart's window math (the toggle vocabulary itself lives in
// components/RangePicker — the site-wide This month … All). The window is
// anchored to the LAST point's date (not today) so a briefly-stale report
// still selects a full window. `start` is the index of the baseline point —
// the last point at or before the cutoff — so the drawn line begins at
// the value the gain is measured from; `end` is the last point drawn: the
// newest one for every window that runs to now, and for "1m" — the last
// COMPLETE month, the one window that closes before today — the last
// point at or before the previous month, so the gain is that month's
// alone. The series is one point per month, so the two short windows are
// two points: the change since the last month-end, and the change across
// the month before. Mobile twin: `trendWindow` in lib/pure.ts.
export type TrendRange = Range;

export function trendWindow(points: [string, number][], range: TrendRange): {
  start: number; end: number; gain: number | null; pct: number | null;
  full: boolean;
} {
  const n = points.length;
  if (n < 2) return { start: 0, end: n - 1, gain: null, pct: null, full: true };
  // last point at or before an ISO cutoff; -1 when the data starts later
  const at = (cutoff: string) => {
    for (let i = n - 1; i >= 0; i--) if (points[i][0] <= cutoff) return i;
    return -1;
  };
  let start = 0, end = n - 1;
  if (range !== "all") {
    const months = RANGE_MONTHS[range]!;
    // clamped, not rolled: a series whose last point is a 31st would
    // otherwise land the cutoff in the month AFTER the one asked for and
    // measure the gain over a window short by a month
    // none = the data is shorter than the range and the window IS the
    // whole series
    start = Math.max(0, at(shiftMonthsUtc(points[n - 1][0], months)));
    if (range === "1m") {
      const close = at(shiftMonthsUtc(points[n - 1][0], 1));
      // a series too short to close the month falls back to the open
      // window rather than measuring nothing
      if (close > start) end = close;
    }
  }
  const base = points[start][1];
  const gain = points[end][1] - base;
  return { start, end, gain,
           pct: base > 0 ? (100 * gain) / base : null,
           full: start === 0 };
}

const pl = (v: number) => (
  <td className={`num ${v >= 0 ? "pos" : "neg"}`}>
    {v >= 0 ? "+" : "−"}{money(Math.abs(v))}
  </td>
);
const pct = (v: number, p: number | null) => (
  <td className={`num ${v >= 0 ? "pos" : "neg"}`}>
    {p !== null && p !== undefined ? `${p >= 0 ? "+" : ""}${p.toFixed(1)}%` : "—"}
  </td>
);

export default function NetWorth() {
  const q = useQuery({ queryKey: ["report", "networth"], queryFn: api.reportNetworth });
  const [range, setRange] = useState<TrendRange>("all");
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const nw: NetWorthReport = q.data;
  const rc = nw.retirement_combined;
  const rtot = rc?.total;
  const mortgage = nw.property_items
    .filter((p) => p.kind === "mortgage")
    .reduce((s, p) => s + p.value, 0);
  // the hero's "this month" — derived from the trend already in the payload,
  // so it costs no new query and cannot disagree with the chart beside it
  const t = nw.trend ?? [];
  const delta = t.length >= 2 ? t[t.length - 1][1] - t[t.length - 2][1] : null;

  return (
    <>
      <h1>Net Worth</h1>
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
            <span className={delta >= 0 ? "pos" : "neg"}>
              {delta >= 0 ? "+" : "−"}{money(Math.abs(delta))} this month</span>
          )}
        </div>
        <div className="grid cols2" style={{ gap: ".6rem", marginTop: ".7rem" }}>
          <div className="tile">
            <div className="sub">Financial accounts</div>
            <div className="kpi sm">{money(nw.current_total)}</div>
            <div className="sub">cash + investments − card debt · live
              {" · "}<Link to="/debt">payoff plan</Link></div>
          </div>
          <div className="tile">
            <div className="sub">Property &amp; vehicles, net</div>
            <div className="kpi sm">{money(nw.property_net)}</div>
            <div className="sub">after {money(Math.abs(mortgage))} mortgage payoff</div>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Over time</h2>
        <TrendCard trend={t} estimatedUntil={nw.trend_estimated_until}
                   range={range} setRange={setRange} />
        {/* no legend: the dashed segment labels itself at the boundary,
            and the caveat about reconstruction is said once, in the cache
            footnote at the foot of the page */}
      </div>

      <div className="grid cols2">
        <div className="card"><h2>By asset class</h2><Donut segments={nw.by_asset_class} /></div>
        <div className="card"><h2>By institution</h2><Donut segments={nw.by_institution} /></div>
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
              {rtot.pct !== null && <> · <b>{rtot.pct >= 0 ? "+" : ""}
                {rtot.pct.toFixed(1)}%</b></>}
            </span>
          </div>
          <table>
            <thead><tr><th>Account</th><th className="num">Invested</th>
              <th className="num">Value now</th><th className="num">P/L</th>
              <th className="num">Return</th></tr></thead>
            <tbody>
              {rc.groups.map((g) => (
                <FragmentRows key={g.provider} g={g} />
              ))}
              <tr style={{ borderTop: "2px solid var(--line)", fontWeight: 600 }}>
                <td>Total</td>
                <td className="num">{money(rtot.invested)}</td>
                <td className="num">{money(rtot.value)}</td>
                {pl(rtot.pl)}{pct(rtot.pl, rtot.pct)}
              </tr>
            </tbody>
          </table>
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
          <table>
            <thead><tr><th>Account</th><th className="num">Cash in (bank)</th>
              <th className="num">Cash out</th><th className="num">Value now</th>
              <th className="num">P/L</th><th className="num">Return</th></tr></thead>
            <tbody>
              {nw.coinbase_pl.map((c) => (
                <tr key={c.name}
                    style={c.name === "Combined"
                      ? { borderTop: "2px solid var(--line)", fontWeight: 600 } : undefined}>
                  <td>{c.name}</td>
                  <td className="num">{money(c.cash_in)}</td>
                  <td className="num">{money(c.cash_out)}</td>
                  <td className="num">{money(c.value)}</td>
                  {pl(c.pl)}
                  <td className={`num ${c.pl >= 0 ? "pos" : "neg"}`}>
                    {c.pct !== null ? `${c.pct >= 0 ? "+" : ""}${c.pct.toFixed(0)}%` : "—"}</td>
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
                <td className={`num ${p.value < 0 ? "neg" : ""}`}>{money(p.value)}</td>
                <td className="mut" style={{ fontSize: 12 }}>{p.source}</td>
              </tr>
            ))}

          </tbody>
        </table>
        <ManualAssetsEditor />
      </div>
      {nw._built_at && (
        <div className="sub" style={{ marginTop: ".6rem" }}>
          Reports cached {nw._built_at} · history before the first recorded
          balance is reconstructed from holdings × historical prices, with
          transfers netted out</div>
      )}
    </>
  );
}

// Timeframe toggles + the stats they imply. The chart is keyed by range
// so its internal drag/zoom window resets when the timeframe changes —
// a pan carried across windows would show a slice of the wrong slice.
function TrendCard({ trend, estimatedUntil, range, setRange }: {
  trend: [string, number][];
  estimatedUntil?: number;
  range: TrendRange;
  setRange: (r: TrendRange) => void;
}) {
  const w = trendWindow(trend, range);
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
                     dead={(r) => trendWindow(trend, r).full} />
        {w.gain !== null && (
          <span className={w.gain >= 0 ? "pos" : "neg"}
                style={{ marginLeft: "auto", fontSize: 13 }}>
            {w.gain >= 0 ? "+" : "−"}{money(Math.abs(w.gain))}
            {w.pct !== null && <> · {w.pct >= 0 ? "+" : ""}
              {w.pct.toFixed(1)}%</>}
            <span className="mut"> over {rangeCaption(range)}
            </span>
          </span>
        )}
      </div>
      <AreaChart key={range} points={sliced} estimatedUntil={eu}
                 xticks="years" />
    </>
  );
}

// Investment fees as a section of this page — fee drag only means
// something next to the balances it erodes. The report is investment-only
// (bank/card/ATM fees are ordinary spending), so the section stays hidden
// when nothing matched.
function InvestmentFees() {
  const q = useQuery({ queryKey: ["report", "fees"], queryFn: api.reportFees });
  if (q.isPending || q.isError) return null;
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
            <th className="num">Count</th></tr></thead>
          <tbody>
            {fe.by_platform.map(([inst, amt, n]) => (
              <tr key={inst}><td>{inst}</td>
                <td className="num">{money(amt)}</td>
                <td className="num mut">{n}</td></tr>
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
// today.
function ManualAssetsEditor() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings,
                       enabled: canEdit(me) });
  const [rows, setRows] = useState<ManualAsset[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    // the save returns the full settings view, so the editor reopens on
    // the saved rows at once; the report is recomputed server-side from
    // them, so it is the one thing left to refetch
    mutationFn: (assets: ManualAsset[]) =>
      saveSettings(qc, { manual_assets: assets }),
    onSuccess: () => {
      setErr(null); setRows(null);
      qc.invalidateQueries({ queryKey: ["report", "networth"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  if (isViewer(me) || !s.data) return null;
  const saved = s.data.manual_assets ?? [];
  if (rows === null) {
    return (
      <button style={{ marginTop: ".6rem" }} onClick={() =>
        setRows(saved.map((a) => ({ ...a })))}>
        {saved.length ? "Edit assets" : "Add property, vehicle or mortgage"}
      </button>
    );
  }
  const upd = (i: number, patch: Partial<ManualAsset>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const num = (v: string) => Number(v.replace(/[$,\s]/g, "") || 0);
  return (
    <div style={{ marginTop: ".8rem" }}>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
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
            <label>Value $
              <input inputMode="decimal" value={String(r.value)}
                     onChange={(e) => upd(i, { value: num(e.target.value),
                                               as_of: null })} /></label>
          </div>
          {r.auto && (
            <div className="mut" style={{ fontSize: 12, marginTop: ".3rem" }}>
              auto-valued ({r.auto}) — a manual value edit wins until the
              next auto update</div>
          )}
          <button type="button" style={{ marginTop: ".4rem" }}
                  onClick={() => setRows(rows.filter((_, j) => j !== i))}>
            Remove</button>
        </div>
      ))}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem" }}>
        <button type="button" onClick={() => setRows([...rows,
          { name: "", kind: "property", value: 0 }])}>Add asset</button>
        <button className="pri" disabled={save.isPending}
                onClick={() => save.mutate(rows.filter((r) => r.name.trim()))}>
          {save.isPending ? "saving…" : "Save assets"}</button>
        <button type="button" onClick={() => { setRows(null); setErr(null); }}>
          Cancel</button>
      </div>
    </div>
  );
}

function FragmentRows({ g }: {
  g: { provider: string; accounts: PLRow[]; invested: number; value: number;
       pl: number; pct: number | null };
}) {
  return (
    <>
      <tr style={{ fontWeight: 600 }}>
        <td style={{ paddingTop: ".7rem" }}>{g.provider}</td>
        <td className="num" style={{ paddingTop: ".7rem" }}>{money(g.invested)}</td>
        <td className="num" style={{ paddingTop: ".7rem" }}>{money(g.value)}</td>
        {pl(g.pl)}{pct(g.pl, g.pct)}
      </tr>
      {g.accounts.map((c) => (
        <tr key={c.name}>
          <td className="mut" style={{ paddingLeft: "1.6rem" }}>{c.name}</td>
          <td className="num">{money(c.invested ?? 0)}</td>
          <td className="num">{money(c.value)}</td>
          {pl(c.pl)}{pct(c.pl, c.pct)}
        </tr>
      ))}
    </>
  );
}
