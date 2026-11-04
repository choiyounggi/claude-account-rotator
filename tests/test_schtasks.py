import os
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List
from unittest import mock

from rotator import daemon, launchd, schtasks
from rotator.switcher import Timeouts

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, ".claude", "tmp", "test-schtasks")
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
COPIED_AT = datetime(2026, 11, 2, 3, 4, 5, tzinfo=timezone.utc)


def scratch(name):
    path = os.path.join(BASE, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def make_source(root):
    """A fake checkout: the runnable app plus everything install must leave behind."""
    files = {"bin/rotator": "#!/usr/bin/python3\nprint('entry')\n", "rotator/__init__.py": "",
             "rotator/cli.py": "CLI = 1\n", ".git/HEAD": "ref: refs/heads/main\n"}
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return root


class FakeRunner:
    """schtasks.exe by verb (argv[1]). A list answers one call per item (the last repeats).

    When ``lock_path`` is set, ``/Run`` grabs the real daemon lock there (never released), simulating the
    daemon install() just launched taking it -- what install()'s post-/Run poll (1c) waits for.
    """

    def __init__(self, script=None, lock_path=None):
        self.script = script or {}
        self.calls: List[tuple] = []
        self.lock_path = lock_path
        self._held_lock = None

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        verb = argv[1]
        if verb == "/Run" and self.lock_path is not None and self._held_lock is None:
            self._held_lock = daemon.try_lock(self.lock_path)
        v = self.script.get(verb, (0, "", ""))
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        return v

    def verbs(self):
        return [argv[1] for argv, _ in self.calls]


class FakeClock:
    """An injectable clock/sleep pair for install()/uninstall()'s polling: ``sleep`` advances the clock instead
    of actually blocking, so a poll that waits out a full timeout still runs instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: List[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


# --- schtasks_exe ------------------------------------------------------------------------------------------


class TestSchtasksExe(unittest.TestCase):
    def test_default_system_root(self):
        self.assertEqual(schtasks.schtasks_exe({}), r"C:\Windows\System32\schtasks.exe")

    def test_custom_system_root(self):
        self.assertEqual(schtasks.schtasks_exe({"SystemRoot": r"D:\WINNT"}), r"D:\WINNT\System32\schtasks.exe")

    def test_empty_system_root_is_not_replaced_by_the_default(self):
        # environ.get's default only applies when the key is absent, not when it is an empty string.
        self.assertEqual(schtasks.schtasks_exe({"SystemRoot": ""}), r"System32\schtasks.exe")


# --- build_task_xml ------------------------------------------------------------------------------------------


class TestBuildTaskXml(unittest.TestCase):
    PYTHON = r"C:\Program Files\Python39\python.exe"
    APP_ROOT = r"C:\Users\Test & User\AppData\Local\claude-account-rotator\app"
    STATE_DIR = r"C:\Users\Test & User\AppData\Local\claude-account-rotator"
    CONFIG = r"C:\Users\Test & User\AppData\Roaming\claude-account-rotator\config.json"
    USER_ID = r"EXAMPLE-DOMAIN\test&user"

    def build(self, exists):
        xml = schtasks.build_task_xml(self.PYTHON, self.APP_ROOT, self.CONFIG, self.USER_ID, self.STATE_DIR,
                                      exists=exists)
        self.assertIn('encoding="UTF-16"', xml.splitlines()[0])
        return ET.fromstring(xml.encode("utf-16"))

    def test_trigger_and_principal(self):
        root = self.build(exists=lambda p: False)
        trigger = root.find(NS + "Triggers/" + NS + "LogonTrigger")
        self.assertEqual(trigger.find(NS + "UserId").text, self.USER_ID)
        principal = root.find(NS + "Principals/" + NS + "Principal")
        self.assertEqual(principal.find(NS + "UserId").text, self.USER_ID)
        self.assertEqual(principal.find(NS + "LogonType").text, "InteractiveToken")
        self.assertEqual(principal.find(NS + "RunLevel").text, "LeastPrivilege")

    def test_trigger_repeats_every_5_minutes_indefinitely(self):
        # A daemon that merely exits (config error, Orca gate, crash) is not a launch failure, so
        # RestartOnFailure alone never revives it; Repetition is what gets it back within 5 minutes.
        root = self.build(exists=lambda p: False)
        rep = root.find(NS + "Triggers/" + NS + "LogonTrigger/" + NS + "Repetition")
        self.assertIsNotNone(rep)
        self.assertEqual(rep.find(NS + "Interval").text, "PT5M")
        self.assertIsNone(rep.find(NS + "Duration"))  # absent Duration: repeats indefinitely
        self.assertEqual(rep.find(NS + "StopAtDurationEnd").text, "false")

    def test_settings(self):
        root = self.build(exists=lambda p: False)
        settings = root.find(NS + "Settings")
        self.assertEqual(settings.find(NS + "MultipleInstancesPolicy").text, "IgnoreNew")
        self.assertEqual(settings.find(NS + "DisallowStartIfOnBatteries").text, "false")
        self.assertEqual(settings.find(NS + "StopIfGoingOnBatteries").text, "false")
        self.assertEqual(settings.find(NS + "ExecutionTimeLimit").text, "PT0S")
        self.assertEqual(settings.find(NS + "StartWhenAvailable").text, "true")
        restart = settings.find(NS + "RestartOnFailure")
        self.assertEqual(restart.find(NS + "Interval").text, "PT1M")
        self.assertEqual(restart.find(NS + "Count").text, "999")

    def test_action_falls_back_to_python_exe_without_pythonw(self):
        root = self.build(exists=lambda p: False)
        exe = root.find(NS + "Actions/" + NS + "Exec")
        self.assertEqual(exe.find(NS + "Command").text, '"%s"' % self.PYTHON)
        self.assertEqual(exe.find(NS + "WorkingDirectory").text, self.STATE_DIR)
        args = exe.find(NS + "Arguments").text
        self.assertIn(os.path.join(self.APP_ROOT, "bin", "rotator"), args)
        self.assertIn("daemon", args)
        self.assertIn("--config", args)
        self.assertIn(self.CONFIG, args)

    def test_action_prefers_pythonw_next_to_python_exe(self):
        pythonw = os.path.join(os.path.dirname(self.PYTHON), "pythonw.exe")
        root = self.build(exists=lambda p: p == pythonw)
        exe = root.find(NS + "Actions/" + NS + "Exec")
        self.assertEqual(exe.find(NS + "Command").text, '"%s"' % pythonw)

    def test_ampersand_and_spaces_survive_the_round_trip(self):
        # "Test & User" and the spaces must read back exactly: escape() must have been applied, and nothing more.
        root = self.build(exists=lambda p: False)
        self.assertEqual(root.find(NS + "Principals/" + NS + "Principal/" + NS + "UserId").text, self.USER_ID)
        self.assertEqual(root.find(NS + "Actions/" + NS + "Exec/" + NS + "WorkingDirectory").text, self.STATE_DIR)


# --- query_installed / install_stale --------------------------------------------------------------------


class TestQueryInstalled(unittest.TestCase):
    def test_true_on_rc0(self):
        self.assertIs(schtasks.query_installed(FakeRunner()), True)

    def test_false_on_nonzero_rc(self):
        self.assertIs(schtasks.query_installed(FakeRunner({"/Query": (1, "", "not found")})), False)


class TestInstallStale(unittest.TestCase):
    def setUp(self):
        self.state = scratch("stale-state-" + self._testMethodName)
        self.source = scratch("stale-src-" + self._testMethodName)

    def version(self, text):
        os.makedirs(os.path.join(self.state, "app"), exist_ok=True)
        with open(os.path.join(self.state, "app", "VERSION"), "w", encoding="utf-8") as f:
            f.write(text)

    def stale(self, commit, runner=None):
        return schtasks.install_stale(runner or FakeRunner(), self.state, self.source, commit_of=lambda r: commit)

    def test_differs(self):
        self.version("abc1234\n2026-11-02T03:04:05Z\n")
        self.assertIs(self.stale("def5678"), True)

    def test_same_or_prefix(self):
        self.version("abc1234\n2026-11-02T03:04:05Z\n")
        self.assertIs(self.stale("abc1234"), False)

    def test_not_installed(self):
        self.version("abc1234\n")
        self.assertIs(self.stale("def5678", FakeRunner({"/Query": (1, "", "")})), False)

    def test_errors_are_silent(self):
        self.version("abc1234\n")

        def boom(root):
            raise OSError("git gone")
        self.assertIs(schtasks.install_stale(FakeRunner(), self.state, self.source, commit_of=boom), False)


# --- install -------------------------------------------------------------------------------------------


class InstallBase(unittest.TestCase):
    def setUp(self):
        self.source = make_source(scratch("src-" + self._testMethodName))
        self.state = scratch("state-" + self._testMethodName)
        self.app = os.path.join(self.state, "app")
        self.task_xml = os.path.join(self.state, schtasks.TASK_XML_FILENAME)
        self.commit = "abc1234"
        self.clock = FakeClock()

    def lock_path(self, state_dir=None):
        return os.path.join(state_dir or self.state, daemon.LOCK_FILENAME)

    def install(self, runner, **kw):
        if isinstance(runner, FakeRunner) and runner.lock_path is None:
            runner.lock_path = self.lock_path(kw.get("state_dir"))
        args = dict(runner=runner, python_exe=r"C:\Python39\python.exe", source_root=self.source,
                    state_dir=self.state, config_path=r"C:\Users\test\config.json",
                    commit_of=lambda root: self.commit, now=lambda: COPIED_AT, timeouts=Timeouts(),
                    environ={"USERDOMAIN": "EXAMPLE", "USERNAME": "test"},
                    sleep=self.clock.sleep, clock=self.clock.clock)
        args.update(kw)
        return schtasks.install(**args)

    def uninstall(self, runner, **kw):
        args = dict(runner=runner, state_dir=self.state, timeouts=Timeouts(),
                    sleep=self.clock.sleep, clock=self.clock.clock)
        args.update(kw)
        return schtasks.uninstall(**args)


class TestInstall(InstallBase):
    def test_success_copies_writes_xml_and_registers(self):
        r = FakeRunner()
        rc, msg = self.install(r)
        self.assertEqual(rc, 0)
        self.assertIn(schtasks.TASK_NAME, msg)
        self.assertIn(self.app, msg)
        self.assertIn(self.commit, msg)
        self.assertEqual(r.verbs(), ["/End", "/Create", "/Run", "/Query"])
        self.assertTrue(os.path.isfile(os.path.join(self.app, "bin", "rotator")))
        self.assertTrue(os.path.isfile(self.task_xml))
        with open(self.task_xml, "rb") as f:
            data = f.read()
        root = ET.fromstring(data)
        self.assertEqual(root.find(NS + "Principals/" + NS + "Principal/" + NS + "UserId").text,
                         r"EXAMPLE\test")
        create_argv = r.calls[1][0]
        self.assertIn(self.task_xml, create_argv)
        self.assertIn("/F", create_argv)

    def test_username_only_when_no_domain(self):
        r = FakeRunner()
        self.install(r, environ={"USERNAME": "solo"})
        with open(self.task_xml, "rb") as f:
            root = ET.fromstring(f.read())
        self.assertEqual(root.find(NS + "Principals/" + NS + "Principal/" + NS + "UserId").text, "solo")

    def test_missing_username_fails_with_exit_2(self):
        r = FakeRunner()
        rc, msg = self.install(r, environ={})
        self.assertEqual(rc, 2)
        self.assertIn("USERNAME", msg)
        self.assertEqual(r.calls, [])
        self.assertFalse(os.path.isdir(self.app))

    def test_create_failure_reports_stderr(self):
        r = FakeRunner({"/Create": (1, "", "ERROR: Access is denied.")})
        rc, msg = self.install(r)
        self.assertEqual(rc, 1)
        self.assertIn("Access is denied", msg)
        self.assertNotIn("/Run", r.verbs())

    def test_run_failure_reports_stderr(self):
        r = FakeRunner({"/Run": (1, "", "ERROR: cannot start")})
        rc, msg = self.install(r)
        self.assertEqual(rc, 1)
        self.assertIn("cannot start", msg)
        self.assertNotIn("/Query", r.verbs())

    def test_query_failure_reports_stderr(self):
        r = FakeRunner({"/Query": (1, "", "ERROR: not found right after create")})
        rc, msg = self.install(r)
        self.assertEqual(rc, 1)
        self.assertIn("not found right after create", msg)

    def test_end_rc_is_ignored(self):
        r = FakeRunner({"/End": (1, "", "not running")})
        rc, _ = self.install(r)
        self.assertEqual(rc, 0)

    def test_end_runs_before_the_app_copy(self):
        # Unlike POSIX rename(), Windows refuses to rename/replace a directory that is a running process's
        # current working directory -- and WorkingDirectory in the task XML is exactly the app copy's path.
        # /End must stop the previous instance before copy_app touches that directory, or a reinstall while
        # the daemon is still running (the normal upgrade path) would fail every time.
        events = []

        def runner(argv, timeout):
            events.append(("run", argv[1]))
            if argv[1] == "/Run":
                daemon.try_lock(self.lock_path())  # simulate the daemon starting and taking its lock
            return (0, "", "")

        def copy_file(src, dst):
            events.append(("copy", None))
            shutil.copyfile(src, dst)

        rc, _ = self.install(runner, copy_file=copy_file)
        self.assertEqual(rc, 0)
        first_end = events.index(("run", "/End"))
        first_copy = events.index(("copy", None))
        self.assertLess(first_end, first_copy, events)

    def test_app_copy_failure_leaves_no_xml(self):
        rc, msg = self.install(FakeRunner(), source_root=scratch("empty-" + self._testMethodName))
        self.assertEqual(rc, 1)
        self.assertIn("app copy failed", msg)
        self.assertFalse(os.path.isfile(self.task_xml))


# --- uninstall -------------------------------------------------------------------------------------------


class TestUninstall(InstallBase):
    def test_uninstalls_when_installed(self):
        self.install(FakeRunner())
        r = FakeRunner()
        rc, msg = self.uninstall(r)
        self.assertEqual(rc, 0)
        self.assertIn(schtasks.TASK_NAME, msg)
        self.assertEqual(r.verbs(), ["/Query", "/End", "/Delete"])
        self.assertFalse(os.path.isdir(self.app))
        self.assertFalse(os.path.isfile(self.task_xml))

    def test_not_installed_is_not_an_error(self):
        r = FakeRunner({"/Query": (1, "", "")})
        rc, msg = self.uninstall(r)
        self.assertEqual(rc, 0)
        self.assertEqual(msg, "not installed")
        self.assertEqual(r.verbs(), ["/Query"])

    def test_delete_failure_reports_stderr(self):
        self.install(FakeRunner())
        r = FakeRunner({"/Delete": (1, "", "ERROR: Access is denied.")})
        rc, msg = self.uninstall(r)
        self.assertEqual(rc, 1)
        self.assertIn("Access is denied", msg)
        self.assertTrue(os.path.isdir(self.app))  # the app copy is only removed after a successful delete

    def test_uninstall_without_ever_installing(self):
        r = FakeRunner({"/Query": (1, "", "")})
        rc, msg = self.uninstall(r, state_dir=scratch("never-" + self._testMethodName))
        self.assertEqual((rc, msg), (0, "not installed"))


# --- install's post-/Run poll (1c) ----------------------------------------------------------------------


class TestInstallWaitsForDaemonStart(InstallBase):
    def test_reports_error_when_the_daemon_never_takes_the_lock(self):
        def runner(argv, timeout):  # a plain runner, never grabs the lock -- nothing ever "starts"
            return (0, "", "")
        rc, msg = self.install(runner)
        self.assertEqual(rc, 1)
        self.assertIn("did not start", msg)
        self.assertIn(schtasks.LOG_DIR_DEFAULT, msg)
        self.assertEqual(self.clock.t, schtasks.DAEMON_START_TIMEOUT_S)  # waited the full budget
        # The probe must not have leaked the lock itself: it is still free afterward.
        lk = daemon.try_lock(self.lock_path())
        self.assertIsNotNone(lk)
        lk.release()

    def test_succeeds_as_soon_as_the_daemon_takes_the_lock(self):
        rc, _ = self.install(FakeRunner())  # auto-wired: /Run grabs the lock, as a real daemon would
        self.assertEqual(rc, 0)
        self.assertEqual(self.clock.t, 0.0)  # held on the very first check: no polling needed


# --- graceful stop before /End (finding 2) --------------------------------------------------------------


class TestGracefulStop(InstallBase):
    def stop_request_path(self):
        return os.path.join(self.state, daemon.STOP_REQUEST_FILENAME)

    def test_install_writes_the_request_and_removes_it_after_end(self):
        seen = {}

        def runner(argv, timeout):
            if argv[1] == "/End":
                seen["present_at_end"] = os.path.isfile(self.stop_request_path())
            if argv[1] == "/Run":
                daemon.try_lock(self.lock_path())
            return (0, "", "")

        rc, _ = self.install(runner)
        self.assertEqual(rc, 0)
        self.assertTrue(seen["present_at_end"])
        self.assertFalse(os.path.isfile(self.stop_request_path()))

    def test_install_proceeds_as_soon_as_an_already_running_daemon_frees_the_lock(self):
        lk = daemon.try_lock(self.lock_path())
        self.assertIsNotNone(lk)
        released_after = 3
        real_sleep = self.clock.sleep

        def sleep(seconds):
            real_sleep(seconds)
            if len(self.clock.sleeps) == released_after:
                lk.release()
        self.clock.sleep = sleep

        def runner(argv, timeout):
            if argv[1] == "/Run":
                daemon.try_lock(self.lock_path())
            return (0, "", "")

        rc, _ = self.install(runner)
        self.assertEqual(rc, 0)
        full_budget = launchd.exit_timeout_for(Timeouts())
        self.assertLess(self.clock.t, full_budget)  # stopped well short of the full wait, not a fixed sleep

    def test_uninstall_falls_back_to_end_after_the_full_wait(self):
        lk = daemon.try_lock(self.lock_path())
        self.assertIsNotNone(lk)
        self.addCleanup(lambda: lk.release())
        order = []

        def runner(argv, timeout):
            order.append(argv[1])
            return (0, "", "")

        timeouts = Timeouts()
        rc, msg = self.uninstall(runner, timeouts=timeouts)
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["/Query", "/End", "/Delete"])  # /End still runs, as a fallback
        self.assertEqual(self.clock.t, launchd.exit_timeout_for(timeouts))  # waited the full budget first
        self.assertFalse(os.path.isfile(self.stop_request_path()))  # removed whether or not the wait succeeded

    def test_uninstall_skips_the_request_when_nothing_is_installed(self):
        r = FakeRunner({"/Query": (1, "", "")})
        rc, msg = self.uninstall(r)
        self.assertEqual((rc, msg), (0, "not installed"))
        self.assertEqual(r.verbs(), ["/Query"])  # no stop-request write, no /End
        self.assertFalse(os.path.isfile(self.stop_request_path()))


# --- run_schtasks (finding 7 and the Windows half of finding 5) ------------------------------------------


class TestRunSchtasks(unittest.TestCase):
    def test_oserror_is_caught(self):
        with mock.patch.object(schtasks.subprocess, "run", side_effect=OSError("no such file")):
            rc, out, err = schtasks.run_schtasks(["schtasks.exe", "/Query"], 5.0)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no such file", err)

    def test_timeout_still_returns_124(self):
        with mock.patch.object(schtasks.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="schtasks", timeout=5.0)):
            rc, out, err = schtasks.run_schtasks(["schtasks.exe"], 5.0)
        self.assertEqual((rc, out, err), (124, "", "timeout"))

    def test_none_output_is_coerced_to_empty_string(self):
        cp = mock.Mock(returncode=0, stdout=None, stderr=None)
        with mock.patch.object(schtasks.subprocess, "run", return_value=cp):
            rc, out, err = schtasks.run_schtasks(["schtasks.exe"], 5.0)
        self.assertEqual((rc, out, err), (0, "", ""))
        err.strip()  # must not raise AttributeError on None

    def test_creationflags_applied_only_when_windows(self):
        captured = []

        def fake_run(argv, **kw):
            captured.append(kw.get("creationflags"))
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(schtasks.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            with mock.patch.object(schtasks, "IS_WINDOWS", True):
                schtasks.run_schtasks(["schtasks.exe"], 5.0)
            with mock.patch.object(schtasks, "IS_WINDOWS", False):
                schtasks.run_schtasks(["schtasks.exe"], 5.0)
        self.assertEqual(captured, [0x08000000, 0])


if __name__ == "__main__":
    unittest.main()
