from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import settings

from .id_filter import parse_id_filter


def _number(name: str, value: Any) -> Decimal:
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be a number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not number.is_finite():
        raise RuntimeError(f"{name} must be finite")
    return number


def _range(
    name: str,
    value: Any,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RuntimeError(f"{name} must be [min, max]")
    low = _number(f"{name}[0]", value[0])
    high = _number(f"{name}[1]", value[1])
    if low > high:
        raise RuntimeError(f"{name}: min cannot be greater than max")
    if minimum is not None and low < minimum:
        raise RuntimeError(f"{name}: min cannot be below {minimum}")
    if maximum is not None and high > maximum:
        raise RuntimeError(f"{name}: max cannot exceed {maximum}")
    return low, high


def validate_settings() -> None:
    if not isinstance(settings.SHUFFLE_WALLETS, bool):
        raise RuntimeError("SHUFFLE_WALLETS must be True or False")
    parse_id_filter(settings.ID_FILTER)
    if type(settings.RETRY) is not int or settings.RETRY < 1:
        raise RuntimeError("RETRY must be a positive integer")
    if settings.EXECUTION_MODE not in {"leader-follower", "all-market"}:
        raise RuntimeError(
            "EXECUTION_MODE must be 'leader-follower' or 'all-market'"
        )

    tokens = settings.TOKENS_TO_TRADE
    if not isinstance(tokens, list) or not tokens:
        raise RuntimeError("TOKENS_TO_TRADE must contain at least one token")
    if any(not isinstance(token, str) or not token.strip() for token in tokens):
        raise RuntimeError("TOKENS_TO_TRADE can contain only token symbols")
    if any(token != token.strip().upper() for token in tokens):
        raise RuntimeError("TOKENS_TO_TRADE symbols must be uppercase")
    if len(tokens) != len(set(tokens)):
        raise RuntimeError("TOKENS_TO_TRADE cannot contain duplicates")

    _range(
        "POSITION_PERCENT",
        settings.POSITION_PERCENT,
        minimum=Decimal("0.01"),
        maximum=Decimal(100),
    )
    if not isinstance(settings.TOKEN_LEVERAGE, dict):
        raise RuntimeError("TOKEN_LEVERAGE must be a dictionary")
    for token in tokens:
        if token not in settings.TOKEN_LEVERAGE:
            raise RuntimeError(f"TOKEN_LEVERAGE must define {token}")
        _range(
            f'TOKEN_LEVERAGE["{token}"]',
            settings.TOKEN_LEVERAGE[token],
            minimum=Decimal(1),
        )

    groups = settings.GROUP_CONFIGS
    if not isinstance(groups, list) or not groups:
        raise RuntimeError("GROUP_CONFIGS must contain at least one [LONG, SHORT] group")
    for group in groups:
        if (
            not isinstance(group, (list, tuple))
            or len(group) != 2
            or any(type(count) is not int or count < 1 for count in group)
        ):
            raise RuntimeError(
                "Each GROUP_CONFIGS item must be [positive LONG, positive SHORT]"
            )

    max_spread = _number("MAX_SPREAD", settings.MAX_SPREAD)
    if max_spread <= 0 or max_spread > 100:
        raise RuntimeError("MAX_SPREAD must be greater than 0 and no more than 100")
    slippage = _number("SLIPPAGE", settings.SLIPPAGE)
    if slippage < 0 or slippage > 100:
        raise RuntimeError("SLIPPAGE must be between 0 and 100")

    _range("HOLD_MINUTES", settings.HOLD_MINUTES, minimum=Decimal(0))
    if type(settings.TRADES_COUNT) is not int or settings.TRADES_COUNT < 1:
        raise RuntimeError("TRADES_COUNT must be a positive integer")
    _range(
        "DELAY_BETWEEN_TRADES",
        settings.DELAY_BETWEEN_TRADES,
        minimum=Decimal(0),
    )
    _range(
        "DELAY_BETWEEN_WALLETS",
        settings.DELAY_BETWEEN_WALLETS,
        minimum=Decimal(0),
    )
    if _number("POLL_INTERVAL_SEC", settings.POLL_INTERVAL_SEC) <= 0:
        raise RuntimeError("POLL_INTERVAL_SEC must be greater than 0")

    if settings.EXECUTION_MODE == "leader-follower":
        if _number("HEDGE_POLL_INTERVAL_MS", settings.HEDGE_POLL_INTERVAL_MS) <= 0:
            raise RuntimeError("HEDGE_POLL_INTERVAL_MS must be greater than 0")
        if _number("MAKER_REQUOTE_INTERVAL_SEC", settings.MAKER_REQUOTE_INTERVAL_SEC) <= 0:
            raise RuntimeError("MAKER_REQUOTE_INTERVAL_SEC must be greater than 0")
        requote_threshold = _number(
            "MAKER_REQUOTE_THRESHOLD_PERCENT",
            settings.MAKER_REQUOTE_THRESHOLD_PERCENT,
        )
        if requote_threshold < 0 or requote_threshold > 100:
            raise RuntimeError(
                "MAKER_REQUOTE_THRESHOLD_PERCENT must be between 0 and 100"
            )
        if _number("MAX_MAKER_WAIT_SEC", settings.MAX_MAKER_WAIT_SEC) <= 0:
            raise RuntimeError("MAX_MAKER_WAIT_SEC must be greater than 0")
        if (
            type(settings.MAX_HEDGE_RETRIES) is not int
            or settings.MAX_HEDGE_RETRIES < 1
        ):
            raise RuntimeError("MAX_HEDGE_RETRIES must be a positive integer")

    if not isinstance(settings.DEPOSIT_ALL, bool):
        raise RuntimeError("DEPOSIT_ALL must be True or False")
    if not settings.DEPOSIT_ALL:
        _range("DEPOSIT_AMOUNT", settings.DEPOSIT_AMOUNT, minimum=Decimal("0.01"))

    if not isinstance(settings.WITHDRAW_ALL, bool):
        raise RuntimeError("WITHDRAW_ALL must be True or False")
    if not settings.WITHDRAW_ALL:
        _range(
            "WITHDRAW_AMOUNT",
            settings.WITHDRAW_AMOUNT,
            minimum=Decimal("0.01"),
        )


def validate_market_leverage(max_leverage: dict[str, Decimal]) -> None:
    for token in settings.TOKENS_TO_TRADE:
        configured_max = _range(
            f'TOKEN_LEVERAGE["{token}"]',
            settings.TOKEN_LEVERAGE[token],
            minimum=Decimal(1),
        )[1]
        exchange_max = max_leverage[token]
        if configured_max > exchange_max:
            raise RuntimeError(
                f'TOKEN_LEVERAGE["{token}"] max {configured_max} exceeds '
                f"Lighter maximum {exchange_max}x"
            )


def validate_group_wallet_count(wallet_count: int) -> None:
    invalid = [group for group in settings.GROUP_CONFIGS if sum(group) != wallet_count]
    if invalid:
        raise RuntimeError(
            "Количество кошельков после ID_FILTER не совпадает с GROUP_CONFIGS: "
            f"выбрано={wallet_count}, группы={invalid}"
        )
