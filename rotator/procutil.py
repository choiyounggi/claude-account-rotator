"""The one cross-platform process-liveness check shared by daemon.py, sessions.py and switcher.py."""
from __future__ import annotations

from typing import Callable, Optional

import os

from rotator.windows import IS_WINDOWS, _default_win_probe


def pid_alive(pid: int, *, is_windows: bool = IS_WINDOWS,
             windows_probe: Optional[Callable[[int], bool]] = None) -> bool:
    """True iff a process with this pid exists and has not exited.

    POSIX: ``os.kill(pid, 0)`` (a PermissionError still means alive; this never raises). Windows never
    calls ``os.kill`` for this: signal 0 there is CTRL_C_EVENT, not a liveness probe. Instead it opens
    the process with PROCESS_QUERY_LIMITED_INFORMATION and reads GetExitCodeProcess (STILL_ACTIVE).

    ``is_windows``/``windows_probe`` are injectable so the Windows branch runs in tests on any OS.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if is_windows:
        probe = windows_probe if windows_probe is not None else _default_win_probe
        try:
            return probe(pid)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
