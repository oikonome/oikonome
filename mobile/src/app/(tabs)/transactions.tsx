// Transactions — month browse with ‹ › month nav and search. FlashList
// (recycled cells) because the web page's row-render cost is a known
// hazard, and a month of ledger rows is exactly the shape it's for.
import { FlashList, FlashListRef } from "@shopify/flash-list";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter, useScrollToTop } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Text, TextInput,
         View } from "react-native";
import DatePicker, { DateField } from "../../components/date-picker";
import PullRefresh from "../../components/pull-refresh";
import Select from "../../components/select";
import Sheet, { sheetStyles as b } from "../../components/sheet";

import StaleBanner from "../../components/stale-banner";
import TxnRow from "../../components/txn-row";
import { errText, setHandedOffTxn, Txn, TxnPage, TxnQuery }
  from "../../lib/api";
import { patchQueries } from "../../lib/cache";
import { DayHead, TREND_RANGES, withDayHeads, catLabel, filterCategories,
         ledgerRange } from "../../lib/pure";
import { useSession } from "../../lib/session";
import { useViewer } from "../../lib/viewer";
import { C, mmddyy, money } from "../../lib/theme";

const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December"];

export default function Transactions() {
  const { client } = useSession();
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<FlashListRef<Txn | DayHead>>(null);
  useScrollToTop(topRef);
  const viewer = useViewer();
  const router = useRouter();
  const now = new Date();
  // honor the deep-link an Accounts row sends (?acct=…), like the web
  // ...and the one a category label sends (?cat=&y=&m= or a date range),
  // like the web's /transactions deep links
  const sp = useLocalSearchParams<{ acct?: string; cat?: string;
                                   bucket?: string; y?: string;
                                   m?: string; date_from?: string;
                                   date_to?: string }>();
  const str = (v: string | string[] | undefined) => v ? String(v) : "";
  const [ym, setYm] = useState({ y: Number(sp.y) || now.getFullYear(),
                                 m: Number(sp.m) || now.getMonth() + 1 });
  const [q, setQ] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [acct, setAcct] = useState(str(sp.acct));
  const [cat, setCat] = useState(str(sp.cat));
  // a plan bucket (Food / Everything else / a carve-out) for one month —
  // the rows the month verdict counted, which no category filter names
  const [bucket, setBucket] = useState(str(sp.bucket));
  // whether the view is one MONTH's slice. A bucket always is; a category
  // deep-linked with y&m is that month's category. A category picked in
  // the sheet is not — it searches all history, as it always has — so
  // this can't be derived from `cat` alone. (web: monthScope)
  const [monthScope, setMonthScope] = useState(
    !!(sp.bucket || (sp.cat && sp.y && sp.m)));
  const [reimbF, setReimbF] = useState(false);
  const [dateFrom, setDateFrom] = useState(str(sp.date_from));
  const [dateTo, setDateTo] = useState(str(sp.date_to));
  // A new set of route params is a NEW QUESTION, so it replaces the
  // filter state whole. Applying only the params that are present let
  // the previous label's filter survive the next tap — Food then
  // "Everything else" asked for Food again, and a month link after a
  // year link kept the year's date range.
  const linkKey = [sp.acct, sp.cat, sp.bucket, sp.y, sp.m, sp.date_from,
                   sp.date_to].map(str).join("|");
  useEffect(() => {
    setAcct(str(sp.acct));
    setCat(str(sp.cat));
    setBucket(str(sp.bucket));
    setDateFrom(str(sp.date_from));
    setDateTo(str(sp.date_to));
    setYm({ y: Number(sp.y) || now.getFullYear(),
            m: Number(sp.m) || now.getMonth() + 1 });
    setMonthScope(!!(sp.bucket || (sp.cat && sp.y && sp.m)));
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linkKey]);
  // which date field's calendar is open
  const [pick, setPick] = useState<"from" | "to" | null>(null);
  const [owner, setOwner] = useState("");
  // whose money the list shows — the web's "household + business /
  // household only / business only". Business rows carry a named pill
  // and never count toward the figures; this only decides listing.
  const [scope, setScope] = useState("");
  // how it was paid, and checks only — searches server-side (the web's
  // "any channel" select + "checks only" button)
  const [channel, setChannel] = useState("");
  const [checksF, setChecksF] = useState(false);
  const [page, setPage] = useState(1);
  // debounced: a query per keystroke janks the list and spams the server
  const [search, setSearch] = useState("");
  useEffect(() => {
    // a NEW question starts at page 1 — carrying page 3 into a fresh
    // search asked the server for a page that doesn't exist
    // same rule as the web: a beat after the last keystroke, two characters
    // minimum for the automatic path (one letter matches most of the ledger)
    const want = q.trim();
    if (want.length > 0 && want.length < 2) return;
    const id = setTimeout(() => {
      setSearch(want);
      // typing is an all-history search — it leaves any bucket / month
      // scope, and the bucket path ignores the term anyway (setters
      // inline rather than the helper: a stable effect body)
      if (want) { setBucket(""); setMonthScope(false); }
      setPage(1);
    }, 250);
    return () => clearTimeout(id);
  }, [q]);

  // the web's param split: any filter/search → the search path (with
  // paging); otherwise the month view
  const searching = !!(search || acct || cat || bucket || dateFrom || dateTo
                       || channel || checksF);
  const params: TxnQuery = {
    ...(bucket
      ? { bucket, y: String(ym.y), m: String(ym.m), page: String(page) }
      : searching
      ? { q: search, acct, cat, page: String(page),
          date_from: dateFrom, date_to: dateTo,
          // a month's category: no date range, so the server reads y&m
          // and scopes to that month — what a link from a month's lens
          // means, and what the month arrows then re-query
          ...(monthScope ? { y: String(ym.y), m: String(ym.m) } : {}),
          ...(channel ? { channel } : {}),
          ...(checksF ? { checks: "1" } : {}) }
      : { y: String(ym.y), m: String(ym.m) }),
    ...(reimbF && !bucket ? { reimb: "1" } : {}),
    // the search path is owner-agnostic server-side (the web sends owner
    // only in month view) — sending it anyway lit a filter chip that did
    // nothing
    // a bucket listing is the engine's own row set — the lenses would
    // only empty it under a non-zero chip, so none rides along (web same)
    ...(bucket ? {} : {
      ...(owner && !searching ? { owner } : {}),
      ...(scope ? { scope } : {}),
    }),
  };
  const query = useQuery({
    queryKey: ["transactions", params],
    queryFn: () => client!.transactions(params),
    enabled: !!client,
    // keep the last page + its hero stats on screen while the next
    // question loads — without this the total blinks out on every
    // keystroke (the web does the same). But NEVER carry a placeholder
    // across the month ⇄ search mode switch: the two responses have
    // different shapes, and the hero read the wrong one's fields for a
    // frame ("0 results" flashing over a month view). Month params
    // carry y; search params don't.
    placeholderData: (prev, prevQuery) => {
      const prevParams = prevQuery?.queryKey[1] as TxnQuery | undefined;
      if (!prevParams) return undefined;
      // a bucket or month-scoped category carries y like the month view
      // but answers in the search shape; only the search path is paged,
      // so that is the honest mode marker
      const prevSearching = "page" in prevParams;
      return prevSearching === searching ? prev : undefined;
    },
  });
  // pages ACCUMULATE in search mode ("Load more" must never replace
  // what's shown); the pile resets when the question changes
  const PAGE_SIZE = 100;
  const [pile, setPile] = useState<Txn[]>([]);
  const filterKey =
    `${search}|${acct}|${cat}|${bucket}|${monthScope ? `${ym.y}-${ym.m}` : ""
     }|${reimbF}|${dateFrom}|${dateTo}|${owner}|${scope}|${channel}|${checksF}`;
  const pileKey = useRef(filterKey);
  useEffect(() => {
    if (!query.data || !searching) return;
    setPile((prev) => {
      if (pileKey.current !== filterKey || page === 1) {
        pileKey.current = filterKey;
        return query.data!.rows;
      }
      const seen = new Set(prev.map((r) => r.id));
      return [...prev, ...query.data!.rows.filter((r) => !seen.has(r.id))];
    });
  }, [query.data, searching, filterKey, page]);
  const accts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(),
    // also needed while a search names an account (the context line)
    enabled: !!client && (showFilters || !!acct) });
  const cats = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(), enabled: !!client && showFilters });
  const ownersQ = useQuery({ queryKey: ["owners"],
    queryFn: () => client!.owners(), enabled: !!client && showFilters });
  const nFilters = (acct ? 1 : 0) + (cat ? 1 : 0) + (bucket ? 1 : 0)
    + (reimbF ? 1 : 0)
    + (dateFrom || dateTo ? 1 : 0) + (owner && !searching ? 1 : 0)
    + (scope ? 1 : 0) + (channel ? 1 : 0) + (checksF ? 1 : 0);
  // long-press starts selection mode; the bulk bar mirrors the web's
  // TxnTable actions (recategorize, biz on/off, reimb flag/unflag)
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [bulkPicking, setBulkPicking] = useState(false);
  const bulkCats = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(),
    enabled: !!client && bulkPicking });
  const [bulkFilter, setBulkFilter] = useState("");
  const qc2 = useQueryClient();
  const bulk = useMutation({
    mutationFn: ({ action, category }:
        { action: string; category?: string }) =>
      client!.txnBulk([...sel], action, category),
    // the picker closes on the tap; the result arrives behind it. The
    // selection is remembered here because the user can change it while
    // the write is in flight.
    onMutate: () => { setBulkPicking(false); return { ids: new Set(sel) }; },
    onSuccess: (r, v, ctx) => {
      // flip the selected rows in every cached page (and the search pile,
      // which only ever appends from the query) before anything refetches
      // — the rows must not sit there until the active variant returns
      const ids = ctx.ids;
      const patch: Partial<Txn> =
        v.action === "category" ? { category: catLabel(v.category ?? "") }
        : v.action === "reimb_flag" ? { reimb_flag: true }
        : v.action === "reimb_unflag" ? { reimb_flag: false }
        : v.action === "biz_on" ? { biz_flag: true }
        : v.action === "biz_off" ? { biz_flag: false } : {};
      const flip = (rs: Txn[]) =>
        rs.map((x) => ids.has(x.id) ? { ...x, ...patch } : x);
      patchQueries<TxnPage>(qc2, ["transactions"],
        (o) => ({ ...o, rows: flip(o.rows) }));
      setPile(flip);
      setSel(new Set());
      // every bulk action moves money (totals, verdict), and the flag
      // actions feed the Reimburse / business ledgers' own lists
      qc2.invalidateQueries({ queryKey: ["transactions"] });
      qc2.invalidateQueries({ queryKey: ["today"] });
      if (v.action.startsWith("reimb_"))
        qc2.invalidateQueries({ queryKey: ["reimburse"] });
      if (v.action.startsWith("biz_"))
        qc2.invalidateQueries({ queryKey: ["business"] });
      // the web's toast, always, with its exact meaning: flow rows ARE
      // applied — the server counts them so we can say they now count as
      // spending (the old "refused" wording here contradicted the server)
      const bits = [`${r.applied} updated`];
      if (r.flow > 0)
        bits.push(`${r.flow} of them were transfers or income and now `
          + `count as spending`);
      if ((r.missing ?? 0) > 0)
        bits.push(`${r.missing} no longer exist`);
      Alert.alert("Bulk result", bits.join("\n"));
    },
    onError: (e) => Alert.alert("Bulk update failed", errText(e)),
  });
  const toggleSel = (id: string) => setSel((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const shift = (dir: -1 | 1) => {
    // a new month is a new question — page 3 of the old one does not
    // carry over
    setPage(1);
    setYm(({ y, m }) => {
      const next = m + dir;
      return next < 1 ? { y: y - 1, m: 12 }
        : next > 12 ? { y: y + 1, m: 1 } : { y, m: next };
    });
  };

  const pageRows = query.data?.rows ?? [];
  const rows = searching ? (pile.length ? pile : pageRows) : pageRows;
  // the ledger is the app's one long date-ordered list, so it is the one
  // surface that gets day dividers — matching the web's Transactions page.
  // Today's recent pane is short enough that a header per day is chrome.
  const listData = useMemo(() => withDayHeads(rows), [rows]);
  const atNow = ym.y === now.getFullYear() && ym.m === now.getMonth() + 1;
  // a destination count, not a filter — same as the web hero's pill-link
  const reimbQ = useQuery({ queryKey: ["reimburse"],
    queryFn: () => client!.reimbPending(), enabled: !!client });
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  const awaitingN = reimbQ.data?.pending.length ?? 0;
  const d0 = query.data;
  const acctName = (id: string) =>
    accts.data?.accounts.find((a) => a.id === id)?.name ?? "1 account";
  // What a deep link narrowed the list to, in one chip: a bucket in the
  // plan's own words (the server's label, so a custom bucket reads as
  // the household wrote it), a month-scoped category by its label — each
  // with its month, because a link's scope is the part that otherwise
  // goes unsaid. No sheet control shows either one.
  const monthShort = `${MONTHS[ym.m - 1].slice(0, 3)} ${ym.y}`;
  // the previous result stays on screen while the next loads, so the
  // server's label counts only when it answers THIS bucket; until then
  // the link's own word stands in (web reads the same way)
  const bucketLabel =
    (d0?.bucket === bucket && d0?.bucket_label)
    || (bucket === "food" ? "Food"
        : bucket === "other" ? "Everything else" : bucket);
  const scopeChip = bucket
    ? `${bucketLabel} · ${monthShort}`
    : cat && monthScope ? `${catLabel(cat)} · ${monthShort}` : null;
  // ✕ on the chip drops the grouping and keeps the month it named
  const clearScope = () => {
    setBucket(""); setCat(""); setMonthScope(false); setPage(1);
  };
  // every filter in force, as a removable chip under the search box — the
  // sheet can be closed and the list still says what is shaping it
  const active: { key: string; label: string; clear: () => void }[] = [
    ...(acct ? [{ key: "acct", label: acctName(acct),
                  clear: () => setAcct("") }] : []),
    ...(scopeChip ? [{ key: "scope-link", label: scopeChip,
                       clear: clearScope }] : []),
    ...(cat && !monthScope
      ? [{ key: "cat", label: catLabel(cat), clear: () => setCat("") }]
      : []),
    ...(dateFrom || dateTo ? [{ key: "date",
        label: `${dateFrom ? mmddyy(dateFrom) : "start"} – ${
                  dateTo ? mmddyy(dateTo) : "now"}`,
        clear: () => { setDateFrom(""); setDateTo(""); } }] : []),
    ...(owner && !searching ? [{ key: "owner", label: owner,
                                  clear: () => setOwner("") }] : []),
    ...(scope ? [{ key: "scope",
        label: scope === "business" ? "business only" : "household only",
        clear: () => setScope("") }] : []),
    ...(channel ? [{ key: "channel", label: channel,
                     clear: () => setChannel("") }] : []),
    ...(checksF ? [{ key: "checks", label: "checks only",
                     clear: () => setChecksF(false) }] : []),
    ...(reimbF ? [{ key: "reimb", label: "reimbursements only",
                    clear: () => setReimbF(false) }] : []),
  ];
  // a month view narrowed by a lens (whose money, business scope,
  // reimbursements) keeps the month's cash flow as its headline; the
  // rows on screen are then a subset, so say what THEY add up to
  const narrowed = !searching && active.length > 0;
  const listedSum = useMemo(
    () => rows.reduce((a, r) => a + (r.amount ?? 0), 0), [rows]);
  // The sheet's filters search ALL history, so reaching for one leaves a
  // bucket or a month's category behind rather than silently narrowing
  // the new filter to the month that happened to be on screen — and a
  // bucket listing ignores the sheet entirely, so a filter set inside
  // one would otherwise look broken. (web: leaveScope)
  const leaveScope = () => { setBucket(""); setMonthScope(false); };
  // every filter change starts back at page 1 and leaves that scope
  const refilter = () => { setPage(1); leaveScope(); };
  const clearAll = () => {
    setAcct(""); setCat(""); setBucket(""); setMonthScope(false);
    setReimbF(false); setQ(""); setSearch("");
    setDateFrom(""); setDateTo(""); setOwner(""); setScope("");
    setChannel(""); setChecksF(false); setPage(1);
  };
  const summary = !d0 ? (query.isError ? "Couldn't reach the server."
                                       : "Loading…")
    : searching
      ? `${d0.total ?? rows.length} result${
          (d0.total ?? rows.length) === 1 ? "" : "s"} · ${
          money(-(d0.amount_sum ?? 0))} total`
      : `${MONTHS[ym.m - 1]} ${ym.y} · ${rows.length} transaction${
          rows.length === 1 ? "" : "s"}${
          narrowed ? ` · ${money(-listedSum)} listed` : ""}`;

  return (
    <View style={s.wrap}>
      <View style={{ flexDirection: "row", gap: 8 }}>
        <TextInput style={[s.search, { flex: 1 }]}
                   placeholder="search payee, category…"
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   autoCorrect={false} value={q} onChangeText={setQ}
                   returnKeyType="search"
                   // Enter submits ANYTHING, including the one-character
                   // query the as-you-type guard holds back
                   onSubmitEditing={() => {
                     setSearch(q.trim()); setPage(1);
                     if (q.trim()) leaveScope();
                   }} />
        <Pressable style={[s.filterBtn, nFilters > 0 && s.filterOn]}
                   onPress={() => setShowFilters(true)}>
          <Text style={{ color: nFilters > 0 ? C.text : C.mut,
                         fontSize: 13, fontWeight: "600" }}>
            Filter{nFilters > 0 ? ` · ${nFilters}` : ""}
          </Text>
        </Pressable>
      </View>
      {active.length > 0 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
                    keyboardShouldPersistTaps="handled">
          <View style={{ flexDirection: "row", gap: 6 }}>
            {active.map((f) => (
              <Pressable key={f.key} style={[s.chip, s.chipOn]}
                         onPress={() => { f.clear(); setPage(1); }}
                         accessibilityLabel={`remove filter ${f.label}`}>
                <Text style={s.chipText(true)}>{f.label}  ✕</Text>
              </Pressable>
            ))}
            {active.length > 1 && (
              <Pressable style={s.chip} onPress={clearAll}>
                <Text style={s.chipText(false)}>clear all</Text>
              </Pressable>
            )}
          </View>
        </ScrollView>
      )}
      {/* the month arrows belong to any view that IS a month — the
          browse, a bucket, a month's category — so a plan label's rows
          can be walked back through the year in place */}
      {(!searching || monthScope) && (
        <View style={s.nav}>
          <Pressable style={s.navBtn} onPress={() => shift(-1)}>
            <Text style={s.navText}>‹</Text>
          </Pressable>
          <View style={{ flexDirection: "row", alignItems: "center",
                         gap: 10 }}>
            <Text style={s.navLabel}>{MONTHS[ym.m - 1]} {ym.y}</Text>
            {!atNow && (
              <Text style={{ color: C.accent, fontSize: 13 }}
                    onPress={() => { setPage(1);
                                     setYm({ y: now.getFullYear(),
                                             m: now.getMonth() + 1 }); }}>
                today
              </Text>
            )}
          </View>
          <Pressable style={[s.navBtn, atNow && { opacity: 0.3 }]}
                     disabled={atNow} onPress={() => shift(1)}>
            <Text style={s.navText}>›</Text>
          </Pressable>
        </View>
      )}
      <StaleBanner query={query} />
      {/* HERO — the month's cash flow (income / spend / net, the Cash
          Flow page's definitions; transfers and card payments are money
          moved, not made or spent) as the headline, not a footer; a
          search answers with its count + true total (+ per-item Amazon
          spend when the term matched order items) */}
      {d0 && (
        <View style={{ gap: 2 }}>
          <Text style={{ color: C.text, fontSize: 18, fontWeight: "700",
                         fontVariant: ["tabular-nums"] }}>
            {!searching ? (
              <>
                <Text style={{ color: C.good }}>{money(d0.in_sum ?? 0)}</Text>
                <Text style={s.heroMut}> in · </Text>
                {money(d0.out_sum ?? 0)}
                <Text style={s.heroMut}> out · </Text>
                <Text style={{ color: (d0.net_sum ?? 0) < 0 ? C.bad : C.good }}>
                  {(d0.net_sum ?? 0) < 0 ? "" : "+"}{money(d0.net_sum ?? 0)}
                </Text>
                <Text style={s.heroMut}> net · {rows.length} txn
                  {rows.length === 1 ? "" : "s"}
                  {(d0.biz_count ?? 0) > 0
                    ? ` · ${d0.biz_count} business (not in the totals)` : ""}</Text>
              </>
            ) : (
              <>
                {d0.total ?? rows.length}
                <Text style={s.heroMut}>
                  {" result"}{(d0.total ?? rows.length) === 1 ? "" : "s"} ·{" "}
                </Text>
                <Text style={(d0.amount_sum ?? 0) < 0
                               ? { color: C.good } : undefined}>
                  {money(-(d0.amount_sum ?? 0))}
                </Text>
                <Text style={s.heroMut}> total</Text>
                {(d0.biz_count ?? 0) > 0 && scope !== "business" ? (
                  <Text style={s.heroMut}>
                    {" "}· {d0.biz_count} business (not in the total)</Text>
                ) : null}
                {/* the total says how much has gone to this merchant; the
                    average says what one visit costs — the question a
                    merchant search is usually really asking. Mirrors the
                    web hero exactly. */}
                {d0.spend_avg != null ? (
                  <Text style={s.heroMut}>
                    {" · "}
                    <Text style={{ color: C.text }}>{money(d0.spend_avg)}</Text>
                    {" avg of "}{d0.spend_count}
                  </Text>
                ) : null}
                {d0.amazon_items ? (
                  <Text style={s.heroMut}>
                    {" · "}{money(d0.amazon_items.sum)} on{" "}
                    {d0.amazon_items.count} Amazon item
                    {d0.amazon_items.count === 1 ? "" : "s"}
                    {d0.amazon_items.unpriced > 0
                      ? ` (${d0.amazon_items.unpriced} without a price)` : ""}
                  </Text>
                ) : null}
              </>
            )}
          </Text>
          {narrowed && (
            <Text style={{ color: C.mut, fontSize: 12 }}>
              {rows.length} listed ·{" "}
              <Text style={listedSum < 0 ? { color: C.good } : { color: C.text }}>
                {money(-listedSum)}
              </Text>
              {" "}total for this filter
            </Text>
          )}
          {searching && (
            // the web's context line — what question produced these rows
            <Text style={{ color: C.mut, fontSize: 12 }}>
              in <Text style={{ color: C.text }}>
                {acct
                  ? (accts.data?.accounts.find((a) => a.id === acct)?.name
                     ?? "1 account")
                  : "all accounts"}
              </Text>
              {/* a bucket or a month-scoped category is already named by
                  the chip above, with its month — saying it twice reads
                  as two different filters */}
              {cat && !monthScope ? ` · ${catLabel(cat)}` : ""}
              {dateFrom || dateTo
                ? ` · ${dateFrom ? mmddyy(dateFrom) : "start"} – ${
                    dateTo ? mmddyy(dateTo) : "now"}`
                : ""}
              {" · newest first"}
              {/* the term matched an account by name, so that account's
                  rows are in the result — a renamed account found through
                  the BANK'S name states both (mirrors the web line) */}
              {(d0.account_hits ?? []).map((h) => (
                <Text key={h.id}>
                  {" · includes "}
                  <Text style={{ color: C.text }}>{h.label}</Text>
                  {h.via_bank ? ` (the bank calls it “${h.bank_name}”)` : ""}
                </Text>
              ))}
            </Text>
          )}
          {awaitingN > 0 && (
            <View style={{ flexDirection: "row", gap: 14 }}>
              <Text style={{ color: C.warn, fontSize: 12 }}
                    onPress={() => router.push("/reimburse" as never)}>
                {awaitingN} awaiting reimbursement ›
              </Text>
            </View>
          )}
        </View>
      )}
      <FlashList
        ref={topRef}
        data={listData}
        keyExtractor={(it) => "kind" in it ? it.key : it.id}
        // headers and rows recycle in separate pools — one pool would
        // reuse a header cell as a row and pay a full re-layout for it
        getItemType={(it) => "kind" in it ? "day" : "txn"}
        extraData={sel}
        renderItem={useCallback(({ item }: { item: (typeof listData)[number] }) =>
          "kind" in item ? (
            <View style={s.dayHead}>
              <Text style={s.dayHeadL}>{item.label}</Text>
              {/* a day of nothing but transfers or income has a real count
                  and no spend — "$0.00 spent" there would read as a claim
                  about the day rather than about what the total excludes */}
              <Text style={s.dayHeadR}>
                {item.count} transaction{item.count === 1 ? "" : "s"}
                {item.spent > 0 ? ` · ${money(-item.spent)} spent` : ""}
              </Text>
            </View>
          ) : (
          <TxnRow t={item}
            selected={sel.size > 0 ? sel.has(item.id) : undefined}
            onLongPress={viewer ? undefined : (t) => toggleSel(t.id)}
            onPress={(t) => sel.size > 0 ? toggleSel(t.id)
              : (setHandedOffTxn(t),
                 router.push({ pathname: "/txn",
                               params: { id: t.id } }))} />),
          [router, sel, viewer])}
        contentContainerStyle={s.list}
        refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                        onRefresh={() => query.refetch()} />}
        ListEmptyComponent={
          <Text style={s.mut}>
            {query.isPending ? "Loading…"
              : query.isError ? "Couldn't reach the server."
              : "No transactions."}
          </Text>}
        ListFooterComponent={rows.length > 0 ? (
          <View>
            <Text style={s.mut}>
              {rows.length}{query.data?.total != null
                ? ` of ${query.data.total}` : ""} row
              {rows.length === 1 ? "" : "s"}
            </Text>
            {searching && !query.isFetching
              && (query.data?.total ?? 0) > page * PAGE_SIZE && (
              <Text style={[s.mut, { color: C.accent }]}
                    onPress={() => setPage((p) => p + 1)}>
                Load more
              </Text>
            )}
          </View>) : null}
      />

      {sel.size > 0 && (
        <View style={s.bulkBar}>
          <View style={{ flexDirection: "row", alignItems: "center",
                         gap: 14, flexWrap: "wrap" }}>
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "600" }}>
              {sel.size} selected
            </Text>
            <Text style={s.bulkAct}
                  onPress={() =>
                    setSel(new Set(rows.map((r) => r.id)))}>
              All
            </Text>
            <Text style={[s.bulkAct, bulk.isPending && { opacity: 0.4 }]}
                  onPress={() => !bulk.isPending && setBulkPicking(true)}>
              Category
            </Text>
            {/* the web TxnTable's action names, verbatim */}
            <Text style={[s.bulkAct, bulk.isPending && { opacity: 0.4 }]}
                  onPress={() => !bulk.isPending
                    && bulk.mutate({ action: "reimb_flag" })}>
              ⚑ expect reimbursement
            </Text>
            <Text style={[s.bulkAct, bulk.isPending && { opacity: 0.4 }]}
                  onPress={() => !bulk.isPending
                    && bulk.mutate({ action: "reimb_unflag" })}>
              unflag
            </Text>
            <Text style={[s.bulkAct, bulk.isPending && { opacity: 0.4 }]}
                  onPress={() => !bulk.isPending
                    && bulk.mutate({ action: "biz_on" })}>
              mark business
            </Text>
            <Text style={[s.bulkAct, bulk.isPending && { opacity: 0.4 }]}
                  onPress={() => !bulk.isPending
                    && bulk.mutate({ action: "biz_off" })}>
              unmark business
            </Text>
            <Text style={[s.bulkAct, { color: C.mut }]}
                  onPress={() => setSel(new Set())}>
              clear
            </Text>
          </View>
        </View>
      )}

      <Sheet visible={bulkPicking} onClose={() => setBulkPicking(false)}
             title={`Recategorize ${sel.size} transaction${
                      sel.size === 1 ? "" : "s"}`}
             footer={
               <View style={b.footer}>
                 <Pressable style={[b.btn, b.btnQuiet]}
                            onPress={() => setBulkPicking(false)}>
                   <Text style={b.btnQuietText}>Close</Text>
                 </Pressable>
               </View>}>
        <TextInput style={s.search} placeholder="filter…"
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   value={bulkFilter} onChangeText={setBulkFilter} />
        {/* your own categories first, then the standard set — the web
            picker's optgroups; plaid_flow stays unreachable in bulk on
            purpose */}
        {(() => {
          const std = new Set([
            ...(bulkCats.data?.plaid_spend ?? []),
            ...(bulkCats.data?.plaid_flow ?? [])]);
          const custom = (bulkCats.data?.categories ?? [])
            .filter((c) => !std.has(c));
          // matched on the DISPLAY label each row renders, so typing
          // what you can see finds it
          const match = (c: string) =>
            filterCategories([c], bulkFilter).length > 0;
          const row = (c: string) => (
            <Pressable key={c} style={s.catRow}
                       disabled={bulk.isPending}
                       onPress={() => bulk.mutate({
                         action: "category", category: c })}>
              <Text style={{ color: C.text, fontSize: 15 }}>
                {catLabel(c)}
              </Text>
            </Pressable>
          );
          const cu = custom.filter(match);
          const st0 = [...(bulkCats.data?.plaid_spend ?? []),
                       ...(bulkCats.data?.plaid_flow ?? [])]
            .filter(match);
          return (
            <>
              {cu.length > 0 && <Text style={s.lbl}>Your categories</Text>}
              {cu.map(row)}
              {st0.length > 0 && <Text style={s.lbl}>Standard</Text>}
              {st0.map(row)}
            </>
          );
        })()}
      </Sheet>

      <Sheet visible={showFilters} onClose={() => setShowFilters(false)}
             title="Filters"
             footer={
               <View style={b.footer}>
                 <Pressable style={b.btn}
                            onPress={() => setShowFilters(false)}>
                   <Text style={b.btnText}>Done</Text>
                 </Pressable>
                 {(nFilters > 0 || q) && (
                   <Pressable style={[b.btn, b.btnQuiet]} onPress={clearAll}>
                     <Text style={b.btnQuietText}>Clear</Text>
                   </Pressable>
                 )}
               </View>}>
        {/* what the filters below currently produce — live, so a chip
            can be tried and judged without closing the sheet */}
        <Text style={{ color: C.mut, fontSize: 12 }}>
          {summary}{query.isFetching ? " · updating…" : ""}
        </Text>
        <Text style={s.lbl}>Date range</Text>
        {/* the site-wide 3m … All vocabulary, the same six windows every
            over-time toggle offers; a picked from/to that matches none of
            them lights nothing, and All is the no-date-filter state */}
        <View style={{ flexDirection: "row", gap: 6, flexWrap: "wrap" }}>
          {TREND_RANGES.map((r) => {
            const v = ledgerRange(r, now);
            const on = dateFrom === v.from && dateTo === v.to;
            return (
              <Pressable key={r} style={[s.chip, on && s.chipOn]}
                         onPress={() => {
                           // an explicit range answers a different
                           // question from "this bucket, this month"
                           setDateFrom(v.from); setDateTo(v.to); refilter();
                         }}>
                <Text style={s.chipText(on)}>{r === "all" ? "All" : r}</Text>
              </Pressable>
            );
          })}
        </View>
        <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
          <DateField value={dateFrom} placeholder="from"
                     onPress={() => setPick("from")}
                     onClear={() => { setDateFrom(""); refilter(); }} />
          <DateField value={dateTo} placeholder="to"
                     onPress={() => setPick("to")}
                     onClear={() => { setDateTo(""); refilter(); }} />
        </View>
        {/* remounted per opening so the calendar lands on the picked
            month, not the month it showed last time */}
        {pick && (
          <DatePicker key={pick + (pick === "from" ? dateFrom : dateTo)}
                      visible title={pick === "from" ? "From" : "To"}
                      value={pick === "from" ? dateFrom : dateTo}
                      min={pick === "to" ? dateFrom || undefined : undefined}
                      max={pick === "from" ? dateTo || undefined : undefined}
                      onPick={(d) => {
                        if (pick === "from") setDateFrom(d); else setDateTo(d);
                        refilter();
                      }}
                      onClose={() => setPick(null)} />
        )}
        <Text style={s.lbl}>Account</Text>
        {/* live first, then archived/closed under a header — the web's
            optgroup split (balance gone = archived) */}
        <Select title="Account" placeholder="all accounts" value={acct}
                filter={(accts.data?.accounts ?? []).length > 8}
                onChange={(v) => { setAcct(v); refilter(); }}
                options={[
                  ...(accts.data?.accounts ?? [])
                    .filter((a) => a.txns > 0 && a.balance_current !== null)
                    .map((a) => ({ value: a.id, label: a.name })),
                  ...(accts.data?.accounts ?? [])
                    .filter((a) => a.txns > 0 && a.balance_current === null)
                    .map((a) => ({ value: a.id, label: a.name,
                                   group: "Archived / closed", dim: true })),
                ]} />
        <Text style={s.lbl}>Category</Text>
        <Select title="Category" placeholder="all categories" value={cat}
                filter
                // a hand-picked category asks the whole ledger, not the
                // month a link happened to arrive with, and it replaces
                // a bucket question rather than narrowing it
                onChange={(v) => { setCat(v); refilter(); }}
                options={(cats.data?.categories ?? [])
                  .map((c) => ({ value: c, label: catLabel(c) }))} />
        {(ownersQ.data?.owners ?? []).length > 0 && (
          <>
            <Text style={s.lbl}>Whose money</Text>
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
              <Pressable style={[s.chip, !owner && s.chipOn]}
                         onPress={() => { setOwner(""); refilter(); }}>
                <Text style={s.chipText(!owner)}>all</Text>
              </Pressable>
              {ownersQ.data!.owners.map((o) => (
                <Pressable key={o}
                           style={[s.chip, owner === o && s.chipOn]}
                           onPress={() => { setOwner(o); refilter(); }}>
                  <Text style={s.chipText(owner === o)}>{o}</Text>
                </Pressable>
              ))}
            </View>
          </>
        )}
        {!!me.data?.has_business && (
          <>
            <Text style={s.lbl}>Business accounts</Text>
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
              {([["", "household + business"], ["personal", "household only"],
                 ["business", "business only"]] as const).map(([v, l]) => (
                <Pressable key={v} style={[s.chip, scope === v && s.chipOn]}
                           onPress={() => { setScope(v); refilter(); }}>
                  <Text style={s.chipText(scope === v)}>{l}</Text>
                </Pressable>
              ))}
            </View>
          </>
        )}
        <Text style={s.lbl}>How it was paid</Text>
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
          {([["", "any"], ["in store", "in store"], ["online", "online"],
             ["other", "other"]] as const).map(([v, l]) => (
            <Pressable key={v} style={[s.chip, channel === v && s.chipOn]}
                       onPress={() => { setChannel(v); refilter(); }}>
              <Text style={s.chipText(channel === v)}>{l}</Text>
            </Pressable>
          ))}
          <Pressable style={[s.chip, checksF && s.chipOn]}
                     onPress={() => { setChecksF((x) => !x); refilter(); }}>
            <Text style={s.chipText(checksF)}>checks only</Text>
          </Pressable>
        </View>
        <Pressable style={[s.chip, reimbF && s.chipOn,
                           { alignSelf: "flex-start", marginTop: 8 }]}
                   onPress={() => setReimbF((x) => !x)}>
          <Text style={s.chipText(reimbF)}>reimbursements only</Text>
        </Pressable>
      </Sheet>
    </View>
  );
}

const s = {
  ...StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg, padding: 12, gap: 8 },
  search: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 10, color: C.text, fontSize: 15,
            paddingHorizontal: 12, paddingVertical: 9 },
  nav: { alignItems: "center", flexDirection: "row",
         justifyContent: "space-between" },
  navBtn: { paddingHorizontal: 18, paddingVertical: 4 },
  navText: { color: C.accent, fontSize: 24 },
  navLabel: { color: C.text, fontSize: 16, fontWeight: "600" },
  list: { paddingBottom: 24 },
  // reads as the web's band: muted, on the hover surface, the
  // label and the day's spend pushed to opposite ends
  dayHead: { flexDirection: "row", alignItems: "baseline",
             justifyContent: "space-between", gap: 10,
             backgroundColor: C.hover, borderRadius: 6,
             marginTop: 10, marginBottom: 2,
             paddingHorizontal: 8, paddingVertical: 4 },
  dayHeadL: { color: C.mut, fontSize: 11, fontWeight: "700",
              letterSpacing: 0.6, textTransform: "uppercase" },
  dayHeadR: { color: C.mut, fontSize: 12,
              fontVariant: ["tabular-nums"] },
  mut: { color: C.mut, fontSize: 13, paddingVertical: 12,
         textAlign: "center" },
  filterBtn: { backgroundColor: C.card, borderColor: C.border,
               borderWidth: 1, borderRadius: 10, justifyContent: "center",
               paddingHorizontal: 12 },
  filterOn: { borderColor: C.accent },
  lbl: { color: C.mut, fontSize: 12, marginTop: 8 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 12, paddingVertical: 6 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  bulkBar: { flexDirection: "row", alignItems: "center", gap: 14,
             backgroundColor: C.card, borderColor: C.accent, borderWidth: 1,
             borderRadius: 12, paddingHorizontal: 14, paddingVertical: 10 },
  bulkAct: { color: C.accent, fontSize: 13, fontWeight: "600" },
  heroMut: { color: C.mut, fontSize: 13, fontWeight: "400" },
  catRow: { paddingVertical: 9, borderTopColor: C.border,
            borderTopWidth: 0.5 },
  }),
  chipText: (on: boolean) =>
    ({ color: on ? C.text : C.mut, fontSize: 13 }),
};
