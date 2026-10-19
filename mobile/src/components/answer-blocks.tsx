// The assistant's answer drawn block by block — prose, then the charts
// and tables the model asked for as validated specs (the server checks
// the shape; this only draws). react-native-svg over the pure layout in
// lib/answer-pure.ts; the web twin is
// webapp/src/components/AssistantBlocks.tsx with the same palette and
// anatomy. Every chart's figures fold out as a table under it, and a
// lone series has no legend (the title names it); two or more always do.
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import Svg, { Circle, Line, Polyline, Rect, Text as SvgText } from "react-native-svg";

import { type AnswerBlock, type SeriesSpec, barLayout, cellText, chartRows,
         lineLayout, PALETTE, ringLayout } from "../lib/answer-pure";
import { itemLabel, parseRich } from "../lib/richtext";
import { C, money } from "../lib/theme";

/** model prose drawn: **bold** bold, "- " lines a list, the rest
 *  paragraphs (lib/richtext.ts; the web twin is AssistantBlocks'
 *  RichText). selectable = long-press copies */
export function RichText({ text }: { text: string }) {
  const runs = (rs: { bold: boolean; text: string }[]) => rs.map((r, k) =>
    r.bold ? <Text key={k} style={s.bold}>{r.text}</Text> : r.text);
  return (
    <View style={{ gap: 6 }}>
      {parseRich(text).map((b, i) => b.kind === "p"
        ? <Text key={i} style={s.text} selectable>{runs(b.runs)}</Text>
        : <View key={i} style={{ gap: 2, paddingLeft: 4 }}>
            {b.items.map((it, j) => (
              <View key={j} style={{ flexDirection: "row", gap: 6 }}>
                <Text style={s.text}>{b.ordered ? itemLabel(it, j) : "\u2022"}</Text>
                <Text style={[s.text, { flex: 1 }]} selectable>{runs(it.runs)}</Text>
              </View>))}
          </View>)}
    </View>
  );
}

export function AnswerBlocks({ blocks, width }: { blocks: AnswerBlock[]; width: number }) {
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === "text") return <RichText key={i} text={b.text} />;
        if (b.kind === "table")
          return (
            <Figure key={i} title={b.title}>
              <DataTable columns={b.columns} rows={b.rows.map((r) => r.map(cellText))} />
            </Figure>
          );
        return (
          <Figure key={i} title={b.title}>
            {b.kind === "bar" && <Bars labels={b.labels} series={b.series} width={width} />}
            {b.kind === "line" && <Lines labels={b.labels} series={b.series} width={width} />}
            {b.kind === "pie" && <Ring labels={b.labels} values={b.series[0].values} />}
            {b.kind !== "pie" && b.series.length > 1 && <Legend series={b.series} />}
            <Numbers labels={b.labels} series={b.series} />
          </Figure>
        );
      })}
    </>
  );
}

function Figure({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={s.fig}>
      {!!title && <Text style={s.title}>{title}</Text>}
      {children}
    </View>
  );
}

function Legend({ series }: { series: SeriesSpec[] }) {
  return (
    <View style={s.legend}>
      {series.map((sr, i) => (
        <View key={i} style={s.lg}>
          <View style={[s.sw, { backgroundColor: PALETTE[i % PALETTE.length] }]} />
          <Text style={s.lgText}>{sr.name}</Text>
        </View>
      ))}
    </View>
  );
}

function Numbers({ labels, series }: { labels: string[]; series: SeriesSpec[] }) {
  const [open, setOpen] = useState(false);
  const t = chartRows(labels, series);
  return (
    <View style={{ marginTop: 6 }}>
      <Pressable onPress={() => setOpen((o) => !o)} hitSlop={8}>
        <Text style={s.toggle}>{open ? "hide numbers" : "show numbers"}</Text>
      </Pressable>
      {open && <DataTable columns={t.columns} rows={t.rows} />}
    </View>
  );
}

function DataTable({ columns, rows }: { columns: string[]; rows: string[][] }) {
  return (
    <View style={{ marginTop: 6 }}>
      <View style={s.tr}>
        {columns.map((c, i) => (
          <Text key={i} style={[s.th, i > 0 && s.num]} numberOfLines={1}>{c}</Text>))}
      </View>
      {rows.map((r, ri) => (
        <View key={ri} style={s.tr}>
          {r.map((c, ci) => (
            <Text key={ci} style={[s.td, ci > 0 && s.num]} numberOfLines={1}>{c}</Text>))}
        </View>
      ))}
    </View>
  );
}

function Axis({ ticks, width }: { ticks: { y: number; text: string }[]; width: number }) {
  return (
    <>
      {ticks.map((t, i) => (
        <Line key={i} x1={46} x2={width - 6} y1={t.y} y2={t.y}
              stroke={C.border} strokeOpacity={0.8} />
      ))}
      {ticks.map((t, i) => (
        <SvgText key={`t${i}`} x={40} y={t.y + 4} textAnchor="end" fontSize={10}
                 fill={C.mut}>{t.text}</SvgText>
      ))}
    </>
  );
}

function Bars({ labels, series, width }: { labels: string[]; series: SeriesSpec[]; width: number }) {
  const H = 170;
  const L = barLayout(labels, series, width, H);
  return (
    <Svg width={width} height={H} style={{ marginTop: 6 }}>
      <Axis ticks={L.ticks} width={width} />
      <Line x1={46} x2={width - 6} y1={L.zeroY} y2={L.zeroY} stroke={C.mut} strokeOpacity={0.7} />
      {L.bars.map((b, i) => (
        <Rect key={i} x={b.x} y={b.y} width={b.w} height={b.h} rx={2}
              fill={PALETTE[b.si % PALETTE.length]} />
      ))}
      {L.bars.map((b, i) => b.label && (
        <SvgText key={`l${i}`} x={b.x + b.w / 2} y={b.y - 3} textAnchor="middle"
                 fontSize={9} fill={C.text}>{b.label}</SvgText>
      ))}
      {L.xLabels.map((x, i) => (
        <SvgText key={`x${i}`} x={x.x} y={H - 6} textAnchor="middle" fontSize={10}
                 fill={C.mut}>{x.text}</SvgText>
      ))}
    </Svg>
  );
}

function Lines({ labels, series, width }: { labels: string[]; series: SeriesSpec[]; width: number }) {
  const H = 170;
  const L = lineLayout(labels, series, width, H);
  return (
    <Svg width={width} height={H} style={{ marginTop: 6 }}>
      <Axis ticks={L.ticks} width={width} />
      {L.zeroY !== null && (
        <Line x1={46} x2={width - 6} y1={L.zeroY} y2={L.zeroY} stroke={C.mut}
              strokeDasharray="4 3" />
      )}
      {L.lines.map((l) => (
        <Polyline key={l.si} points={l.points} fill="none" strokeWidth={2}
                  strokeLinejoin="round" stroke={PALETTE[l.si % PALETTE.length]} />
      ))}
      {L.xLabels.map((x, i) => (
        <SvgText key={i} x={x.x} y={H - 6} textAnchor={x.anchor} fontSize={10}
                 fill={C.mut}>{x.text}</SvgText>
      ))}
    </Svg>
  );
}

function Ring({ labels, values }: { labels: string[]; values: number[] }) {
  const size = 150, r = size / 2 - 12, cx = size / 2, sw = 22;
  const R = ringLayout(labels, values, r);
  return (
    <View style={s.ringRow}>
      <Svg width={size} height={size}>
        {R.segs.map((g) => (
          <Circle key={g.si} cx={cx} cy={cx} r={r} fill="none"
                  stroke={PALETTE[g.si % PALETTE.length]} strokeWidth={sw}
                  strokeDasharray={`${g.dash} ${R.circ - g.dash}`}
                  strokeDashoffset={-g.offset} rotation={-90} origin={`${cx}, ${cx}`} />
        ))}
        <SvgText x={cx} y={cx - 1} textAnchor="middle" fontSize={15} fontWeight="600"
                 fill={C.text}>{money(R.total)}</SvgText>
        <SvgText x={cx} y={cx + 14} textAnchor="middle" fontSize={10} fill={C.mut}>total</SvgText>
      </Svg>
      <View style={{ flex: 1, gap: 3 }}>
        {R.segs.map((g) => (
          <View key={g.si} style={s.lg}>
            <View style={[s.sw, { backgroundColor: PALETTE[g.si % PALETTE.length] }]} />
            <Text style={s.lgText} numberOfLines={1}>
              {g.label} <Text style={{ fontWeight: "600" }}>{money(g.value)}</Text>
              <Text style={{ color: C.mut }}> {Math.round(g.share * 100)}%</Text>
            </Text>
          </View>
        ))}
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  text: { color: C.text, fontSize: 14, lineHeight: 20 },
  bold: { fontWeight: "700" },
  fig: { marginTop: 8, padding: 8, borderRadius: C.rs, borderWidth: 1,
         borderColor: C.border },
  title: { color: C.text, fontSize: 13, fontWeight: "600" },
  legend: { flexDirection: "row", flexWrap: "wrap", gap: 10, marginTop: 4 },
  lg: { flexDirection: "row", alignItems: "center", gap: 6 },
  sw: { width: 9, height: 9, borderRadius: 2 },
  lgText: { color: C.text, fontSize: 12, flexShrink: 1 },
  toggle: { color: C.mut, fontSize: 12 },
  ringRow: { flexDirection: "row", alignItems: "center", gap: 12, marginTop: 6 },
  tr: { flexDirection: "row", borderBottomWidth: 1, borderBottomColor: C.border,
        paddingVertical: 4, gap: 6 },
  th: { flex: 1, color: C.mut, fontSize: 11, fontWeight: "600" },
  td: { flex: 1, color: C.text, fontSize: 12 },
  num: { textAlign: "right", fontVariant: ["tabular-nums"] },
});
