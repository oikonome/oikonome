"""A forwarded-but-unset numeric knob must not crash the container.

compose materialises EVERY declared key, so a knob nobody set arrives as ""
rather than absent — and `os.environ.get(name, default)` returns the default
only when the name is MISSING — so a forwarded, unset knob becomes
`int('')` at import time and the app container crash-loops.

Every numeric knob the container declares therefore has to treat an empty
string as unset.
"""

import os
import unittest


class EnvNumberTests(unittest.TestCase):

    KNOBS = {
        "OIKONOME_PLAID_WEBHOOK_NEG_TTL": ("oikonome.web.plaid_webhook",
                                           "_NEG_TTL", 60.0),
        "OIKONOME_PLAID_WEBHOOK_FETCH_CEILING": ("oikonome.web.plaid_webhook",
                                                 "_FETCH_CEILING", 4),
        "OIKONOME_RECEIPT_MAX_OPTIMIZE_PX": ("oikonome.engine.receipts",
                                             "MAX_OPTIMIZE_PX", 12000000),
        "OIKONOME_RECEIPT_OPTIMIZE_WORK_PX": ("oikonome.engine.receipts",
                                              "_OPTIMIZE_WORK_PX", 2400),
    }

    def test_empty_is_treated_as_unset(self):
        import importlib
        saved = {k: os.environ.get(k) for k in self.KNOBS}
        try:
            for k in self.KNOBS:
                os.environ[k] = ""
            for k, (mod, attr, want) in self.KNOBS.items():
                m = importlib.reload(importlib.import_module(mod))
                self.assertEqual(getattr(m, attr), want, k)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for mod in {m for m, _, _ in self.KNOBS.values()}:
                importlib.reload(importlib.import_module(mod))

    def test_a_real_value_still_wins(self):
        import importlib
        saved = os.environ.get("OIKONOME_RECEIPT_OPTIMIZE_WORK_PX")
        try:
            os.environ["OIKONOME_RECEIPT_OPTIMIZE_WORK_PX"] = "1200"
            m = importlib.reload(
                importlib.import_module("oikonome.engine.receipts"))
            self.assertEqual(m._OPTIMIZE_WORK_PX, 1200)
        finally:
            if saved is None:
                os.environ.pop("OIKONOME_RECEIPT_OPTIMIZE_WORK_PX", None)
            else:
                os.environ["OIKONOME_RECEIPT_OPTIMIZE_WORK_PX"] = saved
            importlib.reload(importlib.import_module("oikonome.engine.receipts"))

    def test_whitespace_is_also_unset(self):
        # the helper now lives in ONE place (oikonome.envnum) — it had five
        # independent restatements across the codebase, which is how the
        # sixth caller reintroduces the crash this file exists to prevent
        from oikonome.envnum import env_num
        self.assertEqual(env_num("OIKONOME_NOT_SET_ANYWHERE", "7"), "7")
        os.environ["OIKONOME_TMP_KNOB"] = "   "
        try:
            self.assertEqual(env_num("OIKONOME_TMP_KNOB", "7"), "7")
        finally:
            os.environ.pop("OIKONOME_TMP_KNOB", None)
