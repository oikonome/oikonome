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
import Sheet, { FOOTER_SLOP, sheetStyles as b } from "../../components/sheet";

import { EmptyState, FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../../components/loaded";
import TxnRow from "../../components/txn-row";
import { SnapReceiptButton, WaitingBadge }
  from "../../components/waiting-receipts";
import { errText, setHandedOffTxn, Txn, TxnPage, TxnQuery }
  from "../../lib/api";
import { patchQueries } from "../../lib/cache";
import { catKey, DayHead, RANGE_LABEL, TREND_RANGES, withDayHeads, catLabel,
         filterCategories, householdDay, routeYm,
         BULK_CHUNK, BULK_MAX, flattenPile, goneTxnIds, mergePilePages,
         EMPTY_LINES, failLine, figureGreyed, listedSpendWord, runChunked,
         spendDrillState }
  from "../../lib/pure";
import { useRefetchWhenShown } from "../../lib/focus";

const TXNS_KEY = ["transactions"];
import { useSession } from "../../lib/session";
import { useDemo, useMe, useOwner, useViewer } from "../../lib/viewer";
import { WAITING_KEY, useWaitingReceipts } from "../../lib/waiting";
import { C, mmddyy, money } from "../../lib/theme";
import SegmentedControl from "../../components/segmented-control";
import { fieldA11y } from "../../components/field";

const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December"];

export default function Transactions() {
  const { client } = useSession();
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<FlashListRef<Txn | DayHead>>(null);
  useScrollToTop(topRef);
  const viewer = useViewer();
  // the ledger as a file is the owner's to take (web: isOwner)
  const mayExport = useOwner();
  // the demo instance refuses bulk writes; row-by-row edits still go
  // through, so the bar stays (and can clear) but cannot act — the web
  // TxnTable's bulk bar, mirrored
  const demo = useDemo();
  // receipts snapped before their charge: the camera sits beside the
  // ledger (where people look for the charge), and what is still waiting
  // links to the Receipts screen — refetched when this tab is shown, since
  // a sync can match one while the tab is frozen
  const waiting = useWaitingReceipts();
  useRefetchWhenShown(waiting, WAITING_KEY);
  const router = useRouter();
  // "this month" is the household's (/api/me timezone), the month the
  // server's ledger and verdicts count in — not the device's
  const me = useMe();
  const now = householdDay(new Date(), me.data?.timezone);
  // honor the deep-link an Accounts row sends (?acct=…), like the web
  // ...and the one a category label sends (?cat=&y=&m= or a date range),
  // like the web's /transactions deep links
  const sp = useLocalSearchParams<{ acct?: string; cat?: string;
                                   bucket?: string; y?: string;
                                   m?: string; date_from?: string;
                                   date_to?: string; as_of?: string;
                                   counted?: string; q?: string;
                                   spend?: string; merchant?: string;
                                   side?: string }>();
  const str = (v: string | string[] | undefined) => v ? String(v) : "";
  // read through routeYm: a deep link can carry any month at all
  const [ym, setYm] = useState(() => routeYm(sp.y, sp.m, now));
  // a charge's alert links here searched for that charge (?q=)
  const [q, setQ] = useState(str(sp.q));
  const [showFilters, setShowFilters] = useState(false);
  const [acct, setAcct] = useState(str(sp.acct));
  const [cat, setCat] = useState(str(sp.cat));
  // a plan bucket (Food / Everything else / a carve-out) for one month —
  // the rows the month verdict counted, which no category filter names
  const [bucket, setBucket] = useState(str(sp.bucket));
  // the day a past Today screen was read on: its bucket's rows stop there
  const [asOf, setAsOf] = useState(str(sp.as_of));
  // a lens category label's link: the rows that label summed (personal
  // spend, reimbursements netted), not every row filed under the category
  const [counted, setCounted] = useState(str(sp.counted) === "1");
  // a Spending figure's link: the rows that figure summed over one range
  // (`spend`), for a category (`cat`), a merchant or the whole window, on
  // its current or previous side — the server's own row set (web same)
  const [spend, setSpend] = useState(str(sp.spend));
  const [merchant, setMerchant] = useState(str(sp.merchant));
  const [side, setSide] = useState(str(sp.side));
  // the server answered a figure's link without naming its rows: it is
  // older than the drill, so the list fell back to the month browse
  const [drillRefused, setDrillRefused] = useState(false);
  // whether the view is one MONTH's slice. A bucket always is; a category
  // deep-linked with y&m is that month's category. A category picked in
  // the sheet is not — it searches all history, as it always has — so
  // this can't be derived from `cat` alone. (web: monthScope)
  const [monthScope, setMonthScope] = useState(
    !!((sp.bucket && sp.m) || (sp.cat && sp.y && sp.m)));
  // a YEAR's bucket — the year lens's money map label: y with no m, the
  // bucket over the year's budgeted months as one listing (web: yearScope)
  const [yearScope, setYearScope] = useState(!!(sp.bucket && sp.y && !sp.m));
  const [reimbF, setReimbF] = useState(false);
  const [dateFrom, setDateFrom] = useState(str(sp.date_from));
  const [dateTo, setDateTo] = useState(str(sp.date_to));
  // A new set of route params is a NEW QUESTION, so it replaces the
  // filter state whole. Applying only the params that are present let
  // the previous label's filter survive the next tap — Food then
  // "Everything else" asked for Food again, and a month link after a
  // year link kept the year's date range.
  const linkKey = [sp.acct, sp.cat, sp.bucket, sp.y, sp.m, sp.date_from,
                   sp.date_to, sp.as_of, sp.counted, sp.q, sp.spend,
                   sp.merchant, sp.side].map(str).join("|");
  useEffect(() => {
    setQ(str(sp.q));
    setAcct(str(sp.acct));
    setCat(str(sp.cat));
    setBucket(str(sp.bucket));
    setAsOf(str(sp.as_of));
    setCounted(str(sp.counted) === "1");
    setSpend(str(sp.spend));
    setMerchant(str(sp.merchant));
    setSide(str(sp.side));
    setDrillRefused(false);
    setDateFrom(str(sp.date_from));
    setDateTo(str(sp.date_to));
    setYm(routeYm(sp.y, sp.m, now));
    setMonthScope(!!((sp.bucket && sp.m) || (sp.cat && sp.y && sp.m)));
    setYearScope(!!(sp.bucket && sp.y && !sp.m));
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
      // typing is an all-history search — it leaves every scope the
      // Enter path's leaveScope leaves: a counted category listing
      // ignores the term and, with the month dropped, jumped to the
      // current month (setters inline rather than the helper: a stable
      // effect body)
      if (want) {
        setBucket(""); setAsOf(""); setCounted(false);
        setMonthScope(false); setYearScope(false);
        setSpend(""); setMerchant(""); setSide("");
      }
      setPage(1);
    }, 250);
    return () => clearTimeout(id);
  }, [q]);

  // the web's param split: any filter/search → the search path (with
  // paging); otherwise the month view
  const searching = !!(search || acct || cat || bucket || spend || dateFrom
                       || dateTo || channel || checksF);
  const noAccounts = useNoAccounts();
  const params: TxnQuery = {
    ...(spend
      // a Spending figure IS the query: the server names its rows, and no
      // sheet filter rides along (web same)
      ? { spend, page: String(page), ...(cat ? { cat } : {}),
          ...(merchant ? { merchant } : {}), ...(side ? { side } : {}) }
      : bucket
      ? { bucket, y: String(ym.y), page: String(page),
          ...(yearScope ? {} : { m: String(ym.m) }),
          ...(asOf ? { as_of: asOf } : {}) }
      : searching
      ? { q: search, acct, cat, page: String(page),
          date_from: dateFrom, date_to: dateTo,
          // a month's category: no date range, so the server reads y&m
          // and scopes to that month — what a link from a month's lens
          // means, and what the month arrows then re-query
          ...(monthScope ? { y: String(ym.y), m: String(ym.m) } : {}),
          ...(counted && cat ? { counted: "1" } : {}),
          ...(channel ? { channel } : {}),
          ...(checksF ? { checks: "1" } : {}) }
      : { y: String(ym.y), m: String(ym.m) }),
    ...(reimbF && !bucket && !spend && !(counted && cat) ? { reimb: "1" } : {}),
    // the search path is owner-agnostic server-side (the web sends owner
    // only in month view) — sending it anyway lit a filter chip that did
    // nothing
    // a bucket listing is the engine's own row set — the lenses would
    // only empty it under a non-zero chip, so none rides along (web same)
    ...(bucket || spend || (counted && cat) ? {} : {
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
  const [pilePages, setPilePages] = useState<Txn[][]>([]);
  const pile = useMemo(() => flattenPile(pilePages), [pilePages]);
  const filterKey =
    `${search}|${acct}|${cat}|${bucket}|${monthScope ? `${ym.y}-${ym.m}` : ""
     }|${reimbF}|${dateFrom}|${dateTo}|${owner}|${scope}|${channel}|${checksF}`;
  const pileKey = useRef(filterKey);
  const qcPile = useQueryClient();
  // bumped once a whole-pile refresh has settled, so the merge below runs
  // again even when the current page came back unchanged
  const [pileTick, setPileTick] = useState(0);
  // A refresh (the pull, or the tab shown again after a write elsewhere)
  // must reach every page the pile shows, not only the current one: the
  // earlier pages are only ever re-read from the cache, so a row the server
  // added or changed there would never appear, while the total and Load
  // more already counted it. Each loaded page is fetched again (a page the
  // cache has dropped is fetched afresh), then the pile is merged once.
  const refreshAll = async () => {
    const earlier = searching && client
      ? Array.from({ length: page - 1 }, (_, i) => {
          const pp = { ...params, page: String(i + 1) };
          const key = ["transactions", pp];
          return qcPile.getQueryCache().find({ queryKey: key, exact: true })
            ? qcPile.refetchQueries({ queryKey: key, exact: true,
                                      type: "all" })
            : qcPile.prefetchQuery({ queryKey: key,
                                     queryFn: () => client.transactions(pp) });
        })
      : [];
    await Promise.allSettled([...earlier, query.refetch()]);
    setPileTick((t) => t + 1);
  };
  useRefetchWhenShown({ isStale: query.isStale, isFetching: query.isFetching,
                        refetch: refreshAll }, TXNS_KEY);
  // the question the pile answers: a new search shows the pile only once
  // its own first page has arrived, never the last search's rows
  const [pileFor, setPileFor] = useState(filterKey);
  useEffect(() => {
    // the previous question's page standing in (placeholderData) is not
    // this question's answer, and must not be merged into its pile
    if (!query.data || !searching || query.isPlaceholderData) return;
    const reset = pileKey.current !== filterKey || page === 1;
    pileKey.current = filterKey;
    setPileFor(filterKey);
    // every earlier page is re-read from the cache: a row edited or
    // removed on the detail screen is patched there, never in this state
    const cached = (p: number) => qcPile.getQueryData<TxnPage>(
      ["transactions", { ...params, page: String(p) }])?.rows;
    setPilePages((prev) =>
      mergePilePages(prev, page, query.data!.rows, cached, reset,
                     goneTxnIds));
    // params is rebuilt every render; filterKey + page name it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data, query.isPlaceholderData, searching, filterKey, page, qcPile,
      pileTick]);
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
  // pieces written so far of a selection larger than one request
  const [bulkProg, setBulkProg] = useState<{ done: number; total: number } | null>(null);
  const tooMany = sel.size > BULK_MAX;
  const bulk = useMutation({
    mutationFn: ({ action, category }:
        { action: string; category?: string }) =>
      runChunked([...sel], (piece) => client!.txnBulk(piece, action, category),
                 (done, total) => setBulkProg(total > BULK_CHUNK ? { done, total } : null)),
    onSettled: () => setBulkProg(null),
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
        v.action === "category" ? { category: catKey(v.category) }
        : v.action === "reimb_flag" ? { reimb_flag: true }
        : v.action === "reimb_unflag" ? { reimb_flag: false }
        : v.action === "biz_on" ? { biz_flag: true }
        : v.action === "biz_off" ? { biz_flag: false } : {};
      const flip = (rs: Txn[]) =>
        rs.map((x) => ids.has(x.id) ? { ...x, ...patch } : x);
      patchQueries<TxnPage>(qc2, ["transactions"],
        (o) => ({ ...o, rows: flip(o.rows) }));
      setPilePages((ps) => ps.map(flip));
      setSel(new Set());
      // every bulk action moves money (totals, verdict), and the flag
      // actions feed the Reimburse / business ledgers' own lists
      qc2.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
      qc2.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
      if (v.action.startsWith("reimb_"))
        qc2.invalidateQueries({ queryKey: ["reimburse"] });
      if (v.action.startsWith("biz_"))
        qc2.invalidateQueries({ queryKey: ["business"] });
      // the web's toast, always, with its exact meaning: flow rows ARE
      // applied — the server counts them, so the toast says they now
      // count as spending
      const bits = [`${r.applied} updated`];
      if (r.flow > 0)
        bits.push(`${r.flow} of them were transfers or income and now `
          + `count as spending`);
      if ((r.missing ?? 0) > 0)
        bits.push(`${r.missing} no longer exist`);
      Alert.alert("Bulk result", bits.join("\n"));
    },
    onError: (e) => {
      // pieces before a failed one stay written, so the lists refetch
      qc2.invalidateQueries({ queryKey: ["transactions"] });
      Alert.alert("Bulk update failed", errText(e));
    },
  });
  const bulkLocked = bulk.isPending || tooMany || demo;
  const toggleSel = useCallback((id: string) => setSel((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);
  // the row callbacks are stable across renders (the selection is read
  // through a ref) so a memoised TxnRow only repaints when its own
  // transaction or its own checkbox changes — not every row on every
  // keystroke, selection tap and refetch
  const selRef = useRef(sel);
  selRef.current = sel;
  const onLongRow = useCallback((t: Txn) => toggleSel(t.id), [toggleSel]);
  const onPressRow = useCallback((t: Txn) => {
    if (selRef.current.size > 0) { toggleSel(t.id); return; }
    setHandedOffTxn(t);
    router.push({ pathname: "/txn", params: { id: t.id } });
  }, [router, toggleSel]);

  const shift = (dir: -1 | 1) => {
    // a new month is a new question — page 3 of the old one does not
    // carry over
    setPage(1);
    // a past day's as-of belongs to its own month, so it does not travel
    setAsOf("");
    setYm(({ y, m }) => {
      const next = m + dir;
      return next < 1 ? { y: y - 1, m: 12 }
        : next > 12 ? { y: y + 1, m: 1 } : { y, m: next };
    });
  };

  const pageRows = query.data?.rows ?? [];
  const rows = searching
    ? (pileFor === filterKey && pile.length ? pile : pageRows) : pageRows;
  // the ledger is the app's one long date-ordered list, so it is the one
  // surface that gets day dividers — matching the web's Transactions page.
  // Today's recent pane is short enough that a header per day is chrome.
  // Each divider prints the server's whole-day spend (day_spend): a day a
  // page boundary cuts reads the whole day, not the rows loaded so far.
  // A search's pile merges the figures of every loaded page.
  const daySpendMap = useMemo(() => {
    if (!searching) return query.data?.day_spend;
    const m: Record<string, number> = {};
    for (let p = 1; p < page; p++)
      Object.assign(m, qcPile.getQueryData<TxnPage>(
        ["transactions", { ...params, page: String(p) }])?.day_spend ?? {});
    return Object.assign(m, query.data?.day_spend ?? {});
    // params is rebuilt every render; filterKey + page name it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data, searching, filterKey, page, qcPile, pileTick]);
  const listData = useMemo(() => withDayHeads(rows, daySpendMap),
                           [rows, daySpendMap]);
  const atNow = ym.y === now.getFullYear() && ym.m === now.getMonth() + 1;
  // a destination count, not a filter — same as the web hero's pill-link
  const reimbQ = useQuery({ queryKey: ["reimburse"],
    queryFn: () => client!.reimbPending(), enabled: !!client,
    staleTime: 5 * 60_000 });
  const awaitingN = reimbQ.data?.pending.length ?? 0;
  const d0 = query.data;
  // the range buttons' windows (TxnPage.ranges): the last page that
  // answered, so a failed read — a too-wide query — still leaves the
  // buttons that narrow it (web: the same)
  const lastRanges = useRef(d0?.ranges);
  if (d0?.ranges) lastRanges.current = d0.ranges;
  const ranges = d0?.ranges ?? lastRanges.current;
  const acctName = (id: string) =>
    accts.data?.accounts.find((a) => a.id === id)?.name ?? "1 account";
  // What a deep link narrowed the list to, in one chip: a bucket in the
  // plan's own words (the server's label, so a custom bucket reads as
  // the household wrote it), a month-scoped category by its label — each
  // with its month, because a link's scope is the part that otherwise
  // goes unsaid. No sheet control shows either one.
  const monthShort = `${(MONTHS[ym.m - 1] ?? "").slice(0, 3)} ${ym.y}`;
  // the previous result stays on screen while the next loads, so the
  // server's label counts only when it answers THIS bucket; until then
  // the link's own word stands in (web reads the same way)
  const bucketLabel =
    (d0?.bucket === bucket && d0?.bucket_label)
    || (bucket === "food" ? "Food"
        : bucket === "other" ? "Everything else" : bucket);
  // a Spending figure's chip is the server's words (drill_label: the
  // figure and the days it was read over), once they answer THIS figure
  const scopeChip = spend
    ? (d0?.drill_label && !query.isPlaceholderData ? d0.drill_label : null)
    : bucket
    ? `${bucketLabel} · ${yearScope ? ym.y : monthShort}`
    : cat && monthScope ? `${catLabel(cat)} · ${monthShort}` : null;
  // ✕ on the chip drops the grouping and keeps the month it named; a
  // Spending figure's chip returns to the month browse
  const clearScope = () => {
    setBucket(""); setAsOf(""); setCounted(false); setCat("");
    setMonthScope(false); setYearScope(false); setPage(1);
    setSpend(""); setMerchant(""); setSide("");
  };
  // A server from before the drill drops `spend=` and answers its plain
  // search (a category's every row, or the whole month), with no chip to
  // say so. Its reply carries no drill_label: say so in one line and go
  // back to the month browse rather than list rows that are not the figure.
  const drillState = spendDrillState(spend, d0, query.isPlaceholderData);
  useEffect(() => {
    if (drillState !== "unsupported") return;
    setDrillRefused(true);
    setBucket(""); setAsOf(""); setCounted(false); setCat("");
    setMonthScope(false); setYearScope(false); setPage(1);
    setSpend(""); setMerchant(""); setSide("");
  }, [drillState]);
  // every filter in force, as a removable chip under the search box — the
  // sheet can be closed and the list still says what is shaping it
  const active: { key: string; label: string; clear: () => void }[] = [
    ...(acct ? [{ key: "acct", label: acctName(acct),
                  clear: () => setAcct("") }] : []),
    ...(scopeChip ? [{ key: "scope-link", label: scopeChip,
                       clear: clearScope }] : []),
    ...(cat && !monthScope && !spend
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
  // rows on screen are then a subset, so say what THEY spent — the
  // server's figure (listed_spend: spending only, by the rule the day
  // totals and the verdict use; a business's spending when every listed
  // row is a business's, listed_scope saying so), as on the web. An older
  // server sends none and the line leaves the amount out.
  const narrowed = !searching && active.length > 0;
  const listedSpend = d0?.mode === "month" ? d0.listed_spend : undefined;
  const listedWord = listedSpendWord(
    d0?.mode === "month" ? d0.listed_scope : undefined);
  // The sheet's filters search ALL history, so reaching for one leaves a
  // bucket or a month's category behind rather than silently narrowing
  // the new filter to the month that happened to be on screen — and a
  // bucket listing ignores the sheet entirely, so a filter set inside
  // one would otherwise look broken. (web: leaveScope)
  const leaveScope = () => {
    setBucket(""); setAsOf(""); setCounted(false); setMonthScope(false);
    setYearScope(false); setSpend(""); setMerchant(""); setSide("");
  };
  // every filter change starts back at page 1 and leaves that scope
  const refilter = () => { setPage(1); leaveScope(); };
  // A large ledger takes a few seconds before the file arrives; until it
  // does the chip stands down, so a second tap cannot start a second
  // export beside the first (the server answers one with 429 anyway).
  const [exporting, setExporting] = useState(false);
  const exportRows = () => {
    if (exporting) return;
    const pairs = Object.entries(params)
      .filter(([k, v]) => k !== "page" && v !== undefined) as [string, string][];
    const qs = new URLSearchParams(pairs).toString();
    const pick = (format: "csv" | "xlsx") => async () => {
      setExporting(true);
      try {
        await client!.download(`/api/transactions/export?${qs}&format=${format}`,
                               `transactions.${format}`);
      } catch (e) {
        Alert.alert("Download failed", errText(e));
      } finally {
        setExporting(false);
      }
    };
    Alert.alert("Download these transactions",
                "Every row the current filters match, as a file.",
                [{ text: "CSV", onPress: pick("csv") },
                 { text: "XLSX", onPress: pick("xlsx") },
                 { text: "Cancel", style: "cancel" }]);
  };
  const clearAll = () => {
    setAcct(""); setCat(""); setBucket(""); setAsOf(""); setCounted(false);
    setMonthScope(false); setYearScope(false);
    setSpend(""); setMerchant(""); setSide("");
    setReimbF(false); setQ(""); setSearch("");
    setDateFrom(""); setDateTo(""); setOwner(""); setScope("");
    setChannel(""); setChecksF(false); setPage(1);
  };
  const summary = !d0 ? (query.isError
                           ? failLine(query.error, "transactions", "phone").text
                                       : "Loading…")
    : searching
      ? `${d0.total} result${d0.total === 1 ? "" : "s"}${
          d0.flow ? ` · out ${money(d0.flow.money_out)} · in ${
            money(d0.flow.money_in)}` : ""}`
      : `${MONTHS[ym.m - 1]} ${ym.y} · ${rows.length} transaction${
          rows.length === 1 ? "" : "s"}${
          narrowed && listedSpend != null
            ? ` · ${money(listedSpend)} ${listedWord}` : ""}`;

  const primary = accts.data?.primary_checking_id
    ? accts.data.accounts.find((a) => a.id === accts.data!.primary_checking_id)
    : undefined;
  const primaryBal = primary
    ? (primary.balance_available ?? primary.balance_current) : null;

  return (
    <View style={s.wrap}>
      <View style={{ flexDirection: "row", gap: 8 }}>
        <TextInput {...fieldA11y("Search payee, category")} style={[s.search, { flex: 1 }]}
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
        <Pressable accessibilityRole="button" style={[s.filterBtn, nFilters > 0 && s.filterOn]}
                   onPress={() => setShowFilters(true)}>
          <Text style={{ color: nFilters > 0 ? C.text : C.mut,
                         fontSize: 13, fontWeight: "600" }}>
            Filter{nFilters > 0 ? ` · ${nFilters}` : ""}
          </Text>
        </Pressable>
        {!viewer && <SnapReceiptButton />}
      </View>
      {drillRefused && (
        <Text style={{ color: C.mut, fontSize: 12, marginBottom: 6 }}>
          This server can&apos;t list a figure&apos;s rows — showing the month instead.
        </Text>
      )}
      {active.length > 0 && (
        // a ScrollView grows by default, so in this flex column the strip
        // would split the list's height with it and every chip stretched
        // to fill; the strip is exactly one chip tall
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
                    keyboardShouldPersistTaps="handled"
                    style={{ flexGrow: 0, flexShrink: 0 }}>
          <View style={{ flexDirection: "row", gap: 6 }}>
            {active.map((f) => (
              <Pressable accessibilityRole="button" key={f.key} style={[s.chip, s.chipOn]}
                         onPress={() => { f.clear(); setPage(1); }}
                         accessibilityLabel={`remove filter ${f.label}`}>
                <Text style={s.chipText(true)}>{f.label}  ✕</Text>
              </Pressable>
            ))}
            {active.length > 1 && (
              <Pressable accessibilityRole="button" style={s.chip} onPress={clearAll}>
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
          <Pressable style={s.navBtn}
                     accessibilityRole="button" accessibilityLabel="Previous month"
                     onPress={() => shift(-1)}>
            <Text style={s.navText}>‹</Text>
          </Pressable>
          <View style={{ flexDirection: "row", alignItems: "center",
                         gap: 10 }}>
            <Text style={s.navLabel}>{MONTHS[ym.m - 1]} {ym.y}</Text>
            {!atNow && (
              <Text style={{ color: C.accent, fontSize: 13 }}
                    onPress={() => { setPage(1); setAsOf("");
                                     setYm({ y: now.getFullYear(),
                                             m: now.getMonth() + 1 }); }}>
                today
              </Text>
            )}
          </View>
          <Pressable style={[s.navBtn, atNow && { opacity: 0.3 }]}
                     disabled={atNow}
                     accessibilityRole="button" accessibilityLabel="Next month"
                     onPress={() => shift(1)}>
            <Text style={s.navText}>›</Text>
          </Pressable>
        </View>
      )}
      <StaleLine query={query} />
      <FlashList
        ref={topRef}
        // The month's summary rides at the head of the LIST, not above it:
        // fixed over the list it left the rows a third of a phone screen,
        // and a header that grows when the totals arrive resized the list
        // under the recycler — rows scrolled into the top came up blank.
        ListHeaderComponent={
          <View style={{ gap: 8, paddingBottom: 8 }}>
            {/* the primary checking balance heads the ledger (mirrors the web
                header): the account the cash forecast starts from — the live
                pin, else the largest-balance pick — available balance first
                like the runway. Nothing without a checking account; "balance
                not set" when the row has none yet. Tapping opens Accounts,
                where the pin is changed */}
            {primary && (
              <Pressable onPress={() => router.push("/accounts" as never)}
                         accessibilityRole="link"
                         accessibilityLabel={`primary checking ${primary.name}, ${
                           primaryBal == null ? "balance not set" : money(primaryBal)}`}
                         style={{ flexDirection: "row", alignItems: "baseline",
                                  justifyContent: "flex-end", gap: 6,
                                  paddingHorizontal: 12 }}>
                <Text style={{ color: C.mut, fontSize: 12 }} numberOfLines={1}>
                  {primary.name}{primary.mask ? ` ••${primary.mask}` : ""}
                </Text>
                <Text style={{ color: (primaryBal ?? 0) < 0 ? C.bad : C.text,
                               fontSize: 16, fontWeight: "700",
                               fontVariant: ["tabular-nums"] }}>
                  {primaryBal == null ? "balance not set" : money(primaryBal)}
                </Text>
              </Pressable>
            )}
            {(waiting.data?.count ?? 0) > 0 && (
              <View style={{ flexDirection: "row" }}>
                <WaitingBadge />
              </View>
            )}
            {/* the page as a file, filters included: the share sheet takes it,
                since a browser tab cannot carry the bearer token. Owner-only:
                the door refuses a member or a viewer, so they get no chip */}
            {d0 && mayExport && (
              <View style={{ flexDirection: "row", justifyContent: "flex-end",
                             paddingHorizontal: 12 }}>
                <Pressable accessibilityRole="button"
                           style={s.chip} onPress={exportRows} disabled={exporting}
                           accessibilityLabel="download these transactions"
                           accessibilityState={{ busy: exporting, disabled: exporting }}>
                  <Text style={s.chipText(false)}>
                    {exporting ? "preparing download…" : "download CSV / XLSX"}
                  </Text>
                </Pressable>
              </View>
            )}
            {/* HERO — the month's cash flow (income / spend / net, the Cash
                Flow page's definitions; transfers and card payments are money
                moved, not made or spent) as the headline, not a footer; a
                search answers with its count + true total (+ per-item Amazon
                spend when the term matched order items) */}
            {d0 && (
              <View style={{ gap: 2,
                             opacity: figureGreyed(query) ? 0.55 : undefined }}>
                {!searching ? (
                  <>
                    {/* the month's four figures as labelled tiles, two across
                        (the web's row of tiles): a sentence of figures has to be
                        read, tiles are scanned. In and net are null for a month
                        before the bank feed starts: income unknown reads "—",
                        never a false loss */}
                    <View style={{ flexDirection: "row", gap: 6 }}>
                      {([
                        ["In", d0.in_sum == null ? "—" : money(d0.in_sum, false),
                         d0.in_sum == null ? C.mut : C.good,
                         (d0.in_expected ?? 0) > 0
                           ? `+${money(d0.in_expected!, false)} expected` : "landed"],
                        ["Out", money(d0.out_sum, false), C.text, "spent"],
                        ["Net", d0.net_sum == null ? "—"
                           : (d0.net_sum < 0 ? "" : "+") + money(d0.net_sum, false),
                         d0.net_sum == null ? C.mut
                           : d0.net_sum < 0 ? C.bad : C.good,
                         // mid-month the net is the month so far, with
                         // paychecks still to come (web: the same words)
                         (d0.in_expected ?? 0) > 0 ? "so far" : "in less out"],
                        ["A day", d0.per_day == null ? "—"
                           : money(d0.per_day, false),
                         d0.per_day == null ? C.mut : C.text, "spent on average"],
                      ] as const).map(([k, v, color, sub]) => (
                        <View key={k} accessible
                        accessibilityLabel={`${k} ${v}, ${sub}`}
                        style={{ flex: 1, minWidth: 0,
                                         backgroundColor: C.hover,
                                         borderColor: C.border, borderWidth: 1,
                                         borderRadius: 8, paddingVertical: 6,
                                         paddingHorizontal: 8 }}>
                    <Text style={{ color: C.mut, fontSize: 11 }}
                          numberOfLines={1}>{k}</Text>
                    <Text style={{ color, fontSize: 15, fontWeight: "700",
                                   fontVariant: ["tabular-nums"] }}
                          numberOfLines={1} adjustsFontSizeToFit>{v}</Text>
                  </View>
                ))}
                    </View>
                    <Text style={[s.heroMut, { fontSize: 12, marginTop: 4 }]}>
                      {(d0.in_expected ?? 0) > 0
                  ? `+${money(d0.in_expected!, false)} expected · ` : ""}
                {rows.length} txn{rows.length === 1 ? "" : "s"}
                      {(d0.biz_count ?? 0) > 0
                        ? ` · ${d0.biz_count} business (not in the totals)` : ""}
                    </Text>
                  </>
                ) : (
                  <Text style={{ color: C.text, fontSize: 18, fontWeight: "700",
                                 fontVariant: ["tabular-nums"] }}>
                    <>
                      {d0.total}
                      <Text style={s.heroMut}>
                        {" result"}{d0.total === 1 ? "" : "s"}
                      </Text>
                      {/* a bucket's or a counted category's rows: the label's
                          own figure; any other search: out / in / net over
                          every matched row by Cash Flow's rules (TxnPage.flow)
                          — the web hero's words and signs */}
                      {(bucket || counted || spend) && d0.amount_sum != null ? (
                        <>
                          <Text style={s.heroMut}> · </Text>
                          <Text style={d0.amount_sum < 0
                                         ? { color: C.good } : undefined}>
                            {money(-d0.amount_sum)}
                          </Text>
                          <Text style={s.heroMut}> total</Text>
                        </>
                      ) : d0.flow ? (
                        <>
                          <Text style={s.heroMut}> · out </Text>
                          {money(d0.flow.money_out)}
                          <Text style={s.heroMut}> · in </Text>
                          <Text style={d0.flow.money_in > 0
                                         ? { color: C.good } : undefined}>
                            {money(d0.flow.money_in)}</Text>
                          <Text style={s.heroMut}> · net </Text>
                          <Text style={{ color: d0.flow.net < 0 ? C.bad
                                           : d0.flow.net > 0 ? C.good : C.text }}>
                            {(d0.flow.net > 0 ? "+" : "") + money(d0.flow.net)}</Text>
                        </>
                      ) : null}
                      {/* the figure the label wore, when the raw sum of its
                          rows is not it: reimbursements netted, an envelope's
                          overflow counted only past the envelope */}
                      {bucket && d0.bucket === bucket && d0.bucket_amount != null
                        && d0.amount_sum != null
                        && Math.abs(d0.bucket_amount - d0.amount_sum) >= 0.005 ? (
                        <Text style={s.heroMut}>
                          {" · counted as "}
                          <Text style={{ color: C.text }}>{money(d0.bucket_amount)}</Text>
                        </Text>
                      ) : null}
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
                      {/* per DAY over the window asked for, beside the per-visit
                          average — mirrors the web hero */}
                      {d0.per_day != null ? (
                        <Text style={s.heroMut}>
                          {" · "}
                          <Text style={{ color: C.text }}>{money(d0.per_day, false)}</Text>
                          {"/day"}
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
                  </Text>
                )}
                {narrowed && (
                  <Text style={{ color: C.mut, fontSize: 12 }}>
                    {rows.length} listed
                    {listedSpend != null ? <>
                      {" · "}
                      <Text style={{ color: C.text }}>
                        {money(listedSpend)}</Text>
                      {` ${listedWord}`}</> : null}
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
                    {(d0.account_hits ?? []).map((h) => h.rows_listed ? (
                      <Text key={h.id}>
                        {" · includes "}
                        <Text style={{ color: C.text }}>{h.label}</Text>
                        {h.via_bank ? ` (the bank calls it “${h.bank_name}”)` : ""}
                      </Text>
                    ) : (
                      // the term matched rows by their own text; the account
                      // named like it is a link of its own (web: the same)
                      <Text key={h.id}>
                        {" · also: "}
                        <Text style={{ color: C.accent }} accessibilityRole="link"
                              onPress={() => { setQ(""); setSearch("");
                                               refilter(); setAcct(h.id); }}>
                          rows on {h.label}</Text>
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
          </View>
        }
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
                {/* whole dollars, as the web's day divider prints it */}
                {item.spent > 0 ? ` · ${money(item.spent, false)} spent` : ""}
              </Text>
            </View>
          ) : (
          <TxnRow t={item}
            selected={sel.size > 0 ? sel.has(item.id) : undefined}
            onLongPress={viewer ? undefined : onLongRow}
            onPress={onPressRow} />),
          [sel, viewer, onLongRow, onPressRow])}
        contentContainerStyle={s.list}
        refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                        onRefresh={refreshAll} />}
        ListEmptyComponent={query.data === undefined && query.isError
          ? <FailedLine q={query} what="transactions" />
          : query.data === undefined ? <Text style={s.mut}>Loading…</Text>
          // a filter that matches nothing says so, with no action (the
          // web's words); "Connect or import" only with no accounts at all
          : searching || owner || scope || reimbF
            ? <EmptyState title={EMPTY_LINES.filtered} />
          : noAccounts ? <NoAccountsLine />
          : <EmptyState title={EMPTY_LINES.monthNoTransactions} />}
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
              {bulkProg
                ? `Updating ${bulkProg.done} of ${bulkProg.total}…`
                : `${sel.size} selected`}
            </Text>
            {tooMany && (
              <Text style={{ color: C.mut, fontSize: 13 }}>
                select fewer than {BULK_MAX.toLocaleString("en-US")}
              </Text>
            )}
            {demo && (
              <Text style={{ color: C.warn, fontSize: 12 }}>
                Not available on the demo instance — synthetic data only.
              </Text>
            )}
            <Text style={s.bulkAct}
                  onPress={() =>
                    setSel(new Set(rows.map((r) => r.id)))}>
              All
            </Text>
            <Text style={[s.bulkAct, bulkLocked && { opacity: 0.4 }]}
                  onPress={() => !bulkLocked && setBulkPicking(true)}>
              Category
            </Text>
            {/* the web TxnTable's action names, verbatim */}
            <Text style={[s.bulkAct, bulkLocked && { opacity: 0.4 }]}
                  onPress={() => !bulkLocked
                    && bulk.mutate({ action: "reimb_flag" })}>
              ⚑ expect reimbursement
            </Text>
            <Text style={[s.bulkAct, bulkLocked && { opacity: 0.4 }]}
                  onPress={() => !bulkLocked
                    && bulk.mutate({ action: "reimb_unflag" })}>
              unflag
            </Text>
            <Text style={[s.bulkAct, bulkLocked && { opacity: 0.4 }]}
                  onPress={() => !bulkLocked
                    && bulk.mutate({ action: "biz_on" })}>
              mark business
            </Text>
            <Text style={[s.bulkAct, bulkLocked && { opacity: 0.4 }]}
                  onPress={() => !bulkLocked
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
                 <Pressable accessibilityRole="button" hitSlop={FOOTER_SLOP} style={[b.btn, b.btnQuiet]}
                            onPress={() => setBulkPicking(false)}>
                   <Text style={b.btnQuietText}>Close</Text>
                 </Pressable>
               </View>}>
        <TextInput {...fieldA11y("Filter")} style={s.search} placeholder="filter…"
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
            <Pressable accessibilityRole="button" key={c} style={s.catRow}
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
                 <Pressable accessibilityRole="button" hitSlop={FOOTER_SLOP} style={b.btn}
                            onPress={() => setShowFilters(false)}>
                   <Text style={b.btnText}>Done</Text>
                 </Pressable>
                 {(nFilters > 0 || q) && (
                   <Pressable accessibilityRole="button" hitSlop={FOOTER_SLOP}
                              style={[b.btn, b.btnQuiet]} onPress={clearAll}>
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
        {/* the site-wide This month … All vocabulary, the same eight
            windows every over-time toggle offers; a picked from/to that
            matches none of them lights nothing, and All is the
            no-date-filter state */}
        {/* the windows are Cash Flow's, sent with every page
            (TxnPage.ranges): complete months, This month to today, All
            unbounded; with no page yet the buttons wait, and after one they
            keep its windows through a failed read (web: the same) */}
        <SegmentedControl label="Date range" size="sm"
          value={TREND_RANGES.find((r) => {
            const v = ranges?.[r];
            return !!v && dateFrom === v.start && dateTo === v.end;
          }) ?? null}
          onChange={(r) => {
            // an explicit range answers a different question from "this
            // bucket, this month"
            const v = ranges?.[r];
            if (!v) return;
            setDateFrom(v.start); setDateTo(v.end); refilter();
          }}
          options={TREND_RANGES.map((r) => ({ value: r, label: RANGE_LABEL[r],
                                             disabled: !ranges?.[r] }))} />
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
              <Pressable accessibilityRole="button" style={[s.chip, !owner && s.chipOn]}
                         onPress={() => { setOwner(""); refilter(); }}>
                <Text style={s.chipText(!owner)}>all</Text>
              </Pressable>
              {ownersQ.data!.owners.map((o) => (
                <Pressable accessibilityRole="button" key={o}
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
                <Pressable accessibilityRole="button"
                           key={v} style={[s.chip, scope === v && s.chipOn]}
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
            <Pressable accessibilityRole="button"
                       key={v} style={[s.chip, channel === v && s.chipOn]}
                       onPress={() => { setChannel(v); refilter(); }}>
              <Text style={s.chipText(channel === v)}>{l}</Text>
            </Pressable>
          ))}
          <Pressable accessibilityRole="button" style={[s.chip, checksF && s.chipOn]}
                     onPress={() => { setChecksF((x) => !x); refilter(); }}>
            <Text style={s.chipText(checksF)}>checks only</Text>
          </Pressable>
        </View>
        <Pressable accessibilityRole="button" style={[s.chip, reimbF && s.chipOn,
                           { alignSelf: "flex-start", marginTop: 8 }]}
                   // a filter of its own, like the chips above: it leaves a
                   // bucket or a month's category rather than riding along
                   // (a bucket listing ignores it) — web: leaveScope
                   onPress={() => { setReimbF((x) => !x); refilter(); }}>
          <Text style={s.chipText(reimbF)}>reimbursements only</Text>
        </Pressable>
      </Sheet>
    </View>
  );
}

const s = {
  ...StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg, padding: 12, gap: 8 },
  search: { backgroundColor: C.card, borderColor: C.field, borderWidth: 1,
            borderRadius: 10, color: C.text, fontSize: 15,
            paddingHorizontal: 12, paddingVertical: 9 },
  nav: { alignItems: "center", flexDirection: "row",
         justifyContent: "space-between" },
  // a fingertip in each direction: the arrows are the screen's main control
  navBtn: { paddingHorizontal: 18, paddingVertical: 4, minHeight: 44,
            minWidth: 44, alignItems: "center", justifyContent: "center" },
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
