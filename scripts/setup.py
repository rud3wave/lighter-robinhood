from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"

GLOBAL_JS = """// ============================================================
//  GLOBAL CONFIG - Robinhood Chain Lighter ETH/USDG Bot
// ============================================================

// Telegram alerts. Leave empty/0 to disable.
export const TELEGRAM = {
  token: '',
  chatId: 0,
};

// Used for local AES-256-GCM encryption. Set once before the first run.
// Do not change this password after your files have been encrypted.
export const ENCRYPTION_PASSWORD = 'change-me-before-first-run';
"""

PRIVATE_KEYS = """# Приватные ключи (один на строку)
"""

PROXIES = """# Прокси (один на строку). Если не нужны — оставь пустым.
# Формат: http://user:pass@ip:port или ip:port
"""


def ensure_local_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"keep   {path.relative_to(ROOT)}")
        return
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"create {path.relative_to(ROOT)}")


def protect_local_inputs_from_git() -> None:
    try:
        subprocess.run(
            [
                "git",
                "update-index",
                "--skip-worktree",
                "input_data/privatekeys.txt",
                "input_data/proxies.txt",
            ],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def main() -> None:
    ensure_local_file(ROOT / "global.js", GLOBAL_JS)
    ensure_local_file(ROOT / "input_data" / "privatekeys.txt", PRIVATE_KEYS)
    ensure_local_file(ROOT / "input_data" / "proxies.txt", PROXIES)
    protect_local_inputs_from_git()

    if not VENV_PYTHON.exists():
        print("create .venv")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])

    print("install Python dependencies")
    subprocess.check_call(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(ROOT / "requirements.txt"),
        ]
    )
    print("setup complete: configure settings.py, global.js and input_data, then run npm start")


if __name__ == "__main__":
    main()
