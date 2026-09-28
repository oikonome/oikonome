"""Display parity lints over the client SOURCES (web + mobile), the shape of
the pinned-route inventories: cheap, deterministic, and they catch the
class of bug that a one-fixture render cannot — a display transform
copied inline in a 25th place, a raw key rendered where a label belongs.

  * underscore→space lives in ONE helper per client (catLabel); nowhere
    else may spell `.replace(/_/g` (a copied transform is the one a new
    screen forgets);
  * a ledger row's `category` is a LABEL — no client may compare it to a
    raw key with === (the picker would never highlight);
  * the generated Txn is the only Txn.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLIENTS = {"web": ROOT / "webapp" / "src", "mobile": ROOT / "mobile" / "src"}
HELPERS = {"web": "api/client.ts", "mobile": "lib/pure.ts"}


def _sources(base: pathlib.Path):
    for f in sorted(base.rglob("*.ts*")):
        if ".generated." in f.name or f.name.endswith(".d.ts"):
            continue
        yield f


class DisplayParityTests(unittest.TestCase):
    def test_underscore_to_space_lives_in_one_helper_per_client(self):
        offenders = []
        for name, base in CLIENTS.items():
            for f in _sources(base):
                rel = str(f.relative_to(base))
                if rel == HELPERS[name]:
                    continue
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    # a base64url decode legitimately swaps _ for / — that
                    # is not a label transform
                    if ".replace(/_/g" in line and "\"/\"" not in line and "'/'" not in line:
                        offenders.append(f"{name}:{rel}:{i}")
        self.assertEqual(offenders, [], "spell it as catLabel(): " + ", ".join(offenders))

    def test_no_client_compares_a_row_category_to_a_raw_key(self):
        # ledger rows go by t / txn / row / r in both clients; a budget
        # candidate's `c.category` is a KEY and may be compared as one
        pat = re.compile(r"===\s*\b(t|txn|row)\.category\b|\b(t|txn|row)\.category\s*===")
        offenders = []
        for name, base in CLIENTS.items():
            for f in _sources(base):
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if pat.search(line) and "catLabel(" not in line:
                        offenders.append(f"{name}:{f.relative_to(base)}:{i}")
        self.assertEqual(offenders, [], "compare as labels (catLabel both sides): "
                         + ", ".join(offenders))

    def test_only_the_generated_txn_exists(self):
        for name, base in CLIENTS.items():
            for f in _sources(base):
                if "export interface Txn {" in f.read_text(encoding="utf-8"):
                    self.fail(f"{name}:{f.relative_to(base)} hand-writes Txn — it is generated")


if __name__ == "__main__":
    unittest.main()
