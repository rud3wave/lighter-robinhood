from __future__ import annotations

import unittest
from decimal import Decimal

from lighter_bot.trade_report import (
    TradeMetrics,
    discover_open_timestamp,
    summarize_trades,
)


def trade(
    account_index: int,
    timestamp: int,
    usd_amount: str,
    pnl: str | None,
    position_before: str,
    *,
    account_is_maker: bool = False,
    maker_fee: int = 0,
    taker_fee: int = 0,
    integrator_maker_fee: int = 0,
    integrator_taker_fee: int = 0,
) -> dict:
    return {
        "ask_account_id": 999,
        "bid_account_id": account_index,
        "is_maker_ask": not account_is_maker,
        "taker_position_size_before": position_before,
        "maker_position_size_before": "1",
        "maker_fee": maker_fee,
        "taker_fee": taker_fee,
        "integrator_maker_fee": integrator_maker_fee,
        "integrator_taker_fee": integrator_taker_fee,
        "bid_account_pnl": pnl,
        "ask_account_pnl": None,
        "usd_amount": usd_amount,
        "timestamp": timestamp,
    }


class TradeReportTests(unittest.TestCase):
    def test_force_close_uses_realized_fills_not_released_margin(self) -> None:
        rows = [
            (3165, "17.445498", "-0.006570"),
            (2090, "9.754472", "-0.003674"),
            (3186, "23.632686", "-0.000504"),
            (3181, "29.822199", "-0.000636"),
            (3166, "26.262040", "-0.009891"),
        ]
        total = TradeMetrics()
        for account_index, volume, pnl in rows:
            total += summarize_trades(
                [trade(account_index, 2000, volume, pnl, "0.01")],
                account_index,
            )

        self.assertEqual(total.realized_pnl, Decimal("-0.021275"))
        self.assertEqual(total.net_pnl, Decimal("-0.021275"))
        self.assertEqual(total.volume, Decimal("106.916895"))
        self.assertNotEqual(total.net_pnl, Decimal("12.3860"))

    def test_opening_fill_and_funding_are_included(self) -> None:
        account_index = 10
        trades = [
            trade(account_index, 1000, "10", None, "0"),
            trade(account_index, 2000, "10.1", "-0.02", "0.005"),
        ]
        funding = [{"change": "0.003"}]

        self.assertEqual(discover_open_timestamp(trades, account_index), 1000)
        metrics = summarize_trades(trades, account_index, funding)
        self.assertEqual(metrics.realized_pnl, Decimal("-0.02"))
        self.assertEqual(metrics.funding, Decimal("0.003"))
        self.assertEqual(metrics.trading_fee, Decimal("0"))
        self.assertEqual(metrics.net_pnl, Decimal("-0.017"))
        self.assertEqual(metrics.volume, Decimal("20.1"))

    def test_actual_maker_taker_and_integrator_fees_reduce_net_pnl(self) -> None:
        account_index = 10
        trades = [
            trade(
                account_index,
                1000,
                "100",
                "1",
                "0",
                taker_fee=280,
                maker_fee=9999,
                integrator_taker_fee=20,
                integrator_maker_fee=9999,
            ),
            trade(
                account_index,
                2000,
                "50",
                None,
                "1",
                account_is_maker=True,
                maker_fee=40,
                taker_fee=9999,
                integrator_maker_fee=10,
                integrator_taker_fee=9999,
            ),
        ]

        metrics = summarize_trades(trades, account_index, [{"change": "0.1"}])

        self.assertEqual(metrics.maker_fee, Decimal("0.002"))
        self.assertEqual(metrics.taker_fee, Decimal("0.028"))
        self.assertEqual(metrics.integrator_fee, Decimal("0.0025"))
        self.assertEqual(metrics.trading_fee, Decimal("0.0325"))
        self.assertEqual(metrics.net_pnl, Decimal("1.0675"))

    def test_ask_side_uses_the_queried_accounts_fee_role(self) -> None:
        account_index = 10
        row = {
            "ask_account_id": account_index,
            "bid_account_id": 999,
            "is_maker_ask": True,
            "maker_fee": 40,
            "taker_fee": 9999,
            "integrator_maker_fee": 10,
            "integrator_taker_fee": 9999,
            "ask_account_pnl": "0.5",
            "bid_account_pnl": None,
            "usd_amount": "25",
            "timestamp": 1000,
        }

        metrics = summarize_trades([row], account_index)

        self.assertEqual(metrics.maker_fee, Decimal("0.001"))
        self.assertEqual(metrics.integrator_fee, Decimal("0.00025"))
        self.assertEqual(metrics.trading_fee, Decimal("0.00125"))


if __name__ == "__main__":
    unittest.main()
