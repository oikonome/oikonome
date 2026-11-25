// The words beside a chart: the "Show numbers" table every chart folds
// out (decided wording, specs/accessibility.md) and the keyboard walk of
// a chart's tooltip. The table is built only once it is opened, so a
// years-long daily series costs nothing until someone asks for it.
import { useState } from "react";
import type { ChartTable } from "../chartsummary";

export function ShowNumbers({ table }: { table: () => ChartTable }) {
  const [open, setOpen] = useState(false);
  const t = open ? table() : null;
  return (
    <div style={{ marginTop: ".3rem" }}>
      <button type="button" className="as-link sub" style={{ fontSize: 12 }}
              aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? "Hide numbers" : "Show numbers"}</button>
      {t && (
        <table style={{ marginTop: ".4rem", fontSize: 13 }}>
          <thead><tr>{t.columns.map((c, i) => (
            <th key={i} className={i ? "num" : undefined}>{c}</th>))}</tr></thead>
          <tbody>{t.rows.map((r, ri) => (
            <tr key={ri}>{r.map((c, ci) => (
              <td key={ci} className={ci ? "num" : undefined}>{c}</td>))}</tr>))}</tbody>
        </table>)}
    </div>
  );
}
