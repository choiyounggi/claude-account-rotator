"""Live Claude Code session discovery (registry files + macOS process inspection)."""
from __future__ import annotations

import calendar
import ctypes
import ctypes.util
import json
import logging
import os
import re
import struct
from datetime import datetime, timezone
from typing import Callable, Dict, FrozenSet, List, NamedTuple, Optional, Sequence, Tuple

from rotator.accounts import _read_text_utf8
from rotator.models import Profile, ReadText, Session

log = logging.getLogger("rotator.sessions")

ENV_WHITELIST: Tuple[str, ...] = (
    "CLAUDE_CONFIG_DIR",
    "ORCA_TERMINAL_HANDLE",
    "ORCA_AGENT_PANE",
    "ORCA_WORKTREE_ID",
    "ORCA_CLAUDE_PROFILE_POINTER",
)
REGISTRY_STATUSES: FrozenSet[str] = frozenset({"idle", "busy", "shell"})

ListDir = Callable[[str], List[str]]
PidAlive = Callable[[int], bool]
ProcInfo = Tuple[str, Tuple[str, ...], Dict[str, str]]
ReadProc = Callable[[int], ProcInfo]
ProcStart = Callable[[int], Optional[datetime]]

_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROC = 14
_KERN_PROC_PID = 1
_KERN_PROCARGS2 = 49
_KINFO_BUFSIZE = 1024  # >= sizeof(struct kinfo_proc), 648 bytes on current macOS
_LIBC = None


def parse_procargs(buf: bytes) -> ProcInfo:
    """Parse a KERN_PROCARGS2 buffer; only ENV_WHITELIST keys are returned."""
    if type(buf) is not bytes:
        raise TypeError("buf must be bytes")
    if len(buf) < 4:
        raise ValueError("procargs too short")
    (argc,) = struct.unpack_from("=i", buf, 0)
    if argc < 0:
        raise ValueError("procargs negative argc")
    end = buf.find(b"\0", 4)
    if end == -1:
        raise ValueError("procargs exec path unterminated")
    exec_path = buf[4:end]
    bound = 4 + ((end - 4 + 1 + 7) // 8) * 8
    limit = min(bound, len(buf))
    pos = end + 1
    # argv starts at the next 8-byte boundary after the exec path;
    # never skip past it, or an empty argv[0] would be eaten as padding.
    while pos < limit and buf[pos] == 0:
        pos += 1
    argv = []
    for _ in range(argc):
        nul = buf.find(b"\0", pos)
        if nul == -1:
            raise ValueError("procargs argc exceeds strings")
        argv.append(buf[pos:nul])
        pos = nul + 1
    env: Dict[str, str] = {}
    for tok in buf[pos:].split(b"\0"):
        if not tok or b"=" not in tok:
            continue
        key, _, val = tok.partition(b"=")
        k = key.decode("utf-8", "replace")
        if k in ENV_WHITELIST and k not in env:
            env[k] = val.decode("utf-8", "replace")
    return (exec_path.decode("utf-8", "replace"), tuple(a.decode("utf-8", "replace") for a in argv), env)


def _libc():
    global _LIBC
    if _LIBC is None:
        path = ctypes.util.find_library("c")
        if path is None:
            raise OSError("libc not found")
        lib = ctypes.CDLL(path, use_errno=True)
        lib.sysctl.argtypes = (
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        )
        lib.sysctl.restype = ctypes.c_int
        _LIBC = lib
    return _LIBC


def _sysctl(mib: Sequence[int], size: int) -> bytes:
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    sz = ctypes.c_size_t(size)
    rc = _libc().sysctl(arr, len(mib), buf, ctypes.byref(sz), None, 0)
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return buf.raw[: sz.value]


def _sysctl_int(mib: Sequence[int]) -> int:
    raw = _sysctl(mib, ctypes.sizeof(ctypes.c_int))
    if len(raw) < 4:
        raise OSError("sysctl short int")
    return struct.unpack_from("=i", raw, 0)[0]


def _list_dir(path: str) -> List[str]:
    return os.listdir(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def _read_proc(pid: int) -> ProcInfo:
    return parse_procargs(_sysctl((_CTL_KERN, _KERN_PROCARGS2, pid), _sysctl_int((_CTL_KERN, _KERN_ARGMAX))))


def _proc_start(pid: int) -> Optional[datetime]:
    try:
        raw = _sysctl((_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid), _KINFO_BUFSIZE)
    except OSError:
        return None
    if len(raw) < 12:
        return None
    sec, usec = struct.unpack_from("=qi", raw, 0)
    try:
        return datetime.fromtimestamp(sec + usec / 1e6, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


_PID_MAX = 2147483647
_REGISTRY_NAME_RE = re.compile(r"([0-9]+)\.json")
_PROC_START_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +([0-9]{1,2}) "
    r"([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})"
)
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


class _Candidate(NamedTuple):
    pid: int
    session_id: str
    cwd: str
    status_raw: object
    proc_start_raw: object
    started_at_raw: object
    status_updated_at: int
    path: str
    order: int


def _skip(path: str, reason: str) -> None:
    log.debug("skip session %s: %s", path, reason)


def _parse_proc_start(value: object) -> Optional[int]:
    if not isinstance(value, str):
        return None
    m = _PROC_START_RE.fullmatch(value)
    if m is None:
        return None
    try:
        dt = datetime(int(m.group(7)), _MONTHS[m.group(2)], int(m.group(3)),
                      int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=timezone.utc)
    except ValueError:
        return None
    return calendar.timegm(dt.utctimetuple())


def _parse_started_at(value: object) -> Optional[datetime]:
    if type(value) is not int or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _map_status(value: object) -> str:
    return value if isinstance(value, str) and value in REGISTRY_STATUSES else "unknown"


def _scan_registry(homes: Sequence[str], list_dir: ListDir, read_text: ReadText) -> List[_Candidate]:
    out: List[_Candidate] = []
    order = 0
    for home in homes:
        d = os.path.join(home, "sessions")
        try:
            names = list_dir(d)
        except OSError:
            continue
        kept = []
        for name in names:
            m = _REGISTRY_NAME_RE.fullmatch(name)
            if m:
                kept.append((int(m.group(1)), name))
        kept.sort()
        for file_pid, name in kept:
            path = os.path.join(d, name)
            try:
                text = read_text(path)
            except OSError:
                _skip(path, "unreadable")
                continue
            try:
                obj = json.loads(text)
            except (ValueError, RecursionError):
                _skip(path, "bad_json")
                continue
            if not isinstance(obj, dict):
                _skip(path, "bad_shape")
                continue
            pid = obj.get("pid")
            if type(pid) is not int or not 1 <= pid <= _PID_MAX or pid != file_pid:
                _skip(path, "bad_shape")
                continue
            sid, cwd = obj.get("sessionId"), obj.get("cwd")
            if not isinstance(sid, str) or not isinstance(cwd, str):
                _skip(path, "bad_shape")
                continue
            if obj.get("kind") != "interactive":
                _skip(path, "kind")
                continue
            sua = obj.get("statusUpdatedAt")
            sua = sua if type(sua) is int else 0
            out.append(_Candidate(pid, sid, cwd, obj.get("status"), obj.get("procStart"),
                                  obj.get("startedAt"), sua, path, order))
            order += 1
    return out


_CLAUDE_BASENAMES = frozenset({"claude", "claude.exe"})
_CLAUDE_PKG_SEGMENT = "/@anthropic-ai/claude-code/"


def _within(live: object, recorded: int) -> bool:
    return isinstance(live, datetime) and live.utcoffset() is not None and abs(live.timestamp() - recorded) <= 1.0


def _is_claude(exec_path: str, argv: Tuple[str, ...]) -> bool:
    if os.path.basename(exec_path) in _CLAUDE_BASENAMES:
        return True
    if argv and os.path.basename(argv[0]) in _CLAUDE_BASENAMES:
        return True
    return any(_CLAUDE_PKG_SEGMENT in "/" + x for x in (exec_path,) + tuple(argv[:2]))


def _pick_winner(cands: Sequence[_Candidate]) -> _Candidate:
    return min(cands, key=lambda c: (-c.status_updated_at, c.order))


def session_homes(profiles: Sequence[Profile]) -> List[str]:
    """Registry homes to scan: Claude Code's default ~/.claude first, then each profile home in order."""
    return [os.path.normpath(os.path.expanduser("~/.claude"))] + [p.home for p in profiles]


def discover_sessions(
    homes: Sequence[str],
    *,
    list_dir: Optional[ListDir] = None,
    read_text: Optional[ReadText] = None,
    pid_alive: Optional[PidAlive] = None,
    read_proc: Optional[ReadProc] = None,
    proc_start: Optional[ProcStart] = None,
) -> List[Session]:
    """Return the live interactive Claude Code sessions registered under ``homes``, sorted by pid.

    Never raises for bad registry data or unreadable processes: those entries are skipped with a
    DEBUG record on the ``rotator.sessions`` logger. Only CLAUDE_CONFIG_DIR and ORCA_TERMINAL_HANDLE
    are read from a process env. A ``str`` ``homes`` raises TypeError.
    """
    if isinstance(homes, str):
        raise TypeError("homes must be a sequence of paths, not a str")
    list_dir = list_dir if list_dir is not None else _list_dir
    read_text = read_text if read_text is not None else _read_text_utf8
    pid_alive = pid_alive if pid_alive is not None else _pid_alive
    read_proc = read_proc if read_proc is not None else _read_proc
    proc_start = proc_start if proc_start is not None else _proc_start

    cands = _scan_registry(homes, list_dir, read_text)
    groups: Dict[int, List[_Candidate]] = {}
    for c in cands:
        groups.setdefault(c.pid, []).append(c)
    default_home = os.path.normpath(os.path.expanduser("~/.claude"))
    out: List[Session] = []

    for pid in sorted(groups):
        group = groups[pid]
        if not pid_alive(pid):
            for c in group:
                _skip(c.path, "dead")
            continue

        rec = [_parse_proc_start(c.proc_start_raw) for c in group]
        if any(r is not None for r in rec):
            live = proc_start(pid)
            if not isinstance(live, datetime) or live.utcoffset() is None:
                for c in group:
                    _skip(c.path, "unreadable")
                continue
            survivors = []
            for c, r in zip(group, rec):
                if r is not None and not _within(live, r):
                    _skip(c.path, "pid_reused")
                else:
                    survivors.append((c, r))
        else:
            survivors = list(zip(group, rec))
        if not survivors:
            continue

        w = _pick_winner([c for c, _ in survivors])
        anchor = next((r for c, r in survivors if c is w), None)
        if anchor is None:
            anchor = next((r for _, r in survivors if r is not None), None)

        try:
            exec_path, argv, env = read_proc(pid)
        except (OSError, ValueError):
            _skip(w.path, "unreadable")
            continue

        if anchor is not None:
            # Re-check after the read: the process may have exited and its pid been reused meanwhile.
            live2 = proc_start(pid)
            if not isinstance(live2, datetime) or live2.utcoffset() is None:
                _skip(w.path, "unreadable")
                continue
            if not _within(live2, anchor):
                _skip(w.path, "pid_reused")
                continue
        elif not _is_claude(exec_path, tuple(argv)):
            _skip(w.path, "not_claude")
            continue

        cfg = env.get("CLAUDE_CONFIG_DIR")
        if cfg is None or cfg == "":
            config_dir = default_home
        elif os.path.isabs(cfg):
            config_dir = cfg
        else:
            _skip(w.path, "config_dir_relative")
            continue
        terminal_handle = env.get("ORCA_TERMINAL_HANDLE") or None

        try:
            s = Session(
                pid=w.pid,
                session_id=w.session_id,
                cwd=w.cwd,
                status=_map_status(w.status_raw),
                config_dir=config_dir,
                argv=tuple(argv),
                terminal_handle=terminal_handle,
                started_at=_parse_started_at(w.started_at_raw),
            )
        except (ValueError, TypeError):
            _skip(w.path, "invalid")
            continue
        out.append(s)

    return sorted(out, key=lambda s: s.pid)
