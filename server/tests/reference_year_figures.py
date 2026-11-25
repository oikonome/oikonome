"""Run tests/year_households.figures under ANOTHER checkout's code:

    python tests/reference_year_figures.py <reference server dir> <kind> <seed>…

prints one JSON line per seed, {"seed": n, "figures": {…}}, for the
households `kind` names (year_households.build). The reference
checkout's own `oikonome` package and its `tests` helpers are imported;
only the generator module is this checkout's, loaded by path, so both
sides build the same household from the same seed
(test_the_year_by_occurrence_moves_no_right_figure).
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
        "tests.year_households", here / "year_households.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tests.year_households"] = mod
    spec.loader.exec_module(mod)
    import oikonome
    assert str(pathlib.Path(oikonome.__file__).resolve()).startswith(ref), \
        oikonome.__file__
    for raw in argv[3:]:
        conn, today, close = mod.build(argv[2], int(raw))
        try:
            print(json.dumps({"seed": int(raw),
                              "figures": mod.figures(conn, today)}),
                  flush=True)
        finally:
            close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
