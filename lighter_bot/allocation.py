from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Literal

from settings import SHUFFLE_WALLETS


PositionSide = Literal["long", "short"]

_SPLIT_ATTEMPTS = 50


@dataclass(frozen=True)
class Allocation:
    service: Any
    balance: Decimal
    side: PositionSide
    leverage: Decimal
    notional: Decimal


def _leverage_bounds(bounds: list[int | float]) -> tuple[Decimal, Decimal]:
    low, high = sorted(Decimal(str(value)) for value in bounds)
    return max(Decimal(1), low), max(Decimal(1), high)


def _random_between(low: Decimal, high: Decimal) -> Decimal:
    if low == high:
        return low
    return low + (high - low) * Decimal(str(random.random()))


def _bounded_allocate(
    total: Decimal,
    minimums: list[Decimal],
    maximums: list[Decimal],
) -> list[Decimal] | None:
    if not minimums or len(minimums) != len(maximums):
        return None
    if total < sum(minimums, Decimal(0)) or total > sum(maximums, Decimal(0)):
        return None

    remaining = total
    parts: list[Decimal] = []
    for index, (minimum, maximum) in enumerate(zip(minimums, maximums)):
        if index == len(minimums) - 1:
            part = remaining
        else:
            rest_min = sum(minimums[index + 1 :], Decimal(0))
            rest_max = sum(maximums[index + 1 :], Decimal(0))
            low = max(minimum, remaining - rest_max)
            high = min(maximum, remaining - rest_min)
            if low > high:
                return None
            weight = maximum / sum(maximums[index:], Decimal(0))
            part = min(high, max(low, remaining * weight))
        parts.append(part)
        remaining -= part
    return parts if abs(remaining) < Decimal("0.00000001") else None


def _notional_bounds(
    accounts: list[tuple[Any, Decimal]],
    min_percent: Decimal,
    max_percent: Decimal,
    min_leverage: Decimal,
    max_leverage: Decimal,
    minimum_notional: Decimal,
) -> tuple[list[Decimal], list[Decimal]]:
    minimums = [
        max(
            minimum_notional,
            balance * min_percent / Decimal(100) * min_leverage,
        )
        for _, balance in accounts
    ]
    maximums = [
        balance * max_percent / Decimal(100) * max_leverage
        for _, balance in accounts
    ]
    return minimums, maximums


def _pick_leverage(
    notional: Decimal,
    balance: Decimal,
    min_percent: Decimal,
    max_percent: Decimal,
    min_leverage: Decimal,
    max_leverage: Decimal,
) -> Decimal:
    leverage_low = max(
        min_leverage,
        notional / (balance * max_percent / Decimal(100)),
    )
    leverage_high = min(
        max_leverage,
        notional / (balance * min_percent / Decimal(100)),
    )
    low_tick = int(
        (leverage_low * Decimal(10)).to_integral_value(rounding=ROUND_CEILING)
    )
    high_tick = int(
        (leverage_high * Decimal(10)).to_integral_value(rounding=ROUND_FLOOR)
    )
    if low_tick > high_tick:
        raise RuntimeError("Не удалось подобрать плечо с шагом 0.1x")
    return Decimal(random.randint(low_tick, high_tick)) / Decimal(10)


def _build_side(
    accounts: list[tuple[Any, Decimal]],
    side: PositionSide,
    parts: list[Decimal],
    min_percent: Decimal,
    max_percent: Decimal,
    min_leverage: Decimal,
    max_leverage: Decimal,
) -> list[Allocation]:
    return [
        Allocation(
            service=service,
            balance=balance,
            side=side,
            leverage=_pick_leverage(
                notional,
                balance,
                min_percent,
                max_percent,
                min_leverage,
                max_leverage,
            ),
            notional=notional,
        )
        for (service, balance), notional in zip(accounts, parts)
    ]


def calculate_allocation(
    accounts: list[tuple[Any, Decimal]],
    long_count: int,
    leverage_bounds: list[int | float],
    percent_bounds: list[int | float],
    maker_min_notional: Decimal,
    taker_min_notional: Decimal,
    preferred_source_side: PositionSide | None = None,
) -> list[Allocation]:
    if long_count <= 0 or long_count >= len(accounts):
        raise RuntimeError("A delta-neutral group requires both long and short accounts")
    if any(balance <= 0 for _, balance in accounts):
        raise RuntimeError("Для торговли баланс каждого кошелька должен быть больше 0")

    short_count = len(accounts) - long_count
    source_count = min(long_count, short_count)
    source_side: PositionSide = "long" if long_count <= short_count else "short"
    if preferred_source_side is not None:
        preferred_count = long_count if preferred_source_side == "long" else short_count
        if preferred_count != source_count:
            raise RuntimeError("Preferred leader side must be the smaller side of the group")
        source_side = preferred_source_side

    # Окно выравнивания узкое: состав сторон ищется перебором случайных
    # разбивок, разбивка по балансу остаётся последним запасным вариантом.
    orderings: list[list[tuple[Any, Decimal]]] = []
    if SHUFFLE_WALLETS:
        for _ in range(_SPLIT_ATTEMPTS):
            shuffled = list(accounts)
            random.shuffle(shuffled)
            orderings.append(shuffled)
    ranked = sorted(accounts, key=lambda item: item[1], reverse=True)
    orderings.append(ranked)

    last_error: RuntimeError | None = None
    for ordered in orderings:
        try:
            return _split_group(
                ordered,
                source_count,
                source_side,
                leverage_bounds,
                percent_bounds,
                maker_min_notional,
                taker_min_notional,
            )
        except RuntimeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _split_group(
    ordered_accounts: list[tuple[Any, Decimal]],
    source_count: int,
    source_side: PositionSide,
    leverage_bounds: list[int | float],
    percent_bounds: list[int | float],
    maker_min_notional: Decimal,
    taker_min_notional: Decimal,
) -> list[Allocation]:
    min_leverage, max_leverage = _leverage_bounds(leverage_bounds)
    min_percent, max_percent = sorted(Decimal(str(value)) for value in percent_bounds)
    target_side: PositionSide = "short" if source_side == "long" else "long"

    source_accounts = ordered_accounts[:source_count]
    target_accounts = ordered_accounts[source_count:]
    source_minimums, source_maximums = _notional_bounds(
        source_accounts,
        min_percent,
        max_percent,
        min_leverage,
        max_leverage,
        maker_min_notional,
    )
    target_minimums, target_maximums = _notional_bounds(
        target_accounts,
        min_percent,
        max_percent,
        min_leverage,
        max_leverage,
        taker_min_notional,
    )

    total_minimum = max(
        sum(source_minimums, Decimal(0)),
        sum(target_minimums, Decimal(0)),
    )
    total_maximum = min(
        sum(source_maximums, Decimal(0)),
        sum(target_maximums, Decimal(0)),
    )
    if total_minimum > total_maximum:
        raise RuntimeError(
            "Не получается выровнять LONG и SHORT с текущими балансами. "
            "Измени GROUP_CONFIGS, POSITION_PERCENT или TOKEN_LEVERAGE"
        )

    total = _random_between(total_minimum, total_maximum)
    source_parts = _bounded_allocate(total, source_minimums, source_maximums)
    target_parts = _bounded_allocate(total, target_minimums, target_maximums)
    if source_parts is None or target_parts is None:
        raise RuntimeError("Не удалось распределить общий объём между кошельками")

    source = _build_side(
        source_accounts,
        source_side,
        source_parts,
        min_percent,
        max_percent,
        min_leverage,
        max_leverage,
    )
    target = _build_side(
        target_accounts,
        target_side,
        target_parts,
        min_percent,
        max_percent,
        min_leverage,
        max_leverage,
    )
    return source + target
