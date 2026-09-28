"""A "First-ever charge" alert asks the person to go and check a charge, so
it must only fire on a payee the ledger has genuinely never seen.

The alert's payee is the row's identity key — the outlet a chain's fuel arm
was identified as, else the aggregator's merchant name, else the bank's own
line. If the search for a prior charge asks a DIFFERENT question, every row
whose two spellings differ reads as first-ever: a pump under a brand, a
payee that gained a name or a merge, a line the aggregator named one-off and
the resolver moved that name off. Each of those is a payee with a long
history, and an alert on it is noise the person learns to ignore — which is
the failure that matters, because the next one is fraud.
"""
import datetime as dt
import unittest

from oikonome.engine import anomalies

from .util import TODAY, add_txn, make_db, write_config

LONG_AGO = TODAY - dt.timedelta(days=200)


class FirstEverChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def detect(self):
        return anomalies.detect(self.conn, TODAY)

    def _merchant(self, name: str) -> str:
        return self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES (%s, 'manual')"
            " RETURNING id", (name,)).fetchone()["id"]

    def _set(self, txn_id: str, **cols) -> None:
        sets = ", ".join(f"{c} = %s" for c in cols)
        self.conn.execute(f"UPDATE transactions SET {sets} WHERE id = %s",
                          (*cols.values(), txn_id))

    def test_a_habitual_fuel_outlet_is_not_a_first_ever_charge(self):
        """A pump identified as a chain's fuel arm keys on the OUTLET, while
        the aggregator goes on calling every one of its rows the brand. Ask
        for a prior charge under the brand and a tank of fuel bought monthly
        for years alerts every time it is a large one."""
        for n in range(4):
            prior = add_txn(self.conn, LONG_AGO + dt.timedelta(days=30 * n),
                            52.00, "HARBOR LIGHTS #221",
                            merchant="Harbor Lights")
            self._set(prior, merchant_outlet="Harbor Lights Fuel")
        big = add_txn(self.conn, TODAY, 260.00, "HARBOR LIGHTS #221",
                      merchant="Harbor Lights")
        self._set(big, merchant_outlet="Harbor Lights Fuel")
        self.assertEqual([m for m in self.detect() if "First-ever" in m], [])

    def test_a_new_bank_line_for_a_filed_merchant_is_not_a_first_ever_charge(self):
        """The bank restates a payee's descriptor — a store number appears, a
        rename lands, a merge puts two spellings under one name. The row is
        filed under the merchant either way, so its history is right there;
        only a search that ignores what the row is filed under misses it."""
        mid = self._merchant("Juniper Market")
        prior = add_txn(self.conn, LONG_AGO, 41.00, "JUNIPER MARKET",
                        merchant="Juniper Market")
        self._set(prior, merchant_id=mid)
        big = add_txn(self.conn, TODAY, 310.00, "JUNIPER MKT 0148")
        self._set(big, merchant_id=mid)
        self.assertEqual([m for m in self.detect() if "First-ever" in m], [])

    def test_a_set_aside_name_leaves_the_charge_on_its_habitual_line(self):
        """A line the household has used for years gets one charge the
        aggregator names something the line does not say; the resolver files
        it under the merchant the line means and moves that name aside, so
        the row reads as one the aggregator never named. Its payee is then
        the bank line — while the habit rows on that same line answer to the
        aggregator's usual name — and nothing but the merchant it is filed
        under connects the two."""
        mid = self._merchant("Juniper Market")
        for n in range(5):
            prior = add_txn(self.conn, LONG_AGO + dt.timedelta(days=20 * n),
                            23.00, "JUNIPER MARKET SPRINGFIELD",
                            merchant="Juniper Market")
            self._set(prior, merchant_id=mid)
        big = add_txn(self.conn, TODAY, 240.00, "JUNIPER MARKET SPRINGFIELD")
        self._set(big, merchant_id=mid, merchant_name=None,
                  merchant_name_set_aside="Juniper Market Cafe")
        self.assertEqual([m for m in self.detect() if "First-ever" in m], [])

    def test_the_alert_names_the_payee_as_the_ledger_shows_it(self):
        """An identity key is sometimes a descriptor nobody has ever read.
        Asked to recognize a charge, the person is told what the charge looks
        like on the page they would go and look at."""
        mid = self._merchant("Harbor Lights Market")
        big = add_txn(self.conn, TODAY, 420.00, "HRBR LGHTS MKT 8891")
        self._set(big, merchant_id=mid)
        msgs = [m for m in self.detect() if "First-ever" in m]
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Harbor Lights Market", msgs[0])
        self.assertNotIn("HRBR LGHTS", msgs[0])

    def test_a_payee_the_ledger_has_never_seen_still_alerts(self):
        """The whole point of the alert survives all of the above: an outlet
        row, a filed merchant, neither with a charge behind it, is exactly
        the fraud signature this watch exists for."""
        mid = self._merchant("Ridgeway Supply")
        big = add_txn(self.conn, TODAY, 450.00, "RIDGEWAY SUPPLY CO")
        self._set(big, merchant_id=mid, merchant_outlet="Ridgeway Supply Yard")
        msgs = [m for m in self.detect() if "First-ever" in m]
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Ridgeway Supply", msgs[0])


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
