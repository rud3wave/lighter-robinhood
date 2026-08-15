from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import lighter

from lighter_bot.controller import Controller
from lighter_bot.wallets import WalletAccount
from lighter_bot.withdraw import (
    WithdrawalResult,
    _asset_from_payload,
    _bridge_address_from_payload,
    _fast_withdrawal_memo,
    _withdrawal_amount,
    claim_pending_usdg,
    fast_withdraw_usdg,
    withdraw_usdg,
)


ASSET_PAYLOAD = {
    "asset_details": [
        {
            "asset_id": 3,
            "symbol": "USDG",
            "decimals": 6,
            "min_withdrawal_amount": "1.000000",
        }
    ]
}

LAYER1_PAYLOAD = {
    "contract_addresses": [
        {
            "name": "ZkLighterContract",
            "address": "0x94bAB9693Ba2f6358507eFfcbd372b0660AFfF9d",
        }
    ]
}


class WithdrawalTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, balance: str = "12.520000", position: str = "0"):
        service = Mock()
        service.label.return_value = "0x2222...2222"
        service.wallet = SimpleNamespace(
            api_key_index=4,
            account_index=3181,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
        )
        service.account_state.return_value = {
            "available_balance": balance,
            "positions": [{"position": position}],
        }
        service.public._get.return_value = ASSET_PAYLOAD
        service.client = Mock()
        service.client.api_client = Mock()
        service.client.withdraw = AsyncMock(
            return_value=(
                object(),
                SimpleNamespace(code=200, message="", tx_hash="0x" + "3" * 64),
                None,
            )
        )
        return service

    @staticmethod
    def fast_apis(*, fee_i: int = 0, limit: str = "100000"):
        bridge = Mock()
        bridge.fastwithdraw_info = AsyncMock(
            return_value=SimpleNamespace(
                code=200,
                message="",
                to_account_index=999,
                withdraw_limit=limit,
                max_withdrawal_amount="100000",
            )
        )
        bridge.fastwithdraw = AsyncMock(
            return_value=SimpleNamespace(
                code=200,
                message="",
                additional_properties={"tx_hash": "0x" + "5" * 64},
            )
        )
        info = Mock()
        info.transfer_fee_info = AsyncMock(
            return_value=SimpleNamespace(
                code=200,
                message="",
                transfer_fee_usdc=fee_i,
            )
        )
        return bridge, info

    def test_asset_and_amount_use_exchange_precision(self) -> None:
        asset = _asset_from_payload(ASSET_PAYLOAD)
        self.assertEqual(asset.asset_id, 3)
        self.assertEqual(asset.minimum, Decimal("1"))
        self.assertEqual(
            _withdrawal_amount(Decimal("12.1234569"), None, asset.decimals),
            Decimal("12.123456"),
        )
        self.assertEqual(
            _bridge_address_from_payload(LAYER1_PAYLOAD),
            "0x94bAB9693Ba2f6358507eFfcbd372b0660AFfF9d",
        )

    async def test_claims_ready_l1_balance(self) -> None:
        service = self.service()
        service.public._get.side_effect = [ASSET_PAYLOAD, LAYER1_PAYLOAD]
        plan = SimpleNamespace(amount_i=2_500_000, amount=Decimal("2.5"))
        with (
            patch("lighter_bot.withdraw._prepare_claim", return_value=plan),
            patch("lighter_bot.withdraw._send_claim", return_value="0x" + "4" * 64),
            patch("lighter_bot.withdraw.ok"),
        ):
            result = await claim_pending_usdg(service)

        self.assertTrue(result.claimed)
        self.assertEqual(result.amount, Decimal("2.5"))

    async def test_empty_l1_claim_is_quiet(self) -> None:
        service = self.service()
        service.public._get.side_effect = [ASSET_PAYLOAD, LAYER1_PAYLOAD]
        plan = SimpleNamespace(amount_i=0, amount=Decimal(0))
        with (
            patch("lighter_bot.withdraw._prepare_claim", return_value=plan),
            patch("lighter_bot.withdraw._send_claim") as send,
        ):
            result = await claim_pending_usdg(service)

        self.assertFalse(result.claimed)
        send.assert_not_called()

    async def test_withdraw_all_uses_perps_route(self) -> None:
        service = self.service()
        with patch("lighter_bot.withdraw.ok"), patch("lighter_bot.withdraw.plain"):
            result = await withdraw_usdg(service, None)

        self.assertTrue(result.sent)
        self.assertEqual(result.amount, Decimal("12.520000"))
        service.client.withdraw.assert_awaited_once_with(
            asset_id=3,
            route_type=lighter.SignerClient.ROUTE_PERP,
            amount=12.52,
            api_key_index=4,
        )

    async def test_open_position_blocks_withdrawal(self) -> None:
        service = self.service(position="0.01")
        with self.assertRaisesRegex(RuntimeError, "режим 2"):
            await withdraw_usdg(service, None)
        service.client.withdraw.assert_not_awaited()

    async def test_amount_below_exchange_minimum_is_skipped(self) -> None:
        service = self.service(balance="0.50")
        with patch("lighter_bot.withdraw.skip"):
            result = await withdraw_usdg(service, None)
        self.assertFalse(result.sent)
        self.assertIn("минимума", result.detail)
        service.client.withdraw.assert_not_awaited()

    async def test_rejected_transaction_is_an_error(self) -> None:
        service = self.service()
        service.client.withdraw.return_value = (
            None,
            SimpleNamespace(code=400, message="withdraw rejected", tx_hash=""),
            None,
        )
        with patch("lighter_bot.withdraw.plain"):
            with self.assertRaisesRegex(RuntimeError, "withdraw rejected"):
                await withdraw_usdg(service, None)

    async def test_fast_withdraw_all_reserves_fee_and_uses_official_endpoint(self) -> None:
        service = self.service()
        service.client.create_auth_token_with_expiry.return_value = ("auth-token", None)
        service.client.nonce_manager.async_next_nonce = AsyncMock(return_value=(4, 77))
        service.client.sign_transfer.return_value = (
            "transfer",
            "signed-transfer",
            "0x" + "6" * 64,
            None,
        )
        bridge, info = self.fast_apis(fee_i=500_000)

        with (
            patch("lighter_bot.withdraw.lighter.BridgeApi", return_value=bridge),
            patch("lighter_bot.withdraw.lighter.InfoApi", return_value=info),
            patch("lighter_bot.withdraw.plain"),
            patch("lighter_bot.withdraw.ok"),
        ):
            result = await fast_withdraw_usdg(service, None)

        self.assertEqual(result.amount, Decimal("12.020000"))
        self.assertEqual(result.fee, Decimal("0.5"))
        self.assertEqual(result.tx_hash, "0x" + "5" * 64)
        service.client.sign_transfer.assert_called_once_with(
            eth_private_key=service.wallet.private_key,
            to_account_index=999,
            asset_id=3,
            route_from=lighter.SignerClient.ROUTE_PERP,
            route_to=lighter.SignerClient.ROUTE_PERP,
            usdc_amount=12_020_000,
            fee=500_000,
            memo=_fast_withdrawal_memo(service.wallet.address),
            api_key_index=4,
            nonce=77,
        )
        bridge.fastwithdraw.assert_awaited_once_with(
            tx_info="signed-transfer",
            to_address=service.wallet.address,
            authorization="auth-token",
        )

    async def test_fast_withdraw_above_pool_limit_is_skipped(self) -> None:
        service = self.service()
        service.client.create_auth_token_with_expiry.return_value = ("auth-token", None)
        bridge, info = self.fast_apis(limit="10")

        with (
            patch("lighter_bot.withdraw.lighter.BridgeApi", return_value=bridge),
            patch("lighter_bot.withdraw.lighter.InfoApi", return_value=info),
            patch("lighter_bot.withdraw.skip"),
        ):
            result = await fast_withdraw_usdg(service, None)

        self.assertFalse(result.sent)
        self.assertIn("лимита", result.detail)
        service.client.sign_transfer.assert_not_called()
        bridge.fastwithdraw.assert_not_awaited()

    async def test_fast_withdraw_requires_balance_above_fee(self) -> None:
        service = self.service(balance="0.50")
        service.client.create_auth_token_with_expiry.return_value = ("auth-token", None)
        bridge, info = self.fast_apis(fee_i=500_000)

        with (
            patch("lighter_bot.withdraw.lighter.BridgeApi", return_value=bridge),
            patch("lighter_bot.withdraw.lighter.InfoApi", return_value=info),
            patch("lighter_bot.withdraw.skip"),
        ):
            result = await fast_withdraw_usdg(service, None)

        self.assertFalse(result.sent)
        self.assertIn("комиссии", result.detail)
        service.client.sign_transfer.assert_not_called()

    async def test_withdraw_dispatcher_uses_selected_method(self) -> None:
        service = self.service()
        expected = WithdrawalResult(amount=Decimal("5"), tx_hash="0x123")
        with patch(
            "lighter_bot.withdraw.fast_withdraw_usdg",
            new_callable=AsyncMock,
            return_value=expected,
        ) as fast:
            result = await withdraw_usdg(service, Decimal("5"), method="fast")
        self.assertEqual(result, expected)
        fast.assert_awaited_once_with(service, Decimal("5"))


class WithdrawalModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_positions_stop_mode_before_claim_or_withdraw(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=1,
            api_private_key="api-key",
        )
        service = Mock()
        service.account_state.return_value = {
            "positions": [{"symbol": "ETH", "position": "0.01"}]
        }
        service.close = AsyncMock()
        controller = object.__new__(Controller)
        controller._load_wallets = Mock(return_value=[wallet])
        controller._prepare_wallets = AsyncMock(return_value=[wallet])

        with (
            patch("lighter_bot.controller.LighterService", return_value=service),
            patch("lighter_bot.controller.claim_pending_usdg", new_callable=AsyncMock) as claim,
            patch("lighter_bot.controller.withdraw_usdg", new_callable=AsyncMock) as withdraw,
            patch("lighter_bot.controller.send_tg", new_callable=AsyncMock) as telegram,
            patch("lighter_bot.controller.section"),
            patch("lighter_bot.controller.warn"),
        ):
            await controller.withdraw_to_wallets("fast")

        controller._prepare_wallets.assert_awaited_once_with(
            [wallet],
            require_trading=False,
        )
        claim.assert_not_awaited()
        withdraw.assert_not_awaited()
        telegram.assert_awaited_once()
        self.assertEqual(telegram.await_args.kwargs["attempts"], 1)

    async def test_fast_mode_does_not_run_secure_claim_or_delay(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=1,
            api_private_key="api-key",
        )
        preflight = Mock(wallet=wallet)
        preflight.account_state.return_value = {"positions": []}
        preflight.close = AsyncMock()
        live = Mock(wallet=wallet)
        live.close = AsyncMock()
        controller = object.__new__(Controller)
        controller._load_wallets = Mock(return_value=[wallet])
        controller._prepare_wallets = AsyncMock(return_value=[wallet])

        with (
            patch("lighter_bot.controller.LighterService", side_effect=[preflight, live]),
            patch("lighter_bot.controller.claim_pending_usdg", new_callable=AsyncMock) as claim,
            patch("lighter_bot.controller.withdrawal_delay_seconds", new_callable=AsyncMock) as delay,
            patch(
                "lighter_bot.controller.withdraw_usdg",
                new_callable=AsyncMock,
                return_value=WithdrawalResult(
                    amount=Decimal("5"),
                    tx_hash="0x123",
                ),
            ) as withdraw,
            patch("lighter_bot.controller.send_tg", new_callable=AsyncMock),
            patch("lighter_bot.controller.section"),
            patch("lighter_bot.controller.ok"),
        ):
            await controller.withdraw_to_wallets("fast")

        claim.assert_not_awaited()
        delay.assert_not_awaited()
        withdraw.assert_awaited_once_with(live, None, method="fast")

    async def test_secure_mode_prints_and_sends_the_claim_instruction(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=1,
            api_private_key="api-key",
        )
        preflight = Mock(wallet=wallet)
        preflight.account_state.return_value = {"positions": []}
        preflight.close = AsyncMock()
        live = Mock(wallet=wallet)
        live.close = AsyncMock()
        controller = object.__new__(Controller)
        controller._load_wallets = Mock(return_value=[wallet])
        controller._prepare_wallets = AsyncMock(return_value=[wallet])

        with (
            patch("lighter_bot.controller.LighterService", side_effect=[preflight, live]),
            patch(
                "lighter_bot.controller.claim_pending_usdg",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(claimed=False, amount=Decimal(0), detail=""),
            ),
            patch(
                "lighter_bot.controller.withdrawal_delay_seconds",
                new_callable=AsyncMock,
                return_value=3600,
            ),
            patch(
                "lighter_bot.controller.withdraw_usdg",
                new_callable=AsyncMock,
                return_value=WithdrawalResult(
                    amount=Decimal("5"),
                    tx_hash="0x123",
                ),
            ),
            patch("lighter_bot.controller.send_tg", new_callable=AsyncMock) as telegram,
            patch("lighter_bot.controller.section"),
            patch("lighter_bot.controller.ok"),
            patch("lighter_bot.controller.info") as info_log,
        ):
            await controller.withdraw_to_wallets("secure")

        info_log.assert_called_once()
        self.assertIn("режим 5 -> Secure", info_log.call_args.args[1])
        self.assertIn("режим 5 -> Secure", telegram.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
