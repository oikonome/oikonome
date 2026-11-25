// The setup wizard's history gate: it holds the bills step until every
// connection has pulled its history, and fails open only when the import
// status could never be read. Runs under plain node (v22.6+):
//
//   node --experimental-strip-types scripts/wizard-gate-tests.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { backfillFailsOpen } from "../src/components/wizard-logic.ts";

test("a failed poll with a status already read keeps the gate shut", () => {
  // a background refetch failing keeps the last answer, which still says
  // history is arriving: the gate must keep waiting on it
  assert.equal(backfillFailsOpen({ isError: true,
    data: { all_ready: false } }), false);
});

test("an import status that never answered opens the gate", () => {
  assert.equal(backfillFailsOpen({ isError: true, data: undefined }), true);
  assert.equal(backfillFailsOpen({ isError: false, data: undefined }), false);
});

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("the phone wizard opens the gate on an error only with nothing read", () => {
  const src = read("../src/app/welcome.tsx");
  const at = src.indexOf("const backfillReady =");
  assert.ok(at >= 0);
  const expr = src.slice(at, src.indexOf(";", at));
  assert.ok(!/backfill\.isError\s*\|\|/.test(expr),
            "a bare isError must not open the gate");
  assert.ok(/backfillFailsOpen\(backfill\)/.test(expr));
});

test("the phone wizard scans only behind an open gate, and only when asked", () => {
  // detection on a partial ledger finds almost nothing: the scan is the
  // question's Continue, and that button exists only once the gate opens
  const src = read("../src/app/welcome.tsx");
  assert.ok(/primary: !backfillReady \? null/.test(src),
            "the bills step has no Continue while history is arriving");
  assert.ok(/if \(setupMode === "auto" && client && !viewer\) detect\.mutate\(\);/
              .test(src), "Continue on find-them-for-me is the scan");
});

test("the web wizard opens its gates on an error only with nothing read", () => {
  const src = read("../../webapp/src/pages/Welcome.tsx");
  assert.ok(!/q\.isError \|\|/.test(src), "BackfillGate opens on a bare isError");
  const at = src.indexOf("const backfillReady =");
  assert.ok(at >= 0);
  const expr = src.slice(at, src.indexOf(";", at));
  assert.ok(!/backfill\.isError\s*\|\|/.test(expr),
            "backfillReady opens on a bare isError");
});

test("both wizards read who saved the plan, never budgets_set alone", () => {
  // the server stores its first-run seed once a month has closed, so
  // budgets_set holds with nothing saved: the seeding choice and Continue
  // read budgets_saved_by_user (planSaved / budgetsStepKept)
  const phone = read("../src/app/welcome.tsx");
  assert.ok(/saved: plannerSaved \|\| planSaved\(ob\.data\)/.test(phone),
            "the phone planner seeds from planSaved");
  assert.ok(/disabled: !d \|\| !budgetsStepKept\(d\) \|\| budgetDirty/
              .test(phone), "the phone's Continue reads budgetsStepKept");
  assert.ok(!/!d\?\.budgets_set/.test(phone));
  const web = read("../../webapp/src/pages/Welcome.tsx");
  assert.ok(/disabled: !d \|\| !budgetsStepKept\(d\) \|\| budgetDirty/
              .test(web), "the web's Continue reads budgetsStepKept");
  assert.ok(!/!d\?\.budgets_set/.test(web));
  const planner = read("../../webapp/src/components/BudgetPlanner.tsx");
  assert.ok(/: planSaved\(od\);/.test(planner),
            "the web planner seeds from planSaved");
  assert.ok(!/od\.budgets_set/.test(planner));
});
