from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lighter_bot.api import BookSnapshot
from lighter_bot.controller import Controller
from lighter_bot.wallets import WalletAccount


def wallet(index: int) -> WalletAccount:
    digit = str(index + 1)
    return WalletAccount(
        index=index,
        private_key="0x" + digit * 64,
        address="0x" + digit * 40,
    )


class ControllerSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_wallet_start_delay_separates_real_account_requests(self) -> None:
        controller = object.__new__(Controller)
        services = [
            SimpleNamespace(account_state=Mock(return_value={}))
            for _ in range(3)
        ]
        sleep = AsyncMock()

        with (
            patch("lighter_bot.controller.pick_range", return_value=2.5),
            patch("lighter_bot.controller.asyncio.sleep", sleep),
        ):
            await controller._stagger_trading_wallets(services)

        self.assertEqual(sleep.await_count, 2)
        sleep.assert_any_await(2.5)
        self.assertTrue(all(service.account_state.call_count == 1 for service in services))

    async def test_wallet_start_delay_does_not_slow_balance_mode(self) -> None:
        controller = object.__new__(Controller)
        controller._load_wallets = Mock(return_value=[wallet(0)])
        controller._prepare_wallets = AsyncMock(return_value=[wallet(0)])
        with (
            patch("lighter_bot.controller.LighterService") as service_class,
            patch.object(controller, "_stagger_trading_wallets", AsyncMock()) as stagger,
        ):
            services = await controller._services(require_trading=False)

        self.assertEqual(services, [service_class.return_value])
        stagger.assert_not_awaited()

    async def test_poll_interval_controls_spread_checks(self) -> None:
        controller = object.__new__(Controller)
        controller.public = SimpleNamespace(
            book=Mock(
                side_effect=[
                    BookSnapshot(Decimal("100"), Decimal("101")),
                    BookSnapshot(Decimal("100"), Decimal("100.01")),
                ]
            )
        )
        sleep = AsyncMock()

        with (
            patch("lighter_bot.controller.POLL_INTERVAL_SEC", 2.5),
            patch("lighter_bot.controller.asyncio.sleep", sleep),
        ):
            result = await controller._wait_for_spread(SimpleNamespace(market_id=0))

        self.assertEqual(result.best_ask, Decimal("100.01"))
        sleep.assert_awaited_once_with(2.5)


if __name__ == "__main__":
    unittest.main()
