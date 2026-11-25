"""Run tests/envelope_households.figures under ANOTHER checkout's code:

    python tests/reference_figures.py <reference server dir> <seed> [<seed>…]

prints one JSON line per seed, {"seed": n, "figures": {…}}. The
reference checkout's own `oikonome` package and its `tests` helpers are
imported (its tests.util builds the household into its own test
database, OIKONOME_TEST_DB as the caller sets it); only the generator
module is this checkout's, loaded by path, so both sides build the same
household from the same seed. test_envelopes_move_nothing_but_the_split
runs it with the code from before the Envelopes row to show that nothing
but the printed split moved.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys


def main(argv: list[str]) -> int:
    ref = str(pathlib.Path(argv[1]).resolve())
    here = pathlib.Path(__file__).resolve().parent
    sys.path = [ref] + [p for p in sys.path
                        if pathlib.Path(p or ".").resolve() != here.parent]
    import tests  # noqa: F401 — the reference checkout's helpers package
    spec = importlib.util.spec_from_file_location(
        "tests.envelope_households", here / "envelope_households.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tests.envelope_households"] = mod
    spec.loader.exec_module(mod)
    import oikonome
    assert str(pathlib.Path(oikonome.__file__).resolve()).startswith(ref), \
        oikonome.__file__
    for raw in argv[2:]:
        h = mod.EnvelopeHousehold(int(raw))
        try:
            print(json.dumps({"seed": int(raw),
                              "figures": mod.figures(h.conn)}), flush=True)
        finally:
            h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
