"""A restore ZIP's income scenarios, savings goals and hand-entered assets
are held to the settings door's own rules before they land.

The settings blob goes in verbatim, and Today, the forecast, Settings and
Net Worth read these lists straight into float() and arithmetic: a string
where a list belongs, a NaN take-home or an over-long list would make those
pages answer 500 (or carry NaN into a total), with no door the household
could use to take it back out. Unusable entries are dropped with a note,
the lists are cut at the door's caps, and usable entries come through."""

import csv
import io
import json
import math
import unittest
import zipfile

from oikonome.engine import budget, forecast, reporting, savings
from oikonome.sync import restore

from .util import TODAY, make_db, write_config


def _zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        # allow_nan: the archive is hostile input, and a hand-edited one
        # can carry a bare NaN that Python's json reader accepts
        w.writerow({"config": json.dumps(config, allow_nan=True)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class ARestoredConfigStaysReadable(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _restore(self, config):
        out = restore.restore_zip(self.conn, _zip(config))
        return budget.load_config(self.conn), " ".join(out.get("_notes") or [])

    def _pages_read_it(self, cfg):
        budget.active_monthly_income(cfg)
        savings.progress(self.conn, cfg, TODAY)
        forecast.build(self.conn, TODAY)
        nw = reporting.compute_networth(self.conn)
        self.assertTrue(math.isfinite(nw["property_net"]))

    def test_lists_that_are_not_lists_are_left_out(self):
        cfg, notes = self._restore({"income_scenarios": "lots",
                                    "active_scenario": "lots",
                                    "savings_goals": 7,
                                    "manual_assets": {"house": 1}})
        for k in ("income_scenarios", "active_scenario", "savings_goals",
                  "manual_assets"):
            self.assertNotIn(k, cfg)
        self.assertIn("income scenarios were not a list", notes)
        self.assertIn("savings goals were not a list", notes)
        self._pages_read_it(cfg)

    def test_a_non_finite_or_unusable_amount_drops_only_its_entry(self):
        cfg, notes = self._restore({
            "income_scenarios": [
                {"name": "broken", "take_home": float("nan")},
                {"name": "words", "take_home": "plenty"},
                {"name": "", "take_home": 100},
                "not an object",
                {"name": "job", "take_home": "2,400", "cadence": "Biweekly"}],
            "active_scenario": "broken",
            "income_biweekly_saving": "lots",
            "savings_goals": [
                {"name": "Trip", "target": float("inf")},
                {"name": "Letters", "monthly_plan": 50, "tokens": "abc"},
                {"name": "Soon", "monthly_plan": 50, "target_date": "someday"},
                {"name": "House", "target": "20000", "monthly_plan": 300}],
            "manual_assets": [
                {"name": "Car", "value": float("nan")},
                {"name": "Boat", "value": 1e300},
                {"name": "Shed", "value": 900, "as_of": "yesterday"},
                {"name": "Home", "value": "250,000", "kind": "property"}]})
        self.assertEqual(cfg["income_scenarios"],
                         [{"name": "job", "take_home": 2400.0,
                           "cadence": "biweekly"}])
        self.assertEqual(cfg["active_scenario"], "job")
        self.assertNotIn("income_biweekly_saving", cfg)
        self.assertEqual([g["name"] for g in cfg["savings_goals"]], ["House"])
        self.assertEqual(cfg["savings_goals"][0]["target"], 20000.0)
        self.assertEqual([a["name"] for a in cfg["manual_assets"]], ["Home"])
        self.assertEqual(cfg["manual_assets"][0]["value"], 250000.0)
        self.assertIn("4 income scenarios", notes)
        self.assertIn("3 savings goals", notes)
        self.assertIn("3 hand-entered assets", notes)
        self.assertEqual(budget.active_monthly_income(cfg), 4800.0)
        self._pages_read_it(cfg)

    def test_lists_are_cut_at_the_doors_caps(self):
        cfg, notes = self._restore({
            "income_scenarios": [{"name": f"s{i}", "take_home": 100}
                                 for i in range(50)],
            "savings_goals": [{"name": f"g{i}", "monthly_plan": 10}
                              for i in range(50)],
            "manual_assets": [{"name": f"a{i}", "value": 10}
                              for i in range(50)]})
        self.assertEqual(len(cfg["income_scenarios"]), 20)
        self.assertEqual(len(cfg["savings_goals"]), 30)
        self.assertEqual(len(cfg["manual_assets"]), 30)
        self.assertIn("20 savings goals", notes)
        self._pages_read_it(cfg)

    def test_a_well_formed_config_comes_through(self):
        cfg, notes = self._restore({
            "income_scenarios": [{"name": "job", "take_home": 2000,
                                  "cadence": "monthly"}],
            "active_scenario": "job",
            "savings_goals": [{"name": "Fund", "target": 1000,
                               "monthly_plan": 100, "mode": "sweep",
                               "target_date": "2030-01-01"}]})
        self.assertEqual(cfg["active_scenario"], "job")
        self.assertEqual(cfg["savings_goals"][0]["mode"], "sweep")
        self.assertEqual(cfg["savings_goals"][0]["target_date"], "2030-01-01")
        self.assertNotIn("could not be used", notes)
        self._pages_read_it(cfg)


if __name__ == "__main__":
    unittest.main()
