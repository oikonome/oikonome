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

test("the phone wizard detects again once a reopened gate closes", () => {
  // detection that ran on a partial ledger must run again on the full one
  const src = read("../src/app/welcome.tsx");
  assert.ok(/if \(!backfillReady\) detectRan\.current = false/.test(src),
            "detectRan must reset when the gate closes again");
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
