// the money map — every row is a gauge of ITS OWN budget (full track =
// the row's budget), so "how are we doing" reads identically on every
// line. Overspend renders as a striped segment past a solid budget
// divider — the budget boundary never disappears into a longer bar. Pace
// is the standard post tick: a 2px ink line through the bar, overshooting
// it 3px top and bottom — the one "today" marker on every bar in the
// product.
//
// The hero bar lives OUT of this card, in each page's verdict card (Track
// is exported for it). The map renders only bars with something to measure — plan-only rows
// (actualLabel "—") fold into one muted footer sentence. There is no legend
// line: the striped and amber segments carry their meaning as a tooltip,
// and states label themselves inline — "over pace so far" red on the
// right, awaiting/still-due on the Bills row.
import { Link } from "react-router";
import { money } from "../api/client";
import { RowAddsUp } from "./BillsTable";
import type { PlanRow } from "./PlanBars";
import type { ReactNode } from "react";

// stripes read "past the budget" even without color vision
const OVERFLOW_BG =
  "repeating-linear-gradient(135deg,#b6473c 0 5px,#8f382f 5px 10px)";

/** Right-side status text so over/under is never color-alone: the row's
 *  status (todayview.row_status) in the words and tone the server wrote —
 *  the phone and the email print the same `text`. */
const TONE_COLOR = { neg: "var(--red)", pos: "var(--green)", mut: "var(--mut)" };
function rowDelta(row: PlanRow): { text: string; color: string } | null {
  const st = row.status;
  if (!st) return null;
  return { text: st.text, color: TONE_COLOR[st.tone] };
}

/** The bar itself: segments are % of the row's own budget (or of the
 *  actual when over). Overspend sits PAST a 2px budget divider as a
 *  striped segment; the post tick marks today's pace. Exported for
 *  the verdict-card hero bars (Today + the lenses). */
export function Track({ row, paceLabel = null }: {
  /** the tick's label as the server words it (null: none) */
  row: PlanRow; paceLabel?: string | null;
}) {
  const unbudgeted = row.budget <= 0.005 && row.actual > 0.005;
  const extent = Math.max(row.budget, row.actual + (row.pending ?? 0), 1);
  const w = (v: number) => (100 * v) / extent;
  const isover = row.judged ? row.actual > row.expected + 0.5
                            : row.actual > row.budget + 0.5;
  // a month that took money back out of savings reads a negative actual:
  // nothing is filled, and the rest of the bar is still only the budget
  const spent = Math.max(0, Math.min(row.actual, row.budget));
  const pend = Math.min(row.pending ?? 0, Math.max(row.budget - spent, 0));
  const over = Math.max(row.actual - row.budget, 0);
  const remaining = Math.max(row.budget - spent - pend, 0);
  const pace = row.judged && row.expected > 0 && row.expected < extent - 0.5
    ? w(row.expected) : null;
  return (
    <div style={{ position: "relative", marginTop: 3 }}>
      <div className="bartrack" style={{ display: "flex" }}>
        {unbudgeted ? (
          <div style={{ width: "100%", background: "var(--amber)",
                        opacity: .45 }} />
        ) : (
          <>
            {spent > 0.005 && (
              <div style={{ width: `${w(spent)}%`,
                            background: isover ? "var(--red)"
                                               : "var(--green)" }} />
            )}
            {pend > 0.005 && (
              <div title="awaiting" style={{ width: `${w(pend)}%`,
                            background: "var(--amber)" }} />
            )}
            {remaining > 0.005 && (
              // "light = still coming out of checking" — the remainder is
              // visible money, not empty track
              <div style={{ width: `${w(remaining)}%`,
                            background: "var(--green)", opacity: .22 }} />
            )}
            {over > 0.005 && (
              <>
                {/* the budget divider — overspend is PAST it, never
                    absorbed into a longer bar */}
                <div style={{ width: 2, flexShrink: 0,
                              background: "var(--ink)" }} />
                <div title="past the budget"
                     style={{ width: `${w(over)}%`,
                              background: OVERFLOW_BG }} />
              </>
            )}
          </>
        )}
      </div>
      {pace !== null && (
        <>
          <div className="bartick" style={{ left: `${pace}%` }} />
          {paceLabel && (
            <div className="sub"
                 style={{ position: "absolute", top: 20, left: `${pace}%`,
                          transform: "translateX(-50%)", fontSize: 10,
                          whiteSpace: "nowrap" }}>{paceLabel}</div>
          )}
        </>
      )}
    </div>
  );
}

// the "· not counted in the verdict" tail is legend-speak — the map now
// says it once via which rows carry a pace caret, not on every caption.
// The Bills row's caption ("posted $P · plan $Z") is printed too: its
// posted figure and the plan its table adds up to (bills_breakdown)
function terseCaption(row: PlanRow): string | null {
  if (!row.caption) return null;
  const c = row.caption.replace(/\s*·\s*not counted in the verdict\s*$/, "");
  return c || null;
}

function MapBar({ row, indent = false }: { row: PlanRow; indent?: boolean }) {
  const ind = indent ? "1.5rem" : undefined;
  const unbudgeted = row.budget <= 0.005 && row.actual > 0.005;
  const delta = rowDelta(row);
  const caption = terseCaption(row);
  return (
    <div style={{ margin: ".55rem 0" }}>
      {/* a gap and right-aligned figures: a long status wraps under its
          own figures instead of running them into the label */}
      <div style={{ display: "flex", justifyContent: "space-between",
                    gap: ".6rem", fontSize: 13, paddingLeft: ind }}>
        {row.to
          ? <Link className="catlink" to={row.to}>{row.label}</Link>
          : <span>{row.label}</span>}
        <span className="sub" style={{ textAlign: "right" }}>
          {row.actualLabel ?? money(row.actual)} / {unbudgeted ? "—" : money(row.budget)}
          {delta && <span style={{ color: delta.color, fontWeight: 600,
                                   marginLeft: ".45rem" }}>{delta.text}</span>}
        </span>
      </div>
      <div style={{ marginLeft: ind }}>
        <Track row={row} />
      </div>
      {unbudgeted ? (
        <div className="sub" style={{ fontSize: 11, marginTop: 1,
              color: "var(--amber)", paddingLeft: ind }}>
          no budget set — <Link to="/budget">set one</Link> so this counts
          toward the verdict
        </div>
      ) : caption ? (
        <div className="sub" style={{ fontSize: 11, marginTop: 1,
              paddingLeft: ind }}>
          {caption}</div>
      ) : null}
      {/* the Bills row opens how its figures add up (bills_breakdown
          .table), the Envelopes row its pools (envelope_breakdown
          .table), when the server sends them */}
      {row.breakdown?.table && (
        <div style={{ paddingLeft: ind }}>
          <RowAddsUp breakdown={row.breakdown} /></div>)}
    </div>
  );
}

/** The plan's rows, each a gauge of its own budget. The judged (variable)
 *  rows sum into the same "Variable spending" header the Budget page
 *  tracks and sit indented under it; Bills / Savings
 *  stay flat. Plan-only rows (nothing measurable) become one muted
 *  footer sentence instead of empty bars. */
export default function MoneyMap({ rows, total: totalRow, footnote }: {
  rows: PlanRow[];
  /** the "Variable spending" row over the judged rows — the server's
   *  (todayview.plan_total) on Today, the lens's own sum on a lens */
  total: PlanRow | null;
  footnote?: ReactNode;
}) {
  const measurable = rows.filter((r) => r.actualLabel !== "—");
  const folded = rows.filter((r) => r.actualLabel === "—");
  const judged = measurable.filter((r) => r.judged);
  const unjudged = measurable.filter((r) => !r.judged);
  // the header row stands over the judged rows only when there are two or
  // more of them to stand over
  const total = judged.length > 1 ? totalRow : null;
  // the excess row's words are the server's (todayview.map_rows): it is
  // the plan's figure, said as one
  const foldedBits = folded.map((r) =>
    r.label === "Excess cash"
      ? r.caption ?? ""
      : `${r.label.toLowerCase()} plan ${money(r.budget)}/mo (plan only)`);
  return (
    <>
      {total && <MapBar row={total} />}
      {judged.map((r) => (
        <MapBar key={r.label} row={r} indent={!!total} />
      ))}
      {unjudged.map((r) => <MapBar key={r.label} row={r} />)}
      {foldedBits.length > 0 && (
        <div className="sub" style={{ fontSize: 12, marginTop: ".5rem" }}>
          Also this month: {foldedBits.join(" · ")}
        </div>
      )}
      {footnote}
    </>
  );
}
