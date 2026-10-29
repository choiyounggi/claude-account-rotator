import calendar
import importlib
import json
import builtins
import os
import re
import struct
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, timezone

import rotator.sessions
from rotator.sessions import (
    ENV_WHITELIST,
    TRANSCRIPT_TAIL_BYTES,
    _map_status,
    _parse_proc_start,
    _parse_started_at,
    _scan_registry,
    discover_sessions,
    discover_sessions_with_model,
    model_from_argv,
    parse_procargs,
    read_session_model,
    session_homes,
)
from rotator.models import Profile, Session


def procargs(argc, body):
    return struct.pack("=i", argc) + body


HEAD = b"/opt/claude\0\0\0claude\0"


class ParseProcargsTest(unittest.TestCase):
    def test_r7_example(self):
        buf = procargs(
            2,
            b"/opt/claude\0\0\0claude\0--resume a b\0CLAUDE_CONFIG_DIR=/x/App Support/h\0"
            b"A=b=c=d\0TOKEN=s\0executable_path=/opt/claude\0",
        )
        self.assertEqual(
            parse_procargs(buf),
            ("/opt/claude", ("claude", "--resume a b"), {"CLAUDE_CONFIG_DIR": "/x/App Support/h"}),
        )

    def test_value_with_equals_kept_whole(self):
        _, _, env = parse_procargs(procargs(1, HEAD + b"ORCA_WORKTREE_ID=a=b=c\0"))
        self.assertEqual(env, {"ORCA_WORKTREE_ID": "a=b=c"})

    def test_first_occurrence_wins(self):
        _, _, env = parse_procargs(procargs(1, HEAD + b"CLAUDE_CONFIG_DIR=/first\0CLAUDE_CONFIG_DIR=/second\0"))
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/first"})

    def test_secret_values_never_returned(self):
        result = parse_procargs(
            procargs(
                1,
                HEAD
                + b"CLAUDE_CODE_MESSAGING_TOKEN=sk-SECRET-0000\0ORCA_AGENT_HOOK_TOKEN=hook-SECRET-0000\0"
                b"ORCA_TERMINAL_HANDLE=term_00000000-0000-4000-8000-000000000001\0",
            )
        )
        env = result[2]
        self.assertTrue(set(env) <= set(ENV_WHITELIST))
        self.assertNotIn("SECRET", repr(result))
        self.assertIn("term_00000000", repr(result))

    def test_argc_zero(self):
        _, argv, env = parse_procargs(procargs(0, b"/bin/x\0\0CLAUDE_CONFIG_DIR=/h/a\0"))
        self.assertEqual(argv, ())
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/h/a"})

    def test_leading_empty_argv_element(self):
        _, argv, env = parse_procargs(
            procargs(2, b"/x/claude.exe\0" + b"\0\0" + b"\0" + b"--resume\0" + b"CLAUDE_CONFIG_DIR=/h/a\0")
        )
        self.assertEqual(argv, ("", "--resume"))
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/h/a"})
        self.assertTrue(all("=" not in a for a in argv))

    def test_aligned_exec_path_has_no_padding(self):
        _, argv, _ = parse_procargs(procargs(2, b"/bin/cl\0" + b"\0x\0"))
        self.assertEqual(argv, ("", "x"))

    def test_invalid_utf8_is_replaced(self):
        _, argv, _ = parse_procargs(procargs(1, b"/x\0a\xffb\0"))
        self.assertEqual(argv, ("a�b",))

    def test_too_short_rejected(self):
        with self.assertRaises(ValueError):
            parse_procargs(b"\x01\x00")

    def test_negative_argc_rejected(self):
        with self.assertRaises(ValueError):
            parse_procargs(procargs(-1, b"/x\0"))

    def test_unterminated_exec_path_rejected(self):
        with self.assertRaises(ValueError):
            parse_procargs(procargs(1, b"/no-terminator"))

    def test_argc_exceeds_strings_rejected(self):
        with self.assertRaises(ValueError):
            parse_procargs(procargs(3, b"/x\0\0a\0b\0"))

    def test_non_bytes_rejected(self):
        with self.assertRaises(TypeError):
            parse_procargs("str")


class ImportTimeTest(unittest.TestCase):
    def test_import_loads_no_libc_and_lists_nothing(self):
        with unittest.mock.patch("ctypes.CDLL", side_effect=AssertionError("CDLL at import")), unittest.mock.patch(
            "ctypes.util.find_library", side_effect=AssertionError("find_library at import")
        ), unittest.mock.patch("os.listdir", side_effect=AssertionError("listdir at import")):
            importlib.reload(rotator.sessions)
        self.assertIsNone(rotator.sessions._LIBC)


class FakeFS:
    def __init__(self, dirs, files):
        self.dirs = dirs
        self.files = files
        self.reads = []

    def list_dir(self, path):
        value = self.dirs.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FileNotFoundError(path)
        return list(value)

    def read_text(self, path):
        self.reads.append(path)
        value = self.files.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FileNotFoundError(path)
        return value


def entry(pid, **over):
    d = {
        "pid": pid,
        "sessionId": "00000000-0000-4000-8000-%012d" % pid,
        "cwd": "/work/proj",
        "startedAt": 1793161321336,
        "procStart": "Wed Oct 28 04:21:59 2026",
        "version": "2.1.296",
        "kind": "interactive",
        "entrypoint": "cli",
        "name": "proj",
        "nameSince": 1793161321336,
        "nameSource": "derived",
        "status": "idle",
        "statusUpdatedAt": 1793161321400,
        "updatedAt": 1793161321400,
        "messagingSocketPath": "/h/sock/%d.sock" % pid,
        "peerProtocol": 1,
        "peerFeatures": [],
        "pidDomain": "darwin",
    }
    d.update(over)
    return d


ACC1 = "/h/acc1/sessions"


class RegistryScanTest(unittest.TestCase):
    def scan_one_dir(self, files, homes=("/h/root", "/h/acc1")):
        """files: {name: text-or-Exception} placed under /h/acc1/sessions."""
        fs = FakeFS({ACC1: list(files)}, {ACC1 + "/" + n: v for n, v in files.items()})
        return _scan_registry(list(homes), fs.list_dir, fs.read_text), fs

    def assert_skip(self, name, text, reason):
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            cands, _ = self.scan_one_dir({name: text, "9.json": json.dumps(entry(9))})
        self.assertEqual([c.pid for c in cands], [9])
        self.assertIn("skip session %s/%s: %s" % (ACC1, name, reason), "\n".join(cm.output))

    def test_normal(self):
        cands, _ = self.scan_one_dir({"4242.json": json.dumps(entry(4242))})
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.pid, 4242)
        self.assertEqual(c.session_id, "00000000-0000-4000-8000-000000004242")
        self.assertEqual(c.cwd, "/work/proj")
        self.assertEqual(c.status_raw, "idle")
        self.assertEqual(c.proc_start_raw, "Wed Oct 28 04:21:59 2026")
        self.assertEqual(c.started_at_raw, 1793161321336)
        self.assertEqual(c.status_updated_at, 1793161321400)
        self.assertEqual(c.path, "/h/acc1/sessions/4242.json")
        self.assertEqual(c.order, 0)

    def test_file_name_filter_reads_only_pid_json_sorted(self):
        files = {
            "4242.json": json.dumps(entry(4242)),
            "4242.abcd.key": "never-read",
            "notes.txt": "never-read",
            "7.json": json.dumps(entry(7)),
            "7.json.tmp": "never-read",
        }
        cands, fs = self.scan_one_dir(files)
        self.assertEqual(fs.reads, ["/h/acc1/sessions/7.json", "/h/acc1/sessions/4242.json"])
        self.assertEqual([c.pid for c in cands], [7, 4242])

    def test_missing_dir_and_listing_error_contribute_nothing(self):
        fs = FakeFS({"/h/acc1/sessions": PermissionError(13, "denied")}, {})
        self.assertEqual(_scan_registry(["/h/root", "/h/acc1"], fs.list_dir, fs.read_text), [])
        self.assertEqual(fs.reads, [])

    def test_skip_unreadable(self):
        self.assert_skip("4.json", OSError(5, "io"), "unreadable")

    def test_skip_bad_json(self):
        self.assert_skip("4.json", "{", "bad_json")

    def test_skip_not_a_dict(self):
        self.assert_skip("4.json", "[]", "bad_shape")

    def test_skip_bool_pid(self):
        e = entry(4)
        e["pid"] = True
        self.assert_skip("4.json", json.dumps(e), "bad_shape")

    def test_skip_pid_differs_from_file_name(self):
        self.assert_skip("4.json", json.dumps(entry(5)), "bad_shape")

    def test_skip_non_str_session_id(self):
        self.assert_skip("4.json", json.dumps(entry(4, sessionId=7)), "bad_shape")

    def test_skip_headless_kind(self):
        self.assert_skip("4.json", json.dumps(entry(4, kind="headless")), "kind")

    def test_skip_missing_kind(self):
        e = entry(4)
        del e["kind"]
        self.assert_skip("4.json", json.dumps(e), "kind")

    def test_pid_bounds(self):
        files = {
            "2147483647.json": json.dumps(entry(2147483647)),
            "2147483648.json": json.dumps(entry(2147483648)),
            "0.json": json.dumps(entry(0)),
        }
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            cands, _ = self.scan_one_dir(files)
        self.assertEqual([c.pid for c in cands], [2147483647])
        out = "\n".join(cm.output)
        self.assertIn("skip session %s/2147483648.json: bad_shape" % ACC1, out)
        self.assertIn("skip session %s/0.json: bad_shape" % ACC1, out)

    def test_status_updated_at_non_int_or_missing_is_zero(self):
        e = entry(8)
        del e["statusUpdatedAt"]
        cands, _ = self.scan_one_dir({"7.json": json.dumps(entry(7, statusUpdatedAt=True)), "8.json": json.dumps(e)})
        self.assertEqual([(c.pid, c.status_updated_at) for c in cands], [(7, 0), (8, 0)])

    def test_order_follows_homes(self):
        fs = FakeFS(
            {"/h/root/sessions": ["5.json"], "/h/acc1/sessions": ["3.json"]},
            {"/h/root/sessions/5.json": json.dumps(entry(5)), "/h/acc1/sessions/3.json": json.dumps(entry(3))},
        )
        cands = _scan_registry(["/h/root", "/h/acc1"], fs.list_dir, fs.read_text)
        self.assertEqual([(c.pid, c.order) for c in cands], [(5, 0), (3, 1)])


class FieldParseTest(unittest.TestCase):
    def test_proc_start_parses_utc(self):
        self.assertEqual(_parse_proc_start("Wed Oct 28 04:21:59 2026"), 1793161319)

    def test_proc_start_single_digit_day_both_forms(self):
        expected = calendar.timegm((2026, 10, 3, 8, 3, 34))
        self.assertEqual(_parse_proc_start("Fri Oct  3 08:03:34 2026"), expected)
        self.assertEqual(_parse_proc_start("Fri Oct 03 08:03:34 2026"), expected)

    def test_proc_start_rejects(self):
        for bad in ("Fri Foo 03 08:03:34 2026", "Tue Feb 31 00:00:00 2026", "", None, 1793161319):
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_proc_start(bad))

    def test_started_at(self):
        self.assertEqual(
            _parse_started_at(1793161321336), datetime(2026, 10, 28, 4, 22, 1, 336000, tzinfo=timezone.utc)
        )
        self.assertEqual(_parse_started_at(0), datetime(1970, 1, 1, tzinfo=timezone.utc))
        for bad in ("soon", True, -1, 10**20):
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_started_at(bad))

    def test_map_status(self):
        self.assertEqual(_map_status("busy"), "busy")
        self.assertEqual(_map_status("shell"), "shell")
        self.assertEqual(_map_status("idle"), "idle")
        self.assertEqual(_map_status("compacting"), "unknown")
        self.assertEqual(_map_status(None), "unknown")
        self.assertEqual(_map_status(["idle"]), "unknown")


class FakeProc:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, pid):
        self.calls.append(pid)
        value = self.table[pid]
        if isinstance(value, Exception):
            raise value
        return value


class FakeStart:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, pid):
        self.calls.append(pid)
        value = self.table[pid]
        if isinstance(value, list):
            index = min(sum(1 for p in self.calls if p == pid) - 1, len(value) - 1)
            return value[index]
        return value


T0 = datetime(2026, 10, 28, 4, 21, 59, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 28, 9, 0, 0, tzinfo=timezone.utc)
HANDLE = "term_00000000-0000-4000-8000-000000000001"
CLAUDE = (
    "/x/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    ("claude",),
    {"CLAUDE_CONFIG_DIR": "/h/acc1", "ORCA_TERMINAL_HANDLE": HANDLE},
)
DEFAULT_HOME = os.path.normpath(os.path.expanduser("~/.claude"))
P4242 = "/h/acc1/sessions/4242.json"


def run(files, homes=("/h/root", "/h/acc1"), alive=None, proc=None, start=None, **kw):
    dirs = {}
    texts = {}
    pids = set()
    for path, value in files.items():
        dirs.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
        texts[path] = json.dumps(value) if isinstance(value, dict) else value
        pids.add(int(os.path.basename(path).split(".")[0]))
    fs = FakeFS(dirs, texts)
    alive = pids if alive is None else alive
    proc = FakeProc({p: CLAUDE for p in pids}) if proc is None else proc
    start = FakeStart({p: T0 for p in pids}) if start is None else start
    result = discover_sessions(
        list(homes),
        list_dir=fs.list_dir,
        read_text=fs.read_text,
        pid_alive=lambda p: p in alive,
        read_proc=proc,
        proc_start=start,
        **kw,
    )
    return result, fs, proc, start


def without(d, key):
    d = dict(d)
    del d[key]
    return d


class DiscoverTest(unittest.TestCase):
    def test_normal_session_is_built_from_registry_and_env(self):
        result, _, proc, start = run({P4242: entry(4242)})
        self.assertEqual(
            result,
            [
                Session(
                    pid=4242,
                    session_id="00000000-0000-4000-8000-000000004242",
                    cwd="/work/proj",
                    status="idle",
                    config_dir="/h/acc1",
                    argv=("claude",),
                    terminal_handle=HANDLE,
                    started_at=datetime(2026, 10, 28, 4, 22, 1, 336000, tzinfo=timezone.utc),
                )
            ],
        )
        self.assertEqual(proc.calls, [4242])
        self.assertEqual(start.calls, [4242, 4242])

    def test_sorted_by_pid(self):
        result, _, _, _ = run({"/h/acc1/sessions/50.json": entry(50), "/h/root/sessions/7.json": entry(7)})
        self.assertEqual([s.pid for s in result], [7, 50])

    def test_str_homes_rejected_and_empty_homes(self):
        with self.assertRaises(TypeError):
            discover_sessions("/h")
        self.assertEqual(discover_sessions([]), [])

    def test_session_homes(self):
        profile = Profile(
            account_id="00000000-0000-4000-8000-000000000001",
            home="/h/acc1",
            keychain_service="Claude Code-credentials-00000000",
        )
        self.assertEqual(session_homes([profile]), [DEFAULT_HOME, "/h/acc1"])
        self.assertEqual(session_homes([]), [DEFAULT_HOME])

    def test_dead_pid_skipped_without_touching_the_process(self):
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, proc, start = run({P4242: entry(4242)}, alive=set())
        self.assertEqual(result, [])
        self.assertEqual(proc.calls, [])
        self.assertEqual(start.calls, [])
        self.assertIn("skip session %s: dead" % P4242, "\n".join(cm.output))

    def test_unreadable_process_skipped(self):
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, _, _ = run({P4242: entry(4242)}, proc=FakeProc({4242: OSError(22, "Invalid argument")}))
        self.assertEqual(result, [])
        self.assertIn("skip session %s: unreadable" % P4242, "\n".join(cm.output))

    def test_malformed_procargs_skipped(self):
        result, _, _, _ = run({P4242: entry(4242)}, proc=lambda pid: parse_procargs(b"\x01"))
        self.assertEqual(result, [])

    def test_pid_reused_before_read(self):
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, proc, _ = run({P4242: entry(4242)}, start=FakeStart({4242: LATER}))
        self.assertEqual(result, [])
        self.assertEqual(proc.calls, [])
        self.assertIn("skip session %s: pid_reused" % P4242, "\n".join(cm.output))

    def test_start_within_one_second_kept(self):
        result, _, _, _ = run({P4242: entry(4242)}, start=FakeStart({4242: T0.replace(microsecond=900000)}))
        self.assertEqual([s.pid for s in result], [4242])

    def test_start_more_than_one_second_off_dropped(self):
        result, _, _, _ = run({P4242: entry(4242)}, start=FakeStart({4242: T0.replace(second=57)}))
        self.assertEqual(result, [])

    def test_live_start_unavailable_or_naive_is_unreadable(self):
        for live in (None, T0.replace(tzinfo=None)):
            with self.subTest(live=live), self.assertLogs("rotator.sessions", "DEBUG") as cm:
                result, _, proc, _ = run({P4242: entry(4242)}, start=FakeStart({4242: live}))
            self.assertEqual(result, [])
            self.assertEqual(proc.calls, [])
            self.assertIn("skip session %s: unreadable" % P4242, "\n".join(cm.output))

    def test_pid_reused_during_read_race(self):
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, proc, _ = run({P4242: entry(4242)}, start=FakeStart({4242: [T0, LATER]}))
        self.assertEqual(result, [])
        self.assertEqual(proc.calls, [4242])
        self.assertIn("skip session %s: pid_reused" % P4242, "\n".join(cm.output))

    def test_live_start_vanishes_after_read(self):
        result, _, proc, _ = run({P4242: entry(4242)}, start=FakeStart({4242: [T0, None]}))
        self.assertEqual(result, [])
        self.assertEqual(proc.calls, [4242])

    def test_name_fallback_without_proc_start(self):
        accepted = [
            ("/x/node_modules/@anthropic-ai/claude-code/bin/claude.exe", ("claude",)),
            ("/usr/local/bin/claude", ("claude",)),
            ("/usr/bin/node", ("node", "/x/@anthropic-ai/claude-code/cli.js")),
        ]
        rejected = [
            ("/usr/bin/python3", ("python3",)),
            ("/usr/bin/python3", ("python3", "/Users/u/.claude/hooks/x.py")),
            ("/usr/bin/python3", ("python3", "/w/claude-account-rotator/bin/rotator")),
        ]
        for exec_path, argv in accepted + rejected:
            for registry in (without(entry(4242), "procStart"), entry(4242, procStart="garbage")):
                with self.subTest(exec_path=exec_path, argv=argv, procStart=registry.get("procStart")):
                    proc = FakeProc({4242: (exec_path, argv, {"CLAUDE_CONFIG_DIR": "/h/acc1"})})
                    with self.assertLogs("rotator.sessions", "DEBUG") as cm:
                        result, _, _, start = run({P4242: registry, "/h/root/sessions/1.json": "{"}, proc=proc)
                    self.assertEqual(start.calls, [])
                    if (exec_path, argv) in accepted:
                        self.assertEqual([s.argv for s in result], [argv])
                    else:
                        self.assertEqual(result, [])
                        self.assertIn("skip session %s: not_claude" % P4242, "\n".join(cm.output))

    def test_duplicate_pid_newest_status_wins(self):
        files = {
            "/h/root/sessions/4242.json": entry(4242, statusUpdatedAt=100, status="idle"),
            P4242: entry(4242, statusUpdatedAt=200, status="busy"),
        }
        result, _, proc, _ = run(files)
        self.assertEqual([s.status for s in result], ["busy"])
        self.assertEqual(proc.calls, [4242])

    def test_duplicate_pid_tie_keeps_first_home(self):
        files = {
            "/h/root/sessions/4242.json": entry(4242, statusUpdatedAt=100, status="idle"),
            P4242: entry(4242, statusUpdatedAt=100, status="busy"),
        }
        result, _, _, _ = run(files)
        self.assertEqual([s.status for s in result], ["idle"])

    def test_stale_newer_duplicate_loses_to_matching_entry(self):
        files = {
            "/h/root/sessions/4242.json": entry(4242, statusUpdatedAt=100, status="idle"),
            P4242: entry(4242, statusUpdatedAt=900, status="busy", procStart="Wed Oct 28 23:00:00 2026"),
        }
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, _, _ = run(files)
        self.assertEqual([s.status for s in result], ["idle"])
        self.assertIn("skip session %s: pid_reused" % P4242, "\n".join(cm.output))

    def test_winner_without_proc_start_uses_sibling_anchor(self):
        files = {
            "/h/root/sessions/4242.json": entry(4242, statusUpdatedAt=100, status="idle"),
            P4242: without(entry(4242, statusUpdatedAt=900, status="busy"), "procStart"),
        }
        python = FakeProc({4242: ("/usr/bin/python3", ("python3",), {})})
        result, _, _, _ = run(files, proc=python, start=FakeStart({4242: [T0, T0]}))
        self.assertEqual([(s.status, s.argv) for s in result], [("busy", ("python3",))])

        python = FakeProc({4242: ("/usr/bin/python3", ("python3",), {})})
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, _, _ = run(files, proc=python, start=FakeStart({4242: [T0, LATER]}))
        self.assertEqual(result, [])
        self.assertIn("skip session %s: pid_reused" % P4242, "\n".join(cm.output))

    def discover_with_env(self, env):
        proc = FakeProc({4242: ("/usr/local/bin/claude", ("claude",), env)})
        return run({P4242: entry(4242)}, proc=proc)[0]

    def test_config_dir_default_when_env_lacks_it(self):
        for env in ({}, {"CLAUDE_CONFIG_DIR": ""}):
            with self.subTest(env=env):
                self.assertEqual([s.config_dir for s in self.discover_with_env(env)], [DEFAULT_HOME])

    def test_config_dir_with_space_returned_unchanged(self):
        home = "/Users/u/Library/Application Support/orca/claude-profiles/00000000-0000-4000-8000-000000000001/home"
        self.assertEqual([s.config_dir for s in self.discover_with_env({"CLAUDE_CONFIG_DIR": home})], [home])
        self.assertEqual([s.config_dir for s in self.discover_with_env({"CLAUDE_CONFIG_DIR": "/h/acc1/"})], ["/h/acc1/"])

    def test_relative_config_dir_skipped(self):
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result = self.discover_with_env({"CLAUDE_CONFIG_DIR": "rel/home"})
        self.assertEqual(result, [])
        self.assertIn("skip session %s: config_dir_relative" % P4242, "\n".join(cm.output))

    def test_empty_terminal_handle_is_none(self):
        result = self.discover_with_env({"CLAUDE_CONFIG_DIR": "/h/acc1", "ORCA_TERMINAL_HANDLE": ""})
        self.assertEqual([s.terminal_handle for s in result], [None])

    def test_status_mapping(self):
        cases = [(entry(4242, status="busy"), "busy"), (entry(4242, status="compacting"), "unknown"),
                 (without(entry(4242), "status"), "unknown")]
        for registry, expected in cases:
            with self.subTest(expected=expected, raw=registry.get("status")):
                result, _, _, _ = run({P4242: registry})
                self.assertEqual([s.status for s in result], [expected])

    def test_bad_started_at_keeps_session(self):
        result, _, _, _ = run({P4242: entry(4242, startedAt="soon")})
        self.assertEqual([(s.pid, s.started_at) for s in result], [(4242, None)])

    def test_invalid_session_fields_skipped(self):
        files = {P4242: entry(4242, cwd=""), "/h/acc1/sessions/9.json": entry(9)}
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, _, _ = run(files)
        self.assertEqual([s.pid for s in result], [9])
        self.assertIn("skip session %s: invalid" % P4242, "\n".join(cm.output))

    def test_argv_preserved(self):
        argv = ("claude", "--resume", "00000000-0000-4000-8000-000000000042")
        proc = FakeProc({4242: (CLAUDE[0], argv, CLAUDE[2])})
        result, _, _, _ = run({P4242: entry(4242)}, proc=proc)
        self.assertEqual([s.argv for s in result], [argv])

    def test_mixed_bag_never_raises(self):
        files = {
            "/h/acc1/sessions/1.json": "{",
            "/h/acc1/sessions/2.json": "[]",
            "/h/acc1/sessions/3.json": entry(3),
            "/h/acc1/sessions/4.json": entry(4),
            P4242: entry(4242),
        }
        proc = FakeProc({4: OSError(1, "Operation not permitted"), 4242: CLAUDE})
        result, _, _, _ = run(files, alive={4, 4242}, proc=proc)
        self.assertEqual([s.pid for s in result], [4242])


class SecretAbsenceTest(unittest.TestCase):
    def test_secret_env_never_in_result_or_log(self):
        buf = procargs(
            1,
            HEAD
            + b"CLAUDE_CODE_MESSAGING_TOKEN=sk-SECRET-0000\0ORCA_AGENT_HOOK_TOKEN=hook-SECRET-0000\0"
            b"CLAUDE_CONFIG_DIR=/h/acc1\0ORCA_TERMINAL_HANDLE=term_00000000-0000-4000-8000-000000000001\0",
        )
        files = {P4242: entry(4242), "/h/acc1/sessions/77.json": entry(77)}
        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            result, _, _, _ = run(files, alive={4242}, proc=lambda pid: parse_procargs(buf))
        log_text = "\n".join(cm.output)
        self.assertNotIn("SECRET", repr(result))
        self.assertIn("term_00000000", repr(result))
        self.assertNotIn("SECRET", log_text)
        self.assertIn("skip session", log_text)



REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_BASE = os.path.join(REPO, ".claude", "tmp", "test-sessions-model")


def a_line(model):
    return json.dumps({"type": "assistant",
                       "message": {"model": model, "content": [{"type": "text", "text": "SECRET-PROMPT-TEXT"}]}})


def u_line(text="SECRET-PROMPT-TEXT"):
    return json.dumps({"type": "user", "message": {"content": text}})


class ReadSessionModelTest(unittest.TestCase):
    cwd = "/work/my proj.v2"
    sid = "00000000-0000-4000-8000-000000000001"

    @classmethod
    def setUpClass(cls):
        os.makedirs(MODEL_BASE, exist_ok=True)

    def setUp(self):
        td = tempfile.TemporaryDirectory(dir=MODEL_BASE)
        self.addCleanup(td.cleanup)
        self.home = os.path.join(td.name, "home")
        os.makedirs(self.home)

    def write_bytes(self, data, sub=None):
        d = os.path.join(self.home, "projects", sub or re.sub("[^A-Za-z0-9]", "-", self.cwd))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, self.sid + ".jsonl")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def write(self, lines, sub=None):
        return self.write_bytes(("\n".join(lines) + "\n").encode("utf-8"), sub)

    def read(self, **kw):
        return read_session_model(self.home, self.cwd, self.sid, **kw)

    def test_model_change_last_wins(self):
        self.write([a_line("claude-opus-5-5"), u_line(), a_line("claude-fable-5-1")])
        self.assertEqual(self.read(), "claude-fable-5-1")

    def test_trailing_synthetic_skipped(self):
        self.write([a_line("claude-opus-5-5"), u_line(), a_line("claude-fable-5-1"), a_line("<synthetic>")])
        self.assertEqual(self.read(), "claude-fable-5-1")

    def test_cwd_space_and_dot_map_to_dash(self):
        path = self.write([a_line("claude-opus-5-5")])
        self.assertEqual(os.path.basename(os.path.dirname(path)), "-work-my-proj-v2")
        self.assertEqual(self.read(), "claude-opus-5-5")

    def test_glob_fallback_single_hit(self):
        self.assertIsNone(self.read())
        self.write([a_line("claude-opus-5-5")], sub="-other-dir")
        self.assertEqual(self.read(), "claude-opus-5-5")
        self.write([a_line("claude-opus-5-5")], sub="-third-dir")
        self.assertIsNone(self.read())

    def test_no_assistant_entry(self):
        self.write([u_line(), json.dumps({"type": "attachment"})])
        self.assertIsNone(self.read())

    def test_last_real_model_unsafe_is_none(self):
        self.write([a_line("claude-opus-5-5"), a_line("x y")])
        self.assertIsNone(self.read())

    def test_bad_lines_skipped(self):
        self.write_bytes(a_line("claude-opus-5-5").encode() + b"\n{not json\n\xff\xfe\n"
                         + '{"type": "user", "message": {"content": "한글 텍스트"}}'.encode("utf-8") + b"\n")
        self.assertEqual(self.read(), "claude-opus-5-5")

    def _big(self, before_mib, after_kib, model="claude-fable-5-1"):
        filler = u_line("x" * 1000)
        n_before = int(before_mib * 1024 * 1024) // (len(filler) + 1) + 1
        n_after = int(after_kib * 1024) // (len(filler) + 1) + 1
        return self.write([filler] * n_before + [a_line(model)] + [filler] * n_after)

    def test_bound_found_within_tail(self):
        self._big(3, 100)
        self.assertEqual(self.read(), "claude-fable-5-1")

    def test_bound_entry_beyond_tail_not_seen(self):
        path = self.write([a_line("claude-fable-5-1")] + [u_line("x" * 1000)] * 1160)
        self.assertGreater(os.path.getsize(path), int(1.1 * 1024 * 1024))
        self.assertIsNone(self.read())
        self.assertEqual(self.read(tail_bytes=os.path.getsize(path)), "claude-fable-5-1")

    def test_small_chunks_cross_edges(self):
        self.write([u_line("a" * 300), a_line("claude-opus-5-5"), u_line("b")])
        self.assertEqual(self.read(tail_bytes=200, chunk_bytes=64), "claude-opus-5-5")
        self.assertIsNone(self.read(tail_bytes=40, chunk_bytes=64))

    def test_reads_at_most_tail(self):
        self._big(3, 100)
        recorded = []

        class Proxy:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()
                return False

            def seek(self, *a):
                return self.f.seek(*a)

            def read(self, n):
                recorded.append(n)
                return self.f.read(n)

        def spy_open(path, mode):
            return Proxy(builtins.open(path, mode))

        with unittest.mock.patch("rotator.sessions.open", create=True, new=spy_open):
            self.assertEqual(self.read(), "claude-fable-5-1")
        self.assertTrue(recorded)
        self.assertLessEqual(sum(recorded), TRANSCRIPT_TAIL_BYTES)

    def test_bad_session_id(self):
        self.write([a_line("claude-opus-5-5")])
        for sid in ("../x", "a/b", ".", "..", "", "a\x00b"):
            with self.subTest(sid=sid):
                self.assertIsNone(read_session_model(self.home, self.cwd, sid))

    def test_bad_bounds_raise(self):
        with self.assertRaises(ValueError):
            self.read(tail_bytes=0)
        with self.assertRaises(ValueError):
            self.read(chunk_bytes=0)

    def test_missing_projects_dir(self):
        self.assertFalse(os.path.exists(os.path.join(self.home, "projects")))
        self.assertIsNone(self.read())

    def test_sidechain_entries_skipped(self):
        side = json.loads(a_line("claude-haiku-5-5"))
        side["isSidechain"] = True
        main = json.loads(a_line("claude-fable-5-1"))
        main["isSidechain"] = False
        self.write([json.dumps(main), u_line(), json.dumps(side)])
        self.assertEqual(self.read(), "claude-fable-5-1")
        self.write([json.dumps(side)])
        self.assertIsNone(self.read())

    def _read_in_thread(self):
        out = []
        t = threading.Thread(target=lambda: out.append(self.read()), daemon=True)
        t.start()
        t.join(5)
        return t.is_alive(), out

    def test_fifo_is_not_read(self):
        for sub in (re.sub("[^A-Za-z0-9]", "-", self.cwd), "-other-dir"):
            with self.subTest(sub=sub):
                d = os.path.join(self.home, "projects", sub)
                os.makedirs(d)
                fifo = os.path.join(d, self.sid + ".jsonl")
                os.mkfifo(fifo)
                try:
                    alive, out = self._read_in_thread()
                finally:
                    try:  # unblocks a reader stuck in open() if the guard regresses
                        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                    except OSError:
                        pass
                    os.unlink(fifo)
                    os.rmdir(d)
                self.assertFalse(alive, "reader blocked on a FIFO")
                self.assertEqual(out, [None])

    def test_directory_is_not_read(self):
        d = os.path.join(self.home, "projects", "-other-dir", self.sid + ".jsonl")
        os.makedirs(d)
        self.assertIsNone(self.read())


class ModelFromArgvTest(unittest.TestCase):
    def test_examples(self):
        cases = [
            (("claude", "--model", "fable"), "fable"),
            (("claude", "--model=claude-opus-5-5", "--model", "claude-fable-5-1"), "claude-fable-5-1"),
            (("claude", "--model", "claude-fable-5-1", "--model=claude-opus-5-5"), "claude-opus-5-5"),
            (("claude", "--model"), None),
            (("claude", "--model", "--resume"), None),
            (("claude",), None),
            (("claude", "--model", "<synthetic>"), None),
            (("claude", "--settings", "tok-SECRET-1"), None),
            (("claude", "--model", "fable[1m]"), "fable"),
            (("claude", "--model=claude-opus-5-5[1m]"), "claude-opus-5-5"),
            (("claude", "--model", "[1m]"), None),
            (("claude", "--model", "fable[1m"), None),
            (("claude", "--model", "fable[1m][2m]"), None),
        ]
        for argv, want in cases:
            with self.subTest(argv=argv):
                self.assertEqual(model_from_argv(argv), want)


class DiscoverModelTest(unittest.TestCase):
    def proc(self, argv):
        return FakeProc({4242: (CLAUDE[0], argv, CLAUDE[2])})

    def one(self, argv=("claude",), **kw):
        result, _, _, _ = run({P4242: entry(4242)}, proc=self.proc(argv), **kw)
        self.assertEqual(len(result), 1)
        return result[0]

    def test_default_off_reads_nothing(self):
        with unittest.mock.patch("rotator.sessions.read_session_model", unittest.mock.Mock()) as reader, \
                unittest.mock.patch("rotator.sessions.glob.glob", unittest.mock.Mock()) as g:
            s = self.one(("claude", "--model", "claude-fable-5-1"))
        reader.assert_not_called()
        g.assert_not_called()
        self.assertIsNone(s.model)

    def test_reader_value_attached(self):
        calls = []
        s = self.one(read_model=lambda c, w, i: calls.append((c, w, i)) or "claude-fable-5-1")
        self.assertEqual(s.model, "claude-fable-5-1")
        self.assertEqual(calls, [("/h/acc1", "/work/proj", entry(4242)["sessionId"])])

    def test_argv_fallback_when_reader_none(self):
        argv = ("claude", "--model", "fable")
        self.assertEqual(self.one(argv, read_model=lambda c, w, i: None).model, "fable")
        self.assertEqual(self.one(argv, read_model=lambda c, w, i: "claude-opus-5-5").model, "claude-opus-5-5")

    def test_reader_exception_contained(self):
        def boom(c, w, i):
            raise RuntimeError("SECRET-PROMPT-TEXT /h/acc1/projects/x.jsonl")

        with self.assertLogs("rotator.sessions", "DEBUG") as cm:
            s = self.one(read_model=boom)
        self.assertIsNone(s.model)
        msgs = [r.getMessage() for r in cm.records]
        self.assertTrue(any(m.endswith("reader_error") for m in msgs))
        self.assertFalse(any("SECRET-PROMPT-TEXT" in m or "/projects" in m for m in msgs))
        self.assertNotIn("SECRET-PROMPT-TEXT", repr(s))
        self.assertNotIn("/projects", repr(s))

    def test_reader_exception_then_argv(self):
        def boom(c, w, i):
            raise RuntimeError("x")

        s = self.one(("claude", "--model", "claude-fable-5-1"), read_model=boom)
        self.assertEqual(s.model, "claude-fable-5-1")

    def test_invalid_model_kept_session(self):
        for bad in ("<synthetic>", 5):
            with self.subTest(bad=bad):
                with self.assertLogs("rotator.sessions", "DEBUG") as cm:
                    s = self.one(read_model=lambda c, w, i: bad)
                self.assertIsNone(s.model)
                self.assertTrue(any(r.getMessage().endswith("invalid_model") for r in cm.records))

    def test_with_model_delegates(self):
        with unittest.mock.patch.object(rotator.sessions, "discover_sessions") as m:
            discover_sessions_with_model(["/h"])
        m.assert_called_once_with(["/h"], read_model=rotator.sessions.read_session_model)


if __name__ == "__main__":
    unittest.main()
