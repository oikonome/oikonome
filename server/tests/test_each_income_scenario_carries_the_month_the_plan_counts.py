"""Each income scenario's monthly figure is the plan's, sent by the server.

The scenario card prints "Active now: $X/mo". Both clients used to work
that out with their own cadence table; a table that drifted from the one
the plan's income uses would state one month on the card and another in
the plan. The settings read now carries each scenario's `monthly`, from
`budget.scenario_monthly` — the conversion `active_monthly_income` itself
uses — and the clients print it.
"""

import unittest

from oikonome.engine import budget
from oikonome.web.api_settings import _settings_view


def _cfg(**kw):
    cfg = {"income_scenarios": [
        {"name": "full", "take_home": 2400.0, "cadence": "biweekly"},
        {"name": "part", "take_home": 600.0, "cadence": "weekly"},
        {"name": "lump", "take_home": 5000.0, "cadence": "monthly"}],
        "active_scenario": "full"}
    cfg.update(kw)
    return cfg


class TheSettingsReadCarriesEachScenariosMonth(unittest.TestCase):
    def test_every_scenario_has_the_plans_monthly_figure(self):
        cfg = _cfg()
        view = _settings_view(cfg)
        got = {s["name"]: s["monthly"] for s in view["income_scenarios"]}
        self.assertEqual(got, {
            s["name"]: budget.scenario_monthly(s)
            for s in cfg["income_scenarios"]})
        # the active one's figure IS the plan's income
        self.assertEqual(got["full"], budget.active_monthly_income(cfg))

    def test_the_figure_is_not_stored_back(self):
        cfg = _cfg()
        _settings_view(cfg)
        self.assertTrue(all("monthly" not in s
                            for s in cfg["income_scenarios"]))

    def test_a_stored_take_home_that_is_not_a_number_reads_as_no_figure(self):
        cfg = _cfg()
        cfg["income_scenarios"][1]["take_home"] = "abc"
        view = _settings_view(cfg)
        got = {s["name"]: s["monthly"] for s in view["income_scenarios"]}
        self.assertIsNone(got["part"])
        self.assertIsNotNone(got["full"])

    def test_the_legacy_pair_reads_with_its_figures_too(self):
        cfg = {"income_biweekly_saving": 2000.0,
               "income_biweekly_not_saving": 2300.0,
               "retirement_saving": True}
        view = _settings_view(cfg)
        self.assertEqual(
            [s["monthly"] for s in view["income_scenarios"]],
            [budget.scenario_monthly({"take_home": 2000.0,
                                      "cadence": "biweekly"}),
             budget.scenario_monthly({"take_home": 2300.0,
                                      "cadence": "biweekly"})])


if __name__ == "__main__":
    unittest.main()
