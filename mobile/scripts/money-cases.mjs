// One case table for the money helpers both clients carry: mobile's
// src/lib/pure.ts and the web's webapp/src/moneymath.ts. The mobile
// pure-logic tests run it against mobile; the server suite runs it against
// both, so the twins cannot drift apart.
//
// `impl` is a module exposing parseMoneyDraft, signedBalance,
// mergePlannerBuckets, spendPickerGroups, localYmd,
// adoptsDeviceZone, the reimbursement matcher's reimbCandidateWhy,
// pairGapHint and reimbLeft, receiptDetailsDraft, and the whole-value
// save rebase (rebaseFields, rebaseKeyed, rebaseNamedList, rebaseNamedSave,
// assetListLanded), the planner's historyBasisNote, the words after a
// narrowed month's listed spending (listedSpendWord), and the compare-and-swap for the four whole-list settings
// (listVersionsFor, remergeWholeList, settingsMovedOf, saveWholeLists,
// SETTINGS_MOVED_TEXT), and the sentence naming the stored credentials a
// restore did not keep (droppedCredentialsNote), and an import's count line
// (importCountText), a net's printed sign (netSign), the screen-states words, retry rule and figure
// gate (failLine, failCause, staleLine, retryRead, knownOr), and the budget planner's shares (plannerShares, over
// the table in share-cases.json the server suite also runs) — some cases
// are async, so
// a runner awaits each.

import shareCases from "./share-cases.json" with { type: "json" };

export function moneyCases(impl, assert) {
  const { parseMoneyDraft, signedBalance, mergePlannerBuckets,
          spendPickerGroups, localYmd, adoptsDeviceZone, reimbCandidateWhy,
          pairGapHint, reimbLeft, receiptDetailsDraft, rebaseFields,
          rebaseKeyed, rebaseNamedList, rebaseNamedSave, assetListLanded,
          historyBasisNote, listedSpendWord, listVersionsFor, remergeWholeList,
          settingsMovedOf, saveWholeLists, SETTINGS_MOVED_TEXT,
          droppedCredentialsNote, importCountText, mergeBucketRules,
          bucketRulesSaved, plannerBucketsSaved, savedMergeBase,
          plannerShares, saveWaits, SAVE_WAITS_LOADING, SAVE_WAITS_FAILED,
          SettingsUnreadError, UNKNOWN_FIGURE, inputPhase, plannerWaits,
          PLANNER_BILLS_FAILED, PLANNER_HISTORY_FAILED, failLine, staleLine,
          shownAt, failureStatus, retryRead, retryReadDelay, knownOr,
          figureGreyed, busyText, restartingText, failCause, noAnswer,
          EMPTY_LINES, noAccountsYet, READ_LIMIT_MS, isRead, timedOut,
          fetchRead, readFinished, savedAmount, netSign, bizViewWithout } = impl;
  // A failed read in each client's shape: the web throws
  // Error("NNN: <body>") (BusyError and AuthError carry `status` too), the
  // phone an ApiError with `status` and the parsed `detail`; a request
  // that got no answer is a TypeError on both.
  const webErr = (status, body, extra = {}) =>
    Object.assign(new Error(`${status}: ${body}`), extra);
  const phoneErr = (status, detail, extra = {}) =>
    Object.assign(new Error(`${status}: ${detail}`),
                  { status, detail, ...extra });
  return {
    // A settings save stores numbers: blank is 0, a typed amount is read
    // by the server's separator rules, and text that is not an amount
    // refuses the save, naming the field — never a silent $0.
    "saved amounts: unparseable text refuses the save": () => {
      assert.equal(savedAmount("", "Gas"), 0);
      assert.equal(savedAmount(" $ ", "Gas"), 0);
      assert.equal(savedAmount("$1,250.50", "Gas"), 1250.5);
      assert.equal(savedAmount("12,50", "Gas"), 12.5);
      for (const bad of ["abc", "1.25,5", "12,5,0", "-"])
        assert.throws(() => savedAmount(bad, "Gas"),
                      (e) => e.message.startsWith(`Gas: "${bad}"`), bad);
    },
    "bucket rules: an unparseable amount refuses the save, not $0": () => {
      const row = { name: "Gas", parent: "other", monthly: "abc",
                    categories: ["gas"], merchants: "", touched: true };
      assert.throws(() => mergeBucketRules([row], [], [], savedAmount),
                    /Gas: "abc" is not an amount/);
      assert.equal(mergeBucketRules([{ ...row, monthly: "80" }], [], [],
                                    savedAmount)[0].monthly, 80);
    },
    // The planner's shares are worked out on the client (they echo what is
    // being typed) by the server's rule: one table, run here through both
    // twins and in the server suite through reporting.shown_shares.
    "planner shares: the shared case table": () => {
      for (const c of shareCases.cases)
        assert.deepEqual(plannerShares(c.parts, c.base), c.shares, c.name);
    },
    // A save can land while the person is still typing: the rows stay as
    // typed and only the base moves. It may then name only buckets the
    // rows sent held — a bucket another card saved, carried through by
    // that save, is not in the rows and must stay "added elsewhere".
    "bucket rules: a save landing mid-edit keeps a bucket added elsewhere": () => {
      const gas = { name: "Gas", parent: "other", monthly: 80,
                    categories: ["gas"], merchants: [] };
      const pets = { name: "Pets", parent: "other", monthly: 50,
                     categories: ["pets"], merchants: [] };
      const opened = bucketRulesSaved([gas]);
      const sent = opened.rows;
      const first = mergeBucketRules(sent, [gas, pets], opened.base, Number);
      assert.deepEqual(first, [gas, pets]);
      const landed = bucketRulesSaved(first, sent);
      assert.deepEqual(landed.base, ["Gas"]);
      // still typing: the on-screen rows never held Pets
      const typed = sent.map((r) => ({ ...r, monthly: "90", touched: true }));
      assert.deepEqual(mergeBucketRules(typed, first, landed.base, Number),
                       [{ ...gas, monthly: 90 }, pets]);
      // a bucket added here and saved mid-edit is in the base: removing
      // or renaming it later still sticks
      const added = [...sent, { name: "Toys", parent: "other", monthly: "10",
                                categories: ["toys"], merchants: "",
                                touched: true }];
      const second = mergeBucketRules(added, [gas], opened.base, Number);
      const b2 = bucketRulesSaved(second, added).base;
      assert.deepEqual(b2, ["Gas", "Toys"]);
      assert.deepEqual(mergeBucketRules(sent, second, b2, Number), [gas]);
      const renamed = [...sent, { ...added[1], name: "Toy" }];
      assert.deepEqual(
        mergeBucketRules(renamed, second, b2, Number).map((b) => b.name),
        ["Gas", "Toy"]);
    },

    // Mid-edit the on-screen rows keep a bucket that was deleted or
    // renamed elsewhere meanwhile. Its name must stay in the base, so the
    // next save reads the untouched row as "deleted elsewhere", not as a
    // bucket new here.
    "bucket rules: deleted or renamed elsewhere mid-edit stays that way": () => {
      const a = { name: "A", parent: "other", monthly: 10, categories: ["a"],
                  merchants: [] };
      const b = { name: "B", parent: "other", monthly: 20, categories: ["b"],
                  merchants: [] };
      for (const [label, live, want] of [
          ["deleted", [b], ["B"]],
          ["renamed", [{ ...a, name: "A2" }, b], ["B", "A2"]]]) {
        const opened = bucketRulesSaved([a, b]);
        const sent = opened.rows.map((r) => r.name === "B"
          ? { ...r, monthly: "25", touched: true } : r);
        const first = mergeBucketRules(sent, live, opened.base, Number);
        assert.deepEqual(first.map((x) => x.name), want, label);
        // landed mid-edit: the rows still show A, untouched
        const landed = bucketRulesSaved(first, sent, opened.base);
        const typed = sent.map((r) => r.name === "B"
          ? { ...r, monthly: "30" } : r);
        assert.deepEqual(
          mergeBucketRules(typed, first, landed.base, Number)
            .map((x) => x.name), want, label);
        // an EDITED row is the person's: it is written even though its
        // bucket was removed elsewhere
        const kept = typed.map((r) => r.name === "A"
          ? { ...r, touched: true } : r);
        assert.ok(mergeBucketRules(kept, first, landed.base, Number)
          .some((x) => x.name === "A"), label);
      }
    },

    "savings goals: a save landing mid-edit keeps a goal added elsewhere": () => {
      const g = (name) => ({ name, target: 100, target_date: null,
        monthly_plan: 10, account_id: null, tokens: [], start_balance: 0,
        mode: "monthly" });
      // the planner saves its Savings goal while the goals card is open
      // with Car, and the card's save lands mid-edit
      const stored = [g("Car"), g("Savings")];
      const base = savedMergeBase(stored.map((x) => x.name),
                                  [{ name: "Car" }], ["Car"]);
      assert.deepEqual(base, ["Car"]);
      // re-seeded: every stored name
      assert.deepEqual(savedMergeBase(["Car", "Savings"]), ["Car", "Savings"]);
      // removed elsewhere mid-edit: stays in the base
      assert.deepEqual(savedMergeBase(["Car"], [{ name: "Car" },
                                                { name: "Trip" }],
                                      ["Car", "Trip"]), ["Car", "Trip"]);
    },

    "planner: a save landing mid-edit keeps a bucket added elsewhere": () => {
      const food = { name: "Food", monthly: "500", food: true };
      const gas = { name: "Gas", parent: "other", monthly: 80,
                    categories: ["gas"], merchants: [] };
      const pets = { name: "Pets", parent: "other", monthly: 50,
                     categories: ["pets"], merchants: [] };
      const opened = plannerBucketsSaved([gas]);
      const sent = [food, ...opened.rows];
      const first = mergePlannerBuckets(sent, [gas, pets], opened.base);
      assert.deepEqual(first, [gas, pets]);
      const landed = plannerBucketsSaved(first, sent);
      assert.deepEqual(landed.base, ["Gas"]);
      const typed = sent.map((r) => r.food ? r
        : { ...r, monthly: "90", touched: true });
      assert.deepEqual(mergePlannerBuckets(typed, first, landed.base),
                       [{ ...gas, monthly: 90 }, pets]);
      // added here and saved mid-edit: the row keeps no `orig`, and its
      // removal or rename later still sticks
      const added = [...sent, { name: "Hobby", monthly: "20",
                                category: "hobbies" }];
      const second = mergePlannerBuckets(added, [gas], opened.base);
      const b2 = plannerBucketsSaved(second, added).base;
      assert.deepEqual(b2, ["Gas", "Hobby"]);
      assert.deepEqual(mergePlannerBuckets(sent, second, b2), [gas]);
      const renamed = [...sent, { ...added[2], name: "Hob", touched: true }];
      assert.deepEqual(
        mergePlannerBuckets(renamed, second, b2).map((b) => b.name),
        ["Gas", "Hob"]);
    },

    "planner: a bucket added, saved, then removed stays gone": () => {
      const food = { name: "Food", monthly: "500", food: true };
      const opened = plannerBucketsSaved([]);
      const first = mergePlannerBuckets(
        [food, ...opened.rows,
         { name: "Hobby", monthly: "20", category: "hobbies" }],
        [], opened.base);
      assert.deepEqual(first, [{ name: "Hobby", parent: "other", monthly: 20,
                                 categories: ["hobbies"], merchants: [] }]);
      // the saved view is the planner's new merge base
      const saved = plannerBucketsSaved(first);
      assert.deepEqual(saved.base, ["Hobby"]);
      assert.deepEqual(mergePlannerBuckets([food], first, saved.base), []);
    },

    "planner: a bucket added, saved, then renamed is one bucket": () => {
      const food = { name: "Food", monthly: "500", food: true };
      const snack = { name: "Snacks", parent: "food", monthly: 30,
                      categories: ["snacks"], merchants: [] };
      const first = mergePlannerBuckets(
        [food, { name: "Hobby", monthly: "20", category: "hobbies" }],
        [snack], []);
      const saved = plannerBucketsSaved(first);
      // food buckets are the Bucket-rules card's: never planner rows
      assert.deepEqual(saved.base, ["Hobby"]);
      assert.deepEqual(saved.rows.map((r) => r.orig), ["Hobby"]);
      const renamed = [food, ...saved.rows.map((r) => ({ ...r, name: "Hob",
                                                         touched: true }))];
      assert.deepEqual(mergePlannerBuckets(renamed, first, saved.base), [
        snack, { name: "Hob", parent: "other", monthly: 20,
                 categories: ["hobbies"], merchants: [] }]);
      // untouched after the save: follows a later edit made elsewhere
      const elsewhere = [snack, { ...first[1], monthly: 25 }];
      assert.deepEqual(
        mergePlannerBuckets([food, ...saved.rows], elsewhere, saved.base),
        elsewhere);
    },

    "bucket rules: a bucket added, saved, then removed stays gone": () => {
      // the card opens on an empty list and the person adds Pets
      const opened = bucketRulesSaved([]);
      const added = [...opened.rows,
        { name: "Pets", parent: "other", monthly: "100",
          categories: ["pets"], merchants: "", touched: true }];
      const first = mergeBucketRules(added, [], opened.base, Number);
      assert.deepEqual(first, [{ name: "Pets", parent: "other", monthly: 100,
                                 categories: ["pets"], merchants: [] }]);
      // the server stores it; the saved view is the card's new merge base
      const saved = bucketRulesSaved(first);
      assert.deepEqual(saved.base, ["Pets"]);
      // removed in the same session: the fresh read still holds Pets, and
      // it must not come back as "added elsewhere"
      assert.deepEqual(mergeBucketRules([], first, saved.base, Number), []);
    },

    "bucket rules: a bucket added, saved, then renamed is one bucket": () => {
      const opened = bucketRulesSaved([]);
      const first = mergeBucketRules([
        { name: "Pets", parent: "other", monthly: "100",
          categories: ["pets"], merchants: "", touched: true }],
        [], opened.base, Number);
      const saved = bucketRulesSaved(first);
      const renamed = saved.rows.map((r) => ({ ...r, name: "Pet",
                                               touched: true }));
      assert.deepEqual(mergeBucketRules(renamed, first, saved.base, Number), [
        { name: "Pet", parent: "other", monthly: 100, categories: ["pets"],
          merchants: [] }]);
      // after the save the rows are the stored copy again: an untouched
      // row follows what another device saved since, and a bucket added
      // elsewhere after the save is still carried through
      const elsewhere = [{ ...first[0], monthly: 120 },
        { name: "Gym", parent: "other", monthly: 40, categories: ["fitness"],
          merchants: [] }];
      assert.deepEqual(
        mergeBucketRules(saved.rows, elsewhere, saved.base, Number),
        elsewhere);
    },

    "an import counts what it added apart from what was already here": () => {
      // an older server sends no `matched`: exactly today's words
      assert.equal(importCountText({ imported: 3 }), "Imported 3 transactions");
      assert.equal(importCountText({ imported: 3, skipped_duplicates: 2 }),
                   "Imported 3 transactions · skipped 2 already covered");
      assert.equal(importCountText({}), "Imported 0 transactions");
      // nothing matched: unchanged
      assert.equal(importCountText({ imported: 3, matched: 0 }),
                   "Imported 3 transactions");
      // the same file a second time
      assert.equal(importCountText({ imported: 0, matched: 3 }),
                   "Nothing new: 3 already here");
      assert.equal(importCountText({ imported: 2, matched: 5 }),
                   "Imported 2 new, 5 already here");
      // the covered-by-another-source tail rides every form
      assert.equal(importCountText({ imported: 0, matched: 3,
                                     skipped_duplicates: 1 }),
                   "Nothing new: 3 already here · skipped 1 already covered");
      // zero everything, with the key present
      assert.equal(importCountText({ imported: 0, matched: 0,
                                     skipped_duplicates: 0, skipped_older: 0 }),
                   "Imported 0 transactions");
      // a folder's per-file line: no verb, no covered tail, as today
      assert.equal(importCountText({ imported: 4, skipped_duplicates: 2 },
                                   "file"), "4 transactions");
      assert.equal(importCountText({ imported: 0, matched: 4 }, "file"),
                   "Nothing new: 4 already here");
      assert.equal(importCountText({ imported: 1, matched: 4 }, "file"),
                   "Imported 1 new, 4 already here");
      // the older-than-history rows are their own note, never in here
      assert.equal(importCountText({ imported: 3, skipped_older: 9 }),
                   "Imported 3 transactions");
    },
    "a restore says which stored credentials it did not keep, and nothing else": () => {
      const AI = "The AI service's address changed, so its key was not kept. "
        + "Enter it again under Settings → AI.";
      const MAIL = "The mail server changed, so its password was not kept. "
        + "Enter it again under Settings → Email & Push.";
      // an older server sends no field; an empty list has nothing to say
      assert.equal(droppedCredentialsNote(undefined), null);
      assert.equal(droppedCredentialsNote(null), null);
      assert.equal(droppedCredentialsNote([]), null);
      assert.equal(droppedCredentialsNote(["ai_key"]), AI);
      assert.equal(droppedCredentialsNote(["mail_password"]), MAIL);
      assert.equal(droppedCredentialsNote(["ai_key", "mail_password"]),
                   `${AI} ${MAIL}`);
      // the sentences keep their own order, whatever order arrives
      assert.equal(droppedCredentialsNote(["mail_password", "ai_key"]),
                   `${AI} ${MAIL}`);
      // a name this client does not know is not guessed at
      assert.equal(droppedCredentialsNote(["plaid_secret"]), null);
      assert.equal(droppedCredentialsNote(["plaid_secret", "ai_key"]), AI);
      assert.equal(droppedCredentialsNote("ai_key"), null);
    },
    "a net that prints $0 is even, whichever side of zero its cents are": () => {
      assert.equal(netSign(-0.4), 0);
      assert.equal(netSign(0.3), 0);
      assert.equal(netSign(0), 0);
      // halves to even, as money() prints them
      assert.equal(netSign(0.5), 0);
      assert.equal(netSign(-0.5), 0);
      assert.equal(netSign(1.5), 1);
      assert.equal(netSign(-1.5), -1);
      assert.equal(netSign(-0.6), -1);
      assert.equal(netSign(7), 1);
    },
    "a narrowed month's spending says whose it is, by the server's scope": () => {
      assert.equal(listedSpendWord("business"), "spent (business)");
      assert.equal(listedSpendWord("household"), "spent");
      // an older server sends no scope: the household's words
      assert.equal(listedSpendWord(undefined), "spent");
      assert.equal(listedSpendWord(null), "spent");
    },
    "the planner says how many days a scaled suggestion stands on": () => {
      assert.equal(historyBasisNote({ history_days: 12, history_scaled: true,
                                      min_rate_days: 7 }),
                   "Based on 12 days of history — scaled to a month.");
      // under the settled threshold the line says the figure will move
      // and asks for a revisit
      assert.equal(historyBasisNote({ history_days: 3, history_scaled: true,
                                      min_rate_days: 7 }),
                   "Based on 3 days of history — scaled to a month — under "
                   + "a week, so expect it to move; revisit after your first "
                   + "full month.");
      assert.equal(historyBasisNote({ history_days: 1, history_scaled: true,
                                      min_rate_days: 7 }).startsWith(
                     "Based on 1 day of history"), true);
      // complete months behind it, no history, or no payload: no line
      assert.equal(historyBasisNote({ history_days: 95, history_scaled: false,
                                      min_rate_days: 7 }), null);
      assert.equal(historyBasisNote({ history_days: 0, history_scaled: true }),
                   null);
      assert.equal(historyBasisNote(undefined), null);
      // a ledger that began today: no suggestion, and the line says why
      assert.equal(historyBasisNote({ history_days: 0, history_scaled: true,
                                      not_enough_history: true }),
                   "Not enough history yet — come back tomorrow.");
    },
    "a whole-value save keeps what another device saved meanwhile": () => {
      // one field changed here, another changed elsewhere: both survive
      assert.deepEqual(
        rebaseFields({ on: true, hour: 7 }, { on: false, hour: 7 },
                     { on: false, hour: 9 }),
        { on: true, hour: 9 });
      // a field changed both places: this save's edit wins
      assert.deepEqual(
        rebaseFields({ hour: 8 }, { hour: 7 }, { hour: 9 }), { hour: 8 });
      // keyed (debt overrides, schedule cadences): untouched keys follow
      // the stored copy, a key deleted elsewhere stays gone unless edited
      // here, a key added elsewhere is kept, one removed here stays removed
      const base = { a: { apr: "", skip: false }, b: { apr: "5", skip: false },
                     c: { apr: "", skip: false }, d: { apr: "1", skip: false } };
      const live = { a: { apr: "19", skip: false }, b: { apr: "5", skip: true },
                     e: { apr: "", skip: false } };
      const mine = { a: { apr: "", skip: true }, b: { apr: "6", skip: false },
                     c: { apr: "2", skip: false } };
      assert.deepEqual(rebaseKeyed(mine, base, live), {
        a: { apr: "19", skip: true }, b: { apr: "6", skip: true },
        e: { apr: "", skip: false }, c: { apr: "2", skip: false } });
      // nothing edited: the save writes back exactly what is stored
      assert.deepEqual(rebaseKeyed(base, base, live), live);
    },
    "the asset list saves only this editor's rows over the stored list": () => {
      const opened = [
        { name: "Home", kind: "property", value: 400000, as_of: "2026-01-01",
          auto: "zillow" },
        { name: "Car", kind: "vehicle", value: 20000, as_of: "2026-01-01" },
        { name: "Boat", kind: "other", value: 5000, as_of: "2026-01-01" }];
      // since the editor opened: the nightly valuation moved Home, another
      // device added a Loan and deleted the Boat
      const stored = [
        { ...opened[0], value: 410000, as_of: "2026-02-01" }, opened[1],
        { name: "Loan", kind: "mortgage", value: -90000, as_of: "2026-02-01" }];
      const keys = ["Home\u00000", "Car\u00000", "Boat\u00000"];
      // here: Car's value edited (as_of cleared), Home untouched, Boat
      // left alone, one new row
      const rows = [
        { ...opened[0], orig: keys[0] },
        { ...opened[1], value: 18000, as_of: null, orig: keys[1] },
        { ...opened[2], orig: keys[2] },
        { name: "Bike", kind: "vehicle", value: 900 }];
      assert.deepEqual(rebaseNamedList(rows, opened, stored), [
        stored[0],
        { name: "Car", kind: "vehicle", value: 18000, as_of: null },
        stored[2],
        { name: "Bike", kind: "vehicle", value: 900 }]);
      // a row removed here stays removed; a rename keeps the row's
      // stored fields under the new name
      assert.deepEqual(rebaseNamedList(
        [{ ...opened[0], name: "House", orig: keys[0] }], opened.slice(0, 2),
        stored.slice(0, 2)),
        [{ ...stored[0], name: "House" }]);
      // two rows of one name are two rows
      const twins = [{ name: "Lot", value: 1 }, { name: "Lot", value: 2 }];
      assert.deepEqual(rebaseNamedList(
        [{ ...twins[0], orig: "Lot\u00000" }, { name: "Lot", value: 3, orig: "Lot\u00001" }],
        twins, twins), [{ name: "Lot", value: 1 }, { name: "Lot", value: 3 }]);
    },
    "saving the asset list again after a lost reply adds nothing twice": () => {
      // opened on [House]; House renamed Home, Car added; the first Save
      // lands but its reply is lost, so the editor is still open
      const opened = [{ name: "House", kind: "property", value: 300000,
                        as_of: "2026-01-01" }];
      const rows = [{ ...opened[0], name: "Home", orig: "House\u00000" },
                    { name: " Car ", kind: "vehicle", value: 9000.004 }];
      const first = rebaseNamedSave(rows, opened, opened, null);
      assert.deepEqual(first.list.map((a) => a.name), ["Home", " Car "]);
      // the server stored it its own way: names trimmed, a date stamped
      const stored = [{ ...opened[0], name: "Home" },
                      { name: "Car", kind: "vehicle", value: 9000,
                        as_of: "2026-10-02" }];
      assert.equal(assetListLanded(first.list, stored), true);
      const kept = rows.map((r, i) => ({ ...r, ...first.rows[i] }));
      const again = rebaseNamedSave(kept, first.baseline, stored, first.list);
      assert.deepEqual(again.list.map((a) => a.name.trim()), ["Home", "Car"]);
      assert.deepEqual(again.baseline, stored);
      // the old rebase, given the same retry, wrote both rows twice
      assert.equal(rebaseNamedList(rows, opened, stored).length, 4);
      // an edit made after the lost reply still goes in, once
      const edited = kept.map((r, i) => (i === 1 ? { ...r, value: 8000 } : r));
      const third = rebaseNamedSave(edited, first.baseline, stored, first.list);
      assert.equal(third.list.length, 2);
      assert.equal(third.list[1].value, 8000);
      // the first Save never landed: the retry is the ordinary rebase
      const retry = rebaseNamedSave(kept, first.baseline, opened, first.list);
      assert.deepEqual(retry.list, first.list);
      assert.equal(assetListLanded(first.list, opened), false);
    },
    "a receipt's typed details are money in, a real day and a short store": () => {
      assert.deepEqual(receiptDetailsDraft("$12.34", "2030-03-01"),
        { ok: true, body: { total: 12.34, date: "2030-03-01" } });
      assert.deepEqual(receiptDetailsDraft(" 1,204.5 ", "2030-03-01", "  Lantern  "),
        { ok: true, body: { total: 1204.5, date: "2030-03-01", merchant: "Lantern" } });
      // a store field the person emptied is sent empty: it clears a store
      // misread off the slip (no field at all keeps it)
      assert.deepEqual(receiptDetailsDraft("12.34", "2030-03-01", "   "),
        { ok: true, body: { total: 12.34, date: "2030-03-01", merchant: "" } });
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
      // the store the form was filled with, saved untouched, is kept as
      // stored however long a reading left it; a changed one is checked
      const read = "y".repeat(100);
      assert.deepEqual(receiptDetailsDraft("5", "2030-03-01", read, read),
        { ok: true, body: { total: 5, date: "2030-03-01" } });
      assert.equal(receiptDetailsDraft("5", "2030-03-01", read + "z", read).ok, false);
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

    "a typed comma is read by the server's separator rules": () => {
      // the server reads "12,50" as twelve-fifty; stripping every comma
      // sent 1250 for it. A comma that fits no convention is refused.
      for (const [text, want] of [
        ["12,50", 12.5], ["12,5", 12.5], ["-12,50", -12.5],
        ["1.234,56", 1234.56], ["$12.345,67", 12345.67],
        ["1,234.56", 1234.56], ["1,234", 1234], ["1,234,567", 1234567],
        ["1,2,3", null], ["12,345,6", null], ["1,2345", null],
        ["12,", null], [",50", null], ["1.234,567", null],
        ["1,234.", null]]) {
        assert.equal(parseMoneyDraft(text), want, JSON.stringify(text));
      }
      assert.equal(receiptDetailsDraft("27,35", "2030-03-01").body.total,
                   27.35);
      const rows = [{ name: "Hobby", monthly: "12,50", category: "hobbies" },
                    { name: "Club", monthly: "1,2,3", category: "clubs" }];
      assert.deepEqual(mergePlannerBuckets(rows, [], []), [
        { name: "Hobby", parent: "other", monthly: 12.5,
          categories: ["hobbies"], merchants: [] }]);
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
    },

    "a stored bucket set to $0 in the planner keeps its rules": () => {
      // $0 is a real plan ("nothing allowed here"), not a removal — the
      // row's ✕ is the removal. Dropping the bucket threw away the
      // categories and merchants it claims spending by.
      const b = { name: "B", parent: "other", monthly: 20,
                  categories: ["b"], merchants: ["Invented Shop"] };
      for (const monthly of ["0", "", "0.00"]) {
        const zero = [{ name: "B", monthly, orig: "B", touched: true }];
        assert.deepEqual(mergePlannerBuckets(zero, [b], ["B"]),
                         [{ ...b, monthly: 0 }], JSON.stringify(monthly));
      }
      // its rules survive even when the live copy vanished meanwhile
      const seeded = [{ name: "B", monthly: "0", cats: ["b"],
                        merch: ["Invented Shop"], orig: "B", touched: true }];
      assert.deepEqual(mergePlannerBuckets(seeded, [], ["B"]),
                       [{ ...b, monthly: 0 }]);
      // a row that was never a bucket has no rules to keep: $0 adds none
      const fresh = [{ name: "New", monthly: "0", category: "x" }];
      assert.deepEqual(mergePlannerBuckets(fresh, [], []), []);
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

    "an unflagged business row leaves only the views whose scope holds it": () => {
      const row = { id: "t2", date: "2032-03-14", amount: 120 };
      const y2032 = { rows: [{ id: "t1" }, { id: "t2" }], count: 9,
                      total: 900, years: [2032, 2031] };
      const y2031 = { rows: [{ id: "t9" }], count: 4, total: 400,
                      years: [2032, 2031] };
      // another year's view is untouched: none of its rows changed
      assert.deepEqual(bizViewWithout(y2031, 2031, row), y2031);
      const same = bizViewWithout(y2032, 2032, row);
      assert.deepEqual(same.rows, [{ id: "t1" }]);
      assert.equal(same.count, 8);
      assert.equal(same.total, 780);
      const all = bizViewWithout({ ...y2031, count: 13, total: 1300 },
                                 "all", row);
      assert.equal(all.count, 12);
      assert.equal(all.total, 1180);
      assert.equal(bizViewWithout(y2032, undefined, row).count, 8);
      // the count spans the whole scope: a row past the fetch cap still
      // comes off it, though it was never among the fetched rows
      const capped = bizViewWithout({ ...y2032, rows: [{ id: "t1" }] },
                                    2032, row);
      assert.equal(capped.count, 8);
      assert.equal(capped.total, 780);
      // a second success for the same row (two taps, two requests) takes
      // nothing more off — a fetched row once, a row past the cap once
      assert.deepEqual(bizViewWithout(same, 2032, row), same);
      assert.deepEqual(bizViewWithout(capped, 2032, row), capped);
      // a view that holds its whole scope and not the row has nothing of it
      const whole = { rows: [{ id: "t1" }], count: 1, total: 100,
                      years: [2032] };
      assert.deepEqual(bizViewWithout(whole, 2032, row), whole);
      // repeated unflags of other-year rows never drive a view negative
      let v = y2031;
      for (let i = 0; i < 10; i++)
        v = bizViewWithout(v, 2031, { id: `x${i}`, date: "2032-01-0" + (i % 9 + 1), amount: 300 });
      assert.deepEqual(v, y2031);
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
    "an account still enrolling its second factor adopts no zone until it has one": () => {
      const me = { role: "owner", demo: false, hosted: true, needs_2fa: true,
                   timezone: "Etc/UTC", timezone_source: "instance" };
      // every settings write is refused until a factor is enrolled
      assert.equal(adoptsDeviceZone(me, "America/Chicago"), false);
      // the /api/me after enrolment is the one that adopts
      assert.equal(adoptsDeviceZone({ ...me, needs_2fa: false },
                                    "America/Chicago"), true);
    },
    // ---- whole-list settings: compare-and-swap, re-merge, one retry ----
    "a settings save carries versions only for the whole-list keys it sends": () => {
      const v = { savings_goals: "g1", custom_buckets: "b1",
                  income_scenarios: "s1", email_schedule: "e1" };
      assert.deepEqual(listVersionsFor({ savings_goals: [], theme: "dark" }, v),
                       { savings_goals: "g1" });
      assert.deepEqual(
        listVersionsFor({ custom_buckets: null, email_schedule: {} }, v),
        { custom_buckets: "b1", email_schedule: "e1" });
      // nothing whole-list in the body: nothing attached
      assert.equal(listVersionsFor({ theme: "dark", manual_assets: [] }, v), null);
      // an older server that publishes no versions: nothing attached
      assert.equal(listVersionsFor({ savings_goals: [] }, undefined), null);
      assert.equal(listVersionsFor({ savings_goals: [] }, {}), null);
    },
    "a moved savings-goals list keeps the other device's goals and applies mine": () => {
      const g = (name, target, monthly_plan = 0) =>
        ({ name, target, monthly_plan, tokens: [], start_balance: 0 });
      const base = [g("Boat", 900), g("Trip", 400), g("Roof", 7000)];
      // here: Boat retargeted, Roof removed, Bike added
      const mine = [g("Boat", 1200), g("Trip", 400), g("Bike", 300)];
      // there: Trip's plan changed, Pond added, Boat's plan changed
      const theirs = [g("Boat", 900, 50), g("Trip", 400, 25), g("Roof", 7000),
                      g("Pond", 150)];
      assert.deepEqual(remergeWholeList("savings_goals", base, mine, theirs),
        [g("Boat", 1200, 50), g("Trip", 400, 25), g("Pond", 150),
         g("Bike", 300)]);
      // the same field changed on both: the save being made wins
      assert.deepEqual(
        remergeWholeList("savings_goals", [g("Boat", 900)], [g("Boat", 1200)],
                         [g("Boat", 1000)]),
        [g("Boat", 1200)]);
    },
    "a moved bucket list keeps the other device's buckets and applies mine": () => {
      const b = (name, cap, parent = "Food") => ({ name, parent, cap, tokens: [] });
      const base = [b("Snacks", 40), b("Coffee", 30)];
      const mine = [b("Snacks", 55)];                 // Coffee removed here
      const theirs = [b("Snacks", 40), b("Coffee", 35), b("Lunch", 90)];
      assert.deepEqual(remergeWholeList("custom_buckets", base, mine, theirs),
                       [b("Snacks", 55), b("Lunch", 90)]);
      // everything cleared here: what the other device added still stands
      assert.deepEqual(remergeWholeList("custom_buckets", base, null, theirs),
                       [b("Lunch", 90)]);
    },
    "a moved scenario list matches names the way the server does": () => {
      const s = (name, take_home, cadence = "biweekly") =>
        ({ name, take_home, cadence });
      const base = [s("Plan A", 2100), s("Plan B", 1800)];
      const mine = [s("Plan A", 2250), s("Plan B", 1800)];
      // the other device renamed nothing but re-cased B and changed its pay
      const theirs = [s("Plan A", 2100), s("plan b", 1900), s("Side", 400)];
      assert.deepEqual(remergeWholeList("income_scenarios", base, mine, theirs),
                       [s("Plan A", 2250), s("plan b", 1900), s("Side", 400)]);
    },
    "a moved email schedule merges per cadence and sends only what it names": () => {
      const base = { daily: { on: true, hour: 7 }, monthly: { on: true, hour: 8 },
                     yearly: { on: false, hour: 9 } };
      // this edit: the daily hour, and only the daily cadence is sent
      const mine = { daily: { on: true, hour: 6 } };
      const theirs = { daily: { on: true, hour: 7, push: true },
                       monthly: { on: false, hour: 8 },
                       yearly: { on: false, hour: 9 } };
      assert.deepEqual(remergeWholeList("email_schedule", base, mine, theirs),
                       { daily: { on: true, hour: 6, push: true } });
      // a reset (null / {}) is sent as it is
      assert.equal(remergeWholeList("email_schedule", base, null, theirs), null);
      assert.deepEqual(remergeWholeList("email_schedule", base, {}, theirs), {});
    },
    "the AI backends, their routing and the hand-valued assets carry versions too": () => {
      const v = { llm_backends: "l1", llm_roles: "r1", manual_assets: "m1",
                  savings_goals: "g1" };
      assert.deepEqual(
        listVersionsFor({ llm_backends: [], llm_roles: {},
                          taxdocs_allow_remote_llm: "" }, v),
        { llm_backends: "l1", llm_roles: "r1" });
      assert.deepEqual(listVersionsFor({ manual_assets: [] }, v),
                       { manual_assets: "m1" });
    },
    "a moved AI backend list merges by id and keeps the other device's backend": () => {
      const k = (id, model, extra = {}) =>
        ({ id, name: id, url: "https://ai.example/v1", model, ...extra });
      // the view rows carry presence flags the save never sends
      const base = [k("a1", "m-1", { api_key_set: true }), k("b2", "m-2")];
      // here: a1's model changed, b2 removed, c3 added
      const mine = [k("a1", "m-1b"), k("c3", "m-3")];
      // there: d4 added, a1's name changed
      const theirs = [{ ...k("a1", "m-1", { api_key_set: true }), name: "Main" },
                      k("b2", "m-2"), k("d4", "m-4", { api_key_set: true })];
      assert.deepEqual(remergeWholeList("llm_backends", base, mine, theirs),
        [{ ...k("a1", "m-1b"), name: "Main" },
         k("d4", "m-4", { api_key_set: true }), k("c3", "m-3")]);
    },
    "a moved AI routing merges per task": () => {
      const base = { assistant: "a1", categorize: "a1" };
      const mine = { assistant: "c3", categorize: "a1" };       // assistant moved here
      const theirs = { assistant: "a1", categorize: "d4", vision: "d4" };
      assert.deepEqual(remergeWholeList("llm_roles", base, mine, theirs),
                       { assistant: "c3", categorize: "d4", vision: "d4" });
      // a task unrouted here stays unrouted
      assert.deepEqual(remergeWholeList("llm_roles", base, { categorize: "a1" },
                                        theirs),
                       { categorize: "d4", vision: "d4" });
    },
    "a moved asset list merges by name and keeps the other device's asset": () => {
      const a = (name, value, as_of = null) => ({ name, value, kind: "property", as_of });
      const base = [a("House", 300000), a("Car", 9000)];
      const mine = [a("House", 310000), a("Car", 9000)];
      const theirs = [a("House", 300000), a("Car", 8500, "2026-01-01"),
                      a("Boat", 4000)];
      assert.deepEqual(remergeWholeList("manual_assets", base, mine, theirs),
        [a("House", 310000), a("Car", 8500, "2026-01-01"), a("Boat", 4000)]);
    },
    "a moved reply is recognised in both clients' error shapes": () => {
      const body = { error: "settings_moved", keys: ["savings_goals"],
                     list_versions: { savings_goals: "g2" } };
      // the phone: a status and a structured body
      assert.deepEqual(settingsMovedOf({ status: 409, body }),
                       { keys: ["savings_goals"] });
      // the web: "409: <json>" in the message, the fields mirrored at the
      // top and nested under detail
      assert.deepEqual(
        settingsMovedOf(new Error("409: " + JSON.stringify({ ...body, detail: body }))),
        { keys: ["savings_goals"] });
      assert.equal(settingsMovedOf(new Error("409: " + JSON.stringify(
        { detail: "device limit" }))), null);
      assert.equal(settingsMovedOf({ status: 409, body: { error: "other" } }), null);
      assert.equal(settingsMovedOf(new Error("400: {\"detail\":\"bad\"}")), null);
      assert.equal(settingsMovedOf(null), null);
    },
    "a moved list is re-merged onto the fresh list and retried once": async () => {
      const g = (name, target) =>
        ({ name, target, monthly_plan: 0, tokens: [], start_balance: 0 });
      const basis = { savings_goals: [g("Boat", 900)],
                      list_versions: { savings_goals: "v1", custom_buckets: "c1" } };
      const fresh = { savings_goals: [g("Boat", 900), g("Pond", 150)],
                      list_versions: { savings_goals: "v2", custom_buckets: "c1" } };
      const sent = [];
      const moved = Object.assign(new Error("moved"), { status: 409,
        body: { error: "settings_moved", keys: ["savings_goals"] } });
      const send = async (b) => {
        sent.push(b);
        if (sent.length === 1) throw moved;
        return { ok: true, list_versions: { savings_goals: "v3" } };
      };
      let reads = 0;
      const read = async () => { reads += 1; return fresh; };
      const out = await saveWholeLists(
        { savings_goals: [g("Boat", 1200)], theme: "dark" }, basis, send, read);
      assert.deepEqual(out, { ok: true, list_versions: { savings_goals: "v3" } });
      assert.equal(reads, 1);
      assert.deepEqual(sent[0].list_versions, { savings_goals: "v1" });
      assert.deepEqual(sent[1], {
        savings_goals: [g("Boat", 1200), g("Pond", 150)], theme: "dark",
        list_versions: { savings_goals: "v2" } });
    },
    "a second moved reply stops with the reload sentence and the latest view": async () => {
      const basis = { custom_buckets: [], list_versions: { custom_buckets: "c1" } };
      const latest = { custom_buckets: [{ name: "Lunch" }],
                       list_versions: { custom_buckets: "c3" } };
      let sends = 0, reads = 0;
      const send = async () => {
        sends += 1;
        throw Object.assign(new Error("moved"), { status: 409,
          body: { error: "settings_moved", keys: ["custom_buckets"] } });
      };
      const read = async () => {
        reads += 1;
        return reads === 1 ? { custom_buckets: [],
                               list_versions: { custom_buckets: "c2" } } : latest;
      };
      await assert.rejects(
        saveWholeLists({ custom_buckets: [{ name: "Snacks" }] }, basis, send, read),
        (e) => e.message === SETTINGS_MOVED_TEXT && e.latest === latest);
      assert.equal(sends, 2);
      assert.equal(SETTINGS_MOVED_TEXT,
        "Changed on another device. Reloaded the latest; make your change again.");
    },
    // A form whose read has not answered (or failed) holds defaults: an
    // empty backend list, an empty routing. Sent, they would replace what
    // is stored. No view under the save means nothing was read.
    "a whole-list save with no settings view under it is refused unsent": async () => {
      for (const basis of [undefined, null]) {
        const sent = [];
        const send = async (b) => { sent.push(b); return { ok: true }; };
        const read = async () => { throw new Error("must not re-read"); };
        await assert.rejects(
          saveWholeLists({ llm_backends: [], llm_roles: {},
                           taxdocs_allow_remote_llm: "" }, basis, send, read),
          (e) => e instanceof SettingsUnreadError
            && e.message === SAVE_WAITS_FAILED);
        assert.deepEqual(sent, [], String(basis));
        // a body with no whole list in it needs no view
        assert.deepEqual(await saveWholeLists({ theme: "dark" }, basis, send,
                                              read), { ok: true });
        assert.deepEqual(sent, [{ theme: "dark" }]);
      }
    },
    // The planner's Bills and the savings left over are worked out from
    // reads; a read that has not answered is not $0 bills, and a plan
    // worked out from it must never be shown or saved.
    "a figure waits for every input it is worked out from": () => {
      assert.equal(UNKNOWN_FIGURE, "·");
      assert.equal(inputPhase({ data: { avg_bills: 0 }, isError: false }), "ready");
      // a failed refetch keeps the answer it had
      assert.equal(inputPhase({ data: { avg_bills: 0 }, isError: true }), "ready");
      assert.equal(inputPhase({ data: undefined, isError: false }), "loading");
      assert.equal(inputPhase({ data: undefined, isError: true }), "failed");
      assert.equal(plannerWaits("ready", "ready"), null);
      assert.equal(plannerWaits("loading", "ready"), SAVE_WAITS_LOADING);
      assert.equal(plannerWaits("ready", "loading"), SAVE_WAITS_LOADING);
      assert.equal(plannerWaits("failed", "ready"), PLANNER_BILLS_FAILED);
      assert.equal(plannerWaits("failed", "failed"), PLANNER_BILLS_FAILED);
      assert.equal(plannerWaits("loading", "failed"), PLANNER_HISTORY_FAILED);
      assert.equal(plannerWaits("ready", "failed"), PLANNER_HISTORY_FAILED);
      assert.equal(PLANNER_BILLS_FAILED,
        "Couldn't load your bills, so the plan can't be worked out or saved yet.");
      assert.equal(PLANNER_HISTORY_FAILED, "Couldn't load your spending "
        + "history, so the plan can't be filled in or saved yet.");
    },
    // One failed read says what failed and why, the same way on both
    // clients — never "Couldn't reach the server" for an answer that came
    // back, never "Offline" for a 5xx, a 403 or a 429.
    "a failed read names its true cause": () => {
      const line = (e, c = "web") => failLine(e, "bills", c);
      assert.deepEqual(line(new TypeError("Failed to fetch")),
        { text: "Couldn't load bills — can't reach the server.", retry: true });
      assert.deepEqual(line(new TypeError("Network request failed"), "phone"),
        { text: "Couldn't load bills — you're offline.", retry: true });
      for (const e of [webErr(500, '{"detail":"boom"}'), phoneErr(500, "boom"),
                       phoneErr(502, "bad gateway")])
        for (const c of ["web", "phone"])
          assert.deepEqual(line(e, c), { text: "Couldn't load bills — "
            + "something went wrong on the server.", retry: true });
      // the proxy answering while the app restarts; a 503 shedding load
      assert.deepEqual(line(webErr(502, restartingText(null),
                                   { status: 502, restarting: true,
                                     retryAfter: null })),
        { text: "The server is restarting — try again in a moment.",
          retry: true });
      assert.deepEqual(line(phoneErr(503, "database full",
                                     { retryAfter: "5" }), "phone"),
        { text: "The server is busy — try again in 5 seconds.", retry: true });
      assert.equal(busyText("200"), "The server is busy — try again in a moment.");
      for (const e of [webErr(429, '{"detail":"slow down"}'),
                       phoneErr(429, "slow down")])
        assert.deepEqual(line(e, "phone"), { text:
          "Too many requests — wait a minute, then try again.", retry: true });
      // a 403: the server's sentence, else who may see it
      assert.deepEqual(line(webErr(403, '{"detail":"Viewers can\'t open this"}')),
        { text: "Couldn't load bills — viewers can't open this.", retry: false });
      assert.deepEqual(line(phoneErr(403, "Viewers can't open this"), "phone"),
        { text: "Couldn't load bills — viewers can't open this.", retry: false });
      assert.deepEqual(line(webErr(403, "")),
        { text: "Your role doesn't include bills.", retry: false });
      assert.deepEqual(line(webErr(409, '{"detail":"A sync is running."}')),
        { text: "Couldn't load bills — a sync is running.", retry: true });
      assert.deepEqual(line(webErr(400, '{"detail":"bad month"}')),
        { text: "Couldn't load bills — bad month.", retry: false });
      // an initialism keeps its capitals; a markup page is never read out
      assert.deepEqual(line(phoneErr(422, "CSV header missing")),
        { text: "Couldn't load bills — CSV header missing.", retry: false });
      assert.deepEqual(line(webErr(404, "<html>nope</html>")),
        { text: "Couldn't load bills — the server refused the request (404).",
          retry: false });
      assert.deepEqual(line(Object.assign(new Error("not signed in"),
                                          { status: 401 })),
        { text: "Couldn't load bills — sign in again.", retry: false });
      assert.deepEqual(line(new Error("odd")),
        { text: "Couldn't load bills — something went wrong.", retry: true });
      assert.equal(failureStatus(webErr(503, '{"detail":"450 rows"}')), 503);
      assert.equal(failureStatus(new Error("the 404 page")), null);
      assert.equal(failureStatus(null), null);
    },
    // A write that failed says why in the app's words: the server's own
    // sentence when it sent one, else the cause — never "TypeError: …", a
    // bare status, a JSON body or a page.
    "a failed write names its cause, never the raw failure": () => {
      const off = new TypeError("Network request failed");
      assert.equal(failCause(off, "phone"), "You're offline.");
      assert.equal(failCause(new TypeError("Failed to fetch"), "web"),
                   "Can't reach the server.");
      assert.equal(noAnswer(off), true);
      assert.equal(noAnswer(phoneErr(500, "boom")), false);
      assert.equal(noAnswer(new Error("Pick an account first.")), false);
      // the server's sentence stands
      assert.equal(failCause(webErr(409, '{"detail":"A sync is running."}'),
                             "web"), null);
      assert.equal(failCause(phoneErr(502, "the bank did not answer"),
                             "phone"), null);
      // no sentence of its own: the cause, never the status or the body
      for (const c of ["web", "phone"]) {
        assert.equal(failCause(webErr(500, ""), c),
                     "Something went wrong on the server.");
        assert.equal(failCause(webErr(500, "<html>oops</html>"), c),
                     "Something went wrong on the server.");
        assert.equal(failCause(webErr(422, '{"detail":[]}'), c),
                     "The server refused the request.");
        assert.equal(failCause(webErr(429, ""), c),
                     "Too many requests — wait a minute, then try again.");
        assert.equal(failCause(phoneErr(408, ""), c),
                     "The server took too long to answer.");
      }
      assert.equal(failCause(phoneErr(503, "full", { retryAfter: "5" }), "phone"),
                   "The server is busy — try again in 5 seconds.");
      // a client's own sentence is not a request failure
      assert.equal(failCause(new Error("Pick an account first."), "web"), null);
      // the failed line takes the write's verb
      assert.deepEqual(failLine(off, "your report", "phone", "send"),
        { text: "Couldn't send your report — you're offline.", retry: true });
      assert.deepEqual(failLine(webErr(403, ""), "your report", "web", "send"),
        { text: "Your role doesn't allow that.", retry: false });
    },
    // On a device the phone's fetch is expo/fetch, and a request with no
    // answer rejects with its FetchError: a plain Error (name "Error")
    // reading "fetch failed: <the native cause>", never a TypeError. That
    // is no answer too; a request its own signal cancelled is not.
    "expo/fetch's no-answer failure is no answer, its cancel says nothing": () => {
      class FetchError extends Error {
        constructor(m) { super(`fetch failed: ${m}`); }
      }
      const off = new FetchError("Unable to resolve host \"x.test\"");
      assert.equal(off.name, "Error");
      assert.equal(noAnswer(off), true);
      assert.equal(noAnswer(new FetchError("timeout")), true);
      assert.equal(failCause(off, "phone"), "You're offline.");
      assert.equal(failCause(off, "web"), "Can't reach the server.");
      assert.deepEqual(failLine(off, "your report", "phone", "send"),
        { text: "Couldn't send your report — you're offline.", retry: true });
      assert.equal(staleLine(off, 0, "phone", 0),
        "Offline — showing what was here before.");
      for (const m of ["The operation was aborted.",
                       "Fetch request has been canceled"]) {
        const cancelled = new FetchError(m);
        assert.equal(noAnswer(cancelled), false, m);
        assert.equal(failCause(cancelled, "phone"), null, m);
      }
      // the server answered with a redirect the request refused
      assert.equal(noAnswer(new FetchError(
        "Redirect is not allowed when redirect mode is 'error'")), false);
      // an app's own sentence is never mistaken for one
      assert.equal(noAnswer(new Error("fetch failed")), false);
      assert.equal(noAnswer(new Error("Could not fetch failed rows.")), false);
      assert.equal(noAnswer(Object.assign(new Error("fetch failed: x"),
                                          { status: 500 })), false);
    },
    // "Connect or import" is offered only to a household with no accounts
    // at all — known from an answer, never from a read that failed or has
    // not answered (that is not "nothing linked").
    "an empty screen offers connecting only when nothing is linked": () => {
      assert.equal(noAccountsYet({ data: { accounts: [] } }), true);
      assert.equal(noAccountsYet({ data: { accounts: [{ id: "a" }] } }), false);
      assert.equal(noAccountsYet({ data: undefined }), false);
      assert.equal(noAccountsYet({}), false);
      assert.equal(EMPTY_LINES.filtered, "Nothing matches these filters.");
      assert.equal(EMPTY_LINES.noAccounts, "No accounts linked yet.");
      // one short line each: a sentence, never a paragraph
      for (const [k, v] of Object.entries(EMPTY_LINES))
        assert.ok(v.length <= 60 && !v.includes("\n"), k);
    },
    "the stale line says when the copy on screen is from": () => {
      const now = new Date(2031, 2, 3, 15, 0).getTime();
      const at = (...a) => new Date(...a).getTime();
      const off = new TypeError("Network request failed");
      const down = phoneErr(500, "boom");
      assert.equal(shownAt(at(2031, 2, 3, 9, 41), now), "9:41 AM");
      assert.equal(shownAt(at(2031, 2, 3, 0, 5), now), "12:05 AM");
      assert.equal(shownAt(at(2031, 2, 3, 12, 0), now), "12:00 PM");
      assert.equal(shownAt(at(2031, 2, 1, 21, 5), now), "Mar 1");
      assert.equal(shownAt(at(2030, 11, 30, 8, 0), now), "Dec 30, 2030");
      assert.equal(staleLine(off, at(2031, 2, 3, 9, 41), "phone", now),
        "Offline — showing what was here at 9:41 AM.");
      // only the phone calls no answer "offline"; a 5xx is never offline
      assert.equal(staleLine(off, at(2031, 2, 3, 9, 41), "web", now),
        "Couldn't refresh — showing what was here at 9:41 AM.");
      assert.equal(staleLine(down, at(2031, 2, 3, 9, 41), "phone", now),
        "Couldn't refresh — showing what was here at 9:41 AM.");
      assert.equal(staleLine(phoneErr(429, "slow"), at(2031, 2, 1, 9, 0),
                             "phone", now),
        "Couldn't refresh — showing what was here at Mar 1.");
      assert.equal(staleLine(down, 0, "web", now),
        "Couldn't refresh — showing what was here before.");
    },
    // One retry rule on both clients: a 429 retried is another strike
    // toward the address ban, and a 403 asked again only delays the answer.
    "a failed read is asked again only when it can heal": () => {
      const off = new TypeError("Network request failed");
      assert.equal(retryRead(0, off), true);
      assert.equal(retryRead(2, off), true);
      assert.equal(retryRead(3, off), false);
      for (const e of [webErr(500, "x"), phoneErr(503, "busy"),
                       phoneErr(408, "timeout")])
        assert.equal(retryRead(0, e), true);
      for (const e of [webErr(429, "x"), phoneErr(429, "x"), phoneErr(403, "x"),
                       webErr(404, "x"), Object.assign(new Error("gone"),
                                                       { status: 401 })])
        assert.equal(retryRead(0, e), false);
      assert.deepEqual([0, 1, 2, 3, 6].map(retryReadDelay),
                       [1000, 2000, 4000, 8000, 8000]);
    },
    // A read the server or the network never answers must end: without a
    // limit the screen sits on "Loading…" until the page is reloaded.
    "a read that never answers is abandoned as no answer": async () => {
      assert.equal(READ_LIMIT_MS, 30_000);
      for (const m of [undefined, null, "", "GET", "get", "HEAD"])
        assert.equal(isRead(m), true, String(m));
      for (const m of ["POST", "put", "PATCH", "DELETE"])
        assert.equal(isRead(m), false, m);
      // a server that never answers: the request ends only when its
      // signal is aborted, the way fetch does
      const never = (signal) => new Promise((_, reject) => {
        signal.addEventListener("abort", () => reject(
          Object.assign(new Error("aborted"), { name: "AbortError" })));
      });
      const e = await fetchRead(never, "GET", undefined, 5)
        .then(() => null, (x) => x);
      assert.ok(e, "the read was abandoned");
      // the same failure as a dead connection, in every reader's eyes:
      // not a status, not a sign-out, not a cancelled request
      assert.equal(timedOut(e), true);
      assert.equal(noAnswer(e), true);
      assert.equal(failureStatus(e), null);
      assert.notEqual(e.name, "AbortError");
      assert.equal(failLine(e, "bills", "web").text,
        "Couldn't load bills — can't reach the server.");
      assert.deepEqual(failLine(e, "bills", "phone"),
        { text: "Couldn't load bills — you're offline.", retry: true });
      assert.equal(timedOut(new TypeError("Network request failed")), false);
      assert.equal(timedOut(null), false);
    },
    "a write is never abandoned by the client": async () => {
      for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
        const outer = new AbortController();
        let got = "unset";
        let finish;
        const slow = (signal) => {
          got = signal;
          return new Promise((resolve) => { finish = resolve; });
        };
        const p = fetchRead(slow, method, outer.signal, 5);
        await new Promise((r) => setTimeout(r, 25));
        // the caller's signal goes through untouched: no clock on it
        assert.equal(got, outer.signal, method);
        assert.equal(outer.signal.aborted, false, method);
        const answer = { ok: true };
        finish(answer);
        assert.equal(await p, answer, method);
      }
    },
    "an answered read is left alone, and the caller's signal still ends it": async () => {
      // answered inside the limit: the clock stops, so a body that takes
      // longer than the limit to arrive is not cut off
      let seen;
      const answer = {};
      const r = await fetchRead(async (signal) => { seen = signal; return answer; },
                                "GET", undefined, 5);
      assert.equal(r, answer);
      await new Promise((res) => setTimeout(res, 25));
      assert.equal(seen.aborted, false, "the limit ends at the first answer");
      // the caller's signal (an account switch) still cancels the body
      // while it is being read, and stops being watched once it is read
      const outer = new AbortController();
      let a, b;
      const ra = await fetchRead(async (s) => { a = s; return {}; }, "GET",
                                 outer.signal, 5);
      const rb = await fetchRead(async (s) => { b = s; return {}; }, "GET",
                                 outer.signal, 5);
      readFinished(rb);
      readFinished(rb);
      readFinished(null);
      outer.abort();
      assert.equal(a.aborted, true);
      assert.equal(b.aborted, false);
      readFinished(ra);
      // a caller's abort before any answer is the caller's abort, never
      // reported as a timeout
      const early = new AbortController();
      const hung = fetchRead((signal) => new Promise((_, reject) => {
        signal.addEventListener("abort", () => reject(
          Object.assign(new Error("aborted"), { name: "AbortError" })));
      }), "GET", early.signal, 10_000).then(() => null, (x) => x);
      early.abort();
      const cancelled = await hung;
      assert.equal(cancelled.name, "AbortError");
      assert.equal(timedOut(cancelled), false);
      // already aborted when the read starts: it starts aborted
      const gone = new AbortController();
      gone.abort();
      let g;
      readFinished(await fetchRead(async (s) => { g = s; return {}; }, "GET",
                                   gone.signal, 5));
      assert.equal(g.aborted, true);
      // any other failure passes through as it was
      const boom = new TypeError("Network request failed");
      assert.equal(await fetchRead(async () => { throw boom; }, "GET",
                                   undefined, 5).catch((x) => x), boom);
    },
    // Three more waits of the full limit would be two silent minutes.
    "a read abandoned at the limit is asked again once, not three times": async () => {
      const e = await fetchRead((signal) => new Promise((_, reject) => {
        signal.addEventListener("abort", () => reject(new Error("aborted")));
      }), "GET", undefined, 1).catch((x) => x);
      assert.equal(retryRead(0, e), true);
      assert.equal(retryRead(1, e), false);
      assert.equal(retryRead(2, e), false);
      // a dead connection keeps its three
      assert.equal(retryRead(2, new TypeError("Network request failed")), true);
    },
    // A figure comes only from an answer on screen: never `?? 0` for a
    // read that has not answered, or failed with nothing kept.
    "a figure is known only from an answer, and greyed when it is not current": () => {
      const n = (d) => d.n;
      assert.equal(knownOr({ data: { n: 0 } }, n), 0);   // a real zero
      assert.equal(knownOr({ data: undefined }, n), null);
      assert.equal(knownOr({ data: {} }, n), null);
      // the previous filter's answer standing in, and a failed refetch's
      // last answer, are printed — greyed
      assert.equal(knownOr({ data: { n: 4 }, isPlaceholderData: true }, n), 4);
      assert.equal(knownOr({ data: { n: 7 }, isError: true }, n), 7);
      assert.equal(figureGreyed({ data: { n: 7 }, isError: true }), true);
      assert.equal(figureGreyed({ data: { n: 4 }, isError: false,
                                  isPlaceholderData: true }), true);
      assert.equal(figureGreyed({ data: { n: 7 }, isError: false }), false);
      assert.equal(figureGreyed({ data: undefined, isError: true }), false);
    },
    "a form's save waits, and says why, until it was seeded from a read": () => {
      assert.equal(saveWaits(true, false), null);
      assert.equal(saveWaits(true, true), null);
      assert.equal(saveWaits(false, false), SAVE_WAITS_LOADING);
      assert.equal(saveWaits(false, true), SAVE_WAITS_FAILED);
      assert.equal(SAVE_WAITS_LOADING, "Loading…");
      assert.equal(SAVE_WAITS_FAILED,
        "Couldn't load what's saved, so nothing can be saved over it yet.");
    },
    "a save to a server without versions sends the body as it is and never retries": async () => {
      const sent = [];
      const err = new Error("409: {\"detail\":\"something else\"}");
      const send = async (b) => { sent.push(b); throw err; };
      const read = async () => { throw new Error("must not re-read"); };
      await assert.rejects(
        saveWholeLists({ savings_goals: [] }, { savings_goals: [] }, send, read),
        (e) => e === err);
      assert.deepEqual(sent, [{ savings_goals: [] }]);
      // with versions, any other refusal is handed back untouched too
      const sent2 = [];
      await assert.rejects(saveWholeLists({ savings_goals: [] },
        { list_versions: { savings_goals: "v1" } },
        async (b) => { sent2.push(b); throw err; }, read), (e) => e === err);
      assert.equal(sent2.length, 1);
    },
  };
}
