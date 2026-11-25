"""A restored forecast night is one the instance could have kept itself.

The instance keeps one night per household-local day, and the first row of
a day stands. A restored row dated today or later would stand in for the
night the instance is about to keep — that night's record would never be
written, and the grading would read the source's forecast as this one's.
And the walks
are the shape the record writes (each scenario's balances at every
horizon), so no reader meets a shape it cannot read. Figures and names
are invented.
"""

import csv
import datetime as dt
import io
import json
import unittest
import uuid
import zipfile

from oikonome import localtime
from oikonome.db import tenancy
from oikonome.engine import forecast, forecast_score
from oikonome.sync import export, restore

from .test_the_forecast_keeps_what_it_said import _household
from .util import TODAY, _ensure_db


def _with_nights(data: bytes, rows: list[dict]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            body = zin.read(info)
            if info.filename.endswith("forecast_nights.csv"):
                buf = io.StringIO()
                w = csv.DictWriter(buf, list(rows[0]), lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
                body = buf.getvalue().encode()
            zout.writestr(info, body)
    return out.getvalue()


class ARestoredNight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _household()
        try:
            forecast.forget_learned()
            forecast_score.record(src, TODAY)
            cls.data = export.build_zip(src)
        finally:
            src.close()
        with zipfile.ZipFile(io.BytesIO(cls.data)) as z:
            name = next(n for n in z.namelist()
                        if n.endswith("forecast_nights.csv"))
            cls.row = next(csv.DictReader(io.StringIO(z.read(name).decode())))

    def _restore(self, rows):
        _ensure_db()
        admin = tenancy.admin_connect()
        tid = tenancy.create_tenant(admin, f"fn-{uuid.uuid4().hex[:8]}")
        admin.close()
        dest = tenancy.tenant_connect(tid)
        self.addCleanup(dest.close)
        self.counts, _out = restore.restore_outcomes(
            dest, _with_nights(self.data, rows))
        self.dest = dest
        today = localtime.household_day(dest)
        return today, {r["as_of"] for r in dest.execute(
            "SELECT as_of FROM forecast_nights")}

    def _night(self, day, **k):
        return dict(self.row, as_of=day.isoformat(), **k)

    def test_today_and_later_are_not_restored(self):
        today = localtime.now_local({}).date()
        days = [today - dt.timedelta(days=1), today,
                today + dt.timedelta(days=1), today + dt.timedelta(days=30)]
        got_today, got = self._restore([self._night(d) for d in days])
        self.assertEqual(got_today, today)
        self.assertEqual(got, {today - dt.timedelta(days=1)})
        # and the result names no damage: the instance keeps its own today
        from oikonome.web.pages import restore_result
        said = " ".join(restore_result(self.counts)["warnings"] or [])
        self.assertNotIn("larger than any real sum", said)
        self.assertNotIn("out of range", " ".join(
            str(x) for x in restore_result(self.counts).values()))

    def test_input_events_of_another_shape_land_without_events(self):
        day = localtime.now_local({}).date() - dt.timedelta(days=3)
        inputs = json.loads(self.row["inputs"])
        good = [[3, -4200, "Willow rent", "bill", None],
                [9, -1500, "Willow card", "card_bill", "card1"]]
        cases = {
            "good": good,
            "not a list": "x",
            "short item": [[3, -4200, "Willow rent", "bill"]],
            "bool offset": [[True, -4200, "Willow rent", "bill", None]],
            "text cents": [[3, "-4200", "Willow rent", "bill", None]],
            "number card": [[3, -4200, "Willow rent", "bill", 7]],
            "too many": [[3, -1, "Willow", "bill", None]] * 5000,
        }
        rows = [self._night(day - dt.timedelta(days=i), inputs=json.dumps(
            dict(inputs, events=ev))) for i, ev in enumerate(cases.values())]
        _t, got = self._restore(rows)
        self.assertEqual(len(got), len(cases))
        landed = {r["as_of"]: r["inputs"] for r in self.dest.execute(
            "SELECT as_of, inputs FROM forecast_nights")}
        for i, name in enumerate(cases):
            with self.subTest(case=name):
                ev = landed[day - dt.timedelta(days=i)].get("events")
                self.assertEqual(ev, good if name == "good" else None)

    def test_walks_of_another_shape_are_not_restored(self):
        good = json.loads(self.row["walks"])
        at = good["usual"]["at"]
        bad = [
            {"usual": "x"},
            {"usual": dict(good["usual"], at="1,2")},
            {"usual": dict(good["usual"], at=at[:-1])},
            {"usual": dict(good["usual"], at=[True] + at[1:])},
            {"usual": dict(good["usual"], at=["12"] + at[1:])},
            {"usual": dict(good["usual"], at=[10 ** 30] + at[1:])},
            {"usual": dict(good["usual"], min="0")},
            {"usual": dict(good["usual"], min_day=1.5)},
            {"usual": dict(good["usual"], cards_out=[1])},
            {k: v for k, v in good.items() if k != "usual"},
        ]
        start = localtime.now_local({}).date() - dt.timedelta(days=60)
        rows = [self._night(start + dt.timedelta(days=i),
                            walks=json.dumps(w)) for i, w in enumerate(bad)]
        fine = start - dt.timedelta(days=1)
        rows.append(self._night(fine))
        _t, got = self._restore(rows)
        self.assertEqual(got, {fine})


if __name__ == "__main__":
    unittest.main()
