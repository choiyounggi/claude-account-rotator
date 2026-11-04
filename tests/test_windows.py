"""rotator.windows: Windows-only primitives, exercised on any OS through their injectable probes."""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from rotator import windows


class FakeMsvcrt:
    """A stand-in for the msvcrt module, for tests that run on any OS."""

    def __init__(self, *, osfhandle=1234, kbhit_sequence=(), getwch_sequence=(), lock_fails=False):
        self._osfhandle = osfhandle
        self._kbhit_sequence = list(kbhit_sequence)
        self._getwch_sequence = list(getwch_sequence)
        self.lock_calls = []
        self._lock_fails = lock_fails
        self.LK_NBLCK = "LK_NBLCK"
        self.LK_UNLCK = "LK_UNLCK"

    def get_osfhandle(self, fd):
        return self._osfhandle

    def kbhit(self):
        return bool(self._kbhit_sequence) and self._kbhit_sequence.pop(0)

    def getwch(self):
        return self._getwch_sequence.pop(0)

    def locking(self, fd, mode, nbytes):
        self.lock_calls.append((fd, mode, nbytes))
        if self._lock_fails:
            raise OSError("locked by another process")


class FakeKernel32:
    def __init__(self, *, console_mode=0, set_ok=True):
        self.console_mode = console_mode
        self._set_ok = set_ok
        self.set_calls = []

    def GetConsoleMode(self, handle, mode_ptr):
        mode_ptr._obj.value = self.console_mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_calls.append(mode)
        return 1 if self._set_ok else 0


def _counter(start=0.0, step=0.01):
    state = {"t": start}

    def _tick():
        state["t"] += step
        return state["t"]

    return _tick


class PathDefaultsTests(unittest.TestCase):
    def test_orca_user_data_root_uses_override(self):
        root = windows.orca_user_data_root(
            environ={"ORCA_USER_DATA_PATH": r"D:\custom", "APPDATA": r"C:\Users\x\AppData\Roaming"})
        self.assertEqual(root, r"D:\custom")

    def test_orca_user_data_root_defaults_to_appdata(self):
        appdata = r"C:\Users\x\AppData\Roaming"
        root = windows.orca_user_data_root(environ={"APPDATA": appdata})
        self.assertEqual(root, os.path.join(appdata, "orca"))

    def test_rotator_config_dir_under_appdata(self):
        appdata = r"C:\Users\x\AppData\Roaming"
        d = windows.rotator_config_dir(environ={"APPDATA": appdata})
        self.assertEqual(d, os.path.join(appdata, "claude-account-rotator"))

    def test_rotator_log_dir_under_localappdata(self):
        local = r"C:\Users\x\AppData\Local"
        d = windows.rotator_log_dir(environ={"LOCALAPPDATA": local})
        self.assertEqual(d, os.path.join(local, "claude-account-rotator", "Logs"))

    def test_missing_env_vars_do_not_raise(self):
        self.assertEqual(windows.orca_user_data_root(environ={}), os.path.join("", "orca"))


class FiletimeTests(unittest.TestCase):
    def test_known_value_round_trips(self):
        # 2021-01-01T00:00:00Z, converted to 100 ns ticks since the FILETIME epoch (1601-01-01).
        target = datetime(2021, 1, 1, tzinfo=timezone.utc)
        ticks = int((target.timestamp() + windows._FILETIME_EPOCH_OFFSET_S) * 1e7)
        low, high = ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF
        self.assertEqual(windows.filetime_to_datetime(low, high), target)

    def test_non_positive_ticks_is_none(self):
        self.assertIsNone(windows.filetime_to_datetime(0, 0))


class ProcStartTests(unittest.TestCase):
    def test_none_probe_result_is_none(self):
        self.assertIsNone(windows.proc_start(123, probe=lambda pid: None))

    def test_probe_result_converted(self):
        target = datetime(2022, 6, 1, tzinfo=timezone.utc)
        ticks = int((target.timestamp() + windows._FILETIME_EPOCH_OFFSET_S) * 1e7)
        low, high = ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF
        self.assertEqual(windows.proc_start(123, probe=lambda pid: (low, high)), target)


class ReadProcTests(unittest.TestCase):
    def test_probe_path_becomes_exec_path_with_empty_argv_env(self):
        self.assertEqual(windows.read_proc(123, probe=lambda pid: "claude.exe"), ("claude.exe", (), {}))

    def test_missing_image_path_is_empty_string(self):
        self.assertEqual(windows.read_proc(123, probe=lambda pid: None), ("", (), {}))


class EnableVtModeTests(unittest.TestCase):
    def test_success_sets_the_virtual_terminal_bit(self):
        k32 = FakeKernel32(console_mode=0x1)
        with mock.patch.object(windows, "msvcrt", FakeMsvcrt()):
            self.assertTrue(windows.enable_vt_mode(1, kernel32=k32))
        self.assertEqual(k32.set_calls, [0x1 | windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING])

    def test_set_console_mode_failure_is_false(self):
        k32 = FakeKernel32(set_ok=False)
        with mock.patch.object(windows, "msvcrt", FakeMsvcrt()):
            self.assertFalse(windows.enable_vt_mode(1, kernel32=k32))

    def test_bad_fd_is_false(self):
        fake = FakeMsvcrt()
        fake.get_osfhandle = mock.Mock(side_effect=OSError("bad fd"))
        with mock.patch.object(windows, "msvcrt", fake):
            self.assertFalse(windows.enable_vt_mode(99, kernel32=FakeKernel32()))


class ReadKeyMsvcrtTests(unittest.TestCase):
    def test_returns_the_typed_character(self):
        fake = FakeMsvcrt(kbhit_sequence=[True], getwch_sequence=["q"])
        got = windows.read_key_msvcrt(1.0, kbhit=fake.kbhit, getwch=fake.getwch,
                                      sleep=lambda s: None, monotonic=_counter())
        self.assertEqual(got, "q")

    def test_function_key_lead_byte_is_discarded_as_no_key(self):
        fake = FakeMsvcrt(kbhit_sequence=[True], getwch_sequence=["\x00", "H"])  # e.g. an arrow key
        got = windows.read_key_msvcrt(1.0, kbhit=fake.kbhit, getwch=fake.getwch,
                                      sleep=lambda s: None, monotonic=_counter())
        self.assertIsNone(got)

    def test_timeout_without_a_key_is_none(self):
        fake = FakeMsvcrt(kbhit_sequence=[False, False, False])
        slept = []
        got = windows.read_key_msvcrt(0.05, kbhit=fake.kbhit, getwch=fake.getwch,
                                      sleep=slept.append, monotonic=_counter(step=0.02))
        self.assertIsNone(got)
        self.assertTrue(slept)


class FileLockBytesTests(unittest.TestCase):
    def test_try_lock_then_unlock_round_trip(self):
        fake = FakeMsvcrt()
        with mock.patch.object(windows, "msvcrt", fake), \
                mock.patch("os.lseek", return_value=0):
            self.assertTrue(windows.try_lock_bytes(5))
            windows.unlock_bytes(5)
        self.assertEqual([c[1] for c in fake.lock_calls], ["LK_NBLCK", "LK_UNLCK"])

    def test_try_lock_already_held_is_false(self):
        fake = FakeMsvcrt(lock_fails=True)
        with mock.patch.object(windows, "msvcrt", fake), mock.patch("os.lseek", return_value=0):
            self.assertFalse(windows.try_lock_bytes(5))


if __name__ == "__main__":
    unittest.main()
