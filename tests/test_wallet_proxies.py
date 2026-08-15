from __future__ import annotations

import unittest
from unittest.mock import patch

from lighter_bot.wallets import (
    WalletAccount,
    check_proxies_health,
    init_wallets,
    load_wallets_from_privatekeys,
)


class ProxyPoolTests(unittest.TestCase):
    def test_dead_and_duplicate_proxies_are_skipped_without_stopping(self) -> None:
        proxies = [
            "http://dead.example:8000",
            "http://good.example:8000",
            "http://good.example:8000",
        ]

        with patch(
            "lighter_bot.wallets.check_proxy",
            side_effect=lambda proxy: "good.example" in proxy,
        ) as checker:
            alive = check_proxies_health(proxies)

        self.assertEqual(alive, ["http://good.example:8000"])
        self.assertEqual(
            {call.args[0] for call in checker.call_args_list},
            {"http://dead.example:8000", "http://good.example:8000"},
        )

    def test_working_proxies_are_reused_for_multiple_wallets(self) -> None:
        keys = ["0x" + str(index) * 64 for index in range(1, 4)]
        pool = ["http://good.example:8000"]

        with (
            patch("lighter_bot.wallets.read_private_keys", return_value=keys),
            patch("lighter_bot.wallets.read_proxies", return_value=pool),
            patch("lighter_bot.wallets.check_proxies_health", return_value=pool),
            patch(
                "lighter_bot.wallets.address_from_private_key",
                side_effect=["0x" + str(index) * 40 for index in range(1, 4)],
            ),
        ):
            wallets = load_wallets_from_privatekeys()

        self.assertEqual([wallet.proxy_url for wallet in wallets], pool * 3)
        self.assertEqual([wallet.wallet_id for wallet in wallets], [1, 2, 3])

    def test_cached_dead_proxy_does_not_replace_fresh_assignment(self) -> None:
        fresh = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "1" * 40,
            proxy_url="http://good.example:8000",
        )
        cached = WalletAccount(
            index=0,
            private_key=fresh.private_key,
            address=fresh.address,
            proxy_url="http://dead.example:8000",
            account_index=123,
            api_private_key="api-key",
        )

        with (
            patch("lighter_bot.wallets.load_wallets_from_privatekeys", return_value=[fresh]),
            patch("lighter_bot.wallets.load_wallets_db", return_value=[cached]),
            patch("lighter_bot.wallets.save_wallets"),
            patch("lighter_bot.wallets.section"),
            patch("lighter_bot.wallets.plain"),
        ):
            wallets = init_wallets()

        self.assertEqual(wallets[0].proxy_url, "http://good.example:8000")
        self.assertEqual(wallets[0].account_index, 123)
        self.assertEqual(wallets[0].api_private_key, "api-key")


if __name__ == "__main__":
    unittest.main()
