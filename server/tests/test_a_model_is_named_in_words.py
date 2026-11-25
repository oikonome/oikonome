"""Settings names the model that answers in words a person reads: the
side menu printed a raw model id ("vendor-family-5-5"). The server sends
`label` beside each role's model; the id stays for the AI section's own
editor. Names are invented."""
import unittest

from oikonome.web import api_settings


class ModelWords(unittest.TestCase):
    def test_an_id_reads_as_words_and_a_version(self):
        self.assertEqual(api_settings.model_words("acme-sage-5-5"),
                         "Acme Sage 5.5")
        self.assertEqual(api_settings.model_words("org/acme_sage-7b"),
                         "Acme Sage 7b")
        self.assertEqual(api_settings.model_words("acme-sage:3.1"),
                         "Acme Sage 3.1")
        self.assertEqual(api_settings.model_words(""), "")

    def test_a_name_written_with_capitals_is_kept(self):
        self.assertEqual(api_settings.model_words("AcmeGPT-4"), "AcmeGPT 4")


if __name__ == "__main__":
    unittest.main()
