"""A file or a typed name that spells a full card number reaches the one
account it means after `oikonome migrate` has moved that account onto an
id keeping only the last four — and asks, before anything runs, when it
cannot tell which.

Before names were masked, two names that slug alike ("Visa 4111…" and
"VISA 4111…", or a hidden account and the live one made beside it) were
told apart by a "-2" the account doors append. The move keeps that
suffix and records each lineage on its own, so the import must walk the
suffixes as it did before the move and pick the account by its stored
name, or a statement lands on the wrong card. A folder import plans once
and runs later, so the plan must see a name it cannot place and the job
must follow an account that moved while it waited.
"""
import unittest

from oikonome.db import staging
from oikonome.jobs import bulk_import_job
from oikonome.sync import base
from oikonome.web import bulk_import, pages

from .bulk_job_util import run_job, submit, submit_and_run
from .util import make_db, write_config

NUMBER = "4111111111111111"            # a test card number
OTHER = "5500000000001111"             # another, with the same last four
CARD_CSV = (b"Date,Description,Amount\n"
            b"2021-05-01,HARBOR BAKERY,-8.00\n"
            b"2021-05-02,NORTHWIND FUEL,-31.00\n"
            b"2021-05-03,CONTOSO STREAM,-12.00\n")
YNAB = ('"Account","Flag","Date","Payee","Category Group/Category",'
        '"Category Group","Category","Memo","Outflow","Inflow","Cleared"\n'
        f'"Visa {NUMBER}","","2026-04-02","Harbor Bakery","","","","",'
        '"$8.00","$0.00","Cleared"\n'
        '"Everyday Checking","","2026-04-03","Northwind Fuel","","","","",'
        '"$31.00","$0.00","Cleared"\n')


class _Household(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('manual','manual','Manual')"
            " ON CONFLICT (tenant_id, id) DO NOTHING")

    def _old(self, aid, name, hidden=False):
        """An account as a release before masked names made it, holding
        the card file's rows under its full-number id."""
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s,'manual',%s,'credit','credit card')", (aid, name))
        plan = bulk_import.analyze(self.conn, [("card.csv", CARD_CSV)])
        res = submit_and_run(self.conn, plan["token"], [
            dict(plan["files"][0], account_id=aid, new_account_name="",
                 amount_sign="bank")])[0]
        self.assertTrue(res["ok"], res)
        if hidden:
            self.conn.execute("UPDATE accounts SET user_removed_at = now()"
                              " WHERE id = %s", (aid,))
        return aid

    def _plan(self, typed, account_id=None):
        plan = bulk_import.analyze(self.conn, [("card.csv", CARD_CSV)])
        return plan["token"], [dict(plan["files"][0], account_id=account_id,
                                    new_account_name=typed,
                                    amount_sign="bank")]

    def _run(self, typed):
        return submit_and_run(self.conn, *self._plan(typed))[0]

    def _counts(self):
        return self.conn.execute(
            "SELECT (SELECT count(*) FROM accounts) AS accounts,"
            " (SELECT count(*) FROM transactions) AS rows").fetchone()


class ASuffixedTwin(_Household):
    def test_a_hidden_base_and_a_live_twin_send_the_number_to_the_twin(self):
        full = f"manual:visa-{NUMBER}"
        self._old(full, f"Visa {NUMBER}", hidden=True)
        self._old(full + "-2", f"Visa {NUMBER}")
        moved = base.rekey_numbered_accounts(self.conn)
        self.assertEqual(moved[full + "-2"], "manual:visa-1111-2")
        before = self._counts()
        res = self._run(f"Visa {NUMBER}")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["account_id"], "manual:visa-1111-2")
        self.assertEqual(self._counts(), before)

    def test_two_live_twins_named_in_different_case_each_get_their_own(self):
        full = f"manual:visa-{NUMBER}"
        self._old(full, f"Visa {NUMBER}")
        self._old(full + "-2", f"VISA {NUMBER}")
        base.rekey_numbered_accounts(self.conn)
        before = self._counts()
        for typed, want in ((f"VISA {NUMBER}", "manual:visa-1111-2"),
                            (f"Visa {NUMBER}", "manual:visa-1111")):
            res = self._run(typed)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["account_id"], want, typed)
        self.assertEqual(self._counts(), before)

    def test_a_typed_new_account_name_takes_the_same_road(self):
        full = f"manual:visa-{NUMBER}"
        self._old(full, f"Visa {NUMBER}", hidden=True)
        self._old(full + "-2", f"Visa {NUMBER}")
        base.rekey_numbered_accounts(self.conn)
        with self.conn.transaction():
            got = bulk_import._ensure_account(self.conn, f"Visa {NUMBER}")
        self.assertEqual(got, "manual:visa-1111-2")


class ALoneHiddenTarget(_Household):
    def test_a_hidden_moved_account_makes_one_new_account_beside_it(self):
        """A hidden account is not a destination (the upsert refuses its
        rows), so a name leading only to one makes a new account beside
        it — what a hidden account of the same plain name has always
        meant — and every later file finds that new one."""
        self._old(f"manual:visa-{NUMBER}", f"Visa {NUMBER}", hidden=True)
        moved = base.rekey_numbered_accounts(self.conn)
        first = self._run(f"Visa {NUMBER}")
        self.assertTrue(first["ok"], first)
        self.assertNotIn(first["account_id"], moved.values())
        self.assertRegex(first["account_id"], r"^manual:[a-z]{16}$")
        after = self._counts()
        again = self._run(f"Visa {NUMBER}")
        self.assertEqual(again["account_id"], first["account_id"])
        self.assertEqual(self._counts(), after)


class AQueuedJobAcrossAMove(_Household):
    def test_a_job_queued_for_a_numbered_account_lands_on_the_moved_one(self):
        old = self._old(f"manual:visa-{NUMBER}", f"Visa {NUMBER}")
        before = self._counts()
        sub = submit(self.conn, *self._plan("", account_id=old))
        moved = base.rekey_numbered_accounts(self.conn)
        st = run_job(self.conn, sub["job_id"])
        res = bulk_import_job.compat_results(st)[0]
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["account_id"], moved[old])
        self.assertEqual(self._counts(), before)

    def test_a_job_whose_account_is_gone_fails_that_file_plainly(self):
        sub = submit(self.conn, *self._plan("", account_id="card"))
        self.conn.execute("DELETE FROM transactions WHERE account_id='card'")
        self.conn.execute("DELETE FROM accounts WHERE id = 'card'")
        res = bulk_import_job.compat_results(
            run_job(self.conn, sub["job_id"]))[0]
        self.assertFalse(res["ok"], res)
        self.assertIn("no longer exists", res["error"])


class TwoCardsSharingTheLastFour(_Household):
    """The three callers that can meet a name two moved cards share."""

    def setUp(self):
        super().setUp()
        self._old(f"manual:visa-{NUMBER}", f"Visa {NUMBER}")
        self._old(f"manual:visa-{OTHER}", f"Visa {OTHER}")

    def _move(self):
        self.moves = base.rekey_numbered_accounts(self.conn)
        self.moved = sorted(self.moves.values())
        self.assertEqual(len(self.moved), 2)

    def test_the_plan_lists_the_cards_before_anything_runs(self):
        self._move()
        before = self._counts()
        token, decisions = self._plan(f"Visa {NUMBER}")
        with self.assertRaises(ValueError) as e:
            submit(self.conn, token, decisions)
        self.assertIn("choose one", str(e.exception))
        self.assertIn("card.csv", str(e.exception))
        self.assertEqual(self._counts(), before)
        # nothing spent: the person picks one and runs the same plan
        out = submit_and_run(self.conn, token, [
            dict(decisions[0], account_id=self.moved[1],
                 new_account_name="")])[0]
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["account_id"], self.moved[1])
        self.assertEqual(self._counts(), before)

    def test_a_file_named_by_the_number_is_planned_as_a_choice(self):
        self._move()
        plan = bulk_import.analyze(
            self.conn, [(f"Visa {NUMBER}.csv", CARD_CSV)])
        entry = plan["files"][0]
        self.assertIsNone(entry["account_id"])
        self.assertEqual(sorted(c["id"] for c in entry["choices"]),
                         self.moved)
        self.assertIn("choose one", entry["note"])

    def test_the_job_refuses_a_file_the_move_made_ambiguous(self):
        # planned while the cards still had their numbers, run after
        sub = submit(self.conn, *self._plan(f"Visa {NUMBER}"))
        self._move()
        before = self._counts()
        res = bulk_import_job.compat_results(
            run_job(self.conn, sub["job_id"]))[0]
        self.assertFalse(res["ok"], res)
        self.assertIn("choose one", res["error"])
        self.assertEqual(self._counts(), before)

    def test_a_hidden_card_does_not_send_its_number_to_the_other_card(self):
        """One cut record leads to both cards and cannot say which is
        which, so hiding one must not make the other the lone answer:
        the import asks, and nothing lands."""
        self._move()
        hidden = self.moves[f"manual:visa-{NUMBER}"]
        self.conn.execute("UPDATE accounts SET user_removed_at = now()"
                          " WHERE id = %s", (hidden,))
        before = self._counts()
        choices = bulk_import.account_choices(self.conn, f"Visa {NUMBER}")
        # the hidden card takes no rows, so it is not offered; the live
        # one is offered, never picked
        self.assertEqual([c["id"] for c in choices],
                         [m for m in self.moved if m != hidden])
        token, decisions = self._plan(f"Visa {NUMBER}")
        with self.assertRaises(ValueError) as e:
            submit(self.conn, token, decisions)
        self.assertIn("choose one", str(e.exception))
        # and a plan that names the hidden card is refused up front
        token, decisions = self._plan("", account_id=hidden)
        with self.assertRaises(ValueError) as e:
            submit(self.conn, token, decisions)
        self.assertIn("hidden", str(e.exception))
        with self.conn.transaction(), self.assertRaises(
                bulk_import.AccountChoiceNeeded):
            bulk_import._ensure_account(self.conn, f"Visa {NUMBER}")
        self.assertEqual(self._counts(), before)

    def test_both_cards_hidden_leaves_the_name_a_new_account(self):
        """With every card the record leads to hidden, the name has no
        destination: the door makes a new account beside them, as it does
        for a hidden account of the same plain name."""
        self._move()
        self.conn.execute("UPDATE accounts SET user_removed_at = now()"
                          " WHERE id = ANY(%s)", (self.moved,))
        self.assertEqual(
            bulk_import.account_choices(self.conn, f"Visa {NUMBER}"), [])
        before = self._counts()
        res = self._run(f"Visa {NUMBER}")
        self.assertTrue(res["ok"], res)
        self.assertNotIn(res["account_id"], self.moved)
        after = self._counts()
        self.assertEqual(after["accounts"], before["accounts"] + 1)
        self.assertGreater(after["rows"], before["rows"])

    def test_names_differing_only_in_case_do_not_pick_a_card(self):
        """The stored names are both "…1111" cut names; a case difference
        between them says nothing about which card a number is."""
        self.conn.execute("UPDATE accounts SET name = %s WHERE id = %s",
                          (f"VISA {OTHER}", f"manual:visa-{OTHER}"))
        self._move()
        before = self._counts()
        for typed in (f"VISA {NUMBER}", f"Visa {NUMBER}"):
            choices = bulk_import.account_choices(self.conn, typed)
            self.assertEqual(sorted(c["id"] for c in choices), self.moved,
                             typed)
            with self.conn.transaction(), self.assertRaises(
                    bulk_import.AccountChoiceNeeded):
                bulk_import._ensure_account(self.conn, typed)
        plan = bulk_import.analyze(
            self.conn, [(f"VISA {NUMBER}.csv", CARD_CSV)])
        self.assertIsNone(plan["files"][0]["account_id"])
        self.assertEqual(sorted(c["id"] for c in plan["files"][0]["choices"]),
                         self.moved)
        self.assertEqual(self._counts(), before)

    def test_the_ynab_card_keeps_its_upload_and_takes_an_existing_account(
            self):
        self._move()
        result, need = pages._dispatch_import_unlocked(
            self.conn, "chk", "bank", "register.csv", YNAB.encode(),
            role="owner", ynab_choice=True)
        self.assertIsNotNone(need, result)
        token = need["token"]
        before = self._counts()
        names = sorted(a["name"] for a in need["accounts"])
        visa = next(n for n in names if n.lower().startswith("visa"))
        answers = {n: "chk" for n in names}
        answers[visa] = {"new": f"Visa {NUMBER}"}
        out = pages.run_ynab_import(self.conn, token, answers, None)
        self.assertIn("choose one", out["error"])
        self.assertEqual(self._counts(), before)
        # the upload is still parked under the same token, and an
        # existing account answers it
        self.assertIsNotNone(staging.peek(self.conn, "ynab", token))
        answers[visa] = self.moved[0]
        out = pages.run_ynab_import(self.conn, token, answers, None)
        self.assertNotIn("error", out, out)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE account_id = %s"
            "   AND name = 'Harbor Bakery'",
            (self.moved[0],)).fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
