"""Minting a script token, exporting the connection credentials and
importing a connections file each hand out or take in something that
reaches the household's data, and repointing where the AI tasks send the
household's text decides who reads it, so each tells the account at its own
address, as a factor change does — and none claims other sign-ins ended."""
import unittest

from oikonome.web import app as app_mod


class CredentialGradeNotices(unittest.TestCase):
    def test_each_act_has_a_notice_that_keeps_other_sign_ins(self):
        for change in ("api_token_created", "connections_exported",
                       "connections_imported", "ai_route_changed",
                       "ai_route_restored"):
            self.assertIn(change, app_mod._SECURITY_CHANGES)
            self.assertIn(change, app_mod._KEEPS_OTHER_SIGN_INS)

    def test_the_doors_send_it(self):
        import inspect
        from oikonome.web import api
        for fn, change in ((api.tokens_mint_api, "api_token_created"),
                           (api.connections_export, "connections_exported"),
                           (api.connections_import, "connections_imported"),
                           (api.settings_set, "ai_route_changed")):
            self.assertIn(f'_notify_security_change(user, "{change}")',
                          inspect.getsource(fn), fn.__name__)


    def test_a_restore_says_it_was_a_restore(self):
        """The owner reading a restore's notice did not touch Settings, so
        the notice names the act that did happen."""
        import inspect
        from oikonome.sync import restore
        self.assertIn('"ai_route_restored"', inspect.getsource(
            restore._tell_the_owner_the_ai_route_moved))
        text = app_mod._SECURITY_CHANGES["ai_route_restored"]
        self.assertNotIn("Settings", text)
        self.assertIn("restor", text)


if __name__ == "__main__":
    unittest.main()
