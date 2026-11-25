"""A restore ZIP's bill switches, occurrence caps, dismissed hints, wizard
marks and retirement flags are held to the shape the settings doors write.

Each of these keys is written by its own door, which type-checks it; the
restore merge bypasses every door. A list where a dict belongs (or a number
where a list does) then lands verbatim, and Today, Budget, the forecast, the
Bills list and the daily email trip on it on every load, while the doors
that could repair it (bill save, bill rename, the wizard step save) trip on
the same value. A malformed key is dropped (or its malformed entries are)
with a note; a well-formed one comes through unchanged."""

import copy
import csv
import datetime as dt
import io
import json
import pathlib
import re
import unittest
import uuid
import zipfile

from oikonome.db import tenancy
from oikonome.engine import budget, forecast
from oikonome.sync import restore

from .util import (TODAY, _ensure_db, add_bill, make_db, seed_accounts,
                   write_config)


def _zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


def _zip_nan(config: dict) -> bytes:
    """An archive as a hand edit can write it: a bare NaN or Infinity is
    JSON Python's reader accepts."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config, allow_nan=True)})
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
            "ret_wizard_answers": [41],
            "birthdate": ["1971-06-15"]})
        for k in ("occurrence_caps", "disabled_bills", "dismissed_hints",
                  "dismissed_health", "wizard_steps", "biz_wizard_steps",
                  "ret_wizard_steps", "ret_wizard_answers", "birthdate"):
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

    def test_retirement_answers_keep_only_known_finite_numbers(self):
        """The retirement wizard reads each answer back into a number box;
        a string, a bool, an unknown key or a number outside what the
        save door accepts is dropped, the rest kept."""
        cfg, notes = self._restore({"ret_wizard_answers": {
            "age": 41, "spend": "3800", "ret": True, "infl": -1.5,
            "end": -3, "employer_mo": 10 ** 12, "nonsense": 1,
            "taxable_mo": None}})
        self.assertEqual(cfg["ret_wizard_answers"], {"age": 41, "infl": -1.5})
        # each answer is held to the range the settings door holds it to
        cfg, _ = self._restore({"ret_wizard_answers": {
            "infl": 31, "ret": -20, "age": 17, "end": 110}})
        self.assertEqual(cfg["ret_wizard_answers"], {"ret": -20, "end": 110})
        self.assertIn("not in the shape", notes)

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
                "ret_wizard_answers": {"age": 41, "spend": 3800.5},
                "retirement_saving": True,
                "birthdate": "1972-02-29"}
        cfg, notes = self._restore(good)
        for k, v in good.items():
            self.assertEqual(cfg[k], v, k)
        self.assertNotIn("not in the shape", notes)
        self._pages_read_it(cfg)



# ---- every key the app writes, walked ---------------------------------------

_SRC = pathlib.Path(restore.__file__).resolve().parents[1]
# a settings document read or written under one of these names; cli.py's
# `config` is the logging setup, not a household's settings
_WRITES = re.compile(
    r'\b(?:cfg|live|judged|config)\[\s*"([a-z0-9_]+)"\s*\]\s*=(?!=)'
    r'|\b(?:cfg|live|config)\.setdefault\(\s*"([a-z0-9_]+)"')
_READS = re.compile(
    r'\b(?:cfg|config)(?:\.get\(|\[|\.pop\()\s*"([a-z0-9_]+)"')
_NOT_SETTINGS = {"cli.py"}


def known_settings_keys() -> set[str]:
    """Every settings key the app reads or writes: the settings door's own
    field list, the keys spelled out at each read and write of the
    document, and the keys the writers name through a constant."""
    from oikonome import homepages
    from oikonome.engine import debt, paychecks
    from oikonome.notify import email_prefs, outbound_pause
    from oikonome.web import api_settings
    keys = set(api_settings._SETTINGS_FIELDS)
    for path in _SRC.rglob("*.py"):
        if path.name in _NOT_SETTINGS:
            continue
        text = path.read_text()
        for m in _WRITES.finditer(text):
            keys.add(m.group(1) or m.group(2))
        keys.update(m.group(1) for m in _READS.finditer(text))
    keys |= {debt.PLAN_KEY, paychecks.CONFIG_KEY, email_prefs.KEY,
             outbound_pause.KEY, budget.SAVED_BY_USER_KEY,
             api_settings._REMOVED_KEY, *homepages.VOCAB,
             # the demo seeds its own document as one literal
             "demo_seed", "demo_years"}
    return keys


def _deep(n: int):
    v: list = []
    for _ in range(n):
        v = [v]
    return v


# the values no door writes under any key: the wrong kind, numbers no
# figure can be, a megabyte of text, a list nested far past any shape
HOSTILE = {
    "list of objects": [{"x": 1}],
    "object of lists": {"62": [1], "4821": [1], "x": [1]},
    "NaN": float("nan"),
    "infinity": float("inf"),
    "near the float ceiling": 1e308,
    "object near the ceiling": {"67": 1e308, "62": 1.6e308,
                                "4821": 1e308, "abcd": -1e308},
    "a megabyte of text": "x" * (1 << 20),
    "deeply nested list": _deep(150),
}


def _held(key: str) -> bool:
    return (key in restore._CONFIG_SHAPES
            or key in restore.HELD_BY_OTHER_CLEANERS
            or key in restore._POLICY_KEYS
            or key in restore._CREDENTIAL_COMPANION_KEYS
            or restore._secret_key(key))


class EveryKnownKeyHasAShape(unittest.TestCase):
    def test_every_key_the_app_reads_or_writes_is_held_to_a_shape(self):
        """A new setting with no shape here would land from an archive
        exactly as the archive spelled it."""
        missing = sorted(k for k in known_settings_keys() if not _held(k))
        self.assertEqual(missing, [])

    def test_the_walk_finds_the_keys_it_is_meant_to(self):
        keys = known_settings_keys()
        for k in ("ss_estimates", "investment_basis_override",
                  "checking_account_id", "history_months", "debt_plan",
                  "savings_goal_state", "email_prefs", "paycheck_payers"):
            self.assertIn(k, keys)


def _client():
    import os
    from fastapi.testclient import TestClient
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return TestClient(appmod.app)


class AHouseholdRestoredWithHostileSettingsStillLoads(unittest.TestCase):
    """Retirement, Net worth and Today load for a household whose archive
    carried a hostile value under every known key, and no hostile value
    lands."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.client.post("/api/signup", data={
            "email": f"shape-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _restore(self, config: dict) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            restore.restore_zip(conn, _zip_nan(config))
            return budget.load_config(conn)
        finally:
            conn.close()

    def _pages_load(self):
        for path in ("/api/retirement", "/api/reports/networth", "/api/today"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, (path, r.text[:300]))

    def test_a_social_security_estimate_near_the_float_ceiling(self):
        cfg = self._restore({"ss_estimates": {"67": 1e308, "62": 1.6e308}})
        self.assertNotIn(1e308, (cfg.get("ss_estimates") or {}).values())
        self._pages_load()

    def test_a_basis_override_that_is_not_an_object_of_amounts(self):
        for bad in (["4821"], {"4821": "abc"}, {"4821": [1]}):
            cfg = self._restore({"investment_basis_override": bad})
            self._pages_load()
            self.assertNotEqual(cfg.get("investment_basis_override"), bad)

    def _walk(self, keys, label, bad):
        with self.subTest(hostile=label, keys=keys[0]):
            cfg = self._restore({k: copy.deepcopy(bad) for k in keys})
            landed = []
            for k in keys:
                if k not in cfg:
                    continue
                v = cfg[k]
                if v == bad and v is not None or (
                        isinstance(v, str) and len(v) > 4096):
                    landed.append(k)
                elif k in restore._CONFIG_SHAPES:
                    again, lost = restore._CONFIG_SHAPES[k](
                        copy.deepcopy(v))
                    if again is restore._DROP or lost or again != v:
                        landed.append(k)
            self.assertEqual(landed, [])
            self._pages_load()

    def test_every_known_key_given_every_hostile_value(self):
        # a malformed AI backend list or role map refuses the whole
        # archive (nothing lands); each is walked on its own below
        refused = {"llm_backends", "llm_roles"}
        keys = sorted(known_settings_keys() - refused)
        for label, bad in HOSTILE.items():
            # a megabyte under every key at once is past the archive's
            # record limit, which refuses the file before any key is read
            step = 6 if isinstance(bad, str) else len(keys)
            for i in range(0, len(keys), step):
                self._walk(keys[i:i + step], label, bad)
        for k in sorted(refused):
            for label, bad in HOSTILE.items():
                if k == "llm_backends" and bad in (_deep(0), None):
                    continue
                with self.subTest(refused=k, hostile=label):
                    try:
                        cfg = self._restore({k: copy.deepcopy(bad)})
                    except (ValueError, restore._ArchiveRefused):
                        continue
                    self.assertNotEqual(cfg.get(k), bad)
                    self._pages_load()


if __name__ == "__main__":
    unittest.main()
