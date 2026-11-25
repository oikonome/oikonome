"""Two settings saves at once lose nothing: different keys both land, and
the same key ends as one of the two values (specs/invariants-and-fuzzing.md,
section 3, third scenario; D2, the later save wins).

Every save is a read-modify-write of the whole settings document under
its row lock (budget.config_txn). Two tabs saving at the same moment must
therefore end as they would one after the other: a key only one of them
changed is never written back to its old value by the other.
"""

from __future__ import annotations

import unittest

from oikonome.engine import budget
from oikonome.web import api, api_settings  # noqa: F401  (api first: it loads the app)
from oikonome.web.fields import Fields

from .concurrency import ScenarioCase, Scenario, Seam

SETTINGS_ROW = Seam("the settings row", rank=5)
KEYS = ("food_monthly", "timezone")


def _save(body):
    def save(hh, ctx):
        api_settings.settings_set(user=hh.user, body=Fields(dict(body)))
    return save


def _digest(hh, ctx):
    cfg = budget.load_config(hh.conn)
    return tuple((k, cfg.get(k)) for k in KEYS)


class SettingsSavedAtOnce(ScenarioCase, unittest.TestCase):
    def test_saves_of_different_keys_both_land(self):
        self.check(Scenario(
            "saves of different keys",
            setup=lambda hh: None,
            actors={"A": _save({"food_monthly": 640}),
                    "B": _save({"timezone": "America/Denver"})},
            seams=(SETTINGS_ROW,), digest=_digest,
            doors=frozenset({"A", "B"})))

    def test_saves_of_one_key_end_as_one_of_them(self):
        self.check(Scenario(
            "saves of one key",
            setup=lambda hh: None,
            actors={"A": _save({"food_monthly": 640}),
                    "B": _save({"food_monthly": 710})},
            seams=(SETTINGS_ROW,), digest=_digest,
            doors=frozenset({"A", "B"})))


if __name__ == "__main__":
    unittest.main()
