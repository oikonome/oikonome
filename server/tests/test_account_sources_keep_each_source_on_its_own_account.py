"""A hand-made or imported account's id is opaque, and which account a
source name fills is looked up, never re-derived from the name.

Ids used to be the slug of the name, so the name carried identity: cutting
a card number out of the name cut it out of the identity, two cards
sharing their last four became one account, a rename or a re-cut name lost
its file, and a taken slug grew a "-N" the card grammar then had to tell
from the number's own last group. Now every new account gets
`manual:<letters>` once, and account_sources records which account each
(door, source) fills — keyed under the household's secret, so a full
number is told apart without being stored. Accounts made before keep
their ids (a file row's id is hashed over its account id), and are found
by their stored name or the slug walk those builds made.

Every name and number here is invented."""
import io
import pathlib
import re
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import account_sources, export, ofximport, qifimport, restore
from oikonome.web import bulk_import

from .bulk_job_util import csv_for, submit_and_run
from .util import TEST_DB, _admin_dsn, make_db, write_config

CARD_A = "Visa 4000 1111 2222 0123"
CARD_B = "Visa 4000 3333 4444 0123"          # same last four, another card
OPAQUE = re.compile(r"^manual:[a-z]{16}$")
# the app role's password in the test cluster (tests/util)
APP_PASSWORD = "apppass"

MIGRATION = (pathlib.Path(account_sources.__file__).parent.parent / "db"
             / "migrations"
             / "207_account_sources_say_which_account_a_source_fills.sql")


def _qif(*accounts):
    out = []
    for name, rows in accounts:
        out += ["!Account", f"N{name}", "TCCard", "^", "!Type:CCard"]
        for day, amount, payee in rows:
            out += [f"D03/{day:02d}/2026", f"T{amount:.2f}", f"P{payee}", "^"]
    return "\n".join(out) + "\n"


def _cc_stmt(acct: str, fitid: str, amount: str) -> str:
    return (f"<CCSTMTTRNRS><TRNUID>{fitid}<STATUS><CODE>0<SEVERITY>INFO"
            f"</STATUS><CCSTMTRS><CURDEF>USD<CCACCTFROM><ACCTID>{acct}"
            "</CCACCTFROM><BANKTRANLIST><DTSTART>20260701<DTEND>20260712"
            f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260710<TRNAMT>{amount}"
            f"<FITID>{fitid}<NAME>NORTHWIND FUEL</STMTTRN></BANKTRANLIST>"
            "<LEDGERBAL><BALAMT>-10.00<DTASOF>20260712</LEDGERBAL>"
            "</CCSTMTRS></CCSTMTTRNRS>")


OFX_TWO_CARDS = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
                 "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
                 "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>"
                 "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO"
                 "</STATUS><DTSERVER>20260712120000<LANGUAGE>ENG</SONRS>"
                 "</SIGNONMSGSRSV1><CREDITCARDMSGSRSV1>"
                 + _cc_stmt("4000111122220123", "A001", "-30.00")
                 + _cc_stmt("4000333344440123", "B001", "-12.00")
                 + "</CREDITCARDMSGSRSV1></OFX>\n").encode()


class _Household(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('manual','manual','Manual')"
            " ON CONFLICT (tenant_id, id) DO NOTHING")

    def _ensure(self, typed, sign="bank"):
        with self.conn.transaction():
            return bulk_import._ensure_account(self.conn, typed, sign)

    def _old(self, aid, name):
        """An account as a build before opaque ids made it."""
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s,'manual',%s,'credit','credit card')", (aid, name))
        account_sources.backfill(self.conn)
        return aid

    def _name(self, aid):
        return self.conn.execute("SELECT name FROM accounts WHERE id = %s",
                                 (aid,)).fetchone()["name"]

    def _count(self, sql="SELECT count(*) AS n FROM accounts"):
        return self.conn.execute(sql).fetchone()["n"]

    def _plan_and_run(self, filename, data, **decision):
        plan = bulk_import.analyze(self.conn, [(filename, data)])
        entry = dict(plan["files"][0], amount_sign="bank", **decision)
        return plan, submit_and_run(self.conn, plan["token"], [entry])[0]


class TwoCardsSharingTheirLastFour(_Household):
    def test_two_typed_cards_stay_two_accounts(self):
        a, b = self._ensure(CARD_A, "card"), self._ensure(CARD_B, "card")
        self.assertNotEqual(a, b)
        self.assertEqual(self._name(a), "Visa …0123")
        self.assertEqual(self._name(b), "Visa …0123")
        # each card's next statement finds its own, however it is typed
        self.assertEqual(self._ensure("Visa 4000-1111-2222-0123"), a)
        self.assertEqual(self._ensure(CARD_B), b)

    def test_two_statements_in_a_folder_stay_two_accounts(self):
        _, ra = self._plan_and_run("Visa 4000111122220123.csv",
                                   csv_for("HARBOR"))
        plan, rb = self._plan_and_run("Visa 4000333344440123.csv",
                                      csv_for("NORTHWIND"))
        self.assertTrue(ra["ok"] and rb["ok"], (ra, rb))
        self.assertIsNone(plan["files"][0]["account_id"], plan)
        self.assertNotEqual(ra["account_id"], rb["account_id"])
        _, again = self._plan_and_run("Visa 4000333344440123.csv",
                                      csv_for("NORTHWIND"))
        self.assertEqual(again["account_id"], rb["account_id"])

    def test_an_earlier_account_takes_the_first_card_only(self):
        old = self._old("manual:visa-0123", "Visa …0123")
        self.assertEqual(self._ensure(CARD_A), old)
        b = self._ensure(CARD_B)
        self.assertNotEqual(b, old)
        self.assertRegex(b, OPAQUE)
        self.assertEqual(self._ensure(CARD_A), old)

    def test_an_earlier_account_holding_both_cards_keeps_the_second(self):
        """The upgrade shape of the one above: the earlier account already
        holds the second card's rows, so its re-import stays there and
        adds nothing, where a fresh account would take the card's whole
        history a second time."""
        old = self._old("manual:visa-0123", "Visa …0123")
        a_file = ("Visa 4000111122220123.csv", csv_for("HARBOR"))
        b_file = ("Visa 4000333344440123.csv", csv_for("NORTHWIND"))
        for f in (a_file, b_file):
            self._plan_and_run(*f, account_id=old, new_account_name="")
        rows = self._count("SELECT count(*) AS n FROM transactions")
        self.assertEqual(self._ensure(CARD_A), old)
        _, rb = self._plan_and_run(*b_file, account_id=None,
                                   new_account_name=CARD_B)
        self.assertEqual(rb["account_id"], old, rb)
        self.assertEqual(self._count("SELECT count(*) AS n FROM transactions"),
                         rows)
        self.assertEqual(self._count("SELECT count(*) AS n FROM accounts"
                                     " WHERE item_id = 'manual'"), 1)
        self.assertEqual(self._ensure(CARD_B), old)
        self.assertEqual(self._ensure(CARD_A), old)

    def test_two_cards_in_one_qif_file_stay_two_accounts(self):
        text = _qif(("Visa 4000111122220123", [(4, -61.20, "HARBOR GROCER")]),
                    ("Visa 4000333344440123", [(5, -5.00, "CONTOSO KIOSK")]))
        qifimport.import_qif(self.conn, "chk", text)
        split = self.conn.execute(
            "SELECT account_id, count(*) AS n FROM transactions"
            " WHERE account_id LIKE 'manual:qif-%' GROUP BY account_id"
        ).fetchall()
        self.assertEqual(len(split), 2, split)
        qifimport.import_qif(self.conn, "chk", text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM transactions"
            " WHERE account_id LIKE 'manual:qif-%'"), 2)

    def test_two_cards_in_one_ofx_file_stay_two_accounts(self):
        ofximport.import_ofx(self.conn, "chk", OFX_TWO_CARDS)
        ids = {r["account_id"] for r in self.conn.execute(
            "SELECT account_id FROM transactions WHERE id LIKE 'ofx:%'")}
        self.assertEqual(len(ids), 2, ids)
        ofximport.import_ofx(self.conn, "chk", OFX_TWO_CARDS)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'ofx:%'"),
            2)
        for aid in ids:
            self.assertNotRegex(aid, r"\d")


class TheChoiceIsAskedOnce(_Household):
    def setUp(self):
        super().setUp()
        self.first = self._old("manual:visa-0123", "Visa …0123")
        self.second = self._old("manual:visa-0123-2", "Visa …0123")

    def test_a_picked_card_is_remembered(self):
        with self.assertRaises(bulk_import.AccountChoiceNeeded):
            self._ensure(CARD_A)
        filename = "Visa 4000111122220123.csv"
        plan = bulk_import.analyze(self.conn, [(filename, csv_for("FUEL"))])
        entry = plan["files"][0]
        self.assertEqual(sorted(c["id"] for c in entry["choices"]),
                         [self.first, self.second])
        res = submit_and_run(self.conn, plan["token"], [
            dict(entry, account_id=self.second, new_account_name="",
                 amount_sign="bank")])[0]
        self.assertTrue(res["ok"], res)
        # asked once: the plan and the door both know the card now
        self.assertEqual(bulk_import.account_choices(self.conn, CARD_A), [])
        self.assertEqual(self._ensure(CARD_A), self.second)
        plan = bulk_import.analyze(self.conn, [(filename, csv_for("FUEL"))])
        self.assertEqual(plan["files"][0]["account_id"], self.second)


class ARenamedAccountStillReceivesItsFile(_Household):
    def test_a_rename_of_both_names_keeps_the_file(self):
        name = "Invented Credit Union.csv"
        _, first = self._plan_and_run(name, csv_for("HARBOR"))
        self.assertTrue(first["ok"], first)
        aid = first["account_id"]
        rows = self._count("SELECT count(*) AS n FROM transactions")
        self.conn.execute(
            "UPDATE accounts SET name = 'Household Reserve',"
            " display_name = 'Rainy Day' WHERE id = %s", (aid,))
        plan, again = self._plan_and_run(name, csv_for("HARBOR"))
        self.assertEqual(plan["files"][0]["account_id"], aid)
        self.assertEqual(again["account_id"], aid)
        self.assertEqual(self._count("SELECT count(*) AS n FROM transactions"),
                         rows)
        # and the typed name the plan offered finds it too
        self.assertEqual(self._ensure("Invented Credit Union"), aid)


class NoNewAccountGetsASuffix(_Household):
    def test_alike_names_get_opaque_ids_with_no_suffix(self):
        made = [bulk_import.create_manual_account(
                    self.conn, n, "depository", "checking")
                for n in ("Checking!", "Checking?", "Checking!")]
        made += [self._ensure("Wallet!"), self._ensure("Wallet?")]
        self.conn.execute("UPDATE accounts SET user_removed_at = now()"
                          " WHERE id = %s", (made[-1],))
        made.append(self._ensure("Wallet?"))       # beside a hidden one
        self.assertEqual(len(set(made)), len(made), made)
        for aid in made:
            self.assertRegex(aid, OPAQUE)
            self.assertIsNone(re.search(r"-\d+$", aid), aid)
        # the typed name the first account-add made is the one it fills
        self.assertEqual(self._ensure("Checking!"), made[0])


class AnAccountMadeUnderItsSlugKeepsItsId(_Household):
    def test_a_reimport_lands_on_the_old_slug_id_and_adds_nothing(self):
        old = self._old("manual:invented-checking", "Invented Checking")
        data = csv_for("CONTOSO")
        _, first = self._plan_and_run("statement.csv", data, account_id=old,
                                      new_account_name="")
        self.assertTrue(first["ok"], first)
        rows = self._count("SELECT count(*) AS n FROM transactions")
        _, again = self._plan_and_run("statement.csv", data, account_id=None,
                                      new_account_name="Invented Checking")
        self.assertEqual(again["account_id"], old)
        self.assertEqual(self._count("SELECT count(*) AS n FROM transactions"),
                         rows)

    def test_one_with_no_name_row_is_found_by_the_slug_walk(self):
        old = "manual:invented-savings-2"
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) VALUES"
            " ('manual:invented-savings','manual','Invented Savings!',"
            "  'depository','savings'),"
            " (%s,'manual','Invented Savings?','depository','savings')",
            (old,))
        self.assertEqual(self._ensure("Invented Savings?"), old)


def _export(conn) -> bytes:
    return export.build_zip(conn)


class TheTableTravelsWithTheHousehold(_Household):
    def _sources(self, conn):
        return conn.execute(
            "SELECT count(*) AS n FROM account_sources").fetchone()["n"]

    def test_a_round_trip_carries_it_and_the_names_still_lead_home(self):
        savings = self._ensure("Invented Savings")
        card = self._ensure(CARD_A, "card")
        data = _export(self.conn)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = z.read("account_sources.csv").decode("utf-8")
        self.assertNotIn("Invented", member)
        self.assertNotIn("4000", member)
        n = self._sources(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        out = restore.restore_zip(dest, data)
        self.assertFalse(out.get("error"), out)
        self.assertEqual(self._sources(dest), n)
        # another household: its secret differs, so the stored names lead
        with dest.transaction():
            self.assertEqual(
                bulk_import._ensure_account(dest, "Invented Savings"),
                savings)
            self.assertEqual(bulk_import._ensure_account(dest, CARD_A),
                             card)
        # and a second restore of the same archive writes nothing
        before = self._sources(dest)
        restore.restore_zip(dest, data)
        self.assertEqual(self._sources(dest), before)

    def test_an_archive_from_before_the_table_gets_its_name_rows(self):
        old = self._old("manual:invented-brokerage", "Invented Brokerage")
        data = _export(self.conn)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, \
                zipfile.ZipFile(buf, "w") as zout:
            for item in zin.infolist():
                if item.filename != "account_sources.csv":
                    zout.writestr(item, zin.read(item.filename))
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, buf.getvalue())
        self.assertEqual(account_sources.by_name(dest, ["Invented Brokerage"]),
                         [old])


class ADeletedAccountTakesItsRows(_Household):
    def test_a_purge_leaves_no_source_row_behind(self):
        from oikonome.sync import base
        aid = self._ensure(CARD_A, "card")
        self.assertTrue(self._count(
            "SELECT count(*) AS n FROM account_sources"))
        base.purge_account_data(self.conn, aid)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM account_sources"), 0)


class TheNameRowNeverDigestsANumber(_Household):
    """A c-row is an unkeyed digest that rides every export: it is taken
    over the name with every full number cut, and it counts only while it
    is the digest of the stored name of the account it names."""

    WHOLE = "Visa 4111.1111.1111.1111"

    def _digests(self):
        return {r["source_hmac"] for r in self.conn.execute(
            "SELECT source_hmac FROM account_sources"
            " WHERE left(source_hmac, 2) = 'c:'")}

    def _whole(self):
        import hashlib
        return "c:" + hashlib.sha256(self.WHOLE.encode()).hexdigest()

    def test_a_name_still_spelling_a_number_gets_no_digest_of_it(self):
        from oikonome.sync import base
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) VALUES"
            " ('manual:old-visa','manual',%s,'credit','credit card')",
            (self.WHOLE,))
        account_sources.backfill(self.conn)
        self.assertNotIn(self._whole(), self._digests())
        base.mask_manual_account_names(self.conn)
        self.assertNotIn(self._whole(), self._digests())
        self.assertEqual(account_sources.by_name(self.conn, [self.WHOLE]),
                         ["manual:old-visa"])

    def test_migrate_rewrites_a_digest_207_took_before_the_cut(self):
        from oikonome.db import migrate
        from oikonome.sync import base
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) VALUES"
            " ('manual:old-visa','manual',%s,'credit','credit card')",
            (self.WHOLE,))
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        admin.execute(MIGRATION.read_text())
        self.assertIn(self._whole(), self._digests())
        # what `oikonome migrate` does after the .sql files: the names are
        # cut, then every name row is made true again
        base.mask_manual_account_names(self.conn)
        migrate.key_numbered_file_rows(admin, APP_PASSWORD)
        self.assertNotIn(self._whole(), self._digests())
        self.assertEqual(account_sources.by_name(self.conn, [self.WHOLE]),
                         ["manual:old-visa"])

    def test_migrate_makes_name_rows_true_only_under_the_household_lock(
            self):
        # migrate runs while the app serves: a purge or a restore holding
        # the household's sync lock must never have a name row written or
        # dropped under it, so a busy household is left for the next
        # migrate, and an idle one is made true in the same visit
        from oikonome.db import migrate
        aid = self._ensure("Invented Savings")
        self.conn.execute("DELETE FROM account_sources WHERE account_id = %s",
                          (aid,))
        stale = account_sources._name_digest("Invented Brokerage")
        self.conn.execute(
            "INSERT INTO account_sources (door, source_hmac, account_id)"
            " VALUES ('manual', %s, %s)", (stale, aid))
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        holder = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(holder.close)
        holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                       (f"oikonome:sync:{tid}",))
        migrate.key_numbered_file_rows(admin, APP_PASSWORD)
        self.assertEqual(self._digests(), {stale})
        holder.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                       (f"oikonome:sync:{tid}",))
        migrate.key_numbered_file_rows(admin, APP_PASSWORD)
        self.assertEqual(self._digests(),
                         {account_sources._name_digest("Invented Savings")})

    def test_a_row_naming_another_name_leads_nowhere(self):
        aid = self._ensure("Invented Savings")
        # a name row an archive brought for a name this account does not
        # carry: no claim on it
        self.conn.execute(
            "INSERT INTO account_sources (door, source_hmac, account_id)"
            " VALUES ('manual', %s, %s)",
            (account_sources._name_digest("Invented Brokerage"), aid))
        self.assertEqual(
            account_sources.by_name(self.conn, ["Invented Brokerage"]), [])
        self.assertEqual(
            account_sources.by_name(self.conn, ["Invented Savings"]), [aid])
        account_sources.backfill(self.conn)
        self.assertEqual(len(self._digests()), 1)


class ANumberOnlyTheSlugReadsIsCutByMigrate(_Household):
    """A hand-made account an older build stored with its number spaced
    by separators only a slug reads as one (underscores, en dashes,
    commas) spells the whole number on every page and in every export.
    `oikonome migrate` must select such a name the way the name rows
    read it, cut it the way the account doors cut it, and write its name
    row in the same visit; a second migrate changes nothing, and a name
    holding no number (a year range is none) is left as typed. Every
    number here is invented."""

    NAMES = {
        # id: (name, display_name, the stored names once cut)
        "manual:inventedunderscor": ("Chase_4000_0000_0000_0002", None,
                                     ("Chase_…0002", None)),
        "manual:inventedendashxxx": ("Chase 4000–0000–0000–0002",
                                     None, ("Chase …0002", None)),
        "manual:inventedrenamedxx": ("Invented Card",
                                     "Card 4000,0000,0000,0002",
                                     ("Invented Card", "Card …0002")),
        "manual:inventedsavingsxx": ("Invented Savings", None,
                                     ("Invented Savings", None)),
        "manual:inventedleaseyrsx": ("Lease 2023–2024", None,
                                     ("Lease 2023–2024", None)),
    }

    def _state(self):
        names = {r["id"]: (r["name"], r["display_name"]) for r in
                 self.conn.execute(
                     "SELECT id, name, display_name FROM accounts"
                     " WHERE id LIKE 'manual:invented%%'")}
        rows = sorted((r["account_id"], r["source_hmac"]) for r in
                      self.conn.execute(
                          "SELECT account_id, source_hmac"
                          " FROM account_sources"
                          " WHERE left(source_hmac, 2) = 'c:'"
                          "   AND account_id LIKE 'manual:invented%%'"))
        return names, rows

    def test_migrate_cuts_it_and_writes_its_name_row_once(self):
        from oikonome.db import migrate
        for aid, (name, shown, _) in self.NAMES.items():
            self.conn.execute(
                "INSERT INTO accounts (id, item_id, name, display_name, type,"
                " subtype) VALUES (%s,'manual',%s,%s,'credit','credit card')",
                (aid, name, shown))
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        migrate.key_numbered_file_rows(admin, APP_PASSWORD)
        names, rows = self._state()
        self.assertEqual(names, {aid: cut for aid, (_, _, cut)
                                 in self.NAMES.items()})
        self.assertEqual(rows, sorted(
            (aid, account_sources._name_digest(cut[0]))
            for aid, (_, _, cut) in self.NAMES.items()))
        migrate.key_numbered_file_rows(admin, APP_PASSWORD)
        self.assertEqual(self._state(), (names, rows))

    def test_a_rename_is_cut_the_same_way(self):
        # the rename door stores what migrate would leave, so the page
        # never shows a number the next migrate would cut
        from oikonome.web import data
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) VALUES"
            " ('manual:inventedrenamedxx','manual','Invented Card','credit',"
            "  'credit card')")
        for typed, shown in (("Card_4000_0000_0000_0002", "Card_…0002"),
                             ("Card 4000\u20130000\u20130000\u20130002",
                              "Card …0002"),
                             ("Lease 2023\u20132024", "Lease 2023\u20132024")):
            self.assertEqual(data.set_account_display_name(
                self.conn, "manual:inventedrenamedxx", typed), 1)
            self.assertEqual(self.conn.execute(
                "SELECT display_name FROM accounts"
                " WHERE id = 'manual:inventedrenamedxx'"
            ).fetchone()["display_name"], shown, typed)


class Migration207(_Household):
    def _all(self, admin):
        return admin.execute(
            "SELECT tenant_id::text, door, source_hmac, account_id"
            "  FROM account_sources ORDER BY 1, 2, 3, 4").fetchall()

    def test_it_backfills_once_and_a_second_run_changes_nothing(self):
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype) VALUES"
            " ('manual:invented-line','manual','Invented  Line ',"
            "  'depository','checking')")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        admin.execute(MIGRATION.read_text())
        once = self._all(admin)
        admin.execute(MIGRATION.read_text())
        self.assertEqual(self._all(admin), once)
        self.assertEqual(account_sources.by_name(self.conn, ["Invented Line"]),
                         ["manual:invented-line"])
        # the Python twin a restore runs agrees with it: nothing to add
        self.assertEqual(account_sources.backfill(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
