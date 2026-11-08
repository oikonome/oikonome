"""Budget-page list saves: one queue, and a save never eats later typing.

POST /api/settings replaces savings_goals and custom_buckets whole. Each
Budget card guards its own write by reading the server fresh, merging and
posting the whole list — two round trips nothing makes atomic. Two cards'
saves in flight together (the planner and Savings goals, the planner and
Bucket rules) each read the same base, and the later POST erases the
other's rows. Every such save therefore rides the one shared mutation
queue (`scope: SETTINGS_SAVES`) that Settings and Welcome already use, so
each save's read runs after the previous save's write.

And a Savings-goals save that lands after the person kept typing must not
replace the rows with the saved view: the typing since is unsaved, and
wiping it reads as the app throwing input away. Web and mobile both.
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent

_CARDS = ["webapp/src/components/BudgetPlanner.tsx",
          "webapp/src/components/SavingsGoalsCard.tsx",
          "webapp/src/components/BudgetFineTune.tsx",
          "mobile/src/app/budget.tsx"]


def _mutations(src: str):
    """Each `useMutation({...})` object literal, with its line number."""
    for m in re.finditer(r"useMutation\(\{", src):
        i, depth = m.end(), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        yield src[:m.start()].count("\n") + 1, src[m.start():i]


def unscoped_settings_saves(rel: str, src: str) -> list[str]:
    return [f"{rel}:{line}" for line, body in _mutations(src)
            if "saveSettings(" in body
            and "scope: SETTINGS_SAVES" not in body]


def goals_save_keeps_inflight_edits(src: str) -> bool:
    """The goals save sends the rows as its variables and replaces them on
    success only when they are still the very array it sent."""
    return ("rowsNow.current !== sent" in src
            and "if (!edited) setRows(goals.map(goalRow))" in src
            and "save.mutate(rows)" in src)


class BudgetListSavesShareOneQueue(unittest.TestCase):
    def _read(self, rel):
        p = _APP / rel
        if not p.exists():
            self.skipTest(f"{rel} not present")
        return p.read_text()

    def test_every_settings_save_in_either_client_rides_the_shared_queue(self):
        """The settings door swaps lists whole, so any two saves in flight
        together can erase each other; every screen that saves settings
        queues on the one scope, not only the budget cards."""
        bad = []
        for pat in ("webapp/src/**/*.tsx", "mobile/src/**/*.tsx"):
            for p in sorted(_APP.glob(pat)):
                bad += unscoped_settings_saves(
                    str(p.relative_to(_APP)), p.read_text())
        self.assertEqual(bad, [], "settings saves outside SETTINGS_SAVES")

    def test_every_fresh_read_list_save_rides_the_shared_queue(self):
        bad = []
        for rel in _CARDS:
            bad += unscoped_settings_saves(rel, self._read(rel))
        self.assertEqual(bad, [], "settings saves outside SETTINGS_SAVES")

    def test_a_goals_save_never_replaces_rows_typed_while_in_flight(self):
        for rel in ("webapp/src/components/SavingsGoalsCard.tsx",
                    "mobile/src/app/budget.tsx"):
            self.assertTrue(goals_save_keeps_inflight_edits(self._read(rel)),
                            rel)


if __name__ == "__main__":
    unittest.main()
