// The Cash Flow page's flow picture — paychecks / interest / other on the
// left, fixed bills / variable spend / saved on the right, band widths
// proportional to dollars. The web's FlowChart, drawn with react-native-svg.
import { memo } from "react";
import { Text, View } from "react-native";
import Svg, { Path, Rect, Text as SvgText } from "react-native-svg";

import type { FlowPeriod } from "../lib/api";
import { flowGeometry } from "../lib/flowlayout";
import { C, money } from "../lib/theme";

function FlowChart({ flow, width, height = 200 }: {
  flow: FlowPeriod; width: number; height?: number;
}) {
  const g = flowGeometry(flow, width, height);
  if (!g)
    return <Text style={{ color: C.mut, fontSize: 12, marginVertical: 8 }}>
      No income or spending in this period yet.</Text>;
  const pct = (v: number, base: number) =>
    base ? ` · ${Math.round(100 * v / base)}%` : "";
  const textY = (n: { y: number; h: number }) => n.y + n.h / 2 + 4;
  // a sliver too thin for its own label is named under the picture (the
  // web does the same) — inline it would sit on the neighbour's band
  const inline = (n: { h: number }) => n.h >= 14;
  const small = [...g.S.filter((n) => !inline(n)).map((n) => ({ ...n, side: "in" })),
                 ...g.K.filter((n) => !inline(n)).map((n) => ({ ...n, side: "out" }))];
  return (
    <View>
      <Svg width={width} height={height}>
        {g.bands.map((b, i) => <Path key={i} d={b.d} fill={b.color} opacity={0.3} />)}
        {g.S.map((n) => <Rect key={n.key} x={0} y={n.y} width={g.nodeW} height={n.h}
                              rx={2} fill={n.color} />)}
        {g.K.map((n) => <Rect key={n.key} x={g.x1} y={n.y} width={g.nodeW}
                              height={n.h} rx={2} fill={n.color} />)}
        {g.S.filter(inline).map((n) => (
          <SvgText key={n.key} x={g.nodeW + 6} y={textY(n)} fontSize={11}
                   fill={C.text}>
            {n.label}{" "}{money(n.value, false)}
            {n.key === "overspent" ? "" : pct(n.value, flow.in.total)}
          </SvgText>))}
        {g.K.filter(inline).map((n) => (
          <SvgText key={n.key} x={g.x1 - 6} y={textY(n)} fontSize={11}
                   fill={C.text} textAnchor="end">
            {n.label}{" "}{money(n.value, false)}{pct(n.value, g.L)}
          </SvgText>))}
      </Svg>
      {small.length > 0 && (
        <Text style={{ color: C.mut, fontSize: 11, marginTop: 2 }}>
          {small.map((n, i) => `${i ? " · " : ""}${n.label} ${money(n.value, false)}`
            + (n.key === "overspent" ? ""
               : pct(n.value, n.side === "in" ? flow.in.total : g.L))).join("")}
        </Text>)}
    </View>
  );
}

export default memo(FlowChart);
