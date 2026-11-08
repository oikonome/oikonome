// The email schedule saves a cadence whole, so every client lays its edit
// over the schedule stored NOW rather than over the copy it rendered from.
// Runs under plain node (v22.6+):
//
//   node --experimental-strip-types scripts/schedule-tests.mjs
//
// The web keeps the same rule in webapp/src/moneymath.ts (rebaseSchedule);
// both copies run the same cases here so they cannot drift apart.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { rebaseSched } from "../src/lib/schedule.ts";
import { rebaseSchedule } from "../../webapp/src/moneymath.ts";

const twins = [["mobile", rebaseSched], ["web", rebaseSchedule]];

for (const [side, rebase] of twins) {
  test(`${side}: a daily edit keeps the reports and channels stored since`, () => {
    // another device turned the monthly report off and daily SMS on after
    // this screen rendered; this tap only moves the send hour
    const base = { daily: { on: true, push: false, hour: 7, summary: false } };
    const live = { daily: { on: true, hour: 7, sms: true },
                   monthly: { on: false, hour: 8 },
                   yearly: { on: true, hour: 7 } };
    const sent = rebase({ daily: { ...base.daily, hour: 6 } }, base, live,
                        "daily");
    assert.deepEqual(sent.monthly, live.monthly);
    assert.deepEqual(sent.yearly, live.yearly);
    assert.equal(sent.daily.hour, 6);
    assert.equal(sent.daily.sms, true);
  });

  test(`${side}: the edited cadence is always in the body`, () => {
    const base = { daily: { on: true, push: false, hour: 7, summary: false } };
    const sent = rebase({ daily: { ...base.daily } }, base, {}, "daily");
    assert.equal(sent.daily.on, true);
  });
}

test("the web wizard's email step reads the stored schedule before it saves", () => {
  // posting a schedule built from the settings cache would put back a
  // report or channel another device changed since the step rendered
  const src = readFileSync(
    new URL("../../webapp/src/pages/Welcome.tsx", import.meta.url), "utf8");
  const at = src.indexOf("function EmailPushStep");
  assert.ok(at >= 0, "EmailPushStep not found");
  const body = src.slice(at, src.indexOf("\nfunction ", at + 10));
  const m = body.indexOf("const save = useMutation");
  assert.ok(m >= 0, "EmailPushStep save mutation not found");
  const save = body.slice(m, body.indexOf("scope: SETTINGS_SAVES", m));
  assert.ok(!/s\.data\?\.email_schedule/.test(save),
            "the save must not spread the cached schedule");
  assert.ok(/await api\.settings\(\)/.test(save),
            "the save must read the stored schedule first");
  assert.ok(/mutationFn: async \(patch/.test(save) && /\.\.\.was, \.\.\.patch/.test(save),
            "the save must send only the fields the tap changed, over the stored daily");
});
