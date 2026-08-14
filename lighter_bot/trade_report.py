from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TradeMetrics:
    realized_pnl: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    volume: Decimal = Decimal(0)
    fills: int = 0
    latest_timestamp: int = 0

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl + self.funding

    def __add__(self, other: "TradeMetrics") -> "TradeMetrics":
        return TradeMetrics(
            realized_pnl=self.realized_pnl + other.realized_pnl,
            funding=self.funding + other.funding,
            volume=self.volume + other.volume,
            fills=self.fills + other.fills,
            latest_timestamp=max(self.latest_timestamp, other.latest_timestamp),
        )


def _value(item: Any, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _decimal(value: Any) -> Decimal:
    return Decimal(0) if value in (None, "") else Decimal(str(value))


def account_is_ask(trade: Any, account_index: int) -> bool:
    ask_index = int(_value(trade, "ask_account_id", -1))
    bid_index = int(_value(trade, "bid_account_id", -1))
    if ask_index == account_index:
        return True
    if bid_index == account_index:
        return False
    raise RuntimeError(f"Trade does not belong to account {account_index}")


def account_position_before(trade: Any, account_index: int) -> Decimal:
    is_ask = account_is_ask(trade, account_index)
    ask_is_maker = bool(_value(trade, "is_maker_ask", False))
    account_is_maker = ask_is_maker if is_ask else not ask_is_maker
    field = "maker_position_size_before" if account_is_maker else "taker_position_size_before"
    return _decimal(_value(trade, field))


def discover_open_timestamp(trades: list[Any], account_index: int) -> int:
    opening_timestamps = [
        int(_value(trade, "timestamp", 0))
        for trade in trades
        if account_position_before(trade, account_index) == 0
    ]
    if not opening_timestamps:
        raise RuntimeError("opening fill was not found in trade history")
    return max(opening_timestamps)


def summarize_trades(
    trades: list[Any],
    account_index: int,
    funding_rows: list[Any] | None = None,
) -> TradeMetrics:
    realized_pnl = Decimal(0)
    volume = Decimal(0)
    latest_timestamp = 0
    for trade in trades:
        is_ask = account_is_ask(trade, account_index)
        pnl_field = "ask_account_pnl" if is_ask else "bid_account_pnl"
        realized_pnl += _decimal(_value(trade, pnl_field))
        volume += _decimal(_value(trade, "usd_amount"))
        latest_timestamp = max(latest_timestamp, int(_value(trade, "timestamp", 0)))

    funding = sum(
        (_decimal(_value(row, "change")) for row in funding_rows or []),
        Decimal(0),
    )
    return TradeMetrics(
        realized_pnl=realized_pnl,
        funding=funding,
        volume=volume,
        fills=len(trades),
        latest_timestamp=latest_timestamp,
    )
