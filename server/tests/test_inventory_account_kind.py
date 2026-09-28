"""Inventory: nothing compares an account's `kind` with bare equality.

`/api/accounts` does not serve a bare Plaid type. `web/data.py` builds the
field as ``COALESCE(a.type,'') || '/' || COALESCE(a.subtype,'')``, so every
real account arrives as ``"depository/checking"``, ``"credit/credit card"``,
``"loan/student"``. A test of the form ``kind === "depository"`` therefore
matches NOTHING — not "usually", not "for odd subtypes": nothing, ever.

That is a silent failure, which is what makes it worth a lint. The shape
it takes: a business tax set-aside card that sums cash over an
always-empty filter reports $0 in the business and tells the user "Short by
<the whole estimated tax bill>" however well funded they are — the one
number the card exists to answer. An Overview cash tile, or a wizard's
attach-accounts step whose eligible list is always empty, fail the same way
and just as quietly.

Two halves of the guard, because either alone is escapable:

1. The SERVER contract — `kind` really is composite. If someone "simplifies"
   data.py to emit a bare type, the SPA's prefix matching still works, but
   this pins the shape everything else is written against.
2. The CLIENT usage — no bare-equality comparison anywhere in the SPA. The
   one blessed way to ask is `kindIs(a, "depository")` in `api/client.ts`.

This is a lint, not a behaviour test, and it is deliberate: the bug is
invisible at runtime (an empty list renders as $0, not as an error), so
there is nothing to assert on. The only reliable moment to catch it is the
moment someone writes the comparison.
"""

import pathlib
import re
import unittest

_WEBAPP = pathlib.Path(__file__).resolve().parents[2] / "webapp" / "src"
_DATA_PY = pathlib.Path(__file__).resolve().parents[1] / "oikonome" / "web" / "data.py"

# `kind === "depository"` / `kind !== 'credit'` / `kind == "loan"` — any direct
# comparison of the composite field against a bare Plaid type.
_BARE_EQ = re.compile(
    r"""\.kind\s*[=!]==?\s*["'](depository|credit|loan|investment|brokerage|other)["']"""
)


class AccountKindInventory(unittest.TestCase):

    def test_server_serves_kind_as_type_slash_subtype(self):
        """The composite shape the SPA is written against."""
        src = _DATA_PY.read_text()
        self.assertIn("|| '/' ||", src,
                      "accounts `kind` is no longer built as type/subtype — "
                      "every kindIs() prefix match in the SPA assumes it is")

    def test_no_bare_equality_kind_comparison_in_spa(self):
        """`kind === \"depository\"` can never be true. Use kindIs()."""
        offenders = []
        for path in sorted(_WEBAPP.rglob("*.ts*")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if _BARE_EQ.search(line):
                    rel = path.relative_to(_WEBAPP.parent.parent)
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(
            ["account `kind` is \"<type>/<subtype>\", so these comparisons "
             "match nothing and silently yield an empty set. Use "
             "kindIs(a, \"depository\") from api/client.ts:"] + offenders))

    def test_kind_is_helper_matches_prefix_not_substring(self):
        """kindIs must accept the composite and the bare form, and must not
        match a type that merely starts with the same letters."""
        src = (_WEBAPP / "api" / "client.ts").read_text()
        self.assertIn("export const kindIs", src, "kindIs helper is gone — "
                      "every call site would fall back to hand-rolled matching")
        # pinned semantics: exact OR "<type>/"; NOT a bare startsWith(t), which
        # would let "depositoryfoo" through
        self.assertIn('a.kind === t || a.kind.startsWith(t + "/")', src,
                      "kindIs no longer matches on the type boundary")


if __name__ == "__main__":
    unittest.main()
