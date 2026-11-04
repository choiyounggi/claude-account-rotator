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
from datetime import datetime, timezone
from typing import Callable, List, Mapping, Optional, Tuple
from xml.sax.saxutils import escape

from rotator import launchd

TASK_NAME = "claude-account-rotator"
TASK_XML_FILENAME = "task.xml"
QUERY_TIMEOUT_S = 30.0
CREATE_TIMEOUT_S = 30.0

Runner = Callable[[List[str], float], Tuple[int, str, str]]


def schtasks_exe(environ: Mapping[str, str] = os.environ) -> str:
    # ntpath, not os.path: this is always a Windows path for the target machine, even when built in a test
    # running on macOS/Linux.
    return ntpath.join(environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")


def run_schtasks(argv: List[str], timeout_s: float) -> Tuple[int, str, str]:
    """The real schtasks runner."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    return (p.returncode, p.stdout, p.stderr)


def _user_id(environ: Mapping[str, str]) -> Optional[str]:
    """``DOMAIN\\user`` from the environment, or bare ``user`` when USERDOMAIN is missing; None when even
    USERNAME is missing."""
    username = environ.get("USERNAME")
    if not username:
        return None
    domain = environ.get("USERDOMAIN")
    return "%s\\%s" % (domain, username) if domain else username


def build_task_xml(python_exe: str, app_root: str, config_path: str, user_id: str, *,
                   exists: Callable[[str], bool] = os.path.isfile) -> str:
    """Task Scheduler schema 1.2 XML: a LogonTrigger for ``user_id``, an InteractiveToken/LeastPrivilege
    principal, and an Exec action that runs the daemon with no console window.

    Every value is escaped; the result must be written as UTF-16 with a BOM (``_write_task_xml``), which is
    what the declared ``encoding="UTF-16"`` promises and what ``schtasks /XML`` reliably accepts.
    """
    pythonw = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
    command = pythonw if exists(pythonw) else python_exe
    entry = os.path.join(app_root, "bin", "rotator")
    args = subprocess.list2cmdline([entry, "daemon", "--config", config_path])
    uid, cmd, arglist, cwd = escape(user_id), escape(command), escape(args), escape(app_root)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <Triggers>\n'
        '    <LogonTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        '      <UserId>%(uid)s</UserId>\n'
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


def install(*, runner: Runner, python_exe: str, source_root: str, state_dir: str, config_path: str,
            commit_of: Callable[[str], Optional[str]] = launchd.source_commit,
            now: Callable[[], datetime] = _utc_now, copy_file: launchd.CopyFile = shutil.copyfile,
            environ: Mapping[str, str] = os.environ) -> Tuple[int, str]:
    """Copy the app out of ``source_root``, write the task XML and register it; returns (exit code, message).

    Nothing is written when USERNAME is missing or the app copy fails."""
    user_id = _user_id(environ)
    if user_id is None:
        return (2, "USERNAME is not set in the environment; cannot register a Task Scheduler logon trigger")
    sch = schtasks_exe(environ)
    # Before touching the app directory: unlike POSIX rename(), Windows refuses to rename/replace a directory
    # that is a running process's current working directory -- and that directory is exactly what the task's
    # WorkingDirectory points a running instance at. Ending it first is what makes a reinstall over an already
    # running daemon (the normal upgrade path) succeed instead of failing every time with a copy error.
    runner([sch, "/End", "/TN", TASK_NAME], QUERY_TIMEOUT_S)  # ignore rc: fine whether or not it was running
    commit = commit_of(source_root) or launchd.UNKNOWN_COMMIT
    try:
        app_root = launchd.copy_app(source_root, state_dir, commit=commit, copied_at=now(), copy_file=copy_file)
    except (launchd.AppCopyError, OSError) as e:
        return (1, "app copy failed: %s" % (e if isinstance(e, launchd.AppCopyError)
                                            else "%s (%s)" % (type(e).__name__, e)))
    xml = build_task_xml(python_exe, app_root, config_path, user_id)
    task_xml_path = os.path.join(state_dir, TASK_XML_FILENAME)
    _write_task_xml(task_xml_path, xml)
    rc, _, err = runner([sch, "/Create", "/TN", TASK_NAME, "/XML", task_xml_path, "/F"], CREATE_TIMEOUT_S)
    if rc != 0:
        return (1, "schtasks /Create failed: %s" % err.strip())
    rc, _, err = runner([sch, "/Run", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    if rc != 0:
        return (1, "schtasks /Run failed: %s" % err.strip())
    rc, _, err = runner([sch, "/Query", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    if rc != 0:
        return (1, "schtasks /Query does not show %s after /Create: %s" % (TASK_NAME, err.strip()))
    return (0, "installed %s (%s); app copy %s at %s" % (TASK_NAME, task_xml_path, app_root, commit))


def uninstall(*, runner: Runner, state_dir: str, environ: Mapping[str, str] = os.environ) -> Tuple[int, str]:
    """End and delete the task (if registered), then remove the app copy and task.xml. Returns (0, "not
    installed") when the task was never registered -- that is not an error."""
    sch = schtasks_exe(environ)
    rc, _, _ = runner([sch, "/Query", "/TN", TASK_NAME], QUERY_TIMEOUT_S)
    if rc == 0:
        runner([sch, "/End", "/TN", TASK_NAME], QUERY_TIMEOUT_S)  # ignore rc: it may already be stopped
        rc2, _, err2 = runner([sch, "/Delete", "/TN", TASK_NAME, "/F"], QUERY_TIMEOUT_S)
        if rc2 != 0:
            return (1, "schtasks /Delete failed: %s" % err2.strip())
    problem = launchd.remove_app(state_dir)
    with contextlib.suppress(OSError):
        os.remove(os.path.join(state_dir, TASK_XML_FILENAME))
    if problem is not None:
        return (1, "uninstalled %s; %s" % (TASK_NAME, problem))
    return (0, "not installed" if rc != 0 else "uninstalled %s" % TASK_NAME)
