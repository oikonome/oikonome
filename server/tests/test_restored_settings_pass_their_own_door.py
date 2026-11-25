"""Every restored setting passes its own door's validation
(specs/invariants-and-fuzzing.md, R5).

Property: a household (tests/money_households.py, with a plan) saves a
generated settings document through `POST /api/settings`, is exported,
and the archive is restored into a second household. Posting each key of
the restored document back to the same door, exactly as the door's read
(`GET /api/settings`) shows it, answers 2xx and leaves the stored document
as it was. The door and the restore share their cleaners
(`sync.restore.ret_wizard_answer`, `_CONFIG_SHAPES`); a cleaner the two
have drifted apart on fails here — the restore keeps something the door
refuses, or the door rewrites what the restore kept.
"""

from __future__ import annotations

import random
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import export, restore

from . import body_fuzz, props
from .money_households import Household
from .money_surfaces import at_today
from .util import seed_accounts

_ZONES = ("America/Denver", "America/New_York", "Europe/Berlin", "UTC")


def settings_body(rng: random.Random) -> dict:
    """A settings save a person could make, every value one the door takes."""
    def some(keys):
        return {k: rng.choice(("done", "skipped"))
                for k in keys if rng.random() < 0.6}
    answers = {"age": rng.randint(18, 90), "end": rng.randint(70, 110),
               "spend": rng.randint(0, 200) * 1000,
               "employer_mo": rng.randint(0, 40) * 50,
               "taxable_mo": rng.randint(0, 40) * 50,
               "ret": round(rng.uniform(0, 12), 1),
               "infl": round(rng.uniform(0, 6), 1)}
    answers = {k: v for k, v in answers.items() if rng.random() < 0.7}
    born = f"{rng.randint(1951, 1999)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    body = {
        "theme": rng.choice(("auto", "light", "dark")),
        "today_view": rng.choice(("summary", "detail")),
        "timezone": rng.choice(_ZONES),
        "history_months": rng.choice((1, 3, 6, 12, None)),
        "setup_mode": rng.choice(("auto", "manual")),
        "dynamic_variable_budget": rng.random() < 0.5,
        "wizard_steps": some(("appearance", "connect", "sync", "import",
                              "bills", "budgets", "email", "finish")),
        "ret_wizard_steps": some(("age", "spend", "saving", "assumptions",
                                  "done")),
        "ret_wizard_answers": answers,
        "ret_wizard_ssage": rng.randint(62, 70),
        "ss_estimates": {str(a): float(rng.randint(800, 4000))
                         for a in (62, 67, 70) if rng.random() < 0.7},
        "email_schedule": {c: {"on": rng.random() < 0.5,
                               "hour": rng.randint(0, 23)}
                           for c in ("daily", "monthly", "yearly")},
        "disabled_bills": ["Gym Club"] if rng.random() < 0.5 else [],
        "birthdate": born,
        "manual_assets": [{"name": "Test House", "kind": "property",
                           "value": float(rng.randint(100, 900) * 1000),
                           "as_of": "2026-06-01"}] if rng.random() < 0.5 else [],
        "custom_buckets": [{"name": "Coffee", "parent": "food",
                            "categories": [], "merchants": ["Noodle Bar"],
                            "monthly": float(rng.randint(20, 200))}]
        if rng.random() < 0.5 else [],
    }
    if rng.random() < 0.5:
        body["ret_answers_v"] = 2
    if rng.random() < 0.4:
        body["income_scenarios"] = [
            {"name": "Now", "take_home": float(rng.randint(1000, 4000)),
             "cadence": rng.choice(("biweekly", "monthly"))},
            {"name": "Later", "take_home": float(rng.randint(1000, 4000)),
             "cadence": "semimonthly"}]
        body["active_scenario"] = rng.choice(("Now", "Later"))
    return body


class RestoredSettingsPassTheirOwnDoor(unittest.TestCase):

    def test_each_restored_key_saves_back_unchanged(self):
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        for seed in props.seeds():
            with self.subTest(seed=seed), at_today():
                src, _e, src_tid = body_fuzz.signup(appmod.app, "r5a")
                conn = tenancy.tenant_connect(src_tid)
                try:
                    seed_accounts(conn)
                    hh = Household(seed, plan=True, conn=conn)
                    r = src.post("/api/settings", json=settings_body(hh.rng))
                    self.assertEqual(r.status_code, 200, r.text[:300])
                    data = export.build_zip(conn)
                finally:
                    conn.close()
                    src.close()
                dest, _e, dest_tid = body_fuzz.signup(appmod.app, "r5b")
                conn = tenancy.tenant_connect(dest_tid)
                try:
                    restore.restore_outcomes(conn, data)
                    stored = budget.load_config(conn)
                    view = dest.get("/api/settings").json()
                    keys = sorted(k for k in stored if k in view)
                    self.assertGreater(len(keys), 10, keys)
                    for key in keys:
                        r = dest.post("/api/settings", json={key: view[key]})
                        self.assertLess(r.status_code, 300,
                                        (key, view[key], r.text[:200]))
                        after = budget.load_config(conn)
                        self.assertEqual(
                            {k: (stored.get(k), after.get(k))
                             for k in set(stored) | set(after)
                             if stored.get(k) != after.get(k)}, {}, key)
                finally:
                    conn.close()
                    dest.close()

    def test_marked_retirement_answers_save_back_as_read(self):
        """The read shows which answers a current client saved (the mark
        inside `ret_wizard_answers`); sending that object back as read is
        a save of the same answers, not an unknown answer."""
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        client, _e, tid = body_fuzz.signup(appmod.app, "r5m")
        try:
            r = client.post("/api/settings", json={
                "ret_wizard_answers": {"age": 41, "spend": 52000},
                "ret_answers_v": 2})
            self.assertEqual(r.status_code, 200, r.text[:200])
            read = client.get("/api/settings").json()["ret_wizard_answers"]
            self.assertIn("answers_v2", read)
            r = client.post("/api/settings", json={"ret_wizard_answers": read})
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertEqual(
                client.get("/api/settings").json()["ret_wizard_answers"], read)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
