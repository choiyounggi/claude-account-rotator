"""Windows Task Scheduler registration for the rotator daemon: task XML, install, uninstall.

Mirrors the shape of rotator/launchd.py; the app copy (copy_app/remove_app), commit tracking
(source_commit/installed_commit/commits_differ) and repo_root are reused from there unchanged --
they are plain file and git operations with nothing macOS-specific about them. launchd.protected_dir
is a macOS TCC rule and has no Windows equivalent, so install() here never calls it.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, List, Mapping, Optional, Tuple
from xml.sax.saxutils import escape

from rotator import daemon, launchd
from rotator.config import LOG_DIR_DEFAULT
from rotator.switcher import Timeouts
from rotator.windows import IS_WINDOWS

TASK_NAME = "claude-account-rotator"
TASK_XML_FILENAME = "task.xml"
QUERY_TIMEOUT_S = 30.0
CREATE_TIMEOUT_S = 30.0
# How long install() waits, after /Run, for the daemon to take its lock before reporting it never started.
DAEMON_START_TIMEOUT_S = 10.0
LOCK_POLL_INTERVAL_S = 0.5

Runner = Callable[[List[str], float], Tuple[int, str, str]]


def schtasks_exe(environ: Mapping[str, str] = os.environ) -> str:
    # ntpath, not os.path: this is always a Windows path for the target machine, even when built in a test
    # running on macOS/Linux.
    return ntpath.join(environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")


def run_schtasks(argv: List[str], timeout_s: float) -> Tuple[int, str, str]:
    """The real schtasks runner. ``creationflags`` suppresses the console schtasks.exe would otherwise flash
    under pythonw.exe; ``errors="replace"`` keeps output that fails to decode from coming back as None, which
    would make a later ``.strip()`` raise."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout_s,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    except OSError as e:
        return (1, "", "%s: %s" % (type(e).__name__, e))
    return (p.returncode, p.stdout or "", p.stderr or "")


def _user_id(environ: Mapping[str, str]) -> Optional[str]:
    """``DOMAIN\\user`` from the environment, or bare ``user`` when USERDOMAIN is missing; None when even
    USERNAME is missing."""
    username = environ.get("USERNAME")
    if not username:
        return None
    domain = environ.get("USERDOMAIN")
    return "%s\\%s" % (domain, username) if domain else username


def build_task_xml(python_exe: str, app_root: str, config_path: str, user_id: str, state_dir: str, *,
                   exists: Callable[[str], bool] = os.path.isfile) -> str:
    """Task Scheduler schema 1.2 XML: a LogonTrigger for ``user_id`` that repeats every 5 minutes so a daemon
    that merely exits (a config error, the Orca gate, a crash -- none of which is a launch failure) is revived
    instead of staying dead until the next logon; an InteractiveToken/LeastPrivilege principal; and an Exec
    action that runs the daemon with no console window.

    ``WorkingDirectory`` is ``state_dir``, not the app copy: Windows refuses to rename a running process's
    current working directory, and the app copy under ``state_dir`` is exactly what install() renames.

    Every value is escaped; ``Command`` is additionally wrapped in literal double quotes so a path containing a
    space (``C:\\Program Files\\...``) does not split into two tokens -- ``escape()`` does not touch ``"`` in
    element text, so the quotes survive verbatim. The result must be written as UTF-16 with a BOM
    (``_write_task_xml``), which is what the declared ``encoding="UTF-16"`` promises and what ``schtasks /XML``
    reliably accepts.
    """
    pythonw = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
    command = pythonw if exists(pythonw) else python_exe
    entry = os.path.join(app_root, "bin", "rotator")
    args = subprocess.list2cmdline([entry, "daemon", "--config", config_path])
    uid, cmd = escape(user_id), escape('"%s"' % command)
    arglist, cwd = escape(args), escape(state_dir)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <Triggers>\n'
        '    <LogonTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        '      <UserId>%(uid)s</UserId>\n'
        '      <Repetition>\n'
        '        <Interval>PT5M</Interval>\n'
        '        <StopAtDurationEnd>false</StopAtDurationEnd>\n'
        '      </Repetition>\n'
        '    </LogonTrigger>\n'
        '  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        '      <UserId>%(uid)s</UserId>\n'
        '      <LogonType>InteractiveToken</LogonType>\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <RestartOnFailure>\n'
        '      <Interval>PT1M</Interval>\n'
        '      <Count>999</Count>\n'
        '    </RestartOnFailure>\n'
        '  </Settings>\n'
        '  <Actions Context="Author">\n'
        '    <Exec>\n'
        '      <Command>%(cmd)s</Command>\n'
        '      <Arguments>%(args)s</Arguments>\n'
        '      <WorkingDirectory>%(cwd)s</WorkingDirectory>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    ) % {"uid": uid, "cmd": cmd, "args": arglist, "cwd": cwd}


def _write_task_xml(path: str, xml: str) -> None:
    data = xml.encode("utf-16")  # adds a BOM, matching the declared encoding="UTF-16"
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".task-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def query_installed(runner: Runner, *, environ: Mapping[str, str] = os.environ) -> bool:
    rc, _, _ = runner([schtasks_exe(environ), "/Query", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    return rc == 0


def install_stale(runner: Runner, state_dir: str, source_root: str,
                  commit_of: Callable[[str], Optional[str]] = launchd.source_commit,
                  environ: Mapping[str, str] = os.environ) -> bool:
    """True only when the task is registered in Task Scheduler and its app copy came from a different commit
    than source_root; mirrors launchd.install_stale (see launchd.commits_differ)."""
    try:
        if not query_installed(runner, environ=environ):
            return False
        return launchd.commits_differ(launchd.installed_commit(state_dir), commit_of(source_root))
    except Exception:
        return False


def _lock_is_free(lock_path: str) -> bool:
    """True when the daemon's lock can be taken (so nothing holds it); releases it immediately when it can,
    per daemon.try_lock's own contract."""
    lk = daemon.try_lock(lock_path)
    if lk is None:
        return False
    lk.release()
    return True


def _poll_until(condition: Callable[[], bool], timeout_s: float, *, sleep: Callable[[float], None],
                clock: Callable[[], float], interval_s: float = LOCK_POLL_INTERVAL_S) -> bool:
    """True once ``condition()`` is true (checked immediately, then every ``interval_s``); False if it never
    becomes true within ``timeout_s``."""
    deadline = clock() + timeout_s
    while True:
        if condition():
            return True
        if clock() >= deadline:
            return False
        sleep(interval_s)


def _stop_daemon(runner: Runner, sch: str, state_dir: str, timeout_s: float, *,
                 sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
    """Ask a running daemon to exit on its own -- writing ``<state_dir>/stop-request``, which it polls for --
    and wait up to ``timeout_s`` for its lock to free up, so a token refresh in flight can finish and write
    instead of being killed mid-write by ``/End`` (TerminateProcess). ``/End`` still runs as a fallback
    afterward either way (its rc is ignored, as before this existed: fine whether or not an instance was ever
    running), and the request file is removed whether or not the wait succeeded.
    """
    stop_path = os.path.join(state_dir, daemon.STOP_REQUEST_FILENAME)
    daemon.atomic_write_text(stop_path, "%s\n" % _utc_now().isoformat())
    lock_path = os.path.join(state_dir, daemon.LOCK_FILENAME)
    _poll_until(lambda: _lock_is_free(lock_path), timeout_s, sleep=sleep, clock=clock)
    runner([sch, "/End", "/TN", TASK_NAME], QUERY_TIMEOUT_S)  # ignore rc: fine whether or not it was running
    with contextlib.suppress(OSError):
        os.remove(stop_path)


def install(*, runner: Runner, python_exe: str, source_root: str, state_dir: str, config_path: str,
            timeouts: Timeouts, commit_of: Callable[[str], Optional[str]] = launchd.source_commit,
            now: Callable[[], datetime] = _utc_now, copy_file: launchd.CopyFile = shutil.copyfile,
            environ: Mapping[str, str] = os.environ, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> Tuple[int, str]:
    """Copy the app out of ``source_root``, write the task XML and register it; returns (exit code, message).

    Nothing is written when USERNAME is missing or the app copy fails."""
    user_id = _user_id(environ)
    if user_id is None:
        return (2, "USERNAME is not set in the environment; cannot register a Task Scheduler logon trigger")
    sch = schtasks_exe(environ)
    # Before touching the app directory: unlike POSIX rename(), Windows refuses to rename/replace a directory
    # that is a running process's current working directory -- and that directory is exactly what the task's
    # WorkingDirectory points a running instance at. Stopping it first is what makes a reinstall over an
    # already running daemon (the normal upgrade path) succeed instead of failing every time with a copy error.
    _stop_daemon(runner, sch, state_dir, launchd.exit_timeout_for(timeouts), sleep=sleep, clock=clock)
    commit = commit_of(source_root) or launchd.UNKNOWN_COMMIT
    try:
        app_root = launchd.copy_app(source_root, state_dir, commit=commit, copied_at=now(), copy_file=copy_file)
    except (launchd.AppCopyError, OSError) as e:
        return (1, "app copy failed: %s" % (e if isinstance(e, launchd.AppCopyError)
                                            else "%s (%s)" % (type(e).__name__, e)))
    xml = build_task_xml(python_exe, app_root, config_path, user_id, state_dir)
    task_xml_path = os.path.join(state_dir, TASK_XML_FILENAME)
    _write_task_xml(task_xml_path, xml)
    rc, _, err = runner([sch, "/Create", "/TN", TASK_NAME, "/XML", task_xml_path, "/F"], CREATE_TIMEOUT_S)
    if rc != 0:
        return (1, "schtasks /Create failed: %s" % err.strip())
    rc, _, err = runner([sch, "/Run", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    if rc != 0:
        return (1, "schtasks /Run failed: %s" % err.strip())
    lock_path = os.path.join(state_dir, daemon.LOCK_FILENAME)
    started = _poll_until(lambda: not _lock_is_free(lock_path), DAEMON_START_TIMEOUT_S, sleep=sleep, clock=clock)
    if not started:
        return (1, "task registered but the daemon did not start; see %s" % LOG_DIR_DEFAULT)
    rc, _, err = runner([sch, "/Query", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    if rc != 0:
        return (1, "schtasks /Query does not show %s after /Create: %s" % (TASK_NAME, err.strip()))
    return (0, "installed %s (%s); app copy %s at %s" % (TASK_NAME, task_xml_path, app_root, commit))


def uninstall(*, runner: Runner, state_dir: str, timeouts: Timeouts,
             environ: Mapping[str, str] = os.environ, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> Tuple[int, str]:
    """End and delete the task (if registered), then remove the app copy and task.xml. Returns (0, "not
    installed") when the task was never registered -- that is not an error."""
    sch = schtasks_exe(environ)
    rc, _, _ = runner([sch, "/Query", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    if rc == 0:
        _stop_daemon(runner, sch, state_dir, launchd.exit_timeout_for(timeouts), sleep=sleep, clock=clock)
        rc2, _, err2 = runner([sch, "/Delete", "/TN", TASK_NAME, "/F"], QUERY_TIMEOUT_S)
        if rc2 != 0:
            return (1, "schtasks /Delete failed: %s" % err2.strip())
    problem = launchd.remove_app(state_dir)
    with contextlib.suppress(OSError):
        os.remove(os.path.join(state_dir, TASK_XML_FILENAME))
    if problem is not None:
        return (1, "uninstalled %s; %s" % (TASK_NAME, problem))
    return (0, "not installed" if rc != 0 else "uninstalled %s" % TASK_NAME)
