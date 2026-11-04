"""Windows-only primitives: paths, process liveness/info, file locking and console VT mode.

Nothing here touches ``ctypes.windll`` or ``msvcrt`` at import time -- only inside the functions that
need them, at call time -- so importing this module on macOS/Linux is always safe. Every OS-specific
probe takes an injectable parameter so its surrounding logic can be exercised on any platform in tests;
only the small ``_default_*`` probes themselves require a real Windows console/kernel to run.
"""
from __future__ import annotations

import ctypes
import os
import sys
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

try:
    import msvcrt
except ImportError:  # not on Windows
    msvcrt = None  # type: ignore[assignment]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
# ENABLE_VIRTUAL_TERMINAL_PROCESSING: lets the console interpret ANSI/VT escape sequences (cursor,
# alt screen, colors) the way every POSIX terminal already does.
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# --- paths ---------------------------------------------------------------------------------------------
# Orca source (read-only reference, see task notes): src/cli/runtime/metadata.ts getPlatformUserDataPath
# (%APPDATA%/orca, ORCA_USER_DATA_PATH override) and src/main/startup/windows-app-data-path.ts.


def orca_user_data_root(*, environ: Mapping[str, str] = os.environ) -> str:
    """Orca's per-user data root: ``%ORCA_USER_DATA_PATH%`` overrides, else ``%APPDATA%\\orca``."""
    override = environ.get("ORCA_USER_DATA_PATH")
    if override:
        return override
    return os.path.join(environ.get("APPDATA", ""), "orca")


def rotator_config_dir(*, environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(environ.get("APPDATA", ""), "claude-account-rotator")


def rotator_log_dir(*, environ: Mapping[str, str] = os.environ) -> str:
    return os.path.join(environ.get("LOCALAPPDATA", ""), "claude-account-rotator", "Logs")


# --- process liveness ------------------------------------------------------------------------------------


def _default_win_probe(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # Windows-only attribute of ctypes
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# --- process start time and image path (rotator.sessions discovery) --------------------------------------


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def _default_create_time_probe(pid: int) -> Optional[Tuple[int, int]]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_t, kernel_t, user_t = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        ok = kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                      ctypes.byref(kernel_t), ctypes.byref(user_t))
        if not ok:
            return None
        return creation.dwLowDateTime, creation.dwHighDateTime
    finally:
        kernel32.CloseHandle(handle)


_FILETIME_EPOCH_OFFSET_S = 11644473600  # seconds between 1601-01-01 (FILETIME epoch) and 1970-01-01


def filetime_to_datetime(low: int, high: int) -> Optional[datetime]:
    """A Windows FILETIME (100 ns ticks since 1601-01-01 UTC) as an aware UTC datetime; None if <= 0."""
    ticks = (high << 32) | low
    if ticks <= 0:
        return None
    seconds = ticks / 1e7 - _FILETIME_EPOCH_OFFSET_S
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def proc_start(pid: int, *,
               probe: Callable[[int], Optional[Tuple[int, int]]] = _default_create_time_probe
               ) -> Optional[datetime]:
    """A process's start time via GetProcessTimes; None when it cannot be read."""
    raw = probe(pid)
    return None if raw is None else filetime_to_datetime(*raw)


def _default_image_path_probe(pid: int) -> Optional[str]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def read_proc(pid: int, *, probe: Callable[[int], Optional[str]] = _default_image_path_probe
             ) -> Tuple[str, Tuple[str, ...], dict]:
    """(exec_path, argv, env) for ``pid``.

    Windows cannot read another process's argv/env without an elevated handle, so those always come
    back empty; rotator.sessions still recognizes a Claude Code process from its exe name and shows the
    session, with switching marked unavailable (no ORCA_TERMINAL_HANDLE to bind a pane to) this release.
    """
    path = probe(pid)
    return (path or "", (), {})


# --- console mode and key reading (rotator.cli status --watch) -------------------------------------------


def enable_vt_mode(fd: int, *, kernel32: Optional[object] = None) -> bool:
    """Turn on ENABLE_VIRTUAL_TERMINAL_PROCESSING for the console behind ``fd``; False on any failure.

    cli.py falls back to ``--ascii``-safe, non-ANSI output when this fails (old consoles, redirected
    output, cmd.exe before Windows 10).
    """
    k32 = kernel32 if kernel32 is not None else ctypes.windll.kernel32
    try:
        handle = msvcrt.get_osfhandle(fd)
    except (OSError, ValueError):
        return False
    mode = ctypes.c_ulong()
    if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(k32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))


def read_key_msvcrt(timeout: float, *, kbhit: Callable[[], bool] = None, getwch: Callable[[], str] = None,
                    sleep: Callable[[float], None] = None, monotonic: Callable[[], float] = None
                    ) -> Optional[str]:
    """Poll ``msvcrt.kbhit``/``getwch`` for up to ``timeout`` seconds; None when nothing was typed.

    A lead byte of a function/arrow key ('\\x00' or '\\xe0') is drained (its second byte discarded) and
    treated as no key: only single characters (q, Ctrl-C) matter to status --watch. Ctrl-C itself never
    reaches here -- the console's default ENABLE_PROCESSED_INPUT delivers it as a real KeyboardInterrupt,
    the same role cbreak/ISIG plays for RealTerm on POSIX.
    """
    import time as _time
    kbhit = kbhit if kbhit is not None else msvcrt.kbhit
    getwch = getwch if getwch is not None else msvcrt.getwch
    sleep = sleep if sleep is not None else _time.sleep
    monotonic = monotonic if monotonic is not None else _time.monotonic
    poll_s = 0.02
    deadline = monotonic() + timeout
    while True:
        if kbhit():
            ch = getwch()
            if ch in ("\x00", "\xe0"):
                getwch()  # discard the function/arrow key's second byte
                return None
            return ch
        left = deadline - monotonic()
        if left <= 0:
            return None
        sleep(min(poll_s, left))


# --- file locking (rotator.daemon FileLock) ---------------------------------------------------------------

# A fixed byte offset past any realistic lock-file content (a pid or a small JSON summary, both well
# under 1 KiB), so msvcrt's mandatory byte-range lock never overlaps bytes a reader actually looks at.
LOCK_BYTE_OFFSET = 4096
LOCK_NBYTES = 1


def try_lock_bytes(fd: int) -> bool:
    """Take a non-blocking exclusive lock on one byte at LOCK_BYTE_OFFSET; False when already held."""
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, LOCK_NBYTES)
    except OSError:
        return False
    finally:
        os.lseek(fd, pos, os.SEEK_SET)
    return True


def unlock_bytes(fd: int) -> None:
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, LOCK_NBYTES)
    except OSError:
        pass
    finally:
        os.lseek(fd, pos, os.SEEK_SET)
