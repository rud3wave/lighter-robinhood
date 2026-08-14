import asyncio
import random
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable


def short_id(value: int | str) -> str:
    text = str(value)
    return text if len(text) <= 10 else f"{text[:6]}...{text[-4:]}"


def pick_range(bounds: list[int | float]) -> float:
    if len(bounds) != 2:
        raise ValueError("range settings must be [min, max]")
    lo, hi = bounds
    return float(lo) if lo == hi else random.uniform(float(lo), float(hi))


def now_ms() -> int:
    return int(time.time() * 1000)


def quantize_decimal(value: Decimal, decimals: int, rounding) -> int:
    scale = Decimal(10) ** decimals
    return int((value * scale).to_integral_value(rounding=rounding))


def base_to_int(base_amount: Decimal, decimals: int) -> int:
    return max(1, quantize_decimal(base_amount, decimals, ROUND_FLOOR))


def quantize_base(base_amount: Decimal, decimals: int) -> Decimal:
    scale = Decimal(10) ** decimals
    units = (base_amount * scale).to_integral_value(rounding=ROUND_FLOOR)
    return units / scale


def price_to_int(price: Decimal, decimals: int, is_buy: bool) -> int:
    rounding = ROUND_CEILING if is_buy else ROUND_FLOOR
    return max(1, quantize_decimal(price, decimals, rounding))


def maker_price_to_int(price: Decimal, decimals: int, is_buy: bool) -> int:
    # A maker buy must not round above the bid; a maker sell must not round below the ask.
    rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
    return max(1, quantize_decimal(price, decimals, rounding))


async def gather_limited(limit: int, coros: Iterable):
    semaphore = asyncio.Semaphore(limit)

    async def run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(run(coro) for coro in coros))
