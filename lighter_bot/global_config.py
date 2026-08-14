import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLOBAL_JS = ROOT / "global.js"


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: int

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.chat_id != 0


@dataclass(frozen=True)
class GlobalConfig:
    telegram: TelegramConfig
    encryption_password: str


def _match_string(source: str, name: str, default: str = "") -> str:
    match = re.search(rf"{re.escape(name)}\s*[:=]\s*['\"]([^'\"]*)['\"]", source)
    return match.group(1) if match else default


def _match_int(source: str, name: str, default: int = 0) -> int:
    match = re.search(rf"{re.escape(name)}\s*[:=]\s*(\d+)", source)
    return int(match.group(1)) if match else default


def load_global_config(path: Path = GLOBAL_JS) -> GlobalConfig:
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    return GlobalConfig(
        telegram=TelegramConfig(
            token=_match_string(source, "token"),
            chat_id=_match_int(source, "chatId"),
        ),
        encryption_password=_match_string(source, "ENCRYPTION_PASSWORD", "change-me-before-first-run"),
    )
