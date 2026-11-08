"""The settings that name a bill by its payee follow the bill when a
migration cut the card number out of its name.

Migration 206 renamed every detected bill whose payee spelled a card
number and moved two of the settings keyed on the payee (the bills
switched off, the occurrence caps), not the dismissed fit hints or health
pills. On an upgraded household the dismissal stayed under the whole
number, the bill was read under the cut one, and the dismissed hint came
back while the whole number rode every export. Migration 210 re-keys what
206 left behind (db/migrate.py, PYTHON_STEPS), and the cut a restore and
every migrate run over detected bills does the same. Every number here is
a test-card shape, invented.
"""
import unittest

from oikonome.db import migrate
from oikonome.engine import bills, budget

from .util import add_bill, make_db

WHOLE = "CARD 4000 0000 0000 0002 AUTOPAY"
CUT = "CARD …0002 AUTOPAY"
STEP = "210_payee_keyed_settings_follow_a_cut_bill_name.sql"


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _config(self, cfg):
        self.conn.execute(
            "INSERT INTO tenant_settings (config) VALUES (%s::jsonb)"
            " ON CONFLICT (tenant_id) DO UPDATE SET config = EXCLUDED.config",
            (bills.jsonb(cfg),))

    def _read(self):
        return self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"]

    def _step(self):
        migrate.PYTHON_STEPS[STEP](self.conn)

    def _left_behind(self):
        """Every payee-keyed setting as 206 left it on an upgraded
        household: two moved onto the cut name, two still whole."""
        return {"disabled_bills": [CUT, "Rent"],
                "occurrence_caps": {CUT: 50},
                "dismissed_hints": {WHOLE: {"amount": 40}},
                "dismissed_health": {WHOLE: {"stale": "2026-08-01"}},
                "unrelated": {WHOLE: 1}}


class TheSettingsFollowTheCutName(_Db):
    def test_dismissals_left_under_the_whole_number_move_to_the_bill(self):
        add_bill(self.conn, CUT, 40)
        self._config(self._left_behind())
        self._step()
        cfg = self._read()
        self.assertEqual(cfg["dismissed_hints"], {CUT: {"amount": 40}})
        self.assertEqual(cfg["dismissed_health"],
                         {CUT: {"stale": "2026-08-01"}})
        self.assertEqual(cfg["disabled_bills"], [CUT, "Rent"])
        self.assertEqual(cfg["occurrence_caps"], {CUT: 50})
        # only what names a bill by its payee is read
        self.assertEqual(cfg["unrelated"], {WHOLE: 1})

    def test_every_payee_keyed_setting_follows(self):
        add_bill(self.conn, CUT, 40)
        self._config({"disabled_bills": [WHOLE],
                      "occurrence_caps": {WHOLE: 50},
                      "dismissed_hints": {WHOLE: {"amount": 40}},
                      "dismissed_health": {WHOLE: {"stale": "x"}}})
        self._step()
        cfg = self._read()
        for key in bills.PAYEE_KEYED_CONFIG:
            self.assertNotIn(WHOLE, cfg[key], key)
            self.assertIn(CUT, cfg[key], key)

    def test_the_setting_under_the_cut_name_wins_and_the_whole_goes(self):
        add_bill(self.conn, CUT, 40)
        self._config({"disabled_bills": [WHOLE, CUT],
                      "dismissed_hints": {WHOLE: {"amount": 40},
                                          CUT: {"amount": 41}}})
        self._step()
        cfg = self._read()
        self.assertEqual(cfg["disabled_bills"], [CUT])
        self.assertEqual(cfg["dismissed_hints"], {CUT: {"amount": 41}})

    def test_a_bill_still_held_under_the_whole_spelling_keeps_its_own(self):
        # a bill a person made keeps its name as typed, and both live
        add_bill(self.conn, CUT, 40)
        self.conn.execute(
            "INSERT INTO bills (id, payee, amount, frequency, raw)"
            " VALUES ('typed', %s, 40, 'MONTHLY', '{\"source\": \"manual\"}')",
            (WHOLE,))
        self._config({"dismissed_hints": {WHOLE: {"amount": 40}}})
        self._step()
        self.assertEqual(self._read()["dismissed_hints"],
                         {WHOLE: {"amount": 40}})

    def test_a_setting_naming_no_bill_is_left_as_it_is(self):
        self._config({"dismissed_hints": {WHOLE: {"amount": 40}}})
        self._step()
        self.assertEqual(self._read()["dismissed_hints"],
                         {WHOLE: {"amount": 40}})

    def test_it_never_follows_onto_a_bill_a_person_named(self):
        self.conn.execute(
            "INSERT INTO bills (id, payee, amount, frequency, raw)"
            " VALUES ('typed', %s, 40, 'MONTHLY', '{\"source\": \"manual\"}')",
            (CUT,))
        self._config({"dismissed_hints": {WHOLE: {"amount": 40}}})
        self._step()
        self.assertEqual(self._read()["dismissed_hints"],
                         {WHOLE: {"amount": 40}})

    def test_it_is_idempotent(self):
        add_bill(self.conn, CUT, 40)
        self._config(self._left_behind())
        self._step()
        once = self._read()
        self.assertEqual(bills.follow_cut_payees(self.conn), 0)
        self.assertEqual(self._read(), once)

    def test_the_restores_cut_of_detected_bills_follows_them_too(self):
        # a restore (and every migrate) runs cut_detected_keys over the
        # household; an archive written on an upgraded household carries
        # the left-behind dismissal
        add_bill(self.conn, CUT, 40)
        self._config(self._left_behind())
        bills.cut_detected_keys(self.conn)
        self.assertEqual(self._read()["dismissed_hints"],
                         {CUT: {"amount": 40}})

    def test_the_hint_stays_dismissed_on_the_bills_page(self):
        add_bill(self.conn, CUT, 40)
        self._config(self._left_behind())
        self._step()
        cfg = budget.load_config(self.conn)
        self.assertIn(CUT, cfg.get("dismissed_hints") or {})


if __name__ == "__main__":
    unittest.main()
