"""Minting a script token, exporting the connection credentials and
importing a connections file each hand out or take in something that
reaches the household's data, so each tells the account at its own
address, as a factor change does — and none claims other sign-ins ended."""
import unittest

from oikonome.web import app as app_mod


class CredentialGradeNotices(unittest.TestCase):
    def test_each_act_has_a_notice_that_keeps_other_sign_ins(self):
        for change in ("api_token_created", "connections_exported",
                       "connections_imported"):
            self.assertIn(change, app_mod._SECURITY_CHANGES)
            self.assertIn(change, app_mod._KEEPS_OTHER_SIGN_INS)

    def test_the_doors_send_it(self):
        import inspect
        from oikonome.web import api
        for fn, change in ((api.tokens_mint_api, "api_token_created"),
                           (api.connections_export, "connections_exported"),
                           (api.connections_import, "connections_imported")):
            self.assertIn(f'_notify_security_change(user, "{change}")',
                          inspect.getsource(fn), fn.__name__)


if __name__ == "__main__":
    unittest.main()
