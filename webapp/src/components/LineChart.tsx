// Interactive multi-series line chart — hover crosshair + value readout.
// Pure SVG, no chart library; series share x positions (daily points).
import { useMemo, useRef, useState } from "react";
import { mmdd, money } from "../api/client";

export interface ChartSeries {
  label: string;
  color: string;
  dash?: string;
  points: [string, number][];
}

export default function LineChart({ series, height = 220 }: {
  series: ChartSeries[]; height?: number;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const W = 720, H = height, padL = 58, padR = 10, padT = 12, padB = 22;
  const iw = W - padL - padR, ih = H - padT - padB;

  const { xs, ymin, ymax } = useMemo(() => {
    const all = series.flatMap((s) => s.points.map((p) => p[1]));
    const ymin = Math.min(...all, 0), ymax = Math.max(...all, 0);
    return { xs: series[0]?.points.map((p) => p[0]) ?? [], ymin, ymax };
  }, [series]);
  const n = xs.length;
  if (!n) return null;
  const span = ymax - ymin || 1;
  const X = (i: number) => padL + (iw * i) / Math.max(1, n - 1);
  const Y = (v: number) => padT + ih - ((v - ymin) / span) * ih;
  // format the magnitude, sign outside — `$-1.5k` / Math.round's
  // toward-positive negative halves otherwise
  const short = (v: number) => {
    const sign = v < 0 ? "-" : "";
    const a = Math.abs(v);
    return a >= 1000 ? `${sign}$${(a / 1000).toFixed(a % 1000 ? 1 : 0)}k`
                     : `${sign}$${Math.round(a)}`;
  };

  const onMove = (e: React.MouseEvent) => {
    const rect = ref.current!.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.round(((fx - padL) / iw) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };

  return (
    <svg ref={ref} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", marginTop: 8 }}
         onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      {[ymax, (ymax + ymin) / 2, ymin].map((v, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={Y(v)} y2={Y(v)}
                stroke="var(--line)" strokeOpacity=".6" />
          <text x={padL - 6} y={Y(v) + 4} textAnchor="end" fontSize="11"
                fill="var(--mut)">{short(v)}</text>
        </g>
      ))}
      {ymin < 0 && (
        <line x1={padL} x2={W - padR} y1={Y(0)} y2={Y(0)}
              stroke="var(--red)" strokeDasharray="5 3" strokeOpacity=".8" />
      )}
      {[0, Math.floor(n / 2), n - 1].map((i) => (
        <text key={i} x={X(i)} y={H - 5} textAnchor="middle" fontSize="11"
              fill="var(--mut)">{mmdd(xs[i])}</text>
      ))}
      {series.map((s, si) => (
        <polyline key={si} fill="none" stroke={s.color} strokeWidth={si === 0 ? 2.5 : 2}
          strokeDasharray={s.dash}
          points={s.points.map((p, i) => `${X(i)},${Y(p[1])}`).join(" ")} />
      ))}
      {hover !== null && (
        <g pointerEvents="none">
          <line x1={X(hover)} x2={X(hover)} y1={padT} y2={padT + ih}
                stroke="var(--mut)" strokeOpacity=".5" strokeDasharray="3 3" />
          {series.map((s, si) => (
            <circle key={si} cx={X(hover)} cy={Y(s.points[hover][1])} r="3.5"
                    fill={s.color} />
          ))}
          <g transform={`translate(${Math.min(X(hover) + 8, W - 190)}, ${padT + 4})`}>
            <rect width="182" height={16 + series.length * 15} rx="6"
                  fill="var(--bg)" stroke="var(--line)" />
            <text x="8" y="13" fontSize="11" fill="var(--mut)">{mmdd(xs[hover])}</text>
            {series.map((s, si) => (
              <text key={si} x="8" y={28 + si * 15} fontSize="11" fill={s.color}>
                {s.label}: {money(s.points[hover][1])}
              </text>
            ))}
          </g>
        </g>
      )}
    </svg>
  );
}
