// One case table for the money helpers both clients carry: mobile's
// src/lib/pure.ts and the web's webapp/src/moneymath.ts. The mobile
// pure-logic tests run it against mobile; the server suite runs it against
// both, so the twins cannot drift apart.
//
// `impl` is a module exposing parseMoneyDraft, signedBalance,
// mergePlannerBuckets, reimbOwed, spendPickerGroups, localYmd,
// adoptsDeviceZone, the reimbursement matcher's reimbCandidateWhy,
// pairGapHint and reimbLeft, and receiptDetailsDraft.

// The awaiting-reimbursement rows the owed-back case runs, exported so the
// server suite can seed the same rows and check the server's own figure
// (alerts.reimb_pending, which the Today strip and the daily email state)
// against the client helper's. `amount` is the ledger sign: a charge is
// positive, a deposit negative.
export const REIMB_ROWS = [
  // a partial expectation: only the insurer's share comes back
  { amount: 100, expected: 60, received: 0 },
  // no expectation: the whole charge, less what already came back
  { amount: 40, expected: null, received: 15 },
  // a flagged DEPOSIT with no expectation: nothing is owed back on money
  // that came in, so it adds 0 rather than its face value
  { amount: -40, expected: null, received: 15 },
  { amount: -500, expected: null, received: 0 },
  // more came back than expected: nothing owed, never negative
  { amount: 80, expected: 30, received: 50 },
  { amount: 12 },
  // an expectation above the charge (saved before the server refused
  // one) counts only the charge: no link can bring back more
  { amount: 100, expected: 150, received: 0 },
  // a flagged deposit carrying an expectation still owes nothing back
  { amount: -40, expected: 40, received: 0 }];
export const REIMB_OWED = 60 + 25 + 0 + 0 + 0 + 12 + 100 + 0;

export function moneyCases(impl, assert) {
  const { parseMoneyDraft, signedBalance, mergePlannerBuckets, reimbOwed,
          spendPickerGroups, localYmd, adoptsDeviceZone, reimbCandidateWhy,
          pairGapHint, reimbLeft, receiptDetailsDraft } = impl;
  return {
    "a receipt's typed details are money in, a real day and a short store": () => {
      assert.deepEqual(receiptDetailsDraft("$12.34", "2030-03-01"),
        { ok: true, body: { total: 12.34, date: "2030-03-01" } });
      assert.deepEqual(receiptDetailsDraft(" 1,204.5 ", "2030-03-01", "  Lantern  "),
        { ok: true, body: { total: 1204.5, date: "2030-03-01", merchant: "Lantern" } });
      // a third decimal rounds to the cent the matcher compares — the
      // banker's way, like every money figure here (a half goes to even)
      assert.equal(receiptDetailsDraft("4.005", "2026-01-31").body.total, 4);
      assert.equal(receiptDetailsDraft("4.015", "2026-01-31").body.total, 4.02);
      for (const bad of ["", "abc", "0", "-3", "0.004", "."])
        assert.equal(receiptDetailsDraft(bad, "2030-03-01").ok, false, bad);
      for (const bad of ["", "03/01/2030", "2026-02-30", "2026-13-01", "2026-9-4"])
        assert.equal(receiptDetailsDraft("5", bad).ok, false, bad);
      assert.equal(receiptDetailsDraft("5", "2030-03-01", "x".repeat(81)).ok, false);
      assert.equal(receiptDetailsDraft("5", "2030-03-01", "x".repeat(80)).ok, true);
    },
    "a typed amount is parsed only once it is a number": () => {
      for (const [text, want] of [
        ["-250", -250], ["-0.5", -0.5], ["1200.", 1200], [".75", 0.75],
        ["$1,234.56", 1234.56], [" 42 ", 42], ["−180000", -180000],
        ["-", null], ["0-", null], [".", null], ["-.", null],
        ["NaN", null], ["Infinity", null], ["1e5", null], ["12-3", null],
        ["abc", null], ["", null], ["--5", null]]) {
        assert.equal(parseMoneyDraft(text), want, JSON.stringify(text));
      }
    },

    "card and loan balances come off a headline total": () => {
      const accts = [
        { kind: "depository/checking", balance_current: 1000 },
        { kind: "credit/credit card", balance_current: 250 },
        { kind: "loan/auto", balance_current: 400 },
        { kind: "investment/brokerage", balance_current: 50 },
        { kind: "depository/savings", balance_current: null },
        { kind: "", balance_current: 5 }];
      assert.deepEqual(accts.map(signedBalance), [1000, -250, -400, 50, 0, 5]);
      assert.equal(accts.reduce((t, a) => t + signedBalance(a), 0), 405);
    },

    "planner save keeps rule edits made elsewhere since it was seeded": () => {
      const pets = { name: "Pets", parent: "other", monthly: 60,
                     categories: ["pets"], merchants: ["Invented Pet Barn"] };
      const gym = { name: "Gym", parent: "other", monthly: 40,
                    categories: ["fitness"], merchants: [] };
      const snack = { name: "Snacks", parent: "food", monthly: 30,
                      categories: ["snacks"], merchants: [] };
      // the planner was seeded with Pets (no merchants yet) and Gym
      const rows = [
        { name: "Food", monthly: "500", food: true },
        { name: "Pets", monthly: "75", cats: ["pets"], merch: [],
          orig: "Pets", touched: true },          // amount edited here
        { name: "Gym", monthly: "40", cats: ["fitness"], merch: [],
          orig: "Gym" },                            // untouched here
        { name: "Hobby", monthly: "20", category: "hobbies" }];  // new here
      // meanwhile Bucket rules added a merchant to Pets, raised Gym, and
      // another device added a Travel bucket and a food bucket
      const travel = { name: "Travel", parent: "other", monthly: 90,
                       categories: ["travel"], merchants: [] };
      const live = [snack, pets, { ...gym, monthly: 45 }, travel];
      const out = mergePlannerBuckets(rows, live, ["Pets", "Gym"]);
      assert.deepEqual(out, [
        snack,
        { ...pets, monthly: 75 },                 // our amount, their merchant
        { ...gym, monthly: 45 },                  // untouched → live copy
        { name: "Hobby", parent: "other", monthly: 20,
          categories: ["hobbies"], merchants: [] },
        travel]);                                  // added elsewhere → kept
    },

    "planner save honours removals on either side": () => {
      const a = { name: "A", parent: "other", monthly: 10,
                  categories: ["a"], merchants: [] };
      const b = { name: "B", parent: "other", monthly: 20,
                  categories: ["b"], merchants: [] };
      // A removed here; B deleted elsewhere while untouched here
      const rows = [{ name: "B", monthly: "20", orig: "B" }];
      assert.deepEqual(mergePlannerBuckets(rows, [a], ["A", "B"]), []);
      // a touched row whose bucket vanished elsewhere is still written
      const renamed = [{ name: "B2", monthly: "25", cats: ["b"], merch: [],
                         orig: "B", touched: true }];
      assert.deepEqual(mergePlannerBuckets(renamed, [], ["B"]), [
        { name: "B2", parent: "other", monthly: 25, categories: ["b"],
          merchants: [] }]);
      // renaming keeps the live rules and does not duplicate the bucket
      assert.deepEqual(mergePlannerBuckets(renamed, [b], ["B"]), [
        { ...b, name: "B2", monthly: 25 }]);
      // a zeroed touched row is dropped, as the planner always did
      const zero = [{ name: "B", monthly: "0", orig: "B", touched: true }];
      assert.deepEqual(mergePlannerBuckets(zero, [b], ["B"]), []);
    },

    "what is owed back counts each charge against its expected amount": () => {
      assert.equal(reimbOwed(REIMB_ROWS), REIMB_OWED);
      assert.equal(reimbOwed([]), 0);
    },

    "a matcher row is compared by what each side has left": () => {
      const charge = { amount: 120, date: "2030-03-01" };
      assert.equal(reimbCandidateWhy({ amount: -120, date: "2030-03-11" },
                                     charge), "exact amount · 10 days later");
      assert.equal(reimbCandidateWhy({ amount: -100, date: "2030-03-02" },
                                     charge), "$20.00 short · 1 day later");
      assert.equal(reimbCandidateWhy({ amount: -150, date: "2030-02-27" },
                                     charge), "$30.00 over · 2 days earlier");
      assert.equal(reimbCandidateWhy({ amount: -150, date: "2030-03-01" },
                                     charge), "$30.00 over · same day");
      // a $100 charge that already got $60 back needs $40: a fresh $40
      // deposit is its exact match, not "$60.00 short"
      const owing = { amount: 100, date: "2030-03-01", left_amount: 40 };
      assert.equal(reimbCandidateWhy({ amount: -40, date: "2030-03-01",
                                       left_amount: 40 }, owing),
                   "exact amount · same day");
      // a $150 deposit with $100 already spent on another charge has $50
      // to give a $50 charge — not "$100.00 over"
      assert.equal(reimbCandidateWhy(
        { amount: -150, date: "2030-03-01", left_amount: 50 },
        { amount: 50, date: "2030-03-01", left_amount: 50 }),
        "exact amount · same day");
      // an anchor with nothing left is weighed at face value, as ranked
      assert.equal(reimbCandidateWhy({ amount: -80, date: "2030-03-01" },
        { amount: 80, date: "2030-03-01", left_amount: 0 }),
        "exact amount · same day");
      assert.equal(reimbLeft({ amount: -150, left_amount: 50 }), 50);
      assert.equal(reimbLeft({ amount: -150 }), 150);
    },

    "the matcher's gap line says what Pair will record": () => {
      const c50 = { amount: 50 };
      assert.equal(pairGapHint([50], c50, false), " — exact ✓");
      // one deposit within the dollar of the charge pairs it in full
      assert.match(pairGapHint([49.6], c50, false),
                   /\$0\.40 short, close enough: the whole charge counts as repaid/);
      assert.match(pairGapHint([49], c50, false), /repaid/);
      assert.equal(pairGapHint([50.5], c50, false), " — $0.50 over the charge");
      // past the slack, or ticked partial, the rest really stays spend
      assert.equal(pairGapHint([40], c50, false), " — $10.00 stays as spend");
      assert.equal(pairGapHint([49.6], c50, true), " — $0.40 stays as spend");
      // well over: the charge is repaid and the rest stays income; the
      // allowance is a dollar, not a share of the charge
      assert.equal(pairGapHint([80], c50, false),
                   " — $30.00 over the charge; the rest stays income");
      assert.match(pairGapHint([10400], { amount: 10000 }, false),
                   /rest stays income/);
      assert.doesNotMatch(pairGapHint([1001], { amount: 1000 }, false),
                          /rest stays income/);
      // two deposits are linked one at a time: the first is far short, so
      // both record what came back and the charge still owes the cents
      const c100 = { amount: 100 };
      assert.equal(pairGapHint([60, 39.5], c100, false),
                   " — $0.50 stays as spend");
      // a charge that already got $40 back needs $60: a $60 deposit clears it
      const owing = { amount: 100, left_amount: 60, received: 40 };
      assert.equal(pairGapHint([60], owing, false), " — exact ✓");
      // and the slack does not apply once something has come back
      assert.equal(pairGapHint([59.5], owing, false),
                   " — $0.50 stays as spend");
      // a second deposit after a full pair is refused and stays income
      assert.equal(pairGapHint([99.5, 20], c100, false),
                   " — $20.00 over the charge; the rest stays income");
      // a flagged DEPOSIT anchoring charges
      const dep = { amount: -100, left_amount: 100 };
      assert.equal(pairGapHint([100], dep, false), " — exact ✓");
      assert.equal(pairGapHint([60], dep, false),
                   " — $40.00 of this deposit stays income");
      assert.equal(pairGapHint([60, 70], dep, false),
                   " — $30.00 of those charges stays as spend");
    },

    "a rule or split picker never offers a flow category": () => {
      const cats = {
        plaid_spend: ["FOOD_AND_DRINK", "GENERAL_MERCHANDISE"],
        plaid_flow: ["TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME"],
        categories: ["FOOD_AND_DRINK", "TRANSFER_OUT", "Invented Hobby",
                     "income", "", "Invented Pets"] };
      assert.deepEqual(spendPickerGroups(cats), {
        spend: ["FOOD_AND_DRINK", "GENERAL_MERCHANDISE"],
        custom: ["Invented Hobby", "Invented Pets"] });
      assert.deepEqual(spendPickerGroups(undefined), { spend: [], custom: [] });
    },

    "a form's date default is the local calendar day": () => {
      // late evening locally: whatever the zone, the local fields win
      const d = new Date(2031, 11, 31, 23, 30);
      assert.equal(localYmd(d), "2031-12-31");
      assert.equal(localYmd(new Date(2031, 0, 5, 0, 5)), "2031-01-05");
    },

    "only a hosted owner on the instance zone adopts the device zone": () => {
      const me = { role: "owner", demo: false, hosted: true,
                   timezone: "Etc/UTC", timezone_source: "instance" };
      assert.equal(adoptsDeviceZone(me, "America/Chicago"), true);
      // self-hosted: the operator's configured zone keeps applying
      assert.equal(adoptsDeviceZone({ ...me, hosted: false },
                                    "America/Chicago"), false);
      assert.equal(adoptsDeviceZone({ ...me, role: "viewer" },
                                    "America/Chicago"), false);
      assert.equal(adoptsDeviceZone({ ...me, demo: true },
                                    "America/Chicago"), false);
      assert.equal(adoptsDeviceZone({ ...me, timezone_source: "household" },
                                    "America/Chicago"), false);
      assert.equal(adoptsDeviceZone(me, ""), false);
      assert.equal(adoptsDeviceZone({ ...me, timezone: "America/Chicago" },
                                    "America/Chicago"), false);
      // a privacy-hardened browser reports UTC, not a place
      for (const z of ["UTC", "Etc/UTC", "Etc/GMT", "GMT"])
        assert.equal(adoptsDeviceZone({ ...me, timezone: "America/Chicago" },
                                      z), false, z);
      assert.equal(adoptsDeviceZone(undefined, "America/Chicago"), false);
    },
  };
}
