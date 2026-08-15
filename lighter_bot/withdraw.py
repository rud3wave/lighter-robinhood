import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import lighter
from web3 import Web3

from .constants import (
    API_BASE_URL,
    DEPOSIT_TOKEN_SYMBOL,
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_RPC_URL,
)
from .deposit import wallet_web3
from .pretty import fmt_number, ok, plain, skip
from .service import LighterService
from .utils import pick_range
from .wallets import mask_secret


LIGHTER_BRIDGE_ABI = [
    {
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_assetIndex", "type": "uint16"},
        ],
        "name": "getPendingBalance",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_assetIndex", "type": "uint16"},
            {"name": "_baseAmount", "type": "uint128"},
        ],
        "name": "withdrawPendingBalance",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass(frozen=True)
class WithdrawalAsset:
    asset_id: int
    decimals: int
    minimum: Decimal


@dataclass(frozen=True)
class WithdrawalResult:
    amount: Decimal = Decimal(0)
    tx_hash: str | None = None
    detail: str = ""

    @property
    def sent(self) -> bool:
        return self.tx_hash is not None


@dataclass(frozen=True)
class ClaimResult:
    amount: Decimal = Decimal(0)
    tx_hash: str | None = None
    detail: str = ""

    @property
    def claimed(self) -> bool:
        return self.tx_hash is not None


@dataclass(frozen=True)
class ClaimPlan:
    w3: Web3
    contract: Any
    amount: Decimal
    amount_i: int


def _asset_from_payload(payload: dict) -> WithdrawalAsset:
    for item in payload.get("asset_details", []):
        if str(item.get("symbol", "")).upper() == DEPOSIT_TOKEN_SYMBOL.upper():
            return WithdrawalAsset(
                asset_id=int(item["asset_id"]),
                decimals=int(item["decimals"]),
                minimum=Decimal(str(item["min_withdrawal_amount"])),
            )
    raise RuntimeError(f"{DEPOSIT_TOKEN_SYMBOL} не найден в Lighter")


def _bridge_address_from_payload(payload: dict) -> str:
    for item in payload.get("contract_addresses", []):
        if item.get("name") == "ZkLighterContract":
            return Web3.to_checksum_address(item["address"])
    raise RuntimeError("L1-контракт Lighter не найден")


def _withdrawal_amount(
    available: Decimal,
    requested: Decimal | None,
    decimals: int,
) -> Decimal:
    unit = Decimal(1).scaleb(-decimals)
    amount = available if requested is None else requested
    return amount.quantize(unit, rounding=ROUND_DOWN)


async def withdrawal_delay_seconds(proxy_url: str = "") -> int:
    config = lighter.Configuration(host=API_BASE_URL)
    config.proxy = proxy_url or None
    client = lighter.ApiClient(config)
    try:
        response = await lighter.InfoApi(client).withdrawal_delay()
        return max(0, int(response.seconds))
    finally:
        await client.close()


def _prepare_claim(
    service: LighterService,
    asset: WithdrawalAsset,
    bridge_address: str,
) -> ClaimPlan:
    wallet = service.wallet
    w3 = wallet_web3(wallet)
    if not w3.is_connected():
        raise RuntimeError(f"Robinhood RPC недоступен: {ROBINHOOD_RPC_URL}")
    if w3.eth.chain_id != ROBINHOOD_CHAIN_ID:
        raise RuntimeError("Robinhood RPC подключён к неверной сети")

    contract = w3.eth.contract(address=bridge_address, abi=LIGHTER_BRIDGE_ABI)
    amount_i = int(
        contract.functions.getPendingBalance(
            Web3.to_checksum_address(wallet.address),
            asset.asset_id,
        ).call()
    )
    amount = Decimal(amount_i) / (Decimal(10) ** asset.decimals)
    return ClaimPlan(w3=w3, contract=contract, amount=amount, amount_i=amount_i)


def _send_claim(
    service: LighterService,
    asset: WithdrawalAsset,
    plan: ClaimPlan,
) -> str:
    wallet = service.wallet
    owner = Web3.to_checksum_address(wallet.address)
    tx = plan.contract.functions.withdrawPendingBalance(
        owner,
        asset.asset_id,
        plan.amount_i,
    ).build_transaction(
        {
            "from": owner,
            "nonce": plan.w3.eth.get_transaction_count(owner),
            "chainId": ROBINHOOD_CHAIN_ID,
        }
    )
    tx.setdefault("gas", int(plan.w3.eth.estimate_gas(tx) * 1.2))
    if "maxFeePerGas" not in tx and "gasPrice" not in tx:
        tx["gasPrice"] = plan.w3.eth.gas_price

    signed = plan.w3.eth.account.sign_transaction(tx, wallet.private_key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = plan.w3.eth.send_raw_transaction(raw).hex()
    receipt = plan.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError("получение USDG отклонено сетью")
    return tx_hash


async def claim_pending_usdg(service: LighterService) -> ClaimResult:
    asset_payload, layer1_payload = await asyncio.gather(
        asyncio.to_thread(service.public._get, "/api/v1/assetDetails"),
        asyncio.to_thread(service.public._get, "/api/v1/layer1BasicInfo"),
    )
    asset = _asset_from_payload(asset_payload)
    bridge_address = _bridge_address_from_payload(layer1_payload)
    plan = await asyncio.to_thread(_prepare_claim, service, asset, bridge_address)
    if plan.amount_i <= 0:
        return ClaimResult(detail="нет готовых средств")

    tx_hash = await asyncio.to_thread(_send_claim, service, asset, plan)
    ok(
        service.label(),
        f"получено {fmt_number(plan.amount)} {DEPOSIT_TOKEN_SYMBOL} | "
        f"tx {mask_secret(tx_hash)}",
    )
    return ClaimResult(amount=plan.amount, tx_hash=tx_hash)


async def withdraw_usdg(
    service: LighterService,
    requested_amount: Decimal | None,
) -> WithdrawalResult:
    if service.client is None:
        raise RuntimeError("торговый доступ не настроен")

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

    asset = _asset_from_payload(asset_payload)
    available = Decimal(str(state.get("available_balance", "0")))
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


def pick_withdrawal_amount(bounds: list[int | float]) -> Decimal:
    amount = Decimal(str(pick_range(bounds)))
    if amount <= 0:
        raise RuntimeError("WITHDRAW_AMOUNT должен быть больше 0")
    return amount
