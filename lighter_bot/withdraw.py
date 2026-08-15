import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import lighter

from .constants import (
    API_BASE_URL,
    DEPOSIT_TOKEN_SYMBOL,
)
from .pretty import fmt_number, ok, plain, skip
from .service import LighterService
from .utils import pick_range
from .wallets import mask_secret


@dataclass(frozen=True)
class WithdrawalAsset:
    asset_id: int
    decimals: int
    minimum: Decimal


@dataclass(frozen=True)
class WithdrawalResult:
    amount: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    tx_hash: str | None = None
    detail: str = ""

    @property
    def sent(self) -> bool:
        return self.tx_hash is not None


def _asset_from_payload(payload: dict) -> WithdrawalAsset:
    for item in payload.get("asset_details", []):
        if str(item.get("symbol", "")).upper() == DEPOSIT_TOKEN_SYMBOL.upper():
            return WithdrawalAsset(
                asset_id=int(item["asset_id"]),
                decimals=int(item["decimals"]),
                minimum=Decimal(str(item["min_withdrawal_amount"])),
            )
    raise RuntimeError(f"{DEPOSIT_TOKEN_SYMBOL} не найден в Lighter")


def _withdrawal_amount(
    available: Decimal,
    requested: Decimal | None,
    decimals: int,
) -> Decimal:
    unit = Decimal(1).scaleb(-decimals)
    amount = available if requested is None else requested
    return amount.quantize(unit, rounding=ROUND_DOWN)


def _response_value(response: Any, name: str, default: Any = None) -> Any:
    if isinstance(response, dict):
        return response.get(name, default)
    value = getattr(response, name, None)
    if value is not None:
        return value
    additional = getattr(response, "additional_properties", None) or {}
    return additional.get(name, default)


def _fast_withdrawal_limit(response: Any) -> Decimal:
    limits = []
    for name in ("withdraw_limit", "max_withdrawal_amount"):
        value = Decimal(str(_response_value(response, name, "0")))
        if value.is_finite() and value > 0:
            limits.append(value)
    if not limits:
        raise RuntimeError("быстрый вывод сейчас недоступен")
    return min(limits)


def _fast_withdrawal_memo(address: str) -> str:
    raw = bytes.fromhex(address.lower().removeprefix("0x"))
    if len(raw) != 20:
        raise RuntimeError("некорректный адрес кошелька для быстрого вывода")
    return (raw + b"\x00" * 12).hex()


async def _withdrawal_context(
    service: LighterService,
) -> tuple[WithdrawalAsset, Decimal]:
    state, asset_payload = await asyncio.gather(
        asyncio.to_thread(service.account_state),
        asyncio.to_thread(service.public._get, "/api/v1/assetDetails"),
    )
    open_positions = [
        item
        for item in state.get("positions", [])
        if Decimal(str(item.get("position", "0"))) != 0
    ]
    if open_positions:
        raise RuntimeError("сначала закрой все позиции через режим 2")
    return (
        _asset_from_payload(asset_payload),
        Decimal(str(state.get("available_balance", "0"))),
    )


async def withdrawal_delay_seconds(proxy_url: str = "") -> int:
    config = lighter.Configuration(host=API_BASE_URL)
    config.proxy = proxy_url or None
    client = lighter.ApiClient(config)
    try:
        response = await lighter.InfoApi(client).withdrawal_delay()
        return max(0, int(response.seconds))
    finally:
        await client.close()


async def secure_withdraw_usdg(
    service: LighterService,
    requested_amount: Decimal | None,
) -> WithdrawalResult:
    if service.client is None:
        raise RuntimeError("торговый доступ не настроен")

    asset, available = await _withdrawal_context(service)
    amount = _withdrawal_amount(available, requested_amount, asset.decimals)
    label = service.label()

    if amount <= 0:
        skip(label, f"баланс {DEPOSIT_TOKEN_SYMBOL}=0")
        return WithdrawalResult(detail="баланс 0")
    if amount < asset.minimum:
        detail = (
            f"меньше минимума {fmt_number(asset.minimum)} "
            f"{DEPOSIT_TOKEN_SYMBOL}"
        )
        skip(label, detail)
        return WithdrawalResult(detail=detail)
    if amount > available:
        detail = f"недостаточно {DEPOSIT_TOKEN_SYMBOL}"
        skip(label, detail)
        return WithdrawalResult(detail=detail)

    plain(
        label,
        f"доступно={fmt_number(available)} {DEPOSIT_TOKEN_SYMBOL} | "
        f"вывод={fmt_number(amount)} {DEPOSIT_TOKEN_SYMBOL}",
    )
    _, response, err = await service.client.withdraw(
        asset_id=asset.asset_id,
        route_type=lighter.SignerClient.ROUTE_PERP,
        amount=float(amount),
        api_key_index=service.wallet.api_key_index,
    )
    if err:
        raise RuntimeError(err)
    if response is None or response.code != 200:
        message = getattr(response, "message", None) if response else None
        raise RuntimeError(message or "биржа отклонила вывод")

    tx_hash = response.tx_hash
    ok(
        label,
        f"вывод запрошен {fmt_number(amount)} {DEPOSIT_TOKEN_SYMBOL} | "
        f"tx {mask_secret(tx_hash)}",
    )
    return WithdrawalResult(amount=amount, tx_hash=tx_hash)


async def fast_withdraw_usdg(
    service: LighterService,
    requested_amount: Decimal | None,
) -> WithdrawalResult:
    if service.client is None or service.wallet.account_index is None:
        raise RuntimeError("торговый доступ не настроен")

    asset, available = await _withdrawal_context(service)
    label = service.label()
    auth, auth_error = service.client.create_auth_token_with_expiry(
        api_key_index=service.wallet.api_key_index
    )
    if auth_error:
        raise RuntimeError(f"не удалось авторизовать быстрый вывод: {auth_error}")

    bridge_api = lighter.BridgeApi(service.client.api_client)
    pool = await bridge_api.fastwithdraw_info(
        authorization=auth,
        account_index=service.wallet.account_index,
    )
    if int(_response_value(pool, "code", 0)) != 200:
        raise RuntimeError(
            _response_value(pool, "message") or "быстрый вывод сейчас недоступен"
        )
    pool_account = int(_response_value(pool, "to_account_index"))
    limit = _fast_withdrawal_limit(pool)

    fee_response = await lighter.InfoApi(
        service.client.api_client
    ).transfer_fee_info(
        authorization=auth,
        account_index=service.wallet.account_index,
        to_account_index=pool_account,
    )
    if int(_response_value(fee_response, "code", 0)) != 200:
        raise RuntimeError(
            _response_value(fee_response, "message")
            or "не удалось получить комиссию быстрого вывода"
        )
    scale = Decimal(10) ** asset.decimals
    fee_i = int(_response_value(fee_response, "transfer_fee_usdc", 0))
    if fee_i < 0:
        raise RuntimeError("биржа вернула некорректную комиссию быстрого вывода")
    fee = Decimal(fee_i) / scale
    if fee > 0 and available <= fee:
        detail = (
            f"баланса недостаточно для комиссии {fmt_number(fee)} "
            f"{DEPOSIT_TOKEN_SYMBOL}"
        )
        skip(label, detail)
        return WithdrawalResult(detail=detail)
    spendable = max(Decimal(0), available - fee)
    amount = _withdrawal_amount(spendable, requested_amount, asset.decimals)

    if amount <= 0:
        skip(label, f"баланс {DEPOSIT_TOKEN_SYMBOL}=0")
        return WithdrawalResult(detail="баланс 0")
    if amount < asset.minimum:
        detail = (
            f"меньше минимума {fmt_number(asset.minimum)} "
            f"{DEPOSIT_TOKEN_SYMBOL}"
        )
        skip(label, detail)
        return WithdrawalResult(detail=detail)
    if amount + fee > available:
        detail = f"недостаточно {DEPOSIT_TOKEN_SYMBOL} с учётом комиссии"
        skip(label, detail)
        return WithdrawalResult(detail=detail)
    if amount > limit:
        detail = f"выше лимита быстрого вывода {fmt_number(limit)} {DEPOSIT_TOKEN_SYMBOL}"
        skip(label, detail)
        return WithdrawalResult(detail=detail)

    amount_i = int(amount * scale)
    plain(
        label,
        f"доступно={fmt_number(available)} {DEPOSIT_TOKEN_SYMBOL} | "
        f"вывод={fmt_number(amount)} {DEPOSIT_TOKEN_SYMBOL} | "
        f"комиссия={fmt_number(fee)}",
    )
    api_key_index, nonce = await service.client.nonce_manager.async_next_nonce(
        service.wallet.api_key_index
    )
    _, tx_info, signed_hash, sign_error = service.client.sign_transfer(
        eth_private_key=service.wallet.private_key,
        to_account_index=pool_account,
        asset_id=asset.asset_id,
        route_from=lighter.SignerClient.ROUTE_PERP,
        route_to=lighter.SignerClient.ROUTE_PERP,
        usdc_amount=amount_i,
        fee=fee_i,
        memo=_fast_withdrawal_memo(service.wallet.address),
        api_key_index=api_key_index,
        nonce=nonce,
    )
    if sign_error:
        service.client.nonce_manager.acknowledge_failure(api_key_index)
        raise RuntimeError(sign_error)
    if not tx_info:
        service.client.nonce_manager.acknowledge_failure(api_key_index)
        raise RuntimeError("не удалось подписать быстрый вывод")

    response = await bridge_api.fastwithdraw(
        tx_info=tx_info,
        to_address=service.wallet.address,
        authorization=auth,
    )
    if int(_response_value(response, "code", 0)) != 200:
        service.client.nonce_manager.acknowledge_failure(api_key_index)
        raise RuntimeError(
            _response_value(response, "message") or "биржа отклонила быстрый вывод"
        )
    tx_hash = _response_value(response, "tx_hash") or signed_hash
    if not tx_hash:
        raise RuntimeError("биржа не вернула хеш быстрого вывода")
    ok(
        label,
        f"выведено {fmt_number(amount)} {DEPOSIT_TOKEN_SYMBOL} | "
        f"комиссия {fmt_number(fee)} | tx {mask_secret(tx_hash)}",
    )
    return WithdrawalResult(amount=amount, fee=fee, tx_hash=tx_hash)


async def withdraw_usdg(
    service: LighterService,
    requested_amount: Decimal | None,
    method: str = "secure",
) -> WithdrawalResult:
    if method == "fast":
        return await fast_withdraw_usdg(service, requested_amount)
    if method == "secure":
        return await secure_withdraw_usdg(service, requested_amount)
    raise RuntimeError("неизвестный метод вывода")


def pick_withdrawal_amount(bounds: list[int | float]) -> Decimal:
    amount = Decimal(str(pick_range(bounds)))
    if amount <= 0:
        raise RuntimeError("WITHDRAW_AMOUNT должен быть больше 0")
    return amount
