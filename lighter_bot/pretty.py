from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

USE_COLOR = os.environ.get("NO_COLOR", "").lower() not in ("1", "true", "yes")
PRIVATE_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
EVM_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
PROXY_AUTH_RE = re.compile(r"(https?://)[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)


class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"


def paint(text: Any, color: str = "") -> str:
    text = str(text)
    if not USE_COLOR or not color:
        return text
    return f"{color}{text}{C.RESET}"


def redact(value: Any) -> str:
    text = str(value)
    text = PROXY_AUTH_RE.sub(r"\1***@", text)
    text = PRIVATE_HEX_RE.sub("<private-hex>", text)
    return EVM_ADDRESS_RE.sub("<address>", text)


def exception_summary(exc: BaseException, limit: int = 240) -> str:
    status_code = getattr(exc, "status", None)
    body = str(getattr(exc, "body", "") or "")
    source = body or str(exc)
    api_error = re.search(r"code=(\d+)\s+message=['\"]([^'\"]+)", source)
    if api_error:
        prefix = f"HTTP {status_code} | " if status_code else ""
        return f"{prefix}code={api_error.group(1)} | {api_error.group(2)}"

    compact = " ".join(str(exc).split()) or exc.__class__.__name__
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def ts() -> str:
    return paint(datetime.now().strftime("%H:%M:%S"), C.GRAY)


def line(char: str = "=", width: int = 68) -> str:
    return paint(char * width, C.GRAY)


def banner() -> None:
    title = "LIGHTER ROBINHOOD ETH"
    subtitle = "Delta-neutral trading"
    print()
    print(paint("+" + "=" * 66 + "+", C.CYAN))
    print(paint("|", C.CYAN) + paint(f"{title:^66}", C.BOLD + C.WHITE) + paint("|", C.CYAN))
    print(paint("|", C.CYAN) + paint(f"{subtitle:^66}", C.GRAY) + paint("|", C.CYAN))
    print(paint("+" + "=" * 66 + "+", C.CYAN))


def section(title: str, hint: str = "") -> None:
    title = redact(title)
    hint = redact(hint)
    print()
    print(line())
    print(paint(f" {title} ", C.BOLD + C.CYAN) + (paint(f" {hint}", C.GRAY) if hint else ""))
    print(line("-"))


def status(kind: str, message: str, detail: str = "") -> None:
    styles = {
        "OK": C.GREEN,
        "INFO": C.CYAN,
        "STEP": C.BLUE,
        "WARN": C.YELLOW,
        "ERR": C.RED,
        "SKIP": C.YELLOW,
        "DRY": C.MAGENTA,
    }
    message = redact(message)
    detail = redact(detail)
    color = styles.get(kind, C.WHITE)
    suffix = paint(f" | {detail}", C.GRAY) if detail else ""
    print(f"{ts()} {paint(kind.rjust(4), color + C.BOLD)} {message}{suffix}")


def plain(message: str, detail: str = "") -> None:
    message = redact(message)
    detail = redact(detail)
    suffix = paint(f" | {detail}", C.GRAY) if detail else ""
    print(f"{ts()} {message}{suffix}")


def ok(message: str, detail: str = "") -> None:
    status("OK", message, detail)


def info(message: str, detail: str = "") -> None:
    status("INFO", message, detail)


def step(message: str, detail: str = "") -> None:
    status("STEP", message, detail)


def warn(message: str, detail: str = "") -> None:
    status("WARN", message, detail)


def error(message: str, detail: str = "") -> None:
    status("ERR", message, detail)


def skip(message: str, detail: str = "") -> None:
    status("SKIP", message, detail)


def dry(message: str, detail: str = "") -> None:
    status("DRY", message, detail)


def wallet_prefix(index: int, address: str = "") -> str:
    del index
    return paint(address or "wallet", C.BOLD + C.WHITE)
