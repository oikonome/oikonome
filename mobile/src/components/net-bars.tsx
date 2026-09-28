// Saved per month with the amount on every bar — the web's NetBars. The
// forecast tail (from `estimatedFrom`) is drawn hollow. Above ~12 bars only
// every nth label is written, or they would overprint each other.
import { memo } from "react";
import Svg, { Line, Rect, Text as SvgText } from "react-native-svg";

import { k$ } from "../lib/flowlayout";
import { C, money } from "../lib/theme";

function NetBars({ points, estimatedFrom, width, height = 170,
                   posColor }: {
  points: [string, number][]; estimatedFrom?: number;
  width: number; height?: number;
  // green by default (saved); the Income tab passes the flow picture's
  // income blue so "money in" keeps one color everywhere
  posColor?: string;
}) {
  if (!points.length) return null;
  const n = points.length;
  const maxAbs = Math.max(1, ...points.map((p) => Math.abs(p[1])));
  const labelTop = 14, labelBot = 16, axis = 14;
  const plotH = height - labelTop - labelBot - axis;
  const zero = labelTop + plotH / 2;
  const slot = width / n;
  const barW = Math.max(3, Math.min(28, slot * 0.62));
  const every = n <= 12 ? 1 : Math.ceil(n / 12);
  const ef = estimatedFrom ?? n;
  return (
    <Svg width={width} height={height}>
      <Line x1={0} x2={width} y1={zero} y2={zero} stroke={C.border} />
      {points.map(([label, v], i) => {
        const x = slot * i + (slot - barW) / 2;
        const h = Math.abs(v) / maxAbs * (plotH / 2);
        const y = v >= 0 ? zero - h : zero;
        const fc = i >= ef;
        const color = v >= 0 ? (posColor ?? C.good) : C.bad;
        const show = i % every === 0 || i === n - 1;
        return [
          <Rect key={`b${i}`} x={x} y={y} width={barW} height={Math.max(h, 1)} rx={3}
                fill={fc ? "none" : color} stroke={color}
                strokeDasharray={fc ? "3 3" : undefined} opacity={fc ? 0.7 : 0.9} />,
          show ? <SvgText key={`v${i}`} x={x + barW / 2} y={v >= 0 ? y - 3 : y + h + 11}
                          fontSize={9.5} textAnchor="middle"
                          fill={fc ? C.mut : color}>{k$(v, money)}</SvgText> : null,
          show ? <SvgText key={`l${i}`} x={x + barW / 2} y={height - 3} fontSize={9.5}
                          textAnchor="middle" fill={C.mut}>{label}</SvgText> : null,
        ];
      })}
    </Svg>
  );
}

export default memo(NetBars);
