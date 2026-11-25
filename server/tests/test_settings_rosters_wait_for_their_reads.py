"""Settings rosters and security claim nothing from a read that failed.

A failed passkeys read raised "no 2FA or passkey" in the Settings sidebar
and dropped the Passkeys card; failed sessions, devices, members, webhooks,
tokens and delivery-log reads read as empty rosters ("No webhooks yet.",
"nothing sent yet", no sessions, nobody in the household); a failed
notify-status read showed SMS and push as "not configured"; a failed first
settings read left the web page on "loading…" for good.
(specs/screen-states.md)
"""

import pathlib
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


def _fn(src: str, head: str) -> str:
    """The body of one top-level function, from its head to the next."""
    i = src.index(head)
    j = src.find("\nfunction ", i + len(head))
    return src[i:j if j > 0 else len(src)]


class SettingsRostersWait(unittest.TestCase):
    def test_no_2fa_warning_only_from_an_answered_passkey_list(self):
        web = _src(self, _WEB / "pages" / "Settings.tsx")
        self.assertNotIn("(passkeyQ.data?.passkeys ?? []).length", web)
        self.assertIn("const nKeys = passkeyQ.data ? passkeyQ.data.passkeys"
                      ".length : null;", web)
        self.assertIn('nKeys === 0 ? { text: "no 2FA or passkey", warn: true }',
                      web)

    def test_web_rosters_say_a_failed_read(self):
        web = _src(self, _WEB / "pages" / "Settings.tsx")
        for head, what in (
                ("function PasskeysCard(", "the passkeys"),
                ("function SessionsCard(", "the sessions"),
                ("function SessionsCard(", "the mobile devices"),
                ("function FamilyCard(", "the household's people"),
                ("function FamilyCard(", "the invitations"),
                ("function WebhooksCard(", "the webhooks"),
                ("function ScriptTokensCard(", "the tokens"),
                ("function DeliveryLog(", "the delivery log")):
            self.assertIn(f'what="{what}"', _fn(web, head), head)
        # a failed read no longer hides the card outright
        for head in ("function PasskeysCard(", "function SessionsCard(",
                     "function FamilyCard("):
            self.assertNotIn(".isLoadingError) return null", _fn(web, head),
                             head)
        hooks = _fn(web, "function WebhooksCard(")
        self.assertIn("{!hooks.length && q.data !== undefined && (", hooks)
        log = _fn(web, "function DeliveryLog(")
        self.assertLess(log.index("if (q.data === undefined) return <FailedLine"),
                        log.index("nothing sent yet"))

    def test_web_settings_never_loads_forever_and_marks_a_kept_copy(self):
        web = _src(self, _WEB / "pages" / "Settings.tsx")
        page = _fn(web, "export default function Settings(")
        self.assertNotIn("Couldn't load settings: {String(q.error)}", page)
        self.assertLess(page.index('<FailedLine q={q} what="settings" />'),
                        page.index("if (q.isPending || form === null)"))
        self.assertIn("<StaleLine query={q} />", page)

    def test_unread_notify_status_is_not_not_configured(self):
        web = _src(self, _WEB / "pages" / "Settings.tsx")
        self.assertIn('notify.data === undefined ? ""\n    : !notify.data.sms_available',
                      web)
        self.assertIn('<FailedLine q={notify} what="the text and push setup" />',
                      web)
        mob = _src(self, _MOB / "app" / "notifications.tsx")
        self.assertIn("{!smsOk && !smsLock && notify.data !== undefined && (",
                      mob)
        self.assertIn("{!pushOk && !pushLock && notify.data !== undefined && (",
                      mob)
        self.assertIn('<FailedLine q={notify} what="the text and push setup" />',
                      mob)
        self.assertIn('<FailedLine q={query} what="the email schedule" />', mob)
        self.assertIn("<StaleLine query={query} />", mob)

    def test_phone_rosters_say_a_failed_read_in_the_same_words(self):
        sec = _src(self, _MOB / "app" / "security.tsx")
        for q, what in (("sessions", "the sessions"),
                        ("devices", "the mobile devices"),
                        ("members", "the household's people"),
                        ("invites", "the invitations")):
            self.assertIn(f'<FailedLine q={{{q}}} what="{what}" />', sec)
        for q in ("sessions", "members"):
            self.assertIn(f"<StaleLine query={{{q}}} />", sec)
        st = _src(self, _MOB / "app" / "settings.tsx")
        self.assertIn('<FailedLine q={q} what="the tokens" />', st)
        self.assertIn('<FailedLine q={q} what="the webhooks" />', st)
        self.assertIn("{!hooks.length && q.data !== undefined ? (", st)
        self.assertIn('<FailedLine q={devices} what="the devices" />', st)
        self.assertIn("<StaleLine query={me} />", st)


if __name__ == "__main__":
    unittest.main()
