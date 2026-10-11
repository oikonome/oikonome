"""A merchant's category rule follows it through a rename or a merge.

A rule is stored under a merchant's name. Renaming the merchant, or merging
it into another, moves every row of that name, and a rule left under the
old name then reaches nothing: the household's correction still shows on
the Rules page, files no charge, and a machine guess under the new name
re-files the whole history. So the rename carries the rule to the merchant
the rows went to. A survivor's own rule from a person is never overwritten
(the old one is set aside), a person's rule outranks a machine's, and
everything set aside is journalled so undo puts it back.
"""

import datetime as dt
import unittest

from oikonome.engine import (llm_categorize, merchant_dedup, merchant_identity,
                             merchant_merge)

from .util import TODAY, add_txn, make_db, write_config

OLD = "Copperline Bikes"
NEW = "Copperline Cycles"
SURVIVOR = "Copperline"


class _Rules(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        # a file import's rows: the bank line cleans to the display name, so
        # no alias row carries the name the person's rule is stored under
        for back in (90, 60, 30):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), 42.0,
                    "SQ *COPPERLINE BIKES", merchant=None)
        merchant_dedup.apply(self.conn)
        llm_categorize.upsert_user_rule(self.conn, OLD, "ENTERTAINMENT")
        llm_categorize.apply(self.conn)

    def survivor_rows(self, n=2):
        for back in range(n):
            add_txn(self.conn, TODAY - dt.timedelta(days=200 + back), 15.0,
                    "COPPERLINE SERVICE DESK", merchant=SURVIVOR)
        merchant_identity.resolve(self.conn)

    def rule(self, name):
        r = self.conn.execute(
            "SELECT category_primary AS cat, source FROM merchant_categories "
            "WHERE merchant=%s", (name,)).fetchone()
        return (r["cat"], r["source"]) if r else None

    def cats(self, line="SQ *COPPERLINE BIKES"):
        return {r["category_primary"] for r in self.conn.execute(
            "SELECT category_primary FROM transactions WHERE name=%s",
            (line,)).fetchall()}

    def rename(self, frm, to):
        with self.conn.transaction():
            merchant_dedup.rename(self.conn, frm, to)
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC").fetchall()]

    def undo(self, ids):
        for cid in ids:
            with self.conn.transaction():
                merchant_dedup.undo(self.conn, cid)

    def machine_guess(self, name, cat="GENERAL_SERVICES"):
        """What the categorizer does with a merchant no rule covers."""
        llm_categorize.record_model_category(self.conn, name, cat)
        llm_categorize.apply(self.conn)


class RenameCarriesTheRule(_Rules):
    def test_a_rename_keeps_the_households_category(self):
        changes = self.rename(OLD, NEW)
        self.assertEqual(self.rule(NEW), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(OLD))
        self.machine_guess(NEW)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})
        self.undo(changes)
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(NEW))

    def test_a_merge_into_a_merchant_with_no_rule_brings_it(self):
        self.survivor_rows()
        changes = self.rename(OLD, SURVIVOR)
        self.assertEqual(self.rule(SURVIVOR), ("ENTERTAINMENT", "user"))
        self.machine_guess(SURVIVOR)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})
        self.undo(changes)
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(SURVIVOR))

    def test_the_survivors_own_rule_from_a_person_is_never_overwritten(self):
        self.survivor_rows()
        llm_categorize.upsert_user_rule(self.conn, SURVIVOR, "HOME_IMPROVEMENT")
        changes = self.rename(OLD, SURVIVOR)
        self.assertEqual(self.rule(SURVIVOR), ("HOME_IMPROVEMENT", "user"))
        self.assertIsNone(self.rule(OLD))
        # set aside, not lost: undo brings it back with the rows
        self.undo(changes)
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertEqual(self.rule(SURVIVOR), ("HOME_IMPROVEMENT", "user"))
        llm_categorize.apply(self.conn)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})

    def test_a_persons_rule_outranks_the_survivors_machine_rule(self):
        self.survivor_rows()
        llm_categorize.record_model_category(self.conn, SURVIVOR, "GENERAL_SERVICES")
        changes = self.rename(OLD, SURVIVOR)
        self.assertEqual(self.rule(SURVIVOR), ("ENTERTAINMENT", "user"))
        llm_categorize.apply(self.conn)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})
        self.undo(changes)
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertEqual(self.rule(SURVIVOR), ("GENERAL_SERVICES", "llm"))

    def test_a_rule_edited_after_the_merge_survives_its_undo(self):
        self.survivor_rows()
        changes = self.rename(OLD, SURVIVOR)
        llm_categorize.upsert_user_rule(self.conn, SURVIVOR, "HOME_IMPROVEMENT")
        self.undo(changes)
        self.assertEqual(self.rule(SURVIVOR), ("HOME_IMPROVEMENT", "user"))
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))

    def test_approving_a_merge_offer_carries_the_rule(self):
        self.survivor_rows()
        ids = {r["name"]: r["id"] for r in self.conn.execute(
            "SELECT id, name FROM merchants WHERE merged_into IS NULL").fetchall()}
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id, "
            " into_merchant_id) VALUES ('mm:offer', %s, %s)",
            (ids[OLD], ids[SURVIVOR]))
        got = merchant_merge.decide(self.conn, "mm:offer", "approve")
        self.assertNotIn("error", got)
        self.assertEqual(self.rule(SURVIVOR), ("ENTERTAINMENT", "user"))
        self.machine_guess(SURVIVOR)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})


class RenamesMadeBeforeRulesFollowed(_Rules):
    """The operator repair for renames that left their rule behind."""

    def orphan(self):
        """A merge as it was applied before the rename carried rules: the
        rows moved, the rule stayed under the old name, no note."""
        self.survivor_rows()
        self.rename(OLD, SURVIVOR)
        self.conn.execute("UPDATE merchant_renames SET rules = NULL")
        self.conn.execute("UPDATE merchant_categories SET merchant=%s "
                          "WHERE merchant=%s", (OLD, SURVIVOR))

    def test_a_dry_run_names_the_change_and_writes_nothing(self):
        self.orphan()
        got = merchant_dedup.rekey_orphaned_rules(self.conn)
        self.assertEqual([(g["from"], g["into"]) for g in got], [(OLD, SURVIVOR)])
        self.assertEqual(got[0]["rules"][0]["fate"], "moved")
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(SURVIVOR))

    def test_apply_rekeys_once_and_undo_still_restores(self):
        self.orphan()
        self.assertEqual(len(merchant_dedup.rekey_orphaned_rules(
            self.conn, apply=True)), 1)
        self.assertEqual(self.rule(SURVIVOR), ("ENTERTAINMENT", "user"))
        self.assertEqual(merchant_dedup.rekey_orphaned_rules(
            self.conn, apply=True), [])
        self.machine_guess(SURVIVOR)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})
        changes = [r["id"] for r in self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC").fetchall()]
        self.undo(changes)
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(SURVIVOR))

    def test_an_undo_while_the_repair_waits_for_the_lock_is_respected(self):
        """The repair reads its groups before it takes the lock. A rename
        undone in between put its strings back under the old merchant, so
        that merchant's rule is its own again and must not be carried."""
        from unittest import mock
        self.orphan()
        changes = [r["id"] for r in self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC").fetchall()]
        real = merchant_identity._lock_identity
        undone = []

        def lock_after_an_undo(conn):
            real(conn)
            if not undone:
                undone.append(True)
                for cid in changes:
                    merchant_dedup.undo(conn, cid)

        with mock.patch.object(merchant_identity, "_lock_identity",
                               side_effect=lock_after_an_undo):
            got = merchant_dedup.rekey_orphaned_rules(self.conn, apply=True)
        self.assertTrue(undone)
        self.assertEqual(got, [])
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(SURVIVOR))


class SurvivorAlreadyHoldsTheKey(_Rules):
    """The survivor's name is also one of the strings that moved, and a rule
    already lives under it. Carrying the old name's rule there is a write
    onto an occupied key: precedence decides it, and it never raises."""

    def collide(self, survivor_cat="GENERAL_SERVICES", survivor_source="llm",
                old_source="user"):
        """An orphaned merge whose journal also moved the survivor's own
        string, with a rule waiting under that string."""
        self.survivor_rows()
        self.rename(OLD, SURVIVOR)
        self.conn.execute("UPDATE merchant_renames SET rules = NULL")
        self.conn.execute("UPDATE merchant_categories SET merchant=%s, source=%s "
                          "WHERE merchant=%s", (OLD, old_source, SURVIVOR))
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical, "
            " to_canonical) VALUES (%s, %s, %s)", (SURVIVOR, OLD, SURVIVOR))
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, source) "
            "VALUES (%s, %s, %s)", (SURVIVOR, survivor_cat, survivor_source))

    def snapshot(self):
        return self.conn.execute(
            "SELECT merchant, category_primary, source, disabled "
            "FROM merchant_categories ORDER BY merchant").fetchall(), \
            self.conn.execute(
                "SELECT id, rules FROM merchant_renames ORDER BY id").fetchall()

    def notes(self):
        # one note per rename, written to each of its journal rows
        return [r["rules"] for r in self.conn.execute(
            "SELECT DISTINCT rules FROM merchant_renames "
            "WHERE rules IS NOT NULL").fetchall()]

    def test_a_dry_run_plans_the_collision_and_writes_nothing(self):
        self.collide()
        before = self.snapshot()
        got = merchant_dedup.rekey_orphaned_rules(self.conn)
        self.assertEqual([(g["from"], g["into"]) for g in got], [(OLD, SURVIVOR)])
        self.assertEqual(got[0]["rules"][0]["fate"], "moved")
        self.assertEqual([d["merchant"] for d in got[0]["displaced"]], [SURVIVOR])
        self.assertEqual(self.snapshot(), before)

    def test_a_persons_rule_beats_the_machine_rule_on_the_key(self):
        self.collide()
        got = merchant_dedup.rekey_orphaned_rules(self.conn, apply=True)
        self.assertEqual(len(got), 1)
        self.assertEqual(self.rule(SURVIVOR), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(OLD))
        self.assertEqual(merchant_dedup.rekey_orphaned_rules(
            self.conn, apply=True), [])
        llm_categorize.apply(self.conn)
        self.assertEqual(self.cats(), {"ENTERTAINMENT"})

    def test_a_machine_rule_never_overwrites_the_survivors(self):
        for source in ("llm", "user"):
            with self.subTest(survivor=source):
                self.setUp()
                self.collide(survivor_cat="HOME_IMPROVEMENT",
                             survivor_source=source, old_source="seed")
                merchant_dedup.rekey_orphaned_rules(self.conn, apply=True)
                self.assertEqual(self.rule(SURVIVOR), ("HOME_IMPROVEMENT", source))
                self.assertIsNone(self.rule(OLD))
                dropped = [d for n in self.notes() for d in n["rules"]]
                self.assertEqual([(d["merchant"], d["source"], d["fate"])
                                  for d in dropped], [(OLD, "seed", "dropped")])

    def test_two_persons_rules_keep_the_survivors_and_record_the_other(self):
        self.collide(survivor_cat="HOME_IMPROVEMENT", survivor_source="user")
        merchant_dedup.rekey_orphaned_rules(self.conn, apply=True)
        self.assertEqual(self.rule(SURVIVOR), ("HOME_IMPROVEMENT", "user"))
        self.assertIsNone(self.rule(OLD))
        dropped = [d for n in self.notes() for d in n["rules"]]
        self.assertEqual([(d["merchant"], d["category_primary"], d["fate"])
                          for d in dropped], [(OLD, "ENTERTAINMENT", "dropped")])
        self.assertEqual(merchant_dedup.rekey_orphaned_rules(
            self.conn, apply=True), [])

    def test_a_live_merge_onto_an_occupied_key_resolves_by_precedence(self):
        # the survivor's own string first displays as the old name, then the
        # old name is merged back onto it: that string moves with the rows,
        # and a machine rule is already stored under it
        self.survivor_rows()
        self.rename(SURVIVOR, OLD)
        llm_categorize.record_model_category(self.conn, SURVIVOR, "GENERAL_SERVICES")
        self.assertEqual(self.rule(SURVIVOR), ("GENERAL_SERVICES", "llm"))
        changes = self.rename(OLD, SURVIVOR)
        self.assertEqual(self.rule(SURVIVOR), ("ENTERTAINMENT", "user"))
        self.assertIsNone(self.rule(OLD))
        self.undo(changes[:1])
        self.assertEqual(self.rule(OLD), ("ENTERTAINMENT", "user"))


if __name__ == "__main__":
    unittest.main()
