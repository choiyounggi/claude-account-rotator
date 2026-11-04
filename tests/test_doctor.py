import os
import subprocess
import unittest
from unittest import mock

from rotator import doctor
from rotator.doctor import FAIL, OK, WARN, Check


class TestParseVersion(unittest.TestCase):
    def test_dotted_triple(self):
        self.assertEqual(doctor.parse_version("1.4.241"), (1, 4, 241))
        self.assertEqual(doctor.parse_version(" 1.10.0\n"), (1, 10, 0))

    def test_ignores_a_suffix(self):
        self.assertEqual(doctor.parse_version("1.4.250-beta.1"), (1, 4, 250))
        self.assertEqual(doctor.parse_version("1.4.250 (build 7)"), (1, 4, 250))

    def test_rejects_other_shapes(self):
        for text in ("", "1.4", "v1.4.241", "1.4.x", "1.4.241.1", None, 1.4):
            self.assertIsNone(doctor.parse_version(text), text)


class TestOrcaVersion(unittest.TestCase):
    def test_reads_short_version(self):
        seen = []
        v = doctor.orca_version(lambda path: seen.append(path) or {"CFBundleShortVersionString": "1.4.241"})
        self.assertEqual(v, "1.4.241")
        self.assertEqual(seen, [os.path.join("/Applications/Orca.app", "Contents", "Info.plist")])

    def test_missing_or_unreadable_is_none(self):
        def missing(path):
            raise FileNotFoundError(path)
        self.assertIsNone(doctor.orca_version(missing))
        self.assertIsNone(doctor.orca_version(lambda path: {}))
        self.assertIsNone(doctor.orca_version(lambda path: {"CFBundleShortVersionString": 1}))


class TestCheckOrca(unittest.TestCase):
    def test_minimum_is_folder_accounts_release(self):
        self.assertEqual(doctor.ORCA_MIN_VERSION, (1, 4, 224))

    def test_boundary(self):
        self.assertEqual(doctor.check_orca("1.4.223").level, FAIL)
        self.assertIn("too old", doctor.check_orca("1.4.223").detail)
        self.assertEqual(doctor.check_orca("1.4.224").level, OK)
        self.assertEqual(doctor.check_orca("1.4.241").level, OK)

    def test_compares_numbers_not_text(self):
        self.assertEqual(doctor.check_orca("1.10.0").level, OK)  # "1.10.0" < "1.4.224" as text
        self.assertEqual(doctor.check_orca("1.4.30").level, FAIL)  # "1.4.30" > "1.4.224" as text

    def test_missing_or_unreadable_fails(self):
        self.assertIn("not found", doctor.check_orca(None).detail)
        self.assertEqual(doctor.check_orca(None).level, FAIL)
        self.assertEqual(doctor.check_orca("beta").level, FAIL)
        self.assertEqual(doctor.check_orca("1.5.0-beta").level, OK)
        self.assertEqual(doctor.check_orca("1.4.223-beta").level, FAIL)


class TestOrcaVersionWindows(unittest.TestCase):
    def test_no_cli_path_is_none(self):
        self.assertIsNone(doctor.orca_version_windows(None))

    def test_reads_stdout_from_the_version_flag(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="1.4.241\n"))
        v = doctor.orca_version_windows(r"C:\orca.exe", run=run)
        self.assertEqual(v, "1.4.241")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [r"C:\orca.exe", "--version"])

    def test_nonzero_exit_is_none(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1, stdout=""))
        self.assertIsNone(doctor.orca_version_windows(r"C:\orca.exe", run=run))

    def test_run_failure_is_none(self):
        run = mock.Mock(side_effect=OSError("not found"))
        self.assertIsNone(doctor.orca_version_windows(r"C:\orca.exe", run=run))

    def test_creationflags_applied_only_when_windows(self):
        # cmd.exe would otherwise flash a console under pythonw.exe at every daemon start.
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="1.4.241\n"))
        with mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            with mock.patch.object(doctor, "IS_WINDOWS", True):
                doctor.orca_version_windows(r"C:\orca.exe", run=run)
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
            with mock.patch.object(doctor, "IS_WINDOWS", False):
                doctor.orca_version_windows(r"C:\orca.exe", run=run)
            self.assertEqual(run.call_args.kwargs["creationflags"], 0)


class TestOtherChecks(unittest.TestCase):
    def test_system(self):
        self.assertEqual(doctor.check_system("Darwin").level, OK)
        self.assertEqual(doctor.check_system("Linux").level, FAIL)
        self.assertEqual(doctor.check_system("").level, FAIL)

    def test_system_windows_is_ok(self):
        c = doctor.check_system("Windows")
        self.assertEqual(c.level, OK)
        self.assertIn("Windows", c.detail)

    def test_orca_where_customizes_the_not_found_message(self):
        c = doctor.check_orca(None, where="orca.exe on PATH")
        self.assertEqual(c.level, FAIL)
        self.assertIn("orca.exe on PATH", c.detail)

    def test_agent_unsupported_warns_regardless_of_installed_or_stale(self):
        for installed, stale in ((False, False), (True, False), (True, True)):
            c = doctor.check_agent(installed, stale, supported=False)
            self.assertEqual(c.level, WARN)
            self.assertIn("Windows", c.detail)

    def test_gate_where_flows_into_the_orca_reason(self):
        reason = doctor.gate("Windows", None, where="orca.exe on PATH")
        self.assertIn("orca.exe on PATH", reason)
        self.assertIsNone(doctor.gate("Windows", "1.4.241"))

    def test_python_boundary(self):
        self.assertEqual(doctor.check_python((3, 8, 18)).level, FAIL)
        self.assertEqual(doctor.check_python((3, 9, 0)).level, OK)
        self.assertEqual(doctor.check_python((3, 12, 1)).level, OK)

    def test_orca_cli(self):
        self.assertEqual(doctor.check_orca_cli("/x/orca", True).level, OK)
        self.assertEqual(doctor.check_orca_cli("/x/orca", False).level, FAIL)

    def test_accounts_never_fail(self):
        self.assertEqual(doctor.check_accounts(0, "/r").level, WARN)
        self.assertEqual(doctor.check_accounts(1, "/r").level, WARN)
        self.assertEqual(doctor.check_accounts(2, "/r").level, OK)

    def test_config(self):
        self.assertEqual(doctor.check_config(None).level, OK)
        bad = doctor.check_config("/x/config.json: unknown key 'old'")
        self.assertEqual(bad.level, FAIL)
        self.assertIn("unknown key", bad.detail)

    def test_agent(self):
        self.assertEqual(doctor.check_agent(False, False).level, WARN)
        self.assertEqual(doctor.check_agent(True, True).level, WARN)
        self.assertEqual(doctor.check_agent(True, False).level, OK)


class TestGate(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(doctor.gate("Darwin", "1.4.224"))

    def test_reasons(self):
        self.assertTrue(doctor.gate("Linux", "1.4.241").startswith("macOS: "))
        self.assertTrue(doctor.gate("Darwin", "1.4.200").startswith("Orca: "))
        self.assertTrue(doctor.gate("Darwin", None).startswith("Orca: "))


class TestRender(unittest.TestCase):
    def test_lines_and_exit_code(self):
        checks = [Check("macOS", OK, "26.5"), Check("Claude accounts", WARN, "1"), Check("Orca", FAIL, "old")]
        self.assertEqual(doctor.render(checks), ["[ok]   macOS            26.5",
                                                 "[warn] Claude accounts  1",
                                                 "[fail] Orca             old"])
        self.assertEqual(doctor.exit_code(checks), 1)
        self.assertEqual(doctor.exit_code(checks[:2]), 0)


if __name__ == "__main__":
    unittest.main()
