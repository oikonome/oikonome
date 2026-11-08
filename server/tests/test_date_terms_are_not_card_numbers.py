"""A number typed in groups is cut to its last four; a date never is.

Group size is what tells them apart: every date shape has a one- or
two-digit day or month, a card or account number is groups of three or
more digits (4-4-4-4, 4-6-5, 4-4-4-3), whatever sits between the groups.
Cut as a number, two instruments named by their terms would read as one
account and share one id, and the second's identical lines would be lost."""
import unittest

from oikonome.sync import base, csvimport, qifimport

from .util import make_db, write_config


class DatesKeepTheirDigits(unittest.TestCase):
    def test_date_shapes_are_left_whole(self):
        for name in ("CD 03/15/2025 09/15/2025", "CD 3/15/2025-9/15/2025",
                     "CD 03/15/2025 - 09/15/2025", "T-Bill 2025-03-15 2025-09-15",
                     "Checking 12/31/2024 1234", "Statement 2021-01-01_2021-12-31",
                     "T-Bill 2025-03-15 2025-09-15"):
            self.assertEqual(base.mask_account_name(name), name, name)
            # the SQL selector may pick the slug up; the re-key's own judge
            # (the name cut) leaves it where it is
            slug = "manual:" + base.re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            self.assertEqual(base.renumbered_manual_id(slug), slug)

    def test_card_layouts_are_cut_and_nothing_else_is_guessed(self):
        for name, cut in (("Visa 4111 1111 1111 1111", "Visa …1111"),
                          ("Visa 4111-1111-1111-1111", "Visa …1111"),
                          ("Amex 3714 496353 98431", "Amex …8431"),
                          ("Visa 4111111111111111", "Visa …1111"),
                          ("Card 12 4111 1111 1111 1111", "Card 12 …1111"),
                          ("Two 4111 1111 1111 1111 / 4000 0000 0000 0002",
                           "Two …1111 / …0002"),
                          # a card layout whatever separates the groups,
                          # as the id's slug has always read it
                          ("Visa 4111 - 1111 - 1111 - 1111", "Visa …1111"),
                          ("Visa 4111.1111.1111.1111", "Visa …1111"),
                          # not a card layout: kept as typed
                          ("Checking 0012 3456 7890 123", "Checking 0012 3456 7890 123")):
            self.assertEqual(base.mask_account_name(name), cut, name)


def _qif(name, rows):
    body = "".join(f"D{d}\nT{t}\nP{p}\n^\n" for d, t, p in rows)
    return f"!Account\nN{name}\nTBank\n^\n!Type:Bank\n{body}"


class TwoInstrumentsNamedByTheirTerms(unittest.TestCase):
    def test_stay_two_accounts_and_keep_their_shared_line(self):
        conn = make_db()
        try:
            write_config(conn)
            text = (_qif("CD 03/15/2025 09/15/2025",
                         [("06/30/2025", "41.10", "INTEREST PAYMENT"),
                          ("07/01/2025", "5.00", "A ONLY")])
                    + _qif("CD 01/15/2025 07/15/2025",
                           [("06/30/2025", "41.10", "INTEREST PAYMENT"),
                            ("07/02/2025", "7.00", "B ONLY")]))
            qifimport.import_qif(conn, "chk", text)
            names = {r["id"]: r["name"] for r in conn.execute(
                "SELECT id, name FROM accounts WHERE id LIKE 'manual:qif-%%'").fetchall()}
            self.assertEqual(len(names), 2, names)
            n = conn.execute("SELECT count(*) AS n FROM transactions"
                             " WHERE account_id LIKE 'manual:qif-%%'").fetchone()["n"]
            self.assertEqual(n, 4)
        finally:
            conn.close()

    def test_a_long_digit_run_is_cut_in_one_pass(self):
        import time
        t0 = time.monotonic()
        out = base.mask_account_name("1" * 100_000)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(out, "…1111")
        t0 = time.monotonic()
        base.mask_account_name("1234 " * 20_000)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_what_follows_a_number_is_never_pulled_into_it(self):
        for name, cut in (("Chase 4111 1111 1111 1234, 05/27", "Chase …1234, 05/27"),
                          ("Visa 4111 1111 1111 1234 (2)", "Visa …1234 (2)"),
                          ("Card 4111-1111-1111-1234 #2", "Card …1234 #2"),
                          ("Visa 4111 1111 1111 1234 - 2025", "Visa …1234 - 2025"),
                          ("Card 4111-1111-1111-1234-2", "Card …1234-2")):
            self.assertEqual(base.mask_account_name(name), cut, name)

    def test_two_cards_sharing_their_last_four_in_one_file_keep_every_line(self):
        conn = make_db()
        try:
            write_config(conn)
            text = (_qif("Visa 4111 1111 1111 1234",
                         [("06/30/2025", "-41.10", "COFFEE"), ("07/01/2025", "-5.00", "A ONLY")])
                    + _qif("Visa 4000 0000 0000 1234",
                           [("06/30/2025", "-41.10", "COFFEE"), ("07/02/2025", "-7.00", "B ONLY")]))
            qifimport.import_qif(conn, "chk", text)
            # names that cut alike share one account, as they always have;
            # what must not happen is a line of one card overwriting the
            # other's identical line
            ids = sorted(r["id"] for r in conn.execute(
                "SELECT id FROM accounts WHERE id LIKE 'manual:qif-%%'").fetchall())
            for i in ids:
                self.assertNotRegex(i, r"\d{8}|\d{3,}-\d{3,}-\d{3,}")
            n = conn.execute("SELECT count(*) AS n FROM transactions"
                             " WHERE account_id LIKE 'manual:qif-%%'").fetchone()["n"]
            self.assertEqual(n, 4)
            # and the same file again adds nothing
            qifimport.import_qif(conn, "chk", text)
            n2 = conn.execute("SELECT count(*) AS n FROM transactions"
                              " WHERE account_id LIKE 'manual:qif-%%'").fetchone()["n"]
            self.assertEqual(n2, 4)
        finally:
            conn.close()


class AmountsAreFinite(unittest.TestCase):
    def test_inf_nan_and_exponents_are_refused(self):
        for s in ("inf", "-inf", "nan", "1e999", "1e16"):
            with self.assertRaises(ValueError, msg=s):
                csvimport.parse_money_text(s)
        self.assertEqual(csvimport.parse_money_text("1e3"), 1000.0)
