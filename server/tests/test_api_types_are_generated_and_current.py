"""The clients' API types are generated from the server and kept current.

specs/openapi.json is the server's client-facing doors with their
documented shapes; scripts/gen-api-types.py writes it and, through the
pinned generator in tools/api-types, the TypeScript both clients compile
against (specs/api-types.md). Pinned here:

* the checked-in schema and both .ts copies are what the generator writes
  now — a documented model, a route or a parameter changed without a
  regenerate fails, and the two copies are byte-identical;
* the shapes only DESCRIBE: no route has a response_model and no handler a
  return annotation (FastAPI would turn either into a filter that drops
  undeclared keys and coerces values — a wire change for an installed
  phone);
* an add-on's doors never reach the public file;
* the doors left out of the clients' types are a pinned list;
* the generator is pinned to exact versions with a lockfile.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts" / "gen-api-types.py"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_api_types", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    from oikonome.web.app import app
    return app


class GeneratedFilesAreCurrent(unittest.TestCase):
    def test_schema_and_both_copies_are_what_the_generator_writes(self):
        # a fresh process: the generator imports the app as a plain
        # instance, which this process (add-ons and all) may not be
        r = subprocess.run([sys.executable, str(GEN), "--check"],
                           capture_output=True, text=True, cwd=str(ROOT))
        if r.returncode == 2 and shutil.which("node") is None:
            self.skipTest("node not installed: " + r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_two_copies_are_byte_identical(self):
        g = _generator()
        web, mobile = (p.read_bytes() for p in g.TS_TARGETS)
        self.assertEqual(web, mobile)
        self.assertTrue(web.startswith(g.HEADER.encode()),
                        "the generated file lost its do-not-edit header")


class ShapesOnlyDescribe(unittest.TestCase):
    def test_no_route_filters_or_coerces_its_response(self):
        g = _generator()
        filtered, annotated = [], []
        for r in g._flat_routes(_app().routes):
            where = f"{sorted(r.methods)} {r.path}"
            if r.response_model is not None:
                filtered.append(where)
            if (inspect.signature(r.endpoint).return_annotation
                    is not inspect.Signature.empty):
                annotated.append(where)
        self.assertEqual(filtered, [], "a response_model filters the reply")
        self.assertEqual(annotated, [],
                         "FastAPI turns a return annotation into a "
                         "response_model; document the shape with "
                         "@documented(response=...) instead")


class TheSchemaIsThePlainInstance(unittest.TestCase):
    def test_an_addon_router_is_refused(self):
        g = _generator()
        app = _app()
        from oikonome import ext
        with mock.patch.object(ext, "routers", return_value=[object()]):
            with self.assertRaises(g.AddonRoutesPresent):
                g.build_schema(app)

    def test_the_left_out_doors_are_a_pinned_list(self):
        g = _generator()
        self.assertEqual(g.EXCLUDED_MODULES, frozenset({
            "oikonome.web.testing",
            "oikonome.web.plaid_webhook",
            "oikonome.web.postmark_webhook",
        }))
        # and every entry still names a module with /api doors
        mods = {r.endpoint.__module__ for r in g._flat_routes(_app().routes)
                if r.path.startswith("/api/")}
        self.assertEqual(g.EXCLUDED_MODULES - mods, frozenset())

    def test_the_schema_holds_only_api_doors(self):
        schema = json.loads((ROOT / "specs" / "openapi.json").read_text())
        self.assertTrue(schema["paths"])
        self.assertEqual([p for p in schema["paths"]
                          if not p.startswith("/api/")], [])
        for p, ops in schema["paths"].items():
            for op in ops.values():
                self.assertNotIn("description", op, p)
                self.assertNotIn("summary", op, p)


class TheGeneratorIsPinned(unittest.TestCase):
    def test_exact_versions_and_a_lockfile_that_agrees(self):
        tool = ROOT / "tools" / "api-types"
        pkg = json.loads((tool / "package.json").read_text())
        lock = json.loads((tool / "package-lock.json").read_text())
        deps = pkg["devDependencies"]
        self.assertEqual(set(deps), {"openapi-typescript", "typescript"})
        for name, ver in deps.items():
            self.assertRegex(ver, r"^\d+\.\d+\.\d+$",
                             f"{name} must be pinned to an exact version")
            self.assertEqual(
                lock["packages"][f"node_modules/{name}"]["version"], ver,
                f"the lockfile's {name} differs from package.json")


if __name__ == "__main__":
    unittest.main()
