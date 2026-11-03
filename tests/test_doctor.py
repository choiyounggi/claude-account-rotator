import unittest

from rotator import doctor
from rotator.doctor import FAIL, OK, WARN, Check


class TestParseVersion(unittest.TestCase):
    def test_dotted_triple(self):
        self.assertEqual(doctor.parse_version("1.4.241"), (1, 4, 241))
        self.assertEqual(doctor.parse_version(" 1.10.0\n"), (1, 10, 0))

    def test_rejects_other_shapes(self):
        for text in ("", "1.4", "1.4.241-beta", "v1.4.241", "1.4.x", None, 1.4):
            self.assertIsNone(doctor.parse_version(text), text)


class TestOrcaVersion(unittest.TestCase):
    def test_reads_short_version(self):
        seen = []
        v = doctor.orca_version(lambda path: seen.append(path) or {"CFBundleShortVersionString": "1.4.241"})
        self.assertEqual(v, "1.4.241")
        self.assertEqual(seen, ["/Applications/Orca.app/Contents/Info.plist"])

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
        self.assertEqual(doctor.check_orca("1.5.0-beta").level, FAIL)


class TestOtherChecks(unittest.TestCase):
    def test_system(self):
        self.assertEqual(doctor.check_system("Darwin").level, OK)
        self.assertEqual(doctor.check_system("Linux").level, FAIL)
        self.assertEqual(doctor.check_system("").level, FAIL)

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
