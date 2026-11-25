"""Opening a setup wizard step only reads. The bills step sent the backfill
nudge on mount with every import complete, and the "find them for me" pane
queued a whole-ledger bill scan each time it opened — so a re-run wizard
that only looked still queued work. Now the nudge goes only while a bank
still owes history, and the scan runs on the person's ask (Continue on
"find them for me", "Scan again"), on web and phone."""

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class OpeningAStepWritesNothingTests(unittest.TestCase):
    def test_the_web_nudges_only_while_history_is_owed(self):
        gate = _read("webapp/src/pages/Welcome.tsx").split(
            "function BackfillGate")[1].split("const [reveal")[0]
        effect = gate.split("useEffect(() => {")[1]
        self.assertLess(effect.index("if (!waiting) return;"),
                        effect.index("api.onboardingBackfillNudge()"))

    def test_the_phone_nudges_only_while_history_is_owed(self):
        mw = _read("mobile/src/app/welcome.tsx")
        self.assertIn("if (!client || !backfillWaiting) return;\n"
                      "    client.onboardingBackfillNudge()", mw)

    def test_the_web_pane_scans_only_when_asked(self):
        wb = _read("webapp/src/components/WizardBills.tsx")
        self.assertIn("export function AutoBillsPane({ scan = 0 }", wb)
        self.assertRegex(wb, r"if \(q\.isSuccess && scan > ran\.current\)")
        w = _read("webapp/src/pages/Welcome.tsx")
        # the ask is Continue on the automatic answer
        self.assertIn('if (mode === "auto") setScanAsk((n) => n + 1);', w)
        self.assertIn("<AutoBillsPane scan={scanAsk} />", w)

    def test_the_phone_scans_only_when_asked(self):
        mw = _read("mobile/src/app/welcome.tsx")
        # no effect fires the finder because the pane is on screen
        for body in re.findall(r"useEffect\(\(\) => \{(.*?)\n  \}, \[",
                               mw, re.S):
            self.assertNotIn("detect.mutate()", body)
        self.assertIn('if (setupMode === "auto" && client && !viewer) '
                      "detect.mutate();", mw)


if __name__ == "__main__":
    unittest.main()
