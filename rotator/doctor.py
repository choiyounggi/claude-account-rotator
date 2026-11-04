"""Check that this Mac can run the rotator: macOS, Python, and an Orca with folder-based Claude accounts."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import subprocess
import sys
from typing import Any, Callable, List, NamedTuple, Optional, Tuple

from rotator.windows import IS_WINDOWS

ORCA_APP = "/Applications/Orca.app"
ORCA_INFO_PLIST = os.path.join(ORCA_APP, "Contents", "Info.plist")
# Orca keeps one folder per Claude account (claude-profiles/<id>/home) and the selected-host pointer from
# 1.4.224 on; before that every account shared one credential set that Orca replayed, which the rotator
# cannot switch between.
ORCA_MIN_VERSION: Tuple[int, int, int] = (1, 4, 224)
PYTHON_MIN_VERSION: Tuple[int, int] = (3, 9)
ORCA_CLI_VERSION_TIMEOUT_S = 10.0
# A suffix after the third number ("1.4.250-beta.1") is ignored; a leading "v" or a missing number is not.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?![\d.])")

OK = "ok"
WARN = "warn"
FAIL = "fail"


class Check(NamedTuple):
    name: str
    level: str  # OK, WARN or FAIL
    detail: str


def _dotted(v: Tuple[int, ...]) -> str:
    return ".".join(str(x) for x in v)


def parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    """``"1.4.241"`` or ``"1.4.241-beta.1"`` -> (1, 4, 241); None when it does not start with three numbers."""
    m = _VERSION_RE.match(text.strip()) if isinstance(text, str) else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _read_plist(path: str) -> dict:
    with open(path, "rb") as f:
        return plistlib.load(f)


def orca_version(read_plist: Callable[[str], dict] = _read_plist) -> Optional[str]:
    """CFBundleShortVersionString of the installed Orca.app; None when it is missing or unreadable."""
    try:
        v = read_plist(ORCA_INFO_PLIST).get("CFBundleShortVersionString")
    except Exception:
        return None
    return v if isinstance(v, str) else None


def orca_version_windows(cli_path: Optional[str], *, run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """Orca's version on Windows via ``<cli_path> --version``; None when ``cli_path`` is unset or it fails.

    Electron intercepts ``--version`` on the app's own ``Orca.exe``, so the bundled CLI (src/cli/index.ts in the
    Orca source: ``argv[0] in ('--version', '-v')`` prints the bare version string) is the only reliable
    way to read it; there is no Windows equivalent of macOS's Info.plist bundle version. ``creationflags``
    suppresses the console this would otherwise flash under pythonw.exe (the daemon start calls this via the
    Orca gate, with no console of its own to flash into).
    """
    if not cli_path:
        return None
    try:
        proc = run([cli_path, "--version"], capture_output=True, text=True,
                  timeout=ORCA_CLI_VERSION_TIMEOUT_S, check=False,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not isinstance(proc.stdout, str):
        return None
    out = proc.stdout.strip()
    return out or None


def check_system(system: str) -> Check:
    if system == "Darwin":
        return Check("macOS", OK, platform.mac_ver()[0] or system)
    if system == "Windows":
        return Check("macOS", OK, "Windows %s" % (platform.win32_ver()[0] or system))
    return Check("macOS", FAIL, "%s: the rotator runs only on macOS or Windows" % (system or "unknown OS"))


def check_python(version: Tuple[int, ...]) -> Check:
    shown = "%s (%s)" % (_dotted(version[:3]), sys.executable)
    if tuple(version[:2]) >= PYTHON_MIN_VERSION:
        return Check("Python", OK, shown)
    return Check("Python", FAIL, "%s: %s or newer is required" % (shown, _dotted(PYTHON_MIN_VERSION)))


def check_orca(version: Optional[str], *, where: str = ORCA_APP) -> Check:
    need = "%s or newer" % _dotted(ORCA_MIN_VERSION)
    if version is None:
        return Check("Orca", FAIL, "not found at %s: install Orca (%s)" % (where, need))
    parsed = parse_version(version)
    if parsed is None:
        return Check("Orca", FAIL, "version %r is not readable; %s is required" % (version, need))
    if parsed < ORCA_MIN_VERSION:
        return Check("Orca", FAIL, "%s is too old: update Orca to %s (folder-based Claude accounts)"
                     % (version, need))
    return Check("Orca", OK, "%s (%s)" % (version, need))


def check_orca_cli(path: str, executable: bool) -> Check:
    if executable:
        return Check("orca CLI", OK, path)
    return Check("orca CLI", FAIL, "%s is missing or not executable: reinstall Orca" % path)


def check_accounts(count: int, root: str) -> Check:
    if count == 0:
        return Check("Claude accounts", WARN, "none yet under %s: add Claude accounts in Orca" % root)
    if count == 1:
        return Check("Claude accounts", WARN, "1 under %s: switching needs at least 2" % root)
    return Check("Claude accounts", OK, "%d under %s" % (count, root))


def check_config(error: Optional[str]) -> Check:
    if error is None:
        return Check("config", OK, "readable")
    return Check("config", FAIL, "%s (the other checks use the defaults)" % error)


def check_agent(installed: bool, stale: bool, *, supported: bool = True) -> Check:
    if not supported:
        return Check("background agent", WARN, "not available on Windows yet")
    if not installed:
        return Check("background agent", WARN, "not installed: run `rotator install` to switch automatically")
    if stale:
        return Check("background agent", WARN, "runs an older copy: run `rotator install` again")
    return Check("background agent", OK, "installed")


def gate(system: str, version: Optional[str], *, where: str = ORCA_APP) -> Optional[str]:
    """Why this machine cannot run the rotator (its OS and the Orca version), or None when it can."""
    for c in (check_system(system), check_orca(version, where=where)):
        if c.level == FAIL:
            return "%s: %s" % (c.name, c.detail)
    return None


def render(checks: List[Check]) -> List[str]:
    mark = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]"}
    width = max(len(c.name) for c in checks)
    return ["%s %s  %s" % (mark[c.level], c.name.ljust(width), c.detail) for c in checks]


def exit_code(checks: List[Check]) -> int:
    """1 when any check failed (the rotator cannot run here), else 0; warnings never fail."""
    return 1 if any(c.level == FAIL for c in checks) else 0
