"""Any JSON API module can be the first thing a process imports.

The area modules (`web/api_<area>.py`) register their routes as they are
imported, and each needs the signed-in-user dependency to do it. When that
dependency had to be fetched from `web.app` at import time, importing an
area module before the app re-entered the module half-built: the import
failed, and had it not, the app would have mounted a router without that
module's routes, or with them out of order (Starlette answers with the
first pattern that matches).

So each module is imported first in a fresh interpreter, then the app, and
the app's route table must be the one a process that imports the app first
gets: every route, in the same order.
"""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
import subprocess
import sys
import unittest

SERVER = pathlib.Path(__file__).resolve().parent.parent
WEB = SERVER / "oikonome" / "web"

_PROBE = """
import importlib, json, sys
first = sys.argv[1]
if first:
    importlib.import_module("oikonome.web." + first)
from oikonome.web import app as appmod
from tests.util import all_routes
print(json.dumps([[r.path, sorted(r.methods)]
                  for r in all_routes(appmod.app)]))
"""


def _route_table(first: str) -> tuple[int, str, list]:
    done = subprocess.run([sys.executable, "-c", _PROBE, first],
                          cwd=SERVER, capture_output=True, text=True,
                          timeout=300)
    table = []
    if done.returncode == 0:
        table = json.loads(done.stdout.strip().splitlines()[-1])
    return done.returncode, done.stderr[-600:], table


class EachApiModuleImportsFirst(unittest.TestCase):

    def test_importing_any_api_module_first_builds_the_same_app(self):
        modules = sorted(p.stem for p in WEB.glob("api*.py"))
        self.assertIn("api_common", modules)
        self.assertGreater(len(modules), 10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(zip([""] + modules,
                               pool.map(_route_table, [""] + modules)))
        code, err, expected = results.pop("")
        self.assertEqual(code, 0, err)
        self.assertGreater(len(expected), 200)
        for name, (code, err, table) in results.items():
            with self.subTest(first=name):
                self.assertEqual(code, 0, f"import {name} first: {err}")
                self.assertEqual(table, expected,
                                 f"importing {name} first changed the app's "
                                 "routes or their order")


if __name__ == "__main__":
    unittest.main()
