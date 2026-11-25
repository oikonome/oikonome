// The Cash Flow page's flow picture — paychecks / interest / other on the
// left, fixed bills / variable spend / saved on the right, band widths
// proportional to dollars. The web's FlowChart, drawn with react-native-svg.
import { memo } from "react";
import { Text, View } from "react-native";
import Svg, { Path, Rect, Text as SvgText } from "react-native-svg";

import type { FlowPeriod } from "../lib/api";
import { partsSentence } from "../lib/chart-summary";
import { type FlowNode, flowGeometry, flowNodeFigure } from "../lib/flowlayout";
import { C, money } from "../lib/theme";
import { ShowNumbers } from "./show-numbers";

function FlowChart({ flow, width, height = 200 }: {
  flow: FlowPeriod; width: number; height?: number;
}) {
  // unknown income (no bank feed covers the window): the line under the
  // picture says so and gives the spend — the web does the same — so an
  // empty-picture sentence here would claim nothing went out either
  if (flow.income_unknown || flow.in.total === null) return null;
  const g = flowGeometry(flow, width, height);
  if (!g)
    return <Text style={{ color: C.mut, fontSize: 12, marginVertical: 8 }}>
      No income or spending in this period yet.</Text>;
  // what each node prints is the server's (flowNodeFigure); the
  // geometry keeps the exact values
  const fig = (n: FlowNode) => {
    const x = flowNodeFigure(flow, n);
    return `${money(x.dollars, false)}${x.share === null ? "" : ` · ${x.share}%`}`;
  };
  const textY = (n: { y: number; h: number }) => n.y + n.h / 2 + 4;
  // a sliver too thin for its own label is named under the picture (the
  // web does the same) — inline it would sit on the neighbour's band
  const inline = (n: { h: number }) => n.h >= 14;
  const small = [...g.S.filter((n) => !inline(n)), ...g.K.filter((n) => !inline(n))];
  // the picture in words: every node with the dollars and share it prints
  const parts = (nodes: FlowNode[]) => nodes.map((n) => {
    const x = flowNodeFigure(flow, n);
    return { label: n.label, figure: money(x.dollars, false), share: x.share };
  });
  const sides = [{ head: "Where the money came from", parts: parts(g.S) },
                 { head: "Where it went", parts: parts(g.K) }];
  return (
    <View>
      <View accessible accessibilityRole="image" accessibilityLabel={partsSentence(sides)}>
      <Svg width={width} height={height}>
        {g.bands.map((b, i) => <Path key={i} d={b.d} fill={b.color} opacity={0.3} />)}
        {g.S.map((n) => <Rect key={n.key} x={0} y={n.y} width={g.nodeW} height={n.h}
                              rx={2} fill={n.color} />)}
        {g.K.map((n) => <Rect key={n.key} x={g.x1} y={n.y} width={g.nodeW}
                              height={n.h} rx={2} fill={n.color} />)}
        {g.S.filter(inline).map((n) => (
          <SvgText key={n.key} x={g.nodeW + 6} y={textY(n)} fontSize={11}
                   fill={C.text}>
            {n.label}{" "}{fig(n)}
          </SvgText>))}
        {g.K.filter(inline).map((n) => (
          <SvgText key={n.key} x={g.x1 - 6} y={textY(n)} fontSize={11}
                   fill={C.text} textAnchor="end">
            {n.label}{" "}{fig(n)}
          </SvgText>))}
      </Svg>
      {small.length > 0 && (
        <Text style={{ color: C.mut, fontSize: 11, marginTop: 2 }}>
          {small.map((n, i) => `${i ? " · " : ""}${n.label} ${fig(n)}`).join("")}
        </Text>)}
      </View>
      <ShowNumbers table={() => ({
        columns: ["Part", "Dollars", "Share"],
        rows: sides.flatMap((sd) => sd.parts.map((p) => [
          p.label, p.figure, p.share === null ? "" : `${p.share}%`])),
      })} />
    </View>
  );
}

export default memo(FlowChart);
