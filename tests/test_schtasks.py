import os
import shutil
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List
from unittest import mock

from rotator import launchd, schtasks

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
    """schtasks.exe by verb (argv[1]). A list answers one call per item (the last repeats)."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: List[tuple] = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        verb = argv[1]
        v = self.script.get(verb, (0, "", ""))
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        return v

    def verbs(self):
        return [argv[1] for argv, _ in self.calls]


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
    CONFIG = r"C:\Users\Test & User\AppData\Roaming\claude-account-rotator\config.json"
    USER_ID = r"EXAMPLE-DOMAIN\test&user"

    def build(self, exists):
        xml = schtasks.build_task_xml(self.PYTHON, self.APP_ROOT, self.CONFIG, self.USER_ID, exists=exists)
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
        self.assertEqual(exe.find(NS + "Command").text, self.PYTHON)
        self.assertEqual(exe.find(NS + "WorkingDirectory").text, self.APP_ROOT)
        args = exe.find(NS + "Arguments").text
        self.assertIn(os.path.join(self.APP_ROOT, "bin", "rotator"), args)
        self.assertIn("daemon", args)
        self.assertIn("--config", args)
        self.assertIn(self.CONFIG, args)

    def test_action_prefers_pythonw_next_to_python_exe(self):
        pythonw = os.path.join(os.path.dirname(self.PYTHON), "pythonw.exe")
        root = self.build(exists=lambda p: p == pythonw)
        exe = root.find(NS + "Actions/" + NS + "Exec")
        self.assertEqual(exe.find(NS + "Command").text, pythonw)

    def test_ampersand_and_spaces_survive_the_round_trip(self):
        # "Test & User" and the spaces must read back exactly: escape() must have been applied, and nothing more.
        root = self.build(exists=lambda p: False)
        self.assertEqual(root.find(NS + "Principals/" + NS + "Principal/" + NS + "UserId").text, self.USER_ID)
        self.assertIn(self.APP_ROOT, root.find(NS + "Actions/" + NS + "Exec/" + NS + "WorkingDirectory").text)


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

    def install(self, runner, **kw):
        args = dict(runner=runner, python_exe=r"C:\Python39\python.exe", source_root=self.source,
                    state_dir=self.state, config_path=r"C:\Users\test\config.json",
                    commit_of=lambda root: self.commit, now=lambda: COPIED_AT,
                    environ={"USERDOMAIN": "EXAMPLE", "USERNAME": "test"})
        args.update(kw)
        return schtasks.install(**args)


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
        rc, msg = schtasks.uninstall(runner=r, state_dir=self.state)
        self.assertEqual(rc, 0)
        self.assertIn(schtasks.TASK_NAME, msg)
        self.assertEqual(r.verbs(), ["/Query", "/End", "/Delete"])
        self.assertFalse(os.path.isdir(self.app))
        self.assertFalse(os.path.isfile(self.task_xml))

    def test_not_installed_is_not_an_error(self):
        r = FakeRunner({"/Query": (1, "", "")})
        rc, msg = schtasks.uninstall(runner=r, state_dir=self.state)
        self.assertEqual(rc, 0)
        self.assertEqual(msg, "not installed")
        self.assertEqual(r.verbs(), ["/Query"])

    def test_delete_failure_reports_stderr(self):
        self.install(FakeRunner())
        r = FakeRunner({"/Delete": (1, "", "ERROR: Access is denied.")})
        rc, msg = schtasks.uninstall(runner=r, state_dir=self.state)
        self.assertEqual(rc, 1)
        self.assertIn("Access is denied", msg)
        self.assertTrue(os.path.isdir(self.app))  # the app copy is only removed after a successful delete

    def test_uninstall_without_ever_installing(self):
        r = FakeRunner({"/Query": (1, "", "")})
        rc, msg = schtasks.uninstall(runner=r, state_dir=scratch("never-" + self._testMethodName))
        self.assertEqual((rc, msg), (0, "not installed"))


if __name__ == "__main__":
    unittest.main()
