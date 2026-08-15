from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from lighter_bot.controller import Controller
from lighter_bot.referral import ReferralResult
from lighter_bot.wallets import WalletAccount


class ReferralModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_account_applies_referral_after_creation(self) -> None:
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
        async def create_api_key(target: WalletAccount) -> bool:
            target.api_private_key = "api-key"
            return True

        controller._ensure_api_key = AsyncMock(side_effect=create_api_key)
        referral = AsyncMock(return_value=ReferralResult(True, True))

        with (
            patch("lighter_bot.controller.deposit_token", AsyncMock(return_value="0x" + "3" * 64)),
            patch("lighter_bot.controller.use_referral", referral),
            patch("lighter_bot.controller.save_wallets"),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        referral.assert_awaited_once_with(wallet)

    async def test_existing_account_confirms_referral_before_deposit(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=123,
        )
        controller = object.__new__(Controller)
        controller._load_wallets = lambda: [wallet]
        controller._resolve_wallet_account = AsyncMock(return_value=False)

        async def recover_api_key(target: WalletAccount) -> bool:
            target.api_private_key = "api-key"
            return True

        controller._ensure_api_key = AsyncMock(side_effect=recover_api_key)
        referral = AsyncMock(return_value=ReferralResult(True, True))

        deposit = AsyncMock(return_value="0x" + "3" * 64)
        with (
            patch("lighter_bot.controller.deposit_token", deposit),
            patch("lighter_bot.controller.use_referral", referral),
            patch("lighter_bot.controller.save_wallets"),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        referral.assert_awaited_once_with(wallet)
        deposit.assert_awaited_once_with(wallet, None)

    async def test_existing_account_skips_deposit_when_referral_is_not_confirmed(self) -> None:
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
        deposit = AsyncMock(return_value="0x" + "3" * 64)

        with (
            patch("lighter_bot.controller.deposit_token", deposit),
            patch(
                "lighter_bot.controller.use_referral",
                AsyncMock(return_value=ReferralResult(False)),
            ),
            patch("lighter_bot.controller.save_wallets"),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        deposit.assert_not_awaited()

    async def test_unconfirmed_referral_blocks_existing_account_deposit(self) -> None:
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
        deposit = AsyncMock(return_value="0x" + "3" * 64)

        with (
            patch("lighter_bot.controller.deposit_token", deposit),
            patch(
                "lighter_bot.controller.use_referral",
                AsyncMock(return_value=ReferralResult(True, False)),
            ),
            patch("lighter_bot.controller.save_wallets"),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        deposit.assert_not_awaited()

    async def test_code_owner_can_still_deposit(self) -> None:
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
        deposit = AsyncMock(return_value="0x" + "3" * 64)

        with (
            patch("lighter_bot.controller.deposit_token", deposit),
            patch(
                "lighter_bot.controller.use_referral",
                AsyncMock(return_value=ReferralResult(True, code_owner=True)),
            ),
            patch("lighter_bot.controller.save_wallets"),
            patch("lighter_bot.controller.send_tg", AsyncMock(return_value=True)),
        ):
            await controller.deposit_from_wallets()

        deposit.assert_awaited_once_with(wallet, None)


if __name__ == "__main__":
    unittest.main()
