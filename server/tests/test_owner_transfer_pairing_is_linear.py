"""Pairing a business's transfers into "internal" moves reads only the legs
of the same amount: the result is the one a scan of every pair gives, and
a long history is paired in well under a second."""
import datetime as dt
import random
import time
import unittest

from oikonome.engine import books

D0 = dt.date(2026, 3, 1)


def _t(tid, amount, day, acct):
    return {"id": tid, "amount": amount, "date": D0 + dt.timedelta(days=day),
            "category": "TRANSFER_OUT" if amount > 0 else "TRANSFER_IN",
            "cat_original": "TRANSFER", "cat_detailed": "",
            "paid_personally": False, "account_id": acct}


def _reference(rows):
    """Every out against every in, the first match in row order."""
    for t in rows:
        t["role"] = books._role(t)
    outs = [t for t in rows if t["role"] == "owner_out"]
    ins = [t for t in rows if t["role"] == "owner_in"]
    used = set()
    for o in outs:
        for i in ins:
            if i["id"] in used or i["account_id"] == o["account_id"]:
                continue
            if (abs(abs(i["amount"]) - abs(o["amount"])) < 0.005
                    and abs((i["date"] - o["date"]).days) <= 5):
                o["role"] = i["role"] = "internal"
                used.add(i["id"])
                break
    return {t["id"]: t["role"] for t in rows}


class PairingTests(unittest.TestCase):
    def test_a_small_fixture(self):
        rows = [_t("o1", 250.0, 0, "chk"), _t("i1", -250.0, 2, "sav"),
                _t("o2", 80.0, 0, "chk"), _t("i2", -80.0, 9, "sav"),
                _t("o3", 40.0, 0, "chk"), _t("i3", -40.0, 1, "chk"),
                _t("o4", 19.996, 3, "chk"), _t("i4", -20.0, 3, "sav")]
        roles = {t["id"]: t["role"] for t in books.row_roles(rows)}
        self.assertEqual(roles, {
            "o1": "internal", "i1": "internal",
            # too far apart in time
            "o2": "owner_out", "i2": "owner_in",
            # the same account is not a move between two of them
            "o3": "owner_out", "i3": "owner_in",
            # within half a cent, across a cent boundary
            "o4": "internal", "i4": "internal"})

    def test_matches_the_full_scan_on_random_histories(self):
        rng = random.Random(7)
        for _ in range(40):
            rows = []
            for n in range(60):
                amt = rng.choice([10.0, 10.004, 9.997, 25.0, 61.5, 300.0])
                rows.append(_t(f"r{n}", amt if rng.random() < .5 else -amt,
                               rng.randrange(20), rng.choice("abc")))
            want = _reference([dict(t) for t in rows])
            got = {t["id"]: t["role"] for t in books.row_roles(rows)}
            self.assertEqual(got, want)

    def test_six_thousand_each_way_is_fast(self):
        rows = [_t(f"o{n}", 100.0 + n, n % 300, "chk") for n in range(6000)]
        rows += [_t(f"i{n}", -(100.0 + n * 1.01), n % 300, "sav")
                 for n in range(6000)]
        t0 = time.perf_counter()
        books.row_roles(rows)
        self.assertLess(time.perf_counter() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
