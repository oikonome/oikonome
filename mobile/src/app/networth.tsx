// Net worth — the web page whole: hero + tiles, full-history trend,
// asset-class/institution shares, retirement & Coinbase P/L tables,
// investment fees, and the property table with the manual-assets
// editor (the page's only mutation).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View,
         useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import Sparkline from "../components/sparkline";
import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink, KV } from "../components/ui";
import { errText, ManualAsset, PLRow } from "../lib/api";
import { saveSettings } from "../lib/cache";
import { RANGE_LABEL, rangeCaption, TREND_RANGES, trendWindow,
         type TrendRange } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";
import { catLabel } from "../lib/pure";

const m$ = (n: number) => money(n, false);
const plTxt = (v: number) => `${v >= 0 ? "+" : "−"}${m$(Math.abs(v))}`;
const pctTxt = (p: number | null | undefined) =>
  p != null ? `${p >= 0 ? "+" : ""}${p.toFixed(1)}%` : "—";

function PLLine({ name, invested, value, pl, pct, indent }: {
  name: string; invested?: number; value: number; pl: number;
  pct: number | null; indent?: boolean;
}) {
  return (
    <View style={[s.plRow, indent && { paddingLeft: 16 }]}>
      <Text style={[indent ? s.mut : s.plName, { flex: 1 }]}
            numberOfLines={1}>{name}</Text>
      <Text style={s.plNum}>{invested != null ? m$(invested) : "—"}</Text>
      <Text style={s.plNum}>{m$(value)}</Text>
      <Text style={[s.plNum, { color: pl >= 0 ? C.good : C.bad }]}>
        {plTxt(pl)}
      </Text>
      <Text style={[s.plNum, { width: 52,
                     color: pl >= 0 ? C.good : C.bad }]}>
        {pctTxt(pct)}
      </Text>
    </View>
  );
}

export default function NetWorth() {
  const { client } = useSession();
  const { width } = useWindowDimensions();
  const query = useQuery({
    queryKey: ["report", "networth"],
    queryFn: () => client!.reportNetworth(),
    enabled: !!client,
  });
  const fees = useQuery({
    queryKey: ["report", "fees"],
    queryFn: () => client!.reportFees(),
    enabled: !!client,
  });
  const d = query.data;
  const fullTrend = d?.trend ?? [];
  // timeframe toggles — same ranges, math and semantics as the web's
  // NetWorth trend card (trendWindow lives in pure.ts, runner-tested)
  const [range, setRange] = useState<TrendRange>("all");
  const w = trendWindow(fullTrend, range);
  const sliced = fullTrend.slice(w.start, w.end + 1);
  const eu = d?.trend_estimated_until !== undefined
    && d.trend_estimated_until - w.start > 0
    ? d.trend_estimated_until - w.start : undefined;
  const delta = fullTrend.length >= 2
    ? fullTrend[fullTrend.length - 1][1]
      - fullTrend[fullTrend.length - 2][1]
    : null;
  const mortgage = (d?.property_items ?? [])
    .filter((p) => p.kind === "mortgage")
    .reduce((t, p) => t + p.value, 0);
  const rc = d?.retirement_combined;
  const rtot = rc?.total;
  // share percentages beside each slice — the web's donut labels;
  // non-positive rows drop out of the shares like the web's Donut
  const withPct = (rows: [string, number][]) => {
    const pos = rows.filter(([, v]) => v > 0);
    const total = pos.reduce((t, [, v]) => t + v, 0) || 1;
    return pos.map(([n, v]) =>
      [n, v, Math.round((100 * v) / total)] as [string, number, number]);
  };

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {d && (
        <Card>
          <Text style={s.mut}>Total net worth</Text>
          <Text style={s.big}>
            {m$(d.full_total)}
            {delta !== null && (
              <Text style={{ fontSize: 14,
                             color: delta >= 0 ? C.good : C.bad }}>
                {"  "}{plTxt(delta)} this month
              </Text>
            )}
          </Text>
          <KV k="Financial accounts" v={m$(d.current_total)} />
          <Text style={s.mut}>cash + investments − card debt · live</Text>
          <KV k="Property & vehicles, net" v={m$(d.property_net)} />
          <Text style={s.mut}>
            after {m$(Math.abs(mortgage))} mortgage payoff
          </Text>
        </Card>
      )}
      {fullTrend.length > 1 && (
        <Card>
          <H>Over time</H>
          <View style={{ flexDirection: "row", gap: 6, marginBottom: 8,
                         alignItems: "center", flexWrap: "wrap" }}>
            {TREND_RANGES.map((r) => {
              // a range longer than the history equals All — one view,
              // one button (mirrors the web's disabled state)
              const dead = r !== "all" && trendWindow(fullTrend, r).full;
              return (
                <Pressable key={r} disabled={dead}
                    onPress={() => setRange(r)}
                    style={{ paddingVertical: 3, paddingHorizontal: 9,
                             borderRadius: 999, borderWidth: 1,
                             opacity: dead ? 0.35 : 1,
                             borderColor: range === r ? C.accent : C.border,
                             backgroundColor: range === r
                               ? C.accent + "22" : "transparent" }}>
                  <Text style={{ fontSize: 12,
                                 color: range === r ? C.accent : C.mut }}>
                    {RANGE_LABEL[r]}</Text>
                </Pressable>
              );
            })}
          </View>
          {w.gain !== null && (
            <Text style={{ fontSize: 12, marginBottom: 6,
                           color: w.gain >= 0 ? C.good : C.bad }}>
              {plTxt(w.gain)}
              {w.pct !== null
                ? ` · ${w.pct >= 0 ? "+" : ""}${w.pct.toFixed(1)}%` : ""}
              <Text style={{ color: C.mut }}>
                {"  over "}{rangeCaption(range)}</Text>
            </Text>
          )}
          {/* the reconstructed prefix draws dashed — an estimate must
              not look like a measurement (the web's estimatedUntil) */}
          <Sparkline width={width - 24 - 28} data={sliced}
                     dashUntil={eu} />
        </Card>
      )}
      {d && d.by_asset_class.length > 0 && (
        <Card>
          <H>By asset class</H>
          {withPct(d.by_asset_class).map(([name, v, p]) => (
            <KV key={name} k={`${name} · ${p}%`} v={m$(v)} />
          ))}
        </Card>
      )}
      {d && d.by_institution.length > 0 && (
        <Card>
          <H>By institution</H>
          {withPct(d.by_institution).map(([name, v, p]) => (
            <KV key={name} k={`${name} · ${p}%`} v={m$(v)} />
          ))}
        </Card>
      )}

      {rtot && rc?.groups && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "baseline",
                         gap: 8 }}>
            <View style={{ flexShrink: 1 }}>
              <H>Retirement &amp; investments</H>
              <Text style={s.mut}>
                {rc.groups.map((g) => g.provider).join(" + ")}, by
                account
              </Text>
            </View>
            <Text style={{ marginLeft: "auto", fontWeight: "700",
                           color: rtot.pl >= 0 ? C.good : C.bad,
                           fontSize: 14 }}>
              {plTxt(rtot.pl)} · {pctTxt(rtot.pct)}
            </Text>
          </View>
          <View style={s.plRow}>
            <Text style={[s.mut, { flex: 1 }]}> </Text>
            <Text style={s.plHead}>Invested</Text>
            <Text style={s.plHead}>Value</Text>
            <Text style={s.plHead}>P/L</Text>
            <Text style={[s.plHead, { width: 52 }]}>Ret.</Text>
          </View>
          {rc.groups.map((g) => (
            <View key={g.provider}>
              <PLLine name={g.provider} invested={g.invested}
                      value={g.value} pl={g.pl} pct={g.pct} />
              {g.accounts.map((a) => (
                <PLLine key={a.name} name={a.name}
                        invested={a.invested ?? 0} value={a.value}
                        pl={a.pl} pct={a.pct} indent />
              ))}
            </View>
          ))}
          <View style={{ borderTopColor: C.text, borderTopWidth: 1 }}>
            <PLLine name="Total" invested={rtot.invested}
                    value={rtot.value} pl={rtot.pl} pct={rtot.pct} />
          </View>
        </Card>
      )}

      {(d?.coinbase_pl ?? []).length > 0 && (
        <Card>
          <H>
            Coinbase{" "}
            <Text style={[s.mut, { fontWeight: "400" }]}>
              — anchored to bank-side flows
            </Text>
          </H>
          <View style={s.plRow}>
            <Text style={s.cbHead}>Cash in</Text>
            <Text style={s.cbHead}>Cash out</Text>
            <Text style={s.cbHead}>Value</Text>
            <Text style={s.cbHead}>P/L</Text>
            <Text style={[s.cbHead, s.cbPct]}>Ret.</Text>
          </View>
          {d!.coinbase_pl.map((c) => (
            <View key={c.name}
                  style={c.name === "Combined"
                    ? { borderTopColor: C.text, borderTopWidth: 1 }
                    : { borderTopColor: C.border,
                        borderTopWidth: StyleSheet.hairlineWidth }}>
            {/* five number columns fill a phone's width, so the name takes
                its own line above them, and the columns share the card's
                width, so they never run past the edge of a narrow phone */}
            <Text style={[s.plName, { marginTop: 6 }]} numberOfLines={1}>
              {c.name}</Text>
            <View style={[s.plRow, { borderTopWidth: 0 }]}>
              <Text style={s.cbNum}>{m$(c.cash_in ?? 0)}</Text>
              <Text style={s.cbNum}>{m$(c.cash_out ?? 0)}</Text>
              <Text style={s.cbNum}>{m$(c.value)}</Text>
              <Text style={[s.cbNum,
                             { color: c.pl >= 0 ? C.good : C.bad }]}>
                {plTxt(c.pl)}
              </Text>
              <Text style={[s.cbNum, s.cbPct, {
                             color: c.pl >= 0 ? C.good : C.bad }]}>
                {c.pct != null
                  ? `${c.pct >= 0 ? "+" : ""}${c.pct.toFixed(0)}%` : "—"}
              </Text>
            </View>
            </View>
          ))}
        </Card>
      )}

      {fees.data && fees.data.by_platform.length > 0 && (
        <Card>
          <H>
            Investment fees{" "}
            <Text style={[s.mut, { fontWeight: "400" }]}>
              (advisory / admin / participant / account, all time)
            </Text>
          </H>
          <Text style={{ color: C.bad, fontSize: 20, fontWeight: "700" }}>
            {m$(fees.data.total)}
          </Text>
          {fees.data.by_platform.map(([inst, amt, n]) => (
            <KV key={inst} k={inst} v={`${m$(amt)} · ${n}`} tone="mut" />
          ))}
          {/* labeled like the web's own card — a year and a platform
              must not read identically */}
          {fees.data.by_year.length > 0 && (
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "700", marginTop: 8 }}>
              Fees by year
            </Text>
          )}
          {[...fees.data.by_year].reverse().slice(0, 20).map(([y, v]) => (
            <KV key={y} k={String(y)} v={m$(v)} tone="mut" />
          ))}
        </Card>
      )}

      {d && d.property_items.length > 0 && (
        <Card>
          <H>Property, vehicles &amp; mortgage</H>
          {d.property_items.map((p) => (
            <View key={p.name}>
              <KV k={`${p.name} · ${catLabel(p.kind)}`}
                  v={m$(p.value)}
                  tone={p.value < 0 ? "warn" : undefined} />
              {p.source ? (
                <Text style={[s.mut, { marginTop: -2 }]}>{p.source}</Text>
              ) : null}
            </View>
          ))}
          <ManualAssets />
        </Card>
      )}
      {d && d.property_items.length === 0 && <Card><ManualAssets /></Card>}
      {/* the caveat stands at the page foot even when the trend card
          can't render — a thin-history tenant needs it most (web rule) */}
      {d && (
        <Text style={[s.mut, { marginHorizontal: 12, marginTop: 8 }]}>
          {d._built_at ? `Reports cached ${d._built_at} · ` : ""}
          history before the first recorded balance is reconstructed
          from holdings × historical prices, with transfers netted out
        </Text>
      )}
      <HelpLink topic="net-worth" />
    </ScrollView>
  );
}

// manual_assets editor — add/edit/delete; editing a value clears as_of
// so the server stamps today
function ManualAssets() {
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(),
    enabled: !!client && !viewer });
  const [rows, setRows] = useState<ManualAsset[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    // the reply seeds ["settings"], so "Edit assets" right after Save
    // opens on the list just written — re-seeding from the stale copy
    // would let the next save overwrite this one
    mutationFn: (assets: ManualAsset[]) =>
      saveSettings(qc, client!, { manual_assets: assets }),
    onSuccess: () => {
      setErr(null); setRows(null);
      // the report itself is rebuilt server-side from the new assets
      qc.invalidateQueries({ queryKey: ["report", "networth"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  if (viewer || !settings.data) return null;
  const saved =
    (settings.data.manual_assets as ManualAsset[] | null) ?? [];
  if (rows === null) {
    return (
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 8 }]}
                 onPress={() => setRows(saved.map((a) => ({ ...a })))}>
        <Text style={s.btnText}>
          {saved.length ? "Edit assets"
            : "Add property, vehicle or mortgage"}
        </Text>
      </Pressable>
    );
  }
  const upd = (i: number, patch: Partial<ManualAsset>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const KINDS = ["property", "vehicle", "mortgage", "other"];
  return (
    <View style={{ marginTop: 8, gap: 8 }}>
      {err ? (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setErr(null)}>{err}</Text>
      ) : null}
      {rows.map((r, i) => (
        <View key={i} style={{ borderColor: C.border, borderWidth: 1,
                               borderRadius: 8, padding: 10, gap: 6 }}>
          <TextInput style={s.input} placeholder="Home"
                     placeholderTextColor={C.mut} value={r.name}
                     onChangeText={(v) => upd(i, { name: v })} />
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
            {KINDS.map((k) => (
              <Pressable key={k}
                         style={[s.chip, (r.kind ?? "other") === k
                           && s.chipOn]}
                         onPress={() => upd(i, { kind: k })}>
                <Text style={{ color: (r.kind ?? "other") === k
                                 ? C.text : C.mut, fontSize: 11 }}>
                  {k === "mortgage" ? "mortgage (negative)" : k}
                </Text>
              </Pressable>
            ))}
          </View>
          <TextInput style={s.input} placeholder="value ($)"
                     placeholderTextColor={C.mut}
                     keyboardType="numbers-and-punctuation"
                     value={String(r.value)}
                     onChangeText={(v) => upd(i, {
                       value: Number(v.replace(/[$,\s]/g, "") || 0),
                       as_of: null })} />
          {r.auto ? (
            <Text style={s.mut}>
              auto-valued ({r.auto}) — a manual value edit wins until
              the next auto update
            </Text>
          ) : null}
          <Text style={{ color: C.bad, fontSize: 12 }}
                onPress={() => setRows(rows.filter((_, j) => j !== i))}>
            Remove
          </Text>
        </View>
      ))}
      <View style={{ flexDirection: "row", gap: 8 }}>
        <Pressable style={[s.btn, s.btnQuiet]}
                   onPress={() => setRows([...rows,
                     { name: "", kind: "property", value: 0 }])}>
          <Text style={[s.btnText, { color: C.mut }]}>Add asset</Text>
        </Pressable>
        <Pressable style={[s.btn, save.isPending && { opacity: 0.5 }]}
                   disabled={save.isPending}
                   onPress={() => save.mutate(
                     rows.filter((r) => r.name.trim()))}>
          <Text style={s.btnText}>
            {save.isPending ? "saving…" : "Save assets"}
          </Text>
        </Pressable>
        <Pressable style={[s.btn, s.btnQuiet]}
                   onPress={() => { setRows(null); setErr(null); }}>
          <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
        </Pressable>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  big: { color: C.text, fontSize: 30, fontWeight: "700",
         marginVertical: 6, fontVariant: ["tabular-nums"] },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  plRow: { flexDirection: "row", alignItems: "center", gap: 6,
           borderTopColor: C.border,
           borderTopWidth: StyleSheet.hairlineWidth,
           paddingVertical: 5 },
  plName: { color: C.text, fontSize: 13, fontWeight: "600" },
  plNum: { color: C.text, fontSize: 12, width: 74, textAlign: "right",
           fontVariant: ["tabular-nums"] },
  plHead: { color: C.mut, fontSize: 10, width: 74, textAlign: "right" },
  // the Coinbase table has no name column: its five columns split the row
  cbNum: { color: C.text, fontSize: 12, flex: 1, minWidth: 0,
           textAlign: "right", fontVariant: ["tabular-nums"] },
  cbHead: { color: C.mut, fontSize: 10, flex: 1, minWidth: 0,
            textAlign: "right" },
  cbPct: { flex: 0.7 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 7 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 9, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
});
