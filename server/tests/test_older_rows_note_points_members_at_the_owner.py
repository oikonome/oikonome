"""The import's "older rows left out" note points each person at a door
they can open.

The history window is the owner's to change: the server refuses anyone
else (permissions.OWNER_REFUSED_SETTINGS) and the Accounts page shows a
member or viewer a read-only line. So both clients' note tells the owner
where to widen it and everyone else that the owner can — the web copy
once sent a member to a control that could only answer 403.
"""

import os
import pathlib
import unittest

import oikonome

ROOT = pathlib.Path(os.environ.get(
    "OIKONOME_CLIENT_ROOT", pathlib.Path(oikonome.__file__).parent.parent.parent))

NOTES = {"web": "webapp/src/pages/Import.tsx",
         "phone": "mobile/src/app/imports.tsx"}


def _note(rel):
    try:
        src = (ROOT / rel).read_text()
    except FileNotFoundError:
        raise unittest.SkipTest(f"{rel} not present")
    start = src.index("function OlderRowsNote(")
    return src[start:src.index("\n}\n", start)]


class OlderRowsNoteTwins(unittest.TestCase):

    def test_both_notes_branch_on_the_owner(self):
        for name, rel in NOTES.items():
            note = _note(rel)
            self.assertIn("{owner", note, name)
            self.assertIn('"the household owner can widen it"', note, name)

    def test_only_the_owner_branch_names_the_control(self):
        for name, rel in NOTES.items():
            note = _note(rel)
            owner_branch = note[note.index("{owner"):
                                note.index('"the household owner can')]
            self.assertIn("widen it on", owner_branch, name)
            self.assertNotIn("widen it on",
                             note[:note.index("{owner")], name)


if __name__ == "__main__":
    unittest.main()
