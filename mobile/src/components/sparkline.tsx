// A small labeled balance curve — the shape of the next weeks plus the
// reference points that make it readable: high/low values, a dashed
// zero line whenever zero is in range, the span's end labels, and (like
// the web AreaChart) an optional second scenario series, a "runs out"
// marker at the first negative point, and a two-entry legend.
//
// Memoized, and the point strings are computed once per data/width: the
// net-worth trend is a point per day for years, and rebuilding that
// polyline on every parent render (a refresh flag, a keystroke in the
// assets editor below) would be visible jank.
import { memo, useMemo } from "react";
import Svg, { Circle, Line, Polyline } from "react-native-svg";
import { StyleSheet, Text, View } from "react-native";

import { C, money } from "../lib/theme";

// a series with more points than the line has pixels draws the same
// shape at a fraction of the cost — keep every Nth point plus the last,
// so the span's end label still names the real last day
function downsample<T>(rows: T[], max: number): T[] {
  if (rows.length <= max) return rows;
  const step = Math.ceil(rows.length / max);
  const out = rows.filter((_, i) => i % step === 0);
  if (out[out.length - 1] !== rows[rows.length - 1])
    out.push(rows[rows.length - 1]);
  return out;
}

export default memo(Sparkline);

function Sparkline({ data: dataIn, second: secondIn, markX: markXIn,
                     markLabel, legend, dashUntil: dashUntilIn,
                     dashFrom: dashFromIn, events: eventsIn,
                     width, height = 84 }: {
  data: [string, number][];
  second?: [string, number][];        // the alternate card-payment scenario
  markX?: number | null;              // index of the first negative point
  markLabel?: string;                 // e.g. "runs out 7/16"
  /** dated events (card autopays) as index+label, the web AreaChart's
   *  `events`: muted lines behind the series. The label is not drawn — a
   *  phone-width chart has no room for rotated captions, so the dates are
   *  named in the row list under the chart, as the web does at desktop */
  events?: { i: number; label: string }[];
  legend?: [string, string];          // [primary, second] series names
  dashUntil?: number;                 // points BEFORE this index are
                                      // estimated/reconstructed → dashed
  dashFrom?: number;                  // points FROM this index are a
                                      // forecast tail → dashed
  width: number; height?: number;
}) {
  const PAD_R = 64;                 // room for the value labels
  const w = width - PAD_R;
  const g = useMemo(() => {
    if (dataIn.length < 2 || w <= 0) return null;
    // one point per pixel is all the line can show; indices that
    // address points (the mark, the dashed cut) move with the sampling
    const maxPts = Math.max(2, Math.floor(w));
    const step = Math.ceil(dataIn.length / maxPts);
    const data = downsample(dataIn, maxPts);
    const second = secondIn ? downsample(secondIn, maxPts) : undefined;
    const idx = (i: number | null | undefined) =>
      i == null ? i : Math.min(Math.round(i / step), data.length - 1);
    const markX = idx(markXIn);
    // event indices address points too, so they move with the sampling
    const events = (eventsIn ?? []).flatMap((e) => {
      const i = idx(e.i);
      return i == null || i < 0 || i >= data.length ? [] : [{ ...e, i }];
    });
    const dashUntil = idx(dashUntilIn);
    const dashFrom = idx(dashFromIn);
    const values = data.map(([, v]) => v);
    const values2 = (second ?? []).map(([, v]) => v);
    const lo = Math.min(...values, ...values2, 0);
    const hi = Math.max(...values, ...values2);
    const span = hi - lo || 1;
    const x = (i: number, n: number) => (i / (n - 1)) * w;
    const y = (v: number) => height - ((v - lo) / span) * (height - 6) - 3;
    const pt = (v: number, i: number) => `${x(i, values.length)},${y(v)}`;
    // measured vs estimated: the primary line splits into a solid segment
    // and a dashed one (reconstructed history before dashUntil, forecast
    // tail from dashFrom) — an estimate must not look like a measurement
    const cut = dashUntil != null
      ? Math.min(Math.max(dashUntil, 0), values.length - 1)
      : dashFrom != null
        ? Math.min(Math.max(dashFrom - 1, 0), values.length - 1)
        : null;
    const solidRange: [number, number] = cut === null ? [0, values.length - 1]
      : dashUntil != null ? [cut, values.length - 1] : [0, cut];
    const dashRange: [number, number] | null = cut === null ? null
      : dashUntil != null ? [0, cut] : [cut, values.length - 1];
    const seg = ([a, b]: [number, number]) =>
      values.slice(a, b + 1).map((v, i) => pt(v, a + i)).join(" ");
    const pts = seg(solidRange);
    const ptsDash = dashRange && dashRange[1] > dashRange[0]
      ? seg(dashRange) : null;
    const pts2 = values2.length >= 2
      ? values2.map((v, i) => `${x(i, values2.length)},${y(v)}`).join(" ")
      : null;
    const zeroInRange = lo < 0 && hi > 0;
    const mark = markX != null && markX >= 0 && markX < values.length
      ? { cx: x(markX, values.length), cy: y(values[markX]) } : null;
    const evLines = events.map((e) => ({ ...e, cx: x(e.i, values.length) }));
    return { data, lo, hi, y, pts, ptsDash, pts2, zeroInRange, mark, evLines };
  }, [dataIn, secondIn, markXIn, dashUntilIn, dashFromIn, eventsIn, w, height]);
  if (!g) return null;
  const { data, lo, hi, y, pts, ptsDash, pts2, zeroInRange, mark, evLines } = g;
  const label = (d: string) => d.slice(5, 10).replace("-", "/");
  return (
    <View style={{ marginTop: 8 }}>
      <View style={{ height, flexDirection: "row" }}>
        <Svg width={w} height={height}>
          {/* floor line anchors the shape */}
          <Line x1={0} y1={y(lo)} x2={w} y2={y(lo)}
                stroke={C.border} strokeWidth={1} />
          {zeroInRange && (
            <Line x1={0} y1={y(0)} x2={w} y2={y(0)}
                  stroke={C.bad} strokeWidth={1} strokeDasharray="4 4" />
          )}
          {evLines.map((e, k) => (
            <Line key={`ev${k}`} x1={e.cx} y1={0} x2={e.cx} y2={height}
                  stroke={C.mut} strokeWidth={1} strokeDasharray="3 3"
                  opacity={0.75} />
          ))}
          {pts2 && (
            <Polyline points={pts2} fill="none" stroke={C.mut}
                      strokeWidth={1.5} strokeDasharray="5 3"
                      strokeLinejoin="round" />
          )}
          {ptsDash && (
            <Polyline points={ptsDash} fill="none" stroke={C.accent}
                      strokeWidth={2} strokeDasharray="4 4"
                      strokeLinejoin="round" opacity={0.7} />
          )}
          <Polyline points={pts} fill="none" stroke={C.accent}
                    strokeWidth={2} strokeLinejoin="round" />
          {mark && (
            <>
              <Line x1={mark.cx} y1={0} x2={mark.cx} y2={height}
                    stroke={C.bad} strokeWidth={1} strokeDasharray="2 3" />
              <Circle cx={mark.cx} cy={mark.cy} r={3} fill={C.bad} />
            </>
          )}
        </Svg>
        <View style={{ width: PAD_R, justifyContent: "space-between",
                       paddingLeft: 6 }}>
          <Text style={s.axis}>{money(hi, false)}</Text>
          {zeroInRange && <Text style={[s.axis, { color: C.bad }]}>$0</Text>}
          <Text style={s.axis}>{money(lo, false)}</Text>
        </View>
      </View>
      <View style={{ flexDirection: "row",
                     justifyContent: "space-between", width: w }}>
        <Text style={s.axis}>{label(data[0][0])}</Text>
        {mark && markLabel ? (
          <Text style={[s.axis, { color: C.bad }]}>{markLabel}</Text>
        ) : null}
        <Text style={s.axis}>{label(data[data.length - 1][0])}</Text>
      </View>
      {legend && pts2 && (
        <View style={{ flexDirection: "row", gap: 14, marginTop: 4 }}>
          <Text style={s.axis}>
            <Text style={{ color: C.accent }}>━</Text> {legend[0]}
          </Text>
          <Text style={s.axis}>
            <Text style={{ color: C.mut }}>┅</Text> {legend[1]}
          </Text>
        </View>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  axis: { color: C.mut, fontSize: 10, fontVariant: ["tabular-nums"] },
});
