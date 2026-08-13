import asyncio
import random
from decimal import Decimal
from pathlib import Path

from settings import (
    API_BASE_URL,
    DELAY_BETWEEN_TRADES,
    DRY_RUN,
    GROUP_CONFIGS,
    HOLD_MINUTES,
    MAX_SPREAD,
    POSITION_PERCENT,
    SHUFFLE_ACCOUNTS,
    SYMBOL,
    TOKEN_LEVERAGE,
    TRADES_COUNT,
)

from .accounts import load_accounts
from .api import LighterPublicApi
from .service import LighterService
from .utils import pick_range


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PATH = ROOT / "input_data" / "accounts.csv"


class Controller:
    def __init__(self):
        self.public = LighterPublicApi(API_BASE_URL)
        self.meta = self.public.market_meta(SYMBOL)

    def market_info(self) -> None:
        book = self.public.book(self.meta.market_id)
        print(f"\nMarket: {self.meta.symbol} | id={self.meta.market_id}")
        print(f"Best bid/ask: {book.best_bid} / {book.best_ask} | spread={book.spread_percent:.4f}%")
        print(
            f"Min base={self.meta.min_base_amount}, min quote=${self.meta.min_quote_amount}, "
            f"size_dec={self.meta.size_decimals}, price_dec={self.meta.price_decimals}, max lev={self.meta.max_leverage}x"
        )

    async def _services(self) -> list[LighterService]:
        accounts = load_accounts(ACCOUNTS_PATH, SHUFFLE_ACCOUNTS)
        if not accounts:
            raise RuntimeError(f"No Lighter accounts configured in {ACCOUNTS_PATH}")
        return [LighterService(acc, API_BASE_URL, DRY_RUN) for acc in accounts]

    async def balances(self) -> None:
        services = await self._services()
        try:
            total = Decimal(0)
            print("\nBalances")
            for svc in services:
                state = svc.account_state()
                available = Decimal(str(state.get("available_balance", "0")))
                collateral = Decimal(str(state.get("collateral", "0")))
                total += available
                pos = svc.position(SYMBOL)
                pos_text = "flat" if not pos else f"{pos['position']} {SYMBOL} sign={pos['sign']}"
                print(f"  {svc.label()} available=${available:.4f} collateral=${collateral:.4f} position={pos_text}")
            print(f"  Total available: ${total:.4f}")
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def cancel_all(self) -> None:
        services = await self._services()
        try:
            await asyncio.gather(*(svc.cancel_all_orders(self.meta) for svc in services))
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def close_positions(self) -> None:
        services = await self._services()
        try:
            await asyncio.gather(*(svc.close_position(self.meta) for svc in services))
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def trade_cycle(self) -> None:
        services = await self._services()
        try:
            group = random.choice(GROUP_CONFIGS)
            needed = sum(group)
            if len(services) < needed:
                raise RuntimeError(f"Need {needed} accounts for group {group}, got {len(services)}")
            selected = services[:needed]
            longs = selected[: group[0]]
            shorts = selected[group[0] :]
            print(f"\nDRY_RUN={DRY_RUN} | {SYMBOL} group: {len(longs)} long / {len(shorts)} short")

            book = self.public.book(self.meta.market_id)
            if book.spread_percent > Decimal(str(MAX_SPREAD)):
                raise RuntimeError(f"Spread {book.spread_percent:.4f}% exceeds MAX_SPREAD={MAX_SPREAD}%")

            leverage = int(min(pick_range(TOKEN_LEVERAGE[SYMBOL]), float(self.meta.max_leverage)))
            pct = Decimal(str(pick_range(POSITION_PERCENT))) / Decimal(100)
            for svc in selected:
                await svc.update_leverage(self.meta, leverage)

            async def open_side(svc: LighterService, side: str):
                margin = svc.available_balance() * pct
                amount_usd = margin * Decimal(leverage)
                await svc.place_market(self.meta, book, side, amount_usd)

            await asyncio.gather(
                *(open_side(svc, "long") for svc in longs),
                *(open_side(svc, "short") for svc in shorts),
            )

            hold = pick_range(HOLD_MINUTES)
            if hold > 0:
                print(f"\nHolding for {hold:.2f} minute(s)")
                await asyncio.sleep(hold * 60)
                await self.close_positions()
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def run_trades(self) -> None:
        for idx in range(TRADES_COUNT):
            print(f"\nCycle {idx + 1}/{TRADES_COUNT}")
            await self.trade_cycle()
            if idx + 1 < TRADES_COUNT:
                delay = pick_range(DELAY_BETWEEN_TRADES)
                print(f"Sleeping {delay:.1f}s")
                await asyncio.sleep(delay)

