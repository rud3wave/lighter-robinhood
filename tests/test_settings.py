from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

import settings
from lighter_bot.settings_validation import (
    validate_group_wallet_count,
    validate_market_leverage,
    validate_settings,
)


class SettingsTests(unittest.TestCase):
    def test_settings_contains_only_user_controls(self) -> None:
        self.assertEqual(settings.TOKENS_TO_TRADE, ["ETH"])
        self.assertEqual(settings.EXECUTION_MODE, "all-market")
        self.assertEqual(settings.ID_FILTER, [])
        self.assertTrue(settings.DEPOSIT_ALL)
        self.assertTrue(settings.WITHDRAW_ALL)
        self.assertEqual(settings.DELAY_BETWEEN_WALLETS, [3, 8])
        self.assertEqual(settings.POLL_INTERVAL_SEC, 5)
        self.assertFalse(hasattr(settings, "STRICT_PROXY_ISOLATION"))

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

    def test_default_settings_are_valid_for_current_eth_limit(self) -> None:
        validate_settings()
        validate_market_leverage({"ETH": Decimal(50)})
        validate_group_wallet_count(sum(settings.GROUP_CONFIGS[0]))

    def test_every_active_token_requires_its_own_leverage_range(self) -> None:
        with (
            patch.object(settings, "TOKENS_TO_TRADE", ["ETH", "SOL"]),
            patch.object(settings, "TOKEN_LEVERAGE", {"ETH": [7, 10]}),
        ):
            with self.assertRaisesRegex(RuntimeError, "must define SOL"):
                validate_settings()

    def test_invalid_wallet_id_filter_is_rejected(self) -> None:
        with patch.object(settings, "ID_FILTER", [["10", "3"]]):
            with self.assertRaisesRegex(RuntimeError, "начало диапазона"):
                validate_settings()

    def test_exchange_leverage_is_not_silently_clamped(self) -> None:
        with patch.object(settings, "TOKEN_LEVERAGE", {"ETH": [7, 60]}):
            with self.assertRaisesRegex(RuntimeError, "exceeds Lighter maximum 50x"):
                validate_market_leverage({"ETH": Decimal(50)})

    def test_group_config_must_use_every_ready_wallet(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "выбрано=4"):
            validate_group_wallet_count(4)


if __name__ == "__main__":
    unittest.main()
