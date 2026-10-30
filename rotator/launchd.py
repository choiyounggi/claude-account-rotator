"""launchd user agent for the rotator daemon: plist, install, uninstall."""

from __future__ import annotations

import math
import os
import plistlib
import subprocess
from typing import Callable, List, Tuple

from rotator.daemon import LEASE_MARGIN_S, atomic_write_text
from rotator.switcher import Timeouts, switch_budget_s

LABEL = "io.github.choiyounggi.claude-account-rotator"
LAUNCHCTL = "/bin/launchctl"
PYTHON = "/usr/bin/python3"
LAUNCHCTL_TIMEOUT_S = 30.0
EXIT_TIMEOUT_SLACK_S = 30

Runner = Callable[[List[str], float], Tuple[int, str, str]]


def plist_path_default() -> str:
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def exit_timeout_for(timeouts: Timeouts) -> int:
    """Seconds launchd waits after SIGTERM: one full switch plus the lease margin and slack."""
    return math.ceil(switch_budget_s(timeouts) + LEASE_MARGIN_S) + EXIT_TIMEOUT_SLACK_S


def build_plist(repo_root: str, config_path: str, log_dir: str, exit_timeout_s: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [PYTHON, os.path.join(repo_root, "bin", "rotator"), "--config", config_path, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": repo_root,
        "StandardOutPath": os.path.join(log_dir, "launchd.out.log"),
        "StandardErrorPath": os.path.join(log_dir, "launchd.err.log"),
        "ExitTimeOut": exit_timeout_s,
    }


def run_launchctl(argv: List[str], timeout_s: float) -> Tuple[int, str, str]:
    """The real launchctl runner."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    return (p.returncode, p.stdout, p.stderr)


def _target(uid: int) -> str:
    return "gui/%d/%s" % (uid, LABEL)


def install(*, runner: Runner, uid: int, plist_path: str, repo_root: str, config_path: str, log_dir: str,
            timeouts: Timeouts) -> Tuple[int, str]:
    """Write the plist and (re)load the agent; returns (exit code, message)."""
    os.makedirs(os.path.dirname(plist_path), 0o700, exist_ok=True)
    os.makedirs(log_dir, 0o700, exist_ok=True)
    xt = exit_timeout_for(timeouts)
    atomic_write_text(plist_path, plistlib.dumps(build_plist(repo_root, config_path, log_dir, xt)).decode("utf-8"))
    target = _target(uid)
    # `print` tells loaded from not-loaded; bootout's own rc for "not loaded" is not documented.
    rc, _, _ = runner([LAUNCHCTL, "print", target], LAUNCHCTL_TIMEOUT_S)
    if rc == 0:
        rc2, _, err2 = runner([LAUNCHCTL, "bootout", target], xt + LAUNCHCTL_TIMEOUT_S)
        if rc2 != 0:
            return (1, "launchctl bootout failed: " + err2.strip())
    rc3, _, err3 = runner([LAUNCHCTL, "bootstrap", "gui/%d" % uid, plist_path], LAUNCHCTL_TIMEOUT_S)
    if rc3 != 0:
        return (1, "launchctl bootstrap failed: " + err3.strip())
    return (0, "installed %s (%s)" % (LABEL, plist_path))


def _installed_exit_timeout(plist_path: str) -> int:
    try:
        with open(plist_path, "rb") as f:
            xt = plistlib.load(f).get("ExitTimeOut")
    except Exception:
        xt = None
    if type(xt) is not int:
        return exit_timeout_for(Timeouts())
    return xt


def uninstall(*, runner: Runner, uid: int, plist_path: str) -> Tuple[int, str]:
    """Unload the agent (exact label) and remove its plist; returns (exit code, message)."""
    xt = _installed_exit_timeout(plist_path)
    target = _target(uid)
    rc, _, _ = runner([LAUNCHCTL, "print", target], LAUNCHCTL_TIMEOUT_S)
    if rc == 0:
        rc2, _, err2 = runner([LAUNCHCTL, "bootout", target], xt + LAUNCHCTL_TIMEOUT_S)
        if rc2 != 0:
            return (1, "launchctl bootout failed: " + err2.strip())
    try:
        os.remove(plist_path)
    except FileNotFoundError:
        pass
    return (0, "uninstalled %s" % LABEL)
