"""Accounts: health is the headline, and one home for connecting.

The page answers "what do I have, and is it all syncing?" in its hero.
Adding a connection has exactly one home — the import section at the foot —
and a broken connection states its failure in words, never only as a coloured
dot whose meaning lives in a hover title a phone cannot show.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class AccountsShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Accounts.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_hero_states_totals_and_freshness(self):
        self.assertIn("const liveTotal", self.src)
        self.assertIn("institution{instCount === 1", self.src)
        self.assertIn("{lastSync && <span>{lastSync}</span>}", self.src)

    def test_connecting_has_exactly_one_home(self):
        self.assertNotIn("＋ Add a connection</summary>", self.src,
                         "the second door at the top of the page")
        self.assertIn('href="#connect"', self.src)
        self.assertIn('<div id="connect"', self.src)

    def test_a_broken_connection_names_its_failure_in_words(self):
        """…and names the RIGHT failure. A dead login and a bank having a
        bad afternoon must read differently, following the server's
        classification; one shared label would make the fixable case and
        the nothing-to-do case indistinguishable."""
        self.assertIn("sign-in expired", self.src)
        self.assertIn("bank having trouble — retrying", self.src)
        self.assertIn("new accounts to share", self.src)
        self.assertIn("needs attention", self.src)

    def test_a_bank_outage_is_never_offered_a_re_auth_button(self):
        """Update mode cannot fix an institution that is down, and a
        button that appears to work is what teaches people the warning
        means nothing."""
        # the fix affordance is gated on the two actionable kinds only
        self.assertIn('(kind === "reauth"', self.src)
        self.assertIn('|| kind === "attention")', self.src)
        # …and the outage gets its own wordless amber state
        self.assertIn('const retrying = !reaped && !hidden '
                      '&& kind === "retrying"', self.src)

    def test_the_aggregator_badge_leaves_healthy_rows(self):
        """plaid/simplefin on a working row is provenance nobody acts on;
        a community script or a manual account still says so."""
        self.assertIn("{(script || !g.conn) && (", self.src)

    def test_explainers_became_subtitles_and_the_sort_key_is_gone(self):
        self.assertNotIn("Keep an entity's money genuinely separate", self.src)
        self.assertNotIn("The connection is still live and the history is kept.",
                         self.src)
        self.assertNotIn("zArchived", self.src)

    def test_last_sync_phrase_renders_verbatim(self):
        # /api/connections sends last_sync already worded ("synced 12m ago").
        # Parsing it as a date yields NaN and silently blanks the line, so
        # the hero's freshness read disappears with no error anywhere.
        self.assertNotIn("function relTime(", self.src)
        self.assertNotIn("new Date(String(conns.data.last_sync", self.src)

    def test_sync_copy_does_not_promise_an_hourly_refresh(self):
        """The poll interval is the operator's to stretch as an instance
        grows, so the screens must not promise every household an hourly
        refresh the instance may not run."""
        self.assertNotIn("refreshed hourly", self.src)
        self.assertNotIn("the hourly worker", _src("components/ConnectHub.tsx"))
        self.assertNotIn("Sync runs hourly on its own", _src("pages/Settings.tsx"))


class ManualSyncDoorTests(unittest.TestCase):
    """There is exactly ONE manual sync door, and it is per connection.

    There is no global "Sync now" chip. A sync
    reads the aggregator's copy of a connection; it cannot make a bank
    send anything, and where webhooks are live the rows have already
    arrived — so a global button is almost always a no-op, which is
    how a person learns a control is broken. The per-connection ↻
    survives because its result can be explained against that bank's own
    clock, and because it is the only recovery door for a missed
    webhook, a webhook-less Item, or a non-Plaid connection.
    """

    def test_the_web_header_chip_is_status_not_a_button(self):
        try:
            src = _src("App.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertIn('<span className={"syncchip"', src)
        self.assertNotIn('<button className={"syncchip"', src)
        self.assertNotIn("api.jobsSyncStart()", src)

    def test_the_per_connection_door_survives(self):
        try:
            src = _src("pages/Accounts.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertIn("onSync(g.itemId)", src)
        # and it says what it actually does
        self.assertIn("pull anything your bank has already sent", src)


class PlaidLinkOutcomeShapeTests(unittest.TestCase):
    """A Plaid session resolves normally when the user closes the window or
    the bank refuses, so no caller may announce "Linked" / "Reconnected"
    without reading how it ended; and the bank tab must open inside the
    click, before the round-trip, or a pop-up blocker stops it silently."""

    CALLERS = ("pages/Accounts.tsx", "components/ManageAccounts.tsx",
               "components/BusinessWizard.tsx")

    def setUp(self):
        try:
            self.hub = _src("components/ConnectHub.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_every_caller_reads_the_session_outcome(self):
        for rel in self.CALLERS:
            src = _src(rel)
            calls = [i for i in range(len(src))
                     if src.startswith("openPlaidHostedLink(", i)]
            self.assertTrue(calls, rel)
            for i in calls:
                self.assertIn("plaidLinkOutcome(", src[i:i + 600],
                              f"{rel}: a link result read without its kind")
        acc = _src("pages/Accounts.tsx")
        self.assertNotIn('onDone("reconnected")', acc)
        self.assertNotIn('"Linked bank"', acc)

    def test_the_outcome_names_a_closed_window_and_a_failure(self):
        fn = self.hub[self.hub.index("export function plaidLinkOutcome("):]
        fn = fn[:fn.index("\n}\n")]
        for kind in ('"exited"', '"cancelled"', '"add_failed"',
                     '"update_failed"'):
            self.assertIn(f"case {kind}", fn)

    def test_the_tab_opens_before_the_session_round_trip(self):
        fn = self.hub[self.hub.index("export async function openTabAt"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertLess(fn.index('window.open("", "_blank")'),
                        fn.index("await fetch()"))
        self.assertIn("if (!tab)", fn)          # a blocked tab is said
        self.assertIn("tab.close()", fn)        # a failed start closes it
        self.assertIn("tab.opener = null", fn)
        plaid = self.hub[self.hub.index("export async function openPlaidHostedLink"):]
        plaid = plaid[:plaid.index("\n}\n")]
        self.assertIn("openTabAt(", plaid)
        self.assertNotIn("window.open(", plaid)
        # the MX widget had the same after-the-await window.open
        self.assertNotIn('window.open(r.url', self.hub)

    def test_a_broken_bank_in_the_hosted_hub_offers_a_repair(self):
        """"needs attention" beside only Disconnect made the destructive
        action the answer to an expired sign-in."""
        self.assertIn("{bad && <HubFixButton c={c} />}", self.hub)
        fix = self.hub[self.hub.index("function HubFixButton("):]
        fix = fix[:fix.index("\n}\n")]
        self.assertIn("plaidLinkOutcome(", fix)
        self.assertIn('kind !== "reauth"', fix)


class BusinessWizardManualAccountKindTests(unittest.TestCase):
    def test_the_manual_account_sends_a_full_kind(self):
        """A bare "checking" was stored as the account TYPE, so the new
        account matched no depository filter — the wizard's own list
        included."""
        try:
            src = _src("components/BusinessWizard.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertIn('<option value="depository/checking">', src)
        self.assertIn('useState("depository/checking")', src)
        self.assertNotIn('<option value="checking">', src)
