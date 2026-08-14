from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


PositionSide = Literal["long", "short"]


@dataclass(frozen=True)
class Allocation:
    service: Any
    balance: Decimal
    side: PositionSide
    leverage: Decimal
    notional: Decimal


def _random_decimal(bounds: list[int | float]) -> Decimal:
    low, high = bounds
    if low == high:
        return Decimal(str(low))
    return Decimal(str(random.uniform(float(low), float(high))))


def _leverage_bounds(bounds: list[int | float]) -> tuple[Decimal, Decimal]:
    low, high = sorted(Decimal(str(value)) for value in bounds)
    return max(Decimal(1), low), max(Decimal(1), high)


def _random_leverage(bounds: tuple[Decimal, Decimal]) -> Decimal:
    low, high = bounds
    if low == high:
        return low.quantize(Decimal("0.1"))
    value = Decimal(str(random.uniform(float(low), float(high))))
    return value.quantize(Decimal("0.1"))


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


def calculate_allocation(
    accounts: list[tuple[Any, Decimal]],
    long_count: int,
    leverage_bounds: list[int | float],
    percent_bounds: list[int | float],
    maker_min_notional: Decimal,
    taker_min_notional: Decimal,
    preferred_source_side: PositionSide | None = None,
    attempts: int = 1000,
) -> list[Allocation]:
    if long_count <= 0 or long_count >= len(accounts):
        raise RuntimeError("A delta-neutral group requires both long and short accounts")

    min_leverage, max_leverage = _leverage_bounds(leverage_bounds)
    min_percent, max_percent = sorted(Decimal(str(value)) for value in percent_bounds)
    sorted_accounts = sorted(accounts, key=lambda item: item[1], reverse=True)
    short_count = len(accounts) - long_count
    source_count = min(long_count, short_count)
    source_side: PositionSide = "long" if long_count <= short_count else "short"
    if preferred_source_side is not None:
        preferred_count = long_count if preferred_source_side == "long" else short_count
        if preferred_count != source_count:
            raise RuntimeError("Preferred leader side must be the smaller side of the group")
        source_side = preferred_source_side
    target_side: PositionSide = "short" if source_side == "long" else "long"

    source_accounts = sorted_accounts[:source_count]
    target_accounts = sorted_accounts[source_count:]

    for _ in range(attempts):
        source: list[Allocation] = []
        target_total = Decimal(0)
        valid = True
        for service, balance in source_accounts:
            leverage = _random_leverage((min_leverage, max_leverage))
            percent = _random_decimal(percent_bounds)
            notional = balance * percent / Decimal(100) * Decimal(leverage)
            if notional < maker_min_notional:
                valid = False
                break
            source.append(Allocation(service, balance, source_side, leverage, notional))
            target_total += notional
        if not valid:
            continue

        minimums: list[Decimal] = []
        maximums: list[Decimal] = []
        for _, balance in target_accounts:
            minimums.append(
                max(
                    taker_min_notional,
                    balance * min_percent / Decimal(100) * min_leverage,
                )
            )
            maximums.append(
                balance * max_percent / Decimal(100) * max_leverage
            )

        parts = _bounded_allocate(target_total, minimums, maximums)
        if parts is None:
            continue

        targets: list[Allocation] = []
        for (service, balance), notional in zip(target_accounts, parts):
            percent_low = max(
                min_percent,
                notional / (balance * max_leverage) * Decimal(100),
            )
            percent_high = min(
                max_percent,
                notional / (balance * min_leverage) * Decimal(100),
            )
            if percent_low > percent_high or percent_low <= 0:
                valid = False
                break
            percent = _random_decimal([float(percent_low), float(percent_high)])
            leverage = (notional / (balance * percent / Decimal(100))).quantize(Decimal("0.1"))
            if leverage < min_leverage or leverage > max_leverage:
                valid = False
                break
            targets.append(Allocation(service, balance, target_side, leverage, notional))
        if not valid:
            continue

        allocations = source + targets
        if any(item.notional / item.balance > Decimal(10) for item in allocations):
            continue
        long_total = sum(
            (item.notional for item in allocations if item.side == "long"),
            Decimal(0),
        )
        short_total = sum(
            (item.notional for item in allocations if item.side == "short"),
            Decimal(0),
        )
        if abs(long_total - short_total) <= Decimal("0.00000001"):
            return allocations

    raise RuntimeError(f"Unable to calculate balanced allocation after {attempts} attempts")
