from __future__ import annotations

import base64
import getpass
import json
import os
import secrets
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

from .global_config import load_global_config


VAULT_VERSION = 2
LINE_PREFIX = "enc:v2:"
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
DEFAULT_PASSWORD = "change-me-before-first-run"


@lru_cache(maxsize=1)
def vault_password() -> str:
    configured = os.environ.get("LIGHTER_ENCRYPTION_PASSWORD", "").strip()
    if not configured:
        configured = load_global_config().encryption_password.strip()

    if not configured or configured == DEFAULT_PASSWORD:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Set LIGHTER_ENCRYPTION_PASSWORD or ENCRYPTION_PASSWORD in global.js "
                "before encrypted wallet storage can be used"
            )
        configured = getpass.getpass("Vault password: ")
        confirmation = getpass.getpass("Repeat vault password: ")
        if configured != confirmation:
            raise RuntimeError("Vault passwords do not match")

    if not configured:
        raise RuntimeError("Vault password cannot be empty")
    return configured


def _aad(context: str) -> bytes:
    return f"lighter-robinhood-lit:v{VAULT_VERSION}:{context}".encode("utf-8")


def _derive_key(password: str, salt: bytes) -> bytes:
    return scrypt(
        password.encode("utf-8"),
        salt,
        key_len=32,
        N=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )


def seal_text(plain: str, context: str) -> dict[str, Any]:
    salt = get_random_bytes(16)
    nonce = get_random_bytes(12)
    cipher = AES.new(_derive_key(vault_password(), salt), AES.MODE_GCM, nonce=nonce)
    cipher.update(_aad(context))
    ciphertext, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
    return {
        "v": VAULT_VERSION,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def open_text(payload: dict[str, Any], context: str) -> str:
    if payload.get("v") != VAULT_VERSION or payload.get("kdf") != "scrypt":
        raise RuntimeError("Unsupported encrypted payload version")
    if (payload.get("n"), payload.get("r"), payload.get("p")) != (
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
    ):
        raise RuntimeError("Unsupported encrypted payload KDF parameters")

    salt = base64.b64decode(payload["salt"], validate=True)
    nonce = base64.b64decode(payload["nonce"], validate=True)
    tag = base64.b64decode(payload["tag"], validate=True)
    ciphertext = base64.b64decode(payload["ciphertext"], validate=True)
    cipher = AES.new(_derive_key(vault_password(), salt), AES.MODE_GCM, nonce=nonce)
    cipher.update(_aad(context))
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


def seal_line(plain: str, context: str) -> str:
    packed = json.dumps(seal_text(plain, context), separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return LINE_PREFIX + token


def open_line(line: str, context: str) -> str:
    if not line.startswith(LINE_PREFIX):
        raise RuntimeError("Encrypted input line has an invalid prefix")
    token = line[len(LINE_PREFIX):]
    token += "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
    return open_text(payload, context)


def protect_lines_atomic(path: Path, values: list[str], context: str) -> None:
    encrypted = [seal_line(value, context) for value in values]
    if [open_line(value, context) for value in encrypted] != values:
        raise RuntimeError(f"Encrypted input verification failed for {path.name}")

    header = "# Encrypted locally by lighter-robinhood-lit vault v2."
    content = header + "\n" + "\n".join(encrypted) + "\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
