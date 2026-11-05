"""Windows-only primitives: paths, process liveness/info, file locking and console VT mode.

Nothing here touches ``ctypes.windll`` or ``msvcrt`` at import time -- only inside the functions that
need them, at call time -- so importing this module on macOS/Linux is always safe. Every OS-specific
probe takes an injectable parameter so its surrounding logic can be exercised on any platform in tests;
only the small ``_default_*`` probes themselves require a real Windows console/kernel to run.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

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


# --- Win32 function prototypes ---------------------------------------------------------------------------
# ctypes calls an unprototyped function as returning a 32-bit int and converts Python ints to 32-bit ints,
# which cuts 64-bit pointers and handles in half. Every call below goes through these prototypes instead, on
# private DLL objects so the shared ``ctypes.windll`` attributes other code may rely on stay untouched.

_API: Optional[object] = None
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _api() -> object:
    global _API
    if _API is None:
        from types import SimpleNamespace
        k32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        shell32 = ctypes.WinDLL("shell32")  # type: ignore[attr-defined]
        P, H, BOOL, DWORD = ctypes.POINTER, ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong

        def proto(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = restype, list(argtypes)
            return fn

        _API = SimpleNamespace(
            OpenProcess=proto(k32, "OpenProcess", H, DWORD, BOOL, DWORD),
            CloseHandle=proto(k32, "CloseHandle", BOOL, H),
            GetExitCodeProcess=proto(k32, "GetExitCodeProcess", BOOL, H, P(DWORD)),
            GetProcessTimes=proto(k32, "GetProcessTimes", BOOL, H, P(_FILETIME), P(_FILETIME), P(_FILETIME),
                                  P(_FILETIME)),
            QueryFullProcessImageNameW=proto(k32, "QueryFullProcessImageNameW", BOOL, H, DWORD, ctypes.c_wchar_p,
                                             P(DWORD)),
            IsWow64Process=proto(k32, "IsWow64Process", BOOL, H, P(ctypes.c_int)),
            ReadProcessMemory=proto(k32, "ReadProcessMemory", BOOL, H, ctypes.c_void_p, P(ctypes.c_char),
                                    ctypes.c_size_t, P(ctypes.c_size_t)),
            LocalFree=proto(k32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p),
            CreateToolhelp32Snapshot=proto(k32, "CreateToolhelp32Snapshot", H, DWORD, DWORD),
            Process32FirstW=proto(k32, "Process32FirstW", BOOL, H, P(_PROCESSENTRY32W)),
            Process32NextW=proto(k32, "Process32NextW", BOOL, H, P(_PROCESSENTRY32W)),
            NtQueryInformationProcess=proto(ntdll, "NtQueryInformationProcess", ctypes.c_long, H, ctypes.c_ulong,
                                            P(_PROCESS_BASIC_INFORMATION), ctypes.c_ulong, P(ctypes.c_ulong)),
            CommandLineToArgvW=proto(shell32, "CommandLineToArgvW", P(ctypes.c_wchar_p), ctypes.c_wchar_p,
                                     P(ctypes.c_int)),
        )
    return _API


# --- process liveness ------------------------------------------------------------------------------------


def _default_win_probe(pid: int) -> bool:
    kernel32 = _api()
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
    kernel32 = _api()
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
    kernel32 = _api()
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


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

# x64 PEB/RTL_USER_PROCESS_PARAMETERS layout (undocumented but stable since Windows XP x64).
_PEB_PROCESS_PARAMETERS_OFFSET = 0x20
_PARAMS_COMMAND_LINE_OFFSET = 0x70  # UNICODE_STRING: Length(u16) MaximumLength(u16) pad(4) Buffer(ptr)
_PARAMS_ENVIRONMENT_OFFSET = 0x80  # PVOID Environment
_UNICODE_STRING_HEADER_SIZE = 16
_MAX_ENVIRONMENT_BYTES = 1024 * 1024  # 1 MiB cap on the environment block read


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def parse_env_block(data: bytes) -> Dict[str, str]:
    """Parse a Windows process environment block: a UTF-16-LE run of ``NAME=value\\0`` entries,
    normally ending in an empty entry (a block missing that final terminator still parses). Entries
    starting with ``=`` -- Windows' per-drive current-directory entries, e.g. ``=C:=C:\\x`` -- are
    skipped. Windows env names are case-insensitive; this keeps whatever casing the process used."""
    text = data.decode("utf-16-le", errors="replace")
    out: Dict[str, str] = {}
    for entry in text.split("\x00"):
        if not entry or entry.startswith("="):
            continue
        name, sep, value = entry.partition("=")
        if not sep:
            continue
        out[name] = value
    return out


_PAGE = 4096


def _env_block_end(data: bytes) -> Optional[int]:
    """Byte length of the block up to and including its final empty entry (two NUL characters), or None."""
    i = data.find(b"\x00\x00\x00\x00")
    while i != -1:
        if i % 2 == 0:
            return i + 4
        i = data.find(b"\x00\x00\x00\x00", i + 1)
    return None


def _read_env_block(read_memory: Callable[[int, int, int], bytes], handle: int, address: int) -> bytes:
    """The environment block at ``address``, read a page at a time up to its own end marker.

    EnvironmentSize is not used: a process that changes its environment gets a new block, and that field can
    then be smaller than the block, which would drop every variable past it. A page that cannot be read ends
    the block where it is; the read never goes past _MAX_ENVIRONMENT_BYTES."""
    data = b""
    while address and len(data) < _MAX_ENVIRONMENT_BYTES:
        want = min(_PAGE - (address % _PAGE), _MAX_ENVIRONMENT_BYTES - len(data))
        try:
            chunk = read_memory(handle, address, want)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
        end = _env_block_end(data)
        if end is not None:
            return data[:end]
        if len(chunk) < want:
            break
        address += len(chunk)
    return data


def _default_pointer_size() -> int:
    return ctypes.sizeof(ctypes.c_void_p)


def _default_open_vm_handle(pid: int) -> int:
    kernel32 = _api()
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(f"OpenProcess failed for pid {pid}")
    return handle


def _default_close_handle(handle: int) -> None:
    _api().CloseHandle(handle)


def _default_is_wow64_process(handle: int) -> bool:
    kernel32 = _api()
    result = ctypes.c_int(0)
    if not kernel32.IsWow64Process(handle, ctypes.byref(result)):
        raise OSError("IsWow64Process failed")
    return bool(result.value)


def _default_peb_address(handle: int) -> int:
    ntdll = _api()
    info = _PROCESS_BASIC_INFORMATION()
    returned = ctypes.c_ulong(0)
    status = ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info),
                                             ctypes.byref(returned))
    if status != 0:
        raise OSError(f"NtQueryInformationProcess failed: status={status:#x}")
    peb = info.PebBaseAddress
    if not peb:
        raise OSError("PebBaseAddress is null")
    return peb


def _default_read_memory(handle: int, address: int, size: int) -> bytes:
    kernel32 = _api()
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size, ctypes.byref(read)):
        raise OSError(f"ReadProcessMemory failed at {address:#x}")
    return buf.raw[: read.value]


def _default_split_command_line(command_line: str) -> Tuple[str, ...]:
    if not command_line:
        return ()
    shell32 = kernel32 = _api()
    argc = ctypes.c_int(0)
    argv_ptr = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
    if not argv_ptr:
        raise OSError("CommandLineToArgvW failed")
    try:
        arr = ctypes.cast(argv_ptr, ctypes.POINTER(ctypes.c_wchar_p))
        return tuple(arr[i] for i in range(argc.value))
    finally:
        kernel32.LocalFree(argv_ptr)


def _read_exact(read_memory: Callable[[int, int, int], bytes], handle: int, address: int, size: int) -> bytes:
    data = read_memory(handle, address, size)
    if len(data) != size:
        raise OSError(f"short read at {address:#x}: {len(data)}/{size} bytes")
    return data


def read_proc(pid: int, *,
              probe: Callable[[int], Optional[str]] = _default_image_path_probe,
              pointer_size: Callable[[], int] = _default_pointer_size,
              open_vm_handle: Callable[[int], int] = _default_open_vm_handle,
              close_handle: Callable[[int], None] = _default_close_handle,
              is_wow64: Callable[[int], bool] = _default_is_wow64_process,
              peb_address: Callable[[int], int] = _default_peb_address,
              read_memory: Callable[[int, int, int], bytes] = _default_read_memory,
              split_command_line: Callable[[str], Tuple[str, ...]] = _default_split_command_line,
              ) -> Tuple[str, Tuple[str, ...], dict]:
    """(exec_path, argv, env) for ``pid``.

    exec_path comes from QueryFullProcessImageNameW. argv and env are read from the target's PEB
    (NtQueryInformationProcess -> ProcessParameters -> CommandLine/Environment, via ReadProcessMemory)
    and really filled in now. Only supports a 64-bit Python reading a 64-bit target: a 32-bit Python or
    a WOW64 (32-bit) target raise OSError, since their PEB layouts differ and are not handled here.

    Raises OSError whenever the process can't be read: it has exited, a Win32/NT call fails, or a read
    comes back short. Never logs env values -- they can hold secrets.
    """
    path = probe(pid)
    if path is None:
        raise OSError(f"could not read process image path for pid {pid}")
    if pointer_size() != 8:
        raise OSError("32-bit Python cannot read a 64-bit process's memory")
    handle = open_vm_handle(pid)
    try:
        if is_wow64(handle):
            raise OSError(f"pid {pid} is a WOW64 (32-bit) process; unsupported")
        peb = peb_address(handle)
        params = struct.unpack(
            "<Q", _read_exact(read_memory, handle, peb + _PEB_PROCESS_PARAMETERS_OFFSET, 8))[0]

        header = _read_exact(read_memory, handle, params + _PARAMS_COMMAND_LINE_OFFSET,
                             _UNICODE_STRING_HEADER_SIZE)
        length = struct.unpack_from("<H", header, 0)[0]
        buffer_ptr = struct.unpack_from("<Q", header, 8)[0]
        cmdline_bytes = _read_exact(read_memory, handle, buffer_ptr, length) if length else b""
        argv = split_command_line(cmdline_bytes.decode("utf-16-le", errors="replace"))

        env_ptr = struct.unpack(
            "<Q", _read_exact(read_memory, handle, params + _PARAMS_ENVIRONMENT_OFFSET, 8))[0]
        env = parse_env_block(_read_env_block(read_memory, handle, env_ptr))
        if not env:
            # A live process always has some variables; none means it is still starting or the read failed.
            raise OSError(f"pid {pid}: environment not readable yet")
    finally:
        close_handle(handle)
    return (path, argv, env)


# --- process table (rotator.sessions discovery) -----------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def _default_snapshot_probe() -> List[Tuple[int, int, str]]:
    """One (pid, parent pid, lowercased image file name) row per process, via Toolhelp32."""
    kernel32 = _api()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot failed")
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise OSError("Process32FirstW failed")
        rows = []
        while True:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return rows
    finally:
        kernel32.CloseHandle(snapshot)


def process_table(*, snapshot_probe: Callable[[], List[Tuple[int, int, str]]] = _default_snapshot_probe
                  ) -> Dict[int, Tuple[int, str]]:
    """pid -> (parent pid, image file name lowercased), via CreateToolhelp32Snapshot.

    Raises OSError when the snapshot fails or comes back empty.
    """
    rows = snapshot_probe()
    if not rows:
        raise OSError("process snapshot came back empty")
    return {pid: (ppid, name) for pid, ppid, name in rows}


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
