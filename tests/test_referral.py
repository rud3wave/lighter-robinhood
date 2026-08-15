from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import lighter

from lighter_bot.constants import REFERRAL_CODE
from lighter_bot.referral import referral_signature, use_referral
from lighter_bot.wallets import WalletAccount


class ReferralTests(unittest.IsolatedAsyncioTestCase):
    def wallet(self) -> WalletAccount:
        return WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=123,
            api_private_key="api-key",
        )

    async def test_existing_matching_code_needs_no_update(self) -> None:
        signer = Mock()
        signer.create_auth_token_with_expiry.return_value = ("auth", None)
        signer.close = AsyncMock()
        client = Mock()
        client.close = AsyncMock()
        account_api = Mock()
        account_api.referral_user_referrals = AsyncMock(
            return_value=SimpleNamespace(code=200, message="", used_code=REFERRAL_CODE)
        )
        referral_api = Mock()
        referral_api.referral_use = AsyncMock()

        with (
            patch("lighter_bot.referral.lighter.SignerClient", return_value=signer),
            patch("lighter_bot.referral.lighter.ApiClient", return_value=client),
            patch("lighter_bot.referral.lighter.AccountApi", return_value=account_api),
            patch("lighter_bot.referral.lighter.ReferralApi", return_value=referral_api),
        ):
            result = await use_referral(self.wallet())

        self.assertTrue(result.accepted)
        self.assertTrue(result.confirmed)

        referral_api.referral_use.assert_not_awaited()

    def test_signature_matches_robinhood_web_client(self) -> None:
        self.assertEqual(
            referral_signature("0x1234", "RUD3WAVE"),
            "MHgxMjM0UlVEM1dBVkV3UDgxekROcEVT",
        )

    async def test_update_is_verified_through_used_code(self) -> None:
        signer = Mock()
        signer.create_auth_token_with_expiry.return_value = ("auth", None)
        signer.close = AsyncMock()
        client = Mock()
        client.close = AsyncMock()
        account_api = Mock()
        account_api.referral_user_referrals = AsyncMock(
            side_effect=[
                SimpleNamespace(code=200, message="", used_code=""),
                SimpleNamespace(code=200, message="", used_code=REFERRAL_CODE),
            ]
        )
        referral_api = Mock()
        referral_api.referral_use = AsyncMock(
            return_value=SimpleNamespace(code=200, message="")
        )

        with (
            patch("lighter_bot.referral.lighter.SignerClient", return_value=signer),
            patch("lighter_bot.referral.lighter.ApiClient", return_value=client),
            patch("lighter_bot.referral.lighter.AccountApi", return_value=account_api),
            patch("lighter_bot.referral.lighter.ReferralApi", return_value=referral_api),
        ):
            result = await use_referral(self.wallet())

        self.assertTrue(result.accepted)
        self.assertTrue(result.confirmed)
        referral_api.referral_use.assert_awaited_once()
        self.assertEqual(
            referral_api.referral_use.await_args.kwargs["signature"],
            referral_signature(self.wallet().address, REFERRAL_CODE),
        )
        self.assertEqual(account_api.referral_user_referrals.await_count, 2)

    async def test_unconfirmed_update_is_not_reported_as_success(self) -> None:
        signer = Mock()
        signer.create_auth_token_with_expiry.return_value = ("auth", None)
        signer.close = AsyncMock()
        client = Mock()
        client.close = AsyncMock()
        account_api = Mock()
        account_api.referral_user_referrals = AsyncMock(
            return_value=SimpleNamespace(code=200, message="", used_code="")
        )
        referral_api = Mock()
        referral_api.referral_use = AsyncMock(
            return_value=SimpleNamespace(code=200, message="")
        )

        with (
            patch("lighter_bot.referral.asyncio.sleep", AsyncMock()),
            patch("lighter_bot.referral.lighter.SignerClient", return_value=signer),
            patch("lighter_bot.referral.lighter.ApiClient", return_value=client),
            patch("lighter_bot.referral.lighter.AccountApi", return_value=account_api),
            patch("lighter_bot.referral.lighter.ReferralApi", return_value=referral_api),
        ):
            result = await use_referral(self.wallet())

        self.assertTrue(result.accepted)
        self.assertFalse(result.confirmed)
        self.assertFalse(result)
        referral_api.referral_use.assert_awaited_once()

    async def test_status_read_failure_does_not_block_update(self) -> None:
        signer = Mock()
        signer.create_auth_token_with_expiry.return_value = ("auth", None)
        signer.close = AsyncMock()
        client = Mock()
        client.close = AsyncMock()
        account_api = Mock()
        account_api.referral_user_referrals = AsyncMock(
            side_effect=RuntimeError("status temporarily unavailable")
        )
        referral_api = Mock()
        referral_api.referral_use = AsyncMock(
            return_value=SimpleNamespace(code=200, message="")
        )

        with (
            patch("lighter_bot.referral.asyncio.sleep", AsyncMock()),
            patch("lighter_bot.referral.lighter.SignerClient", return_value=signer),
            patch("lighter_bot.referral.lighter.ApiClient", return_value=client),
            patch("lighter_bot.referral.lighter.AccountApi", return_value=account_api),
            patch("lighter_bot.referral.lighter.ReferralApi", return_value=referral_api),
        ):
            result = await use_referral(self.wallet())

        self.assertTrue(result.accepted)
        self.assertFalse(result.confirmed)
        self.assertFalse(result)
        referral_api.referral_use.assert_awaited_once()

    async def test_using_own_code_is_a_ready_account_not_an_error(self) -> None:
        signer = Mock()
        signer.create_auth_token_with_expiry.return_value = ("auth", None)
        signer.close = AsyncMock()
        client = Mock()
        client.close = AsyncMock()
        account_api = Mock()
        account_api.referral_user_referrals = AsyncMock(
            return_value=SimpleNamespace(code=200, message="", used_code="")
        )
        referral_api = Mock()
        referral_api.referral_use = AsyncMock(
            side_effect=lighter.ApiException(
                status=400,
                reason="Bad Request",
                body='{"code":41012,"message":"cannot use your own referral code"}',
            )
        )

        with (
            patch("lighter_bot.referral.lighter.SignerClient", return_value=signer),
            patch("lighter_bot.referral.lighter.ApiClient", return_value=client),
            patch("lighter_bot.referral.lighter.AccountApi", return_value=account_api),
            patch("lighter_bot.referral.lighter.ReferralApi", return_value=referral_api),
        ):
            result = await use_referral(self.wallet())

        self.assertTrue(result)
        self.assertTrue(result.code_owner)
        self.assertFalse(result.confirmed)


if __name__ == "__main__":
    unittest.main()
