"""catch the typo at the door.

`someone@gmaill.com` is a real address in the sense that it parses, passes
every `@`-and-a-dot check ever written, and is accepted by every signup form.
It is also, unmistakably, `gmail.com` with a stutter — and the cost of not
noticing is a run of undelivered daily emails and no way to find out.

So: a suggestion, never a block. The rule is that a person always gets to keep
the address they typed. Some real domains genuinely look like typos of big
ones, and a form that argues with you about your own email is worse than a
form that quietly lets a rare mistake through. This only ever produces a
"did you mean…?" for the UI to show.

Local table, no network, no DNS — an MX probe would be a better test but it is
a network call on the signup path, it fails open anyway, and it cannot tell
`gmaill.com` (which resolves, and is registered) from a genuine small domain.
Edit distance against the handful of providers that host most of the world's
mail catches the case that actually happens.
"""

from __future__ import annotations

# The domains worth defending. Nearly every typo'd address in the wild is a
# near-miss of one of these; a long tail here would only add false alarms.
COMMON = (
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com",
    "proton.me", "protonmail.com", "pm.me", "fastmail.com", "gmx.com",
    "zoho.com", "yandex.com", "mail.com", "hey.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "charter.net",
)

# Wrong TLD on an otherwise correct name — edit distance sees these too, but
# spelling them out keeps the intent readable and the distance threshold tight.
TLD_TYPOS = {
    "gmail.co": "gmail.com", "gmail.cm": "gmail.com", "gmail.con": "gmail.com",
    "yahoo.co": "yahoo.com", "hotmail.co": "hotmail.com",
    "outlook.co": "outlook.com", "icloud.co": "icloud.com",
}


def _distance(a: str, b: str, cap: int = 2) -> int:
    """Damerau-Levenshtein (optimal string alignment), abandoned once it
    cannot come in under `cap`.

    Transposition counts as ONE edit, not two, and that is the whole reason
    this is not plain Levenshtein: `gmial.com` is the single most common way
    to misspell `gmail.com`, and under Levenshtein a swapped pair scores the
    same as two unrelated substitutions — so catching it would have meant
    loosening the threshold to 2 for every short domain, which is exactly
    how a rule like this starts telling people their real address is wrong.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            d = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if (i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb):
                d = min(d, prev2[j - 2] + 1)
            cur.append(d)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def suggest(email: str) -> str | None:
    """The address they probably meant, or None. Returns the WHOLE corrected
    address so the UI can offer it as one click rather than making someone
    re-type their own name."""
    if not email or email.count("@") != 1:
        return None
    local, _, domain = email.strip().lower().partition("@")
    if not local or not domain:
        return None
    if domain in COMMON:                    # exactly right — say nothing
        return None
    if domain in TLD_TYPOS:
        return f"{local}@{TLD_TYPOS[domain]}"
    best, best_d = None, 3
    for candidate in COMMON:
        d = _distance(domain, candidate)
        # distance 1 only for short domains, where a single edit is still a
        # decisive resemblance; 2 is allowed once the name is long enough
        # that two edits cannot turn one real domain into another by luck
        limit = 1 if len(candidate) <= 9 else 2
        if d <= limit and d < best_d:
            best, best_d = candidate, d
    return f"{local}@{best}" if best else None
