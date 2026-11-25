"""A connection with no institution name is listed as "Other", in a group
of its own: an institution that is literally named "Other" stays a
separate row beside it (specs/counted-figures.md, "A connection with no
institution name is "Other", in a group of its own").

The grouping is decided once, in reporting.compute_networth; the ring's
shown rows follow its rows in order, and the Accounts page counts the two
as two institutions. No balance or total moves.
"""

import unittest

from oikonome.engine import reporting

from .util import make_db


class AnUnnamedInstitutionIsItsOwnOther(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        c = self.conn
        c.execute("INSERT INTO items (id, aggregator, institution_name, "
                  "access_token) VALUES ('it-none','test',NULL,'t1'), "
                  "('it-blank','test','','t2'), ('it-other','test','Other','t3')")
        c.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                  "balance_current) VALUES "
                  "('a-none','it-none','Jar','depository','savings',300), "
                  "('a-blank','it-blank','Tin','depository','savings',25), "
                  "('a-other','it-other','Other Co Checking','depository',"
                  "'checking',1200)")

    def tearDown(self):
        self.conn.close()

    def test_net_worth_keeps_the_two_others_apart(self):
        nw = reporting.compute_networth(self.conn, trend=False)
        others = sorted(v for k, v in nw["by_institution"] if k == "Other")
        # the unnamed connections together, the named one alone
        self.assertEqual(others, [325.0, 1200.0])
        self.assertEqual(round(sum(v for _k, v in nw["by_institution"]), 2),
                         nw["current_total"])
        ring = nw["rings"]["by_institution"]["shown"]["rows"]
        self.assertEqual([r["label"] for r in ring],
                         [k for k, _v in nw["by_institution"]])
        self.assertEqual(sorted(r["dollars"] for r in ring
                                if r["label"] == "Other"), [325, 1200])

    def test_the_accounts_page_counts_them_as_two_institutions(self):
        totals = reporting.account_totals(self.conn)
        # Test Bank (the fixture's), the unnamed connections, and "Other"
        self.assertEqual(totals["institution_count"], 3)


if __name__ == "__main__":
    unittest.main()
