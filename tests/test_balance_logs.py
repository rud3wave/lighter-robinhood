from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lighter_bot.controller import Controller


class FakeBalanceService:
    def __init__(self) -> None:
        self.wallet = SimpleNamespace(index=0, address="0x6874" + "0" * 32 + "4da5")

    def label(self) -> str:
        return "0x6874...4da5"

    @staticmethod
    def account_state() -> dict:
        return {
            "available_balance": "0.2982",
            "collateral": "9.8992",
            "positions": [
                {"position": "0.0497", "symbol": "ETH", "sign": -1},
            ],
        }

    async def close(self) -> None:
        return None


class BalanceLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_logs_hide_status_sign_and_reward_metadata(self) -> None:
        service = FakeBalanceService()
        controller = object.__new__(Controller)
        controller._services = AsyncMock(return_value=[service])
        telegram = AsyncMock(return_value=True)
        output = io.StringIO()

        with (
            patch("lighter_bot.pretty.USE_COLOR", False),
            patch("lighter_bot.controller.send_tg", telegram),
            redirect_stdout(output),
        ):
            await controller.balances()

        console = output.getvalue()
        self.assertIn(
            "0x6874...4da5 | available=$0.2982 | collateral=$9.8992 | "
            "position=0.0497 ETH",
            console,
        )
        self.assertNotIn("INFO 0x6874...4da5", console)
        self.assertNotIn("sign=", console)
        self.assertNotIn("reward_point_multiplier", console)

        telegram_message = telegram.await_args.args[0]
        self.assertIn("0x6874...4da5 | $0.2982 | 0.0497 ETH", telegram_message)
        self.assertNotIn("sign=", telegram_message)


if __name__ == "__main__":
    unittest.main()
