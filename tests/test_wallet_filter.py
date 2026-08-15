from __future__ import annotations

import unittest
from unittest.mock import patch

from lighter_bot.id_filter import filter_wallets_by_id, parse_id_filter
from lighter_bot.wallets import WalletAccount, init_wallets


def wallet(index: int) -> WalletAccount:
    digit = str((index % 9) + 1)
    return WalletAccount(
        index=index,
        private_key="0x" + digit * 64,
        address="0x" + digit * 40,
    )


class WalletIdFilterTests(unittest.TestCase):
    def test_wallet_id_is_one_based_private_key_position(self) -> None:
        wallets = [wallet(index) for index in range(3)]
        self.assertEqual([item.wallet_id for item in wallets], [1, 2, 3])

    def test_qso_filter_formats_can_be_combined(self) -> None:
        wallets = [wallet(index) for index in range(110)]
        selected = filter_wallets_by_id(
            wallets,
            ["3", ["10", "13"], "20", ">100"],
        )
        self.assertEqual(
            [item.wallet_id for item in selected],
            [3, 10, 11, 12, 13, 20, *range(100, 111)],
        )

    def test_more_and_less_filters_include_the_boundary(self) -> None:
        wallets = [wallet(index) for index in range(10)]
        self.assertEqual(
            [item.wallet_id for item in filter_wallets_by_id(wallets, [">5"])],
            [5, 6, 7, 8, 9, 10],
        )
        self.assertEqual(
            [item.wallet_id for item in filter_wallets_by_id(wallets, ["<5"])],
            [1, 2, 3, 4, 5],
        )

    def test_invalid_filter_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "начало диапазона"):
            parse_id_filter([["10", "3"]])
        with self.assertRaisesRegex(RuntimeError, "положительный ID"):
            parse_id_filter(["wallet-3"])

    def test_init_logs_only_selected_wallets_without_ids(self) -> None:
        wallets = [wallet(index) for index in range(3)]
        with (
            patch("lighter_bot.wallets.load_wallets_from_privatekeys", return_value=wallets),
            patch("lighter_bot.wallets.load_wallets_db", return_value=None),
            patch("lighter_bot.wallets.save_wallets"),
            patch("lighter_bot.wallets.ID_FILTER", ["2"]),
            patch("lighter_bot.wallets.SHUFFLE_WALLETS", False),
            patch("lighter_bot.wallets.section") as section,
            patch("lighter_bot.wallets.plain") as plain,
        ):
            selected = init_wallets()

        self.assertEqual([item.wallet_id for item in selected], [2])
        section.assert_called_once_with("Кошельки", "1")
        plain.assert_called_once()
        log_text = " ".join(str(arg) for arg in plain.call_args.args)
        self.assertNotIn("ID", log_text.upper())
        self.assertNotIn("0002", log_text)


if __name__ == "__main__":
    unittest.main()
