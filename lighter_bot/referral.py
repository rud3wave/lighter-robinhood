import asyncio
import base64
import json
from dataclasses import dataclass

import lighter

from settings import RETRY

from .constants import API_BASE_URL, CHAIN_ID, REFERRAL_CODE, REFERRAL_X
from .pretty import exception_summary
from .service import _apply_proxy
from .wallets import WalletAccount


_REFERRAL_SIGNATURE_SUFFIX = "wP81zDNpES"


@dataclass(frozen=True)
class ReferralResult:
    accepted: bool
    confirmed: bool = False
    error: str = ""
    code_owner: bool = False

    def __bool__(self) -> bool:
        return self.confirmed or self.code_owner


def referral_signature(address: str, referral_code: str) -> str:
    payload = f"{address}{referral_code}{_REFERRAL_SIGNATURE_SUFFIX}"
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _error_code(exc: Exception) -> int | None:
    body = getattr(exc, "body", None)
    if not isinstance(body, str):
        return None
    try:
        return int(json.loads(body).get("code"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def use_referral(wallet: WalletAccount) -> ReferralResult:
    if not REFERRAL_CODE:
        return ReferralResult(False, error="реферальный код не настроен")
    if wallet.account_index is None:
        return ReferralResult(False, error="аккаунт Lighter не найден")
    if not wallet.api_private_key:
        return ReferralResult(False, error="торговый доступ не настроен")

    signer = lighter.SignerClient(
        url=API_BASE_URL,
        account_index=wallet.account_index,
        api_private_keys={wallet.api_key_index: wallet.api_private_key},
        chain_id=CHAIN_ID,
    )
    _apply_proxy(signer, wallet.proxy_url)
    client = None
    try:
        config = lighter.Configuration(host=API_BASE_URL)
        config.proxy = wallet.proxy_url or None
        client = lighter.ApiClient(config)
        account_api = lighter.AccountApi(client)
        referral_api = lighter.ReferralApi(client)
        last_error = "регистрация не подтверждена"

        for attempt in range(1, max(1, RETRY) + 1):
            try:
                auth, err = signer.create_auth_token_with_expiry(
                    api_key_index=wallet.api_key_index
                )
                if err:
                    raise RuntimeError(f"create auth token failed: {err}")

                try:
                    status = await account_api.referral_user_referrals(
                        l1_address=wallet.address,
                        auth=auth,
                        limit=1,
                    )
                    if (
                        status.code == 200
                        and str(status.used_code or "").upper()
                        == REFERRAL_CODE.upper()
                    ):
                        return ReferralResult(True, True)
                except Exception:
                    # Status is advisory. A temporary read failure must not
                    # prevent the referral request from being submitted.
                    pass

                response = await referral_api.referral_use(
                    l1_address=wallet.address,
                    referral_code=REFERRAL_CODE,
                    x=REFERRAL_X,
                    auth=auth,
                    discord="",
                    telegram="",
                    signature=referral_signature(wallet.address, REFERRAL_CODE),
                )
                if response.code == 41012:
                    return ReferralResult(True, code_owner=True)
                if response.code != 200:
                    raise RuntimeError(response.message or "referral request rejected")

                for verification_attempt in range(10):
                    if verification_attempt:
                        await asyncio.sleep(1)
                    try:
                        status = await account_api.referral_user_referrals(
                            l1_address=wallet.address,
                            auth=auth,
                            limit=1,
                        )
                        if (
                            status.code == 200
                            and str(status.used_code or "").upper()
                            == REFERRAL_CODE.upper()
                        ):
                            return ReferralResult(True, True)
                    except Exception:
                        continue

                last_error = "биржа приняла запрос, но код не появился в аккаунте"
                return ReferralResult(True, False, last_error)
            except Exception as exc:
                if _error_code(exc) == 41012:
                    return ReferralResult(True, code_owner=True)
                last_error = exception_summary(exc)

            if attempt < max(1, RETRY):
                await asyncio.sleep(attempt)

        return ReferralResult(False, error=last_error)
    finally:
        await signer.close()
        if client:
            await client.close()
