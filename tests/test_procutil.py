"""rotator.procutil.pid_alive: the one cross-platform liveness check, both branches on any OS."""
import os
import sys
import unittest

from rotator import procutil


class PidAliveGuardsTests(unittest.TestCase):
    def test_zero_and_negative_pid_are_never_alive(self):
        self.assertIs(procutil.pid_alive(0), False)
        self.assertIs(procutil.pid_alive(-1), False)

    def test_bool_pid_is_never_alive(self):
        self.assertIs(procutil.pid_alive(True), False)  # bool is an int subclass; must still be refused


@unittest.skipIf(sys.platform == "win32",
                 "is_windows=False forces the real POSIX os.kill(pid, 0) branch; on a real Windows process "
                 "signal 0 is CTRL_C_EVENT, not a liveness probe, so this must never actually run there")
class PidAlivePosixTests(unittest.TestCase):
    def test_the_current_process_is_alive(self):
        self.assertIs(procutil.pid_alive(os.getpid(), is_windows=False), True)

    def test_an_unlikely_pid_is_not_alive(self):
        self.assertIs(procutil.pid_alive(2 ** 30 - 1, is_windows=False), False)


class PidAliveWindowsBranchTests(unittest.TestCase):
    """Exercises the Windows branch's dispatch and error handling without a real Windows kernel."""

    def test_probe_true_is_alive(self):
        self.assertIs(procutil.pid_alive(123, is_windows=True, windows_probe=lambda pid: True), True)

    def test_probe_false_is_not_alive(self):
        self.assertIs(procutil.pid_alive(123, is_windows=True, windows_probe=lambda pid: False), False)

    def test_probe_receives_the_pid(self):
        seen = []
        procutil.pid_alive(777, is_windows=True, windows_probe=lambda pid: seen.append(pid) or True)
        self.assertEqual(seen, [777])

    def test_probe_oserror_is_not_alive(self):
        def raising(pid):
            raise OSError("access denied")
        self.assertIs(procutil.pid_alive(123, is_windows=True, windows_probe=raising), False)

    def test_windows_branch_never_calls_os_kill(self):
        calls = []
        real_kill = os.kill
        os.kill = lambda *a, **k: calls.append(a) or real_kill_unreachable()  # pragma: no cover
        try:
            procutil.pid_alive(os.getpid(), is_windows=True, windows_probe=lambda pid: True)
        finally:
            os.kill = real_kill
        self.assertEqual(calls, [])


def real_kill_unreachable():
    raise AssertionError("os.kill must never be called on the Windows branch")


if __name__ == "__main__":
    unittest.main()
