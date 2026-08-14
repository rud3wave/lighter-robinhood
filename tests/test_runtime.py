from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main as app_main
from lighter_bot import runtime_control


class OneShotMainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.controller = Mock()
        self.controller.run_trades = AsyncMock()
        self.controller.close_positions = AsyncMock()
        self.controller.balances = AsyncMock()
        self.controller.deposit_from_wallets = AsyncMock()

    async def _run(self, choice: str) -> None:
        with (
            patch.object(app_main, "Controller", return_value=self.controller),
            patch.object(app_main, "banner"),
            patch.object(app_main, "info"),
            patch.object(app_main, "error"),
            patch("builtins.input", side_effect=[choice, AssertionError("menu repeated")]),
        ):
            await app_main.main()

    async def test_balance_mode_runs_once_and_exits(self) -> None:
        await self._run("3")
        self.controller.balances.assert_awaited_once()

    async def test_deposit_mode_runs_once_and_exits(self) -> None:
        await self._run("4")
        self.controller.deposit_from_wallets.assert_awaited_once()

    async def test_close_mode_requests_halt_runs_once_and_exits(self) -> None:
        with patch.object(app_main, "request_trading_halt") as request_halt:
            await self._run("2")
        request_halt.assert_called_once()
        self.controller.close_positions.assert_awaited_once()


class RuntimeControlTests(unittest.TestCase):
    def test_lock_and_halt_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "trading.lock"
            halt_path = root / "halt"
            with (
                patch.object(runtime_control, "LOCK_PATH", lock_path),
                patch.object(runtime_control, "HALT_PATH", halt_path),
            ):
                runtime_control.acquire_trading_lock()
                self.assertTrue(lock_path.exists())
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    runtime_control.acquire_trading_lock()
                runtime_control.request_trading_halt()
                self.assertTrue(runtime_control.is_trading_halted())
                runtime_control.clear_trading_halt()
                self.assertFalse(runtime_control.is_trading_halted())
                runtime_control.release_trading_lock()
                self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
