from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from lighter_bot.pretty import plain


class PrettyLogTests(unittest.TestCase):
    def test_plain_wallet_line_has_no_status_or_account_metadata(self) -> None:
        output = io.StringIO()
        with patch("lighter_bot.pretty.USE_COLOR", False), redirect_stdout(output):
            plain("0xaFBc...1405", "proxy: http://***@130.49.82.50:63532")

        line = output.getvalue().strip()
        self.assertRegex(
            line,
            r"^\d{2}:\d{2}:\d{2} 0xaFBc\.\.\.1405 \| "
            r"proxy: http://\*\*\*@130\.49\.82\.50:63532$",
        )
        self.assertNotIn("INFO", line)
        self.assertNotIn("account:", line)
        self.assertNotIn("api-key", line)


if __name__ == "__main__":
    unittest.main()
