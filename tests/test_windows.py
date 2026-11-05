"""rotator.windows: Windows-only primitives, exercised on any OS through their injectable probes."""
import os
import struct
import subprocess
import sys
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


class FakeProcessMemory:
    """A stand-in for ReadProcessMemory: maps address -> bytes. Slicing a block shorter than the
    requested size (or missing entirely) simulates a short/failed read, without raising itself --
    that lets a test prove read_proc's own length check is what turns it into an OSError."""

    def __init__(self, blocks):
        self.blocks = blocks
        self.calls = []

    def read(self, handle, address, size):
        self.calls.append((handle, address, size))
        return self.blocks.get(address, b"")[:size]


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


class ParseEnvBlockTests(unittest.TestCase):
    def test_normal_block(self):
        data = "A=1\x00BB=two\x00\x00".encode("utf-16-le")
        self.assertEqual(windows.parse_env_block(data), {"A": "1", "BB": "two"})

    def test_empty_block(self):
        self.assertEqual(windows.parse_env_block(b""), {})

    def test_missing_final_terminator_still_parses(self):
        data = "A=1\x00B=2".encode("utf-16-le")  # no trailing double NUL
        self.assertEqual(windows.parse_env_block(data), {"A": "1", "B": "2"})

    def test_drive_relative_cwd_entries_are_skipped(self):
        data = "=C:=C:\\x\x00PATH=C:\\bin\x00\x00".encode("utf-16-le")
        self.assertEqual(windows.parse_env_block(data), {"PATH": "C:\\bin"})

    def test_non_ascii_values_round_trip(self):
        data = "GREETING=caf\u00e9 \u00e8\x00\x00".encode("utf-16-le")
        self.assertEqual(windows.parse_env_block(data), {"GREETING": "caf\u00e9 \u00e8"})


class ReadProcTests(unittest.TestCase):
    def test_missing_image_path_raises(self):
        with self.assertRaises(OSError):
            windows.read_proc(123, probe=lambda pid: None)

    def test_32bit_python_raises_without_opening_a_handle(self):
        def boom(pid):
            raise AssertionError("open_vm_handle must not be called for a 32-bit Python")

        with self.assertRaises(OSError):
            windows.read_proc(123, probe=lambda pid: "x.exe", pointer_size=lambda: 4,
                              open_vm_handle=boom)

    def test_open_vm_handle_failure_raises(self):
        def boom(pid):
            raise OSError("OpenProcess failed")

        with self.assertRaises(OSError):
            windows.read_proc(123, probe=lambda pid: "x.exe", open_vm_handle=boom)

    def test_wow64_target_raises_and_still_closes_handle(self):
        closed = []
        with self.assertRaises(OSError):
            windows.read_proc(123, probe=lambda pid: "x.exe", open_vm_handle=lambda pid: 7,
                              close_handle=closed.append, is_wow64=lambda h: True)
        self.assertEqual(closed, [7])

    def test_short_read_raises(self):
        peb, params = 0x1000, 0x2000
        mem = FakeProcessMemory({peb + 0x20: struct.pack("<Q", params), params + 0x70: b"short"})
        with self.assertRaises(OSError):
            windows.read_proc(123, probe=lambda pid: "x.exe", open_vm_handle=lambda pid: 7,
                              close_handle=lambda h: None, is_wow64=lambda h: False,
                              peb_address=lambda h: peb, read_memory=mem.read)

    def test_fills_argv_and_env_via_the_peb(self):
        peb, params, cmdline_buf, env_buf = 0x7FF000000000, 0x1230000, 0x1230100, 0x1240000
        command_line = 'node.exe "ARG ONE" x'
        cmdline_utf16 = command_line.encode("utf-16-le")
        env_block = "CLAUDE_CONFIG_DIR=C:\\fake\\home\x00ORCA_TERMINAL_HANDLE=term_fake123\x00\x00"
        env_bytes = env_block.encode("utf-16-le")
        mem = FakeProcessMemory({
            peb + 0x20: struct.pack("<Q", params),
            params + 0x70: struct.pack("<HH4xQ", len(cmdline_utf16), len(cmdline_utf16), cmdline_buf),
            params + 0x80: struct.pack("<Q", env_buf),
            params + 0x3F0: struct.pack("<Q", len(env_bytes)),
            cmdline_buf: cmdline_utf16,
            env_buf: env_bytes,
        })
        split_calls = []

        def split_command_line(cl):
            split_calls.append(cl)
            return ("node.exe", "ARG ONE", "x")

        exec_path, argv, env = windows.read_proc(
            123, probe=lambda pid: "node.exe", open_vm_handle=lambda pid: 99,
            close_handle=lambda h: None, is_wow64=lambda h: False, peb_address=lambda h: peb,
            read_memory=mem.read, split_command_line=split_command_line)

        self.assertEqual(exec_path, "node.exe")
        self.assertEqual(argv, ("node.exe", "ARG ONE", "x"))
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "C:\\fake\\home", "ORCA_TERMINAL_HANDLE": "term_fake123"})
        self.assertEqual(split_calls, [command_line])


class RegionMemory:
    """ReadProcessMemory over contiguous regions: a read returns what the region holding ``address`` has from
    there on (up to ``size``), and raises like the real call when no region holds it."""

    def __init__(self, regions):
        self.regions = regions  # start address -> bytes
        self.calls = []

    def read(self, handle, address, size):
        self.calls.append((address, size))
        for start, data in self.regions.items():
            if start <= address < start + len(data):
                return data[address - start:address - start + size]
        raise OSError("ReadProcessMemory failed at %#x" % address)


class ReadEnvironmentTests(unittest.TestCase):
    """The environment block is read up to its own end marker (two NUL characters), not EnvironmentSize: a
    process that changed its environment after start can leave that field smaller than the block."""

    PEB, PARAMS, ENV = 0x7FF000000000, 0x1230000, 0x1240FF0  # ENV sits 16 bytes before a page boundary

    def read(self, regions, env_size_field):
        mem = RegionMemory({
            self.PEB + 0x20: struct.pack("<Q", self.PARAMS),
            self.PARAMS + 0x70: struct.pack("<HH4xQ", 0, 0, 0),
            self.PARAMS + 0x80: struct.pack("<Q", self.ENV),
            self.PARAMS + 0x3F0: struct.pack("<Q", env_size_field),
            **regions,
        })
        _, _, env = windows.read_proc(
            123, probe=lambda pid: "x.exe", open_vm_handle=lambda pid: 9, close_handle=lambda h: None,
            is_wow64=lambda h: False, peb_address=lambda h: self.PEB, read_memory=mem.read,
            split_command_line=lambda cl: ())
        return env, mem

    def block(self, entries):
        return ("\x00".join(entries) + "\x00\x00").encode("utf-16-le")

    def test_reads_past_a_too_small_environment_size(self):
        data = self.block(["A=1", "ORCA_TERMINAL_HANDLE=term_fake123", "Z=last"])
        env, _ = self.read({self.ENV: data + b"garbage-after-the-block"}, env_size_field=8)
        self.assertEqual(env, {"A": "1", "ORCA_TERMINAL_HANDLE": "term_fake123", "Z": "last"})

    def test_a_block_crossing_a_page_boundary_is_read_whole(self):
        data = self.block(["K%d=%s" % (i, "v" * 40) for i in range(200)])
        env, mem = self.read({self.ENV: data}, env_size_field=0)
        self.assertEqual(len(env), 200)
        self.assertGreater(len(mem.calls), 4)  # read in page-sized pieces, not one oversized request

    def test_unterminated_block_ending_at_an_unreadable_page_keeps_what_was_read(self):
        data = "A=1\x00B=2\x00".encode("utf-16-le")  # no final empty entry; nothing readable after it
        env, _ = self.read({self.ENV: data}, env_size_field=0)
        self.assertEqual(env, {"A": "1", "B": "2"})

    def test_an_environment_with_no_variables_is_unreadable(self):
        # Every live Windows process has some (SystemRoot at least); none means a process still starting or
        # a failed read, and an empty env would make its session look like the default account's.
        for regions in ({self.ENV: b"\x00\x00\x00\x00"}, {}):
            with self.subTest(readable=bool(regions)):
                with self.assertRaises(OSError):
                    self.read(regions, env_size_field=0)

    def test_read_stops_at_the_cap(self):
        huge = ("X=" + "y" * 1000 + "\x00").encode("utf-16-le") * 1200  # ~2.4 MiB, never terminated
        env, mem = self.read({self.ENV: huge}, env_size_field=0)
        self.assertLessEqual(sum(size for _, size in mem.calls if _ >= self.ENV), windows._MAX_ENVIRONMENT_BYTES + 4096)
        self.assertIn("X", env)


class ProcessTableTests(unittest.TestCase):
    def test_builds_pid_to_parent_and_name_map(self):
        rows = [(100, 1, "explorer.exe"), (200, 100, "cmd.exe")]
        self.assertEqual(windows.process_table(snapshot_probe=lambda: rows),
                         {100: (1, "explorer.exe"), 200: (100, "cmd.exe")})

    def test_empty_snapshot_raises(self):
        with self.assertRaises(OSError):
            windows.process_table(snapshot_probe=lambda: [])

    def test_probe_failure_propagates(self):
        def boom():
            raise OSError("CreateToolhelp32Snapshot failed")

        with self.assertRaises(OSError):
            windows.process_table(snapshot_probe=boom)


@unittest.skipUnless(sys.platform == "win32", "exercises real Windows process introspection")
class RealWindowsReadProcTests(unittest.TestCase):
    """Only these run for real -- on CI's windows-latest runners. Everything above exercises the same
    code through injected probes, on any OS."""

    def setUp(self):
        # The child says when it is running: read_proc targets live sessions, not a process still starting,
        # whose parameters are not readable yet (the read right after Popen found an empty environment).
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; print('ready', flush=True); time.sleep(30)", "ARG ONE", "x"],
            env=dict(os.environ, ORCA_TERMINAL_HANDLE="term_fake123", CLAUDE_CONFIG_DIR=r"C:\fake\home"),
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.child.stdout.close)
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)
        self.assertEqual(self.child.stdout.readline().strip(), "ready")

    def test_read_proc_reads_real_argv_and_env(self):
        exec_path, argv, env = windows.read_proc(self.child.pid)
        self.assertEqual(argv[-2:], ("ARG ONE", "x"))
        # Names only on failure (values can hold CI secrets), plus where the block read went.
        self.assertEqual(env.get("ORCA_TERMINAL_HANDLE"), "term_fake123",
                         "read %d variables: %s; %s" % (len(env), sorted(env), self._env_probe()))

    def _env_probe(self):
        """Where read_proc's environment read went: the pointers it follows and the first page it reads."""
        h = windows._default_open_vm_handle(self.child.pid)
        try:
            peb = windows._default_peb_address(h)
            params = struct.unpack("<Q", windows._default_read_memory(h, peb + 0x20, 8))[0]
            env_ptr = struct.unpack("<Q", windows._default_read_memory(h, params + 0x80, 8))[0]
            size = struct.unpack("<Q", windows._default_read_memory(h, params + 0x3F0, 8))[0]
            try:
                first = windows._default_read_memory(h, env_ptr, 4096 - env_ptr % 4096)
                head = "first read %d bytes, head %s" % (len(first), first[:32].hex())
            except OSError as e:
                head = "first read failed: %s" % e
            return "peb=%#x params=%#x env=%#x EnvironmentSize=%d %s" % (peb, params, env_ptr, size, head)
        except Exception as e:  # diagnostics only
            return "probe failed: %r" % (e,)
        finally:
            windows._default_close_handle(h)
        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), r"C:\fake\home")

    def test_process_table_includes_child_with_parent_and_image_name(self):
        table = windows.process_table()
        self.assertEqual(table[self.child.pid], (os.getpid(), os.path.basename(sys.executable).lower()))

    def test_read_proc_on_exited_pid_raises(self):
        pid = self.child.pid
        self.child.kill()
        self.child.wait()
        with self.assertRaises(OSError):
            windows.read_proc(pid)


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
