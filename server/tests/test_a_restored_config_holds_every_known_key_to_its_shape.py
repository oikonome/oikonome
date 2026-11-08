"""A restore ZIP's bill switches, occurrence caps, dismissed hints, wizard
marks and retirement flags are held to the shape the settings doors write.

Each of these keys is written by its own door, which type-checks it; the
restore merge bypasses every door. A list where a dict belongs (or a number
where a list does) then lands verbatim, and Today, Budget, the forecast, the
Bills list and the daily email trip on it on every load, while the doors
that could repair it (bill save, bill rename, the wizard step save) trip on
the same value. A malformed key is dropped (or its malformed entries are)
with a note; a well-formed one comes through unchanged."""

import csv
import datetime as dt
import io
import json
import unittest
import zipfile

from oikonome.engine import budget, forecast
from oikonome.sync import restore

from .util import TODAY, add_bill, make_db, write_config


def _zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class ARestoredConfigKeepsEveryKeyInItsShape(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Netflix", 15.99)
        self.addCleanup(self.conn.close)

    def _restore(self, config):
        out = restore.restore_zip(self.conn, _zip(config))
        return budget.load_config(self.conn), " ".join(out.get("_notes") or [])

    def _pages_read_it(self, cfg):
        budget.month_status(self.conn, TODAY)
        budget.upcoming_bill_occurrences(
            self.conn, cfg, TODAY, TODAY + dt.timedelta(days=60))
        budget.suggest_budgets(self.conn, TODAY)
        forecast.build(self.conn, TODAY)
        # the doors that repair these keys read them the same way
        set(cfg.get("disabled_bills") or [])
        (cfg.get("occurrence_caps") or {}).pop("x", None)
        for k in ("dismissed_hints", "dismissed_health"):
            for v in (cfg.get(k) or {}).values():
                v.get("health")
        for k in ("wizard_steps", "biz_wizard_steps", "ret_wizard_steps"):
            dict(cfg.get(k) or {})

    def test_keys_of_the_wrong_kind_are_left_out(self):
        cfg, notes = self._restore({
            "occurrence_caps": ["Netflix"],
            "disabled_bills": 5,
            "dismissed_hints": ["Netflix"],
            "dismissed_health": "Netflix",
            "wizard_steps": ["connect"],
            "biz_wizard_steps": 3,
            "ret_wizard_steps": "done",
            "birthdate": ["1971-06-15"]})
        for k in ("occurrence_caps", "disabled_bills", "dismissed_hints",
                  "dismissed_health", "wizard_steps", "biz_wizard_steps",
                  "ret_wizard_steps", "birthdate"):
            self.assertNotIn(k, cfg)
        self.assertIn("not in the shape", notes)
        self._pages_read_it(cfg)

    def test_entries_of_the_wrong_kind_drop_only_themselves(self):
        cfg, notes = self._restore({
            "occurrence_caps": {"Netflix": 2, "Gym": "3", "Rent": 0,
                                "Water": "lots", "Power": [1]},
            "disabled_bills": [1, {}, "Netflix", "Netflix", None, "Gym"],
            "dismissed_hints": {"Netflix": {"kind": "fixed"}, "Gym": "x"},
            "dismissed_health": {"Netflix": 5,
                                 "Gym": {"health": "stale"}},
            "wizard_steps": {"connect": "done", "bills": "yes",
                             "nonsense": "done", "email": "skipped"},
            "retirement_saving": "yes",
            "dynamic_variable_budget": 0,
            "birthdate": "someday"})
        self.assertEqual(cfg["occurrence_caps"], {"Netflix": 2, "Gym": 3})
        self.assertEqual(cfg["disabled_bills"], ["Netflix", "Gym"])
        self.assertEqual(cfg["dismissed_hints"],
                         {"Netflix": {"kind": "fixed"}})
        self.assertEqual(cfg["dismissed_health"],
                         {"Gym": {"health": "stale"}})
        self.assertEqual(cfg["wizard_steps"],
                         {"connect": "done", "email": "skipped"})
        self.assertIs(cfg["retirement_saving"], True)
        self.assertIs(cfg["dynamic_variable_budget"], False)
        self.assertNotIn("birthdate", cfg)
        self.assertIn("not in the shape", notes)
        self._pages_read_it(cfg)

    def test_a_hint_dismissal_with_an_amount_that_is_not_a_number_is_left_out(self):
        """The Bills page compares a dismissal's amount with the live fit;
        an archive's "abc" or [1] there would answer 500 on every load."""
        from oikonome.engine import bills
        cfg, _ = self._restore({"dismissed_hints": {
            "Netflix": {"kind": "fixed", "cadence": "monthly",
                        "by_month_day": None, "amount": "abc"},
            "Gym": {"kind": "fixed", "amount": [1]},
            "Pool": {"kind": "fixed", "amount": 10 ** 400},
            "Water": {"kind": "fixed", "amount": 30.0}}})
        self.assertEqual(sorted(cfg["dismissed_hints"]), ["Water"])
        fit = {"kind": "fixed", "cadence": "monthly", "by_month_day": None,
               "amount": 15.99}
        self.assertFalse(bills.fit_matches_dismissal(
            fit, {"kind": "fixed", "cadence": "monthly",
                  "by_month_day": None, "amount": "abc"}))

    def test_an_archive_that_chose_the_summary_face_keeps_it(self):
        """The settings door stores the summary face as no key, and a new
        account is seeded on detail; restoring an export with no today_view
        onto that account must open on summary, as the household chose."""
        cfg = budget.load_config(self.conn)
        cfg["today_view"] = "detail"
        budget.save_config(self.conn, cfg)
        got, _ = self._restore({})
        self.assertNotIn("today_view", got)
        got, _ = self._restore({"today_view": "detail"})
        self.assertEqual(got.get("today_view"), "detail")

    def test_a_birthdate_not_in_the_past_is_left_out(self):
        cfg, _ = self._restore({"birthdate": "2999-01-01"})
        self.assertNotIn("birthdate", cfg)

    def test_a_well_formed_config_comes_through(self):
        good = {"occurrence_caps": {"Netflix": 1},
                "disabled_bills": ["Netflix"],
                "dismissed_hints": {"Netflix": {"kind": "fixed",
                                                "cadence": "MONTHLY"}},
                "dismissed_health": {"Netflix": {"health": "stale"}},
                "wizard_steps": {"connect": "done"},
                "biz_wizard_steps": {"what": "skipped"},
                "ret_wizard_steps": {"age": "done"},
                "retirement_saving": True,
                "birthdate": "1972-02-29"}
        cfg, notes = self._restore(good)
        for k, v in good.items():
            self.assertEqual(cfg[k], v, k)
        self.assertNotIn("not in the shape", notes)
        self._pages_read_it(cfg)


if __name__ == "__main__":
    unittest.main()
