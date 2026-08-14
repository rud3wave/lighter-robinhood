from __future__ import annotations

import asyncio
import io
import random
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import patch

from lighter_bot.allocation import calculate_allocation
from lighter_bot.api import BookSnapshot, MarketMeta
from lighter_bot.paired_execution import (
    PairedTarget,
    allocate_targets,
    close_leader_follower,
    execute_paired,
    flatten_positions,
)
from lighter_bot.service import LighterService
from lighter_bot.wallets import WalletAccount
from lighter_bot import paired_execution


META = MarketMeta(
    symbol="ETH",
    market_id=0,
    size_decimals=4,
    price_decimals=2,
    min_base_amount=Decimal("0.0050"),
    min_quote_amount=Decimal("10"),
    max_leverage=Decimal("50"),
)
BOOK = BookSnapshot(best_bid=Decimal("1873.86"), best_ask=Decimal("1874.34"))


class FakePublic:
    def book(self, _: int) -> BookSnapshot:
        return BOOK


class FakeService:
    def __init__(
        self,
        name: str,
        maker_fills: list[Decimal] | None = None,
        market_fill_ratios: list[Decimal] | None = None,
    ):
        self.name = name
        self.public = FakePublic()
        self.dry_run = False
        self.position = Decimal(0)
        self.maker_fills = list(maker_fills or [])
        self.market_fill_ratios = list(market_fill_ratios or [Decimal(1)])
        self.maker_side = "long"
        self.maker_remaining = Decimal(0)
        self.active = False
        self.market_orders: list[Decimal] = []
        self.cancel_count = 0
        self.close_count = 0

    def label(self) -> str:
        return self.name

    @staticmethod
    def quantize_base_amount(meta: MarketMeta, amount: Decimal) -> Decimal:
        scale = Decimal(10) ** meta.size_decimals
        return (abs(amount) * scale // 1) / scale

    async def signed_position(self, _: str) -> Decimal:
        if self.active and self.maker_fills and self.maker_remaining > 0:
            fill = min(self.maker_remaining, self.maker_fills.pop(0))
            direction = Decimal(1) if self.maker_side == "long" else Decimal(-1)
            self.position += fill * direction
            self.maker_remaining -= fill
            if self.maker_remaining <= 0:
                self.active = False
        return self.position

    async def cancel_all_orders(self, _: MarketMeta) -> None:
        self.cancel_count += 1
        self.active = False

    async def place_post_only(
        self,
        _: MarketMeta,
        __: BookSnapshot,
        side: str,
        base_amount: Decimal,
        reduce_only: bool = False,
    ) -> tuple[int, Decimal]:
        self.maker_side = side
        self.maker_remaining = base_amount
        self.active = True
        return 101, BOOK.best_bid if side == "long" else BOOK.best_ask

    async def has_active_order(self, _: MarketMeta, __: int | None = None) -> bool:
        return self.active

    async def place_market(
        self,
        _: MarketMeta,
        __: BookSnapshot,
        side: str,
        amount_usd: Decimal,
        reduce_only: bool = False,
        base_amount_override: Decimal | None = None,
    ) -> int:
        del amount_usd
        amount = base_amount_override or Decimal(0)
        ratio = self.market_fill_ratios.pop(0) if self.market_fill_ratios else Decimal(1)
        filled = self.quantize_base_amount(META, amount * ratio)
        self.market_orders.append(amount)
        direction = Decimal(1) if side == "long" else Decimal(-1)
        if reduce_only:
            next_position = self.position + filled * direction
            if self.position > 0:
                self.position = max(Decimal(0), next_position)
            elif self.position < 0:
                self.position = min(Decimal(0), next_position)
        else:
            self.position += filled * direction
        return len(self.market_orders)

    async def close_position(self, _: MarketMeta) -> None:
        self.close_count += 1
        self.position = Decimal(0)


class FakeReadErrorService(FakeService):
    def __init__(self, name: str):
        super().__init__(name)
        self.fail_reads = 1

    async def signed_position(self, symbol: str) -> Decimal:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise RuntimeError("temporary position read error")
        return await super().signed_position(symbol)


class AllocationTests(unittest.TestCase):
    def test_allocation_is_quote_neutral(self) -> None:
        random.seed(7)
        accounts = [(object(), Decimal("20")), (object(), Decimal("20"))]
        result = calculate_allocation(
            accounts,
            long_count=1,
            leverage_bounds=[2, 2],
            percent_bounds=[25, 25],
            maker_min_notional=Decimal("10"),
            taker_min_notional=Decimal(0),
            preferred_source_side="short",
        )
        long_total = sum((item.notional for item in result if item.side == "long"), Decimal(0))
        short_total = sum((item.notional for item in result if item.side == "short"), Decimal(0))
        self.assertEqual(long_total, short_total)
        self.assertEqual(result[0].side, "short")

    def test_impossible_allocation_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unable to calculate"):
            calculate_allocation(
                [(object(), Decimal("1")), (object(), Decimal("1"))],
                long_count=1,
                leverage_bounds=[1, 1],
                percent_bounds=[10, 10],
                maker_min_notional=Decimal("10"),
                taker_min_notional=Decimal(0),
                attempts=3,
            )

    def test_five_wallet_phoenix_group_is_feasible(self) -> None:
        random.seed(17)
        balances = ["19.8942", "19.8942", "14.9086", "9.9230", "5.5855"]
        result = calculate_allocation(
            [(object(), Decimal(balance)) for balance in balances],
            long_count=3,
            leverage_bounds=[7, 15],
            percent_bounds=[85, 100],
            maker_min_notional=Decimal("10"),
            taker_min_notional=Decimal(0),
        )
        self.assertEqual(len(result), 5)
        long_total = sum((item.notional for item in result if item.side == "long"), Decimal(0))
        short_total = sum((item.notional for item in result if item.side == "short"), Decimal(0))
        self.assertEqual(long_total, short_total)
        self.assertEqual(sum(item.side == "long" for item in result), 3)

    def test_base_allocation_is_exact_after_rounding(self) -> None:
        services = [object(), object(), object()]
        targets = allocate_targets(
            Decimal("0.0123"),
            [(services[0], Decimal("1")), (services[1], Decimal("2")), (services[2], Decimal("3"))],
            META,
        )
        self.assertEqual(sum((target.target_base for target in targets), Decimal(0)), Decimal("0.0123"))
        self.assertTrue(all(target.target_base.as_tuple().exponent >= -4 for target in targets))


class ServiceContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_order_log_is_compact_and_human_readable(self) -> None:
        wallet = WalletAccount(
            index=4,
            private_key="0x" + "1" * 64,
            address="0xaFBc" + "0" * 32 + "1405",
            account_index=1,
            api_private_key="api-key",
        )
        service = LighterService(wallet, "https://example.invalid", dry_run=True)
        book = BookSnapshot(
            best_bid=Decimal("1875.16"),
            best_ask=Decimal("1875.18"),
        )

        output = io.StringIO()
        with patch("lighter_bot.pretty.USE_COLOR", False), redirect_stdout(output):
            await service.place_market(
                META,
                book,
                "short",
                Decimal("13.69"),
                reduce_only=True,
                base_amount_override=Decimal("0.0073"),
            )

        line = output.getvalue()
        self.assertIn(
            "0xaFBc...1405 SHORT | $13.69 | 0.0073 ETH | ENTRY 1874,78",
            line,
        )
        self.assertNotIn("wallet[4]", line)
        self.assertNotIn("reduce-only", line)
        self.assertNotIn("price_i", line)

    async def test_immediate_cancel_uses_nil_timestamp(self) -> None:
        class FakeClient:
            CANCEL_ALL_TIF_IMMEDIATE = 0

            def __init__(self):
                self.args = None

            async def cancel_all_orders(self, *args, **kwargs):
                self.args = (args, kwargs)
                return None, type("Response", (), {"code": 200})(), None

        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x" + "2" * 40,
            account_index=1,
            api_private_key="api-key",
        )
        service = LighterService(wallet, "https://example.invalid", dry_run=True)
        service.dry_run = False
        service.client = FakeClient()
        await service.cancel_all_orders(META)
        args, kwargs = service.client.args
        self.assertEqual(args[:2], (service.client.CANCEL_ALL_TIF_IMMEDIATE, 0))
        self.assertEqual(kwargs["cancel_all_market_index"], META.market_id)


class PairedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.originals = {
            "EXECUTION_MODE": paired_execution.EXECUTION_MODE,
            "HEDGE_POLL_INTERVAL_SECONDS": paired_execution.HEDGE_POLL_INTERVAL_SECONDS,
            "HEDGE_SETTLE_SECONDS": paired_execution.HEDGE_SETTLE_SECONDS,
            "MAKER_REQUOTE_INTERVAL_SECONDS": paired_execution.MAKER_REQUOTE_INTERVAL_SECONDS,
            "MAX_MAKER_WAIT_SECONDS": paired_execution.MAX_MAKER_WAIT_SECONDS,
        }
        paired_execution.EXECUTION_MODE = "leader-follower"
        paired_execution.HEDGE_POLL_INTERVAL_SECONDS = 0.001
        paired_execution.HEDGE_SETTLE_SECONDS = 0.003
        paired_execution.MAKER_REQUOTE_INTERVAL_SECONDS = 0.01
        paired_execution.MAX_MAKER_WAIT_SECONDS = 0.2

    async def asyncTearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(paired_execution, name, value)

    async def test_partial_leader_fills_are_hedged_incrementally(self) -> None:
        maker = FakeService("maker", maker_fills=[Decimal("0.0020"), Decimal("0.0040")])
        follower = FakeService("follower")
        await execute_paired(
            META,
            "long",
            "short",
            [PairedTarget(maker, Decimal("0.0060"))],
            [PairedTarget(follower, Decimal("0.0060"))],
        )
        self.assertEqual(maker.position, Decimal("0.0060"))
        self.assertEqual(follower.position, Decimal("-0.0060"))
        self.assertEqual(follower.market_orders, [Decimal("0.0020"), Decimal("0.0040")])
        self.assertGreaterEqual(maker.cancel_count, 2)

    async def test_partial_ioc_is_retried_from_observed_position(self) -> None:
        maker = FakeService("maker", maker_fills=[Decimal("0.0060")])
        follower = FakeService(
            "follower",
            market_fill_ratios=[Decimal("0.5"), Decimal(1)],
        )
        await execute_paired(
            META,
            "long",
            "short",
            [PairedTarget(maker, Decimal("0.0060"))],
            [PairedTarget(follower, Decimal("0.0060"))],
        )
        self.assertEqual(follower.position, Decimal("-0.0060"))
        self.assertEqual(follower.market_orders, [Decimal("0.0060"), Decimal("0.0030")])

    async def test_two_leaders_hedge_three_followers_in_parallel(self) -> None:
        makers = [
            FakeService("maker-1", maker_fills=[Decimal("0.0060")]),
            FakeService("maker-2", maker_fills=[Decimal("0.0060")]),
        ]
        followers = [
            FakeService("follower-1"),
            FakeService("follower-2"),
            FakeService("follower-3"),
        ]
        await execute_paired(
            META,
            "short",
            "long",
            [PairedTarget(service, Decimal("0.0060")) for service in makers],
            [
                PairedTarget(followers[0], Decimal("0.0059")),
                PairedTarget(followers[1], Decimal("0.0039")),
                PairedTarget(followers[2], Decimal("0.0022")),
            ],
        )
        self.assertEqual(
            [service.position for service in makers],
            [Decimal("-0.0060"), Decimal("-0.0060")],
        )
        self.assertEqual(
            [service.position for service in followers],
            [Decimal("0.0059"), Decimal("0.0039"), Decimal("0.0022")],
        )
        self.assertTrue(all(len(service.market_orders) == 1 for service in followers))

    async def test_timeout_always_cancels_maker(self) -> None:
        maker = FakeService("maker")
        follower = FakeService("follower")
        paired_execution.MAX_MAKER_WAIT_SECONDS = 0.025
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            await execute_paired(
                META,
                "long",
                "short",
                [PairedTarget(maker, Decimal("0.0060"))],
                [PairedTarget(follower, Decimal("0.0060"))],
            )
        self.assertFalse(maker.active)
        self.assertGreaterEqual(maker.cancel_count, 2)

    async def test_follower_overfill_is_rejected(self) -> None:
        maker = FakeService("maker", maker_fills=[Decimal("0.0060")])
        follower = FakeService("follower", market_fill_ratios=[Decimal("1.2")])
        with self.assertRaisesRegex(RuntimeError, "Per-wallet fill mismatch"):
            await execute_paired(
                META,
                "long",
                "short",
                [PairedTarget(maker, Decimal("0.0060"))],
                [PairedTarget(follower, Decimal("0.0060"))],
            )

    async def test_flatten_rechecks_positions(self) -> None:
        long = FakeService("long")
        short = FakeService("short")
        long.position = Decimal("0.0060")
        short.position = Decimal("-0.0060")
        await flatten_positions([long, short], META)
        self.assertEqual(long.position, 0)
        self.assertEqual(short.position, 0)
        self.assertEqual(long.close_count, 1)
        self.assertEqual(short.close_count, 1)

    async def test_close_uses_leader_follower_then_verifies_flat(self) -> None:
        makers = [
            FakeService("maker-1", maker_fills=[Decimal("0.0060")]),
            FakeService("maker-2", maker_fills=[Decimal("0.0060")]),
        ]
        followers = [
            FakeService("follower-1"),
            FakeService("follower-2"),
            FakeService("follower-3"),
        ]
        for service in makers:
            service.position = Decimal("-0.0060")
        followers[0].position = Decimal("0.0059")
        followers[1].position = Decimal("0.0039")
        followers[2].position = Decimal("0.0022")

        await close_leader_follower(makers, followers, META)

        self.assertTrue(all(service.position == 0 for service in makers + followers))
        self.assertTrue(all(service.close_count == 0 for service in makers + followers))
        self.assertTrue(all(service.market_orders for service in followers))

    async def test_all_market_close_flattens_every_wallet(self) -> None:
        paired_execution.EXECUTION_MODE = "all-market"
        makers = [FakeService("maker-1"), FakeService("maker-2")]
        followers = [FakeService("follower-1"), FakeService("follower-2")]
        for service in makers:
            service.position = Decimal("-0.0060")
        for service in followers:
            service.position = Decimal("0.0060")

        await close_leader_follower(makers, followers, META)

        self.assertTrue(all(service.position == 0 for service in makers + followers))
        self.assertTrue(all(service.close_count == 1 for service in makers + followers))

    async def test_close_read_error_still_flattens_every_wallet(self) -> None:
        maker = FakeReadErrorService("maker")
        follower = FakeService("follower")
        maker.position = Decimal("-0.0060")
        follower.position = Decimal("0.0060")

        await close_leader_follower([maker], [follower], META)

        self.assertEqual(maker.position, 0)
        self.assertEqual(follower.position, 0)
        self.assertEqual(maker.close_count, 1)
        self.assertEqual(follower.close_count, 1)


if __name__ == "__main__":
    unittest.main()
