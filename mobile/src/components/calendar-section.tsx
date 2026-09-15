// The 5-week calendar at the bottom of Bills — the web's Monday grid
// (today highlighted, past dimmed). The grid is the whole agenda: a cell
// carries its day's total, and tapping a day opens that one day's events
// beneath the grid (the web shows them in the cell's tooltip). The
// running day-by-day list that used to follow the grid repeated the same
// events in a second shape and buried the Archived table. The calendar is
// a view of the schedule, not a destination, so it renders where the
// schedule lives rather than as a screen of its own.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { Card, H } from "./ui";
import { calendarCells, ymd } from "../lib/dates";
import { useSession } from "../lib/session";
import { C, mmddyy, money } from "../lib/theme";

export default function CalendarSection() {
  const { client } = useSession();
  const query = useQuery({
    queryKey: ["calendar"],
    queryFn: () => client!.calendar(35),
    enabled: !!client,
  });
  const [focus, setFocus] = useState<string | null>(null);
  const all = query.data?.calendar ?? [];
  const focused = focus ? all.find((d) => d.date === focus) : undefined;
  // device-local today — the server's start is the household's day, not
  // UTC-today, which would highlight tomorrow for a US evening user; the
  // device clock still wins when the phone is in another zone
  const todayIso = ymd(new Date());
  // 35 consecutive dates from the Monday of the window's week, each
  // looked up by date — the payload carries only event days, so packing
  // them back-to-back would put every day after the first gap under the
  // wrong weekday header
  const cells = query.data
    ? calendarCells(all, query.data.start, 35) : [];
  const weeks: (typeof cells)[] = [];
  for (let i = 0; i < cells.length; i += 7)
    weeks.push(cells.slice(i, i + 7));
  // a fetch that never resolves must not render as nothing at all —
  // the section owns its own query, so it owns saying so too
  if (query.isPending || (query.isError && !query.data)) {
    return (
      <Card>
        <H>Next five weeks</H>
        {query.isPending ? (
          <Text style={{ color: C.mut, fontSize: 13 }}>Loading…</Text>
        ) : (
          <Text style={{ color: C.bad, fontSize: 13 }}>
            Couldn&apos;t load the calendar — pull down to refresh, or{" "}
            <Text style={{ color: C.accent }}
                  onPress={() => query.refetch()}>
              retry now
            </Text>.
          </Text>
        )}
      </Card>
    );
  }
  return (
    <>
      <Card>
        <H>Next five weeks</H>
        <View style={{ flexDirection: "row" }}>
          {["M", "T", "W", "T", "F", "S", "S"].map((w, i) => (
            <Text key={i} style={s.gridHead}>{w}</Text>
          ))}
        </View>
        {weeks.map((week, wi) => (
          <View key={wi} style={{ flexDirection: "row" }}>
            {week.map((c, ci) => {
              const isToday = c.date === todayIso;
              const past = c.date < todayIso;
              const total = c.d?.total ?? 0;
              const has = (c.d?.events.length ?? 0) > 0;
              return (
                <Pressable key={ci}
                           style={[s.gridCell,
                             isToday && s.gridToday,
                             focus === c.date && !isToday
                               && { borderColor: C.accent,
                                    borderWidth: 1 }]}
                           onPress={() => has
                             && setFocus(focus === c.date
                                           ? null : c.date)}>
                  <Text style={{ color: isToday ? C.accent
                                   : past ? C.mut : C.text,
                                 fontSize: 11,
                                 fontWeight: isToday ? "800" : "400",
                                 opacity: past && !isToday ? 0.5 : 1 }}>
                    {Number(c.date.slice(8, 10))}
                  </Text>
                  {has && !past && (
                    <Text style={{ color: total > 0 ? C.good : C.mut,
                                   fontSize: 9,
                                   fontVariant: ["tabular-nums"] }}
                          numberOfLines={1}>
                      {total > 0 ? "+" : ""}
                      {money(Math.abs(total), false)}
                    </Text>
                  )}
                </Pressable>
              );
            })}
          </View>
        ))}
        {focused && (
          <View style={{ marginTop: 8 }}>
            <View style={s.head}>
              <Text style={s.date}>{mmddyy(focused.date)}</Text>
              <Text style={[s.total, focused.total > 0 && { color: C.good }]}>
                {focused.total > 0 ? "+" : ""}
                {money(Math.abs(focused.total))}
              </Text>
            </View>
            {focused.events.map((e, i) => (
              <View key={`${e.label}-${i}`} style={s.event}>
                <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                      numberOfLines={1}>
                  {e.label}
                </Text>
                <Text style={{ color: e.amount > 0 ? C.good : C.text,
                               fontSize: 13,
                               fontVariant: ["tabular-nums"] }}>
                  {e.amount > 0 ? "+" : ""}{money(Math.abs(e.amount))}
                </Text>
              </View>
            ))}
            <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
              tap the day again to close
            </Text>
          </View>
        )}
      </Card>
    </>
  );
}

const s = StyleSheet.create({
  head: { flexDirection: "row", justifyContent: "space-between",
          marginBottom: 4 },
  date: { color: C.text, fontSize: 15, fontWeight: "700" },
  total: { color: C.mut, fontSize: 14, fontVariant: ["tabular-nums"] },
  event: { flexDirection: "row", gap: 8, paddingVertical: 3 },
  gridHead: { flex: 1, color: C.mut, fontSize: 10, textAlign: "center" },
  gridCell: { flex: 1, minHeight: 40, alignItems: "center",
              justifyContent: "center", borderRadius: 6, margin: 1,
              backgroundColor: C.bg },
  gridToday: { backgroundColor: C.hover, borderColor: C.accent,
               borderWidth: 1 },
});
