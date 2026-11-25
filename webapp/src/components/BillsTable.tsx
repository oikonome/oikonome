// "How this adds up" — the Bills row's table, folded under the row the way
// a chart folds its numbers (ChartText.tsx's ShowNumbers): a button that
// opens it in place. The rows are the server's (bills_breakdown.table):
// each a label and its signed figure, the last the plan they add up to,
// and below it any bill late from an earlier month, which is still due
// and in none of the plan. Nothing here adds or words a figure. The phone
// twin is mobile/src/components/bills-table.tsx.
import { useState } from "react";
import type { components } from "../api/api.generated";

type BillsTableData = components["schemas"]["BillsTable"];
type EnvelopeTableData = components["schemas"]["EnvelopeTable"];
type Breakdown = components["schemas"]["ServerPlanRow"]["breakdown"];

/** The table a map row opens, by the breakdown it carries: the Bills
 *  row's sum, or the Envelopes row's list of pools; nothing without one
 *  (an older server, or any other row). */
export function RowAddsUp({ breakdown }: { breakdown?: Breakdown }) {
  if (!breakdown || !breakdown.table) return null;
  if ("items" in breakdown)
    return <EnvelopesList table={breakdown.table as EnvelopeTableData} />;
  return <BillsAddsUp table={breakdown.table as BillsTableData} />;
}

// The Envelopes row's list (envelope_breakdown.table): one line per pool,
// on the Year a signed line for each cause a pool did not draw all of it,
// and the plan's set-aside. A list, not a sum: nothing here adds. The
// phone twin is mobile/src/components/bills-table.tsx.
export function EnvelopesList({ table }: { table: EnvelopeTableData }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: ".2rem" }}>
      <button type="button" className="as-link sub" style={{ fontSize: 12 }}
              aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? "Hide the pools" : "Each pool"}</button>
      {open && (
        <table style={{ marginTop: ".4rem", fontSize: 13 }}>
          <tbody>
            {table.rows.map((r, i) => (
              <tr key={i}><td>{r.label}</td><td>{r.text}</td></tr>))}
            {/* on the Year, why a pool did not draw all of it, by cause */}
            {table.outside.map((o, i) => (
              <tr key={`o${i}`}><td>{o.label}</td>
                <td className="num">{o.figure}</td></tr>))}
            <tr style={{ fontWeight: 600 }}>
              <td>{table.total.label}</td>
              <td className="num">{table.total.figure}</td></tr>
          </tbody>
        </table>)}
    </div>
  );
}

export function BillsAddsUp({ table }: { table: BillsTableData }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: ".2rem" }}>
      <button type="button" className="as-link sub" style={{ fontSize: 12 }}
              aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? "Hide how this adds up" : "How this adds up"}</button>
      {open && (
        <table style={{ marginTop: ".4rem", fontSize: 13 }}>
          <tbody>
            {table.rows.map((r, i) => (
              <tr key={i} style={r.sign === "=" ? { fontWeight: 600 } : undefined}>
                <td>{r.label}</td>
                <td className="num">{r.figure}</td>
              </tr>))}
          </tbody>
          {table.outside_heading && table.outside.length > 0 && (
            <tbody>
              <tr><th colSpan={2} style={{ paddingTop: ".6rem" }}>
                {table.outside_heading}</th></tr>
              {table.outside.map((r, i) => (
                <tr key={i}>
                  <td>{r.label}</td>
                  <td className="num">{r.figure}</td>
                </tr>))}
            </tbody>)}
        </table>)}
    </div>
  );
}
