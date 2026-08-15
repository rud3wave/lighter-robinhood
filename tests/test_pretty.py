from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from lighter_bot.pretty import fmt_number, format_user_text, plain


class PrettyLogTests(unittest.TestCase):
    def test_numbers_use_at_most_two_decimal_places(self) -> None:
        self.assertEqual(fmt_number("0.00000213"), "0")
        self.assertEqual(fmt_number("12.500000"), "12.5")
        self.assertEqual(fmt_number("12.345"), "12.35")
        self.assertEqual(fmt_number("0.005", signed=True), "+0.01")
        self.assertEqual(fmt_number("-0.004", signed=True), "0")

    def test_text_formatter_preserves_proxy_ip_and_versions(self) -> None:
        text = "proxy http://***@130.49.82.50:63532 | sdk 1.1.2 | fee=0.00000213"
        self.assertEqual(
            format_user_text(text),
            "proxy http://***@130.49.82.50:63532 | sdk 1.1.2 | fee=0",
        )

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
