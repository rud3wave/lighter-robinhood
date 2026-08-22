from __future__ import annotations

import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

USE_COLOR = os.environ.get("NO_COLOR", "").lower() not in ("1", "true", "yes")
PRIVATE_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
EVM_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
PROXY_AUTH_RE = re.compile(r"(https?://)[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)
DECIMAL_RE = re.compile(r"(?<![\w.])([+-]?\d+)([.,])(\d+)(?![\w.])")


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


def fmt_number(
    value: Any,
    *,
    signed: bool = False,
    decimal_separator: str = ".",
) -> str:
    try:
        number = Decimal(str(value).replace(",", "."))
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return str(value)

    if rounded == 0:
        return "0"
    result = f"{rounded:.2f}".rstrip("0").rstrip(".")
    if signed and rounded > 0:
        result = f"+{result}"
    if decimal_separator != ".":
        result = result.replace(".", decimal_separator)
    return result


def fmt_points(value: Any) -> str:
    try:
        number = Decimal(str(value).replace(",", "."))
        rounded = number.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return str(value)

    if rounded == 0:
        return "0"
    return f"{rounded:.6f}".rstrip("0").rstrip(".")


def format_user_text(value: Any) -> str:
    text = str(value)

    def replace_decimal(match: re.Match[str]) -> str:
        prefix = text[max(0, match.start() - 24) : match.start()]
        if re.search(r"Points:\s*$", prefix, re.IGNORECASE):
            return match.group(0)
        raw = "".join(match.groups())
        return fmt_number(
            raw,
            signed=raw.startswith("+"),
            decimal_separator=match.group(2),
        )

    return DECIMAL_RE.sub(replace_decimal, text)


def redact(value: Any) -> str:
    text = str(value)
    text = PROXY_AUTH_RE.sub(r"\1***@", text)
    text = PRIVATE_HEX_RE.sub("<private-hex>", text)
    text = EVM_ADDRESS_RE.sub("<address>", text)
    return format_user_text(text)


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
    inner_width = max(len(title), len(subtitle)) + 4
    border = "+" + "=" * inner_width + "+"
    print()
    print(paint(border, C.CYAN))
    print(
        paint("|", C.CYAN)
        + paint(f"{title:^{inner_width}}", C.BOLD + C.WHITE)
        + paint("|", C.CYAN)
    )
    print(
        paint("|", C.CYAN)
        + paint(f"{subtitle:^{inner_width}}", C.GRAY)
        + paint("|", C.CYAN)
    )
    print(paint(border, C.CYAN))


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
