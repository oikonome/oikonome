// Unit tests for src/lib/pure.ts — mobile has no JS test framework, so
// these run under plain node (v22.6+):
//
//   node --experimental-strip-types scripts/pure-logic-tests.mjs
//
// The server suite invokes this too (tests/test_mobile_budget_shape.py),
// so `make test` goes red if the banker's rounding or the savings-goals
// merge regresses.
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import zlib from "node:zlib";
import test from "node:test";

import { ApiError, busyText, countsInTotals, downloadRefusal, errText,
         inOperatingProfit, signInErrText, whatIfRefusal }
  from "../src/lib/api.ts";
import { alertSections, withLiveLinks, historyToggleLabel, accountTheme, themeFollowsHousehold, billToggleNote, categoryForServer, CLEAR_CATEGORY, composeBugReport,
         filterCategories, dominantCategory, dayLabel,
         elevationProof, recoveryNeedsPassword, hostOf,
         hueOf, isPlanOnlySavingsGoal, isPlanSavingsGoal, isPrivateHost, isValidYmd, logoProxyUrl,
         mergeIncomeScenarios, monogram,
         monthYear, serverUrlVerdict, lastSyncPhrase, mergeSavingsGoals,
         money, moneyParam, numv, pyround, resolveFace, span, trendRangeOf, whatIfQuery,
         withDayHeads, SIGN_IN_AGAIN, twoFactorRecheck }
  from "../src/lib/pure.ts";
import { LINK_POLL_FAST_MS, LINK_POLL_MS, linkPollWait, pendingLinkLine }
  from "../src/lib/pure.ts";

import * as pureModule from "../src/lib/pure.ts";
import { FIRST_SYNC_RUNNING, firstSyncPending, linkedLine }
  from "../src/lib/pure.ts";
import { moneyCases } from "./money-cases.mjs";
import { accountsSummary, adjustOf, billLine, billsSummary, budgetsSummary,
         clockHour, deliverySummary, dueDayOf, finishBlurb, HISTORY_CHOICES, reachLine,
         historyMonthsOf, historyPhrase, MANUAL_CADENCES, manualTally,
         nextDueForDay, ordinal, parseDueDay, proposalCadenceValue,
         recipientRole, WIZARD_STEPS, wizardThemeChoice, cadenceTakesDueDay,
         latestWins, proposalAdjustable, keptBudget, manualBudgetsBody,
         manualBudgetsPrefill, manualBudgetsRefusal, billsContinue,
         checklistOpen, checklistSteps, checklistSummary, connectDoneHint,
         historyRowNote, windowNarrower, planSaved, budgetsStepKept }
  from "../src/components/wizard-logic.ts";

import { clockLabel, glanceFace, STALE_AFTER_MS, wideEnoughForChips,
         widgetOwner, widgetOwnerChanged, credentialStamp, ownedCache,
         saveStillOwned, shouldRefreshWidget,
         WIDGET_REFRESH_MIN_MS } from "../src/lib/glance-pure.ts";

// ---- the home-screen widget's face: the hero's simple face, decided
// from the payload alone; stale beats wrong, and no credential is
// "Sign in", not a zero ----
const glance = (over = {}) => ({
  date: "2026-01-15", budgets_set: true, verdict: "ON BUDGET",
  left_today: 47, day_spent: 29, day_allow: 76, days_left: 16,
  pace_line: "$12/day ahead",
  chips: [{ text: "Groceries $22 today", tone: "" },
          { text: "Gas over by $8", tone: "neg" }],
  as_of: "2026-01-15T09:40:00Z", ...over });
const at = (ms) => ({ glance: glance(), fetched_at: ms });

test("second-factor lockout: a phone signed out by a web enrolment is told to sign in again", () => {
  assert.equal(twoFactorRecheck({ errorStatus: 401 }), "sign-in-again");
  assert.equal(SIGN_IN_AGAIN, "Sign in again to continue");
});

test("second-factor lockout: still required sends the person to set it up on this phone", () => {
  assert.equal(twoFactorRecheck({ me: { needs_2fa: true } }), "set-up-here");
});

test("second-factor lockout: a factor already on clears the lockout", () => {
  assert.equal(twoFactorRecheck({ me: { needs_2fa: false } }), "cleared");
  assert.equal(twoFactorRecheck({ me: {} }), "cleared");
});

test("second-factor lockout: an unreachable server is neither a sign-out nor a clear", () => {
  assert.equal(twoFactorRecheck({ errorStatus: 503 }), "unreachable");
  assert.equal(twoFactorRecheck({ errorStatus: 0 }), "unreachable");
  assert.equal(twoFactorRecheck({ me: null }), "unreachable");
});

test("widget: no credential is Sign in, no plan is Set a plan", () => {
  assert.equal(glanceFace(at(0), false, 0).kind, "signin");
  assert.equal(glanceFace({ glance: glance({ budgets_set: false }),
                            fetched_at: 0 }, true, 0).kind, "noplan");
});

test("widget: the number is the hero's headline in whole dollars", () => {
  const f = glanceFace(at(1000), true, 1000);
  assert.equal(f.kind, "verdict");
  assert.equal(f.number, "$47");
  assert.equal(f.verdict, "On budget");
  assert.equal(f.tone, "good");
  assert.equal(f.sub, "$29 of $76 spent");
  assert.equal(f.pace, "$12/day ahead");
  assert.ok(Math.abs(f.fill - 29 / 76) < 1e-9);
  assert.equal(f.over, false);
  assert.equal(f.stale, false);
  assert.deepEqual(f.chips, [{ text: "Groceries $22 today", neg: false },
                             { text: "Gas over by $8", neg: true }]);
});

test("widget: over is red, the bar is full and the sub-line says by how much", () => {
  const f = glanceFace({ glance: glance({ verdict: "OVER BUDGET",
    left_today: 0, day_spent: 94, day_allow: 76, days_left: 9 }),
    fetched_at: 5 }, true, 5);
  assert.equal(f.verdict, "Over budget");
  assert.equal(f.tone, "bad");
  assert.equal(f.fill, 1);
  assert.equal(f.over, true);
  assert.equal(f.sub, "$18 over · 9 days left");
  assert.equal(f.number, "$0");
});

test("widget: an hour-old payload is stale, a minute-old one is not", () => {
  const now = 10_000_000;
  assert.equal(glanceFace(at(now - STALE_AFTER_MS - 1), true, now).stale, true);
  assert.equal(glanceFace(at(now - 60_000), true, now).stale, false);
  // a credential with nothing cached yet is a placeholder, never Sign in
  const empty = glanceFace(null, true, now);
  assert.equal(empty.kind, "verdict");
  assert.equal(empty.number, "…");
  assert.equal(empty.stale, true);
});

test("widget: yesterday's allowance is stale after midnight, however recent the fetch", () => {
  // fetched at 11:45 PM saying $0 left; at 12:30 AM a new day's allowance
  // has arrived, so the old verdict must not read as current
  const lateNight = new Date(2026, 0, 15, 23, 45).getTime();
  const afterMidnight = new Date(2026, 0, 16, 0, 30).getTime();
  const sameNight = new Date(2026, 0, 15, 23, 59).getTime();
  const spent = { glance: glance({ left_today: 0, day_spent: 76 }),
                  fetched_at: lateNight };
  assert.ok(afterMidnight - lateNight < STALE_AFTER_MS);
  assert.equal(glanceFace(spent, true, afterMidnight).stale, true);
  assert.equal(glanceFace(spent, true, sameNight).stale, false);
  // a morning fetch read mid-morning is the same day and fresh
  const morning = new Date(2026, 0, 16, 7, 0).getTime();
  assert.equal(glanceFace(at(morning), true, morning + 30 * 60_000).stale,
               false);
});

test("widget: thousands are grouped, the clock reads 12-hour", () => {
  const f = glanceFace({ glance: glance({ left_today: 1234, day_allow: 2000,
    day_spent: 766 }), fetched_at: 0 }, true, 0);
  assert.equal(f.number, "$1,234");
  assert.equal(f.sub, "$766 of $2,000 spent");
  const noon = new Date(2026, 0, 15, 12, 5).getTime();
  assert.equal(clockLabel(noon), "12:05 PM");
  assert.equal(clockLabel(new Date(2026, 0, 15, 0, 7).getTime()), "12:07 AM");
  assert.equal(clockLabel(new Date(2026, 0, 15, 7, 15).getTime()), "7:15 AM");
  assert.equal(wideEnoughForChips(160), false);
  assert.equal(wideEnoughForChips(320), true);
});

// ---- the debt planner's labels: a month first is a month, not a day;
// a duration reads in years and months ----

test("a payoff month is named without its day", () => {
  assert.equal(monthYear("2029-03-01"), "Mar 2029");
  assert.equal(monthYear("2026-12-01"), "Dec 2026");
  assert.equal(monthYear(null), "");
  assert.equal(monthYear(""), "");
});

test("a plan's length reads in years and months", () => {
  assert.equal(span(29), "2 yr 5 mo");
  assert.equal(span(24), "2 yr");
  assert.equal(span(7), "7 mo");
  assert.equal(span(0), "today");
  assert.equal(span(null), "not within fifty years");
});

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

test("money() of a figure not known prints the unknown mark, never $0", () => {
  // the web's money(null) prints the same mark
  assert.equal(money(null), "·");
  assert.equal(money(undefined, false), "·");
  assert.equal(money(0), "$0.00");
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

test("a goal added and saved here, then removed, stays removed", () => {
  // the card re-bases on the saved view after every save; with the
  // mount-time base kept, the saved goal looked "added elsewhere" and the
  // second save carried the removed goal back
  const first = mergeSavingsGoals(
    [draft("Emergency"), draft("Car", { touched: true })],
    [goal("Emergency")], ["Emergency"]);
  assert.deepEqual(names(first), ["Car", "Emergency"]);
  const base = first.map((g) => g.name);      // the saved view
  const second = mergeSavingsGoals(
    [draft("Emergency")],                     // the Car row removed
    first, base);
  assert.deepEqual(names(second), ["Emergency"]);
  // and against the stale mount-time base it would have come back
  assert.deepEqual(names(mergeSavingsGoals([draft("Emergency")], first,
                                           ["Emergency"])),
                   ["Car", "Emergency"]);
});

test("a goal amount that is not an amount refuses the save, not $0", () => {
  for (const field of ["target", "monthly_plan", "start_balance"])
    assert.throws(() => mergeSavingsGoals(
      [draft("Trip", { touched: true, [field]: "1.25,5" })], [], []),
      /Trip [a-z ]+: "1\.25,5" is not an amount/, field);
  // blank still reads as 0, and an untouched row is not re-read at all
  assert.equal(mergeSavingsGoals(
    [draft("Trip", { touched: true, start_balance: "" })], [], [])[0]
    .start_balance, 0);
  assert.equal(mergeSavingsGoals(
    [draft("Trip", { target: "abc" })], [goal("Trip")], ["Trip"])[0]
    .target, 100);
});

// ---- Budget screen: every draft edit marks its card unsaved, and only
// the card itself clears the mark. A card left "clean" while it holds
// edits is remounted by the next sibling save, wiping them. Read from
// source: the marks sit inside press handlers no test can call. ----
test("budget cards mark every edit unsaved and clear only their own", () => {
  const src = readFileSync(new URL("../src/app/budget.tsx", import.meta.url),
                           "utf8");
  // the screen's refresh after a save remounts siblings; it must not
  // decide that the saved card has nothing typed in it
  const refresh = src.slice(src.indexOf("const refresh = (saved"),
                            src.indexOf("const tweakBits"));
  assert.ok(refresh.length > 0, "refresh not found");
  assert.ok(!/dirty\.current\[saved\]\s*=\s*false/.test(refresh)
            && !refresh.includes("setPlanDirty(false)"),
            "refresh clears the saved card's unsaved flag");
  // each card clears its own flag on a save that landed with nothing typed
  for (const msg of ["Plan saved.", "Bucket rules saved.",
                     "Savings goals saved."]) {
    const i = src.indexOf(`setMsg("${msg}")`);
    assert.ok(i >= 0, msg);
    assert.match(src.slice(i, i + 80), /if \(!edited\) onDirty\?\.\(false\)/,
                 `${msg}: the card does not clear its own unsaved flag`);
  }
  // the edits that add, remove or retype a row mark the card unsaved
  const marks = (anchor, before) => {
    const i = src.indexOf(anchor);
    assert.ok(i >= 0, `anchor ${anchor} not found`);
    assert.ok(src.slice(Math.max(0, i - before), i).includes("onDirty?.(true)"),
              `${anchor}: the edit does not mark the card unsaved`);
  };
  marks("setUnallocEdited(v)", 80);            // Everything else
  marks("+ add category", 220);
  marks("+ Add goal", 320);
  const remove = src.indexOf('{ color: C.bad }]}>remove</Text>');
  assert.ok(remove >= 0, "bucket remove not found");
  assert.ok(src.slice(remove - 260, remove).includes("onDirty?.(true)"),
            "removing a bucket does not mark the card unsaved");
});

test("a range handed to Spending opens on that window, else the default",
     () => {
  assert.equal(trendRangeOf("3m"), "3m");
  assert.equal(trendRangeOf("all"), "all");
  assert.equal(trendRangeOf(undefined), "1y");
  assert.equal(trendRangeOf("2y"), "1y");
  assert.equal(trendRangeOf(["3m"]), "1y");
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
  for (const h of ["localhost", "127.0.0.1", "10.1.2.3", "192.168.1.20",
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
  assert.equal(hostOf("http://192.168.1.5:8042/api"), "192.168.1.5");
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
// server's whole-day spend it carries (mirrors the web's TxnTable dayGroups) ----

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

test("a day cut short prints the server's whole-day spend, not the rows so far", () => {
  // a day of 12 rows worth $340, only 8 loaded: the divider reads $340
  const rows = Array.from({ length: 8 }, (_, i) =>
    tx(`r${i}`, "2026-08-13", 26.25));
  const [head] = withDayHeads(rows, { "2026-08-13": 340, "2026-08-12": 9 });
  assert.equal(head.count, 8);
  assert.equal(head.spent, 340);
});

test("each day reads its own figure, and nothing is added up on the phone", () => {
  // the amounts would sum to 1,000; the server says 60 (a transfer, money
  // in, a partly reimbursed charge) — the phone prints the server's
  const out = withDayHeads([
    tx("a", "2026-08-10", 100, "MEDICAL", true),
    tx("b", "2026-08-10", 900, "TRANSFER OUT", false),
    tx("c", "2026-08-09", 5),
  ], { "2026-08-10": 60 }).filter((it) => "kind" in it);
  assert.deepEqual(out.map((h) => h.spent), [60, 0]);
  assert.equal(typeof pureModule.daySpend, "undefined");
  assert.equal(typeof pureModule.rowSpend, "undefined");
});

test("an older server without day_spend: the header carries no amount", () => {
  const [head] = withDayHeads([tx("a", "2026-08-10", 236.44, "MEDICAL", true)]);
  assert.equal(head.spent, 0);
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

// ---- the plan's institution allowance: one predicate behind every
// add-a-bank door, so no screen can promise what the server refuses ----
import { institutionAllowance } from "../src/lib/pure.ts";

test("the add-a-bank door closes exactly at the cap", () => {
  assert.deepEqual(institutionAllowance(3, 2), { known: true, full: false });
  assert.deepEqual(institutionAllowance(3, 3), { known: true, full: true });
  // an over-cap household (its ceiling was lowered) is still full, not
  // handed a door the server would refuse after the bank is authorized
  assert.deepEqual(institutionAllowance(3, 4), { known: true, full: true });
  assert.deepEqual(institutionAllowance(2, 0), { known: true, full: false });
});

import { awaitsEmailConfirm, verifyNagDue } from "../src/lib/pure.ts";

test("the freeze warning waits out a new account's first three days", () => {
  const now = Date.parse("2026-10-10T12:00:00Z");
  assert.equal(verifyNagDue("2026-10-10T11:59:00Z", null, now), false);
  assert.equal(verifyNagDue("2026-10-07T12:01:00Z", null, now), false);
  assert.equal(verifyNagDue("2026-10-07T12:00:00Z", null, now), true);
  // an age nobody can read fails toward saying it
  assert.equal(verifyNagDue(null, null, now), true);
  assert.equal(verifyNagDue(undefined, undefined, now), true);
  assert.equal(verifyNagDue("not a date", null, now), true);
  // the deadline is the household's: a member who joined yesterday is
  // told once the freeze is within a week, and not while it is far off
  assert.equal(verifyNagDue("2026-10-09T12:00:00Z", "2026-10-12", now), true);
  assert.equal(verifyNagDue("2026-10-09T12:00:00Z", "2026-11-08", now), false);
});

test("only a hosted, explicitly unconfirmed address closes the bank door",
     () => {
  assert.equal(awaitsEmailConfirm({ hosted: true, verified: false }), true);
  assert.equal(awaitsEmailConfirm({ hosted: true, verified: true }), false);
  // an older server that omits the flag, self-host, and a /api/me still
  // in flight must never lock an owner out of connecting
  assert.equal(awaitsEmailConfirm({ hosted: true }), false);
  assert.equal(awaitsEmailConfirm({ hosted: false, verified: false }), false);
  assert.equal(awaitsEmailConfirm(undefined), false);
});

import { bankAwaitsConfirm } from "../src/lib/pure.ts";

test("the server's word on waiting for confirmation closes the bank door",
     () => {
  // a paying household whose owner has not confirmed may still connect:
  // the server says so, and an unconfirmed address must not overrule it
  assert.equal(bankAwaitsConfirm({ hosted: true, verified: false,
    features: { bank_awaits_confirm: false } }), false);
  assert.equal(bankAwaitsConfirm({ hosted: true, verified: false,
    features: { bank_awaits_confirm: true } }), true);
  // an older server that does not say falls back to "hosted, unconfirmed"
  assert.equal(bankAwaitsConfirm({ hosted: true, verified: false,
    features: {} }), true);
  assert.equal(bankAwaitsConfirm({ hosted: true, verified: true }), false);
  assert.equal(bankAwaitsConfirm(undefined), false);
});

test("an unknown or absent cap leaves the door open and the figure unsaid",
     () => {
  // self-host reports no cap — genuinely uncapped, and the hub's "as many
  // as you like" is true there. An /api/me still in flight looks the same
  // and must not lock an owner out of connecting.
  for (const [cap, used] of [[null, 3], [undefined, 3], [3, undefined],
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
const HOSTED_PASSKEY_ONLY = { methods: ["passkey", "recovery+password"],
                              totp: false };
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

test("a hosted passkey-only account sends the password with the code",
     () => {
  // the server refuses a bare code there: the code stands in for the
  // passkey, and the password is still owed
  assert.equal(
    elevationProof("recovery", HOSTED_PASSKEY_ONLY,
                   typed({ recoveryCode: "k7f2-9qtp" })),
    null);
  assert.deepEqual(
    elevationProof("recovery", HOSTED_PASSKEY_ONLY,
                   typed({ password: "pw", recoveryCode: " k7f2-9qtp " })),
    { password: "pw", recovery_code: "k7f2-9qtp" });
  assert.equal(recoveryNeedsPassword(HOSTED_PASSKEY_ONLY), true);
  assert.equal(recoveryNeedsPassword(PASSKEY_ONLY), false);
  assert.equal(recoveryNeedsPassword(null), false);
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

// ---- the business wizard and the Business page's own arithmetic ----
//
// The web keeps the same rules in webapp/src/bizmath.ts; both copies run
// every case, so the phone and the browser never open the wizard on a
// different business or show a business a different number.

import * as mobileBiz from "../src/lib/pure.ts";
import * as webBiz from "../../webapp/src/bizmath.ts";

const WIZ_STEPS = [{ key: "what" }, { key: "when" }, { key: "accounts" },
                   { key: "startup" }, { key: "done" }];
const BIZ_SIDES = [["mobile", mobileBiz], ["web", webBiz]];

for (const [side, m] of BIZ_SIDES) {
  test(`${side}: a run with no business opens at the first question`, () => {
    // Step marks are household-wide and outlive the business they were
    // written for, and every step after the questions writes against the
    // entity — opened with none, "Assign" would send accounts back to
    // personal and report success.
    assert.equal(m.bizWizardEntryStep(WIZ_STEPS, false), 0);
  });

  test(`${side}: a run continuing a business opens at its accounts step`, () => {
    assert.equal(m.bizWizardEntryStep(WIZ_STEPS, true), 2);
  });

  const ents = [
    { id: "e-closed", status: "archived" },
    { id: "e-first", status: "active" },
    { id: "e-second", status: "active" },
  ];

  test(`${side}: guided setup continues the business the link names`, () => {
    assert.equal(m.bizSetupEntity(ents, "e-second")?.id, "e-second");
  });

  test(`${side}: a bare setup link continues the oldest ACTIVE business`, () => {
    // never a closed one, and never a second business next to one that
    // exists
    assert.equal(m.bizSetupEntity(ents, null)?.id, "e-first");
    assert.equal(m.bizSetupEntity(ents, "e-closed")?.id, "e-first");
    assert.equal(m.bizSetupEntity(ents, "e-gone")?.id, "e-first");
  });

  test(`${side}: with only closed businesses, setup starts a new one`, () => {
    assert.equal(m.bizSetupEntity([ents[0]], null), undefined);
    assert.equal(m.bizSetupEntity([], null), undefined);
  });

  test(`${side}: the tax year list reaches back to the business's first year`, () => {
    const feb = new Date(2031, 1, 10);
    // started two years ago, formed the year before that
    assert.deepEqual(m.bizTaxYears(feb, "2028-11-02", "2029-03-01"),
                     [2031, 2030, 2029, 2028]);
    // a long-running business stops at six years
    assert.deepEqual(m.bizTaxYears(feb, "2001-01-01", null),
                     [2031, 2030, 2029, 2028, 2027, 2026]);
    // no dates known: the P&L's own three years
    assert.deepEqual(m.bizTaxYears(feb, null, ""), [2031, 2030, 2029]);
    // a business that began this year has only this year
    assert.deepEqual(m.bizTaxYears(feb, null, "2031-01-05"), [2031]);
  });

  test(`${side}: filing season opens the tax controls on last year`, () => {
    const years = [2031, 2030, 2029];
    assert.equal(m.bizDefaultTaxYear(new Date(2031, 0, 20), years), 2030);
    assert.equal(m.bizDefaultTaxYear(new Date(2031, 3, 30), years), 2030);
    assert.equal(m.bizDefaultTaxYear(new Date(2031, 4, 1), years), 2031);
    // no last year to file for: this year
    assert.equal(m.bizDefaultTaxYear(new Date(2031, 1, 1), [2031]), 2031);
  });

  test(`${side}: the Books control shows the bucket the P&L counts`, () => {
    // a cost dated before the business started counts as start-up; shown
    // as Operating, choosing Operating would send nothing
    assert.equal(m.bizShownBucket({ bucket: null,
                                    effective_bucket: "startup_195" }),
                 "startup_195");
    assert.equal(m.bizShownBucket({ bucket: "capital",
                                    effective_bucket: "capital" }), "capital");
    // an older server sends no effective bucket; money that moved (an
    // owner transfer, an owner's tax payment) is in no bucket, and shows
    // none rather than a guessed "operating"
    assert.equal(m.bizShownBucket({ bucket: "organizational" }),
                 "organizational");
    assert.equal(m.bizShownBucket({ bucket: null, effective_bucket: null }),
                 "");
  });

  test(`${side}: a bucketed cost with no line is offered one`, () => {
    const r = (bucket, line, amount = 40) =>
      ({ amount, bucket, sched_c_line: line });
    assert.equal(m.bizNeedsLine(r("operating", null)), true);
    assert.equal(m.bizNeedsLine(r("operating", "Supplies")), false);
    // capitalized costs are not filed on a line; an unclassified row is
    // the review queue's; money in is not a cost
    assert.equal(m.bizNeedsLine(r("startup_195", null)), false);
    assert.equal(m.bizNeedsLine(r(null, null)), false);
    assert.equal(m.bizNeedsLine(r("operating", null, -40)), false);
  });

  test(`${side}: a number save sends only the field that changed`, () => {
    const f = (rate, sqft) => ({
      income_tax_rate: { draft: rate, loaded: "22", label: "tax rate" },
      home_office_sqft: { draft: sqft, loaded: "150",
                          label: "home office size" } });
    // the size was not touched here: a copy from load time must not go out
    assert.deepEqual(m.numberFieldsPatch(f("24", "150")),
                     { income_tax_rate: 24 });
    assert.deepEqual(m.numberFieldsPatch(f("22", "")),
                     { home_office_sqft: null });
    assert.deepEqual(m.numberFieldsPatch(f(" 22 ", "150")), {});
    // a typo is refused rather than sent as null, which clears the value
    assert.throws(() => m.numberFieldsPatch(f("7,5", "150")),
                  /tax rate is not a number/);
    assert.equal(m.numberDraft("7%"), undefined);
    assert.equal(m.numberDraft("1,200"), 1200);
  });

  test(`${side}: only a missing route hides the member controls`, () => {
    assert.equal(m.bizMemberDoorMissing(405, "Method Not Allowed"), true);
    assert.equal(m.bizMemberDoorMissing(404, "Not Found"), true);
    // the member was removed elsewhere: the door exists
    assert.equal(m.bizMemberDoorMissing(404, "no such member"), false);
    assert.equal(m.bizMemberDoorMissing(403, "owner only"), false);
    assert.equal(m.bizMemberDoorMissing(undefined, undefined), false);
  });
}

test("the two copies of the business rules are the same text", () => {
  const web = readFileSync(
    new URL("../../webapp/src/bizmath.ts", import.meta.url), "utf8");
  const mob = readFileSync(new URL("../src/lib/pure.ts", import.meta.url), "utf8");
  for (const fn of ["bizSetupEntity", "bizWizardEntryStep",
                    "bizTaxYears", "bizDefaultTaxYear",
                    "bizShownBucket", "bizNeedsLine", "numberDraft",
                    "numberFieldsPatch",
                    "bizMemberDoorMissing"]) {
    const body = (src) => {
      const i = src.indexOf(`export function ${fn}`);
      return src.slice(i, src.indexOf("\n}\n", i));
    };
    assert.notEqual(body(web).length, 0);
    assert.equal(body(mob), body(web), fn);
  }
});

test("a hidden account's kept balance is not business cash", () => {
  // hiding a manual account keeps its last balance (no feed restores it
  // on unhide), so a sum that checked only the link counted money the
  // owner switched off
  const hidden = { balance_current: 700, link: null,
                   user_removed_at: "2031-05-01T00:00:00Z" };
  for (const counts of [countsInTotals, webBiz.countsInTotals]) {
    assert.equal(counts(hidden), false);
    assert.equal(counts({ balance_current: 700, link: null }), true);
    assert.equal(counts({ balance_current: 700,
      link: { group_id: "g", home_rank: 2, primary: false, healthy: true } }),
      false);
  }
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
  // The server's 3m window is the three COMPLETE months before August —
  // May+Jun+Jul — so May must survive the filter. Cut on the device it
  // would not: its cutoff is 2026-06.
  assert.equal(cashflowCutoffMonth(LA_AUG, "3m", DEVICE_IN_SEPTEMBER),
               "2026-05");
  assert.equal(cashflowCutoffMonth(LA_AUG, "1y", DEVICE_IN_SEPTEMBER),
               "2025-08");
  assert.equal(cashflowCutoffMonth(LA_AUG, "all", DEVICE_IN_SEPTEMBER), null);
  // and the household's month is read off the report, not computed
  assert.equal(graphCurrentMonth(LA_AUG), "2026-08");
});

test("the window keeps every month the household's own range covers", () => {
  const cutoff = cashflowCutoffMonth(LA_AUG, "3m", DEVICE_IN_SEPTEMBER);
  const history = LA_AUG.points
    .filter((p, i) => i < LA_AUG.forecast_from && p[0] >= cutoff)
    .map((p) => p[0]);
  assert.deepEqual(history, ["2026-05", "2026-06", "2026-07"]);
});

test("every cash-flow range holds as many closed months as the server's window", () => {
  // reporting._range_window: every key but "cur" is N complete months
  // ending last month; "cur" is the month in progress, with no closed
  // month in the chart's history at all. A household in October, with
  // history from Oct 2020 through September.
  const keys = [];
  for (let y = 2020; y <= 2026; y++) {
    for (let m = 1; m <= 12; m++) {
      const k = `${y}-${String(m).padStart(2, "0")}`;
      if (k >= "2020-10" && k <= "2026-11") keys.push(k);
    }
  }
  const graph = { points: keys.map((k) => [k, 1]),
                  forecast_from: keys.indexOf("2026-10") };
  const closed = (range) => {
    const cutoff = cashflowCutoffMonth(graph, range) ?? "";
    return graph.points.filter((p, i) => i < graph.forecast_from
                               && p[0] >= cutoff).map((p) => p[0]);
  };
  assert.deepEqual(closed("cur"), []);
  assert.deepEqual(closed("1m"), ["2026-09"]);
  assert.deepEqual(closed("3m"), ["2026-07", "2026-08", "2026-09"]);
  for (const [range, n] of [["6m", 6], ["1y", 12], ["3y", 36], ["5y", 60]]) {
    const got = closed(range);
    assert.equal(got.length, n, range);
    assert.equal(got.at(-1), "2026-09", range);
  }
  assert.equal(closed("1y")[0], "2025-10");
});

test("a report with no forecast tail falls back to the device clock", () => {
  // Nothing in the payload names the household's month then, so the
  // device's is all there is — but it must still be a window, not a crash.
  const noTail = { points: [["2026-05", 1], ["2026-06", 2]],
                   forecast_from: 2 };
  assert.equal(graphCurrentMonth(noTail), null);
  assert.equal(cashflowCutoffMonth(noTail, "3m", DEVICE_IN_SEPTEMBER),
               "2026-06");
});

// ---- the month bars end where the server's window ends ----
// The server names the months each range draws; the phone works out no
// window of its own. A range of complete months carries neither the month
// in progress nor the forecast.
import { graphWindow } from "../src/lib/pure.ts";

test("the chart draws the months the server's window names", () => {
  const graph = { ...LA_AUG, windows: {
    "3m": { first: "2026-05", last: "2026-07", note: "closed words" },
    cur: { first: "2026-08", last: "2026-09", note: "live words" } } };
  const drawn = (range) => {
    const w = graphWindow(graph, range);
    return graph.points.filter((p) => p[0] >= w.first && p[0] <= w.last)
      .map((p) => p[0]);
  };
  // August (in progress) and September (forecast) are not after a window
  // that ended in July
  assert.deepEqual(drawn("3m"), ["2026-05", "2026-06", "2026-07"]);
  assert.deepEqual(drawn("cur"), ["2026-08", "2026-09"]);
  assert.equal(graphWindow(graph, "3m").note, "closed words");
});

test("a server that names no window gets the cut its own phones made", () => {
  const w = graphWindow(LA_AUG, "3m");
  assert.equal(w.first, "2026-05");
  // that server's contract: the tail always follows, and it sent no line
  assert.ok(LA_AUG.points.every((p) => p[0] <= w.last));
  assert.equal(w.note, "");
  assert.equal(graphWindow(LA_AUG, "all").first, "");
});

// ---- removing an account refreshes what the removal freed ----
// The plan allowance ("3 of 3 institutions") rides /api/me, and the screen
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
  assert.deepEqual(keys, ["accounts", "today", "transactions", "holdings"]);
});

test("every removal refreshes the top-holdings list", () => {
  // /holdings leaves out hidden and removed accounts; without the key the
  // removed account's funds stay listed (and saved offline) after it went.
  for (const mode of ["hide", "disconnect", "disconnect_purge", "purge"])
    assert.ok(accountRemoveMoved(mode).includes("holdings"), mode);
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

// /holdings leaves out hidden, linked-shadow and business accounts and
// follows the combined-view setting, so every write that changes one of
// those must refresh it — on mobile it is also an offline key, so a stale
// list would outlive a restart. Read from source: the key lists sit inside
// mutation callbacks no test can call.
test("writes that change what top holdings counts refresh it", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  // the text from an anchor to the end of its call, close enough to hold
  // the refresh list without reaching the next mutation
  const after = (src, anchor, n, file) => {
    const i = src.indexOf(anchor);
    assert.ok(i >= 0, `${file}: anchor ${anchor} not found`);
    return src.slice(i, i + n);
  };
  const sites = [
    ["../src/lib/cache.ts", "export const personalKeys", 160, '["holdings"]'],
    ["../src/app/(tabs)/accounts.tsx", "const setHidden = useMutation", 900,
     '"holdings"'],
    ["../src/app/(tabs)/accounts.tsx", "const linkAct = useMutation", 1100,
     '"holdings"'],
    ["../src/app/business.tsx", "client!.businessCombine(on)", 700,
     '["accounts"]'],
    ["../../webapp/src/pages/Accounts.tsx", "function LinkSuggestions", 900,
     '"holdings"'],
    ["../../webapp/src/pages/Accounts.tsx", "function LinkControls", 900,
     '"holdings"'],
    ["../../webapp/src/pages/Accounts.tsx", "const setHidden = useMutation",
     900, '"holdings"'],
    ["../../webapp/src/components/ManageAccounts.tsx",
     "const setHidden = useMutation", 900, '"holdings"'],
    ["../../webapp/src/pages/Business.tsx", "const afterAccountsMoved", 600,
     '"holdings"'],
    ["../../webapp/src/pages/Business.tsx", "api.businessCombine(v)", 500,
     '"holdings"'],
    ["../../webapp/src/components/BusinessWizard.tsx",
     "const assign = useMutation", 900, '"holdings"'],
    ["../../webapp/src/components/RemoveAccountPanel.tsx",
     "export function accountRemoveMoved", 200, '"holdings"'],
  ];
  for (const [file, anchor, n, key] of sites)
    assert.ok(after(read(file), anchor, n, file).includes(key),
              `${file} @ ${anchor}: must refresh ${key}`);
});

test("an empty category filter shows every option, not none", () => {
  // catLabel spells an empty value as "Uncategorized"; passing the typed
  // text through it would turn an empty filter box into "only options that
  // contain the word Uncategorized" — i.e. the picker would open blank.
  const all = ["FOOD_AND_DRINK", "GENERAL_SERVICES", "TRANSFER_OUT", "?"];
  assert.deepEqual(filterCategories(all, ""), all);
  assert.deepEqual(filterCategories(all, "   "), all);
});

test("a merchant's current category is the one most of its rows carry", () => {
  const rows = (...cs) => cs.map((c) => ({ category: c }));
  assert.equal(dominantCategory(rows("A", "B", "B")), "B");
  // a tie keeps the first seen — stable, never flickering between renders
  assert.equal(dominantCategory(rows("A", "B")), "A");
  // uncategorized rows never win the label
  assert.equal(dominantCategory(rows(null, null, "C")), "C");
  assert.equal(dominantCategory(rows(null)), null);
  assert.equal(dominantCategory([]), null);
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

// ---- the date picker's month grid ----
import { monthGrid } from "../src/lib/dates.ts";
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

// ---- the ledger's quick date ranges ----
import { rangeCaption } from "../src/lib/pure.ts";

test("rangeCaption names the short windows in words", () => {
  assert.equal(rangeCaption("cur"), "this month");
  assert.equal(rangeCaption("1m"), "last month");
  assert.equal(rangeCaption("3m"), "3m");
  assert.equal(rangeCaption("all"), "all time");
});

// ---- "use the automatic category": the one write the client can't echo ----
import { catLabel, isCategoryReset } from "../src/lib/pure.ts";

import { catKey, isMovedMoney } from "../src/lib/pure.ts";

test("moved money is told from the category's key, not its display form", () => {
  // the server sends a row's category spaced and upper-case; catLabel now
  // reads it as "Transfer Out", so a test on the label would miss it
  assert.equal(isMovedMoney("TRANSFER OUT", 250), true);
  assert.equal(isMovedMoney("TRANSFER_IN", -250), true);
  assert.equal(isMovedMoney("LOAN PAYMENTS", -400), true);   // card credit
  assert.equal(isMovedMoney("LOAN PAYMENTS", 400), false);   // the payment out
  assert.equal(isMovedMoney("FOOD AND DRINK", 12), false);
  assert.equal(isMovedMoney(null, 12), false);
  // a category somebody typed is theirs, and real spending
  assert.equal(isMovedMoney("Transfer fees", 12), false);
  // the form a cached row is patched with after a recategorize
  assert.equal(catKey("FOOD_AND_DRINK"), "FOOD AND DRINK");
  assert.equal(catLabel(catKey("FOOD_AND_DRINK")), "Food and Drink");
});

import { rowCategoryLabel } from "../src/lib/pure.ts";

test("a card payment is money moved on both sides and says so", () => {
  const cc = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT";
  // the checking side pays out, and is muted like the card side
  assert.equal(isMovedMoney("LOAN PAYMENTS", 400, cc), true);
  assert.equal(isMovedMoney("LOAN PAYMENTS", -400, cc), true);
  // a mortgage shares the category and IS spending
  assert.equal(isMovedMoney("LOAN PAYMENTS", 900,
                            "LOAN_PAYMENTS_MORTGAGE_PAYMENT"), false);
  assert.equal(rowCategoryLabel({ category: "LOAN PAYMENTS",
                                  category_detailed: cc }), "Card payment");
  assert.equal(rowCategoryLabel({ category: "LOAN PAYMENTS",
                                  category_detailed: "LOAN_PAYMENTS_MORTGAGE_PAYMENT" }),
               "Loan Payments");
  // a joining word at the end is a word like any other
  assert.equal(catLabel("TRANSFER IN"), "Transfer In");
  assert.equal(catLabel("FOOD AND DRINK"), "Food and Drink");
});

import { nearDay } from "../src/lib/pure.ts";

test("a coming date reads today or tomorrow on the two days people act on", () => {
  assert.equal(nearDay("2026-03-31", "2026-03-31"), "today");
  // across a month end, by the calendar and not by adding to a day number
  assert.equal(nearDay("2026-04-01", "2026-03-31"), "tomorrow");
  assert.equal(nearDay("2026-04-02", "2026-03-31"), null);
  assert.equal(nearDay("2026-03-30", "2026-03-31"), null);
  assert.equal(nearDay(null, "2026-03-31"), null);
});

test("a category reads in Title Case; a typed name is left as typed", () => {
  assert.equal(catLabel("FOOD_AND_DRINK"), "Food and Drink");
  assert.equal(catLabel("FOOD AND DRINK"), "Food and Drink");
  assert.equal(catLabel("GENERAL_MERCHANDISE"), "General Merchandise");
  assert.equal(catLabel("Pet care"), "Pet care");
  assert.equal(catLabel("mortgage"), "mortgage");
  assert.equal(catLabel("?"), "Uncategorized");
});

test("a category reset has no local echo — the empty category is not a category", () => {
  // the sentinel the picker and the "Use automatic" button send reaches the
  // server as the empty string, which means "drop the override" — NOT a
  // category named "". A screen that echoed what it sent would render this…
  assert.equal(catLabel(categoryForServer(CLEAR_CATEGORY)), "Uncategorized");
  // …so the reset is the write whose result is read back instead
  assert.equal(isCategoryReset(categoryForServer(CLEAR_CATEGORY)), true);
  // every other write IS the client's own answer and echoes immediately
  assert.equal(isCategoryReset(categoryForServer("FOOD_AND_DRINK")), false);
  assert.equal(isCategoryReset(categoryForServer("Groceries")), false);
});

test("one ledger row is read back from its own day, by id", async () => {
  const real = globalThis.fetch;
  const asked = [];
  globalThis.fetch = async (url) => {
    asked.push(String(url));
    return new Response(JSON.stringify({ mode: "search", total: 2, rows: [
      { id: "other", date: "2026-09-04", category: "TRAVEL" },
      { id: "row-1", date: "2026-09-04", category: "FOOD_AND_DRINK",
        override_manual: false, category_why: "the bank's own label" }] }),
      { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    const c = makeClient("https://example.invalid", "tok", () => {});
    const row = await c.txnById("row-1", "2026-09-04T00:00:00");
    assert.equal(row.category, "FOOD_AND_DRINK");
    // the server's answer, not the caller's guess: the flag the "Use
    // automatic" button is gated on comes back false with it
    assert.equal(row.override_manual, false);
    assert.equal(row.category_why, "the bank's own label");
    // scoped to the row's own day — never a whole-ledger scan
    assert.match(asked[0], /date_from=2026-09-04&date_to=2026-09-04/);
  } finally { globalThis.fetch = real; }
});

test("a long day is walked page by page, and an absent row is null", async () => {
  const real = globalThis.fetch;
  const page = (n, rows) => new Response(
    JSON.stringify({ mode: "search", total: 250, rows }),
    { status: 200, headers: { "content-type": "application/json" } });
  globalThis.fetch = async (url) => {
    const n = Number(new URL(String(url)).searchParams.get("page"));
    // 100 rows a page, the row wanted on the last one
    const rows = Array.from({ length: 100 }, (_, i) => (
      { id: `p${n}-${i}`, date: "2026-09-04", category: "TRAVEL" }));
    if (n === 3) rows[7] = { id: "late", date: "2026-09-04",
                             category: "GENERAL_MERCHANDISE" };
    return page(n, n > 3 ? [] : rows);
  };
  try {
    const c = makeClient("https://example.invalid", "tok", () => {});
    assert.equal((await c.txnById("late", "2026-09-04")).category,
                 "GENERAL_MERCHANDISE");
    // a row the day's listing does not hold (a hidden account's) is not an
    // error — the caller keeps what it has rather than showing a guess
    assert.equal(await c.txnById("nowhere", "2026-09-04"), null);
  } finally { globalThis.fetch = real; }
});

// ---- the emailed sign-in link: a refusal the sign-in screen can print ----
import { requestUnlock, unlockRefusal } from "../src/lib/api.ts";

/** The unlock door answering with `status` and the HTML page that route
 *  really serves — it is a page route, so even its refusals are HTML. */
function unlockDoor(status, statusText = "") {
  return async () => new Response(
    "<html><body><h1>Slow down</h1></body></html>",
    { status, statusText, headers: { "content-type": "text/html" } });
}

test("the hourly cap on sign-in links reads as a rate limit, not a status line",
     async () => {
  const real = globalThis.fetch;
  globalThis.fetch = unlockDoor(429, "Too Many Requests");
  try {
    await assert.rejects(
      () => requestUnlock("https://example.invalid", "a@b.dev"),
      (e) => e instanceof ApiError && e.status === 429
             && /wait a few minutes/i.test(errText(e)));
  } finally { globalThis.fetch = real; }
});

test("an unlock refusal with no status line still says something", async () => {
  // React Native's fetch leaves statusText empty on some platforms, which
  // is how a refused tap became a silently blank error line
  const real = globalThis.fetch;
  globalThis.fetch = unlockDoor(500);
  try {
    await assert.rejects(
      () => requestUnlock("https://example.invalid", "a@b.dev"),
      (e) => e instanceof ApiError && errText(e).trim().length > 10);
  } finally { globalThis.fetch = real; }
});

test("the unlock door's 200 is success, HTML page and all", async () => {
  const real = globalThis.fetch;
  globalThis.fetch = unlockDoor(200);
  try {
    await requestUnlock("https://example.invalid", "a@b.dev");
  } finally { globalThis.fetch = real; }
});

test("the sign-in carries the typed sign-in code, and only when there is one",
     async () => {
  // only a sign-in carrying the code the confirm page showed can spend the
  // emailed unlock; the unlock REQUEST hands nothing back to carry
  const real = globalThis.fetch;
  const bodies = [];
  globalThis.fetch = async (_url, init) => {
    bodies.push(String(init.body));
    return new Response(JSON.stringify({ detail: "human_check_failed" }),
      { status: 403, headers: { "content-type": "application/json" } });
  };
  try {
    await assert.rejects(() => loginAndMintDevice(
      "https://example.invalid", "a@b.dev", "pw", "", "phone", "android",
      "", "ABCDE-FGHIJ"));
    assert.match(bodies[0], /unlock_proof=ABCDE-FGHIJ/);
    await assert.rejects(() => loginAndMintDevice(
      "https://example.invalid", "a@b.dev", "pw", "", "phone", "android"));
    assert.doesNotMatch(bodies[1], /unlock_proof/);
  } finally { globalThis.fetch = real; }
});

test("a demo instance has no unlock door, and says so", () => {
  assert.match(unlockRefusal(404), /doesn't offer/i);
  assert.match(unlockRefusal(503), /error 503/);
});

// ---- what-if screens (debt planner, retirement): a phone keyboard's
// "1,000" must reach the GET as the number the save door would store,
// and a refusal is shown at the fields, never as "couldn't reach" ----
test("what-if money: formatted amounts become plain numbers", () => {
  assert.equal(moneyParam("1,000"), "1000");
  assert.equal(moneyParam("$ 250.50"), "250.5");
  // the save door reads a decimal comma as a decimal, so the what-if
  // reads it the same way — and an ambiguous comma is no number at all
  assert.equal(moneyParam("50,5"), "50.5");
  assert.equal(moneyParam("1.234,56"), "1234.56");
  assert.equal(numv("12,50"), 12.5);
  assert.equal(numv("1,2,3"), 0);
  assert.equal(moneyParam(""), "0");
  // text that is not an amount rides as typed, so the server refuses it
  // and the screen says why — never sent as $0 (the web does the same)
  assert.equal(moneyParam("abc"), "abc");
  assert.equal(moneyParam("1,2,3"), "1,2,3");
  assert.equal(moneyParam(" 12x "), "12x");
  assert.deepEqual(whatIfQuery({ spend: "lots", ret: "", end: "90" },
                               ["spend"], true), { spend: "lots", end: "90" });
  // whole dollars round the way every other money figure does — half to
  // even, as the server and the web do — never half away from zero
  assert.equal(moneyParam("250.50", true), "250");
  assert.equal(moneyParam("251.50", true), "252");
  assert.equal(moneyParam("250.51", true), "251");
  assert.equal(moneyParam("60,000", true), "60000");
});

test("what-if query: blanks stay off, money parsed, the rest as typed", () => {
  assert.deepEqual(
    whatIfQuery({ spend: "60,000", employer_mo: " 1,250.40 ", ret: "7.0",
                  cash: "", end: " 95 " },
                ["spend", "employer_mo", "taxable_mo"], true),
    { spend: "60000", employer_mo: "1250", ret: "7.0", end: "95" });
});

test("what-if refusal: a 4xx is the server's sentence, not an outage", () => {
  assert.equal(whatIfRefusal(new ApiError(429, "Too many requests")),
               "Too many requests");
  // a FastAPI validation 422 carries its detail as a list, so the client
  // is left with no sentence: say the server refused it, never a bare status
  assert.equal(whatIfRefusal(new ApiError(422, "")),
               "The server refused the request.");
  // the connection's failures keep the offline wording
  assert.equal(whatIfRefusal(new ApiError(502, "Bad Gateway")), null);
  assert.equal(whatIfRefusal(new TypeError("Network request failed")), null);
  // the session handler owns 401 (back to sign-in)
  assert.equal(whatIfRefusal(new ApiError(401, "Not signed in")), null);
});

// ---- widget cache ownership: a cached glance is one household's day;
// a sign-in to another household (or server) must drop it ----
test("widget owner: same household keeps the cache, another drops it", () => {
  const a = widgetOwner("https://Home.example/", "t-a");
  assert.equal(a, widgetOwner("https://home.example", "t-a"));
  assert.equal(widgetOwnerChanged(a, a), false);
  assert.equal(widgetOwnerChanged(a, widgetOwner("https://home.example", "t-b")),
               true);
  assert.equal(widgetOwnerChanged(a, widgetOwner("https://other.example", "t-a")),
               true);
  // no record is a change: a cache nobody can vouch for is not shown
  assert.equal(widgetOwnerChanged(null, a), true);
});

// a fetch in flight across a change of household must not re-plant the
// old household's numbers under the new credential
test("widget cache: an entry is shown only under the credential it was fetched with", () => {
  const entry = { glance: glance(), fetched_at: 1, owner: credentialStamp("tok-a") };
  assert.equal(ownedCache(entry, "tok-a"), entry);
  assert.equal(ownedCache(entry, "tok-b"), null);        // household B now
  assert.equal(ownedCache(entry, null), null);           // signed out
  assert.equal(ownedCache({ glance: glance(), fetched_at: 1 }, "tok-a"), null);
  assert.equal(ownedCache(null, "tok-a"), null);
  assert.notEqual(credentialStamp("tok-a"), credentialStamp("tok-b"));
  assert.equal(credentialStamp("tok-a"), credentialStamp("tok-a"));
  assert.ok(!credentialStamp("tok-a").includes("tok"));  // never the token
});

test("widget cache: a save whose credential changed mid-fetch is skipped", () => {
  assert.equal(saveStillOwned("tok-a", "tok-a"), true);
  assert.equal(saveStillOwned("tok-a", "tok-b"), false);  // re-minted for B
  assert.equal(saveStillOwned("tok-a", null), false);     // signed out
  assert.equal(saveStillOwned(null, null), false);
});

// ---- money drafts, signed totals, the planner's bucket merge (the same
// table the server suite runs against the web's twin) ----
for (const [name, fn] of Object.entries(moneyCases(pureModule, assert)))
  test(`money: ${name}`, fn);

// ---- files left in the app cache for the share sheet ----
test("cache sweep: every shared download kind is ours, the platform's are not", () => {
  const { isSharedDownload } = pureModule;
  for (const n of ["oikonome-export.zip", "dump.sql.gz", "biz.csv",
                   "continuity.pdf", "calendar.ics", "receipt.JPG",
                   "receipt.webp", "oikonome-config.oikx",
                   "oikonome-system-health.json",
                   "oikonome-answer-2026-10-15.html", "transactions.xlsx"])
    assert.equal(isSharedDownload(n), true, n);
  for (const n of ["ImagePicker", "http-cache", "notes.txt", "oikx"])
    assert.equal(isSharedDownload(n), false, n);
});

// Every name the app writes into the cache for the share sheet, read
// from the source itself, must be one the sweep deletes: a format added
// without widening isSharedDownload would leave that household's file in
// the cache, unencrypted, after sign-out. A name whose extension is chosen
// at run time has its choices listed here, so adding one fails loudly
// until it is listed (and swept).
const RUNTIME_EXTENSIONS = {
  "src/app/(tabs)/transactions.tsx": ["csv", "xlsx"],
  "src/app/txn.tsx": ["pdf", "png", "jpg"],
};
test("cache sweep: every file name the app downloads or writes is swept", () => {
  const { isSharedDownload } = pureModule;
  const root = new URL("../", import.meta.url);
  const walk = (dir) => readdirSync(new URL(dir, root), { withFileTypes: true })
    .flatMap((e) => e.isDirectory() ? walk(`${dir}${e.name}/`)
      : /\.tsx?$/.test(e.name) ? [`${dir}${e.name}`] : []);
  const lit = /(["'`])((?:\$\{[^}]*\}|[^"'`\s$])*\.(?:\$\{[^}]+\}|[A-Za-z0-9]+))\1/g;
  const names = [];
  for (const file of walk("src/")) {
    const src = readFileSync(new URL(file, root), "utf8");
    const spots = [
      ...[...src.matchAll(/\.download\(([\s\S]*?)\);/g)].map((m) => m[1]),
      ...[...src.matchAll(/filename=(\{`[^`]*`\}|"[^"]*")/g)].map((m) => m[1]),
      ...[...src.matchAll(/cacheDirectory!?\s*\+\s*(\([^)]*\)|`[^`]*`|"[^"]*")/g)]
        .map((m) => m[1]),
    ];
    for (const spot of spots) {
      const found = [...spot.matchAll(lit)].map((m) => m[2])
        .filter((n) => !n.startsWith("/"));        // a request path, not a name
      for (const n of found.slice(-1)) {
        const ext = /\.\$\{[^}]+\}$/.exec(n);
        if (!ext) { names.push([file, n]); continue; }
        const choices = RUNTIME_EXTENSIONS[file];
        assert.ok(choices, `${file}: ${n} picks its extension at run time; list the choices`);
        for (const c of choices) names.push([file, n.slice(0, ext.index) + "." + c]);
      }
    }
  }
  const files = new Set(names.map(([f]) => f));
  for (const f of ["src/app/(tabs)/transactions.tsx", "src/app/txn.tsx",
                   "src/app/settings.tsx", "src/app/business.tsx",
                   "src/app/doctor.tsx", "src/lib/api.ts"])
    assert.ok(files.has(f), `no download name found in ${f}: the scan has gone blind`);
  assert.ok(names.some(([, n]) => n === "transactions.xlsx"));
  for (const [f, n] of names) assert.equal(isSharedDownload(n), true, `${f}: ${n}`);
});

test("cache sweep: sign-out takes every download, a later sweep spares fresh ones", () => {
  const { downloadsToSweep } = pureModule;
  const now = 10_000;
  const files = [{ name: "old.zip", mtimeS: now - 700 },
                 { name: "fresh.pdf", mtimeS: now - 30 },
                 { name: "bundle.oikx", mtimeS: now - 5 },
                 { name: "ImagePicker", mtimeS: now - 9000 }];
  assert.deepEqual(downloadsToSweep(files, now, 0),
                   ["old.zip", "fresh.pdf", "bundle.oikx"]);
  assert.deepEqual(downloadsToSweep(files, now, 600), ["old.zip"]);
});

// ---- reimburse: the expected-amount editor and the pairing total ----
test("edit expected: blank or 0 clears, a positive amount sets, the rest is refused", () => {
  const { expectedFromDraft } = pureModule;
  assert.deepEqual(expectedFromDraft(""), { ok: true, expected: null });
  assert.deepEqual(expectedFromDraft("  "), { ok: true, expected: null });
  assert.deepEqual(expectedFromDraft("0"), { ok: true, expected: null });
  assert.deepEqual(expectedFromDraft("$1,234.50"),
                   { ok: true, expected: 1234.5 });
  for (const bad of ["-20", "12.5.0", "abc", "1-"])
    assert.deepEqual(expectedFromDraft(bad), { ok: false }, bad);
});

test("pairing total: a chosen deposit the filter hid still counts", () => {
  const { pairSelection } = pureModule;
  const chosen = new Map([["a", { amount: -40 }], ["b", { amount: -15.5 }]]);
  assert.deepEqual(pairSelection(chosen, ["b", "c"]),
                   { total: 55.5, count: 2, hidden: 1 });
  assert.deepEqual(pairSelection(chosen, ["a", "b"]),
                   { total: 55.5, count: 2, hidden: 0 });
  assert.deepEqual(pairSelection(new Map(), []),
                   { total: 0, count: 0, hidden: 0 });
  // a deposit half spent on other charges gives only what it has left
  assert.deepEqual(pairSelection(new Map([["a", { amount: -150,
                                                  left_amount: 50 }]]), ["a"]),
                   { total: 50, count: 1, hidden: 0 });
});

// ---- the bill form's ledger-fit hand-off ----
test("bill prefill: only the in-app hand-off fills the form, once, for its payee", async () => {
  const { setBillPrefill, takeBillPrefill } = await import("../src/lib/api.ts");
  // a link that reaches /bill finds nothing to pre-fill
  assert.equal(takeBillPrefill("Invented Rent"), null);
  const fit = { amount: "12.00", cadence: "MONTHLY", next_due: "2031-02-03" };
  setBillPrefill("Invented Rent", fit);
  // another bill's form does not get this one's values, and the slot is spent
  assert.equal(takeBillPrefill("Invented Gym"), null);
  assert.equal(takeBillPrefill("Invented Rent"), null);
  setBillPrefill("Invented Rent", fit);
  assert.deepEqual(takeBillPrefill("Invented Rent"), fit);
  assert.equal(takeBillPrefill("Invented Rent"), null);
});

// ---- the device-limit picker: finishing a sign-in whose mint hit the
// device ceiling spends the grant that login left, never a second login
// with an already-used one-time code ----
test("device-limit retry: a live grant is spent, whatever the login used", () => {
  const { deviceLimitRetry, MINT_GRANT_USABLE_MS } = pureModule;
  const grant = { ticket: "oikm-invented", cookie: "", issuedAt: 1000 };
  assert.equal(deviceLimitRetry(grant, 1000, true), "mint");
  assert.equal(deviceLimitRetry(grant, 1000, false), "mint");
  assert.equal(deviceLimitRetry(grant, 1000 + MINT_GRANT_USABLE_MS - 1, true),
               "mint");
  // an older server hands back no ticket; its session cookie still works
  assert.equal(deviceLimitRetry({ ticket: "", cookie: "s=1", issuedAt: 0 },
                                10, true), "mint");
});

test("device-limit retry: without a grant a spent code is never resent", () => {
  const { deviceLimitRetry, MINT_GRANT_USABLE_MS, mintGrantRefused } =
    pureModule;
  const stale = { ticket: "oikm-invented", cookie: "", issuedAt: 0 };
  assert.equal(deviceLimitRetry(stale, MINT_GRANT_USABLE_MS, true), "ask-code");
  assert.equal(deviceLimitRetry(stale, MINT_GRANT_USABLE_MS, false), "login");
  assert.equal(deviceLimitRetry(null, 0, true), "ask-code");
  assert.equal(deviceLimitRetry(null, 0, false), "login");
  assert.equal(deviceLimitRetry({ ticket: "", cookie: "", issuedAt: 0 },
                                0, false), "login");
  // a clock that ran backwards does not make a grant look fresh
  assert.equal(deviceLimitRetry(stale, -5, true), "ask-code");
  assert.equal(mintGrantRefused(401), true);
  assert.equal(mintGrantRefused(409), false);
});

test("device-limit retry: the 409 carries the grant and the retry is only the mint", async () => {
  const { loginAndMintDevice, mintDeviceWithGrant } =
    await import("../src/lib/api.ts");
  const calls = [];
  let atCeiling = true;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const path = new URL(url).pathname;
    calls.push({ path, body: init.body ? String(init.body) : "" });
    const json = (status, obj) => new Response(JSON.stringify(obj), {
      status, headers: { "content-type": "application/json" } });
    if (path === "/api/login")
      return json(200, { ok: true, mint_ticket: "oikm-invented-ticket" });
    if (path === "/api/devices") {
      const b = JSON.parse(String(init.body));
      if (atCeiling && !b.replace)
        return json(409, { detail: { error: "device_limit",
          message: "limit", devices: [{ id: "dev-a" }] } });
      return json(200, { token: "oikd_invented", id: "dev-new" });
    }
    return json(200, { ok: true });
  };
  try {
    let err;
    try {
      await loginAndMintDevice("https://example.invalid", "a@example.invalid",
        "pw", "123456", "Phone", "android");
    } catch (e) { err = e; }
    assert.equal(err?.status, 409);
    assert.equal(err.mintGrant?.ticket, "oikm-invented-ticket");
    calls.length = 0;
    const r = await mintDeviceWithGrant("https://example.invalid",
      err.mintGrant, "Phone", "android", "dev-a");
    assert.equal(r.token, "oikd_invented");
    assert.equal(calls.some((c) => c.path === "/api/login"), false);
    const mint = JSON.parse(calls.find((c) => c.path === "/api/devices").body);
    assert.equal(mint.mint_ticket, "oikm-invented-ticket");
    assert.equal(mint.replace, "dev-a");
  } finally {
    globalThis.fetch = realFetch;
  }
});

// ---- a list that never loaded is not an empty list ----
test("listPhase: a failed first fetch reads as failed, not empty", () => {
  const { listPhase } = pureModule;
  assert.equal(listPhase({ isError: true, hasData: false }, 0), "failed");
  assert.equal(listPhase({ isError: false, hasData: false }, 0), "loading");
  assert.equal(listPhase({ isError: false, hasData: true }, 0), "empty");
  // a stale cached answer is still the answer; the banner dates it
  assert.equal(listPhase({ isError: true, hasData: true }, 0), "empty");
  assert.equal(listPhase({ isError: true, hasData: true }, 3), "rows");
});

// ---- the reimbursement matcher's "why" and gap line run from the
// shared case table (money-cases.mjs), against both clients ----

// ---- a non-money number field refuses what it cannot read, rather
// than sending it as null (which the server takes as "clear") ----
test("numberDraft: blank clears, a typo is refused, never NaN", () => {
  const { numberDraft } = pureModule;
  assert.equal(numberDraft(""), null);
  assert.equal(numberDraft("  "), null);
  assert.equal(numberDraft("22.5"), 22.5);
  assert.equal(numberDraft(" 1,200 "), 1200);
  assert.equal(numberDraft("50"), 50);
  for (const bad of ["22,5", "22%", "abc", "1.2.3", "12,00"])
    assert.equal(numberDraft(bad), undefined, bad);
});

// ---- the bank-link poll survives a lost request ----
test("linkPollMayRetry: only a definite refusal ends the wait", () => {
  const { linkPollMayRetry } = pureModule;
  // the poll ticks fast for a while after the app comes back to the
  // foreground — the bank step just ended — else at the normal tick
  assert.equal(linkPollWait(1000, 5000), LINK_POLL_FAST_MS);
  assert.equal(linkPollWait(5000, 5000), LINK_POLL_MS);
  assert.equal(linkPollWait(9000, 0), LINK_POLL_MS);
  assert.equal(pendingLinkLine("add", false), "Finish signing in at your bank…");
  assert.equal(pendingLinkLine("relink", false),
               "Relinking at your bank for more history…");
  assert.equal(pendingLinkLine("fix", true),
               "Finishing the sign-in at your bank… (closing)");
  assert.equal(linkPollMayRetry(new TypeError("Network request failed")), true);
  for (const status of [200, 500, 502, 503, 524, 408, 429])
    assert.equal(linkPollMayRetry({ status }), true, String(status));
  for (const status of [400, 401, 402, 403, 404])
    assert.equal(linkPollMayRetry({ status }), false, String(status));
});

// ---- a sign-out whose revoke missed keeps the token to revoke later ----
test("queueUnrevoked: once per token, newest few, none past their life", () => {
  const { queueUnrevoked, UNREVOKED_MAX } = pureModule;
  const now = 100 * 86_400_000;
  const a = { url: "u", token: "a", at: now - 1000 };
  const old = { url: "u", token: "old", at: now - 91 * 86_400_000 };
  assert.deepEqual(queueUnrevoked([old], a, now), [a]);
  assert.deepEqual(queueUnrevoked([a], { ...a, at: now }, now),
                   [{ ...a, at: now }]);
  const many = Array.from({ length: 9 }, (_, i) =>
    ({ url: "u", token: `t${i}`, at: now }));
  const kept = queueUnrevoked(many, null, now);
  assert.equal(kept.length, UNREVOKED_MAX);
  assert.equal(kept.at(-1).token, "t8");
  assert.deepEqual(queueUnrevoked([], null, now), []);
});

// ---- picked documents are the household's: only the pickers' own
// cache folders are cleared, never a file the app does not own ----
test("isPickerScratch: the two picker folders under the cache, nothing else", () => {
  const { isPickerScratch } = pureModule;
  const cache = "file:///data/user/0/app/cache/";
  assert.equal(isPickerScratch(cache + "ImagePicker/a.jpeg", cache), true);
  assert.equal(isPickerScratch(cache + "DocumentPicker/w2.pdf", cache), true);
  assert.equal(isPickerScratch(cache + "export.zip", cache), false);
  assert.equal(isPickerScratch("content://media/1", cache), false);
  assert.equal(isPickerScratch(cache + "ImagePicker/a.jpeg", null), false);
});

// ---- Rules empty line: first-run only when there are no rules ----
test("rulesEmptyLine: a search with no hits is not 'No rules yet'", () => {
  const { rulesEmptyLine, lastPage } = pureModule;
  const none = { user: 0, llm: 0, model: 0, seed: 0 };
  const some = { user: 0, llm: 12, model: 0, seed: 3000 };
  assert.equal(rulesEmptyLine(none, ""), "first-run");
  assert.equal(rulesEmptyLine(some, "zzz"), "No matches in this group.");
  assert.equal(rulesEmptyLine(some, ""), "No rules here yet.");
  assert.equal(lastPage(0, 50), 1);
  assert.equal(lastPage(101, 50), 3);
  assert.equal(lastPage(100, 50), 2);
});

// ---- a screen that shows a key refetches after every deferred write,
// even one that lands while an earlier refetch is still running ----
import { QueryObserver } from "@tanstack/react-query";
import { AppQueryClient, shownRefetcher } from "../src/lib/shown.ts";

const tick = () => new Promise((r) => setTimeout(r, 0));

function shownScreen(qc, key) {
  const calls = [];
  let state = 0;
  const obs = new QueryObserver(qc, { queryKey: key, retry: false,
    staleTime: 60_000,
    queryFn: () => { const saw = state; return new Promise((res) =>
      calls.push({ saw, land: () => res({ v: saw }) })); } });
  const unsub = obs.subscribe(() => {});
  const w = shownRefetcher(qc, key, () => obs.getCurrentResult());
  const detach = w.attach();
  return { obs, calls, w, write: (v) => { state = v; },
           done: () => { detach(); unsub(); } };
}

test("shown refetch: a write during a refetch is not lost to the older answer", async () => {
  const qc = new AppQueryClient();
  const sc = shownScreen(qc, ["bills"]);
  sc.calls[0].land(); await tick();
  sc.w.focus();
  // write A: its refetch goes out
  sc.write(1);
  qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
  await tick();
  assert.equal(sc.calls.length, 2);
  // write B lands while A's refetch is still out; no cache patch, so
  // TanStack itself raises no second "invalidate" event
  sc.write(2);
  qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
  await tick();
  assert.equal(sc.calls.length, 3, "B must start its own refetch");
  sc.calls[1].land(); sc.calls[2].land(); await tick();
  assert.equal(qc.getQueryData(["bills"]).v, 2);
  sc.done();
});

test("shown refetch: the patch-then-invalidate order is covered too", async () => {
  const qc = new AppQueryClient();
  const sc = shownScreen(qc, ["bills"]);
  sc.calls[0].land(); await tick();
  sc.w.focus();
  sc.write(1);
  qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
  await tick();
  sc.write(2);
  qc.setQueryData(["bills"], (o) => ({ ...o, patched: true }));
  qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
  await tick();
  sc.calls[1].land(); await tick();
  sc.calls.at(-1).land(); await tick();
  assert.equal(qc.getQueryData(["bills"]).v, 2);
  sc.done();
});

test("TanStack raises no event for a second invalidation of an invalid query", () => {
  // why the app announces deferred invalidations itself (shown.ts)
  const qc = new AppQueryClient();
  qc.setQueryData(["k"], 1);
  const seen = [];
  const off = qc.getQueryCache().subscribe((e) => {
    if (e.type === "updated" && e.action.type === "invalidate") seen.push(1);
  });
  const told = [];
  const off2 = qc.onDeferredInvalidate(() => told.push(1));
  qc.invalidateQueries({ queryKey: ["k"], refetchType: "none" });
  qc.invalidateQueries({ queryKey: ["k"], refetchType: "none" });
  assert.equal(seen.length, 1);
  assert.equal(told.length, 2);
  off(); off2();
});

test("shown refetch: a write while hidden is caught up on showing, even after a fetch made it look fresh", async () => {
  const qc = new AppQueryClient();
  const sc = shownScreen(qc, ["today"]);
  sc.calls[0].land(); await tick();
  sc.w.focus(); sc.w.blur();
  // a refetch goes out (pull-to-refresh), the screen is left, a write
  // elsewhere lands, then the old answer settles and reads fresh
  void sc.obs.refetch(); await tick();
  sc.write(1);
  qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
  sc.calls[1].land(); await tick();
  assert.equal(sc.obs.getCurrentResult().isStale, false);
  const before = sc.calls.length;
  sc.w.focus(); await tick();
  assert.equal(sc.calls.length, before + 1);
  sc.calls.at(-1).land(); await tick();
  assert.equal(qc.getQueryData(["today"]).v, 1);
  sc.done();
});

test("shown refetch: a tab that errored retries once when shown again", async () => {
  const qc = new AppQueryClient();
  let fail = true, n = 0;
  const obs = new QueryObserver(qc, { queryKey: ["txns"], retry: false,
    queryFn: async () => { n++; if (fail) throw new Error("down"); return 1; } });
  const unsub = obs.subscribe(() => {});
  await tick(); await tick();
  assert.equal(obs.getCurrentResult().isError, true);
  const w = shownRefetcher(qc, ["txns"], () => obs.getCurrentResult());
  const detach = w.attach();
  fail = false;
  w.focus(); await tick(); await tick();
  assert.equal(n, 2);
  assert.equal(qc.getQueryData(["txns"]), 1);
  detach(); unsub();
});

test("shown refetch: several invalidations from one write make one fetch", async () => {
  const qc = new AppQueryClient();
  const sc = shownScreen(qc, ["today"]);
  sc.calls[0].land(); await tick();
  sc.w.focus();
  qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
  qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
  await tick();
  assert.equal(sc.calls.length, 2);
  // another key's write does not touch this screen
  qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
  await tick();
  assert.equal(sc.calls.length, 2);
  sc.calls[1].land(); await tick();
  sc.done();
});

// ---- push: the "already sent" hint is trusted only while the server
// still holds this device's token live ----
test("pushStillLive: a dead or missing row re-registers", () => {
  const { pushStillLive } = pureModule;
  const me = (push) => [{ current: false, push: "on" },
                        { current: true, push }];
  assert.equal(pushStillLive("u t", "u t", me("on")), true);
  assert.equal(pushStillLive("u t", "u t", me("dead")), false);
  assert.equal(pushStillLive("u t", "u t", me(null)), false);
  // token rotated, server changed, or signed in afresh (hint cleared)
  assert.equal(pushStillLive("u t", "u t2", me("on")), false);
  assert.equal(pushStillLive(null, "u t", me("on")), false);
  // no roster to check against: send, as every launch used to
  assert.equal(pushStillLive("u t", "u t", null), false);
  assert.equal(pushStillLive("u t", "u t", []), false);
});

// ---- the owner's theme reconcile: "match system" is kept, never read
// back as the dark default ----
test("theme: an explicit auto survives the account reconcile", () => {
  assert.equal(accountTheme("auto"), "auto");
  assert.equal(accountTheme("light"), "light");
  assert.equal(accountTheme("dark"), "dark");
  // never chosen: follow the phone's own setting
  assert.equal(accountTheme(undefined), "auto");
  assert.equal(accountTheme(null), "auto");
  // not a theme this build knows: leave the phone's choice alone
  assert.equal(accountTheme("neon"), null);
});

// ---- whose theme is the household's: the web's rule (App.tsx
// perDeviceTheme = viewer-or-loading || demo), so a member's pick is saved
// and the household's pin reaches a member's phone too ----
test("theme: owners and members follow the household, viewers and the demo keep their own", () => {
  assert.equal(themeFollowsHousehold({ role: "owner" }), true);
  assert.equal(themeFollowsHousehold({ role: "member" }), true);
  assert.equal(themeFollowsHousehold({ role: "viewer" }), false);
  assert.equal(themeFollowsHousehold({ role: "owner", demo: true }), false);
  assert.equal(themeFollowsHousehold({ role: "member", demo: true }), false);
  // not yet known: per-device, the safe side (the web's isViewer)
  assert.equal(themeFollowsHousehold(undefined), false);
  assert.equal(themeFollowsHousehold(null), false);
});

// ---- the bill edit picker keeps a bill's own cadence ----
//
// The web keeps the same helpers in webapp/src/cadence.ts; both copies run
// every case, so an "every 4 months" bill opens on the same choice in the
// browser and on the phone.

import * as mobileCad from "../src/lib/pure.ts";
import * as webCad from "../../webapp/src/cadence.ts";

const STD_CADENCES = [["DAILY:1", "daily"], ["MONTHLY:1", "monthly"],
                      ["MONTHLY:3", "every 3 months"], ["YEARLY:1", "yearly"]];

for (const [side, m] of [["mobile", mobileCad], ["web", webCad]]) {
  test(`${side}: a custom interval reads as the bills list words it`, () => {
    assert.equal(m.cadenceWords("MONTHLY:4"), "every 4 months");
    assert.equal(m.cadenceWords("YEARLY:3"), "every 3 years");
    assert.equal(m.cadenceWords("WEEKLY:4"), "every 4 weeks");
    assert.equal(m.cadenceWords("DAILY:10"), "every 10 days");
    assert.equal(m.cadenceWords("MONTHLY:1"), "monthly");
    assert.equal(m.cadenceWords("ENVELOPE:12"), "envelope (annual pool)");
    assert.equal(m.cadenceWords("ONE_TIME:1"), "one-time");
  });

  test(`${side}: the edit picker still offers the bill's saved interval after another pick`, () => {
    // the bill is every 4 months; the person has tried "monthly"
    const opts = m.cadenceChoices(STD_CADENCES, ["MONTHLY:4", "MONTHLY:1"]);
    assert.deepEqual(opts.at(-1), ["MONTHLY:4", "every 4 months"]);
    assert.equal(opts.filter(([k]) => k === "MONTHLY:1").length, 1);
  });

  test(`${side}: a ledger-fit prefill and the saved interval are both offered`, () => {
    const opts = m.cadenceChoices(STD_CADENCES, ["YEARLY:3", "WEEKLY:4"]);
    assert.deepEqual(opts.slice(STD_CADENCES.length).map(([k]) => k),
                     ["YEARLY:3", "WEEKLY:4"]);
  });

  test(`${side}: a standard cadence adds nothing to the picker`, () => {
    assert.deepEqual(m.cadenceChoices(STD_CADENCES, ["MONTHLY:3", "", null]),
                     STD_CADENCES);
  });

  test(`${side}: the add picker can make a one-time, every-2-weeks or every-14-days bill`, () => {
    const add = m.ADD_CADENCES.map(([k]) => k);
    assert.ok(add.includes("ONE_TIME:1"), "one-time is a plain pick");
    const units = m.CUSTOM_UNITS.map(([k]) => k);
    assert.deepEqual(units, ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]);
    assert.equal(m.customCadence("WEEKLY", "2"), "WEEKLY:2");
    assert.equal(m.customCadence("DAILY", "14"), "DAILY:14");
    assert.equal(m.customCadence("MONTHLY", ""), "MONTHLY:1");
    assert.equal(m.customCadence("YEARLY", "0"), "YEARLY:1");
    // the server bounds an interval at about 20 years of its unit, so the
    // picker caps each unit there — 240 years would be refused
    assert.deepEqual(m.CUSTOM_MAX,
                     { DAILY: 7300, WEEKLY: 1043, MONTHLY: 240, YEARLY: 20 });
    assert.equal(m.customMax("YEARLY"), 20);
    assert.equal(m.customMax("weekly"), 1043);
    assert.equal(m.customMax("FORTNIGHTLY"), 20, "an unknown unit gets the smallest cap");
    assert.equal(m.customCadence("YEARLY", "20"), "YEARLY:20");
    assert.equal(m.customCadence("YEARLY", "240"), "YEARLY:20");
    assert.equal(m.customCadence("MONTHLY", "240"), "MONTHLY:240");
    assert.equal(m.customCadence("MONTHLY", "241"), "MONTHLY:240");
    assert.equal(m.customCadence("WEEKLY", "1043"), "WEEKLY:1043");
    assert.equal(m.customCadence("WEEKLY", "1044"), "WEEKLY:1043");
    assert.equal(m.customCadence("DAILY", "7300"), "DAILY:7300");
    assert.equal(m.customCadence("DAILY", "7301"), "DAILY:7300");
    assert.equal(m.customCadence("DAILY", "99999"), "DAILY:7300");
    // every add choice reads in the bills list's own words
    for (const [k, words] of m.ADD_CADENCES)
      assert.equal(m.cadenceWords(k), words, k);
    assert.equal(m.cadenceWords(m.customCadence("WEEKLY", "2")), "every 2 weeks");
    assert.equal(m.cadenceWords(m.customCadence("DAILY", "14")), "every 14 days");
  });
}

test("the two copies of the cadence picker are the same text", () => {
  const web = readFileSync(
    new URL("../../webapp/src/cadence.ts", import.meta.url), "utf8");
  const mob = readFileSync(new URL("../src/lib/pure.ts", import.meta.url), "utf8");
  for (const fn of ["cadenceWords", "cadenceChoices"]) {
    const body = (src) => {
      const i = src.indexOf(`export function ${fn}`);
      return src.slice(i, src.indexOf("\n}\n", i));
    };
    assert.notEqual(body(web).length, 0);
    assert.equal(body(mob), body(web), fn);
  }
});

test("the two copies of the add picker's cadence lists are the same text", () => {
  const web = readFileSync(
    new URL("../../webapp/src/cadence.ts", import.meta.url), "utf8");
  const mob = readFileSync(new URL("../src/lib/pure.ts", import.meta.url), "utf8");
  const body = (src, head, end) => {
    const i = src.indexOf(head);
    return i === -1 ? "" : src.slice(i, src.indexOf(end, i));
  };
  for (const [head, end] of [["export const ADD_CADENCES", "\n];\n"],
                             ["export const CUSTOM_UNITS", "\n];\n"],
                             ["export const CUSTOM_MAX", "\n};\n"],
                             ["export function customMax", "\n}\n"],
                             ["export function customCadence", "\n}\n"]]) {
    assert.notEqual(body(web, head, end).length, 0, head);
    assert.equal(body(mob, head, end), body(web, head, end), head);
  }
});

// ---- the phone adopts its zone only from a fresh /api/me ----

const hostedOwner = { role: "owner", hosted: true, timezone: "America/New_York",
                      timezone_source: "instance" };

test("zone adopt: a restored snapshot (no network answer yet) adopts nothing", () => {
  assert.equal(mobileCad.zoneToAdopt(null, "America/Denver"), null);
});

test("zone adopt: a fresh answer still on the instance zone adopts the phone's", () => {
  assert.equal(mobileCad.zoneToAdopt(hostedOwner, "America/Denver"),
               "America/Denver");
});

test("zone adopt: a fresh answer with a household-chosen zone is left alone", () => {
  assert.equal(mobileCad.zoneToAdopt(
    { ...hostedOwner, timezone_source: "household" }, "America/Denver"), null);
});

test("zone adopt: waits out the second-factor lockout, then adopts on the next fresh answer", () => {
  // the server refuses every settings write until a factor is enrolled,
  // and a refusal is final for the launch — so no attempt is spent there
  assert.equal(mobileCad.zoneToAdopt({ ...hostedOwner, needs_2fa: true },
                                     "America/Denver"), null);
  assert.equal(mobileCad.zoneToAdopt({ ...hostedOwner, needs_2fa: false },
                                     "America/Denver"), "America/Denver");
});

test("zone adopt: a refusal is final, a dropped connection retries a few times", () => {
  assert.equal(mobileCad.retriesZoneAdopt(403, 1), false);
  assert.equal(mobileCad.retriesZoneAdopt(409, 1), false);
  assert.equal(mobileCad.retriesZoneAdopt(0, 1), true);
  assert.equal(mobileCad.retriesZoneAdopt(503, 2), true);
  assert.equal(mobileCad.retriesZoneAdopt(0, mobileCad.ZONE_ADOPT_TRIES), false);
});

// ---- the shared demonstration login: the controls its doors refuse are
// switched off with the refusal's own sentence; nobody else's are ----
test("sharedLock: only the shared login, and never on a demo", () => {
  const { sharedLock, SHARED_LOGIN_NOTE, SHARED_GRANT_NOTE,
          SHARED_CREDENTIAL_NOTE } = pureModule;
  assert.equal(sharedLock({ shared_login: true }, "login"),
               SHARED_LOGIN_NOTE);
  assert.equal(sharedLock({ shared_login: true }, "grant"),
               SHARED_GRANT_NOTE);
  assert.equal(sharedLock({ shared_login: true }, "credential"),
               SHARED_CREDENTIAL_NOTE);
  // an ordinary login, an older server without the flag, and /api/me
  // still loading all leave the control live
  assert.equal(sharedLock({ shared_login: false }, "login"), null);
  assert.equal(sharedLock({}, "grant"), null);
  assert.equal(sharedLock(undefined, "grant"), null);
  // a demo instance greys the whole screen under its own banner already
  assert.equal(sharedLock({ demo: true, shared_login: true }, "login"),
               null);
});

// ---- one edit of the daily-mail list posts the field it changed and
// nothing that rode along from an older copy ----
test("recipientEditBody: a mute names its address and never the schedule", () => {
  const { recipientEditBody } = pureModule;
  const on = recipientEditBody({ kind: "mute", email: "B@example.dev",
                                 on: true, muted: ["a@example.dev"] });
  // the delta is what a current server applies; the list beside it is
  // the fallback an older server understands, built from this copy
  assert.deepEqual(on, { email_muted_add: ["b@example.dev"],
                         email_muted: ["a@example.dev", "b@example.dev"] });
  const off = recipientEditBody({ kind: "mute", email: "A@example.dev",
                                  on: false,
                                  muted: ["A@example.dev", "c@example.dev"] });
  assert.deepEqual(off, { email_muted_remove: ["a@example.dev"],
                          email_muted: ["c@example.dev"] });
  assert.ok(!("email_schedule" in on) && !("email_recipients" in on));
});

test("recipientEditBody: add and remove name one address beside the fallback list", () => {
  const { recipientEditBody } = pureModule;
  const add = recipientEditBody({ kind: "add", email: "n@example.dev",
                                  recipients: ["a@example.dev"] });
  assert.deepEqual(add, { email_recipients_added: ["n@example.dev"],
                          email_recipients: ["a@example.dev", "n@example.dev"] });
  const rm = recipientEditBody({ kind: "remove", email: "A@example.dev",
                                 recipients: ["a@example.dev", "c@example.dev"] });
  assert.deepEqual(rm, { email_recipients_removed: ["A@example.dev"],
                         email_recipients: ["c@example.dev"] });
  for (const b of [add, rm]) {
    assert.ok(!("email_muted" in b));
    assert.ok(!("email_schedule" in b));
    assert.ok(!("timezone" in b));
  }
});

// ---- the bill screen reports the state the server settled on ----
test("bill toggle note names the resulting state", () => {
  assert.match(billToggleNote(true), /^Disabled/);
  assert.match(billToggleNote(true), /excluded from the budget/);
  assert.match(billToggleNote(false), /^Enabled/);
  assert.notEqual(billToggleNote(true), billToggleNote(false));
});

// ---- a missed bill: overdue unless the bank feed withdrew its charge,
// which is a warning with the withdrawal date, never the red overdue ----
import { missedBillPill } from "../src/lib/pure.ts";

test("missed bill: no withdrawal is the red overdue pill", () => {
  assert.deepEqual(missedBillPill(null),
                   { text: "overdue", tone: "bad", note: null });
  assert.deepEqual(missedBillPill(undefined),
                   { text: "overdue", tone: "bad", note: null });
});

test("missed bill: a withdrawn charge is an amber pill with its date", () => {
  assert.deepEqual(missedBillPill("2026-09-22"), {
    text: "withdrawn", tone: "warn",
    note: "charge withdrawn by the bank feed (09/22/26)" });
});

// ---- one-time code refusals: a code from a spent step is not a clock
// problem, and must never be reported as one ----
import { classifyChange, oneTimeCodeSentence, svgFromDataUri }
  from "../src/lib/pure.ts";

test("one-time code: already used asks for the next code", () => {
  const t = oneTimeCodeSentence(
    '401: {"detail":"code already used — enter the next code"}');
  assert.match(t, /already used/);
  assert.doesNotMatch(t, /clock/);
  // the server's own wording carries a machine token and "been"
  assert.doesNotMatch(oneTimeCodeSentence('401: {"detail":"code_used: that '
    + 'code has already been used — wait for the next code"}'), /clock/);
  // even when the server's sentence also names the one-time code
  assert.doesNotMatch(oneTimeCodeSentence("one-time code already used"),
                      /clock/);
});

test("one-time code: a mismatch is still the clock sentence", () => {
  assert.match(oneTimeCodeSentence("401: bad one-time code"), /clock/);
  assert.equal(oneTimeCodeSentence("wrong password"), null);
});

// ---- the enrolment QR arrives as an SVG data URI the native image views
// cannot draw; the markup is unpacked and given a viewBox to scale ----
test("svg data uri: percent-encoded markup gains a viewBox", () => {
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' width='265' "
    + "height='265'><path d='M0 0h5v5H0z'/></svg>";
  const out = svgFromDataUri(
    "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg));
  assert.ok(out.startsWith('<svg viewBox="0 0 265 265"'));
  assert.ok(out.includes("<path d='M0 0h5v5H0z'/>"));
});

test("svg data uri: base64 decodes; an existing viewBox is kept", () => {
  const svg = '<svg viewBox="0 0 10 10" width="10" height="10"></svg>';
  assert.equal(svgFromDataUri("data:image/svg+xml;base64,"
                              + Buffer.from(svg).toString("base64")), svg);
});

test("svg data uri: anything else is null", () => {
  assert.equal(svgFromDataUri("data:image/png;base64,AAAA"), null);
  assert.equal(svgFromDataUri("https://example.dev/qr.svg"), null);
  assert.equal(svgFromDataUri("data:image/svg+xml,%E0%A4%A"), null);
});

// ---- an account editor's classify write carries the anchor only when
// its switch moved; a kind-only save leaves the household's pin alone ----
test("classify: kind-only change omits the primary flag", () => {
  const w = classifyChange("depository/checking", "depository/savings", null);
  assert.deepEqual(w, { kind: "depository/savings" });
  assert.ok(!("primary" in w));
});

test("classify: a moved switch is sent, nothing changed is no write", () => {
  assert.deepEqual(classifyChange("depository/checking",
                                  "depository/checking", true),
                   { kind: "depository/checking", primary: true });
  assert.deepEqual(classifyChange("depository/checking",
                                  "depository/checking", false),
                   { kind: "depository/checking", primary: false });
  assert.equal(classifyChange("credit", "credit", null), null);
});

// ---- the recipient add box, the Resend reply, the debt plan's saving ----
//
// The web keeps the same rules in webapp/src/recipients.ts and
// webapp/src/moneymath.ts; both copies run every case.

import * as mobileRecip from "../src/lib/pure.ts";
import * as webRecip from "../../webapp/src/recipients.ts";
import * as webMoney from "../../webapp/src/moneymath.ts";

for (const [side, m] of [["mobile", mobileRecip], ["web", webRecip]]) {
  test(`${side}: the add box takes a single-label domain the server accepts`, () => {
    // a LAN relay's dotless domain is a real mailbox to the server
    assert.equal(m.looksLikeMailbox("partner@relay"), true);
    assert.equal(m.looksLikeMailbox("a@b.example"), true);
    for (const bad of ["partner", "a b@x.example", "a@", "@x.example",
                       "a@x..example", "a@.x", "a@x."])
      assert.equal(m.looksLikeMailbox(bad), false, bad);
  });

  test(`${side}: the owner's own address is refused with the web's words`, () => {
    assert.equal(m.recipientDuplicate("Me@X.example", [], "me@x.example"),
                 "That's you — you're already on the list.");
    assert.equal(m.recipientDuplicate("p@x.example", ["P@x.example"], null),
                 "Already on the list.");
    assert.equal(m.recipientDuplicate("q@x.example", ["p@x.example"],
                                      "me@x.example"), null);
  });

  test(`${side}: Resend claims a send only when the server made one`, () => {
    // an older server leaves `sent` out and always sent
    assert.equal(m.resendHeld({ ok: true }, "p@x.example"), null);
    assert.equal(m.resendHeld({ sent: true }, "p@x.example"), null);
    assert.match(m.resendHeld({ sent: false, reason: "cooldown" }, "p@x.example"),
                 /a few minutes ago/);
    assert.match(m.resendHeld({ sent: false, reason: "cap" }, "p@x.example"),
                 /Too many/);
    assert.equal(m.resendHeld({ sent: false, reason: "held" }, "p@x.example"),
                 "The invitation to p@x.example was not sent.");
  });

  test(`${side}: a member's card says they receive it only when the worker mails them`, () => {
    const base = { me: "Kid@x.example", on: true, muted: false, prefs: {},
                   verified: true, hosted: false };
    const owner = { email: "o@x.example", verified: true, invite: null };
    // settings not loaded yet, or an older server: on and not muted
    assert.deepEqual(m.myDailyLine(base),
                     { receiving: true, text: "You receive it." });
    assert.match(m.myDailyLine({ ...base, muted: true }).text, /muted/);
    assert.equal(m.myDailyLine({ ...base, on: false }).text, "Off.");
    assert.match(m.myDailyLine({ ...base, prefs: { daily: false } }).text,
                 /turned the daily email off/);
    // a saved list the member is not on
    const off = m.myDailyLine({ ...base, recipients: ["p@x.example"],
      status: [owner, { email: "p@x.example", invite: "accepted" }] });
    assert.equal(off.receiving, false);
    assert.match(off.text, /doesn't include you/);
    // on the list, invitation unanswered; and unsendable without a base URL
    const listed = { ...base, recipients: ["kid@x.example"],
      status: [owner, { email: "kid@x.example", invite: "invited" }] };
    assert.match(m.myDailyLine(listed).text, /Accept the invitation/);
    assert.match(m.myDailyLine({ ...listed, needBaseUrl: true }).text,
                 /OIKONOME_BASE_URL/);
    assert.equal(m.myDailyLine({ ...listed, status: [owner,
      { email: "KID@x.example", invite: "accepted" }] }).receiving, true);
    // no list: unverified gets nothing on hosted, or once anyone verified
    assert.match(m.myDailyLine({ ...base, verified: false, hosted: true }).text,
                 /Confirm your email/);
    assert.match(m.myDailyLine({ ...base, verified: false, recipients: [],
                                 status: [owner] }).text, /Confirm your email/);
    // self-host where nobody ever verified: everyone is mailed
    assert.equal(m.myDailyLine({ ...base, verified: false, recipients: [],
      status: [{ ...owner, verified: false }] }).receiving, true);
    // a held address says so
    assert.match(m.myDailyLine({ ...base, status: [owner,
      { email: "kid@x.example", delivery: "bouncing" }] }).text, /Held/);
  });

  test(`${side}: a member's card reads the server's list facts, not the roster it is no longer shown`, () => {
    // only what the server sends a member: their own row or none. Every
    // stored shape against the mailer itself is the server suite's
    // test_member_mail_card_matches_the_mailer; this pins the branches.
    const base = { me: "kid@x.example", on: true, muted: false, prefs: {},
                   verified: true, hosted: false };
    // a list set without them: no row, an empty roster, yet a list
    const left = m.myDailyLine({ ...base, recipients: [], status: [],
      listConfigured: true, onList: false });
    assert.equal(left.receiving, false);
    assert.match(left.text, /doesn't include you/);
    // nothing configured: the verified-login rule, whatever the roster says
    assert.match(m.myDailyLine({ ...base, verified: false, hosted: true,
      recipients: [], status: [], listConfigured: false,
      onList: false }).text, /Confirm your email/);
  });

  test(`${side}: the add box names the missing base URL`, () => {
    assert.match(m.NEED_BASE_URL, /OIKONOME_BASE_URL/);
  });
}

test("the claim age is answered in the walkthrough, never saved from the what-if panel", () => {
  // only a marked save (answers_v: 2) is an input to the projection, and
  // the adjust panel is a what-if: it saves nothing
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, page, wiz] of [
      ["web", read("../../webapp/src/pages/Retirement.tsx"),
       read("../../webapp/src/components/RetireWizard.tsx")],
      ["mobile", read("../src/app/retire.tsx"), read("../src/app/retire.tsx")]]) {
    assert.doesNotMatch(page, /ret_wizard_answers: \{ ssage/, side);
    assert.doesNotMatch(page, /ssAgeErr/, side);
    assert.match(wiz, /Social Security claim age/, side);
    assert.ok([...page.matchAll(/ret_wizard_answers:/g)].length
              <= [...page.matchAll(/ret_answers_v: 2/g)].length, side);
  }
  const webWiz = read("../../webapp/src/components/RetireWizard.tsx");
  assert.match(webWiz, /ret_answers_v = 2/);
  assert.match(webWiz, /assumptions: \["ret", "infl", "end", "ssage"\]/);
});

test("both retirement screens ask for a saving the household has not set", () => {
  // no saved saving answer: the server projects $0 new saving and names
  // the source "none"; each screen says so and offers a box that saves
  // the answer, as it does for a spend it still needs
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, src] of [
      ["web", read("../../webapp/src/pages/Retirement.tsx")],
      ["mobile", read("../src/app/retire.tsx")]]) {
    assert.match(src, /saving not set — add it/, side);
    assert.match(src, /function SavingAsk/, side);
    assert.match(src, /saving_source === "none"/, side);
    assert.match(src, /ret_wizard_answers: \{ employer_mo/, side);
  }
});

test("both debt screens show the server's differences and estimate flags", () => {
  // savings vs minimums and the other method's difference are server
  // fields (null where one side never finishes), and "estimated" is the
  // server's flag: a client subtraction or an apr_source test would let
  // web and phone disagree, or quote a fifty-year cut-off as a saving
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, src] of [
      ["web", read("../../webapp/src/pages/Debt.tsx")],
      ["mobile", read("../src/app/debt.tsx")]]) {
    assert.match(src, /saves_vs_minimums/, side);
    assert.match(src, /alt_extra_interest/, side);
    assert.match(src, /plan\.starting/, side);
    assert.doesNotMatch(src, /interestSaved|total_interest\s*-\s*\w+\.total_interest/, side);
    assert.doesNotMatch(src, /apr_source === "typical"\)/, side);
    assert.doesNotMatch(src, /min_issuer \?\?/, side);
  }
  for (const [side, m] of [["mobile", mobileRecip], ["web", webMoney]])
    assert.equal(m.interestSaved, undefined, side);
});

// ---- a step-up refusal is a 401 too, and must not read as a lost session:
// the phone would wipe its token and the web would blank to login, both
// swallowing the message the form was about to show. The web keeps the same
// list in webapp/src/reauth.ts; both copies run every case. ----
import * as mobileReauth from "../src/lib/pure.ts";
import * as webReauth from "../../webapp/src/reauth.ts";

for (const [side, m] of [["mobile", mobileReauth], ["web", webReauth]]) {
  test(`${side}: a spent one-time code is a re-auth reply, not a lost session`, () => {
    assert.ok(m.isReauthReply(
      "code_used: that code has already been used — wait for the next code in your authenticator app"));
    // the reworded server message, which already-shipped clients rely on
    assert.ok(m.isReauthReply(
      "code_used: that one-time code was already used — wait for the next one"));
    assert.ok(m.isReauthReply('{"detail":"code_used: that code has already been used"}'));
  });

  test(`${side}: the other step-up refusals stay re-auth replies`, () => {
    for (const d of ["totp_required", "recovery_required", "password_required",
                     "wrong password", "bad one-time code",
                     "current one-time code required", "bad credentials",
                     "passkey verification failed"])
      assert.ok(m.isReauthReply(d), d);
  });

  test(`${side}: a plain signed-out 401 is a lost session`, () => {
    assert.equal(m.isReauthReply("not signed in"), false);
    assert.equal(m.isReauthReply("Unauthorized"), false);
    assert.equal(m.isReauthReply('{"detail":"session expired"}'), false);
  });

  test(`${side}: the server's reauth marker wins over the wording`, () => {
    // top-level and inside FastAPI's detail wrapper, as text or parsed
    assert.ok(m.isReauthReply('{"detail":"try again","error":"reauth"}'));
    assert.ok(m.isReauthReply('{"detail":{"error":"reauth","message":"x"}}'));
    assert.ok(m.isReauthReply("try again", { error: "reauth" }));
    assert.ok(m.isReauthReply("x", { detail: { error: "reauth" } }));
    // another marker, or none, falls back to the wording
    assert.equal(m.isReauthReply("x", { error: "elevation_required" }), false);
    assert.equal(m.isReauthReply('{"error":"session"}'), false);
    assert.equal(m.isReauthReply("not json {", null), false);
  });
}

test("a 401 code_used keeps the phone signed in; a bare 401 does not",
     async () => {
  // pinning the helper alone would still let checkAuth go back to its own
  // inline list
  const real = globalThis.fetch;
  let lost = 0;
  const c = makeClient("https://example.invalid", "tok", () => { lost += 1; });
  try {
    globalThis.fetch = async () => new Response(
      JSON.stringify({ detail: "code_used: that code has already been used — wait for the next code in your authenticator app" }),
      { status: 401, headers: { "content-type": "application/json" } });
    await assert.rejects(() => c.me(), (e) => e instanceof ApiError && e.status === 401);
    assert.equal(lost, 0, "a spent code must not drop the session");
    // a refusal whose sentence names nothing on the list, carrying the marker
    globalThis.fetch = async () => new Response(
      JSON.stringify({ detail: { error: "reauth", message: "try again" } }),
      { status: 401, headers: { "content-type": "application/json" } });
    await assert.rejects(() => c.me());
    assert.equal(lost, 0, "the reauth marker must not drop the session");
    globalThis.fetch = async () => new Response(
      JSON.stringify({ detail: "not signed in" }),
      { status: 401, headers: { "content-type": "application/json" } });
    await assert.rejects(() => c.me());
    assert.equal(lost, 1);
  } finally { globalThis.fetch = real; }
});

// ---- where a tapped alert goes: the server's own link when the alert has
// one, the web's kind map for history rows ----
import { alertCta, alertRoute } from "../src/lib/alert-routes.ts";

test("alerts: what needs attention is split from the history it sits in", () => {
  const rows = [
    { kind: "drift", active: 1, dismissed: 0 },
    { kind: "merges", active: 1, dismissed: 1 },
    { kind: "proposals", active: 0, dismissed: 1 },
    { kind: "anomaly", active: 0, dismissed: 0 },
  ];
  const s = alertSections(rows);
  assert.deepEqual(s.active.map((r) => r.kind), ["drift"]);
  assert.deepEqual(s.dismissed.map((r) => r.kind), ["merges"]);
  assert.deepEqual(s.resolved.map((r) => r.kind), ["proposals", "anomaly"]);
  assert.equal(s.history.length, 4);
  assert.equal(historyToggleLabel(false, 4), "Show history (4 alerts)");
  assert.equal(historyToggleLabel(false, 1), "Show history (1 alert)");
  assert.equal(historyToggleLabel(true, 4), "Hide history");
});

test("an active alert on the Alerts screen keeps its own target from Today's live alert", () => {
  const rows = [
    { kind: "drift", message: "Plan updated", active: 1, dismissed: 0 },
    { kind: "drift", message: "Plan updated", active: 0, dismissed: 0 },
    { kind: "anomaly", message: "Large charge", active: 1, dismissed: 0 },
  ];
  const live = [{ kind: "drift", message: "Plan updated",
                  link: "/bills/history?payee=Example%20Stream" }];
  const out = withLiveLinks(rows, live);
  assert.equal(out[0].link, "/bills/history?payee=Example%20Stream");
  assert.equal(out[1].link, undefined);      // a resolved row is not live
  assert.equal(out[2].link, undefined);      // no live twin: the kind's page
  assert.equal(withLiveLinks(rows, undefined), rows);
});

test("alerts: a live alert follows its own link to the native screen", () => {
  // the finder lives on Bills, which is where the setup alert says to go
  assert.equal(alertRoute({ kind: "setup", link: "/bills" }), "/bills");
  assert.equal(alertRoute({ kind: "llm-backend", link: "/doctor" }), "/doctor");
  assert.equal(alertRoute({ kind: "collector-token-revoked",
                            link: "/settings/scripts" }), "/settings");
  assert.equal(alertRoute({ kind: "drift",
                            link: "/bills/history?payee=Acme%20Power" }),
               "/bill-history?payee=Acme%20Power");
  // a charge's alert opens the search for that charge
  assert.equal(alertRoute({ kind: "anomaly",
                            link: "/transactions?q=Acme%20Tools" }),
               "/transactions?q=Acme%20Tools");
  assert.equal(alertRoute({ kind: "anomaly", link: "/transactions" }),
               "/transactions");
  // a double charge opens on its own days, not on every charge it ever made
  assert.equal(alertRoute({ kind: "anomaly",
                            link: "/transactions?q=Corner%20Cafe"
                              + "&date_from=2026-12-30&date_to=2027-01-02" }),
               "/transactions?q=Corner%20Cafe"
                 + "&date_from=2026-12-30&date_to=2027-01-02");
  // a web-only path falls back to the kind map, never a missing route
  assert.equal(alertRoute({ kind: "stale-pull", link: "/somewhere-web-only" }),
               "/accounts");
});

test("alerts: history rows (no link) route by kind, as the web does", () => {
  for (const [kind, want] of [
    ["setup", "/bills"], ["stale-pull", "/accounts"],
    ["connection-removed", "/accounts"], ["proposals", "/bills"],
    ["drift", "/bills"], ["merges", "/merchants"], ["reimb", "/reimburse"],
    ["funding", "/networth"], ["transfer", "/transactions"],
    ["anomaly", "/transactions"], ["bonus", "/transactions"],
    ["llm-backend", "/doctor"], ["collector-token-revoked", "/settings"]])
    assert.equal(alertRoute({ kind }), want, kind);
  assert.equal(alertRoute({ kind: "unheard-of" }), null);
  // a queue is reviewed, breakage is fixed — the web's verbs
  assert.equal(alertCta("proposals"), "Review");
  assert.equal(alertCta("stale-pull"), "Fix");
});

// ---- the email schedule: an edit is laid over what is stored now, the
// daily email defaults on, and a reply without the schedule never reads
// as everything switched off ----
import { cadenceOf, rebaseSched, storedSched } from "../src/lib/schedule.ts";

test("schedule: an unset daily email is on, the other reports off", () => {
  assert.equal(cadenceOf(null, "daily").on, true);
  assert.equal(cadenceOf({}, "monthly").on, false);
  assert.equal(cadenceOf({}, "yearly").on, false);
  assert.equal(cadenceOf({ daily: { on: false, hour: 9 } }, "daily").on, false);
});

test("schedule: a daily change keeps the reports stored since", () => {
  const live = { daily: { on: true, hour: 7, sms: true },
                 monthly: { on: true, hour: 8, sms: true },
                 yearly: { on: true, hour: 7 } };
  const base = { daily: { on: true, push: false, hour: 7, summary: false } };
  const sent = rebaseSched({ daily: { ...base.daily, hour: 6 } }, base, live,
                           "daily");
  assert.deepEqual(sent.monthly, live.monthly);
  assert.deepEqual(sent.yearly, live.yearly);
  assert.equal(sent.daily.hour, 6);
  assert.equal(sent.daily.sms, true);
});

test("schedule: the edited cadence is always sent", () => {
  // nothing stored and the tap matched what was shown: the cadence must
  // still be in the body, or the server stores it as off
  const base = { daily: { on: true, push: false, hour: 7, summary: false } };
  const sent = rebaseSched({ daily: { ...base.daily } }, base, {}, "daily");
  assert.equal(sent.daily.on, true);
});

test("schedule: a reply without the schedule keeps what was sent", () => {
  const sent = { daily: { on: true, hour: 7 } };
  assert.deepEqual(storedSched({ email_schedule: null }, sent), sent);
  assert.deepEqual(storedSched({}, sent), sent);
  const r = { daily: { on: false, hour: 7 } };
  assert.deepEqual(storedSched({ email_schedule: r }, sent), r);
});

// ---- the budget planner: a saved Savings plan is not taken out of
// Everything else again on reopen ----
import { plannerUnallocAuto } from "../src/lib/planner.ts";

test("planner: reopening a saved plan leaves Everything else as saved", () => {
  // saved other_monthly 1500 (already net of a 500 plan), no carve-outs
  assert.equal(plannerUnallocAuto({ otherBase: 1500, catsTotal: 0,
    savingsTouched: true, savingsTyped: 500, savingsSeed: 500 }), 1500);
  // carve-outs still come out
  assert.equal(plannerUnallocAuto({ otherBase: 1500, catsTotal: 300,
    savingsTouched: true, savingsTyped: 500, savingsSeed: 500 }), 1200);
});

test("planner: only the change typed this session moves the pool", () => {
  assert.equal(plannerUnallocAuto({ otherBase: 1500, catsTotal: 0,
    savingsTouched: true, savingsTyped: 700, savingsSeed: 500 }), 1300);
  // a first plan (nothing saved) comes out whole
  assert.equal(plannerUnallocAuto({ otherBase: 2000, catsTotal: 0,
    savingsTouched: true, savingsTyped: 500, savingsSeed: 0 }), 1500);
  assert.equal(plannerUnallocAuto({ otherBase: 2000, catsTotal: 0,
    savingsTouched: false, savingsTyped: 0, savingsSeed: 0 }), 2000);
});

// ---- the budget planner's excess is the server's, moved only by what
// is typed and not yet saved ----
import { plannerPreview } from "../src/lib/planner.ts";

const savedPlan = { planSurplus: 1200, savingsPlan: 500,
  savedVariable: 1500, typedVariable: 1500, savedGoal: 300, typedGoal: 300,
  goalCounted: true, savedIncome: 0, typedIncome: 0, avgBills: 1800,
  incomeTyped: false };

test("planner: with nothing typed the excess is the server's figure", () => {
  assert.deepEqual(plannerPreview(savedPlan), { excess: 1200, saved: 500 });
});

test("planner: a typed change moves the excess by that change only", () => {
  // raising the owned goal by 100 and spending by 50
  assert.deepEqual(plannerPreview({ ...savedPlan, typedGoal: 400,
    typedVariable: 1550 }), { excess: 1050, saved: 600 });
  // a goal the excess does not count (a sweep) moves neither figure
  assert.deepEqual(plannerPreview({ ...savedPlan, typedGoal: 400,
    goalCounted: false }), { excess: 1200, saved: 500 });
  // a typed income moves it by its own change
  assert.deepEqual(plannerPreview({ ...savedPlan, savedIncome: 6000,
    typedIncome: 6250 }).excess, 1450);
});

test("planner: no server excess and no income box means no preview", () => {
  assert.equal(plannerPreview({ ...savedPlan, planSurplus: null }).excess,
               null);
});

test("planner: a first plan previews from the typed income", () => {
  // nothing saved yet: typed income − the server's bills − the typed
  // spending − every goal's plan (the owned goal as typed)
  assert.deepEqual(plannerPreview({ ...savedPlan, planSurplus: null,
    incomeTyped: true, typedIncome: 5000, typedVariable: 1500,
    savedGoal: 0, typedGoal: 300, savingsPlan: 200 }),
    { excess: 1200, saved: 500 });
});

// ---- settings: the legacy AI endpoint row keeps its vision model, and
// the Enrich cap shown is the one the server stored ----
import { llmBackendsSeed, storedEnrichCap } from "../src/lib/settings-pure.ts";

test("settings: the legacy endpoint row carries the vision model", () => {
  const [row] = llmBackendsSeed({ llm_url: "http://x:11434/v1",
    llm_model: "chat", llm_vision_model: "vl", llm_api_key_set: true,
    llm_extra_body_set: true });
  assert.equal(row.id, "legacy");
  assert.equal(row.vision_model, "vl");
  assert.equal(row.extra_body, "");      // "" keeps the stored JSON
  assert.equal(row.extra_body_set, true);
  assert.equal(row.api_key_set, true);
  assert.deepEqual(llmBackendsSeed({}), []);
  const named = [{ id: "a", name: "A", url: "u", model: "m" }];
  assert.deepEqual(llmBackendsSeed({ llm_backends: named, llm_url: "x" }), named);
});

test("settings: the Enrich cap is the server's clamped value", () => {
  assert.equal(storedEnrichCap({ plaid_enrich_cap: 2000 }, 10000), 2000);
  assert.equal(storedEnrichCap({}, 250), 250);
  assert.equal(storedEnrichCap(null, 250), 250);
});

// ---- cash flow's best month: a "+" only on a gain ----
import { savedFigure } from "../src/lib/pure.ts";

test("cash flow: an all-negative range's best month is not '+-$X'", () => {
  assert.deepEqual(savedFigure(-40), { text: "-$40", tone: "bad" });
  assert.deepEqual(savedFigure(1200), { text: "+$1,200", tone: "good" });
  assert.deepEqual(savedFigure(0), { text: "$0", tone: "plain" });
  assert.deepEqual(savedFigure(-0.4), { text: "$0", tone: "plain" });
});

// ---- push taps open the period the report covers ----
import { pushRoute, pushSentAt } from "../src/lib/pure.ts";

test("push tap: the monthly report opens last month, the yearly last year", () => {
  // sent on the 1st: the report is about the month that just ended
  assert.equal(pushRoute("monthly", {}, new Date(2026, 9, 1, 8)), "/month?y=2026&m=9");
  // January's report is December of the year before
  assert.equal(pushRoute("monthly", undefined, new Date(2027, 0, 1, 8)), "/month?y=2026&m=12");
  assert.equal(pushRoute("yearly", {}, new Date(2027, 0, 1, 8)), "/year?y=2026");
  // a payload that names the period wins
  assert.equal(pushRoute("monthly", { y: 2025, m: "7" }, new Date(2026, 9, 1)), "/month?y=2025&m=7");
  assert.equal(pushRoute("yearly", { y: "2024" }, new Date(2027, 0, 1)), "/year?y=2024");
  // a malformed period falls back rather than opening month 13
  assert.equal(pushRoute("monthly", { y: 2026, m: 13 }, new Date(2026, 9, 1)), "/month?y=2026&m=9");
  assert.equal(pushRoute("alert", {}, new Date()), "/alerts");
  // a security change opens the screen that can undo it, not Alerts
  assert.equal(pushRoute("security", {}, new Date()), "/security");
  assert.equal(pureModule.pushTapRoute(pushRoute("security", {}, new Date()),
    { factor_reset_pending: null }), "/security");
  assert.equal(pushRoute("daily", {}, new Date()), "/");
  assert.equal(pushRoute("weekly", {}, new Date()), "/");   // retired kind: home
});

test("push tap: the sent date is read in seconds (iOS) or milliseconds (Android)", () => {
  const now = new Date(2026, 9, 2, 9);
  const sent = new Date(2026, 9, 1, 8);
  assert.equal(pushSentAt(sent.getTime() / 1000, now).getTime(), sent.getTime());
  assert.equal(pushSentAt(sent.getTime(), now).getTime(), sent.getTime());
  // an iOS seconds value must not open December 1969
  assert.equal(pushRoute("monthly", {}, pushSentAt(sent.getTime() / 1000, now)), "/month?y=2026&m=9");
  // unset, zero, garbage and far-future dates fall back to the tap
  for (const bad of [undefined, 0, -5, NaN, "x", 42, now.getTime() * 10])
    assert.equal(pushSentAt(bad, now).getTime(), now.getTime());
  // the payload's own period still wins over any date
  assert.equal(pushRoute("monthly", { y: 2025, m: 3 }, pushSentAt(42, now)), "/month?y=2025&m=3");
});

test("push tap: the period is read on the household's clock, not the phone's", () => {
  // 03:00 UTC on Oct 1 is still Sep 30 in Los Angeles: the household's
  // monthly report (sent on ITS 1st) has not gone out yet, so a push sent
  // then belongs to the report before — August — whatever zone the phone is in
  const sent = new Date(Date.UTC(2026, 9, 1, 3));
  assert.equal(pushRoute("monthly", {}, sent, "America/Los_Angeles"), "/month?y=2026&m=8");
  assert.equal(pushRoute("monthly", {}, sent, "Europe/Berlin"), "/month?y=2026&m=9");
  // the yearly report across New Year, both sides of the date line
  const ny = new Date(Date.UTC(2027, 0, 1, 2));
  assert.equal(pushRoute("yearly", {}, ny, "America/New_York"), "/year?y=2025");
  assert.equal(pushRoute("yearly", {}, ny, "Asia/Tokyo"), "/year?y=2026");
  // an unknown zone falls back to the device clock rather than throwing
  const local = new Date(2026, 9, 1, 8);
  assert.equal(pushRoute("monthly", {}, local, "Not/AZone"), "/month?y=2026&m=9");
  // the payload's period still wins over any zone
  assert.equal(pushRoute("monthly", { y: 2026, m: 9 }, sent, "America/Los_Angeles"), "/month?y=2026&m=9");
});

// ---- what a removal says ----
// A purge keeps the rows the bank feed relies on and says so in its own
// note; that sentence must reach the person instead of a bare "Removed."
import { accountRemoveNote, keptRowsNote, keptRowsQuestion, purgeKeptRows }
  from "../src/lib/pure.ts";

test("a purge that kept rows says so", () => {
  const kept = "2 rows were kept: the bank feed already relies on them.";
  assert.equal(accountRemoveNote({ note: "", purged: { note: kept } }), kept);
  assert.equal(accountRemoveNote({ note: "Disconnected.", purged: { note: kept } }),
               `Disconnected. ${kept}`);
  // a server that already folded the note in is not repeated
  assert.equal(accountRemoveNote({ note: `Done. ${kept}`, purged: { note: kept } }),
               `Done. ${kept}`);
  assert.equal(accountRemoveNote({ note: "", purged: null }), "Removed.");
  assert.equal(accountRemoveNote({ note: "", plaid_released: true }),
               "Removed. Plaid connection released.");
  assert.ok(purgeKeptRows({ purged: { kept_transactions: 2 } }));
  assert.ok(!purgeKeptRows({ purged: { kept_transactions: 0 } }));
  assert.ok(!purgeKeptRows({ purged: null }));
});

test("a delete that would keep rows only a note holds asks first", () => {
  const q = "1 imported row on this account may stand in for charges a "
    + "bank feed set aside.";
  const body = { detail: q, error: "kept_rows_unconfirmed",
                 kept_transactions: 1 };
  assert.equal(keptRowsQuestion(new ApiError(409, q, body)), q);
  // any other refusal is shown as itself, never as the question
  assert.equal(keptRowsQuestion(new ApiError(409, "a sync is running",
                                             { detail: "a sync is running" })),
               null);
  assert.equal(keptRowsQuestion(new ApiError(400, q, body)), null);
  assert.equal(keptRowsQuestion(new Error("409: boom")), null);
  assert.equal(keptRowsQuestion(null), null);
});

// ---- who receives which kind of email: the grid's rules, both copies ----
for (const [side, m] of [["mobile", mobileRecip], ["web", webRecip]]) {
  test(`${side}: the grid's columns are the server's kinds`, () => {
    assert.deepEqual([...m.EMAIL_KINDS],
                     ["daily", "monthly", "yearly", "alerts"]);
    for (const k of m.EMAIL_KINDS) assert.ok(m.EMAIL_KIND_LABEL[k], k);
  });

  test(`${side}: a row without prefs receives everything`, () => {
    assert.equal(m.prefOn(undefined, "weekly"), true);
    assert.equal(m.prefOn({}, "alerts"), true);
    assert.equal(m.prefOn({ weekly: false }, "weekly"), false);
    assert.equal(m.prefOn({ weekly: false }, "daily"), true);
  });

  test(`${side}: a cadence the household does not send dims its column`, () => {
    const sending = { daily: true, weekly: false, monthly: false };
    assert.equal(m.kindOffNote("daily", sending), null);
    assert.equal(m.kindOffNote("weekly", sending), "weekly report is off");
    assert.equal(m.kindOffNote("yearly", sending), "year in review is off");
    assert.equal(m.kindOffNote("daily", {}), "daily email is off");
    // alerts have no schedule
    assert.equal(m.kindOffNote("alerts", {}), null);
  });

  test(`${side}: a tap flips one box on the matching row only`, () => {
    const rows = [{ email: "Owner@X.example", owner: true },
                  { email: "p@x.example", prefs: { daily: false } }];
    const out = m.withPref(rows, "owner@x.example", "weekly", false);
    assert.deepEqual(out[0].prefs, { weekly: false });
    assert.equal(out[0].owner, true);
    assert.strictEqual(out[1], rows[1]);
    assert.deepEqual(m.withPref(out, "P@X.example", "daily", true)[1].prefs,
                     { daily: true });
    // the input is never changed in place
    assert.equal(rows[0].prefs, undefined);
  });
}

import { routeInt, routeYm, smsPauseSentence } from "../src/lib/pure.ts";

test("a deep link's month and year open in range or on today", () => {
  const now = new Date(2026, 9, 3);              // October 2026
  assert.deepEqual(routeYm("2025", "3", now), { y: 2025, m: 3 });
  // out of range, negative, fractional, junk and missing all fall back
  for (const m of ["13", "-1", "0", "2.5", "1e1", "x", "", undefined, null])
    assert.equal(routeYm("2025", m, now).m, 10, String(m));
  for (const y of ["99999", "1899", "2101", "20.5", "abc", undefined])
    assert.equal(routeYm(y, "3", now).y, 2026, String(y));
  // a repeated param arrives as a list; its first value counts
  assert.deepEqual(routeYm(["2024", "1999"], ["12", "1"], now), { y: 2024, m: 12 });
  assert.equal(routeInt(" 7 ", 1, 12), 7);
  assert.equal(routeInt(7, 1, 12), 7);
  assert.equal(routeInt(7.5, 1, 12), null);
  assert.equal(routeInt([], 1, 12), null);
});

test("a credential change that paused the text number says so", () => {
  assert.match(smsPauseSentence({ sms_number_needs_verify: true }), /paused/);
  assert.ok(smsPauseSentence({ sms_number_needs_verify: true }).startsWith(" "));
  assert.equal(smsPauseSentence({ sms_number_needs_verify: false }), "");
  assert.equal(smsPauseSentence({}), "");             // an older server
  assert.equal(smsPauseSentence(null), "");
});

test("a bulk selection goes in pieces the server accepts", () => {
  const { chunkIds, BULK_CHUNK, BULK_MAX } = pureModule;
  // the server applies its whole cap in one all-or-nothing write, so any
  // selection the screen offers is one request, never a half-applied split
  assert.equal(BULK_CHUNK, BULK_MAX);
  assert.equal(BULK_MAX, 10000);
  const offered = Array.from({ length: BULK_MAX }, (_, i) => `t${i}`);
  assert.deepEqual(chunkIds(offered).map((p) => p.length), [BULK_MAX]);
  const ids = Array.from({ length: 25000 }, (_, i) => `t${i}`);
  const pieces = chunkIds(ids);
  assert.deepEqual(pieces.map((p) => p.length), [10000, 10000, 5000]);
  assert.ok(pieces.every((p) => p.length <= BULK_CHUNK));
  assert.deepEqual(pieces.flat(), ids);                  // order kept, none lost
  assert.deepEqual(chunkIds([]), []);
  assert.deepEqual(chunkIds(["a"]), [["a"]]);
  assert.deepEqual(chunkIds(["a", "b", "c"], 2), [["a", "b"], ["c"]]);
  assert.deepEqual(chunkIds(["a", "b"], 0), [["a"], ["b"]]);  // never a zero step
});

test("a whole offered selection goes in one request", async () => {
  const { runChunked, BULK_MAX } = pureModule;
  const ids = Array.from({ length: 6000 }, (_, i) => `t${i}`);
  const seen = [];
  await runChunked(ids, async (p) => { seen.push(p.length); return {}; });
  assert.deepEqual(seen, [6000]);
  assert.ok(6000 <= BULK_MAX);
});

test("chunked bulk adds the tallies and reports progress", async () => {
  const { runChunked } = pureModule;
  const ids = Array.from({ length: 2300 }, (_, i) => `t${i}`);
  const seen = [];
  const prog = [];
  const r = await runChunked(ids, async (p) => {
    seen.push(p.length);
    return { applied: p.length - 1, missing: 1, flow: 2 };
  }, (d, t) => prog.push([d, t]), 1000);
  assert.deepEqual(seen, [1000, 1000, 300]);
  assert.deepEqual(r, { applied: 2297, missing: 3, flow: 6 });
  assert.deepEqual(prog, [[0, 2300], [1000, 2300], [2000, 2300], [2300, 2300]]);
});

test("chunked bulk that fails midway says how many were written", async () => {
  const { runChunked } = pureModule;
  const ids = Array.from({ length: 2500 }, (_, i) => `t${i}`);
  let n = 0;
  await assert.rejects(runChunked(ids, async () => {
    n += 1;
    if (n === 2) throw new Error("network");
    return { applied: 1000 };
  }, undefined, 1000), /stopped after 1000 of 2500 — network/);
  // the first piece failing is the server's own error, unwrapped
  const first = new Error("ids must be a list");
  await assert.rejects(runChunked(["a"], async () => { throw first; }),
                       (e) => e === first);
});

test("the widget refresh is skipped when none is placed and spaced when unknown", () => {
  const t0 = 1_000_000_000;
  const last = { stamp: "a", at: t0 };
  const later = t0 + WIDGET_REFRESH_MIN_MS;
  // none placed: never, even the first time
  assert.equal(shouldRefreshWidget(0, "a", null, t0), false);
  assert.equal(shouldRefreshWidget(0, "b", last, later), false);
  // placed: on a change at once, or when the last one is old
  assert.equal(shouldRefreshWidget(1, "a", null, t0), true);
  assert.equal(shouldRefreshWidget(1, "b", last, t0 + 1), true);
  assert.equal(shouldRefreshWidget(1, "a", last, t0 + 1), false);
  assert.equal(shouldRefreshWidget(1, "a", last, later), true);
  // unknown: only a change AND fifteen minutes since the last
  assert.equal(shouldRefreshWidget(null, "a", null, t0), true);
  assert.equal(shouldRefreshWidget(null, "b", last, later - 1), false);
  assert.equal(shouldRefreshWidget(null, "a", last, later), false);
  assert.equal(shouldRefreshWidget(null, "b", last, later), true);
});

test("a refused download shows the server's sentence", () => {
  assert.equal(downloadRefusal(JSON.stringify({ detail: "this instance has more than one tenant" })),
               "this instance has more than one tenant");
  assert.equal(downloadRefusal(JSON.stringify({ detail: { message: "pg_dump not available" } })),
               "pg_dump not available");
  assert.equal(downloadRefusal("<html>bad gateway</html>"), "download failed");
  assert.equal(downloadRefusal(""), "download failed");
  assert.equal(downloadRefusal(JSON.stringify({ detail: "" })), "download failed");
});

test("a member's undo that would leave kept rows gets the note, not the question", () => {
  const note = "Some of these rows are kept for a bank feed; ask the owner to undo it.";
  const member = new ApiError(409, note, { detail: note, error: "kept_rows" });
  assert.equal(keptRowsNote(member), note);
  assert.equal(keptRowsQuestion(member), null);   // nothing to answer with force
  const owner = new ApiError(409, "q?", { detail: "q?", error: "kept_rows_unconfirmed" });
  assert.equal(keptRowsNote(owner), null);
  assert.equal(keptRowsNote(new ApiError(409, "a sync is running",
                                         { detail: "a sync is running" })), null);
  assert.equal(keptRowsNote(new ApiError(400, note, { detail: note, error: "kept_rows" })), null);
  assert.equal(keptRowsNote(null), null);
});

test("a timestamp prints as the local day it fell on", () => {
  const { localDayOf, localYmd } = pureModule;
  const ts = "2026-10-04T02:30:00+00:00";
  assert.equal(localDayOf(ts), localYmd(new Date(ts)));
  assert.equal(localDayOf("not a time"), "not a time");
});

// ---- an approved offer the server withdrew leaves the list ----
import { proposalSuperseded } from "../src/lib/pure.ts";

test("bill offer: an already-tracked approval is read as withdrawn", () => {
  // the reply's flag, on success or inside a refusal's body
  assert.equal(proposalSuperseded({ ok: true, payee: "Power", superseded: true }), true);
  assert.equal(proposalSuperseded({ status: 400, detail: "x", body: { superseded: true } }), true);
  // a server that predates the flag says so in its sentence
  assert.equal(proposalSuperseded({ status: 400,
    detail: "'Power' is already tracked — this offer was withdrawn" }), true);
  // any other refusal or a plain approval keeps the card's own handling
  assert.equal(proposalSuperseded({ ok: true, payee: "Power" }), false);
  assert.equal(proposalSuperseded({ status: 400, detail: "bill 'Power' no longer exists" }), false);
  assert.equal(proposalSuperseded(undefined), false);
});

test("a window that fell back to the live month names it as the month so far", () => {
  const { windowLabel } = pureModule;
  assert.equal(windowLabel({ label: "Oct 2026", live_month: true }), "Oct 2026 so far");
  assert.equal(windowLabel({ label: "Oct 2025–Sep 2026", live_month: false }),
               "Oct 2025–Sep 2026");
  // a server that predates the flag keeps its own label
  assert.equal(windowLabel({ label: "Sep 2026" }), "Sep 2026");
});

test("search pile: an edit or removal on an earlier page reaches the rows shown", () => {
  const { mergePilePages, flattenPile } = pureModule;
  const p1 = [{ id: "a", category: "Shopping" }, { id: "b", category: "Shopping" }];
  const p2 = [{ id: "c", category: "Shopping" }];
  let pages = mergePilePages([], 1, p1, () => undefined, true);
  pages = mergePilePages(pages, 2, p2, (p) => p === 1 ? p1 : undefined, false);
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["a", "b", "c"]);
  // the detail screen recategorised "a" and retired "b" in page 1's cache
  const p1Edited = [{ id: "a", category: "Groceries" }];
  pages = mergePilePages(pages, 2, p2, (p) => p === 1 ? p1Edited : undefined, false);
  const shown = flattenPile(pages);
  assert.deepEqual(shown.map((r) => r.id), ["a", "c"]);
  assert.equal(shown[0].category, "Groceries");
  // an evicted page keeps the copy last seen instead of vanishing
  pages = mergePilePages(pages, 2, p2, () => undefined, false);
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["a", "c"]);
  // a new question starts over
  assert.deepEqual(mergePilePages(pages, 1, p2, () => p1, true), [p2]);
});

test("search pile: a row an edit shifts across the page boundary is never skipped", () => {
  const { mergePilePages, flattenPile } = pureModule;
  const ids = (xs) => xs.map((id) => ({ id }));
  const p1 = ids(["a", "b"]);
  const p2 = ids(["c", "d"]);
  let pages = mergePilePages([], 1, p1, () => undefined, true);
  pages = mergePilePages(pages, 2, p2, (p) => p === 1 ? p1 : undefined, false);
  // "a" stopped matching: the server now puts "c" on page 1, but page 1's
  // cache is the old copy and only page 2 is refetched
  pages = mergePilePages(pages, 2, ids(["d", "e"]),
                         (p) => p === 1 ? p1 : undefined, false);
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["a", "b", "c", "d", "e"]);
  // a later patch of the same page keeps it too
  pages = mergePilePages(pages, 2, ids(["d", "e"]),
                         (p) => p === 1 ? p1 : undefined, false);
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["a", "b", "c", "d", "e"]);
  // a retired row is not carried back
  pages = mergePilePages([p1, p2], 2, ids(["d"]),
                         (p) => p === 1 ? p1 : undefined, false, new Set(["c"]));
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["a", "b", "d"]);
  // a row the refreshed page 1 now holds is not doubled
  pages = mergePilePages([p1, p2], 2, ids(["d", "e"]),
                         (p) => p === 1 ? ids(["b", "c"]) : undefined, false);
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["b", "c", "d", "e"]);
});

test("search pile: a refresh that refetches every loaded page shows a row the server added to page one", () => {
  const { mergePilePages, flattenPile } = pureModule;
  const ids = (xs) => xs.map((id) => ({ id }));
  const p1 = ids(["a", "b"]), p2 = ids(["c", "d"]);
  let pages = mergePilePages([], 1, p1, () => undefined, true);
  pages = mergePilePages(pages, 2, p2, (p) => p === 1 ? p1 : undefined, false);
  // a new charge "n" sorts to the top; the refresh refetched page 1 too,
  // so its cache holds the server's new copy when the pile is merged
  pages = mergePilePages(pages, 2, ids(["b", "c"]),
                         (p) => p === 1 ? ids(["n", "a"]) : undefined, false);
  const shown = flattenPile(pages).map((r) => r.id);
  assert.deepEqual(shown, ["n", "a", "b", "c"]);
  // "d" moved to page 3: Load more reaches it, nothing is doubled
  pages = mergePilePages(pages, 3, ids(["d"]),
                         (p) => p === 1 ? ids(["n", "a"])
                              : p === 2 ? ids(["b", "c"]) : undefined, false);
  assert.deepEqual(flattenPile(pages).map((r) => r.id), ["n", "a", "b", "c", "d"]);
});

test("search pile: pull-to-refresh and refocus refresh every loaded page, not only the last", () => {
  const src = readFileSync(
    new URL("../src/app/(tabs)/transactions.tsx", import.meta.url), "utf8");
  assert.match(src, /onRefresh=\{refreshAll\}/);
  assert.match(src, /useRefetchWhenShown\(\{[^}]*refetch: refreshAll \}/);
  // earlier pages are fetched again, even when the cache has dropped them
  assert.match(src, /refetchQueries\(\{ queryKey: key, exact: true,\s*type: "all" \}\)/);
  assert.match(src, /prefetchQuery\(/);
});

// ---- the widget's credential and cache across a re-mint, a midnight and
// a missed reload ----
test("widget: a refused request drops the credential only if it is still the one stored", () => {
  const src = readFileSync(new URL("../src/lib/glance.ts", import.meta.url), "utf8");
  const branch = src.slice(src.indexOf("r.status === 401"));
  const guard = branch.indexOf("saveStillOwned(cred.token");
  const del = branch.indexOf("deleteItemAsync(KEY_WIDGET_TOKEN");
  assert.ok(guard > 0 && del > guard, "the delete must sit behind the ownership check");
});

test("widget: a cached glance is stamped with when its request went out", () => {
  // a request sent at 11:59 PM that lands after midnight carries the day
  // that ended; stamped with its landing time it would read as today's
  const src = readFileSync(new URL("../src/lib/glance.ts", import.meta.url), "utf8");
  assert.doesNotMatch(src, /fetched_at: Date\.now\(\)/);
  const swift = readFileSync(new URL("../targets/glance/index.swift", import.meta.url), "utf8");
  assert.doesNotMatch(swift, /fetchedAt: \.now\)\); return/);
  assert.match(swift, /fetchedAt: started/);
  const sent = new Date(2026, 0, 15, 23, 59, 58).getTime();
  const read = new Date(2026, 0, 16, 0, 0, 30).getTime();
  assert.equal(glanceFace({ glance: glance(), fetched_at: sent }, true, read).stale,
               true);
});

test("widget (iOS): the timeline carries an entry at the hour boundary and at midnight", () => {
  const swift = readFileSync(new URL("../targets/glance/index.swift", import.meta.url), "utf8");
  const tl = swift.slice(swift.indexOf("func getTimeline"),
                         swift.indexOf("private func load"));
  assert.match(tl, /Entry\(date: midnight/);
  assert.match(tl, /addingTimeInterval\(staleAfter \+ 1\)/);
  assert.match(tl, /Entry\(date: hourOld/);
  assert.match(tl, /entries\.sort/);
  // the hour entry is past the boundary, since isStale is strict
  assert.match(swift, /now\.timeIntervalSince\(fetched\) > staleAfter/);
});

test("widget (iOS): the payload stays in the Keychain and out of any HTTP disk cache", () => {
  const swift = readFileSync(new URL("../targets/glance/index.swift", import.meta.url), "utf8");
  const js = readFileSync(new URL("../src/lib/glance.ts", import.meta.url), "utf8");
  assert.match(swift, /URLSessionConfiguration\.ephemeral/);
  assert.match(swift, /urlCache = nil/);
  assert.doesNotMatch(swift, /URLSession\.shared/);
  assert.match(swift, /"no-store", forHTTPHeaderField: "Cache-Control"/);
  assert.match(swift, /kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly/);
  assert.doesNotMatch(swift, /\.set\(/);   // nothing written to defaults
  // the app deletes the extension's item by the same name at sign-out
  const key = swift.match(/static let key = "([^"]+)"/)[1];
  assert.ok(js.includes(`"${key}"`));
  assert.match(js, /deleteItemAsync\(KEY_WIDGET_CACHE_IOS, OPTS\)/);
});

test("folder import: the job poll runs while the job is live and stops once it settles", () => {
  const { bulkJobLive, bulkJobPoll } = pureModule;
  for (const state of ["queued", "running", "cancelling"]) {
    assert.equal(bulkJobLive({ state }), true, state);
    assert.equal(bulkJobPoll({ state }), 2000, state);
  }
  for (const state of ["done", "cancelled", "stopped", "error", "idle"]) {
    assert.equal(bulkJobLive({ state }), false, state);
    assert.equal(bulkJobPoll({ state }), false, state);
  }
  assert.equal(bulkJobPoll(undefined), false);
  assert.equal(bulkJobPoll(null), false);
});

test("folder import: the headline says how far it got and why it stopped", () => {
  const { bulkJobHeadline } = pureModule;
  const files = [{ index: 0, name: "a.csv", state: "done", ok: true },
                 { index: 1, name: "b.csv", state: "skipped", ok: false }];
  const job = (over) => ({ job_id: "j", total: 2, done: 1, files, ...over });
  assert.equal(bulkJobHeadline(job({ state: "running" })),
               "Importing — 1 of 2 files done");
  assert.equal(bulkJobHeadline(job({ state: "cancelled" })),
               "Cancelled — 1 of 2 files imported.");
  assert.match(bulkJobHeadline(job({ state: "stopped",
                                     stop_reason: "signed_out" })),
               /signed out\. 1 of 2 files imported/);
  assert.equal(bulkJobHeadline(job({ state: "done" })), "1/2 imported.");
});

// ---- savings goals: match words with no account can never be measured,
// so the editors prompt for the account (the server refuses the shape) ----
test("goals: match words with no account need an account", () => {
  const { goalNeedsAccount, goalHasWords } = pureModule;
  assert.equal(goalNeedsAccount({ tokens: "trip", account_id: "" }), true);
  assert.equal(goalNeedsAccount({ tokens: "", account_id: "" }), false);
  assert.equal(goalNeedsAccount({ tokens: " , ", account_id: "" }), false);
  assert.equal(goalNeedsAccount({ tokens: "trip", account_id: "sav" }), false);
  assert.equal(goalHasWords({ tokens: "trip, travel" }), true);
});

// ---- the bank-link hold: only the providers the owner is shown as held
// close their link buttons, with the server's own sentence ----
test("bank-link hold: a held provider closes its link door with the server's sentence", () => {
  const { bankLinksHeld, bankLinkHoldNote } = pureModule;
  const held = { outbound_paused: { reason: "recovery", aggregator: true,
    aggregator_keys: [{ provider: "plaid", client_id: "5f2c9e1ab03d4c0012a7f3e1",
                        env: "production", saved_at: null }] } };
  assert.equal(bankLinksHeld(held, "plaid"), true);
  assert.equal(bankLinksHeld(held, "mx"), false);
  assert.match(bankLinkHoldNote(held, "plaid"),
               /^New bank links are on hold since your account was recovered: confirm the Plaid keys/);
  assert.equal(bankLinkHoldNote(held, "mx"), null);
  // a member is told links are held but not which client: the server's
  // refusal speaks for the door
  const member = { outbound_paused: { reason: "rotation", aggregator: true } };
  assert.equal(bankLinksHeld(member, "plaid"), false);
  assert.equal(bankLinksHeld({ outbound_paused: null }, "plaid"), false);
  assert.equal(bankLinksHeld(undefined, "mx"), false);
  const rot = { outbound_paused: { reason: "rotation", aggregator: true,
    aggregator_keys: [{ provider: "mx" }] } };
  assert.match(bankLinkHoldNote(rot, "mx"),
               /since your password, login email or second factor changed: confirm the MX keys/);
});

// ---- a credential change names the members it removed ----
test("rotation: the removed members are named, nothing is said when none were", () => {
  const { membersRemovedNote } = pureModule;
  assert.equal(membersRemovedNote({ ok: true }), "");
  assert.equal(membersRemovedNote({ members_removed: [] }), "");
  assert.equal(membersRemovedNote(undefined), "");
  assert.equal(
    membersRemovedNote({ members_removed: [
      { email: "a@example.dev", role: "member", joined: "2026-10-01" }] }),
    " Removed 1 member who joined through your recent invites: "
    + "a@example.dev — invite back the ones that are yours.");
  assert.match(membersRemovedNote({ members_removed: [
    { email: "a@example.dev" }, { email: "b@example.dev" }] }),
    /^ Removed 2 members who joined .*a@example\.dev, b@example\.dev/);
});

// ---- a YNAB register of several accounts: every account answered ----
test("ynab accounts: the import waits for an answer for every account", () => {
  const { ynabAllAnswered, YNAB_SEVERAL } = pureModule;
  const names = ["Checking", "Visa"];
  assert.equal(ynabAllAnswered(names, {}, {}), false);
  assert.equal(ynabAllAnswered(names, { Checking: "chk" }, {}), false);
  assert.equal(ynabAllAnswered(names, { Checking: "chk", Visa: "card" }, {}),
               true);
  assert.equal(ynabAllAnswered(names, { Checking: "chk", Visa: "skip" }, {}),
               true);
  // everything skipped imports nothing, which the server refuses
  assert.equal(ynabAllAnswered(names, { Checking: "skip", Visa: "skip" }, {}),
               false);
  // a new account needs its name
  assert.equal(ynabAllAnswered(names, { Checking: "chk", Visa: "__new__" },
                               { Visa: "  " }), false);
  assert.equal(ynabAllAnswered(names, { Checking: "chk", Visa: "__new__" },
                               { Visa: "Rewards Visa" }), true);
  // lines with no account name are a group of their own, keyed ""
  assert.equal(ynabAllAnswered(["", "Visa"], { Visa: "card" }, {}), false);
  assert.equal(ynabAllAnswered(["", "Visa"], { "": "chk", Visa: "card" }, {}),
               true);
  assert.match("this YNAB register covers 2 YNAB accounts (Checking, Visa)",
               YNAB_SEVERAL);
  assert.doesNotMatch("CSV columns not recognized — import it alone to map "
                      + "them", YNAB_SEVERAL);
});

// ---- the assistant's charts: the geometry is arithmetic, so it is
// tested as arithmetic — bars stand on the zero line, a negative bar
// hangs below it, ring arcs sum to the circle, counts stay counts ----
import { barLayout, cellText, chartRows, historyNotKept, lineLayout, localTurn,
         keepNotes, mayTidyLog, mergeTurns, nextCursor, pagingCursor, plainNumber, pruneLocal,
         reportBlocks, sharedLogDenied,
         ringLayout, ringLegend, shortMoney, groupThreads, threadKey,
         conversationGone, followUpGone, replyNotKept,
         partialNote, PARTIAL_NOTE, START_GONE_NOTE, REMOVED_NOTE, RESTARTED_NOTE,
         threadMissing, threadDoorMissing, turnMissing } from "../src/lib/answer-pure.ts";

test("answer charts: bars share one zero line and a negative hangs below it", () => {
  const L = barLayout(["Jan", "Feb", "Mar"],
                      [{ name: "Saved", values: [120, -45, 30] }], 300, 170);
  assert.equal(L.bars.length, 3);
  const [a, b, c] = L.bars;
  assert.ok(a.y < L.zeroY && Math.abs(a.y + a.h - L.zeroY) < 0.01, "positive stands on zero");
  assert.ok(Math.abs(b.y - L.zeroY) < 0.01 && b.h > 0, "negative hangs from zero");
  assert.ok(a.h > c.h, "taller bar for the bigger value");
  // one series, few bars: the value is written on each bar
  assert.deepEqual(L.bars.map((r) => r.label), ["$120", "-$45", "$30"]);
  assert.equal(L.xLabels.length, 3);
  // two series: no per-bar labels, the legend names them
  const two = barLayout(["Jan"], [{ name: "a", values: [1] }, { name: "b", values: [2] }], 300, 170);
  assert.equal(two.bars.length, 2);
  assert.ok(two.bars.every((r) => r.label === null));
  assert.ok(two.bars[1].x > two.bars[0].x + two.bars[0].w, "grouped bars do not overlap");
});

test("answer charts: a long axis thins its labels, never drops the ends", () => {
  const labels = Array.from({ length: 36 }, (_, i) => `m${i}`);
  const B = barLayout(labels, [{ name: "x", values: labels.map(() => 1) }], 300, 170);
  assert.ok(B.xLabels.length <= 7 && B.xLabels.length >= 5, String(B.xLabels.length));
  assert.ok(B.bars.every((r) => r.label === null));
  const L = lineLayout(labels, [{ name: "x", values: labels.map((_, i) => i) }], 300, 170);
  assert.equal(L.xLabels[0].text, "m0");
  assert.equal(L.xLabels.at(-1).text, "m35");
  assert.equal(L.xLabels.at(-1).anchor, "end");
  assert.equal(L.zeroY, null, "no zero line when nothing is negative");
  assert.equal(L.lines.length, 1);
  assert.equal(L.lines[0].points.split(" ").length, 36);
});

test("answer charts: ring arcs cover the circle once and skip non-positive parts", () => {
  const R = ringLayout(["a", "b", "c", "d"], [50, 30, 0, -5], 60);
  assert.equal(R.segs.length, 2);
  assert.ok(Math.abs(R.segs.reduce((s, g) => s + g.dash, 0) - R.circ) < 1e-6);
  assert.equal(R.segs[1].offset, R.segs[0].dash);
  assert.ok(Math.abs(R.segs[0].dash / R.circ - 0.625) < 1e-9);
  assert.deepEqual(ringLayout(["a"], [0], 60).segs, []);
  // the geometry carries no printed figure: no total, no share
  assert.equal("total" in R, false);
  assert.equal("share" in R.segs[0], false);
});

test("answer charts: a ring's legend prints the server's dollars and shares", () => {
  const R = ringLayout(["a", "b", "c"], [500, 500, 500], 60);
  const shown = { total: 1500, rows: [
    { label: "a", dollars: 500, share: 34 },
    { label: "b", dollars: 500, share: 33 },
    { label: "c", dollars: 500, share: 33 }] };
  assert.deepEqual(ringLegend(R.segs, shown).map((g) => [g.label, g.dollars, g.share]),
                   [["a", 500, 34], ["b", 500, 33], ["c", 500, 33]]);
  // an older server sends no figures: each row its own value, no share
  assert.deepEqual(ringLegend(R.segs, undefined).map((g) => [g.dollars, g.share]),
                   [[500, null], [500, null], [500, null]]);
  const src = readFileSync(
    new URL("../src/components/answer-blocks.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(src, /Math\.round/);
});

test("answer charts: formats — axis money short, table numbers plain", () => {
  assert.equal(shortMoney(1234), "$1.2k");
  assert.equal(shortMoney(2000), "$2k");
  assert.equal(shortMoney(-45), "-$45");
  assert.equal(shortMoney(1_250_000), "$1.25M");
  assert.equal(plainNumber(52), "52");
  assert.equal(plainNumber(1204.1), "1,204.10");
  assert.equal(cellText("Example Mart"), "Example Mart");
  assert.equal(cellText(52), "52");
  const t = chartRows(["Jan"], [{ name: "Spent", values: [412.5] }]);
  assert.deepEqual(t.columns, ["", "Spent"]);
  assert.equal(t.rows[0][0], "Jan");
  assert.ok(t.rows[0][1].startsWith("$41"));
});

test("answer charts: the report sends the blocks, or the prose as one block", () => {
  const blocks = [{ kind: "text", text: "hi" }];
  assert.equal(reportBlocks("hi", blocks), blocks);
  assert.deepEqual(reportBlocks("prose only", undefined), [{ kind: "text", text: "prose only" }]);
  assert.deepEqual(reportBlocks("prose only", []), [{ kind: "text", text: "prose only" }]);
});

// ---- the assistant keeps an answer the server's log does not show ----
const kept = (id, q = "q" + id) => ({ id, asked_at: "2026-01-0" + id + "T10:00:00Z",
  asked_by: "a", question: q, answer: "a" + id, blocks: [], tools_used: [] });
const NOW = new Date("2026-01-09T12:00:00Z");

test("assistant: an answer with no id (older server, or a turn not kept) is shown", () => {
  const t = localTurn("k1", "net worth?", { answer: "$5", tools_used: ["networth"] }, NOW);
  assert.equal(t.id, null);
  assert.equal(t.local, true);
  assert.deepEqual(t.blocks, []);
  assert.deepEqual(t.tools_used, ["networth"]);
  assert.equal(t.asked_at, NOW.toISOString());
  const shown = mergeTurns([t], [], []);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].answer, "$5");
  // a null id stays even beside server rows — nothing will ever carry it
  assert.equal(mergeTurns([t], [kept("1")], []).length, 2);
  assert.equal(pruneLocal([t], [kept("1")]).length, 1);
});

test("assistant: a kept turn the fresh page does not carry was removed elsewhere", () => {
  const t = localTurn("k1", "q9", { answer: "a9", id: "9" }, NOW);
  const later = { id: "1", asked_at: new Date(NOW.getTime() + 1000).toISOString() };
  const earlier = { id: "2", asked_at: new Date(NOW.getTime() - 1000).toISOString() };
  // a page read after the ask that spans the turn's time: the turn is gone
  assert.deepEqual(pruneLocal([t], [later, earlier], true), []);
  assert.deepEqual(pruneLocal([t], [later], false), []);
  // a page read before the ask (newest row older) says nothing yet
  assert.equal(pruneLocal([t], [earlier], true).length, 1);
  // a page full of newer questions with an older page left: it may be there
  assert.equal(pruneLocal([t], [later], true).length, 1);
  // an empty page cannot tell fresh from stale
  assert.equal(pruneLocal([t], [], false).length, 1);
});

test("assistant: a kept turn shows once — the log's row replaces the local copy", () => {
  const t = localTurn("k1", "q9", { answer: "a9", id: "9" }, NOW);
  assert.deepEqual(mergeTurns([t], [kept("1")], []).map((x) => x.id), ["9", "1"]);
  const after = mergeTurns([t], [kept("9"), kept("1")], []);
  assert.deepEqual(after.map((x) => x.id), ["9", "1"]);
  assert.equal(after[0].local, undefined);
  assert.deepEqual(pruneLocal([t], [kept("9")]), []);
  // an older page that also carries it counts too
  assert.equal(mergeTurns([t], [], [kept("9")]).length, 1);
});

test("assistant: local turns sit newest-first above the log; pages dedupe", () => {
  const a = localTurn("k1", "first", { answer: "1" }, NOW);
  const b = localTurn("k2", "second", { answer: "2" }, NOW);
  const shown = mergeTurns([a, b], [kept("3"), kept("2")], [kept("2"), kept("1")]);
  assert.deepEqual(shown.map((x) => x.question), ["second", "first", "q3", "q2", "q1"]);
});

test("assistant: a 404 history read means no shared log, not an error", () => {
  assert.equal(historyNotKept(new ApiError(404, "Not Found")), true);
  assert.equal(historyNotKept(new Error("404: {\"detail\":\"Not Found\"}")), true);
  assert.equal(historyNotKept(new ApiError(500, "boom")), false);
  assert.equal(historyNotKept(new Error("500: boom")), false);
  assert.equal(historyNotKept(new Error("network down")), false);
  assert.equal(historyNotKept(null), false);
});

// ---- paging the shared log by (asked_at, id): rows sharing a timestamp
// at a page boundary must not be skipped ----
const row = (id, at = "2026-01-15T09:00:00+00:00") => ({ id, asked_at: at });

// ---- conversations: one card per thread, newest first ----
test("assistant: turns group into conversations, newest first, each oldest first", () => {
  const t = (id, thread, at) => ({ ...kept(id), thread_id: thread, asked_at: at });
  const rows = [t("3", "1", "2026-01-03T10:00:00Z"), t("2", "2", "2026-01-02T10:00:00Z"),
                t("1", "1", "2026-01-01T10:00:00Z")];
  const threads = groupThreads(rows);
  assert.deepEqual(threads.map((x) => x.id), ["1", "2"]);
  assert.deepEqual(threads[0].turns.map((x) => x.id), ["1", "3"]);
  assert.equal(threads[0].last_at, "2026-01-03T10:00:00Z");
});

test("assistant: a turn without a thread (older server) is a conversation of one", () => {
  const threads = groupThreads([kept("1"), kept("2")]);
  assert.equal(threads.length, 2);
  assert.equal(threads[0].id, "2");
  assert.equal(threadKey(kept("1")), "1");
});

test("assistant: a local follow-up sits in its thread; an unkept one is its own", () => {
  const first = kept("1");
  const follow = localTurn("k9", "and?", { answer: "more", id: "9", thread_id: "1" }, NOW);
  const lost = localTurn("k8", "lost?", { answer: "x" }, NOW, null);
  const threads = groupThreads([follow, lost, first]);
  const mine = threads.find((x) => x.id === "1");
  assert.deepEqual(mine.turns.map((x) => x.question), ["q1", "and?"]);
  const alone = threads.find((x) => x.key === "k8");
  assert.equal(alone.id, null);
  // a follow-up answered by a server that could not keep it still joins the thread it was asked in
  const unkept = localTurn("k7", "again?", { answer: "y" }, NOW, "1");
  assert.equal(threadKey(unkept), "1");
});

test("assistant: older questions resume from the server's (time, id) cursor", () => {
  const page = { turns: [row("c"), row("b")], more: true,
                 next: { before: "2026-01-15T09:00:00+00:00", before_id: "b" } };
  assert.deepEqual(nextCursor(page), { before: "2026-01-15T09:00:00+00:00", before_id: "b" });
  // a server without `next` still gets the last row's id, not the time alone
  assert.deepEqual(nextCursor({ turns: [row("c"), row("b")], more: true }),
                   { before: "2026-01-15T09:00:00+00:00", before_id: "b" });
});

test("assistant: paging stops when the page says no more", () => {
  assert.equal(nextCursor({ turns: [row("a")], more: false,
                            next: { before: "x", before_id: "a" } }), null);
  assert.equal(nextCursor({ turns: [], more: true }), null);
  assert.equal(nextCursor(undefined), null);
});

test("assistant: the cursor follows the last older page, else the first", () => {
  const first = { turns: [row("d")], more: true, next: { before: "t1", before_id: "d" } };
  const older = { turns: [row("c")], more: true, next: { before: "t0", before_id: "c" } };
  assert.deepEqual(pagingCursor(first, null), { before: "t1", before_id: "d" });
  assert.deepEqual(pagingCursor(first, older), { before: "t0", before_id: "c" });
  assert.equal(pagingCursor(first, { ...older, more: false }), null);
});

test("assistant: remove/clear are for editors who are not the shared login", () => {
  assert.equal(mayTidyLog(true, { shared_login: false }), true);
  assert.equal(mayTidyLog(true, {}), true);              // a demo household's own login
  assert.equal(mayTidyLog(true, { shared_login: true }), false);
  assert.equal(mayTidyLog(false, { shared_login: false }), false);
  assert.equal(mayTidyLog(true, undefined), false);      // unknown is not privileged
});

test("assistant: the shared-login 403 is recognised, other errors are not", () => {
  const sentence = "This is a shared demonstration login — it can't delete questions";
  assert.equal(sharedLogDenied(new ApiError(403, sentence)), true);
  assert.equal(sharedLogDenied(new Error(`403: {"detail":"${sentence}"}`)), true);
  assert.equal(sharedLogDenied(new ApiError(403, "forbidden")), false);
  assert.equal(sharedLogDenied(new ApiError(500, sentence)), false);
  assert.equal(sharedLogDenied(null), false);
});

// ---- a conversation split across log pages, or removed elsewhere ----
test("assistant: a thread whose start is on an older page is marked partial", () => {
  const t = (id, thread, at) => ({ ...kept(id), thread_id: thread, asked_at: at });
  // first page: two follow-ups of thread "1"; its opening turn is on page two
  const firstPage = [t("5", "1", "2026-01-05T10:00:00Z"), t("4", "1", "2026-01-04T10:00:00Z"),
                     t("3", "3", "2026-01-03T10:00:00Z")];
  const split = groupThreads(firstPage);
  assert.equal(split.find((x) => x.id === "1").partial, true);
  assert.equal(split.find((x) => x.id === "3").partial, false, "a root alone is whole");
  // once the older page lands the thread is whole, opening turn first
  const whole = groupThreads([...firstPage, t("1", "1", "2026-01-01T10:00:00Z")]);
  const one = whole.find((x) => x.id === "1");
  assert.equal(one.partial, false);
  assert.deepEqual(one.turns.map((x) => x.id), ["1", "4", "5"]);
  // a turn from a server without threads is never partial
  assert.equal(groupThreads([kept("1")])[0].partial, false);
  // a local follow-up whose thread's start is not loaded is partial too
  const lone = localTurn("k1", "and?", { answer: "x", id: "9", thread_id: "1" }, NOW);
  assert.equal(groupThreads([lone])[0].partial, true);
});

test("assistant: the ask's 404 means the conversation is gone; other errors do not", () => {
  assert.equal(conversationGone(new ApiError(404, "no such conversation")), true);
  assert.equal(conversationGone(new Error("404: {\"detail\":\"no such conversation\"}")), true);
  assert.equal(conversationGone(new ApiError(502, "assistant error")), false);
  assert.equal(conversationGone(new ApiError(503, "busy")), false);
  assert.equal(conversationGone(new Error("network down")), false);
});

test("assistant: the follow-up chip clears only when the log shows its thread removed", () => {
  const t = (id, thread, at) => ({ ...kept(id), thread_id: thread, asked_at: at });
  const f = { id: "1", question: "q1", last_at: "2026-01-04T10:00:00Z" };
  const live = [t("4", "1", "2026-01-04T10:00:00Z"), t("1", "1", "2026-01-01T10:00:00Z")];
  // still there: keep
  assert.equal(followUpGone(f, live, { turns: live, more: false }), false);
  // removed, and the page reaches back past its latest turn: clear
  const after = [t("6", "6", "2026-01-06T10:00:00Z"), t("2", "2", "2026-01-02T10:00:00Z")];
  assert.equal(followUpGone(f, after, { turns: after, more: true }), true);
  // removed, and nothing more to read: clear
  const short = [t("6", "6", "2026-01-06T10:00:00Z")];
  assert.equal(followUpGone(f, short, { turns: short, more: false }), true);
  // an empty log: clear
  assert.equal(followUpGone(f, [], { turns: [], more: false }), true);
  // absent only because newer turns pushed it past the first page: keep
  const pushed = [t("8", "8", "2026-01-08T10:00:00Z"), t("7", "7", "2026-01-07T10:00:00Z")];
  assert.equal(followUpGone(f, pushed, { turns: pushed, more: true }), false);
  // a local turn of the thread (the log has not caught up) keeps it
  const mine = localTurn("k1", "and?", { answer: "x", id: "9", thread_id: "1" }, NOW);
  assert.equal(followUpGone(f, [mine, ...short], { turns: short, more: false }), false);
});

test("assistant: an answer kept in no conversation offers no follow-up", () => {
  assert.equal(replyNotKept({ answer: "x", saved: false }), true);
  assert.equal(replyNotKept({ answer: "x", removed: true }), true);
  assert.equal(replyNotKept({ answer: "x", id: "9", thread_id: "1" }), false);
  // a follow-up whose thread was removed mid-answer: no thread, no id
  const gone = localTurn("k1", "and?", { answer: "x", thread_id: null, removed: true }, NOW, "1");
  assert.equal(gone.thread_id, null);
  assert.equal(gone.id, null);
  const [th] = groupThreads([gone]);
  assert.equal(th.id, null, "no follow-up on it");
  assert.equal(th.key, "k1");
  // the same for one the server could not record
  const unsaved = localTurn("k2", "new?", { answer: "y", id: null, thread_id: null, saved: false },
                            NOW, null);
  assert.equal(groupThreads([unsaved])[0].id, null);
});

test("assistant: a re-asked answer keeps its note after the log reads it back", () => {
  const t = localTurn("k1", "and?", { answer: "x", id: "9", thread_id: "9", saved: true },
                      NOW, null, RESTARTED_NOTE);
  assert.equal(t.note, RESTARTED_NOTE);
  const row = { ...kept("9"), thread_id: "9" };
  // before the prune runs, the log's row already takes the local note
  const early = mergeTurns([t], [row], []);
  assert.equal(early.length, 1, "shown once");
  assert.equal(early[0].note, RESTARTED_NOTE);
  assert.equal("local" in early[0], false, "the log's row is the one shown");
  // the log carries it now: the note moves to the map, the local turn goes
  const notes = keepNotes({}, [t], [row]);
  assert.deepEqual(notes, { 9: RESTARTED_NOTE });
  assert.deepEqual(pruneLocal([t], [row]), []);
  const merged = mergeTurns([], [row], [], notes);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].note, RESTARTED_NOTE);
  // a note on an older-page row too
  assert.equal(mergeTurns([], [], [row], notes)[0].note, RESTARTED_NOTE);
  // nothing new: the same object and array come back (no re-render loop)
  assert.equal(keepNotes(notes, [t], [row]), notes);
  const plain = localTurn("k2", "q", { answer: "a", id: "8" }, NOW);
  const mine = [plain];
  assert.equal(pruneLocal(mine, [row]), mine);
  assert.equal(keepNotes(notes, mine, [{ ...kept("8") }]), notes, "no note to keep");
  assert.equal(mergeTurns([], [row], [])[0].note, undefined);
});

test("assistant: a noted answer whose row leaves the log is not shown again", () => {
  const t = localTurn("k1", "and?", { answer: "x", id: "9", thread_id: "9", saved: true },
                      NOW, null, RESTARTED_NOTE);
  const row = { ...kept("9"), thread_id: "9" };
  const notes = keepNotes({}, [t], [row]);
  const mine = pruneLocal([t], [row]);
  // removed elsewhere (or cleared, or slid past the page): the next read lacks it
  const other = { ...kept("1"), thread_id: "1" };
  const shown = mergeTurns(mine, [other], [], notes);
  assert.deepEqual(shown.map((x) => x.id), ["1"], "no live conversation comes back");
  assert.equal(groupThreads(shown).some((th) => th.id === "9"), false, "no follow-up");
  // and the chip on it is cleared once the read covers its time
  const f = { id: "9", question: "and?", last_at: row.asked_at };
  assert.equal(followUpGone(f, shown, { turns: [other], more: false }), true);
});

test("assistant: an answer whose conversation was removed mid-answer says so", () => {
  const gone = localTurn("k1", "and?", { answer: "x", thread_id: null, removed: true }, NOW, "1");
  assert.equal(gone.note, REMOVED_NOTE);
  assert.equal(gone.thread_id, null);
  // the removal outranks a restart note it was asked with
  const twice = localTurn("k2", "and?", { answer: "x", removed: true }, NOW, null, RESTARTED_NOTE);
  assert.equal(twice.note, REMOVED_NOTE);
  // an ordinary answer carries none
  assert.equal("note" in localTurn("k3", "q", { answer: "a", id: "3" }, NOW), false);
});

test("assistant: a follow-up the server could not save stays in its conversation", () => {
  const unsaved = localTurn("k1", "and?", { answer: "x", id: null, thread_id: "1", saved: false },
                            NOW, "1");
  assert.equal(unsaved.id, null, "no follow-up offered on the unkept answer itself");
  assert.equal(unsaved.thread_id, "1");
  const t = (id, at) => ({ ...kept(id), thread_id: "1", asked_at: at });
  const threads = groupThreads([unsaved, t("1", "2026-01-01T10:00:00Z")]);
  assert.equal(threads.length, 1, "one card, not a separate one");
  assert.equal(threads[0].id, "1");
  assert.deepEqual(threads[0].turns.map((x) => x.question), ["q1", "and?"]);
  // a new question that could not be saved is still a conversation of one
  const lone = localTurn("k2", "new?", { answer: "y", id: null, thread_id: null, saved: false },
                         NOW, null);
  assert.equal(groupThreads([lone])[0].id, null);
});

test("assistant: the partial note promises a start only while an older page is left", () => {
  assert.equal(partialNote(false, true), null);
  assert.equal(partialNote(false, false), null);
  assert.equal(partialNote(true, true), PARTIAL_NOTE);
  assert.equal(partialNote(true, false), START_GONE_NOTE);
});

test("assistant: only the server's own 404 means a conversation is already gone", () => {
  const web = (code, detail) => new Error(`${code}: ${JSON.stringify({ detail })}`);
  assert.equal(threadMissing(new ApiError(404, "no such conversation")), true);
  assert.equal(threadMissing(web(404, "no such conversation")), true);
  // an older server's framework 404: the threads door is missing
  assert.equal(threadMissing(new ApiError(404, "Not Found")), false);
  assert.equal(threadMissing(web(404, "Not Found")), false);
  assert.equal(threadDoorMissing(new ApiError(404, "Not Found")), true);
  assert.equal(threadDoorMissing(web(404, "Not Found")), true);
  assert.equal(threadDoorMissing(new ApiError(404, "no such conversation")), false);
  assert.equal(threadDoorMissing(new ApiError(403, "Not Found")), false);
  assert.equal(threadDoorMissing(new Error("network down")), false);
  // the turn door's own 404 on the fallback means that turn is gone
  assert.equal(turnMissing(new ApiError(404, "no such question")), true);
  assert.equal(turnMissing(web(404, "no such question")), true);
  assert.equal(turnMissing(new ApiError(404, "Not Found")), false);
  assert.equal(turnMissing(new ApiError(400, "no such question")), false);
});

// ---- net worth: the Over-time card draws the server's window, and a
// server that sends none prints no gain rather than one worked out here ----
import { overTimeWindow } from "../src/lib/networth-pure.ts";

test("overTimeWindow: the server's window, or the whole series with no gain", () => {
  const windows = { "1y": { start: 2, end: 14, gain: 1200, pct: 3.5, full: false } };
  assert.deepEqual(overTimeWindow(windows, "1y", 15), windows["1y"]);
  assert.deepEqual(overTimeWindow(windows, "3m", 15),
                   { start: 0, end: 14, gain: null, pct: null, full: true });
  assert.deepEqual(overTimeWindow(undefined, "all", 0),
                   { start: 0, end: 0, gain: null, pct: null, full: true });
});

// ---- the plan's Savings goal is found whatever its stored spelling ----
test("isPlanSavingsGoal: matches Savings case- and space-insensitively only", () => {
  assert.equal(isPlanSavingsGoal("Savings"), true);
  assert.equal(isPlanSavingsGoal("savings"), true);
  assert.equal(isPlanSavingsGoal("  SAVINGS "), true);
  assert.equal(isPlanSavingsGoal("Savings fund"), false);
  assert.equal(isPlanSavingsGoal("Emergency"), false);
  assert.equal(isPlanSavingsGoal(""), false);
});

// ---- the plan's Savings line collapses only while it is plan-only ----
test("isPlanOnlySavingsGoal: a Savings goal with a target, date or words keeps its editor", () => {
  const row = { name: "Savings", target: "0", target_date: "",
                account_id: "", tokens: "" };
  assert.equal(isPlanOnlySavingsGoal(row), true);
  assert.equal(isPlanOnlySavingsGoal({ ...row, target: "" }), true);
  assert.equal(isPlanOnlySavingsGoal({ ...row, target: "10000" }), false);
  assert.equal(isPlanOnlySavingsGoal({ ...row, target_date: "2027-06-01" }),
               false);
  assert.equal(isPlanOnlySavingsGoal({ ...row, tokens: "vault" }), false);
  assert.equal(isPlanOnlySavingsGoal({ ...row, account_id: "a1" }), false);
  assert.equal(isPlanOnlySavingsGoal({ ...row, name: "Trip" }), false);
});

// ---- income scenarios: a save merges onto the live list, so another
// device's edit is never erased by a stale seeded copy ----
test("mergeIncomeScenarios: edits here win, untouched rows take the live value", () => {
  const base = [{ name: "Full", take_home: 2000, cadence: "biweekly" },
                { name: "Half", take_home: 1000, cadence: "biweekly" }];
  const cur = [{ name: "Full", take_home: 2100, cadence: "biweekly" },
               { name: "Half", take_home: 1000, cadence: "biweekly" }];
  const rows = [{ name: "Full", take_home: "2000", cadence: "biweekly" },
                { name: "Half", take_home: "$1,200", cadence: "monthly",
                  touched: true }];
  assert.deepEqual(mergeIncomeScenarios(rows, base, cur), [
    { name: "Full", take_home: 2100, cadence: "biweekly" },
    { name: "Half", take_home: 1200, cadence: "monthly" }]);
});

test("mergeIncomeScenarios: deletions on either side stick, additions elsewhere carry through", () => {
  const base = [{ name: "Full", take_home: 2000, cadence: "biweekly" },
                { name: "Half", take_home: 1000, cadence: "biweekly" },
                { name: "Gone", take_home: 500, cadence: "monthly" }];
  // elsewhere: "Gone" deleted, "Bonus" added
  const cur = [{ name: "Full", take_home: 2000, cadence: "biweekly" },
               { name: "Half", take_home: 1000, cadence: "biweekly" },
               { name: "Bonus", take_home: 300, cadence: "monthly" }];
  // here: "Half" removed, a new "Side" added, a blank row ignored
  const rows = [{ name: "Full", take_home: "2000", cadence: "biweekly" },
                { name: "Gone", take_home: "500", cadence: "monthly" },
                { name: " Side ", take_home: "400", cadence: "weekly",
                  touched: true },
                { name: "  ", take_home: "", cadence: "biweekly",
                  touched: true }];
  assert.deepEqual(mergeIncomeScenarios(rows, base, cur), [
    { name: "Full", take_home: 2000, cadence: "biweekly" },
    { name: "Side", take_home: 400, cadence: "weekly" },
    { name: "Bonus", take_home: 300, cadence: "monthly" }]);
});

test("mergeIncomeScenarios: names compare case-insensitively, no duplicate carried in", () => {
  const base = [];
  const cur = [{ name: "full", take_home: 1, cadence: "monthly" }];
  const rows = [{ name: "Full", take_home: "9", cadence: "weekly",
                  touched: true }];
  assert.deepEqual(mergeIncomeScenarios(rows, base, cur),
    [{ name: "Full", take_home: 9, cadence: "weekly" }]);
  // an untouched row the server still has takes the live spelling
  assert.deepEqual(mergeIncomeScenarios(
    [{ name: "FULL", take_home: "9", cadence: "weekly" }],
    [{ name: "Full", take_home: 9, cadence: "weekly" }], cur), cur);
});

// A typed take-home reaches the save as the number the server would read
// from the same text: "2.100,50" and "2,100.50" are both 2100.5, and a
// comma that fits no convention is refused — the save throws, naming the
// row — never stripped to a figure ten or a hundred times too big, and
// never sent as NaN, which JSON carries as null.
import * as webScenarios from "../../webapp/src/incomescenarios.ts";
for (const [side, merge] of [["mobile", mergeIncomeScenarios],
                             ["web", webScenarios.mergeIncomeScenarios]]) {
  test(`${side}: an income scenario's take-home reads a decimal comma`, () => {
    const rows = [
      { name: "Full", take_home: "2.100,50", cadence: "biweekly", touched: true },
      { name: "Half", take_home: "1050,25", cadence: "biweekly", touched: true },
      { name: "Side", take_home: "2,100.50", cadence: "weekly", touched: true },
      { name: "Blank", take_home: "", cadence: "monthly", touched: true }];
    assert.deepEqual(merge(rows, [], []).map((r) => r.take_home),
                     [2100.5, 1050.25, 2100.5, 0]);
    for (const typed of ["1,2,3", "1,234.", "1,23,456", "12,3456", "abc"])
      assert.throws(() => merge([{ name: "Odd", take_home: typed,
                                   cadence: "weekly", touched: true }],
                                [], []),
                    /Odd: .* is not an amount/, typed);
    // an untouched row is the live value, never re-read from its text
    const live = [{ name: "Odd", take_home: 900, cadence: "weekly" }];
    assert.deepEqual(merge([{ name: "Odd", take_home: "1,234.",
                              cadence: "weekly" }], live, live), live);
  });
}

// ---- Investment fees & basis: the web form and the mobile form post the
// same body for the same rows, and refuse the same mistakes
import * as mobileInvest from "../src/lib/settings-pure.ts";
import * as webInvest from "../../webapp/src/investsettings.ts";

for (const [side, m] of [["mobile", mobileInvest], ["web", webInvest]]) {
  test(`${side}: investment settings convert percent to fraction and back`, () => {
    const form = m.investFormOf({
      advisory_fees: { "robo": 0.0025 },
      advisory_fee_exempt_masks: ["1111", "2222"],
      investment_basis_override: { "3333": 400 },
    });
    assert.deepEqual(form, {
      fees: [{ inst: "robo", pct: "0.25" }],
      exempt: "1111, 2222",
      basis: [{ mask: "3333", amount: "400" }],
    });
    const r = m.investBody(form);
    assert.equal(r.ok, true);
    assert.deepEqual(r.body, {
      advisory_fees: { robo: 0.0025 },
      advisory_fee_exempt_masks: ["1111", "2222"],
      investment_basis_override: { "3333": 400 },
    });
  });

  test(`${side}: an empty view is an empty form, and saving it clears`, () => {
    const form = m.investFormOf({ advisory_fees: null });
    assert.deepEqual(form, { fees: [], exempt: "", basis: [] });
    assert.deepEqual(m.investBody(form).body, {
      advisory_fees: {}, advisory_fee_exempt_masks: [],
      investment_basis_override: {} });
  });

  test(`${side}: investment settings normalize and skip blank rows`, () => {
    const r = m.investBody({
      fees: [{ inst: "  Robo Co ", pct: "0.35%" }, { inst: "", pct: "" }],
      exempt: "1111 ,1111;2222",
      basis: [{ mask: " 3333 ", amount: "$1,250.50" }, { mask: "", amount: "" }],
    });
    assert.equal(r.ok, true);
    assert.deepEqual(r.body, {
      advisory_fees: { "robo co": 0.0035 },
      advisory_fee_exempt_masks: ["1111", "2222"],
      investment_basis_override: { "3333": 1250.5 },
    });
  });

  test(`${side}: an investment save posts only the sections that were edited`, () => {
    // each section replaces its stored key whole, so posting one nobody
    // touched would put back this form's copy over another device's save
    const base = m.investFormOf({
      advisory_fees: { robo: 0.0025, other: 0.004 },
      advisory_fee_exempt_masks: ["1111", "2222"],
      investment_basis_override: { "3333": 400 },
    });
    const edited = { ...base, basis: [...base.basis,
                                      { mask: "4444", amount: "10" }] };
    const r = m.investChanges(base, edited);
    assert.equal(r.ok, true);
    assert.deepEqual(r.body, {
      investment_basis_override: { "3333": 400, "4444": 10 } });
    // reordering, re-spacing or a blank unused row is not an edit
    const same = { fees: [...base.fees].reverse().concat([{ inst: "", pct: "" }]),
                   exempt: "2222 ,1111,", basis: base.basis };
    assert.deepEqual(m.investChanges(base, same).body, {});
    // clearing a section is an edit, and posts it as empty
    assert.deepEqual(m.investChanges(base, { ...base, exempt: "" }).body, {
      advisory_fee_exempt_masks: [] });
    // a bad edit is refused just as a whole-form save would be
    assert.equal(m.investChanges(base, { ...base, exempt: "12-4" }).ok, false);
  });

  test(`${side}: an edited section carries the stored value it started from`, () => {
    // the server refuses a section whose stored value moved since this
    // base was read, so another device's row in it is not dropped
    const view = {
      advisory_fees: { robo: 0.0025 },
      advisory_fee_exempt_masks: null,
      investment_basis_override: { "3333": 400 },
    };
    const seen = m.investSeenOf(view);
    assert.deepEqual(seen, view);
    assert.deepEqual(m.investSeenOf(null), {
      advisory_fees: null, advisory_fee_exempt_masks: null,
      investment_basis_override: null });
    const base = m.investFormOf(view);
    const r = m.investChanges(base, { ...base, exempt: "1111" }, seen);
    assert.equal(r.ok, true);
    assert.deepEqual(r.body, {
      advisory_fee_exempt_masks: ["1111"],
      advisory_fee_exempt_masks_base: null });
    // untouched sections carry neither the value nor a base
    assert.deepEqual(m.investChanges(base, base, seen).body, {});
    // without a base the body is what it always was
    assert.deepEqual(m.investChanges(base, { ...base, exempt: "1111" }).body, {
      advisory_fee_exempt_masks: ["1111"] });
    assert.equal(m.INVEST_STALE_STATUS, 409);
  });

  test(`${side}: a money-in override reads a decimal comma as the server does`, () => {
    const r = m.investBody({ fees: [], exempt: "",
      basis: [{ mask: "3333", amount: "$1.250,50" },
              { mask: "4444", amount: "12,5" }] });
    assert.equal(r.ok, true);
    assert.deepEqual(r.body.investment_basis_override,
                     { "3333": 1250.5, "4444": 12.5 });
    // a comma that fits no convention is refused, not stripped to 123
    assert.equal(m.investBody({ fees: [], exempt: "",
      basis: [{ mask: "3333", amount: "1,2,3" }] }).ok, false);
  });

  test(`${side}: masks accept letters the bank uses`, () => {
    const r = m.investBody({
      fees: [], exempt: "ab12, AB12 x9",
      basis: [{ mask: " z1y2 ", amount: "400" }] });
    assert.equal(r.ok, true);
    assert.deepEqual(r.body.advisory_fee_exempt_masks, ["AB12", "X9"]);
    assert.deepEqual(r.body.investment_basis_override, { Z1Y2: 400 });
  });

  test(`${side}: investment settings refuse what the server refuses`, () => {
    const base = { fees: [], exempt: "", basis: [] };
    for (const bad of [
      { ...base, fees: [{ inst: "", pct: "0.25" }] },
      { ...base, fees: [{ inst: "robo", pct: "" }] },
      { ...base, fees: [{ inst: "robo", pct: "abc" }] },
      { ...base, fees: [{ inst: "robo", pct: "150" }] },
      { ...base, fees: [{ inst: "robo", pct: "-1" }] },
      { ...base, exempt: "12-4" },
      { ...base, exempt: "1" },
      { ...base, exempt: "123456789" },
      { ...base, basis: [{ mask: "", amount: "400" }] },
      { ...base, basis: [{ mask: "33-33", amount: "400" }] },
      { ...base, basis: [{ mask: "3333", amount: "-5" }] },
      { ...base, basis: [{ mask: "3333", amount: "" }] },
    ]) {
      const r = m.investBody(bad);
      assert.equal(r.ok, false, JSON.stringify(bad));
      assert.equal(typeof r.error, "string");
    }
  });
}

test("linkWindowAnswer: Android's 'opened' is no close; only iOS's close releases the slot", () => {
  const { linkWindowAnswer } = pureModule;
  // Android resolves as soon as the Custom Tab starts — the person is
  // still signing in, so nothing may be stamped or set aside
  assert.deepEqual(linkWindowAnswer("opened"), { closed: false, mayReleaseSlot: false });
  // iOS resolves only when the window goes away
  assert.deepEqual(linkWindowAnswer("cancel"), { closed: true, mayReleaseSlot: true });
  assert.deepEqual(linkWindowAnswer("dismiss"), { closed: true, mayReleaseSlot: true });
});

test("noCheckingLinked: a household without checking reads unknown cash, never $0", () => {
  const { noCheckingLinked } = pureModule;
  assert.equal(noCheckingLinked({ checking: null, no_checking: true }), true);
  // an older server without the flag: the null balance alone says it
  assert.equal(noCheckingLinked({ checking: null }), true);
  // a real $0 checking balance is a balance — the alarms stay live
  assert.equal(noCheckingLinked({ checking: 0, no_checking: false }), false);
  // no runway at all (a past day) is not "no checking"
  assert.equal(noCheckingLinked(null), false);
});

test("a sign-in shed with 503 says busy, never a wrong password or a status", () => {
  const shed = new ApiError(503, "server busy — try again shortly");
  assert.equal(signInErrText(shed),
               "The server is busy — try again in a moment.");
  shed.retryAfter = "15";
  assert.equal(signInErrText(shed),
               "The server is busy — try again in 15 seconds.");
  // a long or unreadable wait reads as "a moment", never a big number
  assert.equal(busyText("3600"), "The server is busy — try again in a moment.");
  assert.equal(busyText("soon"), "The server is busy — try again in a moment.");
  assert.equal(busyText("1"), "The server is busy — try again in 1 second.");
  // anything else keeps the server's own sentence
  assert.equal(signInErrText(new ApiError(400, "bad email")), "bad email");
});

// ---- the proxy answering while the app restarts ----
import { restartingText } from "../src/lib/api.ts";

/** A client call answered by `status` with `body` under `headers`. */
async function refusalOf(status, body, headers = {}, statusText = "") {
  const real = globalThis.fetch;
  globalThis.fetch = async () => new Response(body,
    { status, statusText, headers });
  try {
    const c = makeClient("https://example.invalid", "tok", () => {});
    let caught;
    try { await c.me(); } catch (e) { caught = e; }
    assert.ok(caught instanceof ApiError, "a refusal must be an ApiError");
    return caught;
  } finally { globalThis.fetch = real; }
}

test("a gateway status with the proxy's page says the server is restarting",
     async () => {
  const page = "<!doctype html><html><body>Back in a moment</body></html>";
  for (const status of [502, 503, 504]) {
    const e = await refusalOf(status, page, { "content-type": "text/html" },
                              "Service Unavailable");
    assert.equal(e.status, status, "the status survives for the callers");
    assert.equal(e.restarting, true);
    assert.equal(errText(e), "The server is restarting — try again in a moment.");
    assert.equal(signInErrText(e), errText(e));
  }
});

test("an empty or mislabelled gateway answer is the restart too", async () => {
  // HTTP/2 has no reason phrase, so the old status-text fallback was blank
  const empty = await refusalOf(502, "", {}, "");
  assert.equal(empty.restarting, true);
  assert.equal(errText(empty), restartingText(null));
  // an edge may rewrite the content-type; markup is still markup
  const relabelled = await refusalOf(504, "<html>gateway</html>",
                                     { "content-type": "text/plain" });
  assert.equal(relabelled.restarting, true);
});

test("the proxy's Retry-After names the wait in the restarting sentence",
     async () => {
  const e = await refusalOf(503, "<html></html>",
    { "content-type": "text/html", "Retry-After": "20" });
  assert.equal(signInErrText(e), "The server is restarting — try again in 20 seconds.");
  assert.equal(e.retryAfter, "20");
});

test("the app's own 503 stays a busy shed, and other refusals keep their words",
     async () => {
  const shed = await refusalOf(503,
    JSON.stringify({ detail: "server busy — try again shortly" }),
    { "content-type": "application/json", "Retry-After": "5" });
  assert.notEqual(shed.restarting, true);
  assert.equal(shed.detail, "server busy — try again shortly");
  assert.equal(signInErrText(shed), "The server is busy — try again in 5 seconds.");
  const own502 = await refusalOf(502,
    JSON.stringify({ detail: "the bank did not answer" }),
    { "content-type": "application/json" });
  assert.notEqual(own502.restarting, true);
  assert.equal(errText(own502), "the bank did not answer");
  // a non-gateway page still falls back to the status line, as before
  const other = await refusalOf(500, "<html>oops</html>",
    { "content-type": "text/html" }, "Internal Server Error");
  assert.notEqual(other.restarting, true);
  assert.equal(other.detail, "Internal Server Error");
});

test("a bank whose first sync is queued says so, the web's words", () => {
  assert.equal(FIRST_SYNC_RUNNING,
               "first sync running — accounts appear in a minute");
  assert.equal(linkedLine("Chase", "pending"),
               "Linked Chase ✓ — first sync running — accounts appear in a minute");
  assert.equal(linkedLine(undefined, "ok"), "Linked bank ✓");
  const c = { aggregator: "plaid", last_ok: null, status: "ok" };
  assert.equal(firstSyncPending(c), true);
  assert.equal(firstSyncPending({ ...c, last_ok: "2026-10-16T10:00:00Z" }), false);
  // a broken or released link is not "first sync running"
  assert.equal(firstSyncPending({ ...c, status: "error:ITEM_LOGIN_REQUIRED" }), false);
  assert.equal(firstSyncPending({ ...c, status_kind: "reauth" }), false);
  assert.equal(firstSyncPending({ ...c, aggregator: "simplefin" }), false);
});

// ---- the setup wizard: six steps on every instance, the web's words ----
test("wizard: the same six step keys on hosted and self-hosted, in the web's order", () => {
  assert.deepEqual(WIZARD_STEPS,
    ["appearance", "connect", "bills", "budgets", "email", "finish"]);
});

test("wizard appearance: Auto unless the household pinned light or dark", () => {
  assert.equal(wizardThemeChoice(undefined), "auto");
  assert.equal(wizardThemeChoice(null), "auto");
  assert.equal(wizardThemeChoice(""), "auto");
  assert.equal(wizardThemeChoice("auto"), "auto");
  assert.equal(wizardThemeChoice("neon"), "auto");
  assert.equal(wizardThemeChoice("light"), "light");
  assert.equal(wizardThemeChoice("dark"), "dark");
});

test("wizard history pills: five choices, each pill writes its months", () => {
  assert.deepEqual(HISTORY_CHOICES, [
    [1, "Last month"], [3, "3 months"], [6, "6 months"], [12, "1 year"],
    [null, "Everything available"]]);
  for (const v of [1, 3, 6, 12]) assert.equal(historyMonthsOf(v), v);
  // nothing stored, or a value no pill offers, reads as Everything
  for (const v of [undefined, null, 0, 2, 24, "6"])
    assert.equal(historyMonthsOf(v), null);
  assert.equal(historyPhrase(null), "everything available");
  assert.equal(historyPhrase(1), "last month");
  assert.equal(historyPhrase(12), "1 year");
});

test("wizard manual form: the four cadences in the bill-save wire format", () => {
  assert.deepEqual(MANUAL_CADENCES.map(([v]) => v),
    ["MONTHLY:1", "WEEKLY:1", "WEEKLY:2", "YEARLY:1"]);
  assert.deepEqual(MANUAL_CADENCES.map(([, l]) => l),
    ["monthly", "weekly", "every 2 weeks", "yearly"]);
});

test("wizard due day: ordinals, parsing, and the next date it falls on", () => {
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 31, 101, 111]
    .map(ordinal), ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th",
                    "21st", "22nd", "23rd", "31st", "101st", "111th"]);
  assert.equal(parseDueDay("14"), 14);
  assert.equal(parseDueDay(" 1 "), 1);
  for (const bad of ["", "0", "32", "1.5", "x", "-3"])
    assert.equal(parseDueDay(bad), null);
  assert.equal(dueDayOf("2026-11-14"), 14);
  assert.equal(dueDayOf(null), null);
  assert.equal(dueDayOf("soon"), null);
  // today counts; a day already past rolls to next month
  assert.equal(nextDueForDay(17, "2026-10-17"), "2026-10-17");
  assert.equal(nextDueForDay(1, "2026-10-17"), "2026-11-01");
  assert.equal(nextDueForDay(31, "2026-10-17"), "2026-10-31");
  assert.equal(nextDueForDay(5, "2026-12-20"), "2027-01-05");
  // a day the month lacks lands on its last day
  assert.equal(nextDueForDay(31, "2026-04-10"), "2026-04-30");
  assert.equal(nextDueForDay(30, "2026-02-15"), "2026-02-28");
  assert.equal(nextDueForDay(31, "2026-11-30"), "2026-11-30");
});

test("wizard Adjust: only the fields the person moved ride with the approve", () => {
  const p = { payee: "Electric — City Power", amount: 118.4,
              next_due: "2026-11-14", frequency: "MONTHLY", interval: 1 };
  assert.equal(proposalCadenceValue(p), "MONTHLY:1");
  assert.equal(proposalCadenceValue({ frequency: "monthly", interval: 2 }),
               "MONTHLY:2");
  assert.equal(proposalCadenceValue({ frequency: "MONTHLY", interval: 1,
                                      bill_type: "envelope" }), null);
  assert.equal(proposalCadenceValue({}), null);
  const same = { name: p.payee, amount: "118.40", cadence: "MONTHLY:1",
                 dueDay: "14" };
  assert.deepEqual(adjustOf(p, same), { adjust: {} });
  // "$" and "," are how a phone keyboard types money
  assert.deepEqual(adjustOf({ ...p, amount: 1000 },
                            { ...same, amount: "$1,000.00" }), { adjust: {} });
  assert.deepEqual(adjustOf(p, { name: " Electric ", amount: "120",
                                 cadence: "MONTHLY:2", dueDay: "15" }),
    { adjust: { name: "Electric", amount: 120, cadence: "MONTHLY:2",
                due_day: 15 } });
  // a blank due day keeps the finder's
  assert.deepEqual(adjustOf(p, { ...same, dueDay: "" }), { adjust: {} });
  assert.ok("error" in adjustOf(p, { ...same, name: "  " }));
  assert.ok("error" in adjustOf(p, { ...same, amount: "0" }));
  assert.ok("error" in adjustOf(p, { ...same, dueDay: "40" }));
});

test("wizard bill lines and the manual tally read like the web's rows", () => {
  assert.equal(billLine(1000, "MONTHLY:1", "2026-11-01"),
               "monthly · $1,000.00 · 1st");
  assert.equal(billLine(2310, "WEEKLY:2", null), "every 2 weeks · $2,310.00");
  // a label already in words passes through
  assert.equal(billLine(10.99, "monthly", "2026-11-22"),
               "monthly · $10.99 · 22nd");
  assert.equal(manualTally(1, 0), "1 bill · 0 income.");
  assert.equal(manualTally(3, 2), "3 bills · 2 income.");
});

test("wizard Done: the blurb promises only a message the server says reaches someone", () => {
  assert.equal(clockHour(0), "12:00 am");
  assert.equal(clockHour(7), "7:00 am");
  assert.equal(clockHour(12), "12:00 pm");
  assert.equal(clockHour(19), "7:00 pm");
  const lead = "That's everything. The Today page carries your daily "
    + "verdict from here on";
  const reach = (o) => ({ on: true, email: 0, push: 0, sms: false,
                          receiving: true, warn: false, text: "daily 7am", ...o });
  assert.equal(finishBlurb(reach({ email: 1 }), 7),
               `${lead}; the first email arrives tomorrow at 7:00 am.`);
  assert.equal(finishBlurb(reach({ email: 2, push: 1 }), 19),
               `${lead}; the first email arrives tomorrow at 7:00 pm.`);
  assert.equal(finishBlurb(reach({ push: 1 }), 7),
               `${lead}; the first notification arrives tomorrow at 7:00 am.`);
  assert.equal(finishBlurb(reach({ on: false, receiving: false }), 7), `${lead}.`);
  // on, and nobody receiving it (muted owner, no relay, no device): no promise
  const nobody = reach({ receiving: false, warn: true,
                         text: "daily 9am · nobody receiving it (no mail relay)" });
  assert.equal(finishBlurb(nobody, 9), `${lead}.`);
  assert.equal(reachLine(nobody),
               "⚠ daily 9am · nobody receiving it (no mail relay)");
  assert.equal(reachLine(reach({ email: 1 })), "daily 7am");
  // an older server sends no reach: nothing is promised
  assert.equal(finishBlurb(undefined, 7), `${lead}.`);
  assert.equal(reachLine(undefined), null);
});

test("wizard Done: one summary line per step", () => {
  assert.equal(accountsSummary(2, 5, null), "2 connected · everything available");
  assert.equal(accountsSummary(0, 1, 6), "1 account from files · 6 months");
  assert.equal(accountsSummary(0, 0, 3), "none yet · 3 months");
  assert.equal(billsSummary(3, 1), "3 bills, 1 paycheck approved");
  assert.equal(billsSummary(0, 0), "0 bills, 0 paychecks approved");
  // the month's total leads; "other" still holds its carve-outs
  assert.equal(budgetsSummary(true, 640, 1100), "$1,740 a month · Food $640");
  assert.equal(budgetsSummary(true, 0, 900), "$900 a month · Food $0");
  assert.equal(budgetsSummary(true, 0, 0), "$0 a month · Food $0");
  // a provisional seed is not the household's budget
  assert.equal(budgetsSummary(false, 640, 1100),
               "not set yet (Budget page any time)");
  assert.equal(deliverySummary("email", 7, 2), "Email at 7:00 am · 2 recipients");
  assert.equal(deliverySummary("both", 18, 1),
               "Email and push at 6:00 pm · 1 recipient");
  assert.equal(deliverySummary("push", 7, 3), "Push at 7:00 am · 3 recipients");
  // nobody mailed (every address muted, a self-muted owner too) says so
  assert.equal(deliverySummary("off", 7, 0), "Off · nobody");
  assert.equal(recipientRole({ owner: true }), "owner");
  assert.equal(recipientRole({ via: "member" }), "member");
  assert.equal(recipientRole({ invite: "accepted" }), "accepted");
  assert.equal(recipientRole({ invite: "invited" }), "invitation sent");
  assert.equal(recipientRole({ invite: "expired" }), "invitation expired");
  assert.equal(recipientRole({}), "added");
});

// ---- the wizard's one-at-a-time settings saves: two saves sent together
// can commit out of order, leaving the stored history window or setup
// mode different from what the screen shows ----
function deferredSender() {
  const calls = [];
  const send = (v) => new Promise((resolve, reject) =>
    calls.push({ v, resolve, reject }));
  return { calls, send };
}
const drain = () => new Promise((r) => setImmediate(r));

test("wizard saves: one in flight per setting, the last tap wins", async () => {
  const { calls, send } = deferredSender();
  const busy = [];
  const settled = [];
  const s = latestWins(send, { busy: (b) => busy.push(b),
    settled: (v, failed) => settled.push([v, failed]) });
  s.save(3);
  s.save(12);
  s.save(6);
  // the second and third taps wait; only the first is on the wire
  assert.deepEqual(calls.map((c) => c.v), [3]);
  assert.equal(s.pending(), true);
  calls[0].resolve();
  await drain();
  // the waiting 12 was replaced by 6: the last tap is the last sent
  assert.deepEqual(calls.map((c) => c.v), [3, 6]);
  assert.deepEqual(settled, []);
  calls[1].resolve();
  await drain();
  assert.deepEqual(settled, [[6, false]]);
  assert.equal(s.pending(), false);
  assert.equal(busy.at(-1), false);
});

test("wizard saves: a superseded failure is moot, the last one's is reported", async () => {
  const { calls, send } = deferredSender();
  const settled = [];
  const s = latestWins(send, {
    settled: (v, failed, e) => settled.push([v, failed, e?.message]) });
  s.save("manual");
  s.save("auto");
  calls[0].reject(new Error("first"));
  await drain();
  // the later save still goes out; the earlier failure says nothing
  assert.deepEqual(calls.map((c) => c.v), ["manual", "auto"]);
  assert.deepEqual(settled, []);
  calls[1].reject(new Error("last"));
  await drain();
  assert.deepEqual(settled, [["auto", true, "last"]]);
  // a tap after the queue drained sends at once
  s.save("manual");
  assert.equal(calls.length, 3);
});

test("wizard saves: a send that throws is a failed save, not a stuck queue", async () => {
  const settled = [];
  const s = latestWins(() => { throw new Error("no client"); },
    { settled: (v, failed) => settled.push([v, failed]) });
  s.save(1);
  await drain();
  assert.deepEqual(settled, [[1, true]]);
  assert.equal(s.pending(), false);
});

test("wizard Adjust: only an offer to add a bill or income can be adjusted", () => {
  assert.equal(proposalAdjustable({ kind: "add" }), true);
  for (const kind of ["attach", "merchant", "remove", "amount_change",
                      "change", null, undefined, ""])
    assert.equal(proposalAdjustable({ kind }), false, String(kind));
});

test("wizard Adjust: the due day shows only for monthly and yearly cadences", () => {
  for (const c of ["MONTHLY:1", "MONTHLY:3", "YEARLY:1", "monthly:1"])
    assert.equal(cadenceTakesDueDay(c), true, c);
  for (const c of ["ENVELOPE:1", "ENVELOPE:12", "WEEKLY:1", "WEEKLY:2",
                   "DAILY:1", "ONE_TIME:1", "", null, undefined])
    assert.equal(cadenceTakesDueDay(c), false, String(c));
  const p = { payee: "Gym", amount: -40, next_due: "2026-11-14",
              frequency: "MONTHLY", interval: 1 };
  const f = { name: "Gym", amount: "40.00", cadence: "MONTHLY:1",
              dueDay: "20" };
  assert.deepEqual(adjustOf(p, f), { adjust: { due_day: 20 } });
  // switched to weekly, the hidden field's typed day is not sent
  assert.deepEqual(adjustOf(p, { ...f, cadence: "WEEKLY:1" }),
                   { adjust: { cadence: "WEEKLY:1" } });
  // an envelope offer has no cadence of its own and no due date
  assert.deepEqual(adjustOf({ ...p, bill_type: "envelope" },
                            { ...f, cadence: "" }), { adjust: {} });
  // nor is a hidden day validated
  assert.deepEqual(adjustOf(p, { ...f, cadence: "DAILY:1", dueDay: "40" }),
                   { adjust: { cadence: "DAILY:1" } });
});

// The history window, the setup answer and the nightly finder are the
// owner's: the server answers anyone else's change with a 403. Each
// control must check useOwner and give everyone else the web's line
// instead of a control that only snaps back.
test("owner-only settings: a non-owner reads the value, never a control", () => {
  const gated = ["src/components/wizard-history.tsx",
                 "src/components/wizard-bills.tsx",
                 "src/app/(tabs)/bills.tsx"];
  for (const file of gated) {
    const src = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.ok(/\buseOwner\(\)/.test(src), `${file}: gate on useOwner()`);
    assert.ok(src.includes("the household owner sets this"),
              `${file}: the non-owner line must be the web's words`);
  }
  // the wizard's Continue saves the pre-selected answer: owner only
  const welcome = readFileSync(new URL("../src/app/welcome.tsx",
                                       import.meta.url), "utf8");
  assert.ok(/owner && storedMode !== setupMode\) saveMode/.test(welcome),
            "welcome.tsx: a non-owner's Continue must not save setup_mode");
  // the nightly switch is mounted for everyone and gates itself, so a
  // member or viewer still sees where it stands
  const bills = readFileSync(new URL("../src/app/(tabs)/bills.tsx",
                                     import.meta.url), "utf8");
  assert.ok(!/!viewer && <NightlyFinderSwitch/.test(bills),
            "bills.tsx: the switch hides itself from viewers entirely");
});

test("wizard Adjust: a refused save is answered inside the open form", () => {
  const src = readFileSync(new URL("../src/components/wizard-bills.tsx",
                                   import.meta.url), "utf8");
  // the refusal reaches the form, which shows it beside its own checks
  assert.ok(/refused=\{adjustErr\?\.pid === x\.id/.test(src),
            "the form must receive the server's refusal");
  assert.ok(/\{err \|\| refused\}/.test(src),
            "the form must show the refusal inline");
  // and the form is not closed by a failure (only a success closes it)
  const onError = src.slice(src.indexOf("onError: (e, v) =>"));
  assert.ok(!/setAdjusting\(null\)/.test(onError.slice(0, onError.indexOf("},\n  });"))),
            "a refused adjust must keep the form open with its values");
});

test("manual budgets: a blank box over a stored budget keeps the figure", () => {
  const stored = { food_monthly: 600, other_monthly: 400,
                   budgeted_income_monthly: 5000 };
  // only everything-else typed: food and income stay out of the body, so
  // the server keeps 600 and 5000, and the Savings plan counts them
  const r = manualBudgetsBody({ income: "", food: "", other: "450" },
                              stored, true, 1000);
  assert.deepEqual(r.body, { other_monthly: 450 });
  assert.equal(r.monthlyPlan, 5000 - 1000 - 600 - 450);
  // a zero over a stored figure is the same as blank
  assert.deepEqual(manualBudgetsBody({ income: "0", food: "0", other: "450" },
                                     stored, true, 0).body,
                   { other_monthly: 450 });
  // nothing stored: an empty food/other is written as none, but a blank
  // income is never sent, and a provisional seed is not counted as kept
  const fresh = manualBudgetsBody({ income: "", food: "300", other: "" },
                                  stored, false, 1000);
  assert.deepEqual(fresh.body, { food_monthly: 300, other_monthly: 0 });
  assert.equal(fresh.monthlyPlan, 5000 - 1000 - 300);
  assert.equal(manualBudgetsBody({ income: "2,000", food: "$900", other: "" },
                                 {}, false, 1500).monthlyPlan, 0);
  assert.equal(keptBudget(640, false), 0);
  assert.equal(keptBudget(640, true), 640);
  assert.equal(keptBudget(null, true), 0);
});

test("manual budgets: a Savings plan is never worked out from missing bills", () => {
  // the suggestion read the Bills figure comes from has not answered (or
  // failed): saving would store income less food as the Savings plan, as
  // if the household had no bills
  assert.throws(() => manualBudgetsBody({ income: "5000", food: "600",
                                          other: "400" }, {}, false, null),
                (e) => e.message === "Couldn't load your bills, so the "
                  + "plan can't be worked out or saved yet.");
});

test("manual budgets: a stored plan prefills; a new household starts blank", () => {
  const s = { food_monthly: 600, other_monthly: 0,
              budgeted_income_monthly: 5000 };
  assert.deepEqual(manualBudgetsPrefill({ budgets_set: true }, s),
                   { income: "5000", food: "600", other: "" });
  assert.deepEqual(manualBudgetsPrefill(
    { budgets_set: false, wizard_steps: { budgets: "done" } }, s),
    { income: "5000", food: "600", other: "" });
  assert.deepEqual(manualBudgetsPrefill({ budgets_set: false,
                                          wizard_steps: {} }, s),
                   { income: "", food: "", other: "" });
});

test("budgets step: a stored first-run seed is not a plan the household saved", () => {
  // the server stores its seed once a month has closed: budgets_set holds,
  // and only budgets_saved_by_user says nobody chose it
  const seed = { budgets_set: true, budgets_saved_by_user: false };
  assert.equal(planSaved(seed), false);
  assert.equal(budgetsStepKept(seed), false);
  const s = { food_monthly: 600, other_monthly: 900 };
  assert.deepEqual(manualBudgetsPrefill(seed, s),
                   { income: "", food: "", other: "" });
  // a saved plan with a budget in it is kept
  const saved = { budgets_set: true, budgets_saved_by_user: true };
  assert.equal(planSaved(saved), true);
  assert.equal(budgetsStepKept(saved), true);
  // saved, but nothing above zero stored: Continue still waits
  assert.equal(budgetsStepKept({ budgets_set: false,
                                 budgets_saved_by_user: true }), false);
  // an older server without the field answers with budgets_set
  assert.equal(planSaved({ budgets_set: true }), true);
  assert.equal(budgetsStepKept({ budgets_set: true }), true);
  assert.equal(planSaved({ budgets_set: false }), false);
});

test("manual budgets: a save with no food or everything-else is refused", () => {
  const words = "Enter a food or everything-else budget, or tap a suggestion.";
  assert.equal(manualBudgetsRefusal("", ""), words);
  assert.equal(manualBudgetsRefusal("0", "$0"), words);
  assert.equal(manualBudgetsRefusal("abc", ""), words);
  assert.equal(manualBudgetsRefusal("", "450"), null);
  assert.equal(manualBudgetsRefusal("1,200", ""), null);
  // the phone's step uses the shared rules, and the web's words
  const src = readFileSync(new URL("../src/components/wizard-budgets.tsx",
                                   import.meta.url), "utf8");
  assert.ok(src.includes("manualBudgetsBody(") && src.includes("manualBudgetsRefusal("),
            "wizard-budgets.tsx must build its body from the shared rules");
  assert.ok(src.includes('"Saved — Continue below."'));
  assert.ok(src.includes("(kept)"));
  assert.ok(!/food_monthly: numv\(food\)/.test(src),
            "a blank box must never be sent as $0 over a stored budget");
});

// ---- the Today checklist: data facts, never wizard marks ----
test("checklist: five steps from the household's data, the web's order", () => {
  const ob = { accounts: 2, transactions: 78, bills: 0, budgets_set: true,
               primary_checking_set: false };
  const steps = checklistSteps(ob);
  assert.deepEqual(steps.map((s) => s.key),
                   ["accounts", "history", "bills", "budgets", "checking"]);
  assert.deepEqual(steps.map((s) => s.done), [true, true, false, true, false]);
  assert.equal(checklistOpen(ob), true);
  assert.equal(checklistSummary(ob),
               "3/5 done — still open: Confirm recurring bills, "
               + "Pick your primary checking");
  const all = { ...ob, bills: 4, primary_checking_set: true };
  assert.equal(checklistOpen(all), false);
  assert.equal(checklistSummary(all), "5/5 done");
});

test("checklist: the checking step is done when there is nothing to pin", () => {
  const base = { accounts: 1, transactions: 5, bills: 2, budgets_set: true,
                 primary_checking_set: false };
  const none = { ...base, checking_candidates: [] };
  assert.equal(checklistSteps(none).find((s) => s.key === "checking").done, true);
  assert.equal(checklistOpen(none), false);
  assert.equal(checklistSummary(none), "5/5 done");
  const some = { ...base, checking_candidates: ["a1", "a2"] };
  assert.equal(checklistSteps(some).find((s) => s.key === "checking").done, false);
  assert.equal(checklistOpen(some), true);
  // an older server omits the field: only a pin closes the step
  assert.equal(checklistOpen(base), true);
  assert.equal(checklistOpen({ ...base, primary_checking_set: true }), false);
});

test("checklist: the bills step leaves skipped while proposals are pending", () => {
  assert.deepEqual(billsContinue(0), { label: "Continue →", state: "done" });
  const two = billsContinue(2);
  assert.equal(two.label, "Leave 2 pending →");
  assert.equal(two.state, "skipped");
  assert.match(two.hint, /^2 proposals still await/);
  assert.match(billsContinue(1).hint, /^1 proposal still/);
});

test("checklist: the connect step asks for the primary checking only between two", () => {
  assert.equal(connectDoneHint(0, false), undefined);
  assert.equal(connectDoneHint(1, false), undefined);
  assert.equal(connectDoneHint(2, false), "pick your primary checking below");
  assert.equal(connectDoneHint(2, true), undefined);
});

// ---- a bank keeps the window it was linked with ----
test("history window: a bank linked narrower than the household's choice", () => {
  assert.equal(windowNarrower(1, null), true);     // last month vs everything
  assert.equal(windowNarrower(3, 12), true);
  assert.equal(windowNarrower(12, 12), false);
  assert.equal(windowNarrower(12, 3), false);      // wider is never narrower
  assert.equal(windowNarrower(null, 1), false);    // everything is the widest
  assert.equal(windowNarrower(undefined, null), false);  // no window stamped
  assert.equal(historyRowNote(1), "history: last month");
  assert.equal(historyRowNote(null), "history: everything available");
});

// ---- lib/richtext.ts: the little markdown the assistant writes ----
import { inlineRuns, itemLabel, parseRich, plainText } from "../src/lib/richtext.ts";

test("mergeTurns: a row read twice from older pages shows once", () => {
  const row = (id, at) => ({ id, question: id, answer: "", blocks: [], tools_used: [],
                             asked_at: at, asked_by: "", thread_id: id });
  const first = [row("a", "2026-10-19T10:00:00Z")];
  const older = [row("b", "2026-10-18T10:00:00Z"), row("b", "2026-10-18T10:00:00Z"),
                 row("a", "2026-10-19T10:00:00Z")];
  assert.deepEqual(mergeTurns([], first, older).map((t) => t.id), ["a", "b"]);
});

test("richtext: **bold** is a bold run and nothing else is markup", () => {
  assert.deepEqual(inlineRuns("Worth **$12,345.67** now 2 * 3"),
    [{ bold: false, text: "Worth " }, { bold: true, text: "$12,345.67" },
     { bold: false, text: " now 2 * 3" }]);
  assert.deepEqual(inlineRuns("<b>no</b> ACH__REF__1 __x__"),
    [{ bold: false, text: "<b>no</b> ACH__REF__1 __x__" }]);
  assert.deepEqual(inlineRuns(""), [{ bold: false, text: "" }]);
});

test("richtext: bullets, numbered lists, headings and paragraphs come apart", () => {
  const got = parseRich("Intro line\nsecond line\n\n- **A** one\n* B two\n\n1. first\n2) second\n# Head\ntail");
  assert.deepEqual(got.map((b) => b.kind), ["p", "list", "list", "p", "p"]);
  assert.deepEqual(got[0], { kind: "p", runs: [{ bold: false, text: "Intro line" },
    { bold: false, text: "\n" }, { bold: false, text: "second line" }] });
  assert.equal(got[1].kind === "list" && got[1].ordered, false);
  assert.equal(got[1].kind === "list" && got[1].items.length, 2);
  assert.deepEqual(got[1].kind === "list" && got[1].items[0],
    { runs: [{ bold: true, text: "A" }, { bold: false, text: " one" }], n: null });
  assert.equal(got[2].kind === "list" && got[2].ordered, true);
  assert.deepEqual(got[2].kind === "list" && got[2].items.map((it) => it.n), [1, 2]);
  assert.deepEqual(got[3], { kind: "p", runs: [{ bold: true, text: "Head" }] });
  assert.deepEqual(parseRich(""), []);
  assert.deepEqual(parseRich("\n\n"), []);
});

test("richtext: a numbered item keeps its own number", () => {
  // a sub-bullet is not the next rank; a blank line does not restart
  // at 1; a list going on after a chart still reads 4, 5
  const got = parseRich("1. Rent\n   - paid\n2. Groceries\n\n3. Dining");
  assert.deepEqual(got.map((b) => b.kind === "list" ? [b.ordered, b.items.map((it) => it.n)] : b.kind),
    [[true, [1]], [false, [null]], [true, [2]], [true, [3]]]);
  const later = parseRich("4. Utilities\n5. Fuel");
  assert.equal(later[0].kind === "list" && later[0].items.map((it) => itemLabel(it, 0)).join(" "), "4. 5.");
  assert.equal(itemLabel({ runs: [], n: null }, 2), "3.");
  // a digit run past nine digits is prose, as on the server
  assert.equal(parseRich("1234567890. ten")[0].kind, "p");
  assert.equal(parseRich("123456789. nine")[0].kind, "list");
});

test("richtext: copy carries the words without the asterisks, numbers kept", () => {
  assert.equal(plainText("**Cash** is small.\n\n- **A** x\n- B\n\n1. one\n\n2. two"),
    "Cash is small.\n\n- A x\n- B\n\n1. one\n\n2. two");
});

// ---- the retirement wizard's birthdate: the one answer that persists,
// so a shape the settings door would refuse is caught before the save ----

import { birthdateIso } from "../src/lib/pure.ts";

test("birthdateIso reads ISO and US dates, refuses the rest", () => {
  const now = new Date(2026, 0, 15);
  assert.deepEqual(birthdateIso("", now), { iso: "" });
  assert.deepEqual(birthdateIso("1970-03-09", now), { iso: "1970-03-09" });
  assert.deepEqual(birthdateIso(" 3/9/1970 ", now), { iso: "1970-03-09" });
  assert.ok("error" in birthdateIso("9 March 1970", now));
  assert.ok("error" in birthdateIso("1970-02-30", now));   // no such day
  assert.ok("error" in birthdateIso("13/1/1970", now));    // no such month
  assert.ok("error" in birthdateIso("2027-01-01", now));   // future
});

import { ownedSinceIso } from "../src/lib/pure.ts";

test("ownedSinceIso: blank keeps the stored date, a real past day goes out", () => {
  const now = new Date(2026, 10, 17);
  assert.deepEqual(ownedSinceIso("", now), { iso: "" });
  assert.deepEqual(ownedSinceIso("2019-04-12", now), { iso: "2019-04-12" });
  assert.deepEqual(ownedSinceIso("4/12/2019", now), { iso: "2019-04-12" });
  assert.deepEqual(ownedSinceIso("2026-11-17", now), { iso: "2026-11-17" });
  assert.ok("error" in ownedSinceIso("2019-02-30", now));  // no such day
  assert.ok("error" in ownedSinceIso("2027-01-01", now));  // future
  assert.ok("error" in ownedSinceIso("April 2019", now));
});

// ---- the split editor reads each part the way every other money field
// does, so a decimal-comma keypad can enter cents ----

import { splitDraftCheck } from "../src/lib/pure.ts";

test("split editor: a part typed with a decimal comma counts its cents", () => {
  const got = splitDraftCheck(-45.67, [
    { category: "GROCERIES", amount: "33,17" },
    { category: "DINING", amount: "12,50" }]);
  assert.deepEqual(got.cents, [3317, 1250]);
  assert.equal(got.left, 0);
  assert.equal(got.ready, true);
  // pasted forms with a currency sign or a thousands separator read too
  const big = splitDraftCheck(1250.5, [
    { category: "A", amount: "$1,234.56" }, { category: "B", amount: "15.94" }]);
  assert.equal(big.left, 0);
  assert.equal(big.ready, true);
  // a part that is not an amount yet is not ready and adds nothing
  const half = splitDraftCheck(10, [
    { category: "A", amount: "10" }, { category: "B", amount: "-" }]);
  assert.deepEqual(half.cents, [1000, null]);
  assert.equal(half.ready, false);
});

// ---- restarting the wizard clears every step mark the server keeps ----

import { wizardRestartMarks } from "../src/components/wizard-logic.ts";

test("wizard restart: every step key is cleared, the first step included", () => {
  const marks = wizardRestartMarks();
  for (const k of WIZARD_STEPS) assert.equal(marks[k], null, k);
  assert.ok(Object.values(marks).every((v) => v === null));
});

// The ledger and business-books downloads are owner-only on the server: a
// member or a viewer is refused. Both clients must not draw a control that
// can only fail, so every download of those doors sits behind the owner
// gate, and a new screen reaching one must be added here to be checked.
// Read from source: the gate is a render condition no test can call.
test("ledger and business-book downloads are owner-only on both clients", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  const doors = /\/api\/transactions\/export|\/api\/business\/export\.csv|\/pnl\.csv|\/package\.zip/;
  // [file, the control's anchor, the gate that must precede it closely]
  const sites = [
    ["../../webapp/src/pages/Transactions.tsx",
     "<a href={`/api/transactions/export", "isOwner(me)"],
    ["../../webapp/src/pages/Business.tsx", "href={csv}", "isOwner(meQ)"],
    ["../../webapp/src/pages/Business.tsx", "href={exportHref}", "isOwner(me)"],
    ["../src/app/(tabs)/transactions.tsx", "onPress={exportRows}", "mayExport &&"],
    ["../src/app/business.tsx", "/api/business/export.csv", "owner &&"],
    ["../src/app/business.tsx", "/pnl.csv", "owner &&"],
    ["../../webapp/src/pages/Business.tsx", "/package.zip", "isOwner(meQ) &&"],
    ["../src/app/business.tsx", "/package.zip", "owner &&"],
  ];
  for (const [file, anchor, gate] of sites) {
    const src = read(file);
    const i = src.indexOf(anchor);
    assert.ok(i >= 0, `${file}: anchor ${anchor} not found`);
    assert.ok(src.slice(Math.max(0, i - 600), i).includes(gate),
              `${file} @ ${anchor}: must sit behind ${gate}`);
  }
  // every file that names one of the doors is one of the sites above
  const known = new Set(sites.map(([f]) => new URL(f, import.meta.url).pathname));
  known.add(new URL("../../webapp/src/api/client.ts", import.meta.url).pathname);
  // the generated API types name every door's path as a type key, and
  // reach none of them
  known.add(new URL("../../webapp/src/api/api.generated.ts", import.meta.url).pathname);
  known.add(new URL("../src/lib/api.generated.ts", import.meta.url).pathname);
  const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(new URL(e.name + "/", dir))
      : /\.tsx?$/.test(e.name) ? [new URL(e.name, dir)] : []);
  for (const root of ["../src/", "../../webapp/src/"])
    for (const f of walk(new URL(root, import.meta.url)))
      if (doors.test(readFileSync(f, "utf8")))
        assert.ok(known.has(f.pathname),
                  `${f.pathname} reaches an owner-only download: gate it and list it here`);
});

import { describedChoiceOptions, planUnplaced } from "../src/lib/pure.ts";

test("cards sharing a name are told apart in the plan's choice picker", () => {
  // invented accounts: two cards stored as the same "Visa …1111"
  const card = { name: "Visa …1111", mask: null };
  const opts = describedChoiceOptions([
    { id: "manual:visa-1111", ...card, rows: 3, first_date: "2021-05-01",
      last_date: "2021-05-03", balance_cents: -5100 },
    { id: "manual:visa-1111-2", ...card, rows: 1, first_date: "2022-02-10",
      last_date: "2022-02-10", balance_cents: 12345 },
    { id: "manual:visa-1111-3", ...card, rows: 1, first_date: "2022-02-10",
      last_date: "2022-02-10", balance_cents: 12345 },
    { id: "chk", name: "Checking", mask: "0042", rows: 0, first_date: null,
      last_date: null, balance_cents: null }]);
  // the web Import page's planChoiceOptions, word for word
  assert.deepEqual(opts, [
    ["manual:visa-1111",
     "Visa …1111 — 3 rows · 05/01/21–05/03/21 · balance -$51.00"],
    ["manual:visa-1111-2", "Visa …1111 — 1 row · 02/10/22–02/10/22 · "
                           + "balance $123.45 (manual:visa-1111-2)"],
    ["manual:visa-1111-3", "Visa …1111 — 1 row · 02/10/22–02/10/22 · "
                           + "balance $123.45 (manual:visa-1111-3)"],
    ["chk", "Checking …0042 — 0 rows"]]);
});

test("a choose-one row holds the import until a card is picked", () => {
  const row = { action: "import", kind: "CSV", account_id: null,
                new_account_name: "",
                choices: [{ id: "a", name: "Visa …1111", mask: null }] };
  assert.equal(planUnplaced([row]).length, 1);
  assert.equal(planUnplaced([{ ...row, account_id: "a" }]).length, 0);
  assert.equal(planUnplaced([{ ...row, action: "skip" }]).length, 0);
  assert.equal(planUnplaced([{ ...row, kind: "Oikonome export ZIP" }])
    .length, 0);
  assert.equal(planUnplaced([{ ...row, new_account_name: "  " }]).length, 1);
  assert.equal(planUnplaced([{ ...row, new_account_name: "Card" }]).length,
               0);
});

// ---- a rule on several days of the month ----

import * as webDays from "../../webapp/src/monthdays.ts";
import * as mobDays from "../src/lib/monthdays.ts";

test("the two copies of the several-days helpers are the same text", () => {
  assert.equal(
    readFileSync(new URL("../src/lib/monthdays.ts", import.meta.url), "utf8"),
    readFileSync(new URL("../../webapp/src/monthdays.ts", import.meta.url), "utf8"));
});

test("the several-days words read as the server's", () => {
  const w = mobDays.severalDaysWords;
  assert.equal(w([15, -1]), "twice a month (15th and last day)");
  assert.equal(w([-1, 15]), "twice a month (15th and last day)");
  assert.equal(w([1, 15]), "twice a month (1st and 15th)");
  assert.equal(w([2, 23]), "twice a month (2nd and 23rd)");
});

test("the picker offers two days only when there are days to offer", () => {
  const list = [["MONTHLY:1", "monthly"]];
  assert.deepEqual(mobDays.withSeveralDays(list, null), list);
  assert.deepEqual(mobDays.withSeveralDays(list, [15]), list);
  assert.equal(mobDays.withSeveralDays(list, [15, -1])[0][0], mobDays.TWICE_A_MONTH);
  assert.equal(mobDays.cadencePick("MONTHLY:1", [15, -1]), mobDays.TWICE_A_MONTH);
  assert.equal(mobDays.cadencePick("MONTHLY:1", null), "MONTHLY:1");
});

test("a save names days only when the person changed them", () => {
  const f = mobDays.cadenceFields, T = mobDays.TWICE_A_MONTH;
  // untouched: the bill's rule stands, and a stale form cannot restate it
  assert.deepEqual(f(T, [15, -1], [15, -1], "2031-02-15"), { cadence: "MONTHLY:1" });
  // a fit's days onto a bill or a new one
  assert.deepEqual(f(T, null, [1, 15], "2031-02-01"),
                   { cadence: "MONTHLY:1", by_month_day: [1, 15] });
  assert.deepEqual(f(T, [15, -1], [1, 15], "2031-02-01"),
                   { cadence: "MONTHLY:1", by_month_day: [1, 15] });
  // leaving two days for one names the one
  assert.deepEqual(f("MONTHLY:1", [15, -1], [15, -1], "2031-02-20"),
                   { cadence: "MONTHLY:1", by_month_day: [20] });
  // a plain monthly bill stays what the old clients send
  assert.deepEqual(f("MONTHLY:1", null, null, "2031-02-20"), { cadence: "MONTHLY:1" });
  assert.deepEqual(f("WEEKLY:2", [15, -1], [15, -1], "2031-02-20"), { cadence: "WEEKLY:2" });
});

test("the wizard's Adjust leaves an offer's two days only on request", () => {
  const p = { payee: "Payer", amount: 100, next_due: "2031-02-15",
              frequency: "MONTHLY", interval: 1, by_month_day: [15, -1] };
  const form = (o) => ({ name: "Payer", amount: "100.00", dueDay: "15", ...o });
  const T = mobDays.TWICE_A_MONTH;
  assert.equal(proposalCadenceValue(p), T);
  assert.equal(cadenceTakesDueDay(T), false);
  assert.deepEqual(adjustOf(p, form({ cadence: T })).adjust, {});
  // a day the hidden field still holds is not a move
  assert.deepEqual(adjustOf(p, form({ cadence: T, dueDay: "14" })).adjust, {});
  assert.deepEqual(adjustOf(p, form({ cadence: "MONTHLY:1" })).adjust,
                   { cadence: "MONTHLY:1", by_month_day: [15] });
  assert.deepEqual(adjustOf(p, form({ cadence: "MONTHLY:1", dueDay: "20" })).adjust,
                   { cadence: "MONTHLY:1", due_day: 20, by_month_day: [20] });
  assert.deepEqual(adjustOf(p, form({ cadence: "WEEKLY:2" })).adjust,
                   { cadence: "WEEKLY:2" });
});

// ---- copying a one-time secret ----
import { copySecretWith, SECRET_CLIPBOARD_TTL_MS, SecretClipboardClear,
         signedOutOthers } from "../src/lib/pure.ts";

function fakeClipboard({ failSet = false, setReturns = true,
                         failGet = false } = {}) {
  const cb = { value: "", timers: [] };
  cb.ops = {
    set: async (t) => { if (failSet) throw new Error("refused");
                        cb.value = t; return setReturns; },
    get: async () => { if (failGet) throw new Error("denied");
                       return cb.value; },
  };
  cb.schedule = (fn, ms) => { cb.timers.push({ fn, ms }); };
  return cb;
}

test("a copied secret is cleared from the clipboard once its time is up", async () => {
  const cb = fakeClipboard();
  assert.equal(await copySecretWith(cb.ops, "aaaa-bbbb", cb.schedule), true);
  assert.equal(cb.value, "aaaa-bbbb");
  assert.equal(cb.timers.length, 1);
  assert.equal(cb.timers[0].ms, SECRET_CLIPBOARD_TTL_MS);
  await cb.timers[0].fn();
  assert.equal(cb.value, "");
});

test("the timed clear leaves alone whatever the user copied since", async () => {
  const cb = fakeClipboard();
  await copySecretWith(cb.ops, "aaaa-bbbb", cb.schedule);
  cb.value = "something else";
  await cb.timers[0].fn();
  assert.equal(cb.value, "something else");
});

test("a refused copy reports failure instead of throwing, and schedules nothing", async () => {
  const thrown = fakeClipboard({ failSet: true });
  assert.equal(await copySecretWith(thrown.ops, "x", thrown.schedule), false);
  assert.equal(thrown.timers.length, 0);
  const refused = fakeClipboard({ setReturns: false });
  assert.equal(await copySecretWith(refused.ops, "x", refused.schedule), false);
  assert.equal(refused.timers.length, 0);
});

test("a clipboard that cannot be read back is never cleared blind", async () => {
  // no reader (a platform where reading prompts the user): no timer at all
  const cb = fakeClipboard();
  assert.equal(await copySecretWith({ set: cb.ops.set }, "x", cb.schedule), true);
  assert.equal(cb.timers.length, 0);
  // a read that fails when the timer fires is swallowed and clears nothing
  const denied = fakeClipboard({ failGet: true });
  await copySecretWith(denied.ops, "x", denied.schedule);
  await denied.timers[0].fn();
  assert.equal(denied.value, "x");
});

function clockedClear() {
  const clock = { t: 1_000_000 };
  const clear = new SecretClipboardClear(() => clock.t);
  return { clock, clear };
}

test("coming back to the app after the minute clears a secret the paused timer never did", async () => {
  // Android stops JS timers while the app is in the background, which is
  // exactly when the user is pasting elsewhere: the foreground return is
  // the first moment the clear can run.
  const cb = fakeClipboard();
  const { clock, clear } = clockedClear();
  await copySecretWith(cb.ops, "aaaa-bbbb", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  await clear.appStateChanged("background");
  clock.t += SECRET_CLIPBOARD_TTL_MS + 1;
  assert.equal(cb.value, "aaaa-bbbb");
  await clear.appStateChanged("active");
  assert.equal(cb.value, "");
});

test("coming back before the minute is up leaves the secret to be pasted", async () => {
  const cb = fakeClipboard();
  const { clock, clear } = clockedClear();
  await copySecretWith(cb.ops, "aaaa-bbbb", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  await clear.appStateChanged("background");
  clock.t += SECRET_CLIPBOARD_TTL_MS - 1;
  await clear.appStateChanged("active");
  assert.equal(cb.value, "aaaa-bbbb");
  // ...and the later return, once the minute has passed, still clears it
  clock.t += 2;
  await clear.appStateChanged("active");
  assert.equal(cb.value, "");
});

test("leaving the screen after the minute clears the secret, before it does not", async () => {
  const cb = fakeClipboard();
  const { clock, clear } = clockedClear();
  await copySecretWith(cb.ops, "aaaa-bbbb", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  await clear.released();  // Copy & dismiss unmounts at once: keep the clip
  assert.equal(cb.value, "aaaa-bbbb");
  clock.t += SECRET_CLIPBOARD_TTL_MS;
  await clear.released();
  assert.equal(cb.value, "");
});

test("the foreground-return clear keeps whatever the user copied since", async () => {
  const cb = fakeClipboard();
  const { clock, clear } = clockedClear();
  await copySecretWith(cb.ops, "aaaa-bbbb", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  cb.value = "something else";
  clock.t += SECRET_CLIPBOARD_TTL_MS + 1;
  await clear.appStateChanged("active");
  assert.equal(cb.value, "something else");
});

test("a read refused on the return is tried again on the next return", async () => {
  const cb = fakeClipboard();
  let refuse = true;
  const ops = { set: cb.ops.set,
                get: async () => { if (refuse) throw new Error("no focus");
                                   return cb.value; } };
  const { clock, clear } = clockedClear();
  await copySecretWith(ops, "aaaa-bbbb", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  clock.t += SECRET_CLIPBOARD_TTL_MS + 1;
  await clear.appStateChanged("active");
  assert.equal(cb.value, "aaaa-bbbb");
  refuse = false;
  await clear.appStateChanged("active");
  assert.equal(cb.value, "");
});

test("an older copy's timer does not clear a newer secret early", async () => {
  const cb = fakeClipboard();
  const { clear } = clockedClear();
  await copySecretWith(cb.ops, "first", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  await copySecretWith(cb.ops, "second", cb.schedule,
                       SECRET_CLIPBOARD_TTL_MS, clear);
  await cb.timers[0].fn();
  assert.equal(cb.value, "second");
  await cb.timers[1].fn();
  assert.equal(cb.value, "");
});

test("a clear in flight when a newer secret is copied leaves the newer secret alone", async () => {
  // The clear reads the clipboard; in that bridge round trip the user
  // copies a new secret shown once. The native side applies writes in
  // order, so a clear dispatched after the new copy would empty it — and
  // the sheet has already been dismissed as copied.
  const cb = { value: "" };
  let holdGet = null, holdSet = null;
  const ops = {
    get: () => {
      const seen = cb.value;  // read when dispatched, answered later
      return new Promise((r) => { holdGet = () => r(seen); });
    },
    set: (t) => {
      cb.value = t;  // applied on dispatch, acknowledged later
      if (t === "second") return new Promise((r) => { holdSet = () => r(true); });
      return Promise.resolve(true);
    },
  };
  const { clock, clear } = clockedClear();
  await copySecretWith(ops, "first", () => {}, SECRET_CLIPBOARD_TTL_MS, clear);
  clock.t += SECRET_CLIPBOARD_TTL_MS + 1;
  const sweeping = clear.appStateChanged("active");  // reads "first"
  const copying = copySecretWith(ops, "second", () => {},
                                 SECRET_CLIPBOARD_TTL_MS, clear);
  assert.equal(cb.value, "second");
  holdGet();
  await sweeping;
  holdSet();
  assert.equal(await copying, true);
  assert.equal(cb.value, "second");
  // ...and the newer secret's own clear still runs once its minute is up
  clock.t += SECRET_CLIPBOARD_TTL_MS + 1;
  const later = clear.appStateChanged("active");
  holdGet();
  await later;
  assert.equal(cb.value, "");
});

test("a factor change says every other session was signed out", () => {
  assert.equal(signedOutOthers("Two-factor is off"),
               "Two-factor is off — every other session was signed out.");
});

test("a pasted grouped one-time code keeps all six digits", () => {
  assert.equal(pureModule.oneTimeCodeDigits("123 456"), "123456");
  assert.equal(pureModule.oneTimeCodeDigits(" 123-456\n"), "123456");
  assert.equal(pureModule.oneTimeCodeDigits("12345"), "12345");
  // typing past six digits stops at six, as the old field cap did
  assert.equal(pureModule.oneTimeCodeDigits("1234567"), "123456");
  assert.equal(pureModule.oneTimeCodeDigits(""), "");
});

test("no one-time-code field caps the raw text at six characters", () => {
  // the cap would cut a grouped paste ("123 456") before it is cleaned
  for (const rel of ["../src/components/elevation-modal.tsx",
                     "../src/app/security.tsx",
                     "../src/app/notifications.tsx",
                     "../../webapp/src/elevation.tsx"]) {
    const src = readFileSync(new URL(rel, import.meta.url), "utf8");
    assert.doesNotMatch(src, /maxLength=\{6\}/, rel);
  }
});

test("a failed refetch over a loaded business summary keeps the wizard open", () => {
  // Only a FIRST load that failed may replace the wizard with the error
  // card; a background refetch failing over loaded data must not unmount
  // the wizard and throw away the answers typed so far.
  const src = readFileSync(
    new URL("../src/app/business-wizard.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(src, /if \(summary\.isError\)/);
  assert.match(src, /if \(summary\.isLoadingError\)/);
});

test("an alert push opens Security while a two-factor recovery is pending", () => {
  // The relay sends one textless "alert" event for budget alerts and
  // security notices alike; the pending recovery is what tells them apart,
  // and Alerts has no control that can cancel it.
  const lands = "2026-11-14T10:00:00+00:00";
  assert.equal(pureModule.pushTapRoute("/alerts",
    { factor_reset_pending: lands }), "/security");
  assert.equal(pureModule.pushTapRoute("/alerts",
    { factor_reset_pending: null }), "/alerts");
  assert.equal(pureModule.pushTapRoute("/alerts", undefined), "/alerts");
  // a report push still opens its report
  assert.equal(pureModule.pushTapRoute("/month?y=2026&m=10",
    { factor_reset_pending: lands }), "/month?y=2026&m=10");
});

test("a pending two-factor recovery is announced with its landing date", () => {
  const fmt = (iso) => iso.slice(0, 10);
  const line = pureModule.pendingRecoveryLine(
    "2026-11-14T10:00:00+00:00", fmt);
  assert.match(line, /2026-11-14/);
  assert.match(line, /Not you\?/);
  assert.equal(pureModule.pendingRecoveryLine(null, fmt), null);
  assert.equal(pureModule.pendingRecoveryLine(undefined, fmt), null);
  assert.equal(pureModule.pendingRecoveryLine("", fmt), null);
});

test("Today and Alerts link a pending recovery to the card that cancels it", () => {
  for (const rel of ["../src/app/alerts.tsx", "../src/app/(tabs)/index.tsx"]) {
    const src = readFileSync(new URL(rel, import.meta.url), "utf8");
    assert.match(src, /pendingRecoveryLine\(/, rel);
    assert.match(src, /router\.push\("\/security"/, rel);
  }
  const layout = readFileSync(
    new URL("../src/app/_layout.tsx", import.meta.url), "utf8");
  assert.match(layout, /pushTapRoute\(/);
  for (const rel of ["../../webapp/src/pages/Alerts.tsx",
                     "../../webapp/src/pages/Today.tsx"]) {
    const src = readFileSync(new URL(rel, import.meta.url), "utf8");
    assert.match(src, /me\.data\?\.factor_reset_pending &&/, rel);
    assert.match(src, /to="\/settings\/security"/, rel);
  }
});

test("a pending recovery's landing day is the local day of the instant, not its UTC day", () => {
  // 01:30 UTC on the 14th is 5:30 pm on the 13th in Pacific time; telling
  // that household "the 14th" gives an attacker a day they think they have
  withTz("America/Los_Angeles", () => {
    const line = pureModule.pendingRecoveryLine(
      "2026-11-14T01:30:00+00:00", (iso) => iso);
    assert.match(line, /lands 2026-11-13\./);
  });
  withTz("Asia/Tokyo", () => {
    assert.match(pureModule.pendingRecoveryLine(
      "2026-11-14T01:30:00+00:00", (iso) => iso), /lands 2026-11-14\./);
  });
  // the web banner renders the same instant through the local-day helper
  const today = readFileSync(
    new URL("../../webapp/src/pages/Today.tsx", import.meta.url), "utf8");
  assert.match(today, /mmdd\(localDayOf\(me\.data\.factor_reset_pending\)\)/);
});

test("a scan refused because another pass runs is told apart from a failure", () => {
  const { scanAlreadyRunning } = pureModule;
  assert.equal(scanAlreadyRunning({ status: 409, message: "x" }), true);
  assert.equal(scanAlreadyRunning(new Error("bill detection is already running")), true);
  assert.equal(scanAlreadyRunning({ status: 500, message: "boom" }), false);
});

// ---- the household's calendar day, on both clients ----
//
// The server answers "today" and "this month" on the household's clock
// (/api/me `timezone`); a browser or phone in another zone must ask the
// same clock, or near midnight the live day is a day off — its yesterday
// unreachable, its new month greyed out.

for (const [side, m] of [["mobile", mobileRecip], ["web", webMoney]]) {
  test(`${side}: today is the household's calendar day, not the device's`, () => {
    // 01:00 UTC on Nov 7: already the 7th in London, still the 6th in LA
    const at = new Date("2026-11-07T01:00:00Z");
    withTz("America/Los_Angeles", () => {
      assert.equal(m.householdYmd(at, "Europe/London"), "2026-11-07");
      assert.equal(m.householdYmd(at, "America/Los_Angeles"), "2026-11-06");
      // before /api/me answers, or a zone this runtime can't read: the device
      assert.equal(m.householdYmd(at, undefined), "2026-11-06");
      assert.equal(m.householdYmd(at, "Not/AZone"), "2026-11-06");
      // a month boundary: London is on Dec 1 while LA is on Nov 30
      const d = m.householdDay(new Date("2026-12-01T00:30:00Z"), "Europe/London");
      assert.deepEqual([d.getFullYear(), d.getMonth() + 1, d.getDate()], [2026, 12, 1]);
    });
  });
}

test("the lenses and the business screen read today from the household clock", () => {
  const home = readFileSync(
    new URL("../../webapp/src/pages/Home.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(home, /const now = new Date\(\)/);
  assert.match(home, /householdDay\(new Date\(\), me\.data\?\.timezone\)/);
  const biz = readFileSync(new URL("../src/app/business.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(biz, /ymd\(new Date\(\)\)|new Date\(\)\.getFullYear\(\)|TaxYears?\(new Date\(\)/);
});

test("a channel switch already on can always be turned off, even once the channel is gone", () => {
  // the web's chanBox rule: a ticked box can always be unticked
  const src = readFileSync(new URL("../src/app/notifications.tsx", import.meta.url), "utf8");
  assert.match(src, /value=\{!!c\.sms\} disabled=\{!smsOk && !c\.sms\}/);
  assert.match(src, /value=\{!!c\.push\} disabled=\{!pushOk && !c\.push\}/);
});

test("the welcome summary counts a self-muted owner as not mailed and can say nobody", () => {
  const src = readFileSync(
    new URL("../../webapp/src/pages/Welcome.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(src, /off: !r\.owner &&/);
  assert.doesNotMatch(src, /Math\.max\(1, recipientRows/);
});

test("transactions and bills read this month and due days from the household clock", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const rel of ["../../webapp/src/pages/Transactions.tsx",
                     "../../webapp/src/pages/Bills.tsx",
                     "../src/app/(tabs)/transactions.tsx",
                     "../src/app/(tabs)/bills.tsx"]) {
    const src = read(rel);
    // the device's midnight or month standing in for the household's
    assert.doesNotMatch(src, /const (now|n|today) = new Date\(\);|ymOf\(new Date\(\)\)/, rel);
    assert.match(src, /householdDay\(new Date\(\), (me\.data\?\.timezone|zone)\)/, rel);
  }
  // the due countdown is the server's, counted from the household's day
  // (api_bills.due_words); the phone counts it on the device only for an
  // older server, from the zone
  assert.doesNotMatch(read("../../webapp/src/pages/Bills.tsx"), /daysUntil\(/);
  assert.match(read("../../webapp/src/pages/Bills.tsx"), /\{b\.due_text\}/);
  assert.match(read("../src/app/(tabs)/bills.tsx"), /olderDueWords\(b, zone\)/);
});

test("the month and year lenses never ask for the whole Today payload", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const rel of ["../../webapp/src/pages/MonthLens.tsx",
                     "../../webapp/src/pages/YearLens.tsx",
                     "../src/app/month.tsx", "../src/app/year.tsx"]) {
    const src = read(rel);
    // the map's rows (Savings included) come on the lens reply itself
    assert.doesNotMatch(src, /todayFull|savings_goals|LensGoal/, rel);
    assert.match(src, /sentPlanRows\(d\.plan_rows/, rel);
  }
});

test("the tab warm-ups run once per session, after Today's own fresh answer", () => {
  const src = readFileSync(new URL("../src/app/_layout.tsx", import.meta.url), "utf8");
  const pre = src.slice(src.indexOf("function Prefetcher"), src.indexOf("function Gate"));
  // once: a ref remembers the session it warmed for
  assert.match(pre, /warmedFor\.current === client\) return;/);
  // after Today: a persisted answer restored at launch does not count
  assert.match(pre, /dataUpdatedAt/);
  assert.match(pre, /\[phase, client, todayFresh\]/);
});

import { RET_WIZARD_STEPS, retWizardMidway, retWizardPrefill,
         retWizardResumeAt, retWizardStepBody } from "../src/lib/pure.ts";

test("retirement wizard resumes at the first step it has no answer for", () => {
  assert.equal(retWizardResumeAt(undefined), 0);
  assert.equal(retWizardResumeAt({}), 0);
  assert.equal(retWizardResumeAt({ age: "done" }), 1);
  assert.equal(retWizardResumeAt({ age: "done", spend: "done" }), 2);
  // a gap is answered before anything after it
  assert.equal(retWizardResumeAt({ age: "done", saving: "done" }), 1);
  // every question answered but never finished: the summary step
  assert.equal(retWizardResumeAt(
    { age: "done", spend: "done", saving: "done", assumptions: "done" }), 4);
  // a finished (or skipped) walkthrough opened again starts at the top
  assert.equal(retWizardResumeAt({ age: "done", done: "done" }), 0);
  assert.equal(retWizardResumeAt({ done: "skipped" }), 0);
});

test("a retirement wizard left halfway re-opens even once a birthdate is saved", () => {
  assert.equal(retWizardMidway(undefined), false);
  assert.equal(retWizardMidway({}), false);
  assert.equal(retWizardMidway({ age: "done" }), true);
  assert.equal(retWizardMidway({ age: "done", done: "skipped" }), false);
  assert.equal(retWizardMidway({ done: "done" }), false);
});

// The answer keys a server from before the format mark accepts: it refuses
// a whole settings save over any other key inside ret_wizard_answers, so a
// current client never puts one there (the mark and the claim age ride as
// top-level fields, which that server ignores).
const OLD_SERVER_ANSWER_KEYS = new Set(
  ["age", "end", "spend", "employer_mo", "taxable_mo", "ret", "infl"]);

test("each retirement wizard step confirms every box on it, marked", () => {
  // walking the wizard is answering it: a step left with Continue saves
  // each of its boxes as the person's answer, whether retyped or not
  const draft = { age: "52", spend: "60,000", employer_mo: "1,250.50",
                  taxable_mo: "", ret: "6.5", infl: "3", end: "nope",
                  ssage: "67" };
  assert.deepEqual(retWizardStepBody("age", draft), {
    ret_wizard_steps: { age: "done" }, ret_answers_v: 2,
    ret_wizard_answers: { age: 52 } });
  assert.deepEqual(retWizardStepBody("spend", draft), {
    ret_wizard_steps: { spend: "done" }, ret_answers_v: 2,
    ret_wizard_answers: { spend: 60000 } });
  // a cleared box is the server's default, so it clears the saved answer
  assert.deepEqual(retWizardStepBody("saving", draft), {
    ret_wizard_steps: { saving: "done" }, ret_answers_v: 2,
    ret_wizard_answers: { employer_mo: 1250.5, taxable_mo: null } });
  // the Roth saving rides beside the answers, as the claim age does; a
  // draft with no Roth box (above) sends nothing for it
  assert.deepEqual(retWizardStepBody("saving", { ...draft, roth_mo: "400" }),
                   { ret_wizard_steps: { saving: "done" }, ret_answers_v: 2,
                     ret_wizard_answers: { employer_mo: 1250.5,
                                           taxable_mo: null },
                     ret_wizard_roth_mo: 400 });
  // a blank Roth box is an answered $0, never "not asked"
  assert.deepEqual(retWizardStepBody("saving", { ...draft, roth_mo: "" })
                     .ret_wizard_roth_mo, 0);
  // text that is not a number is not saved as one; the claim age rides
  // beside the answers
  assert.deepEqual(retWizardStepBody("assumptions", draft), {
    ret_wizard_steps: { assumptions: "done" }, ret_answers_v: 2,
    ret_wizard_answers: { ret: 6.5, infl: 3, end: null },
    ret_wizard_ssage: 67 });
  assert.deepEqual(retWizardStepBody("done", draft),
                   { ret_wizard_steps: { done: "done" } });
});

test("no client puts a key an older server refuses inside the answers", () => {
  const draft = { age: "52", spend: "1", employer_mo: "1", taxable_mo: "1",
                  ret: "1", infl: "1", end: "90", ssage: "66" };
  for (const step of RET_WIZARD_STEPS)
    for (const k of Object.keys(retWizardStepBody(step, draft)
                                  .ret_wizard_answers ?? {}))
      assert.ok(OLD_SERVER_ANSWER_KEYS.has(k), `${step}: ${k}`);
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, src] of [
      ["web page", read("../../webapp/src/pages/Retirement.tsx")],
      ["web wizard", read("../../webapp/src/components/RetireWizard.tsx")],
      ["mobile", read("../src/app/retire.tsx")]]) {
    assert.doesNotMatch(src, /[^_]answers_v: 2/, side);
    for (const m of src.matchAll(/ret_wizard_answers: \{ ([^}]*)\}/g))
      for (const k of m[1].split(",").map((x) => x.split(":")[0].trim())
             .filter(Boolean))
        assert.ok(OLD_SERVER_ANSWER_KEYS.has(k), `${side}: ${k}`);
  }
});

test("both wizards refuse a claim age that is not a whole year in range", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, src] of [
      ["web", read("../../webapp/src/components/RetireWizard.tsx")],
      ["mobile", read("../src/app/retire.tsx")]]) {
    assert.match(src, /RET_WIZARD_BOUNDS\.ssage/, side);
    assert.match(src, /Number\.isInteger\(/, side);
  }
});

test("saved retirement answers prefill the wizard over the projection's defaults", () => {
  const defaults = { age: 45, spend: 48000, employer_mo: 0, taxable_mo: 0,
                     ret: 7, infl: 3, end: 95, ssage: 67 };
  assert.deepEqual(retWizardPrefill(undefined, defaults), {
    age: "45", spend: "48000", employer_mo: "0", roth_mo: "", taxable_mo: "0",
    ret: "7", infl: "3", end: "95", ssage: "67" });
  assert.equal(retWizardPrefill(
    { roth_mo: 350, answers_v2: ["roth_mo"] }, defaults).roth_mo, "350");
  assert.equal(retWizardPrefill(undefined, { ...defaults, roth_mo: 200 })
                 .roth_mo, "200");
  // a saved total with no Roth answer opens on the server's split of it,
  // only when the server says its figures are that split (saving_hint)
  const saved1000 = { employer_mo: 1000, answers_v2: ["employer_mo"] };
  const split = retWizardPrefill(
    saved1000, { ...defaults, employer_mo: 600, roth_mo: 400 }, true);
  assert.deepEqual([split.employer_mo, split.roth_mo], ["600", "400"]);
  // otherwise the projection's figures may be a what-if: the saved answer
  // stands, and its missing Roth figure is the $0 it is projected as
  const tried = retWizardPrefill(
    saved1000, { ...defaults, employer_mo: 600, roth_mo: 400 });
  assert.deepEqual([tried.employer_mo, tried.roth_mo], ["1000", "0"]);
  // a saved Roth answer is never replaced by a what-if either
  const both = retWizardPrefill(
    { employer_mo: 700, roth_mo: 300, answers_v2: ["employer_mo", "roth_mo"] },
    { ...defaults, employer_mo: 50, roth_mo: 950 });
  assert.deepEqual([both.employer_mo, both.roth_mo], ["700", "300"]);
  // an older server offers no Roth figure: the saved answer stands
  assert.equal(retWizardPrefill(
    { employer_mo: 1000, answers_v2: ["employer_mo"] }, defaults)
    .employer_mo, "1000");
  // only answers a current client saved (listed in answers_v2) are shown;
  // an older client's saved boxes are not the household's answers
  assert.deepEqual(retWizardPrefill(
    { spend: 72000, taxable_mo: 300, end: null, ret: "x", age: 61,
      answers_v2: ["spend", "taxable_mo", "end", "ret"] }, defaults), {
    age: "45", spend: "72000", employer_mo: "0", roth_mo: "", taxable_mo: "300",
    ret: "7", infl: "3", end: "95", ssage: "67" });
  // no saved answer and no figure from the projection (a spend it still
  // needs): the box is blank, never a wizard's own made-up default
  assert.deepEqual(retWizardPrefill({ age: 52, answers_v2: ["age"] },
                                    { ...defaults, spend: null }), {
    age: "52", spend: "", employer_mo: "0", roth_mo: "", taxable_mo: "0",
    ret: "7", infl: "3", end: "95", ssage: "67" });
  assert.equal(retWizardPrefill(undefined, {}).spend, "");
  assert.equal(retWizardPrefill({ spend: 250000 }, {}).spend, "");
  for (const [side, src] of [
      ["web", readFileSync(new URL("../../webapp/src/components/RetireWizard.tsx",
                                   import.meta.url), "utf8")],
      ["mobile", readFileSync(new URL("../src/app/retire.tsx", import.meta.url),
                              "utf8")]])
    assert.doesNotMatch(src, /60000|\?\? 40\b|\|\| 40\b/, side);
});

test("both retirement wizards save each step as it is answered and resume from the saved marks", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  const web = read("../../webapp/src/components/RetireWizard.tsx");
  const mob = read("../src/app/retire.tsx");
  for (const [rel, src] of [["web", web], ["mobile", mob]]) {
    assert.match(src, /retWizardStepBody\(/, rel);
    assert.match(src, /retWizardResumeAt\(/, rel);
    assert.match(src, /retWizardPrefill\(/, rel);
  }
  // the hosts re-open a walkthrough left halfway, birthdate or not
  assert.match(read("../../webapp/src/pages/Retirement.tsx"), /retWizardMidway\(/);
  assert.match(mob, /retWizardMidway\(/);
});

test("both retirement wizards hold each answer to the server's own ranges", async (t) => {
  // a bound the client states that the server does not hold either lets
  // a value through the client check that the save then refuses, or
  // refuses one the server would take
  const url = new URL("../../server/oikonome/sync/restore.py", import.meta.url);
  if (!existsSync(url)) { t.skip("no server tree beside mobile/"); return; }
  const py = readFileSync(url, "utf8");
  const rate = py.match(/^RET_RATE_MIN, RET_RATE_MAX = (\S+), (\S+)$/m);
  assert.ok(rate, "restore.RET_RATE_MIN/MAX not found");
  const names = { RET_RATE_MIN: Number(rate[1]), RET_RATE_MAX: Number(rate[2]) };
  const block = py.match(/^RET_WIZARD_RANGES = \{([^}]*)\}/m);
  assert.ok(block, "restore.RET_WIZARD_RANGES not found");
  const num = (s) => (s in names ? names[s] : Number(s.replace(/_/g, "")));
  const server = Object.fromEntries([...block[1].matchAll(
    /"(\w+)": \(([^,]+), ([^)]+)\)/g)].map(([, k, lo, hi]) =>
      [k, [num(lo.trim()), num(hi.trim())]]));
  assert.ok(Object.keys(server).length >= 7, JSON.stringify(server));
  const web = await import("../../webapp/src/retWizard.ts");
  for (const [side, m] of [["mobile", pureModule], ["web", web]])
    assert.deepEqual(m.RET_WIZARD_BOUNDS, server, side);
  // and the wizards check and state the age from the table, not a literal
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, src] of [
      ["web", read("../../webapp/src/components/RetireWizard.tsx")],
      ["mobile", read("../src/app/retire.tsx")]]) {
    assert.match(src, /RET_WIZARD_BOUNDS\.age/, side);
    assert.doesNotMatch(src, /between 18 and 100|a > 100|"100"/, side);
  }
});

test("both retirement wizards act on a step's save only while still on that step", () => {
  // Continue/Finish waits for its save; a Back, a Skip or a close in the
  // meantime must not let the late reply move the wizard, open the model
  // or show a refusal on a step that has no such field
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  for (const [side, src] of [
      ["web", read("../../webapp/src/components/RetireWizard.tsx")],
      ["mobile", read("../src/app/retire.tsx")]]) {
    const wiz = src.slice(src.indexOf("function RetireWizard"));
    const next = wiz.slice(wiz.indexOf("const next = async"));
    const nextFn = next.slice(0, next.indexOf("\n  };"));
    assert.match(nextFn, /const at = i;/, `${side}: the step is captured`);
    const awaits = [...nextFn.matchAll(/await /g)].length;
    const checks = [...nextFn.matchAll(/if \(!stillOn\(at\)\) return;/g)].length;
    assert.ok(awaits > 0 && checks >= awaits,
              `${side}: ${awaits} awaits, ${checks} still-on-the-step checks`);
    assert.match(wiz, /const stillOn = /, side);
    // Back is not offered while the step's save is pending
    const back = wiz.slice(wiz.indexOf("← Back") - 400, wiz.indexOf("← Back"));
    assert.match(back, /disabled=\{busy\}/, `${side}: Back stays live while saving`);
  }
});

test("the phone wizard's Skip leaves at once, even with a save pending", () => {
  // no settings save has a timeout, so a Skip that waits for a stalled one
  // traps the person in the walkthrough the rule says never traps anyone
  const src = readFileSync(new URL("../src/app/retire.tsx", import.meta.url), "utf8");
  const end = src.indexOf("Skip — open the full model");
  const handler = src.slice(src.lastIndexOf("onPress={", end), end);
  assert.doesNotMatch(handler, /if \(busy\) return/);
  assert.doesNotMatch(handler, /await /);
  assert.match(handler, /onSkip\(\)/);
});

test("both retirement wizards show a refused step save on its step and wait there", () => {
  // A step save carries the step's mark and its answers in one body, and
  // the server refuses the whole body over one out-of-range answer. A
  // swallowed refusal opens the projection with the typed value while
  // nothing of it is stored: a reload or another device prefills the
  // defaults. So the save a step makes is awaited, a refusal the person
  // can correct (400/422) is shown with the server's words and the step
  // waits, exactly as the birthdate on the first step; the chain itself
  // swallows only so one failed link never stops the next.
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  const sides = { web: read("../../webapp/src/components/RetireWizard.tsx"),
                  mobile: read("../src/app/retire.tsx") };
  for (const [side, src] of Object.entries(sides)) {
    const wiz = src.slice(src.indexOf("function RetireWizard"));
    const save = wiz.slice(wiz.indexOf("const saveStep"));
    const saveFn = save.slice(0, save.indexOf("\n  };"));
    assert.doesNotMatch(saveFn, /saveSettings\([^)]*\)\)\s*\.catch\(\(\) => \{\}\)/,
                        `${side}: the step's own save is swallowed`);
    assert.match(saveFn, /saves\.current = link\.catch\(\(\) => \{\}\)/, side);
    assert.match(saveFn, /return link;/, side);
    const next = wiz.slice(wiz.indexOf("const next = async"));
    const nextFn = next.slice(0, next.indexOf("\n  };"));
    assert.match(nextFn, /await saveStep\(/, `${side}: the step waits for its save`);
    assert.match(nextFn, /stepRefusal\(e\)/, `${side}: a refusal is read`);
    assert.match(wiz, /const stepRefusal = /, side);
  }
});

test("both retirement wizards write the done or skipped mark after every step save", () => {
  // Each step save's reply seeds the cache with the whole settings
  // document. A mark sent beside a step still in flight can be answered
  // first; the step's reply (no mark) then lands last and the walkthrough
  // reopens. So the wizard chains its step saves on a promise its host
  // owns, and every exit chains its mark (and a birthdate kept on Skip)
  // on the same one.
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  const wizards = { web: read("../../webapp/src/components/RetireWizard.tsx"),
                    mobile: read("../src/app/retire.tsx") };
  const hosts = { "web Retirement": read("../../webapp/src/pages/Retirement.tsx"),
                  "web RetirementSetup": read("../../webapp/src/pages/RetirementSetup.tsx"),
                  mobile: wizards.mobile };
  for (const [side, src] of Object.entries(wizards)) {
    const wiz = src.slice(src.indexOf("function RetireWizard"));
    assert.match(wiz, /const link = saves\.current\.then\(/, side);
    assert.doesNotMatch(wiz, /useRef<Promise/, `${side}: the chain is the host's`);
  }
  for (const [side, src] of Object.entries(hosts)) {
    assert.match(src, /saves=\{wizSaves\}/, `${side} hands the wizard its chain`);
    const marks = [...src.matchAll(/ret_wizard_steps: \{ done:/g)];
    assert.ok(marks.length > 0, side);
    for (const m of marks) {
      const before = src.slice(Math.max(0, m.index - 160), m.index);
      assert.match(before,
        /wizSaves\.current = wizSaves\.current\s*\.then\(\(\) => saveSettings\(qc, (client!, )?\{ $/,
        `${side}: a mark written outside the step-save chain`);
    }
    // a birthdate saved on the way out rides the chain too, after the mark
    for (const m of src.matchAll(/saveSettings\(qc, (client!, )?\{ birthdate \}/g)) {
      const before = src.slice(Math.max(0, m.index - 80), m.index);
      assert.match(before, /\.then\(\(\) => $/, `${side}: exit birthdate off the chain`);
    }
  }
});

import { pickedImagePart } from "../src/lib/pure.ts";

test("a picked photo is labelled by the file the picker wrote, per platform", () => {
  // Android re-encodes: a HEIC/WebP/GIF source arrives as JPEG bytes
  // (written as .jpeg, or under its source extension for webp/gif) with
  // the SOURCE's mimeType and name still attached
  for (const [uri, mimeType, type] of [
      ["file:///cache/ImagePicker/a.jpeg", "image/heic", "image/jpeg"],
      ["file:///cache/ImagePicker/a.webp", "image/webp", "image/jpeg"],
      ["file:///cache/ImagePicker/a.gif", "image/gif", "image/jpeg"],
      ["file:///cache/ImagePicker/a.bmp", "image/bmp", "image/jpeg"],
      ["file:///cache/ImagePicker/a.png", "image/png", "image/png"],
      ["file:///cache/ImagePicker/a.jpeg", "image/jpeg", "image/jpeg"]])
    assert.equal(pickedImagePart({ uri, mimeType, fileName: "IMG_1.heic" },
                                 "android").type, type, uri);
  assert.deepEqual(pickedImagePart({ uri: "file:///c/a.png",
      mimeType: "image/png" }, "android"),
    { uri: "file:///c/a.png", name: "receipt.png", type: "image/png" });
  // iOS writes the bytes its extension names (Compatible mode turns a
  // HEIC into .jpg); mimeType comes from that same extension
  for (const [uri, mimeType, type] of [
      ["file:///tmp/a.jpg", "image/jpeg", "image/jpeg"],
      ["file:///tmp/a.PNG", "image/png", "image/png"],
      ["file:///tmp/a.webp", "image/webp", "image/webp"],
      ["file:///tmp/a.gif", "image/gif", "image/gif"],
      ["file:///tmp/a.heic", "image/heic", "image/heic"],
      ["file:///tmp/a.jpg?x=1", undefined, "image/jpeg"],
      ["file:///tmp/noext", "image/png", "image/png"]])
    assert.equal(pickedImagePart({ uri, mimeType }, "ios").type, type, uri);
  // nothing to go on: the camera's JPEG; a check keeps its kind's name
  assert.deepEqual(pickedImagePart({ uri: "content://media/42" }, "ios"),
    { uri: "content://media/42", name: "receipt.jpg", type: "image/jpeg" });
  assert.equal(pickedImagePart({ uri: "content://m/1" }, "android",
                               "check").name, "check.jpg");
});

import { fillMonthKeyGaps, fillMonthLabelGaps } from "../src/lib/pure.ts";

test("a month no bank feed covered keeps its place in the month series", () => {
  // the server leaves the hole out; the chart puts an empty slot back
  assert.deepEqual(
    fillMonthKeyGaps([["2024-11", 100], ["2025-02", -50], ["2025-03", 20]]),
    [["2024-11", 100], ["2024-12", null], ["2025-01", null],
     ["2025-02", -50], ["2025-03", 20]]);
  assert.deepEqual(fillMonthKeyGaps([["2025-01", 1], ["2025-02", 2]]),
                   [["2025-01", 1], ["2025-02", 2]]);
  assert.deepEqual(fillMonthKeyGaps([]), []);
  // one year's bars, labelled by month: the hole between bars only
  assert.deepEqual(
    fillMonthLabelGaps([["Mar", 5], ["Jul", 7], ["Aug", 8]]),
    [["Mar", 5], ["Apr", null], ["May", null], ["Jun", null],
     ["Jul", 7], ["Aug", 8]]);
  // labels it does not know are left as they came
  assert.deepEqual(fillMonthLabelGaps([["Q1", 1]]), [["Q1", 1]]);
});

import { keyRetireAges } from "../src/lib/pure.ts";

test("the retire-at-each-age table opens on the ages that decide it", () => {
  const ages = Array.from({ length: 27 }, (_, i) => 46 + i);   // 46..72
  const key = [...keyRetireAges(ages, 61, 67)];
  // today, every fifth birthday, the turn (60 → 61), the stress age, the end
  assert.deepEqual(key, [46, 50, 55, 60, 61, 65, 67, 70, 72]);
  // nothing works: no turn to show, the rest stands
  assert.deepEqual([...keyRetireAges(ages, null, 67)],
                   [46, 50, 55, 60, 65, 67, 70, 72]);
  // an age outside the table is never invented
  assert.deepEqual([...keyRetireAges([60, 61, 62], 61, 99)], [60, 61, 62]);
  assert.deepEqual([...keyRetireAges([58, 59, 61, 62], null, 99)], [58, 62]);
  assert.equal(keyRetireAges([], 50).size, 0);
});

// ---- Doctor: a script source's status reads as the web's pill ----

import { scriptStatusTone } from "../src/lib/pure.ts";

test("an unwatched script source is plain, never drawn as a failure", () => {
  assert.equal(scriptStatusTone("fresh"), "good");
  assert.equal(scriptStatusTone("stale"), "warn");
  // nobody expects an "off" source to push, so it has not failed
  assert.equal(scriptStatusTone("off"), "plain");
});

test("the Doctor screen colours a source by its status alone", () => {
  // the rows carry `status` and no `ok`; reading `ok` once painted every
  // unwatched source red
  const src = readFileSync(new URL("../src/app/doctor.tsx", import.meta.url),
                           "utf8");
  assert.match(src, /scriptStatusTone\(sc\.status\)/);
  assert.doesNotMatch(src, /\bsc\.ok\b/);
});

// ---- Accounts: one institution card per bank login, as the web ----

import { groupByLogin } from "../src/lib/pure.ts";

test("two logins at one bank are two cards, each its own login's", () => {
  const cards = groupByLogin([
    { id: "a1", item_id: "login-2", institution_name: "Northwind Bank" },
    { id: "a2", item_id: "login-1", institution_name: "Northwind Bank" },
    { id: "a3", item_id: "login-3", institution_name: "Arbor Credit Union" },
    { id: "a4", item_id: "login-2", institution_name: "Northwind Bank" },
  ]);
  assert.deepEqual(cards.map((c) => [c.key, c.inst, c.list.map((a) => a.id)]),
    [["login-3", "Arbor Credit Union", ["a3"]],
     ["login-2", "Northwind Bank", ["a1", "a4"]],
     ["login-1", "Northwind Bank", ["a2"]]]);
  // an older server's row with no login falls back to the bank's name
  assert.deepEqual(groupByLogin([{ id: "x", institution_name: "Fern" }])
    .map((c) => c.key), ["Fern"]);
});

test("the Accounts screen prints each login's subtotal from the server", () => {
  const src = readFileSync(
    new URL("../src/app/(tabs)/accounts.tsx", import.meta.url), "utf8");
  assert.match(src, /shown\?\.by_item\[g\.key\]/);
  assert.match(src, /groupByLogin\(personal\)/);
  assert.match(src, /groupByLogin\(business\)/);
});

// ---- Cash Flow: shares and node figures are the server's ----

import { flowNodeFigure } from "../src/lib/flowlayout.ts";

test("a flow node prints the server's dollars and share", () => {
  const flow = {
    in: { total: 4000, paychecks: 4000, interest: 0, other: 0,
          shown: [{ key: "paychecks", dollars: 4000, share: 100 },
                  { key: "interest", dollars: 0, share: null },
                  { key: "other", dollars: 0, share: null }] },
    out: { total: 1400, fixed: 700, variable: 700,
           shown: [{ key: "fixed", dollars: 700, share: 18 },
                   { key: "variable", dollars: 700, share: 17 },
                   { key: "saved", dollars: 2600, share: 65 }] },
    saved: 2600 };
  const node = (key, value) => ({ key, label: key, value, color: "#000" });
  assert.deepEqual(flowNodeFigure(flow, node("fixed", 700)),
                   { dollars: 700, share: 18 });
  assert.deepEqual(flowNodeFigure(flow, node("saved", 2600)),
                   { dollars: 2600, share: 65 });
  // an older server sends no `shown`: the node's own value, no share
  const older = { ...flow, in: { ...flow.in, shown: undefined },
                  out: { ...flow.out, shown: undefined } };
  assert.deepEqual(flowNodeFigure(older, node("fixed", 700.4)),
                   { dollars: 700.4, share: null });
});

test("the Cash Flow screens divide nothing to print a share", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  assert.doesNotMatch(read("../src/components/flow-chart.tsx"), /Math\.round/);
  const spending = read("../src/app/spending.tsx");
  assert.match(spending, /category_shares/);
  assert.doesNotMatch(spending, /\(100 \* v\) \/ w\.total|\(100 \* top\[1\]\)/);
  assert.match(read("../src/app/cashflow.tsx"), /source_shares/);
});

// ---- Changes vs before are the server's ----

import { changeLook } from "../src/lib/pure.ts";

test("a change prints the server's amount and percent", () => {
  // $1,000 against $1,000.40: both print $1,000, so it reads unchanged
  assert.deepEqual(changeLook({ amount: 0, pct: null, unchanged: true }, true),
                   { unchanged: true });
  assert.deepEqual(changeLook({ amount: 25, pct: 2, unchanged: false }, true),
                   { unchanged: false, up: true, sign: "+", dollars: 25,
                     pct: 2, good: false });
  assert.deepEqual(changeLook({ amount: -25, pct: -2, unchanged: false }, true),
                   { unchanged: false, up: false, sign: "−", dollars: 25,
                     pct: 2, good: true });
  // income going up is the good way
  assert.equal(changeLook({ amount: 40, pct: 5, unchanged: false }, false)
                 .good, true);
  // past ±1000% the percent is left out, the dollars stay
  assert.equal(changeLook({ amount: 900, pct: 1800, unchanged: false }, true)
                 .pct, null);
  // no earlier period, or an older server: nothing
  assert.equal(changeLook(null, true), null);
  assert.equal(changeLook(undefined, true), null);
});

test("the Spending, Income and Year screens subtract nothing", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  const spending = read("../src/app/spending.tsx");
  assert.match(spending, /category_changes/);
  assert.match(spending, /w\?\.movers/);
  assert.doesNotMatch(spending, /cur - prev|v - \(p \?\? 0\)|Math\.max\(w\.months/);
  const cashflow = read("../src/app/cashflow.tsx");
  assert.match(cashflow, /source_changes/);
  assert.doesNotMatch(cashflow, /\(100 \* d\) \/ prev/);
  const year = read("../src/app/year.tsx");
  assert.match(year, /changes\?\.categories/);
  assert.doesNotMatch(year, /cur - prev|spend - d\.prev_totals\.spend/);
});

// ---- the leftovers: each screen prints the server's figure ----

test("the money map prints the server's words and works out no row's state", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  const map = read("../src/components/money-map.tsx");
  const delta = map.slice(map.indexOf("function rowDelta"),
                          map.indexOf("export function Track"));
  assert.match(delta, /row\.status/);
  assert.doesNotMatch(delta, /row\.budget|row\.expected|row\.actual/);
  // the "Variable spending" row is the server's on Today and Budget
  assert.match(read("../src/app/(tabs)/index.tsx"), /sentPlanTotal\(d\.plan_total\)/);
  assert.match(read("../src/app/budget.tsx"), /sentPlanTotal\(d\.plan_total\)/);
});

test("the phone's fallback chains and client sums are gone", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  assert.doesNotMatch(read("../src/app/(tabs)/index.tsx"),
                      /month_remaining_paced\s*\?\?/);
  assert.doesNotMatch(read("../src/app/(tabs)/bills.tsx"),
                      /period_left \?\? b\.amount/);
  const biz = read("../src/app/business.tsx");
  const attention = biz.slice(biz.indexOf("// the endpoint's real COUNT, as on the web"),
                              biz.indexOf('k="Needs attention"'));
  assert.ok(attention.length > 0);
  assert.doesNotMatch(attention, /suggestions\.length/);
  const txns = read("../src/app/(tabs)/transactions.tsx");
  assert.doesNotMatch(txns, /listedSum|total for this filter/);
  assert.match(txns, /listed_spend/);
  const month = read("../src/app/month.tsx");
  assert.doesNotMatch(month, /d\.income\.actual - d\.spend_total|budget - d\.variable_actual/);
  assert.doesNotMatch(read("../src/app/reimburse.tsx"), /reimbOwed/);
  // the percent received is the server's, as on the web
  const reimb = read("../src/app/reimburse.tsx");
  assert.match(reimb, /p\.received_pct/);
  assert.doesNotMatch(reimb, /Math\.round\(100 \*/);
  // the day dividers take the server's whole-day figure
  assert.match(txns, /withDayHeads\(rows, daySpendMap\)/);
  assert.match(txns, /day_spend/);
  assert.match(txns, /listedSpendWord\(/);
  assert.equal(typeof pureModule.trendWindow, "undefined");
  assert.equal(typeof pureModule.reimbOwed, "undefined");
});

test("planner shares: an amount typed past what a number holds draws nothing and never hangs", () => {
  assert.equal(numv("1e999"), 0);
  assert.equal(numv("9".repeat(400)), 0);
  assert.deepEqual(pureModule.plannerShares([Infinity, 100, 0], 4000), [null, null, null]);
  assert.deepEqual(pureModule.plannerShares([100, 200], Infinity), [null, null]);
  assert.deepEqual(pureModule.plannerShares([500, 500, 500], 1500), [34, 33, 33]);
});

test("lens money maps: the rows are the server's, links and captions read as on the web", () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
  // the server writes a row's link as a path; the tiles as a bare query
  assert.deepEqual(pureModule.ledgerParams("/transactions?bucket=Food&y=2026&m=3"),
                   { bucket: "Food", y: "2026", m: "3" });
  assert.deepEqual(pureModule.ledgerParams("bucket=other&y=2026"),
                   { bucket: "other", y: "2026" });
  // a household bucket name with "=" or a stray "%" survives
  assert.deepEqual(pureModule.ledgerParams("/transactions?bucket=Rent%20%3D%20home&y=2026"),
                   { bucket: "Rent = home", y: "2026" });
  assert.equal(pureModule.ledgerParams("bucket=50%&y=2026").bucket, "50%");
  // the web's terse caption: the tail is dropped; the Bills row's prints
  // only beside its breakdown (an older server sends none)
  assert.equal(pureModule.terseCaption("Bills",
    "posted $1,250 · awaiting $300 · not counted in the verdict"), null);
  assert.equal(pureModule.terseCaption("Bills",
    "posted $1,250 · plan $1,700 · not counted in the verdict",
    { kind: "running" }), "posted $1,250 · plan $1,700");
  assert.equal(pureModule.terseCaption("Bills",
    "posted $1,250 · not counted in the verdict", null), "posted $1,250");
  assert.equal(pureModule.terseCaption("Savings / Investment",
    "transferred vs plan · not counted in the verdict"), "transferred vs plan");
  assert.equal(pureModule.terseCaption("Savings / Investment",
    "avg / mo, last 3 months"), "avg / mo, last 3 months");
  assert.equal(pureModule.terseCaption("Everything else", null), null);
  // both lens screens print the server's rows and build none of their own
  for (const f of ["../src/app/month.tsx", "../src/app/year.tsx"]) {
    const src = read(f);
    assert.match(src, /sentPlanRows\(d\.plan_rows/);
    assert.match(src, /sentPlanTotal\(d\.plan_total\)/);
    assert.doesNotMatch(src, /planRows\(|lensMap|labelSavingsRow|Savings \/ Investment|buckets_annual|todayFull/);
  }
  const map = read("../src/components/money-map.tsx");
  assert.doesNotMatch(map, /export function (planRows|lensMap)|lensRowStatus/);
  assert.match(map, /terseCaption\(row\.label, row\.caption, row\.breakdown\)/);
});

test("a paged log keeps its pages until the new question answers, and never doubles one", () => {
  const { pagesAfter } = pureModule;
  const empty = { key: "|", pages: [] };
  const a = pagesAfter(empty, "|", null, "p1");
  assert.deepEqual(a.pages.map((x) => x.page), ["p1"]);
  const b = pagesAfter(a, "|", "c1", "p2");
  assert.deepEqual(b.pages.map((x) => x.page), ["p1", "p2"]);
  // a refetch of a page already held replaces it, never appends a copy
  const c = pagesAfter(b, "|", "c1", "p2'");
  assert.deepEqual(c.pages.map((x) => x.page), ["p1", "p2'"]);
  // a refetch of the first page starts over
  assert.deepEqual(pagesAfter(c, "|", null, "p1'").pages.map((x) => x.page),
                   ["p1'"]);
  // another filter's page is never merged into what is held
  const d = pagesAfter(c, "me|", "c1", "q2");
  assert.equal(d.key, "me|");
  assert.deepEqual(d.pages.map((x) => x.page), ["q2"]);
  // the screen keeps the held pages through a filter change and a pull
  // (no reset to empty before the answer), and words a failed read
  const src = readFileSync(new URL("../src/app/activity.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(src, /setPages\(\[\]\)/);
  assert.match(src, /pagesAfter\(/);
  assert.match(src, /<FailedLine q=\{query\} what="the activity log" \/>/);
  assert.match(src, /who \|\| kind \? EMPTY_LINES\.filtered/);
});

// ---- offline saves: the phone's connection, from its own requests ----
import { OFFLINE_WRITE, offlineAfter } from "../src/lib/pure.ts";
import { bindConnection, isOffline, noteSettled, probeReach,
         resetConnection } from "../src/lib/connection.ts";
import { QueryClient as PlainQueryClient } from "@tanstack/react-query";

test("offline: no answer is offline, any answer is online", () => {
  const off = new TypeError("Network request failed");
  assert.equal(offlineAfter(false, off), true);
  assert.equal(offlineAfter(true, null), false);
  // a 5xx or a refusal is the server answering: the phone is online
  assert.equal(offlineAfter(true, new ApiError(500, "boom")), false);
  assert.equal(offlineAfter(true, new ApiError(403, "no")), false);
  // an aborted request or a client-side throw says nothing
  const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
  assert.equal(offlineAfter(true, abort), true);
  assert.equal(offlineAfter(false, abort), false);
  assert.equal(offlineAfter(false, new Error("Pick an account.")), false);
  assert.equal(OFFLINE_WRITE, "You're offline.");
});

test("the phone's one fetch door limits a read and leaves a write alone", async () => {
  const { fetchStamped } = await import("../src/lib/connection.ts");
  const { readFinished, timedOut } = await import("../src/lib/pure.ts");
  const outer = new AbortController();
  const seen = [];
  const answer = async (_url, init) => { seen.push(init); return { ok: true }; };
  // a write carries the caller's signal itself: nothing can time it out
  await fetchStamped("http://x/api/bills", { method: "POST", signal: outer.signal },
                     answer);
  assert.equal(seen[0].signal, outer.signal);
  assert.equal(seen[0].method, "POST");
  // a read carries a signal of its own that the caller's still ends
  const r = await fetchStamped("http://x/api/bills", { signal: outer.signal },
                               answer);
  assert.notEqual(seen[1].signal, outer.signal);
  assert.equal(seen[1].signal.aborted, false);
  outer.abort();
  assert.equal(seen[1].signal.aborted, true);
  readFinished(r);
  // a read that failed for its own reasons is not called a timeout
  const off = await fetchStamped("http://x/api/bills", undefined, async () => {
    throw new TypeError("Network request failed"); }).catch((e) => e);
  assert.equal(off.message, "Network request failed");
  assert.equal(timedOut(off), false);
});

test("offline: the query client's own fetches set and clear it", async () => {
  resetConnection();
  const qc = new PlainQueryClient({ defaultOptions: { queries: { retry: false } } });
  const unbind = bindConnection(qc);
  let up = false;
  await qc.fetchQuery({ queryKey: ["a"], queryFn: () => {
    if (!up) throw new TypeError("Network request failed");
    return 1; } }).catch(() => {});
  assert.equal(isOffline(), true, "a read with no answer greys the writes");
  // a cache patch is not an answer
  qc.setQueryData(["b"], 2);
  assert.equal(isOffline(), true);
  // a mutation the server refused is an answer: back online
  await qc.getMutationCache().build(qc, { mutationFn: async () => {
    throw new ApiError(409, "busy"); } }).execute(undefined).catch(() => {});
  assert.equal(isOffline(), false);
  noteSettled(new TypeError("Network request failed"));
  assert.equal(isOffline(), true);
  up = true;
  await qc.fetchQuery({ queryKey: ["c"], queryFn: () => 3 });
  assert.equal(isOffline(), false);
  unbind();
  resetConnection();
});

test("offline: the health-door look answers both ways, and a hang is offline", async () => {
  resetConnection();
  await probeReach("https://x", 50, async () => { throw new TypeError("Network request failed"); });
  assert.equal(isOffline(), true);
  await probeReach("https://x", 50, async () => new Response("down", { status: 502 }));
  assert.equal(isOffline(), false, "any answer is the phone reaching the server");
  await probeReach("https://x", 20, (_u, init) => new Promise((_r, rej) =>
    init.signal.addEventListener("abort", () =>
      rej(Object.assign(new Error("aborted"), { name: "AbortError" })))));
  assert.equal(isOffline(), true, "no answer in time");
  resetConnection();
});

// On a device the global fetch is expo/fetch: no network rejects with its
// FetchError, a plain Error reading "fetch failed: <the native cause>".
class ExpoFetchError extends Error {
  constructor(m) { super(`fetch failed: ${m}`); }
}

test("offline: expo/fetch's no-answer failure greys the writes, its cancel says nothing", async () => {
  const off = new ExpoFetchError("Unable to resolve host \"x.test\"");
  assert.equal(offlineAfter(false, off), true);
  assert.equal(errText(off), "You're offline.");
  const cancel = new ExpoFetchError("Fetch request has been canceled");
  assert.equal(offlineAfter(false, cancel), false);
  assert.equal(offlineAfter(true, cancel), true);
  resetConnection();
  await probeReach("https://x", 50, async () => { throw off; });
  assert.equal(isOffline(), true, "a refused socket on the device is offline");
  resetConnection();
});

test("offline: a probe started before a newer answer cannot re-mark it offline", async () => {
  resetConnection();
  const qc = new PlainQueryClient({ defaultOptions: { queries: { retry: false } } });
  const unbind = bindConnection(qc);
  // the look at the health door starts first and hangs
  let hang;
  const probe = probeReach("https://x", 10_000, (_u, init) =>
    new Promise((_r, rej) => { hang = () => rej(new TypeError("Network request failed"));
      init.signal.addEventListener("abort", () => rej(new ExpoFetchError(
        "Fetch request has been canceled"))); }));
  // a screen's refetch, started after it, is answered
  await qc.fetchQuery({ queryKey: ["fresh"], queryFn: () => 1 });
  assert.equal(isOffline(), false);
  // the stale look then gives up: old evidence, the answer stands
  hang();
  await probe;
  assert.equal(isOffline(), false, "a probe older than the answer is stale");
  // a request started after the answer still counts
  await qc.fetchQuery({ queryKey: ["later"], queryFn: () => {
    throw new TypeError("Network request failed"); } }).catch(() => {});
  assert.equal(isOffline(), true);
  unbind();
  resetConnection();
});

test("offline: an older failed request cannot re-mark it offline after a newer answer", async () => {
  resetConnection();
  const qc = new PlainQueryClient({ defaultOptions: { queries: { retry: false } } });
  const unbind = bindConnection(qc);
  const real = globalThis.fetch;
  const pending = [];
  globalThis.fetch = (_u, _i) => new Promise((ok, no) => pending.push({ ok, no }));
  try {
    const c = makeClient("https://example.invalid", "tok", () => {});
    // the old request goes out first, on a socket that is dying
    const old = qc.fetchQuery({ queryKey: ["old"], queryFn: () => c.me() })
      .catch(() => {});
    const fresh = qc.fetchQuery({ queryKey: ["fresh"], queryFn: () => c.me() });
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(pending.length, 2);
    pending[1].ok(new Response('{"user":"a"}', { status: 200,
      headers: { "content-type": "application/json" } }));
    await fresh;
    assert.equal(isOffline(), false);
    pending[0].no(new TypeError("Network request failed"));
    await old;
    assert.equal(isOffline(), false, "the older failure is stale evidence");
  } finally { globalThis.fetch = real; unbind(); resetConnection(); }
});

test("offline: a look on resume is not started while one is in flight", async () => {
  resetConnection();
  let calls = 0, done;
  const slow = () => { calls++; return new Promise((r) => { done = r; }); };
  const first = probeReach("https://x", 10_000, slow);
  const second = probeReach("https://x", 10_000, slow);
  await second;
  assert.equal(calls, 1);
  done(new Response("ok", { status: 200 }));
  await first;
  await probeReach("https://x", 10_000, async () => new Response("ok"));
  resetConnection();
});

test("offline: the write surfaces read the one gate", () => {
  for (const f of ["app/bill.tsx", "app/budget.tsx", "app/txn.tsx",
                   "app/settings.tsx", "app/reimburse.tsx",
                   "app/(tabs)/bills.tsx"]) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), "utf8");
    assert.match(src, /useOffline\(\)/, f);
    assert.match(src, /<OfflineNote /, f);
  }
});

// ---- the offline copy: Month and Year for the current period, and Help ----
import { keptOffline } from "../src/lib/pure.ts";

test("offline copy: the current month and year lenses and Help are kept", () => {
  const now = new Date(2031, 3, 9);           // April 2031, the household's day
  assert.equal(keptOffline(["lens", "month", 2031, 4], now), true);
  assert.equal(keptOffline(["lens", "year", 2031], now), true);
  assert.equal(keptOffline(["help"], now), true);
  // a month or year paged back to is not the offline layer
  assert.equal(keptOffline(["lens", "month", 2031, 3], now), false);
  assert.equal(keptOffline(["lens", "month", 2030, 4], now), false);
  assert.equal(keptOffline(["lens", "year", 2030], now), false);
  assert.equal(keptOffline(["lens", "year", 2031, "extra"], now), false);
  // the copies that were already kept stay kept
  for (const k of [["today", "now"], ["bills"], ["accounts"], ["report", "networth"]])
    assert.equal(keptOffline(k, now), true, k.join("/"));
  assert.equal(keptOffline(["transactions", { y: "2031", m: "4" }], now), true);
  assert.equal(keptOffline(["transactions", { y: "2031", m: "4", q: "x" }], now), false);
  assert.equal(keptOffline(["transactions", { y: "2031", m: "3" }], now), false);
  assert.equal(keptOffline(["merchants", ""], now), false);
});

// ---- what a chart says in words: one sentence and a table, every
// figure a point as sent (the web runs the same cases) ----
import * as chartSummary from "../src/lib/chart-summary.ts";
import { chartSummaryCases } from "./chart-summary-cases.mjs";
for (const [name, fn] of Object.entries(chartSummaryCases(chartSummary, assert)))
  test(`chart words: ${name}`, fn);

// ---- the Bills calendar at very large text: a day in words, printing
// the server's in and out (the grid cell's net when an older server sends
// only that) ----
import { CALENDAR_LIST_SCALE, calendarCellFigures, calendarDayLine } from "../src/lib/dates.ts";
test("a calendar cell prints the day's in and out apart, each signed", () => {
  const f = (v) => money(v, false);
  // a paycheck and a bill on one day: never netted into one figure
  assert.deepEqual(calendarCellFigures({ came_in: 2400, went_out: 15 }, f),
                   [{ text: "+$2,400", tone: "in" }, { text: "−$15", tone: "out" }]);
  assert.deepEqual(calendarCellFigures({ came_in: 0, went_out: 86 }, f),
                   [{ text: "−$86", tone: "out" }]);
  assert.deepEqual(calendarCellFigures({}, f), []);
});
test("a listed calendar day reads its in and out in words", () => {
  const f = (v) => money(v, false);
  assert.equal(calendarDayLine({ date: "2026-05-12", total: 1164, came_in: 1250, went_out: 86 }, f),
               "Tue 12: +$1,250 in, $86 out");
  assert.equal(calendarDayLine({ date: "2026-05-13", total: -86, came_in: 0, went_out: 86 }, f),
               "Wed 13: $86 out");
  assert.equal(calendarDayLine({ date: "2026-05-14", total: 40, came_in: 40, went_out: 0 }, f),
               "Thu 14: +$40 in");
  assert.equal(calendarDayLine({ date: "2026-05-15", total: -86 }, f), "Fri 15: $86");
  assert.equal(calendarDayLine({ date: "2026-05-15", total: 86 }, f), "Fri 15: +$86");
  assert.ok(CALENDAR_LIST_SCALE > 1 && CALENDAR_LIST_SCALE <= 1.5);
});

// ---- the planner at rest prints the plan line's own dollars and shares --
test("the planner at rest prints the plan line, typing prints the live figures", () => {
  const pl = { income: 6000, bills: 2500, to_spend: 2000, saved: null, excess: 1500,
               shares: { bills: 42, to_spend: 33, saved: null, excess: 25 } };
  // the live income has cents the header rounded away
  const rest = pureModule.plannerAtRest(pl, { income: 6000.49, out: 4500, saved: 0, excess: 1500 });
  assert.deepEqual(rest, { income: 6000, out: 4500, saved: 0, excess: 1500,
                           outPct: 75, savePct: 0, overPct: 25 });
  // a category typed up by $300: the live figures show
  assert.equal(pureModule.plannerAtRest(pl, { income: 6000, out: 4800, saved: 0, excess: 1200 }), null);
  assert.equal(pureModule.plannerAtRest(null, { income: 1, out: 1, saved: 0, excess: 0 }), null);
});

// approve-all: an offer withdrawn as already tracked never counts as one
// that stayed pending (the web's approveAllWords is the same function)
import { approveAllWords } from "../src/lib/pure.ts";
test("approveAllWords leaves superseded failures out of stayed pending", () => {
  assert.equal(approveAllWords({ approved: 2, failed: [] }),
               "Approved all 2 proposals");
  assert.equal(approveAllWords({ approved: 2, failed: [
    { pid: "a", error: "already tracked", superseded: true }] }),
    "Approved 2 proposals; 1 withdrawn — already tracked");
  assert.equal(approveAllWords({ approved: 1, failed: [
    { pid: "a", error: "already tracked", superseded: true },
    { pid: "b", error: "bill gone", superseded: false }] }),
    "Approved 1; 1 could not be applied and stayed pending (bill gone)"
    + "; 1 withdrawn — already tracked");
  // an older server sends no flag: counted as before
  assert.equal(approveAllWords({ approved: 0, failed: [
    { pid: "b", error: "bill gone" }] }),
    "Approved 0; 1 could not be applied and stayed pending (bill gone)");
});

// a change that rounds to $0 reads "unchanged", never "+$0"; a month is
// named with its year (web twins in moneymath.ts)
import { changeWords, monthWithYear } from "../src/lib/pure.ts";
test("changeWords says unchanged at $0 and signs the rest", () => {
  const m = (n) => "$" + Math.round(n);
  assert.deepEqual(changeWords(0.3, m), { text: "unchanged", tone: "mut" });
  assert.deepEqual(changeWords(-0.4, m), { text: "unchanged", tone: "mut" });
  assert.deepEqual(changeWords(120, m), { text: "+$120", tone: "pos" });
  assert.deepEqual(changeWords(-45, m), { text: "−$45", tone: "neg" });
});
test("monthWithYear names the year with an apostrophe before it", () => {
  assert.equal(monthWithYear("2006-01"), "Jan '06");
  assert.equal(monthWithYear("2025-11"), "Nov '25");
});

test("accounts: a type reads the way a statement says it, never as the feed's pair", () => {
  assert.equal(pureModule.kindWords("depository/checking"), "Checking");
  assert.equal(pureModule.kindWords("investment/403b"), "403(b)");
  assert.equal(pureModule.kindWords("investment/crypto"), "Crypto");
  assert.equal(pureModule.kindWords(""), "");
});

test("accounts: the editor offers each budget switch where the server says it applies", () => {
  const sw = (flags, over = {}) => pureModule.accountSwitches(
    { pinned: false, excluded: false, ...flags, ...over });
  // investment/hsa: its card charges count as spending, so it can be
  // excluded; it anchors no cash forecast
  assert.deepEqual(sw({ can_pin: false, can_exclude: true }),
                   { primary: false, excluded: true });
  // a loan's rows never count
  assert.deepEqual(sw({ can_pin: false, can_exclude: false }),
                   { primary: false, excluded: false });
  // a switch that is on can always be turned off
  assert.deepEqual(sw({ can_pin: false, can_exclude: false },
                      { pinned: true, excluded: true }),
                   { primary: true, excluded: true });
  // a server too old to say: both offered, never hidden
  assert.deepEqual(sw({}), { primary: true, excluded: true });
});

test("signs: a percent and a dollar figure wear a true minus, never a hyphen", () => {
  assert.equal(pureModule.signedPct(2.54), "+2.5%");
  assert.equal(pureModule.signedPct(-7.04), "−7.0%");
  assert.equal(pureModule.signedPct(-18, 0), "−18%");
  const whole = (n) => pureModule.money(n, false);
  assert.equal(pureModule.signedMoney(-1.6, whole), "−$2");
  assert.equal(pureModule.signedMoney(12, whole), "$12");
  assert.equal(pureModule.signedMoney(12, whole, true), "+$12");
  assert.equal(pureModule.signedMoney(0, whole, true), "$0");
});

test("a change is captioned over its window, and a calendar month by its name", () => {
  assert.equal(pureModule.overCaption("1y"), "over 1y");
  assert.equal(pureModule.overCaption("all"), "over all time");
  assert.equal(pureModule.overCaption("cur"), "this month");
  assert.equal(pureModule.overCaption("1m"), "last month");
});

test("bar charts: a label gets the width it is drawn at, and thin bars carry no amounts", () => {
  // a bare month fits the floor; a month with its year needs more
  assert.equal(pureModule.netBarsLabelWidth(["Mar", "Apr"], 6.2, 34), 34);
  assert.ok(pureModule.netBarsLabelWidth(["Mar '24"], 6.2, 34) > 34);
  // twelve bars across a phone carry their amounts; hundreds do not
  assert.equal(pureModule.netBarsShowValues(320 / 12), true);
  assert.equal(pureModule.netBarsShowValues(720 / 300), false);
});

test("names: an account already named after its institution says it once", () => {
  const label = pureModule.accountLabel;
  assert.equal(label({ institution_name: "Fabrikam", name: "Fabrikam (daily)" }),
               "Fabrikam (daily)");
  assert.equal(label({ institution_name: "Contoso Bank", name: "Checking", mask: "1234" }),
               "Contoso Bank Checking 1234");
  assert.equal(label({ institution_name: null, name: "Wallet" }), "Wallet");
});

test("sync: a connection's outcome is said in the Accounts row's words, never the raw status", () => {
  const words = pureModule.connectionStateWords;
  assert.equal(words({ status: "ok", status_kind: "ok" }), "ok");
  assert.equal(words({ status: "error: ITEM_LOGIN_REQUIRED", status_kind: "reauth" }),
               "sign-in expired");
  assert.equal(words({ status: "error: INSTITUTION_DOWN", status_kind: "retrying" }),
               "bank having trouble — retrying");
  assert.equal(words({ status: "reaped", status_kind: "gone" }), "connection removed");
  // an older server sends no kind: anything not ok needs attention
  assert.equal(words({ status: "error: something" }), "needs attention");
  assert.equal(words({ status: null }), "ok");
});

test("a Spending figure links to its rows only when the server lists them", () => {
  const offered = pureModule.spendDrillOffered;
  assert.equal(offered({ total: 120, figures_open_rows: true }), true);
  // a server from before the drill sends no flag: plain figures
  assert.equal(offered({ total: 120 }), false);
  assert.equal(offered({ total: 120, figures_open_rows: false }), false);
  assert.equal(offered(undefined), false);
  assert.equal(offered(null), false);
});

test("a figure's reply without the server's label is a server that cannot drill", () => {
  const st = pureModule.spendDrillState;
  assert.equal(st("", { drill_label: null }, false), "none");
  assert.equal(st("3m", undefined, false), "waiting");
  // the previous list stays on screen while this one loads
  assert.equal(st("3m", { drill_label: "Food and Drink · 3m" }, true), "waiting");
  assert.equal(st("3m", {}, true), "waiting");
  assert.equal(st("3m", { drill_label: "Food and Drink · 3m" }, false), "listed");
  // an older server dropped spend= and answered its plain search
  assert.equal(st("3m", {}, false), "unsupported");
  assert.equal(st("3m", { drill_label: null }, false), "unsupported");
});

test("the phone's Spending figures and drill list follow the server's word", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  const spending = read("../src/app/spending.tsx");
  assert.match(spending, /spendDrillOffered\(w\)/);
  // every figure that links goes through the one gated opener
  assert.doesNotMatch(spending, /onPress=\{\(\) => drill\(/);
  const txns = read("../src/app/(tabs)/transactions.tsx");
  assert.match(txns, /spendDrillState\(/);
  assert.match(txns, /can(&apos;|')t list a figure(&apos;|')s rows/);
});

test("a Spending figure opens the rows it summed: one param set per row kind", () => {
  const p = pureModule.spendDrillParams;
  assert.deepEqual(p("1y"), { spend: "1y" });
  assert.deepEqual(p("3m", { cat: "FOOD AND DRINK" }),
                   { spend: "3m", cat: "FOOD AND DRINK" });
  assert.deepEqual(p("1m", { merchant: "A-B-C HARDWARE" }),
                   { spend: "1m", merchant: "A-B-C HARDWARE" });
  // a dropped merchant was paid only in the window before
  assert.deepEqual(p("1m", { merchant: "OLD GYM", previous: true }),
                   { spend: "1m", merchant: "OLD GYM", side: "previous" });
});


test("the Bills row's table: drawn only when the reply carries it, as sent", () => {
  // an older server sends no breakdown, or a breakdown with no table:
  // no button to draw
  assert.equal(pureModule.billsTableLines(undefined), null);
  assert.equal(pureModule.billsTableLines(null), null);
  assert.equal(pureModule.billsTableLines({}), null);
  assert.equal(pureModule.billsTableLines({ table: null }), null);
  const lines = pureModule.billsTableLines({ table: {
    rows: [{ label: "Paid so far", figure: "+$910", sign: "+" },
           { label: "Over plan", figure: "−$15", sign: "-" },
           { label: "This month's plan", figure: "$895", sign: "=" }],
    outside_heading: "Also still due, from earlier months (not in this plan)",
    outside: [{ label: "Late from Aug 2026: Copper Lane Water",
                figure: "+$48", sign: "+" }] } });
  // the labels and the signed figures as the server printed them, the
  // plan marked as the total
  assert.deepEqual(lines.rows.map((r) => [r.label, r.figure, r.total]),
    [["Paid so far", "+$910", false], ["Over plan", "−$15", false],
     ["This month's plan", "$895", true]]);
  assert.equal(lines.heading,
               "Also still due, from earlier months (not in this plan)");
  assert.deepEqual(lines.outside.map((r) => r.label),
                   ["Late from Aug 2026: Copper Lane Water"]);
  // a heading with nothing under it is not drawn
  assert.equal(pureModule.billsTableLines({ table: {
    rows: [], outside_heading: "x", outside: [] } }).heading, null);
});


test("an older phone prints the Envelopes row self-consistently, never red without its words", () => {
  // rows as a current server sends them (invented figures): the Bills
  // row's due-dated bills, then a pool gauge whose bar ends at drawn + left
  const rows = [
    { label: "Bills", judged: false, actual: 1210, expected: 1210,
      budget: 1390, pending: 0, caption: "posted $1,210 · plan $1,390",
      status: { kind: "due", amount: 180, pending: 0, vs_pace: null,
                text: "$180 still due", tone: "mut" } },
    { label: "Envelopes", judged: false, actual: 75, expected: 75,
      budget: 75, pending: 0,
      caption: "set aside $46 this month · not counted in the verdict",
      status: { kind: "over", amount: 30, pending: 0, vs_pace: null,
                text: "$30 past the pool, counted in spending",
                tone: "neg" } },
    { label: "Envelopes", judged: false, actual: 0, expected: 0,
      budget: 520, pending: 0,
      caption: "set aside $46 this month · not counted in the verdict",
      status: null },
  ];
  // the store build's Track draws an unjudged row red only past its
  // budget (money-map.tsx), and the words in their tone
  const map = readFileSync(new URL("../src/components/money-map.tsx",
                                   import.meta.url), "utf8");
  assert.match(map, /: row\.actual > row\.budget \+ 0\.5;/);
  for (const r of rows) {
    const red = r.actual > r.budget + 0.5;
    assert.equal(red, false, r.label);
    // a negative word only ever names the spill past a pool
    if (r.label === "Envelopes" && r.status?.tone === "neg")
      assert.match(r.status.text, /past the pool, counted in spending$/);
  }
  // one row per label on a screen (the older map keys rows by label)
  assert.deepEqual([...new Set(rows.slice(0, 2).map((r) => r.label))],
                   ["Bills", "Envelopes"]);
  // the Bills row's words name only its own still-due figure
  assert.match(rows[0].status.text, /^\$[\d,]+ still due$/);
  assert.equal(rows[0].actual + 180, rows[0].budget);
  // the older phone drops only the Bills caption; the pool's prints
  assert.equal(pureModule.terseCaption("Envelopes", rows[1].caption),
               "set aside $46 this month");
});

test("the Envelopes row opens its pools as a list, as sent", () => {
  const lines = pureModule.billsTableLines({ items: [], table: {
    rows: [{ label: "Garden (yearly pool $960)",
             text: "$410 this month · $410 this year · $550 left" }],
    outside: [{ label: "Garden: spent before the plan's months",
                amount: 70, sign: "+", figure: "+$70" }],
    total: { label: "Set aside in the plan", amount: 80, figure: "$80" } } });
  assert.deepEqual(lines.rows.map((r) => [r.label, r.figure, r.total]),
    [["Garden (yearly pool $960)",
      "$410 this month · $410 this year · $550 left", false],
     ["Garden: spent before the plan's months", "+$70", false],
     ["Set aside in the plan", "$80", true]]);
  assert.deepEqual(lines.toggle, ["Each pool", "Hide the pools"]);
  assert.equal(lines.heading, null);
  // the Bills table keeps its own button
  assert.deepEqual(pureModule.billsTableLines({ table: {
    rows: [], outside_heading: null, outside: [] } }).toggle,
    ["How this adds up", "Hide how this adds up"]);
});
