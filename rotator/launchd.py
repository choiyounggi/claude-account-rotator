"""launchd user agent for the rotator daemon: plist, install, uninstall."""

from __future__ import annotations

import glob
import math
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from rotator.daemon import LEASE_MARGIN_S, atomic_write_text
from rotator.switcher import Timeouts, switch_budget_s

LABEL = "io.github.choiyounggi.claude-account-rotator"
LAUNCHCTL = "/bin/launchctl"
PYTHON = "/usr/bin/python3"
LAUNCHCTL_TIMEOUT_S = 30.0
EXIT_TIMEOUT_SLACK_S = 30
GIT_TIMEOUT_S = 5.0
APP_DIRNAME = "app"
VERSION_FILENAME = "VERSION"
UNKNOWN_COMMIT = "unknown"
# launchd-spawned processes are denied these TCC-protected folders even when the user's terminal is not.
PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")
_COMMIT_RE = re.compile(r"[0-9a-f]{4,40}")

Runner = Callable[[List[str], float], Tuple[int, str, str]]
CopyFile = Callable[[str, str], object]


def plist_path_default() -> str:
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def exit_timeout_for(timeouts: Timeouts) -> int:
    """Seconds launchd waits after SIGTERM: one full switch plus the lease margin and slack."""
    return math.ceil(switch_budget_s(timeouts) + LEASE_MARGIN_S) + EXIT_TIMEOUT_SLACK_S


def app_dir(state_dir: str) -> str:
    return os.path.join(state_dir, APP_DIRNAME)


def protected_dir(path: str) -> Optional[str]:
    """``~/<folder>`` when ``path`` lies in one of PROTECTED_DIRS, else None."""
    if not path:
        return None
    home = os.path.realpath(os.path.expanduser("~"))
    p = os.path.realpath(path)
    for folder in PROTECTED_DIRS:
        base = os.path.join(home, folder)
        if p == base or p.startswith(base + os.sep):
            return "~/" + folder
    return None


def build_plist(app_root: str, config_path: str, log_dir: str, exit_timeout_s: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [PYTHON, os.path.join(app_root, "bin", "rotator"), "--config", config_path, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": app_root,
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


def run_git(argv: List[str], timeout_s: float) -> Tuple[int, str, str]:
    """The real git runner (a missing git binary raises; source_commit treats that as unknown)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    return (p.returncode, p.stdout, p.stderr)


def source_commit(root: str, runner: Runner = run_git) -> Optional[str]:
    """Short HEAD commit of the checkout at ``root``; None when it is not a checkout or git fails."""
    # Without its own .git, git would walk up and report whatever repository contains root.
    if not os.path.lexists(os.path.join(root, ".git")):
        return None
    try:
        rc, out, _ = runner(["git", "-C", root, "rev-parse", "--short", "HEAD"], GIT_TIMEOUT_S)
    except Exception:
        return None
    commit = out.strip()
    return commit if rc == 0 and _COMMIT_RE.fullmatch(commit) else None


def installed_commit(state_dir: str) -> Optional[str]:
    """The commit recorded in ``<state_dir>/app/VERSION``; None when absent, unreadable or unknown."""
    try:
        with open(os.path.join(app_dir(state_dir), VERSION_FILENAME), encoding="utf-8") as f:
            first = f.readline().strip()
    except (OSError, ValueError):
        return None
    return first if _COMMIT_RE.fullmatch(first) else None


def install_stale(plist_path: str, state_dir: str, source_root: str,
                  commit_of: Callable[[str], Optional[str]] = source_commit) -> bool:
    """True only when the agent is installed and its app copy came from a different commit than source_root.

    Either commit unknown means not stale; short hashes of different lengths match on their common prefix."""
    try:
        if not os.path.isfile(plist_path):
            return False
        installed = installed_commit(state_dir)
        current = commit_of(source_root)
    except Exception:
        return False
    if installed is None or current is None:
        return False
    return not (installed.startswith(current) or current.startswith(installed))


class AppCopyError(Exception):
    pass


def _check_real_dir(path: str) -> bool:
    """False if ``path`` exists as anything but a real directory (a symlink is never followed)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    return stat.S_ISDIR(st.st_mode)


def _app_files(source_root: str) -> List[Tuple[str, int]]:
    """(relative path, mode) of every file of the runnable app: bin/rotator and rotator/*.py, nothing else."""
    entry = os.path.join(source_root, "bin", "rotator")
    if not os.path.isfile(entry):
        raise AppCopyError("no bin/rotator in %s" % source_root)
    mods = sorted(os.path.basename(p) for p in glob.glob(os.path.join(glob.escape(source_root), "rotator", "*.py"))
                  if os.path.isfile(p))
    if not mods:
        raise AppCopyError("no rotator/*.py in %s" % source_root)
    return [(os.path.join("bin", "rotator"), 0o700)] + [(os.path.join("rotator", m), 0o600) for m in mods]


def copy_app(source_root: str, state_dir: str, *, commit: str, copied_at: datetime,
             copy_file: CopyFile = shutil.copyfile) -> str:
    """Copy the runnable app into ``<state_dir>/app`` and return that path.

    The copy is built in a fresh sibling directory and renamed into place; the previous copy is removed only
    after the new one is complete, and is put back if the swap fails. Raises AppCopyError or OSError."""
    dest = app_dir(state_dir)
    if not _check_real_dir(dest):
        raise AppCopyError("%s is not a real directory; remove it yourself" % dest)
    files = _app_files(source_root)
    os.makedirs(state_dir, 0o700, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".app-new-", dir=state_dir)
    old: Optional[str] = None
    try:
        os.chmod(tmp, 0o700)
        for sub in ("bin", "rotator"):
            os.mkdir(os.path.join(tmp, sub), 0o700)
            os.chmod(os.path.join(tmp, sub), 0o700)
        for rel, mode in files:
            target = os.path.join(tmp, rel)
            copy_file(os.path.join(source_root, rel), target)
            os.chmod(target, mode)
        stamp = copied_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fd = os.open(os.path.join(tmp, VERSION_FILENAME), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%s\n%s\n" % (commit, stamp))
        os.chmod(os.path.join(tmp, VERSION_FILENAME), 0o600)
        if os.path.lexists(dest):
            if not _check_real_dir(dest):
                raise AppCopyError("%s is not a real directory; remove it yourself" % dest)
            old = tmp + ".old"
            os.rename(dest, old)
        try:
            os.rename(tmp, dest)
        except BaseException:
            if old is not None:
                os.rename(old, dest)
                old = None
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def remove_app(state_dir: str) -> Optional[str]:
    """Remove exactly ``<state_dir>/app`` if it is a real directory there; returns an error message or None."""
    dest = app_dir(state_dir)
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode) or os.path.dirname(os.path.realpath(dest)) != os.path.realpath(state_dir):
        return "refused to remove %s: not a real directory" % dest
    try:
        shutil.rmtree(dest)
    except OSError as e:
        return "could not remove %s (%s)" % (dest, type(e).__name__)
    return None


def _target(uid: int) -> str:
    return "gui/%d/%s" % (uid, LABEL)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def install(*, runner: Runner, uid: int, plist_path: str, source_root: str, state_dir: str, config_path: str,
            log_dir: str, timeouts: Timeouts, commit_of: Callable[[str], Optional[str]] = source_commit,
            now: Callable[[], datetime] = _utc_now, copy_file: CopyFile = shutil.copyfile) -> Tuple[int, str]:
    """Copy the app out of ``source_root`` into ``<state_dir>/app``, point the plist at that copy and (re)load
    the agent; returns (exit code, message). Nothing is written when the copy fails."""
    folder = protected_dir(config_path)
    if folder is not None:
        return (2, "config %s is under %s, which the launchd agent cannot read; move it (default: %s)"
                % (config_path, folder, os.path.join(state_dir, "config.json")))
    commit = commit_of(source_root) or UNKNOWN_COMMIT
    try:
        app_root = copy_app(source_root, state_dir, commit=commit, copied_at=now(), copy_file=copy_file)
    except (AppCopyError, OSError) as e:
        return (1, "app copy failed: %s" % (e if isinstance(e, AppCopyError) else "%s (%s)" % (type(e).__name__, e)))
    os.makedirs(os.path.dirname(plist_path), 0o700, exist_ok=True)
    os.makedirs(log_dir, 0o700, exist_ok=True)
    xt = exit_timeout_for(timeouts)
    atomic_write_text(plist_path, plistlib.dumps(build_plist(app_root, config_path, log_dir, xt)).decode("utf-8"))
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
    return (0, "installed %s (%s); app copy %s at %s" % (LABEL, plist_path, app_root, commit))


def _installed_exit_timeout(plist_path: str) -> int:
    try:
        with open(plist_path, "rb") as f:
            xt = plistlib.load(f).get("ExitTimeOut")
    except Exception:
        xt = None
    if type(xt) is not int:
        return exit_timeout_for(Timeouts())
    return xt


def uninstall(*, runner: Runner, uid: int, plist_path: str, state_dir: str) -> Tuple[int, str]:
    """Unload the agent (exact label), remove its plist and the app copy; returns (exit code, message)."""
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
    problem = remove_app(state_dir)
    if problem is not None:
        return (1, "uninstalled %s; %s" % (LABEL, problem))
    return (0, "uninstalled %s" % LABEL)
