// Net worth — the web page whole: hero + tiles, full-history trend,
// asset-class/institution shares, retirement & Coinbase P/L tables,
// investment fees, and the property table with the manual-assets
// editor (the page's only mutation).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View,
         useWindowDimensions } from "react-native";
import { useRouter } from "expo-router";
import PullRefresh from "../components/pull-refresh";

import Sparkline from "../components/sparkline";
import { FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../components/loaded";
import { Card, H, HelpLink, KV, Skeleton, Note } from "../components/ui";
import { errText, ManualAsset, PLRow, RingFigures } from "../lib/api";
import { SETTINGS_SAVES, saveSettings } from "../lib/cache";
import { overTimeWindow } from "../lib/networth-pure";
import { changeWords, DEFAULT_TREND_RANGE, overCaption, signedPct,
         type TrendRange } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, mmddyy, money } from "../lib/theme";
import { catLabel, nameKeys, ownedSinceIso, parseMoneyDraft,
         rebaseNamedSave } from "../lib/pure";
import { RangePicker } from "../components/segmented-control";
import { fieldA11y } from "../components/field";

const m$ = (n: number) => money(n, false);
// a change's colour: "unchanged" is plain (changeWords)
const toneColor = (v: number) => {
  const t = changeWords(v, m$).tone;
  return t === "pos" ? C.good : t === "neg" ? C.bad : C.mut;
};
const plTxt = (v: number) => `${v >= 0 ? "+" : "−"}${m$(Math.abs(v))}`;
const pctTxt = (p: number | null | undefined) =>
  p != null ? signedPct(p) : "—";

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

/** A breakdown list (the web Donut's legend): the drawn rows with their
 *  shares, the owed rows, and the net when anything is owed — all the
 *  server's `shown` figures, whole dollars adding up as shown and shares
 *  adding up to 100. An older server sends no `shown`: the rows are then
 *  rounded one by one and carry no share; none is worked out here. */
function Breakdown({ title, rows, ring }: {
  title: string; rows: [string, number][]; ring?: RingFigures;
}) {
  const shown = ring?.shown;
  // older-server path: stands in for `rings.*.shown` (the raw rows)
  const drawn = shown
    ? shown.rows.filter((r) => r.share !== null)
        .map((r) => [r.label, r.dollars, r.share] as const)
    : rows.filter(([, v]) => v > 0).map(([n, v]) => [n, v, null] as const);
  // older-server path: stands in for `rings.*.shown` (the raw rows)
  const owed = shown
    ? shown.rows.filter((r) => r.owed).map((r) => [r.label, r.dollars] as const)
    : rows.filter(([, v]) => v < 0);
  // older-server path: stands in for `rings.*.shown.net`
  const net = shown ? shown.net : ring?.net;
  return (
    <Card>
      <H>{title}</H>
      {/* the web ring's centre figure: what is owned, named "Assets" when
          owed rows follow it, else "Total" (the server's shown.owned) */}
      {shown && (
        <KV k={owed.length ? "Assets" : "Total"} v={m$(shown.owned)} />)}
      {/* keyed by position: two rows can share a label (an unnamed
          connection's "Other" beside an institution named "Other") */}
      {drawn.map(([name, v, p], i) => (
        <KV key={i} k={p !== null ? `${name} · ${p}%` : name} v={m$(v)} />
      ))}
      {/* the web legend's words for a row the ring cannot draw */}
      {owed.map(([name, v], i) => (
        <KV key={`owed-${i}`} k={`${name} · owed · not drawn`} v={m$(v)}
            tone="bad" />
      ))}
      {owed.length > 0 && net !== undefined && (
        <KV k="Net" v={m$(net)} />
      )}
    </Card>
  );
}

export default function NetWorth() {
  const { client } = useSession();
  const router = useRouter();
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
  const read = query.data;
  // nothing linked and nothing entered by hand: the way to start, not a
  // "$0" total (the web's rule)
  const noAccounts = useNoAccounts();
  const nothingYet = noAccounts && !!read && read.property_items.length === 0;
  const d = nothingYet ? undefined : read;
  // Over time charts the same total as the headline — `full_trend`,
  // property included — over the server's window for the timeframe
  const fullTrend = d?.full_trend ?? [];
  // the site-wide default window (specs/timeframes.md: default 1y)
  const [range, setRange] = useState<TrendRange>(DEFAULT_TREND_RANGE);
  const w = overTimeWindow(d?.trend_windows, range, fullTrend.length);
  const sliced = fullTrend.slice(w.start, w.end + 1);
  const est = d?.full_trend_estimated_until;
  const eu = est !== undefined && est - w.start > 0 ? est - w.start : undefined;
  // the hero's and the tiles' changes, server-decided (an older server
  // sends none, and none is shown)
  const delta = d?.month_change ?? null;
  const finDelta = d?.financial_month_change ?? null;
  const propDelta = d?.property_month_change ?? null;
  // what is owed cannot be a share of what is owned: listed after the
  const mortgage = d?.mortgage_payoff ?? null;
  const rc = d?.retirement_combined;
  const rtot = rc?.total;
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleLine query={query} />
      {query.isPending && <Skeleton />}
      {/* a failed first read says so: blank, the screen read as broken */}
      {d === undefined && query.isError && (
        <FailedLine q={query} what="net worth" />)}
      {nothingYet && <Card><NoAccountsLine /></Card>}
      {d && (
        <Card>
          <Text style={s.mut}>Total net worth</Text>
          <Text style={s.big}>
            {m$(d.full_total)}
            {delta !== null && (
              <Text style={{ fontSize: 14, color: toneColor(delta) }}>
                {"  "}{changeWords(delta, m$).text} this month
              </Text>
            )}
          </Text>
          {/* what that change is made of, and money withdrawn that no
              linked account has received — the server's two lines, as
              sent (an older server sends neither) */}
          {d.month_change_text ? (
            <Text style={s.mut}>{d.month_change_text}</Text>) : null}
          {d.month_withdrawn_text ? (
            <Text style={s.mut}>{d.month_withdrawn_text}</Text>) : null}
          <KV k="Financial accounts" v={m$(d.current_total)} />
          <Text style={s.mut}>
            cash + investments − card debt · live
            {/* the web tile's link to the payoff plan */}
            {" · "}
            <Text style={{ color: C.accent }} accessibilityRole="link"
                  onPress={() => router.push("/debt")}>payoff plan</Text>
            {finDelta !== null && (
              <Text style={{ color: toneColor(finDelta) }}>
                {" · "}{changeWords(finDelta, m$).text} this month
              </Text>
            )}
          </Text>
          <KV k="Property & vehicles, net" v={m$(d.property_net)} />
          <Text style={s.mut}>
            {mortgage !== null ? `after ${m$(mortgage)} mortgage payoff` : ""}
            {/* a typed-in balance says how old it is (the server's words;
                an older server sends none) */}
            {mortgage !== null && d.mortgage_entered_text
              ? ` · ${d.mortgage_entered_text}` : ""}
            {propDelta !== null && Math.abs(propDelta) >= 0.5 && (
              <Text style={{ color: toneColor(propDelta) }}>
                {" · "}{changeWords(propDelta, m$).text} this month
              </Text>
            )}
          </Text>
        </Card>
      )}
      {/* shown from the first balance on, as on the web: a household
          with one day says so below rather than losing the card */}
      {d && (
        <Card>
          <H>Over time</H>
          <RangePicker value={range} onChange={setRange}
            dead={(r) => overTimeWindow(d?.trend_windows, r,
                                        fullTrend.length).full}
            style={{ marginBottom: 8 }} />
          {w.gain !== null && (
            <Text style={{ fontSize: 12, marginBottom: 6,
                           color: w.gain >= 0 ? C.good : C.bad }}>
              {plTxt(w.gain)}
              {w.pct !== null
                ? ` · ${signedPct(w.pct)}` : ""}
              <Text style={{ color: C.mut }}>
                {"  "}{overCaption(range)}
                {/* no percent to give the base: the server says where
                    the gain is measured from (the web's words) */}
                {w.from_text ? ` · ${w.from_text}` : ""}</Text>
            </Text>
          )}
          {/* the server's words when the window crosses a typed-in
              property: the gain above is then the financial accounts' */}
          {!!w.note && (
            <Text style={{ fontSize: 11, marginBottom: 6, color: C.mut }}>
              {w.note}</Text>
          )}
          {/* the reconstructed prefix draws dashed — an estimate must
              not look like a measurement (the web's estimatedUntil) */}
          {sliced.length < 2 ? (
            // a household linked today has one balance and no history yet
            <Text style={s.mut}>
              {sliced.length ? `${m$(sliced[0][1])} today. ` : ""}
              The line starts once a second day&apos;s balance is in.
            </Text>
          ) : (
            <Sparkline width={width - 24 - 28} data={sliced}
                       subject="Net worth"
                       dashUntil={eu} />)}
        </Card>
      )}
      {d && d.by_asset_class.length > 0 && (
        <Breakdown title="By asset class" rows={d.by_asset_class}
                   ring={d.rings?.by_asset_class} />
      )}
      {d && d.by_institution.length > 0 && (
        <Breakdown title="By institution" rows={d.by_institution}
                   ring={d.rings?.by_institution} />
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
              {plTxt(rtot.pl)}
              {/* no return to give is no " · —" (the web's header) */}
              {rtot.pct !== null && rtot.pct !== undefined
                ? ` · ${signedPct(rtot.pct)}` : ""}
            </Text>
          </View>
          <View style={s.plRow}>
            <Text style={[s.mut, { flex: 1 }]}> </Text>
            <Text style={s.plHead}>Invested</Text>
            <Text style={s.plHead}>Value now</Text>
            <Text style={s.plHead}>P/L</Text>
            <Text style={[s.plHead, { width: 52 }]}>Return</Text>
          </View>
          {rc.groups.map((g) => (
            <View key={g.provider}>
              <PLLine name={g.provider} invested={g.invested}
                      value={g.value} pl={g.pl} pct={g.pct} />
              {g.accounts.map((a) => (
                <PLLine key={a.name} name={a.name}
                        invested={a.invested} value={a.value}
                        pl={a.pl} pct={a.pct} indent />
              ))}
            </View>
          ))}
          <View style={{ borderTopColor: C.text, borderTopWidth: 1 }}>
            <PLLine name="Total" invested={rtot.invested}
                    value={rtot.value} pl={rtot.pl} pct={rtot.pct} />
          </View>
          {rc.pl_note ? (
            <Text style={[s.mut, { fontSize: 12, marginTop: 6 }]}>
              {rc.pl_note}</Text>
          ) : null}
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
            {/* the web table's column names */}
            <Text style={s.cbHead}>Cash in (bank)</Text>
            <Text style={s.cbHead}>Cash out</Text>
            <Text style={s.cbHead}>Value now</Text>
            <Text style={s.cbHead}>P/L</Text>
            <Text style={[s.cbHead, s.cbPct]}>Return</Text>
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
              <Text style={s.cbNum}>{m$(c.cash_in)}</Text>
              <Text style={s.cbNum}>{m$(c.cash_out)}</Text>
              <Text style={s.cbNum}>{m$(c.value)}</Text>
              <Text style={[s.cbNum,
                             { color: c.pl >= 0 ? C.good : C.bad }]}>
                {plTxt(c.pl)}
              </Text>
              <Text style={[s.cbNum, s.cbPct, {
                             color: c.pl >= 0 ? C.good : C.bad }]}>
                {c.pct != null
                  ? signedPct(c.pct) : "—"}
              </Text>
            </View>
            </View>
          ))}
        </Card>
      )}

      {/* a failed read keeps the card's place: gone, it read as never
          charged */}
      {fees.data === undefined && fees.isError && (
        <Card>
          <H>Investment fees</H>
          <FailedLine q={fees} what="the investment fees" />
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
          {/* a connection with no institution name is sent as null */}
          {/* the count in the server's words ("40 charges", "5 years,
              estimated"); an older server sends none and none is shown */}
          {fees.data.by_platform.map(([inst, amt], i) => (
            <KV key={inst ?? ""} k={inst ?? ""}
                v={`${m$(amt)}${fees.data!.platform_counts?.[i]
                  ? ` · ${fees.data!.platform_counts[i]}` : ""}`}
                tone="mut" />
          ))}
          {/* labeled like the web's own card — a year and a platform
              must not read identically */}
          {fees.data.by_year.length > 0 && (
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "700", marginTop: 8 }}>
              Fees by year
            </Text>
          )}
          {/* every year, as the web's bars */}
          {[...fees.data.by_year].reverse().map(([y, v]) => (
            <KV key={y} k={String(y)} v={m$(v)} tone="mut" />
          ))}
        </Card>
      )}

      {/* the card stands with no items too, as the web's: a viewer sees
          its title rather than an empty box */}
      {d && (
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
              {/* a typed-in loan balance carries its age, toned as a
                  warning once it asks for an update (the web's line) */}
              {p.entered_text ? (
                <Text style={[s.mut, p.update_due ? { color: C.warn } : null]}>
                  {p.entered_text}</Text>
              ) : null}
            </View>
          ))}
          <ManualAssets />
        </Card>
      )}
      {/* the caveat stands at the page foot even when the trend card
          can't render — a thin-history tenant needs it most (web rule) */}
      {d && (
        <Text style={[s.mut, { marginHorizontal: 12, marginTop: 8 }]}>
          {d._built_at
            // built on this request — nothing is cached
            ? `Figures as of ${
                /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(d._built_at)
                  ? `${mmddyy(d._built_at)} ${d._built_at.slice(11, 16)}`
                  : d._built_at} · `
            : ""}
          history before the first recorded balance is reconstructed
          from holdings × historical prices, with transfers netted out
        </Text>
      )}
      <HelpLink topic="net-worth" />
    </ScrollView>
  );
}

// manual_assets editor — add/edit/delete; "Owned since" (acquired_on) is
// the month an item enters the trend; editing a value clears as_of
// so the server stamps today
function ManualAssets() {
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(),
    enabled: !!client && !viewer });
  // `draft` is the value TEXT while it is typed; it is parsed only at
  // save, so "-", "-0." and "1200." can be typed on the way to a number.
  // `orig` is the row's key in `opened`, the list as it was when Edit was
  // pressed (absent: added here). `sent` is the row's place in the list
  // the last Save wrote, and `lastSent` that list: when its reply is lost
  // the next Save checks whether it landed, so nothing goes in twice.
  // `since` is the "Owned since" TEXT while it is typed, read at save
  type Row = ManualAsset & { draft?: string; since?: string; orig?: string;
                             sent?: number };
  type Edited = ManualAsset & { orig?: string; sent?: number };
  const [rows, setRows] = useState<Row[] | null>(null);
  const [opened, setOpened] = useState<ManualAsset[]>([]);
  const lastSent = useRef<ManualAsset[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // the row whose value stopped the save: its field is marked, as the web's
  const [errAt, setErrAt] = useState<number | null>(null);
  const save = useMutation({
    scope: SETTINGS_SAVES,
    // The server replaces the whole list, and this one is a copy from
    // when Edit was pressed: the nightly valuation or another device may
    // have saved since. Read the stored list now and lay only this
    // editor's changes over it.
    // The reply seeds ["settings"], so "Edit assets" right after Save
    // opens on the list just written.
    mutationFn: async ({ edited, at }: { edited: Edited[]; at: number[] }) => {
      // the read the merge is built on is also the basis the save names
      // the list's version from: a save another device lands after it is
      // refused and merged again, never overwritten
      const view = await client!.settingsFull();
      const live = (view.manual_assets as ManualAsset[] | null) ?? [];
      const r = rebaseNamedSave(edited, opened, live, lastSent.current);
      lastSent.current = r.list;
      setOpened(r.baseline);
      setRows((cur) => cur && cur.flatMap((row, j) => {
        const n = at.indexOf(j);
        if (n < 0) return [row];
        const k = r.rows[n];
        return k ? [{ ...row, ...k }] : [];
      }));
      return saveSettings(qc, client!, { manual_assets: r.list }, view);
    },
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
      <Pressable accessibilityRole="button"
                 style={[s.btn, { alignSelf: "flex-start", marginTop: 8 }]}
                 onPress={() => {
                   const keys = nameKeys(saved);
                   setOpened(saved);
                   lastSent.current = null;
                   setRows(saved.map((a, i) => ({ ...a, orig: keys[i] })));
                 }}>
        <Text style={s.btnText}>
          {saved.length ? "Edit assets"
            : "Add property, vehicle or mortgage"}
        </Text>
      </Pressable>
    );
  }
  const upd = (i: number, patch: Partial<Row>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  // settle every typed value; one that is not a number stops the save
  // and says which row, rather than going out as NaN (sent as null → $0)
  const submit = () => {
    setErrAt(null);
    const out: Edited[] = [];
    const at: number[] = [];
    for (const [j, { draft, since, ...r }] of rows.entries()) {
      if (!r.name.trim()) continue;
      at.push(j);
      if (since !== undefined) {
        const got = ownedSinceIso(since);
        if ("error" in got) {
          setErr(`${r.name.trim()}: ${got.error}`);
          return;
        }
        r.acquired_on = got.iso || null;
      }
      if (draft === undefined) { out.push(r); continue; }
      const v = draft.trim() === "" ? 0 : parseMoneyDraft(draft);
      if (v === null) {
        setErr(`${r.name.trim()}: the value is not a number`);
        setErrAt(j);
        return;
      }
      out.push({ ...r, value: v });
    }
    save.mutate({ edited: out, at });
  };
  const KINDS = ["property", "vehicle", "mortgage", "other"];
  return (
    <View style={{ marginTop: 8, gap: 8 }}>
      {err ? (
        <Note bad style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setErr(null)}>{err}</Note>
      ) : null}
      {rows.map((r, i) => (
        <View key={i} style={{ borderColor: C.border, borderWidth: 1,
                               borderRadius: 8, padding: 10, gap: 6 }}>
          <TextInput {...fieldA11y("Asset name")} style={s.input} placeholder="Home"
                     placeholderTextColor={C.mut} value={r.name}
                     onChangeText={(v) => upd(i, { name: v })} />
          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
            {KINDS.map((k) => (
              <Pressable accessibilityRole="button" key={k}
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
          <TextInput {...fieldA11y(`${r.name || "Asset"} value ($)`,
                                   errAt === i ? err : null)}
                     style={[s.input, errAt === i && { borderColor: C.bad }]}
                     placeholder="value ($)"
                     placeholderTextColor={C.mut}
                     keyboardType="numbers-and-punctuation"
                     value={r.draft ?? String(r.value)}
                     onChangeText={(v) => upd(i, { draft: v,
                                                   as_of: null })} />
          {/* the month the item enters the trend; a value edit never
              moves it, and left blank the server keeps the stored date
              (a new item: today) */}
          <TextInput {...fieldA11y(`${r.name || "Asset"} owned since (YYYY-MM-DD)`)}
                     style={s.input} placeholder="owned since (YYYY-MM-DD)"
                     placeholderTextColor={C.mut}
                     keyboardType="numbers-and-punctuation"
                     value={r.since ?? (r.acquired_on ?? "")}
                     onChangeText={(v) => upd(i, { since: v })} />
          <Text style={{ color: C.bad, fontSize: 12 }}
                onPress={() => { setRows(rows.filter((_, j) => j !== i));
                                 setErrAt(null); }}>
            Remove
          </Text>
        </View>
      ))}
      <View style={{ flexDirection: "row", gap: 8 }}>
        <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]}
                   onPress={() => setRows([...rows,
                     { name: "", kind: "property", value: 0 }])}>
          <Text style={[s.btnText, { color: C.mut }]}>Add asset</Text>
        </Pressable>
        <Pressable accessibilityRole="button" style={[s.btn, save.isPending && { opacity: 0.5 }]}
                   disabled={save.isPending}
                   onPress={submit}>
          <Text style={s.btnText}>
            {save.isPending ? "saving…" : "Save assets"}
          </Text>
        </Pressable>
        <Pressable accessibilityRole="button" style={[s.btn, s.btnQuiet]}
                   onPress={() => { setRows(null); setErr(null);
                                    setErrAt(null); }}>
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
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 7 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 9, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
});
