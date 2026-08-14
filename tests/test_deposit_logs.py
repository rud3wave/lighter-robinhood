from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from lighter_bot.deposit import DepositPlan, deposit_token
from lighter_bot.wallets import WalletAccount


class DepositLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_deposit_log_contains_only_user_facing_values(self) -> None:
        wallet = WalletAccount(
            index=3,
            private_key="0x" + "1" * 64,
            address="0x5DbC" + "0" * 32 + "BDA5",
        )
        plan = DepositPlan(
            w3=None,
            contract=None,
            amount=Decimal("12.5"),
            amount_i=12_500_000,
            balance=Decimal("12.5"),
            balance_i=12_500_000,
            min_transfer=Decimal("1"),
        )
        output = io.StringIO()

        with (
            patch("lighter_bot.pretty.USE_COLOR", False),
            patch("lighter_bot.deposit._prepare_deposit", return_value=plan),
            patch(
                "lighter_bot.deposit._intent_address",
                AsyncMock(return_value="0x" + "2" * 40),
            ),
            patch(
                "lighter_bot.deposit._send_deposit",
                return_value=("0x" + "3" * 64, 123456),
            ),
            redirect_stdout(output),
        ):
            await deposit_token(wallet)

        log = output.getvalue()
        self.assertIn("0x5DbC...BDA5 | баланс=12.500000 USDG | депозит=12.500000 USDG", log)
        self.assertIn("задепано 12.500000 USDG | tx 0x3333...3333", log)
        self.assertNotIn("wallet[3]", log)
        self.assertNotIn("block", log)
        self.assertNotIn("tx sent", log)


if __name__ == "__main__":
    unittest.main()
