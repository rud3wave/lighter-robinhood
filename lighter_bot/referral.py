import lighter

from settings import API_BASE_URL, CHAIN_ID, REFERRAL_CODE, REFERRAL_X

from .pretty import ok, skip, warn, wallet_prefix
from .service import _apply_proxy
from .wallets import WalletAccount, mask_secret


async def use_referral(wallet: WalletAccount) -> bool:
    label = wallet_prefix(wallet.index, mask_secret(wallet.address))
    if not REFERRAL_CODE:
        return False
    if wallet.account_index is None:
        skip(label, "referral skipped: no Lighter account for wallet")
        return False
    if not wallet.api_private_key:
        skip(label, "referral skipped: no API key")
        return False

    ok(label, f"referral submit: {REFERRAL_CODE}")

    signer = lighter.SignerClient(
        url=API_BASE_URL,
        account_index=wallet.account_index,
        api_private_keys={wallet.api_key_index: wallet.api_private_key},
        chain_id=CHAIN_ID,
    )
    _apply_proxy(signer, wallet.proxy_url)
    client = None
    try:
        auth, err = signer.create_auth_token_with_expiry(api_key_index=wallet.api_key_index)
        if err:
            raise RuntimeError(f"create auth token failed: {err}")

        config = lighter.Configuration(host=API_BASE_URL)
        config.proxy = wallet.proxy_url or None
        client = lighter.ApiClient(config)
        resp = await lighter.ReferralApi(client).referral_use(
            l1_address=wallet.address,
            referral_code=REFERRAL_CODE,
            x=REFERRAL_X,
            auth=auth,
        )
        if resp.code != 200:
            warn(label, f"referral response: code={resp.code} message={resp.message}")
            return False
        ok(label, f"referral applied: {REFERRAL_CODE}")
        return True
    finally:
        await signer.close()
        if client:
            await client.close()
