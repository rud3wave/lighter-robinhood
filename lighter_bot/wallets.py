import base64
import json
import os
import random
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
from eth_account import Account

from .constants import (
    API_BASE_URL,
    DEFAULT_API_KEY_INDEX,
)
from settings import (
    SHUFFLE_WALLETS,
    STRICT_PROXY_ISOLATION,
)

from .pretty import error, exception_summary, plain, section, warn, wallet_prefix
from .vault import DEFAULT_PASSWORD, LINE_PREFIX, open_line, open_text, protect_lines_atomic, seal_text, vault_password


ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "input_data"
DB_DIR = ROOT / "databases"
WALLETS_DB = DB_DIR / "wallets.json"
PRIVATE_KEYS_PATH = INPUT_DIR / "privatekeys.txt"
PROXIES_PATH = INPUT_DIR / "proxies.txt"


@dataclass
class WalletAccount:
    index: int
    private_key: str
    address: str
    proxy_url: str = ""
    account_index: int | None = None
    api_key_index: int = DEFAULT_API_KEY_INDEX
    api_private_key: str = ""

    @property
    def can_trade(self) -> bool:
        return self.account_index is not None and bool(self.api_private_key)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return f"{value[:6]}...{value[-4:]}" if len(value) > 12 else "***"


def normalize_proxy(proxy: str) -> str:
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        return f"http://{proxy}"
    return proxy


def mask_proxy(proxy: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", proxy)


def _read_secret_lines(path: Path, context: str) -> tuple[list[str], bool]:
    if not path.exists():
        return [], False
    values: list[str] = []
    has_plaintext = False
    for raw_line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(LINE_PREFIX):
            values.append(open_line(line, context))
        else:
            values.append(line)
            has_plaintext = True
    return values, has_plaintext


def read_private_keys() -> list[str]:
    raw_keys, has_plaintext = _read_secret_lines(PRIVATE_KEYS_PATH, "input:privatekeys")
    keys = [validate_evm_private_key(key) for key in raw_keys]
    if not keys:
        raise RuntimeError(f"No private keys found in {PRIVATE_KEYS_PATH}")
    if has_plaintext:
        protect_lines_atomic(PRIVATE_KEYS_PATH, keys, "input:privatekeys")
    return keys


def read_proxies() -> list[str]:
    raw_proxies, has_plaintext = _read_secret_lines(PROXIES_PATH, "input:proxies")
    proxies = [normalize_proxy(proxy) for proxy in raw_proxies]
    if has_plaintext and proxies:
        protect_lines_atomic(PROXIES_PATH, proxies, "input:proxies")
    return proxies


def validate_evm_private_key(private_key: str) -> str:
    key = private_key.strip()
    if key.startswith("0x"):
        key = key[2:]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise RuntimeError(
            "privatekeys.txt must contain EVM private keys for Lighter "
            "(64 hex chars, optional 0x)."
        )
    return "0x" + key


def address_from_private_key(private_key: str) -> str:
    return Account.from_key(validate_evm_private_key(private_key)).address


def check_proxy(proxy_url: str, timeout: int = 12) -> bool:
    try:
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        req = Request(
            API_BASE_URL + "/api/v1/orderBookDetails?filter=perp",
            headers={"accept": "application/json"},
        )
        opener.open(req, timeout=timeout).read(128)
        return True
    except Exception:
        return False


def check_proxies_health(proxies: list[str]) -> list[str]:
    unique = list(dict.fromkeys([proxy for proxy in proxies if proxy]))
    if not unique:
        if STRICT_PROXY_ISOLATION:
            raise RuntimeError(
                "STRICT_PROXY_ISOLATION requires one proxy for every wallet"
            )
        warn("Прокси не указаны", "кошельки используют текущий IP")
        return []
    section("Прокси", str(len(unique)))
    with ThreadPoolExecutor(max_workers=len(unique)) as executor:
        statuses = list(executor.map(check_proxy, unique))

    alive: list[str] = []
    for proxy, is_alive in zip(unique, statuses):
        label = mask_proxy(proxy)
        if is_alive:
            alive.append(proxy)
            plain(label, "подключен")
        else:
            error(label, "не отвечает")
    if STRICT_PROXY_ISOLATION and len(alive) != len(unique):
        raise RuntimeError(
            "Strict proxy isolation stopped the run because one or more proxies failed"
        )
    if not alive:
        warn("Прокси недоступны", "кошельки используют текущий IP")
    return alive


def _derive_legacy_key(password: str, salt: bytes) -> bytes:
    return PBKDF2(password.encode("utf-8"), salt, dkLen=32, count=200_000, hmac_hash_module=SHA256)


def _decrypt_legacy_text(payload: dict[str, str]) -> str:
    salt = base64.b64decode(payload["salt"])
    nonce = base64.b64decode(payload["nonce"])
    tag = base64.b64decode(payload["tag"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    passwords = [vault_password()]
    if DEFAULT_PASSWORD not in passwords:
        passwords.append(DEFAULT_PASSWORD)
    for password in passwords:
        try:
            cipher = AES.new(_derive_legacy_key(password, salt), AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
    raise RuntimeError("Unable to decrypt legacy wallet record")


def _wallet_from_plain(data: dict[str, Any]) -> WalletAccount:
    return WalletAccount(
        index=int(data["index"]),
        private_key=str(data["private_key"]),
        address=str(data["address"]),
        proxy_url=str(data.get("proxy_url") or ""),
        account_index=data.get("account_index"),
        api_key_index=int(data.get("api_key_index") or DEFAULT_API_KEY_INDEX),
        api_private_key=str(data.get("api_private_key") or ""),
    )


def _legacy_wallet_from_db(data: dict[str, Any]) -> WalletAccount:
    plain = dict(data)
    plain["private_key"] = _decrypt_legacy_text(data["private_key"])
    plain["api_private_key"] = (
        _decrypt_legacy_text(data["api_private_key"])
        if data.get("api_private_key")
        else ""
    )
    return _wallet_from_plain(plain)


def save_wallets(wallets: list[WalletAccount]) -> None:
    vault_password()
    DB_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for wallet in wallets:
        record_id = secrets.token_hex(16)
        plain = json.dumps(asdict(wallet), separators=(",", ":"), sort_keys=True)
        records.append(
            {
                "id": record_id,
                "payload": seal_text(plain, f"wallet-record:{record_id}"),
            }
        )
    document = {"version": 2, "wallets": records}
    temporary = WALLETS_DB.with_name(f".{WALLETS_DB.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        written = json.loads(temporary.read_text(encoding="utf-8"))
        if written.get("version") != 2 or len(written.get("wallets", [])) != len(records):
            raise RuntimeError("Encrypted wallet DB verification failed")
        os.replace(temporary, WALLETS_DB)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_wallets_db() -> list[WalletAccount] | None:
    if not WALLETS_DB.exists():
        return None
    try:
        document = json.loads(WALLETS_DB.read_text(encoding="utf-8"))
        if isinstance(document, list):
            return [_legacy_wallet_from_db(item) for item in document]
        if document.get("version") != 2:
            raise RuntimeError("Unsupported wallet database version")
        wallets = []
        for item in document.get("wallets", []):
            record_id = str(item["id"])
            plain = open_text(item["payload"], f"wallet-record:{record_id}")
            wallets.append(_wallet_from_plain(json.loads(plain)))
        return wallets
    except Exception as exc:
        error("Failed to read encrypted wallet DB", exception_summary(exc))
        raise RuntimeError(
            "Encrypted wallet DB could not be opened; existing data was left untouched"
        ) from exc


def load_wallets_from_privatekeys() -> list[WalletAccount]:
    raw_keys = read_private_keys()
    configured_proxies = read_proxies()
    if STRICT_PROXY_ISOLATION:
        if len(configured_proxies) != len(raw_keys):
            raise RuntimeError(
                "STRICT_PROXY_ISOLATION requires exactly one proxy per private key"
            )
        if len(set(configured_proxies)) != len(configured_proxies):
            raise RuntimeError(
                "STRICT_PROXY_ISOLATION requires a unique proxy for every private key"
            )
    proxies = check_proxies_health(configured_proxies)
    wallets: list[WalletAccount] = []
    for index, raw_key in enumerate(raw_keys):
        private_key = raw_key
        proxy_url = proxies[index % len(proxies)] if proxies else ""
        wallets.append(
            WalletAccount(
                index=index,
                private_key=private_key,
                address=address_from_private_key(private_key),
                proxy_url=proxy_url,
            )
        )
    return wallets


def init_wallets() -> list[WalletAccount]:
    current = load_wallets_from_privatekeys()
    existing = load_wallets_db()
    if existing:
        by_address = {wallet.address.lower(): wallet for wallet in existing}
        for wallet in current:
            old = by_address.get(wallet.address.lower())
            if old:
                wallet.account_index = old.account_index
                wallet.api_key_index = old.api_key_index
                wallet.api_private_key = old.api_private_key
                wallet.proxy_url = old.proxy_url or wallet.proxy_url
    save_wallets(current)
    section("Кошельки", str(len(current)))
    for wallet in current:
        label = wallet_prefix(wallet.index, mask_secret(wallet.address))
        proxy = f"proxy: {mask_proxy(wallet.proxy_url)}" if wallet.proxy_url else "proxy: none"
        plain(label, proxy)

    execution_order = list(current)
    if SHUFFLE_WALLETS:
        random.shuffle(execution_order)
    return execution_order
