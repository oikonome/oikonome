// The 5-week calendar at the bottom of Bills — the web's Monday grid
// (today highlighted, past dimmed). The grid is the whole agenda: a cell
// carries its day's total, and tapping a day opens that one day's events
// beneath the grid (the web shows them in the cell's tooltip). The
// calendar is
// a view of the schedule, not a destination, so it renders where the
// schedule lives rather than as a screen of its own.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, StyleSheet, Text, useWindowDimensions, View } from "react-native";

import { Card, H } from "./ui";
import { FailedLine } from "./loaded";
import { CALENDAR_LIST_SCALE, calendarCellFigures, calendarCells, calendarDayLine,
         ymd } from "../lib/dates";
import { useSession } from "../lib/session";
import { useRefetchWhenShown } from "../lib/focus";
import { C, mmddyy, money } from "../lib/theme";

const CALENDAR_KEY = ["calendar"];

type CalDay = { date: string; total: number; came_in?: number;
                went_out?: number;
                // name / note: absent from an older server
                events: { label: string; amount: number; kind: string;
                          name?: string; note?: string | null }[] };

/** `month`: a closed month's own calendar (`month_calendar` of that
 *  month's Bills reply) instead of the next five weeks — the bills it paid
 *  on the days they were paid, the paychecks it received, the bills it
 *  left unpaid; no day is "today" and none is dimmed. */
export default function CalendarSection({ month }:
    { month?: { ym: string; title: string; days: CalDay[] } } = {}) {
  const { client } = useSession();
  const query = useQuery({
    queryKey: CALENDAR_KEY,
    queryFn: () => client!.calendar(35),
    enabled: !!client && !month,
  });
  useRefetchWhenShown(query, CALENDAR_KEY);
  const [focus, setFocus] = useState<string | null>(null);
  const { fontScale } = useWindowDimensions();
  const asList = fontScale >= CALENDAR_LIST_SCALE;
  const all: CalDay[] = month ? month.days : query.data?.calendar ?? [];
  const focused = focus ? all.find((d) => d.date === focus) : undefined;
  // device-local today — the server's start is the household's day, not
  // UTC-today, which would highlight tomorrow for a US evening user; the
  // device clock still wins when the phone is in another zone. A closed
  // month has none.
  const todayIso = month ? "" : ymd(new Date());
  // consecutive dates from the Monday of the window's week, each looked
  // up by date — the payload carries only event days, so packing them
  // back-to-back would put every day after the first gap under the wrong
  // weekday header. A month takes the weeks it spans; its neighbours'
  // padding days stay empty.
  const monthWeeks = month ? (() => {
    const first = new Date(`${month.ym}-01T00:00:00`);
    const lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
    return Math.ceil((((first.getDay() + 6) % 7) + lastDay) / 7);
  })() : 5;
  const cells = month
    ? calendarCells(all, `${month.ym}-01`, monthWeeks * 7)
        .map((c) => c.date.slice(0, 7) === month.ym ? c : { date: c.date })
    : query.data ? calendarCells(all, query.data.start, 35) : [];
  const title = month ? month.title : "Next five weeks";
  const weeks: (typeof cells)[] = [];
  for (let i = 0; i < cells.length; i += 7)
    weeks.push(cells.slice(i, i + 7));
  // a fetch that never resolves must not render as nothing at all —
  // the section owns its own query, so it owns saying so too
  if (!month && (query.isPending || (query.isError && !query.data))) {
    return (
      <Card>
        <H>{title}</H>
        {query.isPending ? (
          <Text style={{ color: C.mut, fontSize: 13 }}>Loading…</Text>
        ) : <FailedLine q={query} what="the calendar" />}
      </Card>
    );
  }
  return (
    <>
      <Card>
        <H>{title}</H>
        {asList ? (
          // very large text: the days that carry money, in words
          <View>
            {all.filter((d) => d.date >= todayIso && d.events.length > 0).map((d) => (
              <Pressable key={d.date} accessibilityRole="button"
                         accessibilityState={{ expanded: focus === d.date }}
                         style={[s.listRow, focus === d.date && { borderColor: C.accent }]}
                         onPress={() => setFocus(focus === d.date ? null : d.date)}>
                <Text style={{ color: d.date === todayIso ? C.accent : C.text, fontSize: 14,
                               fontVariant: ["tabular-nums"] }}>
                  {calendarDayLine(d, (v) => money(v, false))}
                </Text>
              </Pressable>
            ))}
          </View>
        ) : <>
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
              const has = (c.d?.events.length ?? 0) > 0;
              return (
                <Pressable accessibilityRole="button" key={ci}
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
                  {has && !past && c.d && calendarCellFigures(
                    c.d, (v) => money(v, false)).map((f) => (
                    // in and out each with its sign, never netted (the
                    // web cell prints the same); wraps rather than cutting
                    // the figure short — past the list threshold the
                    // month is listed instead
                    <Text key={f.tone}
                          style={{ color: f.tone === "in" ? C.good : C.mut,
                                   fontSize: 9, textAlign: "center",
                                   fontVariant: ["tabular-nums"] }}>
                      {f.text}
                    </Text>
                  ))}
                </Pressable>
              );
            })}
          </View>
        ))}
        </>}
        {focused && (
          <View style={{ marginTop: 8 }}>
            <View style={s.head}>
              <Text style={s.date}>{mmddyy(focused.date)}</Text>
              <Text style={s.total}>
                {[(focused.came_in ?? 0) > 0 ? `+${money(focused.came_in!)} in` : null,
                  (focused.went_out ?? 0) > 0 ? `${money(focused.went_out!)} out` : null]
                  .filter(Boolean).join(" · ")}
              </Text>
            </View>
            {focused.events.map((e, i) => (
              <View key={`${e.label}-${i}`} style={s.event}>
                {/* the name, and what its figure is on a line of its
                    own: joined, one line cut "full balance" mid-word */}
                <View style={{ flex: 1 }}>
                  <Text style={{ color: C.text, fontSize: 13 }}
                        numberOfLines={1}>
                    {e.name !== undefined
                      ? e.name
                      // older-server path: stands in for `name`
                      : e.label}
                  </Text>
                  {!!e.note && (
                    <Text style={{ color: C.mut, fontSize: 11 }}>{e.note}</Text>
                  )}
                </View>
                {/* a bill on a card moves no checking that day: muted,
                    as the web greys it */}
                <Text style={{ color: e.amount > 0 ? C.good
                                 : e.kind === "on_card" ? C.mut : C.text,
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
  listRow: { paddingVertical: 10, paddingHorizontal: 8, minHeight: 44,
             borderRadius: 6, borderWidth: 1, borderColor: "transparent",
             justifyContent: "center" },
  gridCell: { flex: 1, minHeight: 44, alignItems: "center",
              justifyContent: "center", borderRadius: 6, margin: 1,
              backgroundColor: C.bg },
  gridToday: { backgroundColor: C.hover, borderColor: C.accent,
               borderWidth: 1 },
});
