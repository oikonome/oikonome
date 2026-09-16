// The flow picture's layout math — a copy of the web's flowLayout in
// webapp/src/components/CashFlowCharts.tsx (mobile copies web, never
// invents). Pure: no RN imports, so the node runner can test it.
import type { FlowPeriod } from "./api";

export const k$ = (v: number, money: (n: number, cents?: boolean) => string): string => {
  const a = Math.abs(v);
  if (a >= 1_000_000) return `${v < 0 ? "-" : ""}$${(a / 1_000_000).toFixed(2)}m`;
  if (a >= 10_000) return `${v < 0 ? "-" : ""}$${Math.round(a / 1000)}k`;
  if (a >= 1000) return `${v < 0 ? "-" : ""}$${(a / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return money(v, false);
};

export const FLOW_COLORS = {
  paychecks: "#4f9bd6", interest: "#7fb8e6", other: "#3d7fb0",
  overspent: "#e0695d", fixed: "#e0695d", variable: "#f0b429", saved: "#5cb56b",
};

export interface FlowNode { key: string; label: string; value: number; color: string }

export function flowLayout(f: FlowPeriod) {
  const saved = f.saved;
  const sources: FlowNode[] = [
    { key: "paychecks", label: "Paychecks", value: f.in.paychecks, color: FLOW_COLORS.paychecks },
    { key: "interest", label: "Interest & dividends", value: f.in.interest, color: FLOW_COLORS.interest },
    { key: "other", label: "Other income", value: f.in.other, color: FLOW_COLORS.other },
  ];
  if (saved < 0) sources.push({ key: "overspent", label: "Overspent by",
                                value: -saved, color: FLOW_COLORS.overspent });
  const sinks: FlowNode[] = [
    { key: "fixed", label: "Fixed bills", value: f.out.fixed, color: FLOW_COLORS.fixed },
    { key: "variable", label: "Variable spend", value: f.out.variable, color: FLOW_COLORS.variable },
  ];
  if (saved > 0) sinks.push({ key: "saved", label: "Invested & saved",
                              value: saved, color: FLOW_COLORS.saved });
  const L = Math.max(f.in.total, f.out.total, 0);
  return { sources: sources.filter((n) => n.value > 0),
           sinks: sinks.filter((n) => n.value > 0), L };
}

/** Node rectangles + band paths for a width×height box. Shared by the
 *  web and native drawings so both draw the same picture. */
export function flowGeometry(f: FlowPeriod, width: number, height: number) {
  const { sources, sinks, L } = flowLayout(f);
  if (!L || !sources.length || !sinks.length) return null;
  const pad = 8, gap = 10, nodeW = 12, top = 14;
  const usable = height - top - pad - gap * (Math.max(sources.length, sinks.length) - 1);
  const scale = usable / L;
  const place = (nodes: FlowNode[]) => {
    let y = top;
    return nodes.map((n) => {
      const h = Math.max(n.value * scale, 2);
      const o = { ...n, y, h, cursor: y };
      y += h + gap;
      return o;
    });
  };
  const S = place(sources), K = place(sinks);
  const x0 = nodeW, x1 = width - nodeW;
  const bands: { d: string; color: string }[] = [];
  for (const s of S) {
    for (const k of K) {
      const v = s.value * k.value / L;
      if (v <= 0) continue;
      const h = v * scale;
      const y0 = s.cursor, y1 = k.cursor;
      s.cursor += h; k.cursor += h;
      const cx = (x0 + x1) / 2;
      bands.push({ color: k.color,
        d: `M${x0},${y0} C${cx},${y0} ${cx},${y1} ${x1},${y1} L${x1},${y1 + h} `
         + `C${cx},${y1 + h} ${cx},${y0 + h} ${x0},${y0 + h} Z` });
    }
  }
  return { S, K, bands, nodeW, x1, L };
}
