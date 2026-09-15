// Unit tests for src/lib/pure.ts — mobile has no JS test framework, so
// these run under plain node (v22.6+):
//
//   node --experimental-strip-types scripts/pure-logic-tests.mjs
//
// The server suite invokes this too (tests/test_mobile_budget_shape.py),
// so `make test` goes red if the banker's rounding or the savings-goals
// merge regresses.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import zlib from "node:zlib";
import test from "node:test";

import { ApiError, countsInTotals, errText, inOperatingProfit }
  from "../src/lib/api.ts";
import { categoryForServer, CLEAR_CATEGORY, composeBugReport,
         filterCategories, dayLabel, daySpend,
         elevationProof, hostOf,
         hueOf, isPrivateHost, isValidYmd, logoProxyUrl, monogram,
         serverUrlVerdict, lastSyncPhrase, mergeSavingsGoals, money, numv,
         pyround, resolveFace, withDayHeads }
  from "../src/lib/pure.ts";

// ---- banker's rounding: whole-dollar figures must match the server's
// Python round(), the web and the daily email on every .5 boundary ----

test("halves round to the even neighbour, both signs", () => {
  assert.equal(pyround(2.5), 2);
  assert.equal(pyround(3.5), 4);
  assert.equal(pyround(-186.5), -186);
  assert.equal(pyround(-187.5), -188);
  assert.equal(pyround(-0.5), 0);
  assert.equal(pyround(0.5), 0);
});

test("non-halves round to the nearest integer, both signs", () => {
  assert.equal(pyround(2.4), 2);
  assert.equal(pyround(2.6), 3);
  assert.equal(pyround(-2.4), -2);
  assert.equal(pyround(-2.6), -3);
  assert.equal(pyround(187), 187);
});

test("whole-dollar money() renders via banker's rounding", () => {
  // Math.round(-186.5) is -186 but Math.round(186.5) is 187 —
  // half-away/half-up regressions show up on these exact strings
  assert.equal(money(-186.5, false), "-$186");
  assert.equal(money(186.5, false), "$186");
  assert.equal(money(-187.5, false), "-$188");
  assert.equal(money(2.5, false), "$2");
  assert.equal(money(1234.56, false), "$1,235");
});

test("cents money() keeps two decimals and the sign prefix", () => {
  assert.equal(money(1234.5), "$1,234.50");
  assert.equal(money(-0.25), "-$0.25");
});

test("numv strips currency formatting and never yields NaN", () => {
  assert.equal(numv("$1,234.56"), 1234.56);
  assert.equal(numv(" 42 "), 42);
  assert.equal(numv(""), 0);
  assert.equal(numv("abc"), 0);
});

// ---- money aggregates the screens add up themselves must agree with
// the server's, or the phone shows a different number than the web ----

const acct = (bal, link = null) => ({ balance_current: bal, link });
const sumTotals = (list) => list.filter(countsInTotals)
  .reduce((t, a) => t + (a.balance_current ?? 0), 0);

test("a dual-sourced account's balance counts once, not once per source",
     () => {
  // one real checking account fed by two aggregators: the primary
  // serves, the backup is a shadow of the SAME $1,000
  const linked = (rank, primary) =>
    ({ group_id: "g1", home_rank: rank, primary, healthy: true });
  const rows = [acct(1000, linked(1, true)), acct(1000, linked(2, false)),
                acct(250)];
  assert.equal(sumTotals(rows), 1250);   // not 2250
  assert.equal(rows.filter(countsInTotals).length, 2);
});

test("an unlinked account always counts, and a sick primary still does",
     () => {
  // health decides which source SERVES, never whether the money is
  // real — dropping a down primary would erase the account's balance
  assert.equal(countsInTotals(acct(10)), true);
  assert.equal(countsInTotals(acct(10, undefined)), true);
  assert.equal(countsInTotals(
    acct(10, { group_id: "g", home_rank: 1, primary: true,
               healthy: false })), true);
});

// ---- business profit: only operating expenses reduce it ----

const bizRow = (amount, bucket = null, date = "2026-08-04") =>
  ({ amount, bucket, date });
const profit = (rows, start) => rows
  .filter((x) => inOperatingProfit(x, start))
  .reduce((t, x) => t - x.amount, 0);

test("capitalized start-up and organizational costs stay out of profit",
     () => {
  // a $600 filing fee is a §248 organizational cost: the server's
  // net_operating never saw it, so a month figure that subtracts it
  // reads as a loss the year figure above it does not show
  const rows = [bizRow(-2000), bizRow(400, "operating"),
                bizRow(600, "organizational"), bizRow(300, "startup_195")];
  assert.equal(profit(rows), 1600);      // not 700
});

test("an unbucketed expense follows the server's start-date default",
     () => {
  // before the business opened it is pre-operating (§195); on or after,
  // it is an ordinary operating expense
  assert.equal(inOperatingProfit(bizRow(100, null, "2026-01-05"),
                                 "2026-06-01"), false);
  assert.equal(inOperatingProfit(bizRow(100, null, "2026-08-04"),
                                 "2026-06-01"), true);
  assert.equal(inOperatingProfit(bizRow(100, null, "2026-01-05"), null),
               true);
});

test("revenue counts whatever bucket rides along", () => {
  // money in is never bucketed server-side; a stale bucket on an income
  // row must not delete the revenue
  assert.equal(inOperatingProfit(bizRow(-500, "organizational")), true);
});

// ---- the category-clear sentinel: the server clears on the empty
// string; the literal must never reach the wire ----

test("__clear__ maps to the empty string; real names pass through", () => {
  assert.equal(categoryForServer(CLEAR_CATEGORY), "");
  assert.equal(categoryForServer("GROCERIES"), "GROCERIES");
  assert.equal(categoryForServer(""), "");
});

// ---- last_sync is a server-rendered phrase, never a date ----

test("last_sync phrase reaches the screen verbatim", () => {
  assert.equal(lastSyncPhrase("synced 12m ago"), "synced 12m ago");
  assert.equal(lastSyncPhrase("last synced 3 hours ago"),
               "last synced 3 hours ago");
});

test("even a timestamp-shaped last_sync is never reformatted", () => {
  // a Date round-trip would change this string; verbatim proves the
  // render never parses
  assert.equal(lastSyncPhrase("2026-08-11 02:00"), "2026-08-11 02:00");
});

test("missing last_sync yields null so the line is skipped", () => {
  assert.equal(lastSyncPhrase(""), null);
  assert.equal(lastSyncPhrase(null), null);
  assert.equal(lastSyncPhrase(undefined), null);
});

// ---- YYYY-MM-DD validation: real calendar dates only ----

test("isValidYmd accepts real dates, including leap day", () => {
  assert.equal(isValidYmd("2026-08-11"), true);
  assert.equal(isValidYmd("2024-02-29"), true);
  assert.equal(isValidYmd("1999-12-31"), true);
});

test("isValidYmd rejects impossible calendar dates", () => {
  assert.equal(isValidYmd("2026-02-30"), false);
  assert.equal(isValidYmd("2023-02-29"), false);
  assert.equal(isValidYmd("2026-13-01"), false);
  assert.equal(isValidYmd("2026-00-10"), false);
  assert.equal(isValidYmd("2026-04-31"), false);
});

test("isValidYmd rejects malformed shapes", () => {
  assert.equal(isValidYmd("8/11/2026"), false);
  assert.equal(isValidYmd("2026-8-1"), false);
  assert.equal(isValidYmd("2026-08-11T00:00"), false);
  assert.equal(isValidYmd(""), false);
  assert.equal(isValidYmd("tomorrow"), false);
});

// ---- savings-goals merge: POST /api/settings replaces the array
// wholesale, so this merge is the only lost-update guard ----

const draft = (name, over = {}) => ({
  name, target: "100", target_date: "", monthly_plan: "10",
  account_id: "", tokens: "", start_balance: "0", mode: "monthly", ...over,
});
const goal = (name, over = {}) => ({
  name, target: 100, target_date: null, monthly_plan: 10,
  account_id: null, tokens: [], start_balance: 0, ...over,
});
const names = (out) => out.map((g) => g.name).sort();

test("untouched row defers to the server's live copy", () => {
  const out = mergeSavingsGoals(
    [draft("Trip", { monthly_plan: "10" })],
    [goal("Trip", { monthly_plan: 999 })],
    ["Trip"]);
  assert.equal(out.length, 1);
  assert.equal(out[0].monthly_plan, 999);
});

test("a goal deleted elsewhere stays deleted when its row is untouched",
     () => {
  const out = mergeSavingsGoals(
    [draft("Trip"), draft("Car")],
    [goal("Car")],           // Trip was deleted on another device
    ["Trip", "Car"]);
  assert.deepEqual(names(out), ["Car"]);
});

test("a goal added elsewhere survives a save from this card", () => {
  const out = mergeSavingsGoals(
    [draft("Trip", { touched: true, monthly_plan: "25" })],
    [goal("Trip"), goal("House")],   // House appeared since mount
    ["Trip"]);
  assert.deepEqual(names(out), ["House", "Trip"]);
  assert.equal(out.find((g) => g.name === "Trip").monthly_plan, 25);
});

test("a touched row overrides the live copy with the draft's values",
     () => {
  const out = mergeSavingsGoals(
    [draft("Trip", { touched: true, target: "$2,000",
                     tokens: "trip, hawaii" })],
    [goal("Trip", { target: 1 })],
    ["Trip"]);
  assert.equal(out.length, 1);
  assert.equal(out[0].target, 2000);
  assert.deepEqual(out[0].tokens, ["trip", "hawaii"]);
});

test("renaming a goal here neither duplicates nor resurrects the old name",
     () => {
  const out = mergeSavingsGoals(
    [draft("Vacation", { touched: true })],  // was "Trip" at mount
    [goal("Trip")],
    ["Trip"]);
  assert.deepEqual(names(out), ["Vacation"]);
});

test("the contribution mode survives the merge in both directions", () => {
  // a touched row carries its drafted mode; an untouched row keeps the
  // sweep flag another device saved — losing it would silently turn a
  // conditional contribution back into a fixed monthly outflow
  const out = mergeSavingsGoals(
    [draft("Trip", { touched: true, mode: "sweep" }), draft("Car")],
    [goal("Trip"), goal("Car", { mode: "sweep" })],
    ["Trip", "Car"]);
  assert.equal(out.find((g) => g.name === "Trip").mode, "sweep");
  assert.equal(out.find((g) => g.name === "Car").mode, "sweep");
});

test("blank rows are dropped, not saved as empty goals", () => {
  const out = mergeSavingsGoals(
    [draft("  ", { touched: true }), draft("Trip", { touched: true })],
    [], []);
  assert.deepEqual(names(out), ["Trip"]);
});

// ---- local calendar dates: a Y-M-D built through toISOString() drifts
// a day across the UTC boundary — east of Greenwich after local Date
// math, west of Greenwich every evening. These run under a forced
// non-UTC TZ so a UTC round trip sneaking back in goes red. ----

import { calendarCells, shiftYmd, ymd } from "../src/lib/dates.ts";

// node on linux re-reads TZ per Date call, so the suite can pin both
// hemispheres in one process
const withTz = (tz, fn) => {
  const old = process.env.TZ;
  process.env.TZ = tz;
  try { fn(); } finally {
    if (old === undefined) delete process.env.TZ;
    else process.env.TZ = old;
  }
};

test("ymd reports the LOCAL date on both sides of the UTC boundary",
     () => {
  // UTC+14: local midnight is still the previous day in UTC
  withTz("Etc/GMT-14", () => {
    assert.equal(ymd(new Date(2026, 7, 12, 0, 30)), "2026-08-12");
  });
  // UTC-8: a local evening is already tomorrow in UTC
  withTz("Etc/GMT+8", () => {
    assert.equal(ymd(new Date(2026, 7, 12, 21, 0)), "2026-08-12");
  });
});

test("shiftYmd steps exactly one calendar day in any timezone", () => {
  for (const tz of ["Etc/GMT-14", "Etc/GMT+8", "UTC"]) {
    withTz(tz, () => {
      assert.equal(shiftYmd("2026-08-12", -1), "2026-08-11");
      assert.equal(shiftYmd("2026-08-12", 1), "2026-08-13");
      // month and year boundaries come from Date, not string math
      assert.equal(shiftYmd("2026-01-01", -1), "2025-12-31");
      assert.equal(shiftYmd("2026-08-31", 1), "2026-09-01");
    });
  }
});

test("calendar cells sit under their real weekday despite gaps", () => {
  // events with a 8- and 11-day gap: Wed Aug 12, Thu Aug 20, Mon Aug 31
  const days = [{ date: "2026-08-12" }, { date: "2026-08-20" },
                { date: "2026-08-31" }];
  const cells = calendarCells(days, "2026-08-12", 35);
  assert.equal(cells.length, 35);
  // window opens on the Monday of the start's week
  assert.equal(cells[0].date, "2026-08-10");
  // each event lands in its own weekday column (index % 7)
  assert.equal(cells[2].d, days[0]);    // Wednesday
  assert.equal(cells[10].d, days[1]);   // Thursday, next week
  assert.equal(cells[21].d, days[2]);   // Monday, week 4
  // gap days still occupy a cell, with no event attached
  assert.equal(cells[3].date, "2026-08-13");
  assert.equal(cells[3].d, undefined);
  assert.equal(cells.filter((c) => c.d).length, 3);
});

test("login's server can only come from the connect flow's setter", async () => {
  // the login screen signs into getVerifiedServerUrl() or bounces to
  // connect — an app-launch deep link must find the slot empty, and
  // nothing but the connect flow's own setter may fill it
  const { getVerifiedServerUrl, setVerifiedServerUrl } =
    await import("../src/lib/api.ts");
  assert.equal(getVerifiedServerUrl(), null);
  setVerifiedServerUrl("https://money.example.org");
  assert.equal(getVerifiedServerUrl(), "https://money.example.org");
});

test("the transaction screen only trusts a row handed over in-app", async () => {
  // /txn is deep-linkable, so the route carries an id and the ROW comes
  // from the in-memory slot a ledger tap fills — an id nobody deposited
  // (an outside link) yields nothing
  const { getHandedOffTxn, setHandedOffTxn } =
    await import("../src/lib/api.ts");
  assert.equal(getHandedOffTxn("t-1"), null);
  assert.equal(getHandedOffTxn(undefined), null);
  const row = { id: "t-1", date: "2026-08-01", amount: 12.5, payee: "Cafe",
                category: "dining", account: null, pending: 0 };
  setHandedOffTxn(row);
  assert.equal(getHandedOffTxn("t-1"), row);
  assert.equal(getHandedOffTxn("t-2"), null,
               "a different id must not see the deposited row");
});

// ---- plain-http server addresses ----
// http:// is refused for anything routable from the internet and
// allowed (with a warning) for the LAN / loopback hosts a self-hosted
// box answers at; Android's network config can't say this, so this is
// where the rule lives

test("private hosts: loopback, RFC1918, link-local, CGNAT, mDNS", () => {
  for (const h of ["localhost", "127.0.0.1", "10.0.0.5", "192.168.1.20",
                   "172.16.0.1", "172.31.255.254", "169.254.1.1",
                   "100.64.0.1", "100.127.255.255", "nas.local",
                   "box.lan", "::1", "fe80::1", "fd12::1", "[::1]"])
    assert.equal(isPrivateHost(h), true, h);
  for (const h of ["oikonome.example.com", "8.8.8.8", "172.32.0.1",
                   "172.15.0.1", "100.128.0.1", "11.0.0.1", "",
                   "evil.local.example.com", "2001:db8::1",
                   // hostnames that merely start with the ULA letters
                   // are public — the fc00::/7 test is for addresses
                   "homebox.example.org", "fc-remote.example.com",
                   "fe80host.example.net"])
    assert.equal(isPrivateHost(h), false, h);
  assert.equal(serverUrlVerdict("http://homebox.example.org"), "http-public");
});

test("hostOf strips scheme, userinfo, port and path", () => {
  assert.equal(hostOf("http://10.0.0.5:8042/api"), "10.0.0.5");
  assert.equal(hostOf("https://user@money.example.org/"), "money.example.org");
  assert.equal(hostOf("http://[::1]:8042"), "::1");
  assert.equal(hostOf("garbage"), "");
});

test("the connect verdict: https ok, http LAN warned, http public refused",
     () => {
  assert.equal(serverUrlVerdict("https://money.example.org"), "ok");
  assert.equal(serverUrlVerdict("http://192.168.1.20:8042"), "http-private");
  assert.equal(serverUrlVerdict("http://localhost:8042"), "http-private");
  assert.equal(serverUrlVerdict("http://money.example.org"), "http-public");
  assert.equal(serverUrlVerdict("HTTP://8.8.8.8"), "http-public");
  // the userinfo trick — the host is what follows the @, not what precedes
  assert.equal(serverUrlVerdict("http://192.168.1.1@evil.example/"),
               "http-public");
});

// ---- which hero face the Today page shows ----
//
// A demo instance refuses every settings write, so the face is kept on the
// device there. Everywhere else the account is the durable value and the
// device must never speak over it — a demo-only preference leaking into an
// ordinary account would show one person a face nobody chose.

test("the account's saved face wins on an ordinary instance", () => {
  assert.equal(resolveFace({ saved: "detail" }), "detail");
  assert.equal(resolveFace({ saved: "summary" }), "summary");
  // a stale per-device value from some earlier demo must NOT speak over it
  assert.equal(resolveFace({ saved: "summary", perDevice: "detail" }),
               "summary");
});

test("on a demo the device's remembered face wins over the account's", () => {
  assert.equal(resolveFace({ saved: "summary", perDevice: "detail",
                             demo: true }), "detail");
  assert.equal(resolveFace({ saved: "detail", perDevice: "summary",
                             demo: true }), "summary");
});

test("a viewer sees the household's saved face, demo or not", () => {
  assert.equal(resolveFace({ saved: "summary", perDevice: "detail",
                             demo: true, viewer: true }), "summary");
});

test("the tap you just made wins over everything", () => {
  assert.equal(resolveFace({ override: "detail", saved: "summary" }),
               "detail");
  assert.equal(resolveFace({ override: "summary", perDevice: "detail",
                             saved: "detail", demo: true }), "summary");
});

test("nothing chosen anywhere is the summary face", () => {
  assert.equal(resolveFace({}), "summary");
  assert.equal(resolveFace({ demo: true }), "summary");
});


// ---- ledger day dividers: the header before each date change, and the
// day's spend it carries (mirrors the web's TxnTable dayGroups) ----

const tx = (id, date, amount, category = "FOOD AND DRINK", counts) =>
  ({ id, date, amount, category,
     ...(counts === undefined ? {} : { counts_as_spend: counts }) });

test("a header lands before each day, rows keep their order", () => {
  const out = withDayHeads([
    tx("a", "2026-08-13", 4.75), tx("b", "2026-08-13", 62.18),
    tx("c", "2026-08-12", 9.99),
  ]);
  assert.deepEqual(out.map((it) => "kind" in it ? "HEAD" : it.id),
                   ["HEAD", "a", "b", "HEAD", "c"]);
  assert.equal(out[0].count, 2);
  assert.equal(out[3].count, 1);
});

test("the day total sums exactly the rows the server marks as spend", () => {
  // the server's counts_as_spend flag IS the rule — the same SQL as the
  // verdict, the email and the Business worksheet. The client must not
  // second-guess it: row "c" is a card payment whose display category
  // ("LOAN PAYMENTS", spaces, primary) reveals nothing, and row "b" is a
  // loan-account row the client cannot identify at all.
  const spent = daySpend([
    tx("a", "2026-08-10", 236.44, "MEDICAL", true),
    tx("b", "2026-08-10", 800, "FOOD AND DRINK", false),
    tx("c", "2026-08-10", 100, "LOAN PAYMENTS", false),
    tx("d", "2026-08-10", -2450, "INCOME", false),
  ]);
  assert.equal(spent, 236.44);
});

test("an old server without counts_as_spend still gets a non-zero day total", () => {
  // graceful degradation: when the field is absent the client falls back
  // to the display-category approximation (money out, not a transfer)
  // rather than showing $0 for every day
  const spent = daySpend([
    tx("a", "2026-08-10", 236.44, "MEDICAL"),
    tx("b", "2026-08-10", 800, "TRANSFER_OUT"),
    tx("c", "2026-08-10", -2450, "INCOME"),
  ]);
  assert.equal(spent, 236.44);
});

test("a day with no spend still reports its count", () => {
  const [head] = withDayHeads([tx("a", "2026-08-10", -2450, "INCOME")]);
  assert.equal(head.count, 1);
  assert.equal(head.spent, 0);
});

test("an empty ledger produces no headers", () => {
  assert.deepEqual(withDayHeads([]), []);
});

test("headers are keyed by their first row, so a revisited date cannot collide", () => {
  // an unsorted list can return to a date; two headers sharing a key is a
  // duplicate key in a virtualized list, not a cosmetic problem
  const out = withDayHeads([
    tx("a", "2026-08-13", 1), tx("b", "2026-08-12", 1), tx("c", "2026-08-13", 1),
  ]).filter((it) => "kind" in it);
  assert.equal(out.length, 3);
  assert.equal(new Set(out.map((h) => h.key)).size, 3);
});

test("the day label names the weekday and never slips a day westward", () => {
  // new Date("2026-08-13") parses as UTC and renders as the 12th in any
  // negative-offset zone — the label must be built from the parts
  process.env.TZ = "America/Los_Angeles";
  assert.match(dayLabel("2026-08-13"), /Aug\s*13/);
  assert.match(dayLabel("2026-08-13"), /^Thu/);
});

// ---- merchant monogram / hue: the same merchant draws the same mark on
// every screen and on the web (identical formula in MerchantAvatar.tsx) ----

test("monogram takes two initials, or two letters of a lone word", () => {
  assert.equal(monogram("Trader Joe's"), "TJ");
  assert.equal(monogram("Costco"), "Co");
  assert.equal(monogram("7-Eleven"), "7E");
  assert.equal(monogram(""), "?");
});

test("hue is stable and in range", () => {
  assert.equal(hueOf("Costco"), hueOf("Costco"));
  assert.notEqual(hueOf("Costco"), hueOf("Target"));
  for (const n of ["a", "Costco", "U-Haul", "Zebra Coffee"]) {
    assert.ok(hueOf(n) >= 0 && hueOf(n) < 360);
  }
});

// ---- the logo proxy allowlist: /api/logo serves only the two Plaid CDNs,
// so a URL it would refuse must never become a request (a guaranteed 400
// that still spends the shared rate limit) — the monogram covers it ----

test("only the two Plaid logo CDNs get proxied", () => {
  const b = "https://box.example";
  assert.equal(
    logoProxyUrl(b, "https://plaid-merchant-logos.plaid.com/costco.png"),
    b + "/api/logo?u=https%3A%2F%2Fplaid-merchant-logos.plaid.com%2Fcostco.png");
  assert.ok(logoProxyUrl(
    b, "https://plaid-counterparty-logos.plaid.com/x/y.png"));
  // any other host, scheme, extension, port or query is refused
  assert.equal(logoProxyUrl(b, "https://evil.example/costco.png"), null);
  assert.equal(logoProxyUrl(
    b, "http://plaid-merchant-logos.plaid.com/costco.png"), null);
  assert.equal(logoProxyUrl(
    b, "https://plaid-merchant-logos.plaid.com/costco.svg"), null);
  assert.equal(logoProxyUrl(
    b, "https://plaid-merchant-logos.plaid.com:8443/costco.png"), null);
  assert.equal(logoProxyUrl(
    b, "https://plaid-merchant-logos.plaid.com/costco.png?x=1"), null);
  assert.equal(logoProxyUrl(
    b, "https://plaid-merchant-logos.plaid.com/" + "a".repeat(400) + ".png"),
    null);
  assert.equal(logoProxyUrl(b, null), null);
  assert.equal(logoProxyUrl(b, ""), null);
});

// ---- sign-out keeps its promise: the dialog says the server-side
// device token is revoked, so a local-only sign-out is never a silent
// fallback for a devices list that just hadn't loaded yet ----

import { signOutAction } from "../src/lib/pure.ts";

test("sign-out revokes this device's token when it is in the list", () => {
  const devs = [{ id: "a", current: false }, { id: "b", current: true }];
  assert.deepEqual(signOutAction(true, devs), { kind: "revoke", id: "b" });
  // even off a stale-but-successful fetch — the id is what matters
  assert.deepEqual(signOutAction(false, devs), { kind: "revoke", id: "b" });
});

test("an unfetched or failed devices list asks, never silently local",
     () => {
  // fast tap before the query lands
  assert.deepEqual(signOutAction(false, undefined), { kind: "ask" });
  // refetch failed too — the token's fate is unknown
  assert.deepEqual(signOutAction(false, []), { kind: "ask" });
});

test("a successful fetch without this device means already revoked — "
     + "local sign-out keeps the promise", () => {
  assert.deepEqual(signOutAction(true, []), { kind: "local" });
  assert.deepEqual(signOutAction(true, [{ id: "a", current: false }]),
                   { kind: "local" });
});

// ---- read-only (402) paywall notice: a lapsed subscription refuses
// every WRITE with 402 while reads keep working — the notice must fire
// for blocked writes only, collapse repeats into one dialog, prefer the
// server's sentence, and (asserted here by construction) never share
// anything with the 401 sign-out path ----

import { isWriteMethod, PAYWALL_FALLBACK, PAYWALL_NOTICE_WINDOW_MS,
         paywallNotice } from "../src/lib/api.ts";

test("only writes surface the paywall notice — reads never nag", () => {
  assert.equal(paywallNotice("subscription lapsed", "GET", 0, 60_000),
               null);
  assert.equal(paywallNotice("subscription lapsed", "HEAD", 0, 60_000),
               null);
  assert.equal(paywallNotice("subscription lapsed", undefined, 0, 60_000),
               null, "an unknown method must not nag");
  assert.equal(paywallNotice("subscription lapsed", "POST", 0, 60_000),
               "subscription lapsed");
  assert.equal(paywallNotice("subscription lapsed", "DELETE", 0, 60_000),
               "subscription lapsed");
  // the req helper lower-cases nothing; the check must not care
  assert.ok(isWriteMethod("post") && isWriteMethod("Put"));
  assert.ok(!isWriteMethod("get") && !isWriteMethod(""));
});

test("repeated blocked writes collapse into one notice per window", () => {
  const t0 = 1_000_000;
  assert.ok(paywallNotice("d", "POST", 0, t0), "first write notifies");
  assert.equal(paywallNotice("d", "POST", t0, t0 + 1), null);
  assert.equal(
    paywallNotice("d", "POST", t0, t0 + PAYWALL_NOTICE_WINDOW_MS - 1),
    null, "inside the window stays quiet");
  assert.ok(
    paywallNotice("d", "POST", t0, t0 + PAYWALL_NOTICE_WINDOW_MS),
    "a later blocked write may remind");
});

test("the server's sentence wins; the fallback still says read-only", () => {
  assert.equal(
    paywallNotice("this account is read-only until its standing is "
      + "restored", "POST", 0, 60_000),
    "this account is read-only until its standing is restored");
  // a bodyless 402 still says something, same wording as the web's toast
  assert.equal(paywallNotice(undefined, "POST", 0, 60_000),
               PAYWALL_FALLBACK);
  assert.equal(paywallNotice("   ", "POST", 0, 60_000), PAYWALL_FALLBACK);
  assert.match(PAYWALL_FALLBACK, /read-only/);
});

import { showsProviderDoor } from "../src/lib/pure.ts";

test("hosted hides the SimpleFIN door — the server refuses BYO there", () => {
  // web parity: the hosted ConnectHub renders no SimpleFIN row, and a
  // token-paste form on mobile would be a guaranteed 403
  assert.equal(showsProviderDoor("simplefin", true), false);
  // the other doors stay, hosted or not
  for (const key of ["plaid", "mx", "scripts", "files"]) {
    assert.equal(showsProviderDoor(key, true), true, key);
    assert.equal(showsProviderDoor(key, false), true, key);
  }
  // self-host keeps SimpleFIN first-class
  assert.equal(showsProviderDoor("simplefin", false), true);
});

// ---- Step-up: carrying a passkey ticket on the retried request ----

import { withRecoveryCode } from "../src/lib/pure.ts";

const JSON_INIT = { method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ new_password: "x" }) };
const FORM_INIT = { method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: "password=x" };

test("the ticket rides the recovery_code field of a JSON body", () => {
  const out = withRecoveryCode(JSON_INIT, "pkstep-abc");
  assert.deepEqual(JSON.parse(out.body),
                   { new_password: "x", recovery_code: "pkstep-abc" });
  // everything else about the request survives untouched
  assert.equal(out.method, "POST");
  assert.deepEqual(out.headers, JSON_INIT.headers);
});

test("a form body keeps its encoding and gains one field", () => {
  const out = withRecoveryCode(FORM_INIT, "pkstep-abc");
  assert.equal(out.body, "password=x&recovery_code=pkstep-abc");
});

test("a code the person already typed is never overwritten", () => {
  // spending a recovery code is their choice; a silent swap would send
  // the ticket and leave them believing the typed code did the work
  assert.equal(withRecoveryCode(
    { headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recovery_code: "typed" }) }, "pkstep-abc"),
    null);
  assert.equal(withRecoveryCode(
    { headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "password=x&recovery_code=typed" }, "pkstep-abc"), null);
});

test("a body the retry cannot extend gives up instead of guessing", () => {
  // null means "surface the server's refusal", which is what reveals the
  // card's recovery-code field — never a silently dropped ticket
  assert.equal(withRecoveryCode(undefined, "pkstep-abc"), null);
  assert.equal(withRecoveryCode({ method: "GET" }, "pkstep-abc"), null);
  assert.equal(withRecoveryCode(JSON_INIT, ""), null);
  assert.equal(withRecoveryCode(
    { headers: { "Content-Type": "multipart/form-data" },
      body: "…" }, "pkstep-abc"), null);
  // a JSON body that is not an object has nowhere to put the field
  assert.equal(withRecoveryCode(
    { headers: { "Content-Type": "application/json" },
      body: "[1,2]" }, "pkstep-abc"), null);
});

// ---- Card autopay marks on the cash forecast chart ----

import { autopayMarks } from "../src/lib/pure.ts";

const SERIES = [["2026-08-23", 100], ["2026-08-24", 90],
                ["2026-08-25", 80], ["2026-08-26", 70]];

test("an autopay is marked at its own day, not at an approximation", () => {
  assert.deepEqual(autopayMarks(SERIES, [["2026-08-25", -40, "Amex"]]),
                   [{ i: 2, label: "Amex autopay" }]);
});

test("a date outside the series is dropped, never clamped to an edge", () => {
  // clamping would draw a line claiming a payment lands on the last day
  assert.deepEqual(autopayMarks(SERIES, [["2026-12-01", -40, "Amex"]]), []);
  assert.deepEqual(autopayMarks(SERIES, undefined), []);
});

test("cards due the same day become one line, and many become a count", () => {
  assert.deepEqual(
    autopayMarks(SERIES, [["2026-08-24", -1, "Amex"], ["2026-08-24", -2, "Visa"]]),
    [{ i: 1, label: "Amex + Visa autopay" }]);
  assert.deepEqual(
    autopayMarks(SERIES, [["2026-08-24", -1, "A"], ["2026-08-24", -2, "B"],
                          ["2026-08-24", -3, "C"]]),
    [{ i: 1, label: "3 autopays" }]);
});

// ---- SecureStore keys: a scope may contain anything, a key may not ----

import { secureKey } from "../src/lib/pure.ts";

test("a server URL scope is sanitized into a legal SecureStore key", () => {
  // SecureStore accepts only [A-Za-z0-9._-]; ':' and '/' make it THROW, and
  // a caller that swallows storage errors then has a flag that silently
  // never persists — a wizard whose "left" mark never sticks cannot be left
  const k = secureKey("oikonome.leftWizard", "https://money.example.org");
  assert.match(k, /^[A-Za-z0-9._-]+$/);
  assert.ok(k.startsWith("oikonome.leftWizard."));
});

test("distinct servers keep distinct keys, and no scope keeps the base", () => {
  const a = secureKey("k", "https://a.example.com");
  const b = secureKey("k", "https://b.example.com");
  assert.notEqual(a, b);
  assert.equal(secureKey("k"), "k");
  assert.equal(secureKey("k", undefined), "k");
});

// ---- a 2xx that is not JSON: the captive-portal case ----

import { makeClient, parseOkBody, UNREADABLE_REPLY }
  from "../src/lib/api.ts";

test("a JSON 2xx body parses into the value the caller asked for", () => {
  assert.deepEqual(
    parseOkBody(200, "application/json; charset=utf-8", '{"ok":true}'),
    { ok: true });
});

test("an HTML 200 becomes an ApiError, never a thrown SyntaxError", () => {
  // a captive Wi-Fi portal / proxy sign-in page answers 200 with HTML;
  // r.json() on that throws out of whatever awaited the call, with no
  // ErrorBoundary under it
  let caught;
  try {
    parseOkBody(200, "text/html", "<html><body>Sign in to continue</body></html>");
  } catch (e) { caught = e; }
  assert.ok(caught instanceof ApiError, "non-JSON 2xx must be an ApiError");
  assert.equal(caught.status, 200);
  assert.equal(errText(caught), UNREADABLE_REPLY);
});

test("a truncated or empty body with a JSON content-type refuses too", () => {
  for (const body of ['{"ok":', "", "   "]) {
    assert.throws(() => parseOkBody(200, "application/json", body),
                  (e) => e instanceof ApiError && e.detail === UNREADABLE_REPLY);
  }
});

test("the refusal carries a snippet of the body, bounded", () => {
  let caught;
  try { parseOkBody(200, "text/html", "x".repeat(5000)); }
  catch (e) { caught = e; }
  assert.equal(typeof caught.body, "string");
  assert.equal(caught.body.length, 200);
});

test("a captive-portal 200 reaches the caller as an ApiError, not a crash",
     async () => {
  // the whole point of the helper: pinning it alone would still let the
  // client go back to a bare r.json() at the call site
  const real = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    "<html><body>Sign in to continue</body></html>",
    { status: 200, headers: { "content-type": "text/html" } });
  try {
    const c = makeClient("https://example.invalid", "tok", () => {});
    await assert.rejects(
      () => c.me(),
      (e) => e instanceof ApiError && e.detail === UNREADABLE_REPLY);
  } finally { globalThis.fetch = real; }
});

import { loginAndMintDevice } from "../src/lib/api.ts";

/** A server that answers the login POST normally and the device mint with
 *  whatever `mintReply` is — the captive-portal shape the sign-in path has
 *  to survive. */
function serverWhoseMintAnswers(mintReply) {
  return async (url) => {
    if (String(url).endsWith("/api/login")) {
      return new Response('{"mint_ticket":"tick"}',
        { status: 200, headers: { "content-type": "application/json" } });
    }
    if (String(url).endsWith("/api/devices")) return mintReply();
    return new Response('{"ok":true}',
      { status: 200, headers: { "content-type": "application/json" } });
  };
}

test("a captive portal answering the device mint is the app's own error",
     async () => {
  // sign-in is the highest-traffic request in the app and the one most
  // likely to be made on strange Wi-Fi; a bare .json() here would surface
  // the parser's own words ("Unexpected character: <") to the person
  // signing in
  const real = globalThis.fetch;
  globalThis.fetch = serverWhoseMintAnswers(() => new Response(
    "<html><body>Accept the terms to get online</body></html>",
    { status: 200, headers: { "content-type": "text/html" } }));
  try {
    await assert.rejects(
      () => loginAndMintDevice("https://example.invalid", "a@b.dev", "pw",
                               "", "Pixel", "android"),
      (e) => e instanceof ApiError && e.detail === UNREADABLE_REPLY);
  } finally { globalThis.fetch = real; }
});

test("a real mint reply still hands back the device credential", async () => {
  const real = globalThis.fetch;
  globalThis.fetch = serverWhoseMintAnswers(() => new Response(
    '{"token":"dev-tok","id":"dev-1"}',
    { status: 200, headers: { "content-type": "application/json" } }));
  try {
    assert.deepEqual(
      await loginAndMintDevice("https://example.invalid", "a@b.dev", "pw",
                               "", "Pixel", "android"),
      { token: "dev-tok", id: "dev-1" });
  } finally { globalThis.fetch = real; }
});

// ---- net-worth trend timeframe windows: baseline picking, gain math,
// shorter-than-range degradation ----
import { trendWindow } from "../src/lib/pure.ts";

test("trendWindow anchors to the last point and picks the baseline at/before the cutoff", () => {
  const pts = [
    ["2023-01-01", 100], ["2024-01-01", 200], ["2025-06-01", 300],
    ["2026-05-30", 350], ["2026-08-30", 400],
  ];
  // 1y back from the LAST point (2026-08-30) cuts at 2025-08-30; the
  // baseline is the last point at or before it — 2025-06-01 — so the
  // line starts at the value the gain is measured from
  const y1 = trendWindow(pts, "1y");
  assert.equal(pts[y1.start][0], "2025-06-01");
  assert.equal(y1.gain, 100);
});

test("trendWindow gain and pct measure from the baseline", () => {
  const pts = [
    ["2026-02-28", 100], ["2026-05-30", 200], ["2026-08-30", 300],
  ];
  const w = trendWindow(pts, "3m");
  assert.equal(pts[w.start][0], "2026-05-30");
  assert.equal(w.gain, 100);
  assert.equal(w.pct, 50);
  assert.equal(w.full, false);
});

test("trendWindow: a range longer than the data is the whole series and says so", () => {
  const pts = [["2026-07-01", 100], ["2026-08-30", 110]];
  const w = trendWindow(pts, "5y");
  assert.equal(w.start, 0);
  assert.equal(w.full, true);
  assert.equal(w.gain, 10);
});

test("trendWindow: non-positive baseline yields no percent", () => {
  const pts = [["2020-01-01", -50], ["2026-08-30", 100]];
  const w = trendWindow(pts, "all");
  assert.equal(w.gain, 150);
  assert.equal(w.pct, null);
});

// ---- counting months backwards must not depend on today's day ----
// Date#setUTCMonth keeps the day-of-month, so a naive shift off a 31st
// rolls forward into the following month and the window loses its oldest
// month. Cash Flow's "Saved, by month" is drawn from exactly this cutoff,
// and the card's heading comes from the SERVER's window for the same
// range key — so a client that disagrees prints a heading naming a month
// whose bar it never drew.
import { rangeCutoffMonth, shiftMonthsUtc } from "../src/lib/pure.ts";

// (day pinned, then the first month each range must include — the same
// months engine/reporting.py `_range_window` starts its window on)
const CUTOFFS = [
  ["a 31st, in a month the shift lands beside 30-day months",
   "2026-08-31", { "3m": "2026-06", "6m": "2026-03", "1y": "2025-09",
                   "3y": "2023-09", "5y": "2021-09" }],
  ["a 31st that crosses the year boundary",
   "2026-01-31", { "3m": "2025-11", "6m": "2025-08", "1y": "2025-02",
                   "3y": "2023-02", "5y": "2021-02" }],
  ["a 30th, which no February can hold",
   "2026-04-30", { "3m": "2026-02", "6m": "2025-11", "1y": "2025-05",
                   "3y": "2023-05", "5y": "2021-05" }],
  ["a leap-day 29th",
   "2024-02-29", { "3m": "2023-12", "6m": "2023-09", "1y": "2023-03",
                   "3y": "2021-03", "5y": "2019-03" }],
  ["an ordinary mid-month day",
   "2026-07-15", { "3m": "2026-05", "6m": "2026-02", "1y": "2025-08",
                   "3y": "2023-08", "5y": "2021-08" }],
];

for (const [what, today, expected] of CUTOFFS)
  test(`range cutoffs are the same months on ${what}`, () => {
    const now = new Date(today + "T12:00:00Z");
    for (const [range, first] of Object.entries(expected))
      assert.equal(rangeCutoffMonth(range, now), first,
                   `${range} on ${today}`);
    // "All" has no cutoff, on any day
    assert.equal(rangeCutoffMonth("all", now), null);
  });

test("a month shift clamps the day instead of rolling into the next month", () => {
  assert.equal(shiftMonthsUtc("2026-05-31", 3), "2026-02-28");
  assert.equal(shiftMonthsUtc("2024-05-31", 3), "2024-02-29");
  assert.equal(shiftMonthsUtc("2026-03-31", 1), "2026-02-28");
  assert.equal(shiftMonthsUtc("2026-08-31", 12), "2025-08-31");
  assert.equal(shiftMonthsUtc("2026-01-31", 3), "2025-10-31");
  assert.equal(shiftMonthsUtc("2026-07-15", 6), "2026-01-15");
});

test("trendWindow keeps the full window when the last point is a 31st", () => {
  // 3m back from 2026-05-31 is 2026-02-28: the February point is the
  // baseline. A rolled cutoff (2026-03-03) lands past the March point and
  // measures the gain from there — a window short by a month, quietly.
  const pts = [
    ["2026-02-28", 100], ["2026-03-01", 150], ["2026-05-31", 200],
  ];
  const w = trendWindow(pts, "3m");
  assert.equal(pts[w.start][0], "2026-02-28");
  assert.equal(w.gain, 100);
});

// ---- the plan's institution allowance: one predicate behind every
// add-a-bank door, so no screen can promise what the server refuses ----
import { institutionAllowance } from "../src/lib/pure.ts";

test("the add-a-bank door closes exactly at the cap", () => {
  assert.deepEqual(institutionAllowance(6, 5), { known: true, full: false });
  assert.deepEqual(institutionAllowance(6, 6), { known: true, full: true });
  // an over-cap household (its ceiling was lowered) is still full, not
  // handed a door the server would refuse after the bank is authorized
  assert.deepEqual(institutionAllowance(6, 7), { known: true, full: true });
  assert.deepEqual(institutionAllowance(2, 0), { known: true, full: false });
});

test("an unknown or absent cap leaves the door open and the figure unsaid",
     () => {
  // self-host reports no cap — genuinely uncapped, and the hub's "as many
  // as you like" is true there. An /api/me still in flight looks the same
  // and must not lock an owner out of connecting.
  for (const [cap, used] of [[null, 3], [undefined, 3], [6, undefined],
                             [undefined, undefined]])
    assert.deepEqual(institutionAllowance(cap, used),
                     { known: false, full: false },
                     `cap=${cap} used=${used}`);
});


// ---- the elevation sheet's proof: a recovery code stands in for ONE
// factor, so an account with a password sends both. Getting this wrong
// strands a person whose authenticator is lost: the sheet then offers no
// recovery route from the password form, and the shape it would send is
// one the server refuses. ----

const TOTP_ACCOUNT = { methods: ["password"], totp: true };
const PASSKEY_ONLY = { methods: ["passkey"], totp: false };
const BOTH = { methods: ["passkey", "password"], totp: true };
const typed = (o) => ({ password: "", code: "", recoveryCode: "", ...o });

test("a lost authenticator sends the recovery code beside the password",
     () => {
  assert.deepEqual(
    elevationProof("recovery", TOTP_ACCOUNT,
                   typed({ password: "pw", recoveryCode: " k7f2-9qtp-m3xz " })),
    { password: "pw", recovery_code: "k7f2-9qtp-m3xz" });
  // ...and on an account that also holds a passkey, the same pair
  assert.deepEqual(
    elevationProof("recovery", BOTH,
                   typed({ password: "pw", recoveryCode: "k7f2-9qtp" })),
    { password: "pw", recovery_code: "k7f2-9qtp" });
});

test("a bare recovery code is only the passkey-only account's proof", () => {
  assert.deepEqual(
    elevationProof("recovery", PASSKEY_ONLY,
                   typed({ recoveryCode: "k7f2-9qtp" })),
    { recovery_code: "k7f2-9qtp" });
  // the password half is missing on an account that has one: nothing to
  // send yet, rather than a shape the server answers password_required
  assert.equal(
    elevationProof("recovery", TOTP_ACCOUNT, typed({ recoveryCode: "k7f2" })),
    null);
});

test("nothing is sent until the recovery form is complete", () => {
  assert.equal(elevationProof("recovery", TOTP_ACCOUNT,
                              typed({ password: "pw" })), null);
  assert.equal(elevationProof("recovery", PASSKEY_ONLY,
                              typed({ recoveryCode: "   " })), null);
});

test("the ordinary password pair is unchanged", () => {
  assert.deepEqual(
    elevationProof("password", TOTP_ACCOUNT,
                   typed({ password: "pw", code: "123456" })),
    { password: "pw", totp_code: "123456" });
  // a TOTP account owes a full six digits before anything is sent
  assert.equal(elevationProof("password", TOTP_ACCOUNT,
                              typed({ password: "pw", code: "123" })), null);
  assert.deepEqual(
    elevationProof("password", { methods: ["password"], totp: false },
                   typed({ password: "pw" })),
    { password: "pw" });
});

// ---- the business wizard's resume point ----

import { bizWizardResumeStep } from "../src/lib/pure.ts";

const WIZ_STEPS = [{ key: "what" }, { key: "when" }, { key: "accounts" },
                   { key: "startup" }, { key: "done" }];

test("a fresh run opens at the first question", () => {
  assert.equal(bizWizardResumeStep(WIZ_STEPS, {}), 0);
});

test("a run interrupted between the two questions reopens at the first", () => {
  // The name and structure live in component state until the entity is
  // created at the end of the SECOND question, so a mark on the first
  // alone points at a step whose inputs are gone — and the field that
  // collects the name is a step behind it.
  assert.equal(bizWizardResumeStep(WIZ_STEPS, { what: "done" }), 0);
});

test("both questions answered reopens at the accounts step", () => {
  assert.equal(
    bizWizardResumeStep(WIZ_STEPS, { what: "done", when: "done" }), 2);
});

test("a skipped step counts as finished and does not reopen", () => {
  assert.equal(
    bizWizardResumeStep(WIZ_STEPS,
      { what: "done", when: "done", accounts: "skipped" }), 3);
});

test("a finished run has no step to reopen at", () => {
  assert.equal(
    bizWizardResumeStep(WIZ_STEPS,
      { what: "done", when: "done", accounts: "skipped",
        startup: "done" }), -1);
});

// ---- what the owner is told after minting a household invite ----

import { inviteOutcome } from "../src/lib/pure.ts";

test("a delivered-only invite names the mailbox it went to", () => {
  // Hosted withholds the URL — the mail IS the delivery. A card that
  // rendered the URL alone would draw nothing here, so a successful
  // invitation and a dead button would look identical.
  assert.deepEqual(
    inviteOutcome({ url: null, label: "alice@example.dev", emailed: true }),
    { kind: "emailed", label: "alice@example.dev" });
});

test("a self-host mint still hands back the copyable share link", () => {
  assert.deepEqual(
    inviteOutcome({ url: "https://home.lan/app/invite?token=t",
                    label: "the sitter", emailed: false }),
    { kind: "link", url: "https://home.lan/app/invite?token=t",
      emailed: false, label: "the sitter" });
});

test("a self-host mint that also mailed the link says both", () => {
  // Self-host with SMTP configured does both, and the confirmation has to
  // say so — otherwise the owner shares a link the invitee already has.
  assert.deepEqual(
    inviteOutcome({ url: "https://home.lan/app/invite?token=t",
                    label: "alice@example.dev", emailed: true }),
    { kind: "link", url: "https://home.lan/app/invite?token=t",
      emailed: true, label: "alice@example.dev" });
});

test("a missing url is never rendered as an empty link", () => {
  // An older server, or a field the client failed to read, must not put
  // the owner in front of a blank share box.
  assert.equal(inviteOutcome({ label: "alice@example.dev" }).kind, "emailed");
  assert.equal(inviteOutcome({ url: "", label: " alice@example.dev " }).kind,
               "emailed");
  assert.equal(inviteOutcome({ url: "", label: " alice@example.dev " }).label,
               "alice@example.dev");
});


// ---- the cash-flow window belongs to the household, not the device ----
// Every figure on the Cash Flow screen is windowed server-side from the
// HOUSEHOLD's date (localtime.now_local → reporting._range_window). The
// device's clock is in a different month for hours around every boundary
// whenever the household is not on UTC, so a cutoff derived from it draws
// the chart from one window while the numbers beside it come from another
// — and the best/worst caption is then computed over the short one.
import { cashflowCutoffMonth, graphCurrentMonth } from "../src/lib/pure.ts";

// A report built on 2026-08-31 for a household in America/Los_Angeles:
// history stops before August, the forecast tail opens on August. The
// real instant is 2026-09-01T01:00Z — the device is already in September.
const LA_AUG = {
  points: [["2026-05", 100], ["2026-06", 200], ["2026-07", 300],
           ["2026-08", 400], ["2026-09", 500]],
  forecast_from: 3,
};
const DEVICE_IN_SEPTEMBER = new Date("2026-09-01T01:00:00Z");

test("the cash-flow window is cut on the household's month, not the device's", () => {
  // The server's 3m window covers Jun+Jul+Aug, so June must survive the
  // filter. Cut on the device it would not: its cutoff is 2026-07.
  assert.equal(cashflowCutoffMonth(LA_AUG, "3m", DEVICE_IN_SEPTEMBER),
               "2026-06");
  assert.equal(cashflowCutoffMonth(LA_AUG, "1y", DEVICE_IN_SEPTEMBER),
               "2025-09");
  assert.equal(cashflowCutoffMonth(LA_AUG, "all", DEVICE_IN_SEPTEMBER), null);
  // and the household's month is read off the report, not computed
  assert.equal(graphCurrentMonth(LA_AUG), "2026-08");
});

test("the window keeps every month the household's own range covers", () => {
  const cutoff = cashflowCutoffMonth(LA_AUG, "3m", DEVICE_IN_SEPTEMBER);
  const history = LA_AUG.points
    .filter((p, i) => i < LA_AUG.forecast_from && p[0] >= cutoff)
    .map((p) => p[0]);
  assert.deepEqual(history, ["2026-06", "2026-07"]);
});

test("a report with no forecast tail falls back to the device clock", () => {
  // Nothing in the payload names the household's month then, so the
  // device's is all there is — but it must still be a window, not a crash.
  const noTail = { points: [["2026-05", 1], ["2026-06", 2]],
                   forecast_from: 2 };
  assert.equal(graphCurrentMonth(noTail), null);
  assert.equal(cashflowCutoffMonth(noTail, "3m", DEVICE_IN_SEPTEMBER),
               "2026-07");
});


// ---- removing an account refreshes what the removal freed ----
// The plan allowance ("6 of 6 institutions") rides /api/me, and the screen
// offering the removal is the screen showing the allowance. Without ["me"]
// in the refresh, a household disconnects a bank to make room and the
// Connect button stays disabled behind a count cached from before.
import { accountRemoveMoved } from "../src/lib/pure.ts";

test("a disconnect refreshes the institution allowance", () => {
  for (const mode of ["disconnect", "disconnect_purge", "purge"])
    assert.ok(accountRemoveMoved(mode).includes("me"),
              `${mode} must refresh /api/me`);
});

test("hiding an account moves nothing the server counts", () => {
  // A hidden account keeps its connection, so the allowance is unchanged
  // and asking for it again is a request that can only confirm itself.
  const keys = accountRemoveMoved("hide");
  assert.ok(!keys.includes("me"));
  assert.ok(!keys.includes("connections"));
  assert.deepEqual(keys, ["accounts", "today", "transactions"]);
});

test("only a purge disturbs the settings view and the bill calendar", () => {
  // A purge can take the checking anchor or an excluded account with it.
  for (const mode of ["purge", "disconnect_purge"]) {
    const keys = accountRemoveMoved(mode);
    assert.ok(keys.includes("settings"), mode);
    assert.ok(keys.includes("calendar"), mode);
  }
  assert.ok(!accountRemoveMoved("disconnect").includes("settings"));
  assert.ok(!accountRemoveMoved("disconnect").includes("calendar"));
});

test("an empty category filter shows every option, not none", () => {
  // catLabel spells an empty value as "Uncategorized"; passing the typed
  // text through it would turn an empty filter box into "only options that
  // contain the word Uncategorized" — i.e. the picker would open blank.
  const all = ["FOOD_AND_DRINK", "GENERAL_SERVICES", "TRANSFER_OUT", "?"];
  assert.deepEqual(filterCategories(all, ""), all);
  assert.deepEqual(filterCategories(all, "   "), all);
});

test("the category filter matches the display label, underscores as spaces", () => {
  const all = ["FOOD_AND_DRINK", "GENERAL_SERVICES", "TRANSFER_OUT"];
  assert.deepEqual(filterCategories(all, "food and"), ["FOOD_AND_DRINK"]);
  assert.deepEqual(filterCategories(all, "general_serv"), ["GENERAL_SERVICES"]);
  assert.deepEqual(filterCategories(all, "zzz"), []);
});

test("every category picker filters on the label, not the raw constant", () => {
  // The screens below render each option through catLabel — "GENERAL
  // SERVICES" — so matching the typed text against the underscored
  // constant would return nothing for what is plainly on screen. The
  // rule is that the typed text only ever reaches filterCategories; it is
  // checked here because these are screens, and the pure tests are the
  // only thing that runs over them.
  const pickers = {
    "src/app/txn.tsx": "filter",
    "src/app/bill-history.tsx": "filter",
    "src/app/rules.tsx": "catFilter",
    "src/app/(tabs)/transactions.tsx": "bulkFilter",
    "src/app/(tabs)/bills.tsx": "nCategory",
  };
  for (const [file, typed] of Object.entries(pickers)) {
    const src = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.ok(src.includes("filterCategories"),
              `${file}: the category picker must use filterCategories`);
    const raw = new RegExp(`\\.includes\\(\\s*[^)]*\\b${typed}\\b`);
    assert.ok(!raw.test(src),
              `${file}: ${typed} is matched with .includes() — the option `
              + "list is filtered on the raw category, so the label shown "
              + "cannot be typed to find it");
  }
});

// ---- assets: the Android notification glyph must clear every edge ----
//
// Android draws the small icon as a white silhouette clipped to the
// drawable's bounds, so ink touching an edge is cut off and a mark whose
// margins differ sits visibly off-centre. The asset is rendered from the
// canonical SVG; this guards the render, not the renderer. Node has no
// image library, so this decodes just enough PNG (8-bit RGBA, no
// interlace) to find the alpha bounding box.

function pngAlphaBbox(bytes) {
  const sig = [137, 80, 78, 71, 13, 10, 26, 10];
  assert.deepEqual([...bytes.subarray(0, 8)], sig, "not a PNG");
  let pos = 8, width = 0, height = 0, depth = 0, ctype = 0, interlace = 0;
  const idat = [];
  while (pos < bytes.length) {
    const len = bytes.readUInt32BE(pos);
    const type = bytes.toString("latin1", pos + 4, pos + 8);
    const data = bytes.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR") {
      width = data.readUInt32BE(0); height = data.readUInt32BE(4);
      depth = data[8]; ctype = data[9]; interlace = data[12];
    } else if (type === "IDAT") idat.push(data);
    pos += 12 + len;
  }
  assert.equal(depth, 8, "expected 8-bit channels");
  assert.equal(ctype, 6, "expected RGBA");
  assert.equal(interlace, 0, "expected a non-interlaced PNG");
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bpp = 4, stride = width * bpp;
  const px = Buffer.alloc(height * stride);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const f = raw[y * (stride + 1)];
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    const out = px.subarray(y * stride, (y + 1) * stride);
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? out[i - bpp] : 0, b = prev[i],
            c = i >= bpp ? prev[i - bpp] : 0;
      let v;
      if (f === 0) v = line[i];
      else if (f === 1) v = line[i] + a;
      else if (f === 2) v = line[i] + b;
      else if (f === 3) v = line[i] + ((a + b) >> 1);
      else {                                   // Paeth
        const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b),
              pc = Math.abs(p - c);
        v = line[i] + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c);
      }
      out[i] = v & 255;
    }
    prev = out;
  }
  let x0 = width, y0 = height, x1 = -1, y1 = -1;
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++)
      if (px[y * stride + x * bpp + 3] > 8) {
        if (x < x0) x0 = x; if (x > x1) x1 = x;
        if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
  return { width, height, x0, y0, x1, y1 };
}

test("the notification icon's ink clears every edge and sits centred", () => {
  const png = readFileSync(
    new URL("../assets/images/notification-icon.png", import.meta.url));
  const b = pngAlphaBbox(png);
  assert.ok(b.x1 >= 0, "the icon has no opaque pixels at all");
  const margins = { left: b.x0, top: b.y0,
                    right: b.width - 1 - b.x1, bottom: b.height - 1 - b.y1 };
  // 10% a side: the glyph must clear the drawable bounds on every side
  // (Android clips there), but a 20% margin read as a speck in the status
  // bar. The render is 12%, and the build regenerates the drawables.
  const min = Math.round(b.width * 0.10);
  for (const [side, m] of Object.entries(margins))
    assert.ok(m >= min, `${side} margin is ${m}px; needs at least ${min}px`);
  // even margins — a glyph pushed to one side reads as clipped even when
  // it technically is not
  const slack = Math.round(b.width * 0.02);
  assert.ok(Math.abs(margins.left - margins.right) <= slack,
            `off-centre horizontally: ${margins.left} vs ${margins.right}`);
  assert.ok(Math.abs(margins.top - margins.bottom) <= slack,
            `off-centre vertically: ${margins.top} vs ${margins.bottom}`);
});

// ---- the bug-report composer: three prompts, one message ----

test("a bug report folds its prompts into labelled sections, skipping empties", () => {
  assert.equal(composeBugReport("it broke", "a chart", "Spending"),
               "What happened:\nit broke\n\nWhat I expected:\na chart\n\n"
               + "Where:\nSpending");
  assert.equal(composeBugReport(" it broke ", "", "  "),
               "What happened:\nit broke");
  assert.equal(composeBugReport("", "", ""), "");
});

test("the mobile bug-report composer is the web page's, word for word", () => {
  const web = readFileSync(
    new URL("../../webapp/src/pages/Feedback.tsx", import.meta.url), "utf8");
  const mob = readFileSync(new URL("../src/lib/pure.ts", import.meta.url), "utf8");
  const body = (src) => {
    const i = src.indexOf("export function composeBugReport");
    const j = src.indexOf("\n}\n", i);
    return src.slice(i, j);
  };
  assert.equal(body(mob), body(web));
});

// ---- the ledger's quick date ranges and the picker's month grid ----
import { monthGrid } from "../src/lib/dates.ts";
import { ledgerRange } from "../src/lib/pure.ts";

test("ledgerRange opens on the first of the window's opening month, open-ended", () => {
  const today = new Date(2026, 8, 8); // Sep 8 2026
  // the same three calendar months Cash Flow's 3m covers: Jul, Aug, Sep
  assert.deepEqual(ledgerRange("3m", today), { from: "2026-07-01", to: "" });
  assert.deepEqual(ledgerRange("6m", today), { from: "2026-04-01", to: "" });
  assert.deepEqual(ledgerRange("1y", today), { from: "2025-10-01", to: "" });
  assert.deepEqual(ledgerRange("3y", today), { from: "2023-10-01", to: "" });
  assert.deepEqual(ledgerRange("5y", today), { from: "2021-10-01", to: "" });
  assert.deepEqual(ledgerRange("all", today), { from: "", to: "" });
});

test("ledgerRange crosses the year boundary and ignores the day of month", () => {
  // Jan 31: a 31st shifted back by months must not roll forward
  assert.deepEqual(ledgerRange("3m", new Date(2026, 0, 31)),
                   { from: "2025-11-01", to: "" });
  assert.deepEqual(ledgerRange("6m", new Date(2026, 2, 1)),
                   { from: "2025-10-01", to: "" });
});

test("monthGrid is six Monday-first weeks padded with neighbours", () => {
  const g = monthGrid(2026, 9); // September 2026 starts on a Tuesday
  assert.equal(g.length, 6);
  assert.ok(g.every((w) => w.length === 7));
  assert.deepEqual(g[0][0], { date: "2026-08-31", inMonth: false });
  assert.deepEqual(g[0][1], { date: "2026-09-01", inMonth: true });
  assert.equal(g[4][2].date, "2026-09-30");
  assert.equal(g[4][3].inMonth, false);
  // a month starting on Monday has no leading padding
  assert.deepEqual(monthGrid(2026, 6)[0][0], { date: "2026-06-01", inMonth: true });
});

// ---- the daily verdict's delivery choice round-trips its two flags ----
import { deliveryOf, flagsOf } from "../src/lib/pure.ts";

test("delivery reads the email and push flags as one choice and back", () => {
  for (const d of ["email", "push", "both", "off"]) {
    const f = flagsOf(d);
    assert.equal(deliveryOf(f.on, f.push), d);
  }
  assert.deepEqual(flagsOf("push"), { on: false, push: true });
  assert.deepEqual(flagsOf("off"), { on: false, push: false });
  // the legacy default — no schedule saved — is email on, push off
  assert.equal(deliveryOf(true, false), "email");
});
