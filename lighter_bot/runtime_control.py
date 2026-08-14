from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / ".lighter-trading.lock"
HALT_PATH = ROOT / ".lighter-halt"


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_trading_lock() -> None:
    for _ in range(2):
        try:
            descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid()}, handle)
            return
        except FileExistsError:
            try:
                pid = int(json.loads(LOCK_PATH.read_text(encoding="utf-8"))["pid"])
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pid = 0
            if _process_is_alive(pid):
                raise RuntimeError(f"Trading process {pid} is already active")
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
    raise RuntimeError("Could not acquire the trading process lock")


def release_trading_lock() -> None:
    try:
        payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if int(payload.get("pid", 0)) == os.getpid():
            LOCK_PATH.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def request_trading_halt() -> None:
    HALT_PATH.write_text("halt\n", encoding="utf-8")


def clear_trading_halt() -> None:
    try:
        HALT_PATH.unlink()
    except FileNotFoundError:
        pass


def is_trading_halted() -> bool:
    return HALT_PATH.exists()
