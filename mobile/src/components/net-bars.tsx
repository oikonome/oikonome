// Saved per month with the amount on every bar — the web's NetBars. The
// forecast tail (from `estimatedFrom`) is drawn hollow. Above ~12 bars only
// every nth label is written, or they would overprint each other.
import { memo } from "react";
import { View } from "react-native";
import Svg, { Line, Rect, Text as SvgText } from "react-native-svg";

import { chartSentence, chartTable } from "../lib/chart-summary";
import { k$ } from "../lib/flowlayout";
import { netBarsLabelWidth, netBarsShowValues } from "../lib/pure";
import { C, money } from "../lib/theme";
import { ShowNumbers } from "./show-numbers";

function NetBars({ points, estimatedFrom, width, height = 170,
                   posColor, subject = "Left over per month",
                   missing = "no bank feed" }: {
  /** what an empty slot is called in the numbers table (Cash Flow passes
   *  the server's words for an incomplete month) */
  missing?: string;
  // a null is a month no bank feed covers: its slot stays, empty
  points: [string, number | null][]; estimatedFrom?: number;
  width: number; height?: number;
  // green by default (saved); the Income tab passes the flow picture's
  // income blue so "money in" keeps one color everywhere
  posColor?: string;
  /** what the bars are, for the spoken sentence and the table */
  subject?: string;
}) {
  if (!points.length) return null;
  const n = points.length;
  const maxAbs = Math.max(1, ...points.map((p) => Math.abs(p[1] ?? 0)));
  const labelTop = 14, labelBot = 16, axis = 14;
  const plotH = height - labelTop - labelBot - axis;
  const zero = labelTop + plotH / 2;
  const slot = width / n;
  const barW = Math.max(3, Math.min(28, slot * 0.62));
  // a label needs its own width of slot to itself (the web's rule): more
  // when it carries its year
  const labelW = netBarsLabelWidth(points.map((p) => p[0]), 5.4, 30);
  const every = Math.max(n <= 12 ? 1 : Math.ceil(n / 12),
                         Math.ceil(labelW / slot));
  // the amount is written on a bar only while bars are wide enough to
  // carry one; a label is kept whole inside the drawing
  const values = netBarsShowValues(slot);
  const labelX = (x: number) =>
    Math.max(labelW / 2, Math.min(width - labelW / 2, x + barW / 2));
  const ef = estimatedFrom ?? n;
  const named = [{ name: subject, points }];
  return (
    <View>
    <View accessible accessibilityRole="image"
          accessibilityLabel={chartSentence(subject, named, (v) => money(v, false), ef)}>
    <Svg width={width} height={height}>
      <Line x1={0} x2={width} y1={zero} y2={zero} stroke={C.border} />
      {points.map(([label, v], i) => {
        const x = slot * i + (slot - barW) / 2;
        // counted back from the newest bar, as the web does: the last
        // month is always named and no label lands beside it
        const show = (n - 1 - i) % every === 0;
        if (v === null) {
          // no bank feed covered this month: its place stays, empty
          return [
            <SvgText key={`v${i}`} x={x + barW / 2} y={zero - 3} fontSize={9.5}
                     textAnchor="middle" fill={C.mut}>—</SvgText>,
            show ? <SvgText key={`l${i}`} x={labelX(x)} y={height - 3} fontSize={9.5}
                            textAnchor="middle" fill={C.mut}>{label}</SvgText> : null,
          ];
        }
        const h = Math.abs(v) / maxAbs * (plotH / 2);
        const y = v >= 0 ? zero - h : zero;
        const fc = i >= ef;
        const color = v >= 0 ? (posColor ?? C.good) : C.bad;
        return [
          <Rect key={`b${i}`} x={x} y={y} width={barW} height={Math.max(h, 1)} rx={3}
                fill={fc ? "none" : color} stroke={color}
                strokeDasharray={fc ? "3 3" : undefined} opacity={fc ? 0.7 : 0.9} />,
          show && values ? <SvgText key={`v${i}`} x={x + barW / 2} y={v >= 0 ? y - 3 : y + h + 11}
                          fontSize={9.5} textAnchor="middle"
                          fill={fc ? C.mut : color}>{k$(v, money)}</SvgText> : null,
          show ? <SvgText key={`l${i}`} x={labelX(x)} y={height - 3} fontSize={9.5}
                          textAnchor="middle" fill={C.mut}>{label}</SvgText> : null,
        ];
      })}
    </Svg>
    </View>
    <ShowNumbers table={() => chartTable(named, (v) => money(v, false),
                                         { forecastFrom: ef, missing })} />
    </View>
  );
}

export default memo(NetBars);
