from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lighter_bot.controller import Controller
from lighter_bot.service import LighterService


class PointsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_live_points_from_the_web_app_endpoint(self) -> None:
        client = Mock()
        client.create_auth_token_with_expiry.return_value = ("auth-token", None)
        service = object.__new__(LighterService)
        service.wallet = SimpleNamespace(
            can_trade=True,
            api_key_index=4,
            account_index=3181,
        )
        service.client = client
        service.public = Mock()
        service.public._get.return_value = {
            "code": 200,
            "total_live_points": 0.075403,
        }

        value = await service.points()

        self.assertEqual(value, Decimal("0.075403"))
        service.public._get.assert_called_once_with(
            "/api/v1/livePoints/total",
            account_index=3181,
            auth="auth-token",
        )


class PointsModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_each_wallet_and_sends_total_to_telegram(self) -> None:
        first = Mock()
        first.wallet = SimpleNamespace(address="0x" + "1" * 40)
        first.label.return_value = "0x1111...1111"
        first.points = AsyncMock(return_value=Decimal("0.075403"))
        first.close = AsyncMock()

        second = Mock()
        second.wallet = SimpleNamespace(address="0x" + "2" * 40)
        second.label.return_value = "0x2222...2222"
        second.points = AsyncMock(return_value=Decimal("0.1"))
        second.close = AsyncMock()

        controller = object.__new__(Controller)
        controller._services = AsyncMock(return_value=[first, second])

        with (
            patch("lighter_bot.controller.section"),
            patch("lighter_bot.controller.plain") as plain,
            patch("lighter_bot.controller.ok"),
            patch("lighter_bot.controller.send_tg", new_callable=AsyncMock) as telegram,
        ):
            await controller.points()

        controller._services.assert_awaited_once_with(require_trading=True)
        self.assertEqual(plain.call_args_list[0].args[1], "Points: 0.075403")
        message = telegram.await_args.args[0]
        self.assertIn("Points: 0.075403", message)
        self.assertIn("Всего Points: 0.175403", message)
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
