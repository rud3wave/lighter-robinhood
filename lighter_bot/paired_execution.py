from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Literal

from settings import (
    EXECUTION_MODE,
    HEDGE_POLL_INTERVAL_SECONDS,
    HEDGE_SETTLE_SECONDS,
    MAKER_REQUOTE_INTERVAL_SECONDS,
    MAKER_REQUOTE_THRESHOLD_PERCENT,
    MAX_HEDGE_RETRIES,
    MAX_MAKER_WAIT_SECONDS,
    RESIDUAL_CLOSE_RETRIES,
)

from .api import BookSnapshot, MarketMeta
from .pretty import error, info, ok, step, warn


PositionSide = Literal["long", "short"]


@dataclass(frozen=True)
class PairedTarget:
    service: Any
    target_base: Decimal


@dataclass
class _TrackedTarget:
    service: Any
    target_base: Decimal
    start_position: Decimal


def _direction(side: PositionSide) -> Decimal:
    return Decimal(1) if side == "long" else Decimal(-1)


def _unit_scale(meta: MarketMeta) -> Decimal:
    return Decimal(10) ** meta.size_decimals


def _to_units(value: Decimal, meta: MarketMeta) -> int:
    return int((max(Decimal(0), value) * _unit_scale(meta)).to_integral_value(rounding=ROUND_FLOOR))


def _from_units(value: int, meta: MarketMeta) -> Decimal:
    return Decimal(value) / _unit_scale(meta)


def _allocate_units(
    total: Decimal,
    weights: list[Decimal],
    meta: MarketMeta,
    caps: list[Decimal] | None = None,
) -> list[Decimal]:
    total_units = _to_units(total, meta)
    if total_units <= 0 or not weights:
        return [Decimal(0) for _ in weights]

    normalized = [max(Decimal(0), weight) for weight in weights]
    weight_total = sum(normalized, Decimal(0))
    if weight_total <= 0:
        raise RuntimeError("Cannot allocate base target with zero total weight")

    cap_units = (
        [_to_units(cap, meta) for cap in caps]
        if caps is not None
        else [total_units for _ in weights]
    )
    if total_units > sum(cap_units):
        raise RuntimeError("Base target exceeds remaining follower capacity")

    raw = [Decimal(total_units) * weight / weight_total for weight in normalized]
    allocated = [
        min(cap, int(value.to_integral_value(rounding=ROUND_FLOOR)))
        for value, cap in zip(raw, cap_units)
    ]
    remaining = total_units - sum(allocated)
    order = sorted(
        range(len(weights)),
        key=lambda index: (raw[index] - Decimal(int(raw[index])), normalized[index]),
        reverse=True,
    )
    while remaining > 0:
        progressed = False
        for index in order:
            if allocated[index] >= cap_units[index]:
                continue
            allocated[index] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            raise RuntimeError("Unable to distribute quantized base target")
    return [_from_units(value, meta) for value in allocated]


def allocate_targets(
    total_base: Decimal,
    accounts: list[tuple[Any, Decimal]],
    meta: MarketMeta,
) -> list[PairedTarget]:
    if not accounts:
        return []
    amounts = _allocate_units(total_base, [weight for _, weight in accounts], meta)
    return [
        PairedTarget(service, amount)
        for (service, _), amount in zip(accounts, amounts)
        if amount > 0
    ]


async def _track(targets: list[PairedTarget], symbol: str) -> list[_TrackedTarget]:
    starts = await asyncio.gather(*(target.service.signed_position(symbol) for target in targets))
    return [
        _TrackedTarget(target.service, target.target_base, start)
        for target, start in zip(targets, starts)
    ]


async def _read_progress(
    targets: list[_TrackedTarget],
    symbol: str,
    side: PositionSide,
) -> list[Decimal]:
    current = await asyncio.gather(*(target.service.signed_position(symbol) for target in targets))
    direction = _direction(side)
    return [
        min(target.target_base, max(Decimal(0), (position - target.start_position) * direction))
        for target, position in zip(targets, current)
    ]


async def _books(targets: list[_TrackedTarget], meta: MarketMeta) -> list[BookSnapshot]:
    return await asyncio.gather(
        *(asyncio.to_thread(target.service.public.book, meta.market_id) for target in targets)
    )


def _maker_min_base(meta: MarketMeta, book: BookSnapshot) -> Decimal:
    return max(meta.min_base_amount, meta.min_quote_amount / book.mid)


async def execute_paired(
    meta: MarketMeta,
    maker_side: PositionSide,
    follower_side: PositionSide,
    maker_targets: list[PairedTarget],
    follower_targets: list[PairedTarget],
    reduce_only: bool = False,
    halt_check=None,
) -> None:
    if not maker_targets or not follower_targets:
        raise RuntimeError("Leader-follower execution requires both sides")

    maker_total = sum((target.target_base for target in maker_targets), Decimal(0))
    follower_total = sum((target.target_base for target in follower_targets), Decimal(0))
    one_unit = _from_units(1, meta)
    if maker_total <= 0 or abs(maker_total - follower_total) >= one_unit:
        raise RuntimeError(
            f"Paired base targets differ: leader={maker_total}, follower={follower_total}"
        )

    all_targets = maker_targets + follower_targets
    dry_run = all(bool(getattr(target.service, "dry_run", False)) for target in all_targets)
    if dry_run:
        await asyncio.gather(
            *(target.service.cancel_all_orders(meta) for target in maker_targets)
        )
        maker_books = await _books(
            [_TrackedTarget(target.service, target.target_base, Decimal(0)) for target in maker_targets],
            meta,
        )
        await asyncio.gather(
            *(
                target.service.place_post_only(
                    meta, book, maker_side, target.target_base, reduce_only=reduce_only
                )
                for target, book in zip(maker_targets, maker_books)
            )
        )
        follower_books = await _books(
            [_TrackedTarget(target.service, target.target_base, Decimal(0)) for target in follower_targets],
            meta,
        )
        await asyncio.gather(
            *(
                target.service.place_market(
                    meta,
                    book,
                    follower_side,
                    target.target_base * book.mid,
                    reduce_only=reduce_only,
                    base_amount_override=target.target_base,
                )
                for target, book in zip(follower_targets, follower_books)
            )
        )
        ok("DRY leader-follower", f"balanced base={maker_total} {meta.symbol}")
        return

    makers = await _track(maker_targets, meta.symbol)
    followers = await _track(follower_targets, meta.symbol)
    maker_high_water = [Decimal(0) for _ in makers]
    follower_high_water = [Decimal(0) for _ in followers]
    follower_desired = [Decimal(0) for _ in followers]
    allocated_maker_progress = Decimal(0)
    started_at = time.monotonic()
    maker_orders: dict[int, int] = {}
    quoted_reference = Decimal(0)
    quote_started = 0.0

    async def read_maker_progress() -> list[Decimal]:
        observed = await _read_progress(makers, meta.symbol, maker_side)
        for index, value in enumerate(observed):
            maker_high_water[index] = max(maker_high_water[index], value)
        return list(maker_high_water)

    async def read_follower_progress() -> list[Decimal]:
        observed = await _read_progress(followers, meta.symbol, follower_side)
        for index, value in enumerate(observed):
            follower_high_water[index] = max(follower_high_water[index], value)
        return list(follower_high_water)

    async def cancel_maker_orders() -> None:
        await asyncio.gather(*(target.service.cancel_all_orders(meta) for target in makers))
        maker_orders.clear()

    async def hedge_to(maker_progress: Decimal) -> None:
        nonlocal allocated_maker_progress
        delta = maker_progress - allocated_maker_progress
        if delta > 0:
            remaining_caps = [
                target.target_base - desired
                for target, desired in zip(followers, follower_desired)
            ]
            additions = _allocate_units(delta, remaining_caps, meta, caps=remaining_caps)
            for index, addition in enumerate(additions):
                follower_desired[index] += addition
            allocated_maker_progress += sum(additions, Decimal(0))

        for attempt in range(1, MAX_HEDGE_RETRIES + 1):
            progress = await read_follower_progress()
            deficits = [
                target.service.quantize_base_amount(meta, max(Decimal(0), desired - actual))
                for target, desired, actual in zip(followers, follower_desired, progress)
            ]
            pending = [
                (target, deficit)
                for target, deficit in zip(followers, deficits)
                if deficit > 0
            ]
            if not pending:
                return

            info(
                "Follower hedge",
                f"attempt={attempt}/{MAX_HEDGE_RETRIES} | orders={len(pending)} | "
                f"base={sum((item[1] for item in pending), Decimal(0))} {meta.symbol}",
            )
            books = await _books([item[0] for item in pending], meta)
            results = await asyncio.gather(
                *(
                    target.service.place_market(
                        meta,
                        book,
                        follower_side,
                        deficit * book.mid,
                        reduce_only=reduce_only,
                        base_amount_override=deficit,
                    )
                    for (target, deficit), book in zip(pending, books)
                ),
                return_exceptions=True,
            )
            for (target, _), result in zip(pending, results):
                if isinstance(result, BaseException):
                    warn(target.service.label(), f"follower IOC rejected: {result}")

            deadline = time.monotonic() + HEDGE_SETTLE_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(min(HEDGE_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))
                progress = await read_follower_progress()
                if all(actual >= desired for actual, desired in zip(progress, follower_desired)):
                    return

        progress = await read_follower_progress()
        missing = sum(
            (max(Decimal(0), desired - actual) for desired, actual in zip(follower_desired, progress)),
            Decimal(0),
        )
        if missing > 0:
            raise RuntimeError(f"Unable to hedge leader fill; missing {missing} {meta.symbol}")

    async def place_maker_remaining(progress: list[Decimal]) -> None:
        nonlocal quoted_reference, quote_started
        remaining = [
            target.service.quantize_base_amount(meta, target.target_base - filled)
            for target, filled in zip(makers, progress)
        ]
        pending = [(target, amount) for target, amount in zip(makers, remaining) if amount > 0]
        books = await _books([item[0] for item in pending], meta)
        too_small = [
            (target, amount, book)
            for (target, amount), book in zip(pending, books)
            if amount < _maker_min_base(meta, book)
        ]
        if too_small:
            warn(
                "Leader residual",
                "below maker minimum; completing only the residual via MARKET",
            )
            await asyncio.gather(
                *(
                    target.service.place_market(
                        meta,
                        book,
                        maker_side,
                        amount * book.mid,
                        reduce_only=reduce_only,
                        base_amount_override=amount,
                    )
                    for target, amount, book in too_small
                )
            )
            await asyncio.sleep(HEDGE_SETTLE_SECONDS)
            return

        results = await asyncio.gather(
            *(
                target.service.place_post_only(
                    meta, book, maker_side, amount, reduce_only=reduce_only
                )
                for (target, amount), book in zip(pending, books)
            )
        )
        maker_orders.clear()
        for (target, _), (client_order_index, _) in zip(pending, results):
            maker_orders[id(target.service)] = client_order_index
        prices = [price for _, price in results]
        quoted_reference = sum(prices, Decimal(0)) / Decimal(len(prices))
        quote_started = time.monotonic()

    info(
        "Leader-follower",
        f"leader={maker_side.upper()} {maker_total} | follower={follower_side.upper()} "
        f"{follower_total} {meta.symbol}",
    )
    try:
        await cancel_maker_orders()
        maker_progress = Decimal(0)
        while maker_progress < maker_total:
            if time.monotonic() - started_at > MAX_MAKER_WAIT_SECONDS:
                raise RuntimeError(f"Leader phase timed out after {MAX_MAKER_WAIT_SECONDS}s")

            progress = await read_maker_progress()
            maker_progress = sum(progress, Decimal(0))
            await hedge_to(maker_progress)
            if maker_progress >= maker_total:
                break
            if halt_check is not None and halt_check():
                raise RuntimeError("Trading halted by Force Close")

            if not maker_orders:
                await place_maker_remaining(progress)
                await asyncio.sleep(HEDGE_POLL_INTERVAL_SECONDS)
                continue

            await asyncio.sleep(HEDGE_POLL_INTERVAL_SECONDS)
            progress = await read_maker_progress()
            next_progress = sum(progress, Decimal(0))
            if next_progress > maker_progress:
                maker_progress = next_progress
                step(
                    "Leader fill",
                    f"{maker_progress}/{maker_total} {meta.symbol}; hedging delta",
                )
                await hedge_to(maker_progress)
            if maker_progress >= maker_total:
                break

            if time.monotonic() - quote_started < MAKER_REQUOTE_INTERVAL_SECONDS:
                continue

            active = await asyncio.gather(
                *(
                    target.service.has_active_order(meta, maker_orders.get(id(target.service)))
                    for index, target in enumerate(makers)
                    if target.target_base > progress[index]
                )
            )
            snapshot = await asyncio.to_thread(makers[0].service.public.book, meta.market_id)
            current_reference = snapshot.best_ask if maker_side == "long" else snapshot.best_bid
            drift = (
                abs(current_reference - quoted_reference) / quoted_reference * Decimal(100)
                if quoted_reference > 0
                else Decimal("Infinity")
            )
            if not all(active) or drift >= Decimal(str(MAKER_REQUOTE_THRESHOLD_PERCENT)):
                reason = "inactive order" if not all(active) else f"quote drift {drift:.4f}%"
                step("Leader re-quote", reason)
                await cancel_maker_orders()
                progress = await read_maker_progress()
                await hedge_to(sum(progress, Decimal(0)))
            else:
                quote_started = time.monotonic()

        final_maker = sum(await read_maker_progress(), Decimal(0))
        await hedge_to(final_maker)
        final_follower = sum(await read_follower_progress(), Decimal(0))
        if final_maker < maker_total or final_follower < follower_total:
            raise RuntimeError(
                f"Incomplete pair: leader={final_maker}/{maker_total}, "
                f"follower={final_follower}/{follower_total}"
            )
        maker_positions, follower_positions = await asyncio.gather(
            asyncio.gather(*(target.service.signed_position(meta.symbol) for target in makers)),
            asyncio.gather(*(target.service.signed_position(meta.symbol) for target in followers)),
        )
        exact_maker = [
            (position - target.start_position) * _direction(maker_side)
            for target, position in zip(makers, maker_positions)
        ]
        exact_follower = [
            (position - target.start_position) * _direction(follower_side)
            for target, position in zip(followers, follower_positions)
        ]
        mismatches = [
            f"{target.service.label()}={actual}/{target.target_base}"
            for target, actual in zip(makers + followers, exact_maker + exact_follower)
            if abs(actual - target.target_base) >= one_unit
        ]
        if mismatches:
            raise RuntimeError(f"Per-wallet fill mismatch: {', '.join(mismatches)}")
        ok("Pair filled", f"base={maker_total} {meta.symbol} on both sides")
    finally:
        await asyncio.gather(
            *(target.service.cancel_all_orders(meta) for target in makers),
            return_exceptions=True,
        )


async def flatten_positions(services: list[Any], meta: MarketMeta) -> None:
    if not services:
        return
    await asyncio.gather(
        *(service.cancel_all_orders(meta) for service in services),
        return_exceptions=True,
    )
    one_unit = _from_units(1, meta)
    for attempt in range(1, RESIDUAL_CLOSE_RETRIES + 1):
        positions = await asyncio.gather(
            *(service.signed_position(meta.symbol) for service in services),
            return_exceptions=True,
        )
        residuals = [
            service
            for service, position in zip(services, positions)
            if isinstance(position, BaseException) or abs(position) >= one_unit
        ]
        if not residuals:
            ok("Residual check", f"all {len(services)} wallet(s) flat")
            return
        step(
            "Residual close",
            f"attempt={attempt}/{RESIDUAL_CLOSE_RETRIES} | wallets={len(residuals)}",
        )
        results = await asyncio.gather(
            *(service.close_position(meta) for service in residuals),
            return_exceptions=True,
        )
        for service, result in zip(residuals, results):
            if isinstance(result, BaseException):
                error(service.label(), f"residual close failed: {result}")
        await asyncio.sleep(1)

    positions = await asyncio.gather(
        *(service.signed_position(meta.symbol) for service in services),
        return_exceptions=True,
    )
    remaining = [
        f"{service.label()}={position}"
        for service, position in zip(services, positions)
        if isinstance(position, BaseException) or abs(position) >= one_unit
    ]
    if remaining:
        raise RuntimeError(f"Residual {meta.symbol} positions remain: {', '.join(remaining)}")


async def close_leader_follower(
    maker_services: list[Any],
    follower_services: list[Any],
    meta: MarketMeta,
) -> None:
    all_services = list(dict.fromkeys(maker_services + follower_services))
    if not all_services:
        return
    await asyncio.gather(
        *(service.cancel_all_orders(meta) for service in all_services),
        return_exceptions=True,
    )
    try:
        maker_positions = await asyncio.gather(
            *(service.signed_position(meta.symbol) for service in maker_services),
            return_exceptions=True,
        )
        follower_positions = await asyncio.gather(
            *(service.signed_position(meta.symbol) for service in follower_services),
            return_exceptions=True,
        )
        read_errors = [
            (service, position)
            for service, position in zip(
                maker_services + follower_services,
                maker_positions + follower_positions,
            )
            if isinstance(position, BaseException)
        ]
        if read_errors:
            for service, position in read_errors:
                warn(service.label(), f"paired close position read failed: {position}")
            return

        maker_active = [
            (service, position)
            for service, position in zip(maker_services, maker_positions)
            if position != 0
        ]
        follower_active = [
            (service, position)
            for service, position in zip(follower_services, follower_positions)
            if position != 0
        ]
        if EXECUTION_MODE == "all-market" or not maker_active or not follower_active:
            return
        maker_signs = {position > 0 for _, position in maker_active}
        follower_signs = {position > 0 for _, position in follower_active}
        if len(maker_signs) != 1 or len(follower_signs) != 1 or maker_signs == follower_signs:
            warn("Paired close", "positions are not opposite; using MARKET fallback")
            return

        maker_side: PositionSide = "short" if next(iter(maker_signs)) else "long"
        follower_side: PositionSide = "short" if next(iter(follower_signs)) else "long"
        paired_total = min(
            sum((abs(position) for _, position in maker_active), Decimal(0)),
            sum((abs(position) for _, position in follower_active), Decimal(0)),
        )
        maker_targets = allocate_targets(
            paired_total,
            [(service, abs(position)) for service, position in maker_active],
            meta,
        )
        follower_targets = allocate_targets(
            paired_total,
            [(service, abs(position)) for service, position in follower_active],
            meta,
        )
        await execute_paired(
            meta,
            maker_side,
            follower_side,
            maker_targets,
            follower_targets,
            reduce_only=True,
            halt_check=lambda: False,
        )
    except Exception as exc:
        warn("Paired close", f"degraded to MARKET: {exc}")
    finally:
        await flatten_positions(all_services, meta)
