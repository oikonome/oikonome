// The assistant's answer, block by block: prose, and the charts and
// tables the model asked for as validated specs (the server checks the
// shape; this only draws). Pure SVG like every other chart here, the
// mobile twin is mobile/src/components/answer-blocks.tsx — same
// palette, same anatomy. Every chart's numbers are also readable as a
// table under it, and a single series gets no legend (the title names
// it); two or more always do.
import { useState } from "react";
import { money } from "../api/client";
import { parseRich } from "../richtext";

export interface SeriesSpec { name: string; values: number[] }
export type AnswerBlock =
  | { kind: "text"; text: string }
  | { kind: "bar" | "line" | "pie"; title: string; labels: string[];
      series: SeriesSpec[] }
  | { kind: "table"; title: string; columns: string[];
      rows: (string | number)[][] };

// the server-rendered charts' categorical order (CVD-safe); position
// follows the series, never its rank
export const PALETTE = ["#4f9bd6", "#e8833a", "#3a9188", "#8a5fb0",
                        "#4f9d5b", "#d9a441", "#c65c4e", "#7a8a99"];

const short = (v: number) => {
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1_000_000) return `${sign}$${(a / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (a >= 1000) return `${sign}$${(a / 1000).toFixed(a % 1000 ? 1 : 0)}k`;
  return `${sign}$${Math.round(a)}`;
};
const plain = (v: number) =>
  Number.isInteger(v) ? v.toLocaleString("en-US")
    : v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const clip = (s: string, n: number) => (s.length <= n ? s : s.slice(0, n - 1) + "…");

export default function AssistantBlocks({ blocks }: { blocks: AnswerBlock[] }) {
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === "text") return <RichText key={i} text={b.text} />;
        if (b.kind === "table")
          return <Figure key={i} title={b.title}>
            <DataTable columns={b.columns} rows={b.rows} />
          </Figure>;
        return <Figure key={i} title={b.title}>
          {b.kind === "bar" && <BarChart labels={b.labels} series={b.series} />}
          {b.kind === "line" && <LineChart labels={b.labels} series={b.series} />}
          {b.kind === "pie" && <PieChart labels={b.labels} series={b.series} />}
          {b.kind !== "pie" && b.series.length > 1 && <Legend series={b.series} />}
          <Numbers labels={b.labels} series={b.series} />
        </Figure>;
      })}
    </>
  );
}

/** model prose drawn: **bold** bold, "- " lines a list, the rest
 *  paragraphs (richtext.ts; the mobile twin is answer-blocks' RichText) */
export function RichText({ text }: { text: string }) {
  const runs = (rs: { bold: boolean; text: string }[]) =>
    rs.map((r, k) => r.bold ? <strong key={k}>{r.text}</strong> : r.text);
  return (
    <div className="rich">
      {parseRich(text).map((b, i) => b.kind === "p"
        ? <p key={i}>{runs(b.runs)}</p>
        : b.ordered
          // a numbered item keeps its own number: a ranking split around
          // a chart, or spaced by blank lines, still reads 1, 2, 3
          ? <ol key={i}>{b.items.map((it, j) =>
              <li key={j} value={it.n ?? j + 1}>{runs(it.runs)}</li>)}</ol>
          : <ul key={i}>{b.items.map((it, j) => <li key={j}>{runs(it.runs)}</li>)}</ul>)}
    </div>
  );
}

function Figure({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: ".6rem", padding: ".6rem .75rem",
                  border: "1px solid var(--line)", borderRadius: "var(--rs)" }}>
      {title && <div style={{ fontWeight: 600, fontSize: 13.5 }}>{title}</div>}
      {children}
    </div>
  );
}

function Legend({ series }: { series: SeriesSpec[] }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: ".2rem 1rem",
                  marginTop: ".3rem", fontSize: 12.5 }}>
      {series.map((s, i) => (
        <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: 3,
                         background: PALETTE[i % PALETTE.length] }} />
          {s.name}
        </span>
      ))}
    </div>
  );
}

/** the figures behind a chart, folded by default — the picture is never
 *  the only way to read a number */
function Numbers({ labels, series }: { labels: string[]; series: SeriesSpec[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: ".4rem" }}>
      <button style={{ fontSize: 12 }} onClick={() => setOpen((o) => !o)}>
        {open ? "hide numbers" : "show numbers"}
      </button>
      {open && (
        <DataTable columns={["", ...series.map((s) => s.name)]}
                   rows={labels.map((l, i) => [l, ...series.map((s) => s.values[i])])}
                   fmt={money} />
      )}
    </div>
  );
}

function DataTable({ columns, rows, fmt = plain }: {
  columns: string[]; rows: (string | number)[][]; fmt?: (v: number) => string;
}) {
  const numeric = columns.map((_, i) => rows.length > 0
    && rows.every((r) => typeof r[i] === "number"));
  return (
    <table style={{ marginTop: ".4rem", fontSize: 13 }}>
      <thead><tr>{columns.map((c, i) => (
        <th key={i} className={numeric[i] ? "num" : undefined}>{c}</th>))}</tr></thead>
      <tbody>{rows.map((r, ri) => (
        <tr key={ri}>{r.map((c, ci) => (
          <td key={ci} className={typeof c === "number" ? "num" : undefined}>
            {typeof c === "number" ? fmt(c) : c}
          </td>))}</tr>))}</tbody>
    </table>
  );
}

function ticks(values: number[]) {
  const ymin = Math.min(0, ...values), ymax = Math.max(0, ...values);
  return { ymin, ymax, span: ymax - ymin || 1 };
}

function BarChart({ labels, series }: { labels: string[]; series: SeriesSpec[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 720, H = 240, padL = 58, padR = 10, padT = 14, padB = 34;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length, k = series.length;
  const { ymin, ymax, span } = ticks(series.flatMap((s) => s.values));
  const Y = (v: number) => padT + ih - (ih * (v - ymin)) / span;
  const group = iw / n, gap = Math.min(8, group * 0.15);
  const bw = Math.max(2, (group - gap) / k - (k > 1 ? 2 : 0));
  const y0 = Y(0);
  const every = n <= 24 ? 1 : Math.max(1, Math.floor(n / 12));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", marginTop: 6 }}
         onMouseLeave={() => setHover(null)}>
      {[ymax, (ymax + ymin) / 2, ymin].map((v, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={Y(v)} y2={Y(v)} stroke="var(--line)" strokeOpacity=".6" />
          <text x={padL - 6} y={Y(v) + 4} textAnchor="end" fontSize="11" fill="var(--mut)">{short(v)}</text>
        </g>
      ))}
      <line x1={padL} x2={W - padR} y1={y0} y2={y0} stroke="var(--mut)" strokeOpacity=".7" />
      {labels.map((lab, i) => {
        const gx = padL + group * i + gap / 2;
        return (
          <g key={i} onMouseEnter={() => setHover(i)}>
            <rect x={padL + group * i} y={padT} width={group} height={ih} fill="transparent" />
            {series.map((s, j) => {
              const v = s.values[i];
              const x = gx + j * (bw + (k > 1 ? 2 : 0));
              const top = v >= 0 ? Y(v) : y0, bot = v >= 0 ? y0 : Y(v);
              return (
                <g key={j}>
                  <rect x={x} y={top} width={bw} height={Math.max(0, bot - top)} rx={2}
                        fill={PALETTE[j % PALETTE.length]}
                        opacity={hover === null || hover === i ? 1 : 0.55} />
                  {k === 1 && n <= 16 && (
                    <text x={x + bw / 2} y={top - 4} textAnchor="middle" fontSize="10.5"
                          fill="var(--ink)">{short(v)}</text>
                  )}
                </g>
              );
            })}
            {i % every === 0 && (
              <text x={padL + group * i + group / 2} y={H - padB + 16} textAnchor="middle"
                    fontSize="11" fill="var(--mut)">{clip(lab, 12)}</text>
            )}
          </g>
        );
      })}
      {hover !== null && (
        <g>
          <rect x={padL + 4} y={2} width={240} height={14 + 14 * k} rx={4}
                fill="var(--card)" stroke="var(--line)" />
          <text x={padL + 10} y={13} fontSize="11" fill="var(--ink)" fontWeight={600}>
            {labels[hover]}</text>
          {series.map((s, j) => (
            <text key={j} x={padL + 10} y={27 + 14 * j} fontSize="11" fill="var(--ink)">
              {k > 1 ? `${s.name}: ` : ""}{money(s.values[hover])}</text>
          ))}
        </g>
      )}
    </svg>
  );
}

function LineChart({ labels, series }: { labels: string[]; series: SeriesSpec[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 720, H = 240, padL = 58, padR = 10, padT = 14, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = labels.length;
  const { ymin, ymax, span } = ticks(series.flatMap((s) => s.values));
  const X = (i: number) => padL + (iw * i) / Math.max(1, n - 1);
  const Y = (v: number) => padT + ih - (ih * (v - ymin)) / span;
  const every = Math.max(1, Math.ceil(n / 8));
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * W;
    setHover(Math.max(0, Math.min(n - 1, Math.round(((fx - padL) / iw) * (n - 1)))));
  };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", marginTop: 6 }}
         onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      {[ymax, (ymax + ymin) / 2, ymin].map((v, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={Y(v)} y2={Y(v)} stroke="var(--line)" strokeOpacity=".6" />
          <text x={padL - 6} y={Y(v) + 4} textAnchor="end" fontSize="11" fill="var(--mut)">{short(v)}</text>
        </g>
      ))}
      {ymin < 0 && <line x1={padL} x2={W - padR} y1={Y(0)} y2={Y(0)} stroke="var(--mut)" strokeDasharray="5 3" />}
      {series.map((s, j) => (
        <polyline key={j} fill="none" stroke={PALETTE[j % PALETTE.length]} strokeWidth={2}
                  strokeLinejoin="round"
                  points={s.values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")} />
      ))}
      {labels.map((lab, i) => i % every === 0 && (
        <text key={i} x={X(i)} y={H - padB + 16} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
              fontSize="11" fill="var(--mut)">{clip(lab, 12)}</text>
      ))}
      {hover !== null && (
        <g>
          <line x1={X(hover)} x2={X(hover)} y1={padT} y2={padT + ih} stroke="var(--mut)" strokeDasharray="3 3" />
          {series.map((s, j) => (
            <circle key={j} cx={X(hover)} cy={Y(s.values[hover])} r={4}
                    fill={PALETTE[j % PALETTE.length]} stroke="var(--card)" strokeWidth={2} />
          ))}
          <rect x={hover > n / 2 ? padL + 4 : W - padR - 244} y={2} width={240}
                height={14 + 14 * series.length} rx={4} fill="var(--card)" stroke="var(--line)" />
          <text x={(hover > n / 2 ? padL + 4 : W - padR - 244) + 6} y={13} fontSize="11"
                fill="var(--ink)" fontWeight={600}>{labels[hover]}</text>
          {series.map((s, j) => (
            <text key={j} x={(hover > n / 2 ? padL + 4 : W - padR - 244) + 6} y={27 + 14 * j}
                  fontSize="11" fill="var(--ink)">
              {series.length > 1 ? `${s.name}: ` : ""}{money(s.values[hover])}</text>
          ))}
        </g>
      )}
    </svg>
  );
}

/** parts of a whole as a ring, the legend carrying value and share — a
 *  ring with the total in the middle, as the emailed reports draw it */
function PieChart({ labels, series }: { labels: string[]; series: SeriesSpec[] }) {
  const vals = series[0].values;
  const segs = labels.map((l, i) => [l, vals[i]] as const).filter(([, v]) => v > 0);
  const total = segs.reduce((a, [, v]) => a + v, 0) || 1;
  const size = 180, r = size / 2 - 14, cx = size / 2, cy = size / 2, sw = 26;
  const circ = 2 * Math.PI * r;
  let off = 0;
  return (
    <div style={{ display: "flex", gap: "1.2rem", alignItems: "center", flexWrap: "wrap", marginTop: 6 }}>
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size}>
        {segs.map(([l, v], i) => {
          const dash = (v / total) * circ;
          const el = (
            <circle key={i} cx={cx} cy={cy} r={r} fill="none"
                    stroke={PALETTE[i % PALETTE.length]} strokeWidth={sw}
                    strokeDasharray={`${dash} ${circ - dash}`} strokeDashoffset={-off}
                    transform={`rotate(-90 ${cx} ${cy})`}>
              <title>{`${l}: ${money(v)}`}</title>
            </circle>
          );
          off += dash;
          return el;
        })}
        <text x={cx} y={cy - 2} textAnchor="middle" fontSize="17" fontWeight={600} fill="var(--ink)">{money(total)}</text>
        <text x={cx} y={cy + 16} textAnchor="middle" fontSize="11" fill="var(--mut)">total</text>
      </svg>
      <div style={{ fontSize: 13 }}>
        {segs.map(([l, v], i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, margin: "3px 0" }}>
            <span style={{ width: 10, height: 10, borderRadius: 3, background: PALETTE[i % PALETTE.length] }} />
            {l} <b>{money(v)}</b>
            <span className="mut">{Math.round((v / total) * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}
