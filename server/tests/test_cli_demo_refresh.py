"""`oikonome demo-refresh` must print the refresh result as JSON.

`main()` imports `json` locally in other branches, which makes `json` a
local name for the whole function; a branch that forgets its own import
raises UnboundLocalError before the refresh even starts, so the household
silently stays stale.
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from oikonome import cli, demo


class DemoRefreshCommandTests(unittest.TestCase):
    def test_demo_refresh_prints_the_result_as_json(self):
        result = {"tenant": "t-1", "transactions": 12}
        argv = ["oikonome", "demo-refresh", "--email", "viewer@example.com"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(demo, "refresh_demo_household",
                                  return_value=result) as refresh:
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main()
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0], "viewer@example.com")
        self.assertEqual(json.loads(out.getvalue()), result)


if __name__ == "__main__":
    unittest.main()
