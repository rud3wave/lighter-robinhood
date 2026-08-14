from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from lighter_bot.controller import Controller
from lighter_bot.wallets import WalletAccount


class ReferralModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_referral_is_only_applied_after_new_account_creation(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
        )
        controller = object.__new__(Controller)
        controller._load_wallets = lambda: [wallet]
        controller._resolve_wallet_account = AsyncMock(return_value=False)

        async def create_account(_: WalletAccount) -> bool:
            wallet.account_index = 123
            return True

        controller._wait_for_account = AsyncMock(side_effect=create_account)
        controller._ensure_api_key = AsyncMock(return_value=True)
        referral = AsyncMock(return_value=True)

        with (
            patch("lighter_bot.controller.deposit_token", AsyncMock(return_value="0x" + "3" * 64)),
            patch("lighter_bot.controller.use_referral", referral),
            patch("lighter_bot.controller.save_wallets"),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        referral.assert_awaited_once_with(wallet)

    async def test_existing_account_does_not_resubmit_referral(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=123,
            api_private_key="api-key",
        )
        controller = object.__new__(Controller)
        controller._load_wallets = lambda: [wallet]
        controller._resolve_wallet_account = AsyncMock(return_value=False)
        controller._ensure_api_key = AsyncMock(return_value=False)
        referral = AsyncMock(return_value=True)

        with (
            patch("lighter_bot.controller.deposit_token", AsyncMock(return_value="0x" + "3" * 64)),
            patch("lighter_bot.controller.use_referral", referral),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        referral.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
