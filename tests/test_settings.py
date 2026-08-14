from __future__ import annotations

import unittest

import settings


class SettingsTests(unittest.TestCase):
    def test_settings_contains_only_user_controls(self) -> None:
        self.assertEqual(settings.TOKENS_TO_TRADE, ["ETH"])
        self.assertEqual(settings.EXECUTION_MODE, "all-market")
        self.assertTrue(settings.DEPOSIT_ALL)

        internal_names = {
            "API_BASE_URL",
            "CHAIN_ID",
            "DEFAULT_API_KEY_INDEX",
            "DEPOSIT_WAIT_FOR_RECEIPT",
            "POST_DEPOSIT_ACCOUNT_POLL_ATTEMPTS",
            "REFERRAL_CODE",
            "ROBINHOOD_RPC_URL",
            "SPREAD_POLL_INTERVAL_SECONDS",
        }
        self.assertTrue(internal_names.isdisjoint(vars(settings)))


if __name__ == "__main__":
    unittest.main()
