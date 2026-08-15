from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lighter_bot.telegram import send_tg


class TelegramFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_numbers_are_compacted_before_sending(self) -> None:
        config = SimpleNamespace(
            telegram=SimpleNamespace(enabled=True, token="token", chat_id="chat")
        )
        with (
            patch("lighter_bot.telegram.load_global_config", return_value=config),
            patch("lighter_bot.telegram._send") as sender,
        ):
            sent = await send_tg("PnL: +0.00000213$ | balance: $12.500000")

        self.assertTrue(sent)
        sender.assert_called_once_with("PnL: 0$ | balance: $12.5")


if __name__ == "__main__":
    unittest.main()
