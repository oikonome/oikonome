// "How has the forecast done?" and "Why the forecast missed"
// (specs/forecast-grading.md), the web's components/ForecastRecord.tsx:
// the track-record line under the cash chart opens it. Every sentence and
// figure is the server's (forecast_score.panel / forecast_score.miss).
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, Text, View } from "react-native";

import { errText, type Client } from "../lib/api";
import { C, money } from "../lib/theme";
import Sparkline from "./sparkline";

const md = (iso: string) => iso.slice(5, 10).replace("-", "/");

export default function ForecastRecord({ client, text, width }: {
  client: Client; text: string; width: number;
}) {
  const [open, setOpen] = useState(false);
  const [day, setDay] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["forecast-score"],
                       queryFn: () => client.forecastScore(), enabled: open });
  const p = q.data;
  return (
    <View style={{ marginTop: 4 }}>
      <Pressable accessibilityRole="button"
                 accessibilityState={{ expanded: open }}
                 onPress={() => setOpen(!open)}>
        <Text style={{ color: C.mut, fontSize: 11 }}>
          {open ? "▾ " : "▸ "}{text}</Text>
      </Pressable>
      {open && (
        <View style={{ marginTop: 6 }}>
          <Text style={{ color: C.text, fontSize: 12, fontWeight: "600" }}>
            How has the forecast done?</Text>
          {q.isError && <Text style={{ color: C.bad, fontSize: 11 }}>{errText(q.error)}</Text>}
          {p?.empty_text ? <Text style={{ color: C.mut, fontSize: 11 }}>{p.empty_text}</Text> : null}
          {p && p.history.length > 1 && (
            <Sparkline width={width} height={90}
              subject="Real checking balance against the 30-day forecast"
              data={p.history.map((h) => [h.day, h.actual] as [string, number])}
              second={p.history.map((h) => [h.day, h.predicted] as [string, number])}
              legend={["real balance", "forecast 30 days earlier"]} />
          )}
          {p?.coverage_text ? <Text style={{ color: C.mut, fontSize: 11 }}>{p.coverage_text}</Text> : null}
          {p?.card_paying_text ? <Text style={{ color: C.mut, fontSize: 11 }}>{p.card_paying_text}</Text> : null}
          {(p?.skips_text ?? []).map((t) => (
            <Text key={t} style={{ color: C.mut, fontSize: 11 }}>{t}</Text>))}
          {[...(p?.history ?? [])].reverse().slice(0, 30).map((h) => (
            <Pressable key={h.day} accessibilityRole="button"
              onPress={() => setDay(day === h.day ? null : h.day)}
              style={{ flexDirection: "row", justifyContent: "space-between",
                       paddingVertical: 3 }}>
              <Text style={{ color: C.mut, fontSize: 11 }}>{md(h.day)}</Text>
              <Text style={{ color: C.text, fontSize: 11 }}>
                {money(h.predicted)} → {money(h.actual)}</Text>
              <Text style={{ color: C.text, fontSize: 11 }}>{money(h.error)}</Text>
              <Text style={{ color: C.accent, fontSize: 11 }}>
                {day === h.day ? "Hide" : "Why"}</Text>
            </Pressable>
          ))}
          {day && <WhyItMissed client={client} day={day} />}
        </View>
      )}
    </View>
  );
}

function WhyItMissed({ client, day }: { client: Client; day: string }) {
  const q = useQuery({ queryKey: ["forecast-miss", day],
                       queryFn: () => client.forecastMiss(day, 30) });
  const m = q.data;
  if (q.isError) return <Text style={{ color: C.bad, fontSize: 11 }}>{errText(q.error)}</Text>;
  if (!m) return null;
  return (
    <View style={{ marginTop: 6 }}>
      <Text style={{ color: C.text, fontSize: 12, fontWeight: "600" }}>
        Why the forecast missed — {md(m.day)}</Text>
      {m.parts.map((x) => (
        <View key={x.kind} style={{ flexDirection: "row",
                                    justifyContent: "space-between" }}>
          <Text style={{ color: C.mut, fontSize: 11, flex: 1 }}>{x.label}</Text>
          <Text style={{ color: C.text, fontSize: 11 }}>{money(x.diff)}</Text>
        </View>
      ))}
      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
        <Text style={{ color: C.text, fontSize: 11, fontWeight: "600" }}>Miss</Text>
        <Text style={{ color: C.text, fontSize: 11, fontWeight: "600" }}>{money(m.error)}</Text>
      </View>
      {m.items.map((it) => (
        <Text key={it.text} style={{ color: C.mut, fontSize: 11, marginTop: 2 }}>{it.text}</Text>))}
    </View>
  );
}
