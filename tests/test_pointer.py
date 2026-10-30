import os
import stat
import tempfile
import unittest

from rotator.models import Profile
from rotator.pointer import POINTER_FILENAME, read_selected_host, set_selected_host

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, ".claude", "tmp", "test-pointer")
ACC1 = "00000000-0000-0000-0000-000000000001"
ACC2 = "00000000-0000-0000-0000-000000000002"
KC = "Claude Code-credentials-test"


class _PointerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(BASE, exist_ok=True)

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(dir=BASE)
        self.root = self._td.name
        self.addCleanup(self._td.cleanup)

    def _make_profile(self, acc, *, profile_json=True, root=None):
        root = self.root if root is None else root
        os.makedirs(os.path.join(root, acc, "home"))
        if profile_json:
            with open(os.path.join(root, acc, "profile.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        return Profile(account_id=acc, home=os.path.join(root, acc, "home"), keychain_service=KC)

    def _write_pointer(self, data, root=None):
        root = self.root if root is None else root
        with open(os.path.join(root, POINTER_FILENAME), "wb") as f:
            f.write(data)

    def _pointer_bytes(self, root=None):
        root = self.root if root is None else root
        with open(os.path.join(root, POINTER_FILENAME), "rb") as f:
            return f.read()

    def _listing(self, root=None):
        return sorted(os.listdir(self.root if root is None else root))

    def _assert_no_temp(self, root=None):
        leftovers = [n for n in self._listing(root) if n.startswith(".selected-host.")]
        self.assertEqual(leftovers, [])


class TestReadSelectedHost(_PointerCase):
    def test_returns_exact_text(self):
        self._write_pointer(b"/Users/user/x/home")
        self.assertEqual(read_selected_host(self.root), "/Users/user/x/home")

    def test_trailing_newline_kept(self):
        self._write_pointer(b"/Users/user/x/home\n")
        self.assertEqual(read_selected_host(self.root), "/Users/user/x/home\n")

    def test_empty_file_is_empty_string(self):
        self._write_pointer(b"")
        value = read_selected_host(self.root)
        self.assertIsNotNone(value)
        self.assertEqual(value, "")

    def test_missing_is_none(self):
        self.assertIsNone(read_selected_host(self.root))

    def test_non_utf8_is_none(self):
        self._write_pointer(b"\xff")
        self.assertIsNone(read_selected_host(self.root))

    def test_injected_read_text_used(self):
        got = read_selected_host(self.root, read_text=lambda p: "seen:" + os.path.basename(p))
        self.assertEqual(got, "seen:selected-host")

    def test_injected_read_text_oserror_is_none(self):
        def boom(path):
            raise OSError("unreadable")

        self.assertIsNone(read_selected_host(self.root, read_text=boom))


class TestSetSelectedHost(_PointerCase):
    def test_writes_exact_bytes_mode_and_returns_previous(self):
        self._write_pointer(b"/old")
        target = self._make_profile(ACC1)
        previous = set_selected_host(target, self.root)
        self.assertEqual(previous, "/old")
        expected = self.root.encode() + b"/" + ACC1.encode() + b"/home"
        self.assertEqual(self._pointer_bytes(), expected)
        self.assertEqual(self._pointer_bytes(), target.home.encode("utf-8"))
        self.assertFalse(self._pointer_bytes().endswith(b"\n"))
        mode = stat.S_IMODE(os.stat(os.path.join(self.root, POINTER_FILENAME)).st_mode)
        self.assertEqual(mode, 0o600)
        self._assert_no_temp()

    def test_no_previous_returns_none(self):
        target = self._make_profile(ACC1)
        self.assertIsNone(set_selected_host(target, self.root))
        self.assertEqual(self._pointer_bytes(), target.home.encode("utf-8"))

    def test_overwrites_existing_pointer_between_accounts(self):
        first = self._make_profile(ACC1)
        second = self._make_profile(ACC2)
        set_selected_host(first, self.root)
        previous = set_selected_host(second, self.root)
        self.assertEqual(previous, first.home)
        self.assertEqual(self._pointer_bytes(), second.home.encode("utf-8"))
        self._assert_no_temp()

    def test_home_written_verbatim_not_normalised(self):
        self._make_profile(ACC1)
        target = Profile(account_id=ACC1, home=os.path.join(self.root, ".", ACC1, "home"), keychain_service=KC)
        set_selected_host(target, self.root)
        self.assertEqual(self._pointer_bytes(), target.home.encode("utf-8"))
        self.assertIn(b"/./", self._pointer_bytes())

    def test_root_with_space(self):
        sub = os.path.join(self.root, "Profile Root")
        os.makedirs(sub)
        target = self._make_profile(ACC1, root=sub)
        set_selected_host(target, sub)
        self.assertEqual(self._pointer_bytes(sub), target.home.encode("utf-8"))
        mode = stat.S_IMODE(os.stat(os.path.join(sub, POINTER_FILENAME)).st_mode)
        self.assertEqual(mode, 0o600)
        self._assert_no_temp(sub)

    def test_replace_failure_keeps_old_and_no_temp(self):
        self._write_pointer(b"/old")
        target = self._make_profile(ACC1)

        def bad_replace(src, dst):
            raise OSError("disk")

        with self.assertRaises(OSError):
            set_selected_host(target, self.root, replace=bad_replace)
        self.assertEqual(self._pointer_bytes(), b"/old")
        self._assert_no_temp()

    def test_fsync_failure_keeps_old_and_no_temp(self):
        self._write_pointer(b"/old")
        target = self._make_profile(ACC1)

        def bad_fsync(fd):
            raise OSError("disk")

        with self.assertRaises(OSError):
            set_selected_host(target, self.root, fsync=bad_fsync)
        self.assertEqual(self._pointer_bytes(), b"/old")
        self._assert_no_temp()

    def test_previous_returned_verbatim_with_newline(self):
        self._write_pointer(b"/Users/user/x/home\n")
        target = self._make_profile(ACC1)
        self.assertEqual(set_selected_host(target, self.root), "/Users/user/x/home\n")

    def test_temp_file_created_in_pointer_directory(self):
        target = self._make_profile(ACC1)
        moves = []

        def recording_replace(src, dst):
            moves.append((src, dst))
            os.replace(src, dst)

        set_selected_host(target, self.root, replace=recording_replace)
        self.assertEqual(len(moves), 1)
        src, dst = moves[0]
        self.assertEqual(os.path.dirname(src), self.root)
        self.assertTrue(os.path.basename(src).startswith(".selected-host."))
        self.assertEqual(dst, os.path.join(self.root, POINTER_FILENAME))
        self._assert_no_temp()

    def test_directory_fsync_failure_is_ignored(self):
        target = self._make_profile(ACC1)
        calls = []

        def file_ok_dir_fails(fd):
            calls.append(fd)
            if len(calls) > 1:
                raise OSError("dir fsync unsupported")

        set_selected_host(target, self.root, fsync=file_ok_dir_fails)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self._pointer_bytes(), target.home.encode("utf-8"))

    # --- validation: ValueError and nothing created ---

    def _assert_rejected(self, target, root=None):
        before = self._listing()
        with self.assertRaises(ValueError) as cm:
            set_selected_host(target, self.root if root is None else root)
        self.assertEqual(self._listing(), before)
        self.assertNotIn(self.root, str(cm.exception))
        return cm.exception

    def test_relative_root_rejected(self):
        target = self._make_profile(ACC1)
        self._assert_rejected(target, root="rel/root")

    def test_missing_root_rejected(self):
        target = self._make_profile(ACC1)
        self._assert_rejected(target, root=os.path.join(self.root, "nope"))

    def test_account_id_dotdot_rejected(self):
        target = Profile(account_id="..", home=os.path.join(self.root, "..", "home"), keychain_service=KC)
        self._assert_rejected(target)

    def test_account_id_with_sep_rejected(self):
        os.makedirs(os.path.join(self.root, "a", "b", "home"))
        target = Profile(account_id="a/b", home=os.path.join(self.root, "a", "b", "home"), keychain_service=KC)
        self._assert_rejected(target)

    def test_account_id_with_sep_rejected_even_with_profile_json(self):
        os.makedirs(os.path.join(self.root, "a", "b", "home"))
        with open(os.path.join(self.root, "a", "b", "profile.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        target = Profile(account_id="a/b", home=os.path.join(self.root, "a", "b", "home"), keychain_service=KC)
        self._assert_rejected(target)

    def test_home_of_another_account_rejected(self):
        self._make_profile(ACC1)
        other = self._make_profile(ACC2)
        target = Profile(account_id=ACC1, home=other.home, keychain_service=KC)
        self._assert_rejected(target)

    def test_home_not_under_root_rejected(self):
        target = Profile(account_id=ACC1, home="/Users/user/elsewhere/home", keychain_service=KC)
        self._assert_rejected(target)

    def test_home_missing_rejected(self):
        os.makedirs(os.path.join(self.root, ACC1))
        with open(os.path.join(self.root, ACC1, "profile.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        target = Profile(account_id=ACC1, home=os.path.join(self.root, ACC1, "home"), keychain_service=KC)
        self._assert_rejected(target)

    def test_profile_json_missing_rejected(self):
        target = self._make_profile(ACC1, profile_json=False)
        self._assert_rejected(target)

    def test_home_symlinked_outside_root_rejected(self):
        os.makedirs(os.path.join(self.root, ACC1))
        with open(os.path.join(self.root, ACC1, "profile.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        outside = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(outside.cleanup)
        realhome = os.path.join(outside.name, "realhome")
        os.makedirs(realhome)
        os.symlink(realhome, os.path.join(self.root, ACC1, "home"))
        target = Profile(account_id=ACC1, home=os.path.join(self.root, ACC1, "home"), keychain_service=KC)
        self._assert_rejected(target)
        self.assertEqual(os.listdir(outside.name), ["realhome"])


if __name__ == "__main__":
    unittest.main()
