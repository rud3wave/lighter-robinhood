import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import lighter
from web3 import Web3

from settings import (
    API_BASE_URL,
    DEPOSIT_ALL,
    DEPOSIT_TOKEN_SYMBOL,
    DEPOSIT_WAIT_FOR_RECEIPT,
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_RPC_URL,
)

from .api import LighterPublicApi
from .pretty import info, ok, skip, wallet_prefix
from .utils import pick_range
from .wallets import WalletAccount, mask_secret


ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


@dataclass
class DepositPlan:
    w3: Web3
    contract: Any
    amount: Decimal
    amount_i: int
    balance: Decimal
    balance_i: int
    min_transfer: Decimal


def _web3(wallet: WalletAccount) -> Web3:
    kwargs = {}
    if wallet.proxy_url:
        kwargs["proxies"] = {"http": wallet.proxy_url, "https": wallet.proxy_url}
    return Web3(Web3.HTTPProvider(ROBINHOOD_RPC_URL, request_kwargs=kwargs))


def _deposit_asset(wallet: WalletAccount) -> dict:
    payload = LighterPublicApi(API_BASE_URL, wallet.proxy_url)._get("/api/v1/assetDetails")
    for item in payload.get("asset_details", []):
        if str(item.get("symbol", "")).upper() == DEPOSIT_TOKEN_SYMBOL.upper():
            return item
    raise RuntimeError(f"{DEPOSIT_TOKEN_SYMBOL} was not found in Lighter assetDetails")


async def _intent_address(wallet: WalletAccount) -> str:
    config = lighter.Configuration(host=API_BASE_URL)
    config.proxy = wallet.proxy_url or None
    client = lighter.ApiClient(config)
    try:
        # External EVM deposits use an amount-independent intent address. The
        # exact USDG amount is carried by the ERC-20 transfer below.
        resp = await lighter.BridgeApi(client).create_intent_address(
            chain_id=str(ROBINHOOD_CHAIN_ID),
            from_addr=wallet.address,
            amount="0",
            is_external_deposit=True,
        )
        if getattr(resp, "code", None) != 200:
            raise RuntimeError(f"createIntentAddress failed: {resp}")
        return resp.intent_address
    finally:
        await client.close()


def _prepare_deposit(wallet: WalletAccount, requested_amount: Decimal | None) -> DepositPlan:
    w3 = _web3(wallet)
    if not w3.is_connected():
        raise RuntimeError(f"Robinhood RPC is not available: {ROBINHOOD_RPC_URL}")

    chain_id = w3.eth.chain_id
    if chain_id != ROBINHOOD_CHAIN_ID:
        raise RuntimeError(f"RPC chain mismatch: expected {ROBINHOOD_CHAIN_ID}, got {chain_id}")

    asset = _deposit_asset(wallet)
    min_transfer = Decimal(str(asset.get("min_transfer_amount", "0")))
    token_address = Web3.to_checksum_address(asset["l1_address"])
    contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    decimals = int(contract.functions.decimals().call())
    scale = Decimal(10) ** decimals
    balance_i = int(contract.functions.balanceOf(wallet.address).call())
    balance = Decimal(balance_i) / scale

    if requested_amount is None:
        amount_i = balance_i
        amount = balance
    else:
        amount = requested_amount
        amount_i = int(amount * scale)

    return DepositPlan(
        w3=w3,
        contract=contract,
        amount=amount,
        amount_i=amount_i,
        balance=balance,
        balance_i=balance_i,
        min_transfer=min_transfer,
    )


def _send_deposit(
    wallet: WalletAccount,
    plan: DepositPlan,
    intent_address: str,
) -> tuple[str, int | None]:
    tx = plan.contract.functions.transfer(intent_address, plan.amount_i).build_transaction(
        {
            "from": wallet.address,
            "nonce": plan.w3.eth.get_transaction_count(wallet.address),
            "chainId": ROBINHOOD_CHAIN_ID,
        }
    )
    tx.setdefault("gas", int(plan.w3.eth.estimate_gas(tx) * 1.2))
    if "maxFeePerGas" not in tx and "gasPrice" not in tx:
        tx["gasPrice"] = plan.w3.eth.gas_price

    signed = plan.w3.eth.account.sign_transaction(tx, wallet.private_key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = plan.w3.eth.send_raw_transaction(raw).hex()
    if not DEPOSIT_WAIT_FOR_RECEIPT:
        return tx_hash, None

    receipt = plan.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"deposit transfer failed: {tx_hash}")
    return tx_hash, int(receipt.blockNumber)


async def deposit_token(wallet: WalletAccount, amount: Decimal | None = None) -> str | None:
    plan = await asyncio.to_thread(_prepare_deposit, wallet, amount)
    label = wallet_prefix(wallet.index, mask_secret(wallet.address))

    info(
        label,
        f"{DEPOSIT_TOKEN_SYMBOL} balance={plan.balance:.6f} | deposit={plan.amount:.6f}"
    )
    if plan.amount_i <= 0:
        skip(label, f"deposit skipped: zero {DEPOSIT_TOKEN_SYMBOL} balance")
        return None
    if plan.amount < plan.min_transfer:
        skip(label, f"deposit skipped: minimum is {plan.min_transfer} {DEPOSIT_TOKEN_SYMBOL}")
        return None
    if plan.balance_i < plan.amount_i:
        skip(label, f"deposit skipped: not enough {DEPOSIT_TOKEN_SYMBOL}")
        return None
    intent_address = Web3.to_checksum_address(await _intent_address(wallet))
    tx_hash, block_number = await asyncio.to_thread(
        _send_deposit,
        wallet,
        plan,
        intent_address,
    )
    ok(label, f"deposit tx sent: {mask_secret(tx_hash)}")
    if block_number is not None:
        ok(label, f"deposit confirmed: block {block_number}")
    return tx_hash


def pick_deposit_amount(bounds: list[int | float]) -> Decimal:
    if DEPOSIT_ALL:
        raise RuntimeError("DEPOSIT_AMOUNT is disabled while DEPOSIT_ALL=True")
    amount = int(pick_range(bounds))
    if amount <= 0:
        raise RuntimeError("DEPOSIT_AMOUNT must be a positive whole token amount")
    return Decimal(amount)
