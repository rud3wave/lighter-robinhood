from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IdRule:
    minimum: int
    maximum: int | None

    def matches(self, wallet_id: int) -> bool:
        return wallet_id >= self.minimum and (
            self.maximum is None or wallet_id <= self.maximum
        )


def _positive_id(value: Any, location: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value.strip()):
        raise RuntimeError(f"{location} должен содержать положительный ID")
    wallet_id = int(value)
    if wallet_id < 1:
        raise RuntimeError(f"{location} должен быть больше 0")
    return wallet_id


def parse_id_filter(filters: Any) -> list[IdRule]:
    if not isinstance(filters, list):
        raise RuntimeError("ID_FILTER должен быть списком")

    rules: list[IdRule] = []
    for index, item in enumerate(filters):
        location = f"ID_FILTER[{index}]"
        if isinstance(item, str):
            value = item.strip()
            if value.startswith((">", "<")):
                wallet_id = _positive_id(value[1:], location)
                rules.append(
                    IdRule(wallet_id, None)
                    if value[0] == ">"
                    else IdRule(1, wallet_id)
                )
            else:
                wallet_id = _positive_id(value, location)
                rules.append(IdRule(wallet_id, wallet_id))
            continue

        if isinstance(item, (list, tuple)) and len(item) == 2:
            start = _positive_id(item[0], f"{location}[0]")
            end = _positive_id(item[1], f"{location}[1]")
            if start > end:
                raise RuntimeError(f"{location}: начало диапазона больше конца")
            rules.append(IdRule(start, end))
            continue

        raise RuntimeError(
            f"{location} должен быть строкой ID или диапазоном из двух ID"
        )
    return rules


def filter_wallets_by_id(wallets: list[Any], filters: Any) -> list[Any]:
    rules = parse_id_filter(filters)
    if not rules:
        return list(wallets)
    return [
        wallet
        for wallet in wallets
        if any(rule.matches(wallet.wallet_id) for rule in rules)
    ]
