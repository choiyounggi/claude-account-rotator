import contextlib
import copy
import dataclasses
import io
import itertools
import json
import logging
import os
import plistlib
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

try:
    import pty
    import termios
except ImportError:  # Windows has neither; see TestWindowsTerm for the msvcrt-based equivalent
    pty = None
    termios = None

from rotator import cli, daemon, launchd, refresh, schtasks, switcher
from rotator.cli import STATUS_KEYS, build_status, main, render
from rotator.daemon import UsageCache, UsageStore
from rotator.config import (
    CONFIG_DIR_DEFAULT, LOG_DIR_DEFAULT, TIMEOUT_KEYS, TOP_KEYS, Config, ConfigError, load_config, parse_config, prompt_ok,
)
from rotator.daemon import (
    IN_PROGRESS_HINT, ORPHANED_HINT, USAGE_BACKOFF_MAX_S, Deps, UsageCache, record_path, result_dict,
    usage_summary,
)
from rotator.launchd import LABEL, exit_timeout_for
from rotator.models import AccountUsage, Profile, Session, UsageWindow
from rotator.policy import DEFAULT_CONFIG, PolicyConfig
from rotator.switcher import DEFAULT_CONTINUE_PROMPT, STATE_HINTS, SwitchResult, Timeouts, switch_session

CFG = "/Users/user/Library/Application Support/claude-account-rotator-test/config.json"


def _slash(path):
    # Every fixture path here is written with forward slashes. os.path.join on Windows produces
    # backslashes even from a "/"-only input, and os.path.abspath there goes through the real
    # GetFullPathName API, which also resolves a leading "/" against the current drive (e.g.
    # "D:\Users\...", not "\Users\..."); strip a drive prefix too so both sides compare equal
    # regardless of which drive the real filesystem happens to use.
    path = path.replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        path = path[2:]
    return path


def reader(files):
    files = {_slash(k): v for k, v in files.items()}

    def read(path):
        path = _slash(path)
        if path in files:
            return files[path]
        raise FileNotFoundError(path)
    return read


def raising_reader(exc):
    def read(path):
        raise exc
    return read


def load(text):
    return load_config(CFG, read_text=reader({CFG: text}))


class TestConfigDefaults(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        cfg = load_config(CFG, read_text=reader({}))
        self.assertEqual(cfg, Config())
        self.assertEqual(cfg.state_dir, CONFIG_DIR_DEFAULT)
        self.assertEqual(cfg.policy_config(), DEFAULT_CONFIG)
        self.assertEqual(cfg.timeouts, Timeouts())
        self.assertEqual(cfg.continue_prompt, DEFAULT_CONTINUE_PROMPT)
        self.assertIs(cfg.dry_run, False)
        self.assertIs(cfg.auto_switch, True)
        self.assertEqual(cfg.usage_poll_s, 30.0)

    def test_override_keeps_other_defaults(self):
        cfg = load('{"soft_pct": 80, "timeouts": {"ready_s": 90}}')
        self.assertEqual(cfg.soft_pct, 80.0)
        self.assertIsInstance(cfg.soft_pct, float)
        self.assertEqual(cfg.timeouts.ready_s, 90.0)
        self.assertEqual(cfg.timeouts.idle_s, 30.0)
        self.assertEqual(cfg.hard_pct, 95.0)

    def test_timeouts_screen_s(self):
        self.assertEqual(load('{"timeouts": {"screen_s": 2}}').timeouts.screen_s, 2.0)
        self.assertEqual(load('{"timeouts": {"screen_s": 0}}').timeouts.screen_s, 0.0)
        with self.assertRaises(ConfigError):
            load('{"timeouts": {"screen_s": -1}}')

    def test_empty_object(self):
        self.assertEqual(load("{}"), Config())

    def test_profiles_root_tilde_expanded(self):
        cfg = load('{"profiles_root": "~/x"}')
        self.assertTrue(os.path.isabs(cfg.profiles_root))
        # os.path.basename: expanduser leaves the "/x" suffix exactly as given (no normpath call in
        # parse_config), which on Windows can trail a backslash-based home with a literal "/x".
        self.assertEqual(os.path.basename(cfg.profiles_root), "x")

    def test_relative_path_made_absolute(self):
        seen = []

        def read(path):
            seen.append(path)
            raise FileNotFoundError(path)

        cfg = load_config("rel/config.json", read_text=read)
        self.assertTrue(os.path.isabs(seen[0]))
        self.assertTrue(seen[0].endswith(os.sep + os.path.join("rel", "config.json")))
        self.assertEqual(cfg.state_dir, CONFIG_DIR_DEFAULT)

    def test_state_dir_ignores_config_location(self):
        a = load_config("/Users/user/a/config.json", read_text=reader({}))
        b = load_config("/Users/user/b/config.json", read_text=reader({"/Users/user/b/config.json": "{}"}))
        self.assertEqual(a.state_dir, CONFIG_DIR_DEFAULT)
        self.assertEqual(b.state_dir, CONFIG_DIR_DEFAULT)

    def test_token_refresh_default(self):
        self.assertIs(Config().token_refresh, True)
        self.assertIs(load("{}").token_refresh, True)

    def test_token_refresh_false(self):
        self.assertIs(load('{"token_refresh": false}').token_refresh, False)

    def test_session_token_warmup_defaults(self):
        for cfg in (Config(), load("{}")):
            self.assertIs(cfg.session_token_warmup, True)
            self.assertIs(cfg.session_token_warmup_request, True)

    def test_session_token_warmup_false(self):
        cfg = load('{"session_token_warmup": false, "session_token_warmup_request": false}')
        self.assertIs(cfg.session_token_warmup, False)
        self.assertIs(cfg.session_token_warmup_request, False)
        self.assertIs(load('{"session_token_warmup_request": false}').session_token_warmup, True)

    def test_exclusion_ttl_and_copy_trust(self):
        self.assertEqual(load("{}").exclusion_ttl_s, 600.0)
        self.assertIs(load("{}").copy_trust, True)
        cfg = load('{"exclusion_ttl_s": 0, "copy_trust": false}')
        self.assertEqual(cfg.exclusion_ttl_s, 0.0)
        self.assertIs(cfg.copy_trust, False)

    def test_accept_trust_dialog(self):
        self.assertIs(load("{}").accept_trust_dialog, True)
        self.assertIs(load('{"accept_trust_dialog": false}').accept_trust_dialog, False)

    def test_experimental_windows_switch_default_and_override(self):
        self.assertIs(load("{}").experimental_windows_switch, False)
        self.assertIs(Config().experimental_windows_switch, False)
        self.assertIs(load('{"experimental_windows_switch": true}').experimental_windows_switch, True)

    def test_policy_config_carries_overrides(self):
        cfg = load('{"cooldown_s": 60, "stale_max_age_s": 120}')
        pc = cfg.policy_config()
        self.assertEqual(pc.cooldown, timedelta(seconds=60))
        self.assertEqual(pc.stale_max_age, timedelta(seconds=120))
        self.assertEqual(load('{"weekly_to_5h_ratio": 3}').policy_config().weekly_to_5h_ratio, 3.0)
        self.assertEqual(load("{}").policy_config().weekly_to_5h_ratio, PolicyConfig().weekly_to_5h_ratio)


class TestConfigValidation(unittest.TestCase):
    CASES = [
        ('{"hard_pct": 101}', "hard_pct"),
        ('{"soft_pct": true}', "soft_pct"),
        ('{"soft_pct": "85"}', "soft_pct"),
        ('{"soft_pct": NaN}', "soft_pct"),
        ('{"soft_pct": 90, "hard_pct": 80}', "hard_pct"),
        ('{"soft_pct": 5, "target_headroom_pct": 10}', "target_headroom_pct"),
        ('{"weekly_to_5h_ratio": 0.5}', "weekly_to_5h_ratio"),
        ('{"weekly_to_5h_ratio": 51}', "weekly_to_5h_ratio"),
        ('{"weekly_to_5h_ratio": true}', "weekly_to_5h_ratio"),
        ('{"cooldown_s": -1}', "cooldown_s"),
        ('{"session_poll_s": 0}', "session_poll_s"),
        ('{"usage_poll_s": 900}', "usage_poll_s"),
        ('{"timeouts": {"ready_s": -1}}', "timeouts.ready_s"),
        ('{"timeouts": {"poll_s": 0}}', "timeouts.poll_s"),
        ('{"timeouts": {"read_s": 1}}', "timeouts.read_s"),
        ('{"timeouts": []}', "timeouts"),
        ('{"soft_pc": 80}', "soft_pc"),
        ('{"state_dir": "/x"}', "state_dir"),
        ('{"dry_run": 1}', "dry_run"),
        ('{"token_refresh": "no"}', "token_refresh"),
        ('{"token_refresh": 1}', "token_refresh"),
        ('{"session_token_warmup": "yes"}', "session_token_warmup"),
        ('{"session_token_warmup": 0}', "session_token_warmup"),
        ('{"session_token_warmup_request": null}', "session_token_warmup_request"),
        ('{"exclusion_ttl_s": -1}', "exclusion_ttl_s"),
        ('{"exclusion_ttl_s": true}', "exclusion_ttl_s"),
        ('{"copy_trust": 1}', "copy_trust"),
        ('{"accept_trust_dialog": "yes"}', "accept_trust_dialog"),
        ('{"profiles_root": "rel/x"}', "profiles_root"),
        ('{"experimental_windows_switch": 1}', "experimental_windows_switch"),
        ('{"experimental_windows_switch": "true"}', "experimental_windows_switch"),
        ('{"continue_prompt": "a\\tb"}', "continue_prompt"),
    ]

    def test_each_invalid_key_named(self):
        for text, key in self.CASES:
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as cm:
                    load(text)
                self.assertTrue(str(cm.exception).startswith(key + ":"), str(cm.exception))

    def test_usage_poll_below_minimum_message(self):
        with self.assertRaises(ConfigError) as cm:
            load('{"usage_poll_s": 14.9}')
        self.assertEqual(str(cm.exception), "usage_poll_s: must be >= 15")

    def test_non_object_names_file(self):
        with self.assertRaises(ConfigError) as cm:
            load("[]")
        self.assertTrue(str(cm.exception).startswith(os.path.abspath(CFG)))

    def test_unparsable_names_file(self):
        with self.assertRaises(ConfigError) as cm:
            load("{")
        self.assertTrue(str(cm.exception).startswith(os.path.abspath(CFG)))
        self.assertIn("not valid JSON", str(cm.exception))

    def test_unreadable_names_file_and_class(self):
        with self.assertRaises(ConfigError) as cm:
            load_config(CFG, read_text=raising_reader(PermissionError("denied")))
        self.assertTrue(str(cm.exception).startswith(os.path.abspath(CFG)))
        self.assertIn("PermissionError", str(cm.exception))

    def test_parse_config_rejects_non_dict(self):
        with self.assertRaises(ConfigError) as cm:
            parse_config([], state_dir="/x")
        self.assertTrue(str(cm.exception).startswith("config:"))

    def test_boundaries_accepted(self):
        accepted = [
            ('{"soft_pct": 0, "hard_pct": 0, "target_headroom_pct": 0}', "soft_pct", 0.0),
            ('{"soft_pct": 100, "hard_pct": 100}', "hard_pct", 100.0),
            ('{"usage_poll_s": 15}', "usage_poll_s", 15.0),
            ('{"usage_poll_s": 29.9}', "usage_poll_s", 29.9),
            ('{"cooldown_s": 0}', "cooldown_s", 0.0),
            ('{"weekly_to_5h_ratio": 1}', "weekly_to_5h_ratio", 1.0),
            ('{"weekly_to_5h_ratio": 50}', "weekly_to_5h_ratio", 50.0),
        ]
        for text, key, value in accepted:
            with self.subTest(text=text):
                self.assertEqual(getattr(load(text), key), value)
        self.assertEqual(load('{"timeouts": {"poll_s": 0.001}}').timeouts.poll_s, 0.001)


class ZeroDriver:
    def send(self, *a, **k):
        raise AssertionError("driver.send must not be called")

    def read_screen(self, *a, **k):
        raise AssertionError("driver.read_screen must not be called")

    def wait_for(self, *a, **k):
        raise AssertionError("driver.wait_for must not be called")


class ZeroProbe:
    def is_alive(self, pid):
        raise AssertionError("procs.is_alive must not be called")

    def read_env(self, pid):
        raise AssertionError("procs.read_env must not be called")

    def process_table(self):
        raise AssertionError("procs.process_table must not be called")


def raise_os(path):
    raise OSError(path)


DRIFT_SESSION = Session(
    pid=4242, session_id="00000000-0000-4000-8000-000000000001", cwd="/Users/user/proj", status="idle",
    config_dir="/Users/user/Profiles/00000000-0000-0000-0000-00000000000a/home", argv=("claude",),
    terminal_handle=None)
DRIFT_TARGET = Profile(
    account_id="00000000-0000-0000-0000-00000000000b",
    home="/Users/user/Profiles/00000000-0000-0000-0000-00000000000b/home",
    keychain_service="Claude Code-credentials-0000")


class TestPromptDrift(unittest.TestCase):
    SAMPLES = [DEFAULT_CONTINUE_PROMPT, "go on", "", "   ", "a\nb", "a\rb", "a\tb", "a\x7fb",
               "-x", "/resume", "x" * 800, "x" * 801, 5, None]

    def test_prompt_ok_matches_switch_session(self):
        for p in self.SAMPLES:
            with self.subTest(p=repr(p)[:40]):
                expected = prompt_ok(p)
                try:
                    switch_session(DRIFT_SESSION, DRIFT_TARGET, "soft", driver=ZeroDriver(), procs=ZeroProbe(),
                                   read_text=raise_os, glob_paths=lambda pat: [], isdir=lambda path: False,
                                   clock=lambda: 0.0, sleep=lambda s: None, continue_prompt=p)
                    accepted = True
                except ValueError:
                    accepted = False
                self.assertEqual(accepted, expected, repr(p))
                try:
                    parse_config({"continue_prompt": p}, state_dir="/x")
                    parse_ok = True
                except ConfigError:
                    parse_ok = False
                self.assertEqual(prompt_ok(p), parse_ok, repr(p))

    def test_samples_cover_both_outcomes(self):
        results = {prompt_ok(p) for p in self.SAMPLES}
        self.assertEqual(results, {True, False})



# --- shared fixtures (mirror tests/test_daemon.py) --------------------------------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, ".claude", "tmp", "test-cli")
ROOT = "/Users/user/Library/Application Support/orca-test/claude-profiles"
ACC_A = "00000000-0000-0000-0000-00000000000a"
ACC_B = "00000000-0000-0000-0000-00000000000b"
ACC_C = "00000000-0000-0000-0000-00000000000c"
EMAIL = "user@example.com"
UUID_SECRET = "00000000-0000-0000-0000-00000000aaaa"
TOKEN = "sk-ant-oat01-FIXTURE-TOKEN-0000"
# The organization name is not among them: the status document shows it as the account's group.
SECRETS = (EMAIL, UUID_SECRET, TOKEN, "Fixture User")
T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
SID1 = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"
T0_ISO = "2026-10-28T12:00:00+00:00"


def prof(aid):
    return Profile(account_id=aid, home=os.path.join(ROOT, aid, "home"),
                   keychain_service="Claude Code-credentials-0000", email=EMAIL, display_name="Fixture User",
                   account_uuid=UUID_SECRET)


def sess(pid, aid, sid=SID1, handle="term_00000001", status="idle", started_at=T0, argv=None):
    return Session(pid=pid, session_id=sid, cwd="/Users/user/proj", status=status,
                   config_dir=os.path.join(ROOT, aid, "home"),
                   argv=("claude", "--settings", TOKEN) if argv is None else argv,
                   terminal_handle=handle, started_at=started_at)


def live(aid, pct, now=T0):
    return AccountUsage(account_id=aid, source="live", fetched_at=now,
                        five_hour=UsageWindow(pct, now + timedelta(hours=1)),
                        seven_day=UsageWindow(10.0, now + timedelta(days=2)))


def limited(aid, source="cache"):
    if source == "cache":
        return AccountUsage(account_id=aid, source="cache", fetched_at=T0 - timedelta(minutes=10),
                            five_hour=UsageWindow(50.0, T0 + timedelta(hours=1)),
                            seven_day=UsageWindow(10.0, T0 + timedelta(days=2)), error="http_429")
    return AccountUsage(account_id=aid, source="unavailable", fetched_at=T0, error="http_429;cache_missing")


def result(state="untouched", reason="not_idle", ok=False, step="preflight", new_pid=None, cmd="",
           request_id=None):
    return SwitchResult(ok=ok, step=step, reason=reason, state=state, new_pid=new_pid, relaunch_command=cmd,
                        recoverable=state in ("exited", "relaunched", "verified"), request_id=request_id,
                        other_account_id=None)


def make_cfg(**kw):
    return Config(profiles_root=ROOT, state_dir=os.path.join(BASE, "state"), **kw)


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


class _NoDriver:
    def send(self, *a, **k):
        raise AssertionError("driver used outside switch")

    read_screen = wait_for = send


class _NullProbe:
    def read_env(self, pid):
        return None

    def is_alive(self, pid):
        return True

    def process_table(self):
        return {}


def _not_used(*a, **k):
    raise AssertionError("not used by the CLI")


def _no_refresh(*a, **k):
    raise AssertionError("status never refreshes")


class CliEnv:
    def __init__(self):
        self.profiles: List[Profile] = []
        self.sessions: List[Session] = []
        self.usages: Dict[str, AccountUsage] = {}
        self.fetch_raises: Set[str] = set()
        self.profiles_raise = False
        self.files: Dict[str, str] = {}
        self.fail_write: Set[str] = set()
        self.alive_pids: Set[int] = set()
        self.pointer_text: Optional[str] = None
        self.now_v = T0
        self.mono_v = 1000.0
        self.calls: List[tuple] = []
        self.results: List[object] = []
        self.held: Set[str] = set()
        self.switch_kwargs: List[dict] = []
        self.log_records: List[str] = []
        self.waits: List[float] = []
        self.wait_hook: Optional[Callable[[int], None]] = None

    def advance(self, seconds):
        self.now_v = self.now_v + timedelta(seconds=seconds)
        self.mono_v += seconds

    def make_logger(self):
        lg = logging.getLogger("rotator.clitest.%d" % id(self))
        lg.propagate = False
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            lg.addHandler(_ListHandler(self.log_records))
        return lg

    def fetches(self):
        return [c for c in self.calls if c[0] == "fetch"]

    def deps(self, **overrides):
        env = self

        def discover_profiles(root):
            env.calls.append(("discover_profiles",))
            if env.profiles_raise:
                raise RuntimeError("profiles boom")
            return list(env.profiles)

        def fetch_usage(p):
            env.calls.append(("fetch", p.account_id))
            if p.account_id in env.fetch_raises:
                raise RuntimeError("fetch boom")
            return env.usages[p.account_id]

        def switch(session, target, mode, **kw):
            env.calls.append(("switch", session.pid, target.account_id, mode))
            env.switch_kwargs.append(kw)
            r = env.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def wait_stop(event, t):
            env.waits.append(t)
            if env.wait_hook is not None:
                env.wait_hook(len(env.waits))
            return event.is_set()

        def lock(path):
            env.calls.append(("lock", path))
            if path in env.held:
                return None
            env.held.add(path)
            return _CliLock(env, path)

        def read_text(path):
            env.calls.append(("read", path))
            if path in env.files:
                return env.files[path]
            raise FileNotFoundError(path)

        def write_text(path, text):
            if any(x in path for x in env.fail_write):
                raise OSError("fake write failure")
            env.files[path] = text
            env.calls.append(("write", path))

        def unlink(path):
            env.calls.append(("unlink", path))
            env.files.pop(path, None)

        def list_dir(path):
            env.calls.append(("list_dir", path))
            prefix = path + os.sep
            names = sorted({k[len(prefix):] for k in env.files
                            if k.startswith(prefix) and os.sep not in k[len(prefix):]})
            if not names:
                raise FileNotFoundError(path)
            return names

        def discover_sessions(homes):
            env.calls.append(("discover_sessions",))
            return list(env.sessions)

        def read_pointer(root):
            env.calls.append(("read_pointer",))
            return env.pointer_text

        fields = dict(
            discover_profiles=discover_profiles, fetch_usage=fetch_usage,
            refresh_token=_no_refresh, refresh_held=_no_refresh,
            discover_sessions=discover_sessions, read_limit=lambda s: None, switch=switch, make_driver=_NoDriver,
            procs=_NullProbe(), read_pointer=read_pointer, write_pointer=_not_used,
            now=lambda: env.now_v, monotonic=lambda: env.mono_v, spawn=_not_used, wait_stop=wait_stop, lock=lock,
            read_text=read_text, write_text=write_text, unlink=unlink, list_dir=list_dir, isdir=lambda p: True,
            pid_alive=lambda pid: pid in env.alive_pids, install_signals=_not_used, make_logger=self.make_logger,
        )
        fields.update(overrides)
        return Deps(**fields)


class _CliLock:
    def __init__(self, env, path):
        self.env = env
        self.path = path

    def write_owner(self, pid):
        self.env.files[self.path] = str(pid)

    def release(self):
        self.env.held.discard(self.path)
        self.env.calls.append(("release", self.path))


def standard_env():
    env = CliEnv()
    env.profiles = [prof(ACC_A), prof(ACC_B), prof(ACC_C)]
    env.usages = {a: live(a, 20.0) for a in (ACC_A, ACC_B, ACC_C)}
    return env


def snapshot_doc(env, written_at, pid=4242, accounts=(ACC_A, ACC_B, ACC_C), sessions=()):
    return json.dumps({"schema": 1, "written_at": written_at.isoformat(), "daemon_pid": pid,
                       "flags": {"dry_run": False, "auto_switch": True, "pointer_correction": True},
                       "accounts": [usage_summary(live(a, 33.0)) for a in accounts],
                       "pointer_account_id": None, "pointer_target_account_id": None,
                       "sessions": list(sessions)})


def stored_record(pid, sid=SID1, started=T0_ISO, res=None, **kw):
    rec = {"schema": 1, "record_state": "final", "owner_pid": 999999, "attempt": 1, "pid": pid,
           "session_id": sid, "started_at": started, "updated_at": T0_ISO, "source": "daemon", "mode": "soft",
           "target_account_id": ACC_B, "result": result_dict(res or result()), "hint": "", "touched": False,
           "unswitchable": None, "excluded": [], "hard_hold_until": None, "post_verify": None,
           "worker_error": None}
    rec.update(kw)
    return rec


def scratch(name):
    path = os.path.join(BASE, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


# --- Task 10 ---------------------------------------------------------------------------------------------


class FakeRunner:
    """launchctl by verb. A list answers one call per item (the last repeats). Once a bootstrap succeeded, `print`
    answers from "print_after" (default loaded, rc 0) like launchd would."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: List[tuple] = []
        self.bootstrapped = False

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        verb = argv[1]
        key = "print_after" if verb == "print" and self.bootstrapped else verb
        v = self.script.get(key, (0, "", ""))
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        if verb == "bootstrap" and v[0] == 0:
            self.bootstrapped = True
        return v

    def verbs(self):
        return [argv[1] for argv, _ in self.calls]


COPIED_AT = datetime(2026, 11, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_source(root, extra_py=("x.py",)):
    """A fake checkout: the runnable app plus everything install must leave behind."""
    files = {"bin/rotator": "#!/usr/bin/python3\nprint('entry')\n", "rotator/__init__.py": "",
             "rotator/cli.py": "CLI = 1\n", "tests/test_x.py": "T = 1\n", ".git/HEAD": "ref: refs/heads/main\n",
             ".orchestration/notes.md": "n\n", "rotator/__pycache__/cli.cpython-39.pyc": "pyc",
             "rotator/notes.txt": "not code\n", "README.md": "r\n"}
    for name in extra_py:
        files["rotator/" + name] = "# %s\n" % name
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return root


def tree(root):
    out = set()
    for d, dirs, names in os.walk(root):
        for n in dirs + names:
            out.add(os.path.relpath(os.path.join(d, n), root))
    return out


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class LaunchdBase(unittest.TestCase):
    def setUp(self):
        self.agents = scratch("agents-" + self._testMethodName)
        self.plist = os.path.join(self.agents, LABEL + ".plist")
        self.logs = os.path.join(BASE, "logs-" + self._testMethodName)
        shutil.rmtree(self.logs, ignore_errors=True)
        self.source = make_source(scratch("src-" + self._testMethodName))
        self.state = scratch("state-" + self._testMethodName)
        self.app = os.path.join(self.state, "app")
        self.commit = "abc1234"

    def install(self, runner, **kw):
        args = dict(runner=runner, uid=501, plist_path=self.plist, source_root=self.source, state_dir=self.state,
                    config_path="/Users/user/cfg/config.json", log_dir=self.logs, timeouts=Timeouts(),
                    commit_of=lambda root: self.commit, now=lambda: COPIED_AT)
        args.update(kw)
        rc = launchd.install(**args)
        TestLaunchd.all_calls.extend(argv for argv, _ in runner.calls)
        return rc

    def uninstall(self, runner):
        rc = launchd.uninstall(runner=runner, uid=501, plist_path=self.plist, state_dir=self.state)
        TestLaunchd.all_calls.extend(argv for argv, _ in runner.calls)
        return rc

    def leftovers(self):
        return sorted(n for n in os.listdir(self.state) if n != "app")


@unittest.skipIf(sys.platform == "win32", "launchd is macOS-only; this tests its plist/POSIX-permission install mechanics directly")
class TestLaunchd(LaunchdBase):
    all_calls: List[list] = []

    def test_install_not_loaded(self):
        r = FakeRunner({"print": (113, "", "Could not find service")})
        rc, msg = self.install(r)
        self.assertEqual(rc, 0)
        self.assertIn(LABEL, msg)
        self.assertEqual([argv for argv, _ in r.calls], [
            ["/bin/launchctl", "print", "gui/501/" + LABEL],
            ["/bin/launchctl", "bootstrap", "gui/501", self.plist],
            ["/bin/launchctl", "print", "gui/501/" + LABEL]])
        with open(self.plist, "rb") as f:
            pl = plistlib.load(f)
        self.assertEqual(pl["Label"], LABEL)
        self.assertEqual(pl["ExitTimeOut"], 695)
        self.assertGreaterEqual(pl["ExitTimeOut"], 510)
        self.assertEqual(pl["ProgramArguments"][0], "/usr/bin/python3")
        self.assertEqual(pl["ProgramArguments"][1], os.path.join(self.app, "bin", "rotator"))
        self.assertEqual(pl["WorkingDirectory"], self.app)
        self.assertEqual(pl["ProgramArguments"][-3:], ["--config", "/Users/user/cfg/config.json", "daemon"])
        self.assertIs(pl["RunAtLoad"], True)
        self.assertIs(pl["KeepAlive"], True)
        self.assertEqual(pl["StandardErrorPath"], os.path.join(self.logs, "launchd.err.log"))
        self.assertNotIn("EnvironmentVariables", pl)
        self.assertEqual(stat.S_IMODE(os.stat(self.plist).st_mode), 0o600)

    def test_install_loaded_reinstalls(self):
        r = FakeRunner()
        self.assertEqual(self.install(r)[0], 0)
        self.assertEqual(r.verbs(), ["print", "bootout", "bootstrap", "print"])
        self.assertEqual(r.calls[1][1], 695 + 30.0)
        self.assertEqual(r.calls[1][0], ["/bin/launchctl", "bootout", "gui/501/" + LABEL])

    def test_install_bootstrap_failure(self):
        r = FakeRunner({"print": (113, "", ""), "bootstrap": (5, "", "Bootstrap failed: 5: Input/output error\n")})
        self.assertEqual(self.install(r), (1, "launchctl bootstrap failed: Bootstrap failed: 5: Input/output error"))

    def test_install_not_loaded_bootstrap_io_error_not_retried(self):
        sleeps = []
        r = FakeRunner({"print": (113, "", ""), "bootstrap": (5, "", "Bootstrap failed: 5: Input/output error\n")})
        self.assertEqual(self.install(r, sleep=sleeps.append)[0], 1)
        self.assertEqual(r.verbs(), ["print", "bootstrap"])
        self.assertEqual(sleeps, [])

    def test_install_bootout_failure(self):
        r = FakeRunner({"bootout": (3, "", "nope\n")})
        rc, msg = self.install(r)
        self.assertEqual(rc, 1)
        self.assertEqual(msg, "launchctl bootout failed: nope")
        self.assertNotIn("bootstrap", r.verbs())

    def test_uninstall_loaded(self):
        self.install(FakeRunner({"print": (113, "", "")}))
        r = FakeRunner()
        self.assertEqual(self.uninstall(r)[0], 0)
        self.assertEqual(r.verbs(), ["print", "bootout"])
        self.assertEqual(r.calls[1][1], 695 + 30.0)
        self.assertFalse(os.path.exists(self.plist))

    def test_uninstall_not_loaded(self):
        self.install(FakeRunner({"print": (113, "", "")}))
        r = FakeRunner({"print": (113, "", "")})
        self.assertEqual(self.uninstall(r)[0], 0)
        self.assertEqual(r.verbs(), ["print"])
        self.assertFalse(os.path.exists(self.plist))

    def test_uninstall_absent_plist(self):
        r = FakeRunner()
        self.assertEqual(self.uninstall(r), (0, "uninstalled " + LABEL))
        self.assertEqual(r.calls[1][1], 695 + 30.0)

    def test_uninstall_bootout_failure_keeps_plist(self):
        self.install(FakeRunner({"print": (113, "", "")}))
        r = FakeRunner({"bootout": (5, "", "busy\n")})
        self.assertEqual(self.uninstall(r), (1, "launchctl bootout failed: busy"))
        self.assertTrue(os.path.exists(self.plist))

    def test_exact_label_only(self):
        TestLaunchd.all_calls = []
        self.install(FakeRunner({"print": (113, "", "")}))
        self.install(FakeRunner())
        self.uninstall(FakeRunner())
        self.uninstall(FakeRunner({"print": (113, "", "")}))
        self.assertEqual(len(TestLaunchd.all_calls), 3 + 4 + 2 + 1)  # each install ends with a verifying print
        for argv in TestLaunchd.all_calls:
            self.assertEqual(argv[0], "/bin/launchctl")
            if argv[1] == "bootstrap":
                self.assertEqual(argv[2], "gui/501")
                self.assertTrue(argv[3].endswith(LABEL + ".plist"))
            else:
                self.assertEqual(argv[2], "gui/501/" + LABEL)
            for part in argv:
                for bad in ("*", "com.apple", "com.example"):
                    self.assertNotIn(bad, part)

    def test_exit_timeout_tracks_timeouts(self):
        self.assertEqual(exit_timeout_for(Timeouts()), 695)
        self.assertEqual(exit_timeout_for(Timeouts(ready_s=120.0)), exit_timeout_for(Timeouts()) + 60)

    def test_required_seams(self):
        with self.assertRaises(TypeError):
            launchd.install()
        with self.assertRaises(TypeError):
            launchd.uninstall(uid=501, plist_path=self.plist)

    def test_repo_root(self):
        self.assertEqual(launchd.repo_root(), REPO)
        self.assertTrue(launchd.plist_path_default().endswith("/Library/LaunchAgents/" + LABEL + ".plist"))


NOT_LOADED = {"print": (113, "", "")}
APP_TREE = {"VERSION", "bin", "bin/rotator", "rotator", "rotator/__init__.py", "rotator/cli.py", "rotator/x.py"}


def failing_copy(fail_at):
    """shutil.copyfile that raises on its ``fail_at``-th call (1-based)."""
    calls = []

    def copy(src, dst):
        calls.append(src)
        if len(calls) == fail_at:
            raise OSError(28, "No space left on device")
        return shutil.copyfile(src, dst)
    return copy


IO_ERR = (5, "", "Bootstrap failed: 5: Input/output error\n")


@unittest.skipIf(sys.platform == "win32", "launchd is macOS-only; this tests its plist/POSIX-permission install mechanics directly")
class TestInstallRetry(LaunchdBase):
    """bootstrap right after bootout races the old instance still exiting (launchctl rc 5, Input/output error)."""

    def run_install(self, script):
        self.sleeps = []
        r = FakeRunner(script)
        return r, self.install(r, sleep=self.sleeps.append)

    def test_retried_until_success(self):
        r, (rc, msg) = self.run_install({"bootstrap": [IO_ERR, IO_ERR, IO_ERR, (0, "", "")]})
        self.assertEqual(rc, 0)
        self.assertIn(LABEL, msg)
        self.assertEqual(r.verbs(), ["print", "bootout"] + ["bootstrap"] * 4 + ["print"])
        self.assertEqual(self.sleeps, [1.0, 1.0, 1.0])

    def test_gives_up_after_ten_retries(self):
        last = (5, "", "Bootstrap failed: 5: Input/output error (last)\n")
        r, (rc, msg) = self.run_install({"bootstrap": [IO_ERR] * 10 + [last]})
        self.assertEqual(rc, 1)
        self.assertEqual(msg, "launchctl bootstrap failed: Bootstrap failed: 5: Input/output error (last)")
        self.assertEqual(r.verbs().count("bootstrap"), 11)
        self.assertEqual(self.sleeps, [1.0] * 10)
        self.assertNotIn("print", r.verbs()[2:])

    def test_success_on_last_retry(self):
        r, (rc, _) = self.run_install({"bootstrap": [IO_ERR] * 10 + [(0, "", "")]})
        self.assertEqual(rc, 0)
        self.assertEqual(r.verbs().count("bootstrap"), 11)

    def test_io_error_text_with_other_rc_is_retried(self):
        r, (rc, _) = self.run_install({"bootstrap": [(1, "", "Bootstrap failed: Input/output error\n"), (0, "", "")]})
        self.assertEqual(rc, 0)
        self.assertEqual(self.sleeps, [1.0])

    def test_rc5_without_text_is_retried(self):
        r, (rc, _) = self.run_install({"bootstrap": [(5, "", ""), (0, "", "")]})
        self.assertEqual(rc, 0)
        self.assertEqual(self.sleeps, [1.0])

    def test_other_error_not_retried(self):
        r, (rc, msg) = self.run_install({"bootstrap": (37, "", "Operation already in progress\n")})
        self.assertEqual((rc, msg), (1, "launchctl bootstrap failed: Operation already in progress"))
        self.assertEqual(r.verbs(), ["print", "bootout", "bootstrap"])
        self.assertEqual(self.sleeps, [])

    def test_job_missing_after_bootstrap_is_an_error(self):
        r, (rc, msg) = self.run_install({"print_after": (113, "", "Could not find service")})
        self.assertEqual(rc, 1)
        self.assertIn("launchctl print does not show %s" % LABEL, msg)
        self.assertEqual(r.verbs(), ["print", "bootout", "bootstrap", "print"])
        self.assertEqual(r.calls[-1][0], ["/bin/launchctl", "print", "gui/501/" + LABEL])

    def test_job_shown_after_bootstrap(self):
        r, (rc, _) = self.run_install({"print": (113, "", "")})
        self.assertEqual(rc, 0)
        self.assertEqual(r.verbs(), ["print", "bootstrap", "print"])


@unittest.skipIf(sys.platform == "win32", "launchd is macOS-only; this tests its POSIX-permission-preserving app copy directly")
class TestAppCopy(LaunchdBase):
    def mode(self, *rel):
        return stat.S_IMODE(os.lstat(os.path.join(self.app, *rel)).st_mode)

    def test_copies_only_the_runnable_app(self):
        rc, msg = self.install(FakeRunner(NOT_LOADED))
        self.assertEqual(rc, 0, msg)
        self.assertEqual(tree(self.app), APP_TREE)
        for rel in ("bin/rotator", "rotator/__init__.py", "rotator/cli.py", "rotator/x.py"):
            self.assertEqual(read_file(os.path.join(self.app, rel)), read_file(os.path.join(self.source, rel)))
        self.assertEqual(self.leftovers(), [])
        self.assertIn(self.app, msg)
        self.assertIn(self.commit, msg)

    def test_modes(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        self.assertEqual(self.mode(), 0o700)
        self.assertEqual(self.mode("bin"), 0o700)
        self.assertEqual(self.mode("rotator"), 0o700)
        self.assertEqual(self.mode("bin", "rotator"), 0o700)
        for rel in ("__init__.py", "cli.py", "x.py"):
            self.assertEqual(self.mode("rotator", rel), 0o600)
        self.assertEqual(self.mode("VERSION"), 0o600)

    def test_modes_ignore_umask(self):
        old = os.umask(0)
        try:
            self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        finally:
            os.umask(old)
        self.assertEqual(self.mode(), 0o700)
        self.assertEqual(self.mode("rotator"), 0o700)
        self.assertEqual(self.mode("rotator", "cli.py"), 0o600)

    def test_version_file(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        lines = read_file(os.path.join(self.app, "VERSION")).splitlines()
        self.assertEqual(lines, ["abc1234", "2026-11-02T03:04:05Z"])
        self.assertEqual(launchd.installed_commit(self.state), "abc1234")

    def test_version_unknown_without_commit(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED), commit_of=lambda root: None)[0], 0)
        self.assertEqual(read_file(os.path.join(self.app, "VERSION")).splitlines()[0], "unknown")
        self.assertIsNone(launchd.installed_commit(self.state))

    def test_commit_read_from_the_source_root(self):
        seen = []
        self.install(FakeRunner(NOT_LOADED), commit_of=lambda root: seen.append(root) or "abc1234")
        self.assertEqual(seen, [self.source])

    def test_plist_points_to_the_copy(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        with open(self.plist, "rb") as f:
            pl = plistlib.load(f)
        self.assertEqual(pl["ProgramArguments"], ["/usr/bin/python3", os.path.join(self.app, "bin", "rotator"),
                                                  "--config", "/Users/user/cfg/config.json", "daemon"])
        self.assertEqual(pl["WorkingDirectory"], self.app)
        for value in plistlib.dumps(pl).decode("utf-8").split("<string>")[1:]:
            self.assertNotIn(self.source, value)

    def test_reinstall_replaces_the_copy(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        os.remove(os.path.join(self.source, "rotator", "x.py"))
        with open(os.path.join(self.source, "rotator", "y.py"), "w", encoding="utf-8") as f:
            f.write("Y = 2\n")
        self.commit = "def5678"
        self.assertEqual(self.install(FakeRunner())[0], 0)
        self.assertEqual(tree(self.app), APP_TREE - {"rotator/x.py"} | {"rotator/y.py"})
        self.assertEqual(launchd.installed_commit(self.state), "def5678")
        self.assertEqual(self.leftovers(), [])

    def test_failed_copy_keeps_the_old_copy(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        plist_before = read_file(self.plist)
        with open(os.path.join(self.source, "rotator", "cli.py"), "w", encoding="utf-8") as f:
            f.write("CLI = 2\n")
        for fail_at in (1, 2, 4):
            with self.subTest(fail_at=fail_at):
                self.commit = "def5678"
                r = FakeRunner()
                rc, msg = self.install(r, copy_file=failing_copy(fail_at))
                self.assertEqual(rc, 1)
                self.assertIn("copy", msg)
                self.assertEqual(r.calls, [])
                self.assertEqual(tree(self.app), APP_TREE)
                self.assertEqual(read_file(os.path.join(self.app, "rotator", "cli.py")), "CLI = 1\n")
                self.assertEqual(launchd.installed_commit(self.state), "abc1234")
                self.assertEqual(read_file(self.plist), plist_before)
                self.assertEqual(self.leftovers(), [])

    def test_failed_swap_restores_the_old_copy(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        real_rename = os.rename
        renames = []

        def rename(src, dst):
            renames.append((src, dst))
            if dst == self.app and len(renames) == 2:
                raise OSError(1, "Operation not permitted")
            return real_rename(src, dst)
        self.commit = "def5678"
        with mock.patch("rotator.launchd.os.rename", rename):
            rc, _ = self.install(FakeRunner())
        self.assertEqual(rc, 1)
        self.assertEqual(launchd.installed_commit(self.state), "abc1234")
        self.assertEqual(tree(self.app), APP_TREE)
        self.assertEqual(self.leftovers(), [])

    def test_missing_entry_point_refused(self):
        os.remove(os.path.join(self.source, "bin", "rotator"))
        r = FakeRunner(NOT_LOADED)
        rc, _ = self.install(r)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.lexists(self.app))
        self.assertFalse(os.path.exists(self.plist))
        self.assertEqual(r.calls, [])
        self.assertEqual(self.leftovers(), [])

    def test_no_modules_refused(self):
        for name in ("__init__.py", "cli.py", "x.py"):
            os.remove(os.path.join(self.source, "rotator", name))
        rc, _ = self.install(FakeRunner(NOT_LOADED))
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.lexists(self.app))
        self.assertEqual(self.leftovers(), [])

    @unittest.skipIf(sys.platform == "win32",
                     "os.symlink needs elevated/Developer Mode privileges on Windows and is unreliable in CI")
    def test_symlinked_app_dir_refused_on_install(self):
        victim = scratch("victim-" + self._testMethodName)
        with open(os.path.join(victim, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.symlink(victim, self.app)
        r = FakeRunner(NOT_LOADED)
        rc, msg = self.install(r)
        self.assertEqual(rc, 1)
        self.assertIn("not a real directory", msg)
        self.assertTrue(os.path.islink(self.app))
        self.assertEqual(os.listdir(victim), ["keep.txt"])
        self.assertEqual(r.calls, [])
        self.assertEqual(self.leftovers(), [])

    def test_app_file_refused_on_install(self):
        with open(self.app, "w", encoding="utf-8") as f:
            f.write("file\n")
        rc, msg = self.install(FakeRunner(NOT_LOADED))
        self.assertEqual(rc, 1)
        self.assertIn("not a real directory", msg)
        self.assertEqual(read_file(self.app), "file\n")

    def test_protected_config_refused(self):
        home = os.path.expanduser("~")
        for folder in ("Desktop", "Documents", "Downloads"):
            with self.subTest(folder=folder):
                r = FakeRunner(NOT_LOADED)
                rc, msg = self.install(r, config_path=os.path.join(home, folder, "cfg", "config.json"))
                self.assertEqual(rc, 2)
                self.assertIn("~/" + folder, msg)
                self.assertEqual(r.calls, [])
                self.assertFalse(os.path.lexists(self.app))
                self.assertFalse(os.path.exists(self.plist))

    def test_protected_dir_is_component_wise(self):
        home = os.path.expanduser("~")
        self.assertEqual(launchd.protected_dir(os.path.join(home, "Desktop")), "~/Desktop")
        self.assertEqual(launchd.protected_dir(os.path.join(home, "Downloads", "a", "b.json")), "~/Downloads")
        self.assertIsNone(launchd.protected_dir(os.path.join(home, "Desktopx", "config.json")))
        self.assertIsNone(launchd.protected_dir(os.path.join(home, "Library", "Desktop", "config.json")))
        self.assertIsNone(launchd.protected_dir(""))
        rc, _ = self.install(FakeRunner(NOT_LOADED), config_path=os.path.join(home, "Desktopx", "config.json"))
        self.assertEqual(rc, 0)

    def test_uninstall_removes_only_the_copy(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        keep = {"config.json": "{}\n", "state.json": "{}\n", "usage/u-1.json": "{}\n", "app-old/x.py": "x\n",
                "app.bak": "b\n"}
        for rel, text in keep.items():
            p = os.path.join(self.state, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        self.assertEqual(self.uninstall(FakeRunner()), (0, "uninstalled " + LABEL))
        self.assertFalse(os.path.lexists(self.app))
        for rel, text in keep.items():
            self.assertEqual(read_file(os.path.join(self.state, rel)), text)
        self.assertTrue(os.path.isdir(self.source))

    @unittest.skipIf(sys.platform == "win32",
                     "os.symlink needs elevated/Developer Mode privileges on Windows and is unreliable in CI")
    def test_uninstall_symlinked_app_refused(self):
        victim = scratch("victim-" + self._testMethodName)
        with open(os.path.join(victim, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.symlink(victim, self.app)
        rc, msg = self.uninstall(FakeRunner(NOT_LOADED))
        self.assertEqual(rc, 1)
        self.assertIn("refused", msg)
        self.assertIn(self.app, msg)
        self.assertTrue(os.path.islink(self.app))
        self.assertEqual(os.listdir(victim), ["keep.txt"])

    def test_uninstall_app_file_refused(self):
        with open(self.app, "w", encoding="utf-8") as f:
            f.write("file\n")
        rc, msg = self.uninstall(FakeRunner(NOT_LOADED))
        self.assertEqual(rc, 1)
        self.assertIn("refused", msg)
        self.assertEqual(read_file(self.app), "file\n")

    def test_uninstall_bootout_failure_keeps_the_copy(self):
        self.assertEqual(self.install(FakeRunner(NOT_LOADED))[0], 0)
        self.assertEqual(self.uninstall(FakeRunner({"bootout": (5, "", "busy\n")})),
                         (1, "launchctl bootout failed: busy"))
        self.assertEqual(tree(self.app), APP_TREE)

    def test_uninstall_without_copy(self):
        self.assertEqual(self.uninstall(FakeRunner(NOT_LOADED)), (0, "uninstalled " + LABEL))
        self.assertEqual(os.listdir(self.state), [])


class TestStaleInstall(unittest.TestCase):
    def setUp(self):
        self.state = scratch("stale-state-" + self._testMethodName)
        self.plist = os.path.join(scratch("stale-agents-" + self._testMethodName), LABEL + ".plist")
        with open(self.plist, "w", encoding="utf-8") as f:
            f.write("<plist/>\n")
        self.source = scratch("stale-src-" + self._testMethodName)

    def version(self, text):
        os.makedirs(os.path.join(self.state, "app"), exist_ok=True)
        with open(os.path.join(self.state, "app", "VERSION"), "w", encoding="utf-8") as f:
            f.write(text)

    def stale(self, commit):
        return launchd.install_stale(self.plist, self.state, self.source, commit_of=lambda root: commit)

    def test_differs(self):
        self.version("abc1234\n2026-11-02T03:04:05Z\n")
        self.assertIs(self.stale("def5678"), True)

    def test_same_or_prefix(self):
        self.version("abc1234\n2026-11-02T03:04:05Z\n")
        self.assertIs(self.stale("abc1234"), False)
        self.assertIs(self.stale("abc12345678"), False)
        self.version("abc12345678\nx\n")
        self.assertIs(self.stale("abc1234"), False)

    def test_not_installed(self):
        self.version("abc1234\n")
        os.remove(self.plist)
        self.assertIs(self.stale("def5678"), False)

    def test_unknown_either_side(self):
        self.assertIs(self.stale("def5678"), False)  # no VERSION
        self.version("unknown\n2026-11-02T03:04:05Z\n")
        self.assertIs(self.stale("def5678"), False)
        self.version("")
        self.assertIs(self.stale("def5678"), False)
        self.version("abc1234\n")
        self.assertIs(self.stale(None), False)

    def test_errors_are_silent(self):
        self.version("abc1234\n")

        def boom(root):
            raise OSError("git gone")
        self.assertIs(launchd.install_stale(self.plist, self.state, self.source, commit_of=boom), False)
        with open(os.path.join(self.state, "app", "VERSION"), "wb") as f:
            f.write(b"\xff\xfe\x00")
        self.assertIs(self.stale("def5678"), False)

    def test_source_commit(self):
        calls = []

        def runner(out=(0, "abc1234\n", "")):
            def run(argv, timeout):
                calls.append(argv)
                return out
            return run
        self.assertIsNone(launchd.source_commit(self.source, runner()))
        self.assertEqual(calls, [])  # no .git: git is never asked (it would walk up to a parent repo)
        os.makedirs(os.path.join(self.source, ".git"))
        self.assertEqual(launchd.source_commit(self.source, runner()), "abc1234")
        self.assertEqual(calls[-1], ["git", "-C", self.source, "rev-parse", "--short", "HEAD"])
        self.assertIsNone(launchd.source_commit(self.source, runner((128, "", "fatal: not a git repository"))))
        self.assertIsNone(launchd.source_commit(self.source, runner((0, "not a hash\n", ""))))
        self.assertIsNone(launchd.source_commit(self.source, runner((0, "", ""))))

        def missing(argv, timeout):
            raise FileNotFoundError("git")
        self.assertIsNone(launchd.source_commit(self.source, missing))

    def test_source_commit_worktree_git_file(self):
        with open(os.path.join(self.source, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /elsewhere\n")
        self.assertEqual(launchd.source_commit(self.source, lambda argv, timeout: (0, "0123abc\n", "")), "0123abc")

    def test_release_version_matches_only_itself(self):
        self.version("v0.1.0\n2026-11-02T03:04:05Z\n")
        self.assertIs(self.stale("v0.1.0"), False)
        self.assertIs(self.stale("v0.2.0"), True)
        self.assertIs(self.stale("abc1234"), True)  # switched from an npm install to a checkout
        self.version("v0.1.1\n")
        self.assertIs(self.stale("v0.1.10"), True)  # never a prefix match
        self.version("abc1234\n")
        self.assertIs(self.stale("v0.1.0"), True)

    def test_source_commit_falls_back_to_package_version(self):
        def no_git(argv, timeout):
            raise AssertionError("git must not run without .git")
        pkg = os.path.join(self.source, "package.json")
        self.assertIsNone(launchd.source_commit(self.source, no_git))  # neither .git nor package.json
        for text, want in (('{"version": "0.1.0"}', "v0.1.0"), ('{"version": "0.2.0-rc.1"}', "v0.2.0-rc.1"),
                           ('{"version": "0.1"}', None),
                           ('{"version": 1}', None), ("[]", None), ("{", None)):
            with open(pkg, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(launchd.source_commit(self.source, no_git), want, text)
        with open(pkg, "w", encoding="utf-8") as f:
            f.write('{"version": "0.1.0"}')
        os.makedirs(os.path.join(self.source, ".git"))
        self.assertEqual(launchd.source_commit(self.source, lambda argv, timeout: (0, "abc1234\n", "")), "abc1234")

    def test_installed_release_version(self):
        self.version("v0.1.0\n2026-11-02T03:04:05Z\n")
        self.assertEqual(launchd.installed_commit(self.state), "v0.1.0")
        self.version("v0.2.0-rc.1\n")
        self.assertEqual(launchd.installed_commit(self.state), "v0.2.0-rc.1")
        self.version("0.1.0\n")
        self.assertIsNone(launchd.installed_commit(self.state))

    def test_render_line(self):
        doc = {"daemon": {"running": False}, "accounts": [
            {"account_id": "acc-1", "label": "a", "source": "live", "five_hour": None, "seven_day": None}]}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        plain = render(doc, now, timezone.utc, 120)
        stale = render(doc, now, timezone.utc, 120, stale_install=True)
        self.assertNotIn(cli.STALE_INSTALL, plain)
        self.assertEqual(stale[1], cli.STALE_INSTALL)
        self.assertEqual(stale[:1] + stale[2:], plain)
        self.assertEqual(cli.STALE_INSTALL, "설치본이 오래됨 — rotator install 다시 실행")
        empty = render({}, now, timezone.utc, 120, stale_install=True)
        self.assertEqual(empty[1], cli.STALE_INSTALL)
        for line in render(doc, now, timezone.utc, 20, stale_install=True):
            self.assertLessEqual(cli.display_width(line, False), 20)


class TestStaleInstallWiring(unittest.TestCase):
    def setUp(self):
        self.env = standard_env()
        self.dir = scratch("stalecfg-" + self._testMethodName)
        self.cfg_path = os.path.join(self.dir, "config.json")
        env_patch = mock.patch.dict(os.environ, {"COLUMNS": "300", "LINES": "40"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def run_status(self, *argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        rc = main(["--config", self.cfg_path, "status"] + list(argv), make_deps=lambda cfg: self.env.deps(),
                  launchctl=FakeRunner(), uid=lambda: 501, stdout=out, stderr=err, tz=timezone.utc, **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_line_shown_when_stale(self):
        rc, out, _ = self.run_status(stale_check=lambda: True)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[1], cli.STALE_INSTALL)

    def test_no_line_when_current(self):
        rc, out, _ = self.run_status(stale_check=lambda: False)
        self.assertEqual(rc, 0)
        self.assertNotIn(cli.STALE_INSTALL, out)

    def test_check_error_is_silent(self):
        def boom():
            raise OSError("x")
        rc, out, err = self.run_status(stale_check=boom)
        self.assertEqual(rc, 0)
        self.assertNotIn(cli.STALE_INSTALL, out)
        self.assertEqual(err, "")

    def test_injected_deps_never_run_the_real_check(self):
        with mock.patch("rotator.launchd.install_stale", side_effect=AssertionError("real check")) as real:
            rc, out, _ = self.run_status()
        self.assertEqual(rc, 0)
        real.assert_not_called()
        self.assertNotIn(cli.STALE_INSTALL, out)

    def test_real_deps_use_the_real_check(self):
        seen = []
        with mock.patch("rotator.cli.platform.system", return_value="Darwin"), \
                mock.patch("rotator.launchd.install_stale", lambda *a, **k: seen.append(a) or True):
            self.assertIs(cli._real_stale_check(load_config(self.cfg_path))(), True)
        self.assertEqual(seen, [(launchd.plist_path_default(), CONFIG_DIR_DEFAULT, launchd.repo_root())])

    def test_real_check_dispatches_to_schtasks_on_windows(self):
        seen = []
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.schtasks.install_stale", lambda *a, **k: seen.append(a) or True):
            self.assertIs(cli._real_stale_check(load_config(self.cfg_path))(), True)
        self.assertEqual(seen, [(schtasks.run_schtasks, CONFIG_DIR_DEFAULT, launchd.repo_root())])

    def test_json_has_no_line(self):
        rc, out, _ = self.run_status("--json", stale_check=lambda: True)
        self.assertEqual(rc, 0)
        self.assertNotIn(cli.STALE_INSTALL, out)
        self.assertEqual(set(json.loads(out)), set(STATUS_KEYS))

    def test_watch_shows_line(self):
        calls = []

        def check():
            calls.append(1)
            return True
        keys = iter(["q"])
        rc, out, _ = self.run_status("--watch", "--interval", "1", stale_check=check,
                                     read_key=lambda t: next(keys, "q"))
        self.assertEqual(rc, 0)
        self.assertIn(cli.STALE_INSTALL, out)
        self.assertEqual(len(calls), 1)


# --- Task 11 ---------------------------------------------------------------------------------------------


class StatusBase(unittest.TestCase):
    def setUp(self):
        self.env = standard_env()
        self.cfg = make_cfg()
        self.deps = self.env.deps()

    @property
    def state_path(self):
        return os.path.join(self.cfg.state_dir, "state.json")

    def doc(self, usage=None):
        return build_status(self.cfg, self.deps, self.env.now_v, usage=usage)

    def account(self, doc, aid):
        return next(a for a in doc["accounts"] if a["account_id"] == aid)

    def put_record(self, rec):
        self.env.files[record_path(self.cfg.state_dir, rec["session_id"], rec["pid"])] = json.dumps(rec)


class TestBuildStatus(StatusBase):
    def test_keys_pinned(self):
        self.env.sessions = [sess(101, ACC_A)]
        doc = self.doc()
        self.assertEqual(set(doc), set(STATUS_KEYS))
        self.assertEqual(len(STATUS_KEYS), 8)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(set(doc["daemon"]), {"running", "pid", "snapshot_age_s", "flags"})
        self.assertEqual(set(doc["accounts"][0]), {"account_id", "source", "error", "fetched_at", "five_hour",
                                                   "seven_day", "fable", "token_expires_at", "label", "group",
                                                   "plan", "pointer",
                                                   "sessions",
                                                   "refresh"})
        self.assertEqual(set(doc["sessions"][0]), {
            "pid", "session_id", "status", "account_id", "decision", "would_switch", "in_flight", "in_flight_pid",
            "stuck", "unswitchable", "hard_hold_until", "excluded", "record_state", "last_result", "hint",
            "post_verify", "model"})

    def test_fresh_snapshot_no_fetch(self):
        self.env.alive_pids = {4242}
        for age in (30, 59):
            with self.subTest(age=age):
                self.env.files[self.state_path] = snapshot_doc(self.env, T0 - timedelta(seconds=age))
                doc = self.doc()
                self.assertEqual(self.env.fetches(), [])
                self.assertIs(doc["daemon"]["running"], True)
                self.assertEqual(doc["daemon"]["pid"], 4242)
                self.assertEqual(doc["daemon"]["snapshot_age_s"], float(age))
                self.assertEqual(self.account(doc, ACC_A)["five_hour"]["utilization"], 33.0)

    def test_stale_snapshot_fetches(self):
        self.env.alive_pids = {4242}
        self.env.files[self.state_path] = snapshot_doc(self.env, T0 - timedelta(seconds=60))
        self.doc()
        self.assertEqual(len(self.env.fetches()), 3)
        self.env.calls.clear()
        self.env.alive_pids = set()
        self.env.files[self.state_path] = snapshot_doc(self.env, T0 - timedelta(seconds=5))
        doc = self.doc()
        self.assertIs(doc["daemon"]["running"], False)
        self.assertEqual(len(self.env.fetches()), 0)  # the first call's readings are in the shared usage files
        self.assertTrue(all(a["source"] != "unavailable" for a in doc["accounts"]))
        self.env.advance(30.0)  # usage_poll_s later every stored reading is due again
        self.doc()
        self.assertEqual(len(self.env.fetches()), 3)

    def test_non_finite_snapshot_utilization_renders_as_unknown(self):
        self.env.alive_pids = {4242}
        snap = json.loads(snapshot_doc(self.env, T0 - timedelta(seconds=5)))
        snap["accounts"][0]["five_hour"]["utilization"] = float("nan")
        snap["accounts"][0]["seven_day"]["utilization"] = float("inf")
        text = json.dumps(snap)
        self.assertIn("NaN", text)  # json.loads accepts the literal, so the snapshot can carry it
        self.env.files[self.state_path] = text
        doc = self.doc()
        lines = render(doc, T0, TZ9, 200)
        shown = [tuple(cells(l)[3:5]) for l in account_rows(lines)]
        self.assertEqual(shown.count(("-", "-")), 1, shown)  # the other accounts keep their numbers
        self.assertTrue(all("33%" in five for five, _ in shown if five != "-"), shown)
        self.assertEqual(len({cli.display_width(l) for l in tables(lines)[0]}), 1)

    def test_partial_snapshot_fetches_only_missing(self):
        self.env.alive_pids = {4242}
        self.env.files[self.state_path] = snapshot_doc(self.env, T0 - timedelta(seconds=5), accounts=(ACC_A, ACC_B))
        self.doc()
        self.assertEqual(self.env.fetches(), [("fetch", ACC_C)])

    def test_shared_usage_cache(self):
        c = UsageCache(self.cfg.usage_poll_s)
        self.doc(usage=c)
        self.assertEqual(len(self.env.fetches()), 3)
        self.env.advance(1)
        self.doc(usage=c)
        self.assertEqual(len(self.env.fetches()), 3)
        self.env.advance(29)
        self.doc(usage=c)
        self.assertEqual(len(self.env.fetches()), 6)

    def test_shared_cache_raising_fetch(self):
        self.env.fetch_raises = {ACC_B}
        c = UsageCache(self.cfg.usage_poll_s)
        doc = self.doc(usage=c)
        b = self.account(doc, ACC_B)
        self.assertEqual((b["source"], b["error"]), ("unavailable", "request_error;cache_missing"))
        self.env.advance(1)
        self.doc(usage=c)
        self.assertEqual(self.env.fetches().count(("fetch", ACC_B)), 1)

    def test_rate_limited_passthrough(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        c = UsageCache(self.cfg.usage_poll_s)
        b = self.account(self.doc(usage=c), ACC_B)
        self.assertEqual((b["source"], b["error"]), ("cache", "http_429"))
        self.assertEqual(b["fetched_at"], (T0 - timedelta(minutes=10)).isoformat())
        self.env.advance(30)
        self.doc(usage=c)
        self.assertEqual(self.env.fetches().count(("fetch", ACC_B)), 1)
        self.env.advance(30)
        self.doc(usage=c)
        self.assertEqual(self.env.fetches().count(("fetch", ACC_B)), 2)

    def test_no_snapshot(self):
        self.env.fetch_raises = {ACC_C}
        doc = self.doc()
        self.assertIs(doc["daemon"]["running"], False)
        self.assertIsNone(doc["daemon"]["pid"])
        self.assertIsNone(doc["daemon"]["snapshot_age_s"])
        self.assertEqual(len(self.env.fetches()), 3)
        self.assertEqual(self.account(doc, ACC_C)["source"], "unavailable")

    def test_garbage_snapshot(self):
        self.env.files[self.state_path] = "[1, 2"
        doc = self.doc()
        self.assertIs(doc["daemon"]["running"], False)
        self.assertEqual(len(self.env.fetches()), 3)

    def test_pointer_marker(self):
        self.env.pointer_text = prof(ACC_A).home
        doc = self.doc()
        self.assertEqual([a["account_id"] for a in doc["accounts"] if a["pointer"]], [ACC_A])
        self.assertEqual(doc["pointer_account_id"], ACC_A)

    def test_unmanaged_listed(self):
        um = Session(pid=555, session_id=SID2, cwd="/Users/user/proj", status="idle", config_dir="/Users/user/.claude",
                     argv=("claude",), terminal_handle=None, started_at=None)
        self.env.sessions = [sess(101, ACC_A), um]
        doc = self.doc()
        self.assertEqual(doc["unmanaged_sessions"], [{"pid": 555, "session_id": SID2, "status": "idle", "model": None}])
        self.assertEqual([r["pid"] for r in doc["sessions"]], [101])
        self.assertEqual([x["pid"] for a in doc["accounts"] for x in a["sessions"]], [101])

    def test_model_on_every_row(self):
        um = Session(pid=555, session_id=SID2, cwd="/Users/user/proj", status="idle", config_dir="/Users/user/.claude",
                     argv=("claude",), terminal_handle=None, started_at=None, model="claude-opus-5-5")
        self.env.sessions = [dataclasses.replace(sess(101, ACC_A), model="claude-fable-5-1"), um]
        doc = self.doc()
        self.assertEqual(doc["sessions"][0]["model"], "claude-fable-5-1")
        self.assertEqual(doc["unmanaged_sessions"][0]["model"], "claude-opus-5-5")
        self.assertEqual(self.account(doc, ACC_A)["sessions"], [{"pid": 101, "status": "idle", "model": "claude-fable-5-1"}])

    def test_fable_in_accounts(self):
        self.env.usages[ACC_A] = dataclasses.replace(live(ACC_A, 20.0), fable=UsageWindow(25.0, T0 + timedelta(days=5)))
        doc = self.doc()
        self.assertEqual(self.account(doc, ACC_A)["fable"],
                         {"utilization": 25.0, "resets_at": "2026-11-02T12:00:00+00:00"})
        self.assertIsNone(self.account(doc, ACC_B)["fable"])

    def test_discover_error_contained(self):
        def boom(homes):
            raise RuntimeError("SECRET-PROMPT-TEXT")

        self.env.sessions = [sess(101, ACC_A)]
        deps = self.env.deps(discover_sessions=boom)
        with self.assertLogs("rotator.cli", "ERROR") as cm:
            doc = build_status(self.cfg, deps, self.env.now_v)
        self.assertEqual(doc["sessions"], [])
        self.assertEqual(doc["unmanaged_sessions"], [])
        self.assertTrue(doc["accounts"])
        for a in doc["accounts"]:
            self.assertEqual(a["sessions"], [])
        msgs = [r.getMessage() for r in cm.records]
        self.assertTrue(any("event=discover_error what=sessions exc=RuntimeError" in m for m in msgs), msgs)
        self.assertFalse(any("SECRET-PROMPT-TEXT" in m for m in msgs))

    def test_snapshot_row_merged_only_for_same_session(self):
        self.env.alive_pids = {4242}
        row = {"pid": 101, "session_id": SID1, "decision": {"action": "hard", "target_account_id": ACC_B,
                                                           "reason": "x"}, "in_flight": True, "in_flight_pid": 101}
        self.env.files[self.state_path] = snapshot_doc(self.env, T0 - timedelta(seconds=5), sessions=[row])
        self.env.sessions = [sess(101, ACC_A)]
        r = self.doc()["sessions"][0]
        self.assertEqual(r["decision"]["action"], "hard")
        self.assertIs(r["in_flight"], True)
        self.env.sessions = [sess(101, ACC_A, sid=SID2)]
        r = self.doc()["sessions"][0]
        self.assertIsNone(r["decision"])
        self.assertIs(r["in_flight"], False)

    def test_record_fields_and_hints(self):
        cmd = "cd /x && claude --resume y"
        self.put_record(stored_record(101, res=result("exited", "exit_timeout", cmd=cmd),
                                      hint=STATE_HINTS["exited"], touched=True))
        self.put_record(stored_record(102, sid=SID2, record_state="in_progress", result=None, owner_pid=4243))
        self.env.sessions = [sess(101, ACC_A), sess(102, ACC_A, sid=SID2, handle="term_00000002")]
        rows = {r["pid"]: r for r in self.doc()["sessions"]}
        self.assertEqual(rows[101]["last_result"]["relaunch_command"], cmd)
        self.assertEqual(rows[101]["hint"], STATE_HINTS["exited"])
        self.assertEqual(rows[102]["hint"], ORPHANED_HINT)
        self.assertEqual(rows[102]["record_state"], "in_progress")
        self.env.alive_pids = {4243}
        rows = {r["pid"]: r for r in self.doc()["sessions"]}
        self.assertEqual(rows[102]["hint"], IN_PROGRESS_HINT)

    def test_attention(self):
        cmd = "cd /x && claude --resume y"
        hour_ago = (T0 - timedelta(hours=1)).isoformat()
        self.put_record(stored_record(777, sid="77777777-7777-4777-8777-777777777777",
                                      res=result("exited", "exit_timeout", cmd=cmd), touched=True, updated_at=hour_ago))
        self.put_record(stored_record(778, res=result("exited", "exit_timeout", cmd=cmd), touched=True,
                                      updated_at=(T0 - timedelta(hours=25)).isoformat()))
        self.put_record(stored_record(779, res=result("continued", None, ok=True), touched=True))
        self.put_record(stored_record(780))
        self.put_record(stored_record(781, record_state="in_progress", result=None, touched=True))
        att = {a["pid"]: a for a in self.doc()["attention"]}
        self.assertEqual(set(att), {777, 781})
        self.assertEqual(att[777]["last_result"]["relaunch_command"], cmd)
        self.assertEqual(att[781]["hint"], ORPHANED_HINT)
        self.assertEqual([a["pid"] for a in self.doc()["attention"]], [781, 777])

    def test_attention_hides_failures_of_sessions_running_again(self):
        cmd = "cd /x && claude --resume y"
        hour_ago = (T0 - timedelta(hours=1)).isoformat()
        sid_back, sid_gone = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        self.put_record(stored_record(791, sid=sid_back, res=result("exited", "exit_timeout", cmd=cmd),
                                      touched=True, updated_at=hour_ago))
        self.put_record(stored_record(792, sid=sid_gone, res=result("exited", "exit_timeout", cmd=cmd),
                                      touched=True, updated_at=hour_ago))
        self.env.sessions = [sess(9101, ACC_A, sid=sid_back)]  # same session, new process
        att = {a["pid"] for a in self.doc()["attention"]}
        self.assertNotIn(791, att)
        self.assertIn(792, att)

    def test_attention_hides_failures_superseded_by_a_later_record(self):
        cmd = "cd /x && claude --resume y"
        two_h, one_h = (T0 - timedelta(hours=2)).isoformat(), (T0 - timedelta(hours=1)).isoformat()
        sid_later, sid_alone = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
        self.put_record(stored_record(794, sid=sid_later, res=result("exited", "exit_timeout", cmd=cmd),
                                      touched=True, updated_at=two_h))
        self.put_record(stored_record(795, sid=sid_later, res=result("continued", None, ok=True), touched=True,
                                      updated_at=one_h))  # the session ran again after the failure
        self.put_record(stored_record(796, sid=sid_alone, res=result("exited", "exit_timeout", cmd=cmd),
                                      touched=True, updated_at=one_h))
        self.put_record(stored_record(797, sid=sid_alone, res=result("continued", None, ok=True), touched=True,
                                      updated_at=two_h))  # earlier than the failure: no proof
        self.env.sessions = []
        self.assertEqual([a["pid"] for a in self.doc()["attention"]], [796])

    def test_attention_kept_when_no_session_runs(self):
        hour_ago = (T0 - timedelta(hours=1)).isoformat()
        self.put_record(stored_record(793, res=result("exited", "exit_timeout", cmd="cd /x"), touched=True,
                                      updated_at=hour_ago))
        self.env.sessions = []
        self.assertEqual([a["pid"] for a in self.doc()["attention"]], [793])

    def test_usage_warnings_not_printed(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        self.env.fetch_raises = {ACC_C}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            doc = self.doc()
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.account(doc, ACC_B)["error"], "http_429")

    def test_writes_only_the_shared_usage_files(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.doc()
        touched = [c for c in self.env.calls if c[0] in ("write", "lock")]
        usage_dir = os.path.join(self.cfg.state_dir, "usage") + os.sep
        locks_dir = os.path.join(self.cfg.state_dir, "locks", "u-")
        self.assertEqual(len(touched), 6)  # one lock and one write per account
        for kind, path in touched:
            self.assertTrue(path.startswith(usage_dir if kind == "write" else locks_dir), (kind, path))

    def test_discovery_error_propagates(self):
        self.env.profiles_raise = True
        with self.assertRaises(RuntimeError):
            self.doc()

    def test_plan_on_every_account(self):
        self.env.profiles = [dataclasses.replace(prof(ACC_A), rate_limit_tier="default_claude_max_5x"),
                             dataclasses.replace(prof(ACC_B), rate_limit_tier="default_raven"), prof(ACC_C)]
        doc = self.doc()
        self.assertEqual([a["plan"] for a in doc["accounts"]], ["Max5x", "Team", None])
        self.assertEqual([a["label"] for a in doc["accounts"]], ["user"] * 3)  # the label itself never carries plan

    def test_label_on_every_account(self):
        self.env.profiles = [prof(ACC_A), dataclasses.replace(prof(ACC_B), email=None, display_name="홍길동"),
                             dataclasses.replace(prof(ACC_C), email=None, display_name=None)]
        doc = self.doc()
        self.assertEqual([a["label"] for a in doc["accounts"]], ["user", "홍길동", "00000000"])

    def test_no_secrets(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.env.pointer_text = prof(ACC_A).home
        text = json.dumps(self.doc())
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn(ACC_A, text)


# --- dashboard cells -------------------------------------------------------------------------------------


class TestWidth(unittest.TestCase):
    def test_display_width(self):
        self.assertEqual(cli.display_width(""), 0)
        self.assertEqual(cli.display_width("abc"), 3)
        self.assertEqual(cli.display_width("홍길동"), 6)
        self.assertEqual(cli.display_width("ｶ"), 1)  # halfwidth katakana is narrow
        self.assertEqual(cli.display_width("Ａ"), 2)  # fullwidth latin
        self.assertEqual(cli.display_width("é"), 1)  # combining acute adds no column
        self.assertEqual(cli.display_width("a​b"), 2)  # zero-width space (Cf)

    def test_ambiguous_flag(self):
        for glyph in ("─", "│", "┼", "█", "▒", "…", "·", "≥"):
            with self.subTest(glyph=glyph):
                self.assertEqual(cli.display_width(glyph), 1)
                self.assertEqual(cli.display_width(glyph, ambiguous_wide=True), 2)
        for glyph in ("↻", "-", "|", "#", "."):
            with self.subTest(glyph=glyph):
                self.assertEqual(cli.display_width(glyph, ambiguous_wide=True), 1)

    def test_fit_pads_and_cuts_by_columns(self):
        self.assertEqual(cli.fit("ab", 4), "ab  ")
        self.assertEqual(cli.fit("ab", 4, align="right"), "  ab")
        self.assertEqual(cli.fit("abcd", 4), "abcd")
        self.assertEqual(cli.fit("abcdef", 4), "abc…")
        self.assertEqual(cli.fit("홍길동", 6), "홍길동")
        self.assertEqual(cli.fit("홍길동", 5), "홍길…")
        self.assertEqual(cli.fit("홍길동", 4), "홍… ")  # a wide char never straddles the edge
        self.assertEqual(cli.fit("abcdef", 4, ell="~"), "abc~")
        self.assertEqual(cli.fit("abcdef", 4, amb=True), "ab…")
        self.assertEqual(cli.display_width(cli.fit("abcdef", 4, amb=True), ambiguous_wide=True), 4)

    def test_fit_edges(self):
        self.assertEqual(cli.fit("", 3), "   ")
        self.assertEqual(cli.fit("abc", 0), "")
        self.assertEqual(cli.fit("abc", -2), "")
        self.assertEqual(cli.fit("홍", 1), "…")
        self.assertEqual(cli.fit("홍", 1, ell=""), " ")
        self.assertEqual(cli.fit("ab", 1), "…")
        self.assertEqual(cli.fit("ab", 1, amb=True), "a")  # a 2-column ellipsis cannot fit in one column
        self.assertEqual(cli.clip("abcdef", 3, ell=""), "abc")
        self.assertEqual(cli.clip("ab", 5), "ab")

    def test_fit_width_property(self):
        samples = ("", "a", "홍길동 user", "─│█▒…", "éé", "가a나b다c")
        for s in samples:
            for w in range(0, 12):
                for amb in (False, True):
                    with self.subTest(s=s, w=w, amb=amb):
                        self.assertEqual(cli.display_width(cli.fit(s, w, amb=amb), ambiguous_wide=amb), w)


class TestLabel(unittest.TestCase):
    def test_fallback_chain(self):
        p = prof(ACC_A)
        self.assertEqual(cli.account_label(p), "user")
        self.assertEqual(cli.account_label(dataclasses.replace(p, email=None, display_name="홍길동")), "홍길동")
        self.assertEqual(cli.account_label(dataclasses.replace(p, email="", display_name="홍길동")), "홍길동")
        self.assertEqual(cli.account_label(dataclasses.replace(p, email="@example.com", display_name="Kim")), "Kim")
        self.assertEqual(cli.account_label(dataclasses.replace(p, email=None, display_name=None)), "00000000")
        self.assertEqual(cli.account_label(dataclasses.replace(p, email=None, display_name="  ")), "00000000")

    def test_local_part_only(self):
        p = dataclasses.replace(prof(ACC_A), email="first.last+tag@example.com")
        self.assertEqual(cli.account_label(p), "first.last+tag")
        self.assertEqual(cli.account_label(dataclasses.replace(p, email="a@b@example.com")), "a")

    def test_control_chars_removed(self):
        p = dataclasses.replace(prof(ACC_A), email="us\x1b[31mer\n@example.com")
        self.assertEqual(cli.account_label(p), "us[31mer")
        q = dataclasses.replace(prof(ACC_A), email=None, display_name="\x07‎")
        self.assertEqual(cli.account_label(q), "00000000")


class TestCells(unittest.TestCase):
    def test_remaining_text(self):
        cases = [(-5, "1분 안"), (0, "1분 안"), (59.9, "1분 안"), (60, "1분 뒤"), (119.9, "1분 뒤"), (3599, "59분 뒤"),
                 (3600, "1시간 뒤"), (3659, "1시간 뒤"), (3660, "1시간 1분 뒤"), (4860, "1시간 21분 뒤"),
                 (4890, "1시간 21분 뒤"), (86399, "23시간 59분 뒤"), (86400, "1일 뒤"), (89999, "1일 뒤"),
                 (90000, "1일 1시간 뒤"), (6 * 86400 + 86399, "6일 23시간 뒤")]
        for secs, want in cases:
            with self.subTest(secs=secs):
                self.assertEqual(cli.remaining_text(secs), want)

    def test_age_text(self):
        cases = [(-3, "0초 전"), (0, "0초 전"), (59.9, "59초 전"), (60, "1분 전"), (3599, "59분 전"),
                 (3600, "1시간 전"), (86399, "23시간 전"), (86400, "1일 전"), (3 * 86400 + 5, "3일 전")]
        for secs, want in cases:
            with self.subTest(secs=secs):
                self.assertEqual(cli.age_text(secs), want)

    def test_reason_text(self):
        cases = [("token_expired;cache_missing", "토큰 만료·캐시 없음"), ("request_error;cache_missing", "요청 실패·캐시 없음"),
                 ("http_429", "호출 제한"), ("http_429;cache_missing", "호출 제한·캐시 없음"), ("http_500", "HTTP 500"),
                 ("token_missing", "토큰 없음"), ("network;cache_bad_json", "연결 실패·캐시 손상"),
                 ("timeout", "연결 실패"), ("bad_shape", "응답 이상"), ("weird_thing", "확인 불가"),
                 ("network;timeout", "연결 실패"), ("", ""), (None, "")]
        for err, want in cases:
            with self.subTest(err=err):
                self.assertEqual(cli.reason_text(err), want)
        self.assertEqual(cli.reason_text("token_expired;cache_missing", "/"), "토큰 만료/캐시 없음")

    def test_bar_proportional_and_capped(self):
        g = cli.GLYPHS_UNICODE
        cases = [(0, "▒" * 9), (10.9, "▒" * 9), (11.2, "█" + "▒" * 8), (45, "████▒▒▒▒▒"), (89.4, "████████▒"),
                 (99.9, "████████▒"), (100, "█" * 9), (104, "█" * 9), (-3, "▒" * 9)]
        for util, want in cases:
            with self.subTest(util=util):
                self.assertEqual(cli._bar(util, 9, g), want)
        self.assertEqual(cli._bar(50, 4, cli.GLYPHS_ASCII), "##..")

    def test_bar_width_same_for_any_fill(self):
        for g in (cli.GLYPHS_UNICODE, cli.GLYPHS_ASCII):
            for amb in (False, True):
                widths = {cli.display_width(cli._bar(u, 9, g), ambiguous_wide=amb) for u in (0, 50, 100)}
                self.assertEqual(len(widths), 1, (g.fill, amb))
                self.assertEqual(cli.display_width(g.fill, ambiguous_wide=amb),
                                 cli.display_width(g.empty, ambiguous_wide=amb))

    def test_pct_text(self):
        for util, want in ((0, "  0%"), (5.99, "  5%"), (89.4, " 89%"), (100, "100%"), (-1, "  0%")):
            with self.subTest(util=util):
                self.assertEqual(cli._pct_text(util), want)

    def test_window_state(self):
        w = {"utilization": 42.0, "resets_at": (T0 + timedelta(hours=1)).isoformat()}
        self.assertEqual(cli._window_state(w, T0), (42.0, 3600.0))
        self.assertEqual(cli._window_state(w, T0 + timedelta(hours=1)), (0.0, None))  # reset passed: 0%, no ↻
        self.assertEqual(cli._window_state(dict(w, resets_at=None), T0), (42.0, None))
        self.assertEqual(cli._window_state(dict(w, resets_at="garbage"), T0), (42.0, None))
        self.assertIsNone(cli._window_state(None, T0))
        self.assertIsNone(cli._window_state({"utilization": "x"}, T0))
        self.assertIsNone(cli._window_state({"utilization": True}, T0))
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(cli._window_state(dict(w, utilization=bad), T0), bad)


# --- Task 12 ---------------------------------------------------------------------------------------------

TZ9 = timezone(timedelta(hours=9))


def render_doc():
    return {
        "schema": 1, "generated_at": T0_ISO,
        "daemon": {"running": True, "pid": 4242, "snapshot_age_s": 3.0, "flags": {"dry_run": False}},
        "accounts": [
            {"account_id": ACC_A, "label": "alice", "source": "live", "error": None,
             "fetched_at": (T0 - timedelta(seconds=12)).isoformat(),
             "five_hour": {"utilization": 42.0, "resets_at": "2026-10-28T03:00:00+00:00"},
             "seven_day": {"utilization": 10.0, "resets_at": (T0 + timedelta(hours=1, minutes=12)).isoformat()},
             "pointer": True, "sessions": [{"pid": 101, "status": "idle"}]},
            {"account_id": ACC_B, "label": "bob", "source": "unavailable", "error": "token_expired;cache_missing",
             "fetched_at": T0_ISO, "five_hour": None, "seven_day": None, "pointer": False, "sessions": []},
        ],
        "pointer_account_id": ACC_A,
        "sessions": [{"pid": 101, "session_id": SID1, "status": "idle", "account_id": ACC_A,
                      "decision": {"action": "hard", "target_account_id": ACC_B, "reason": "x"},
                      "would_switch": None, "in_flight": False, "in_flight_pid": None, "stuck": False,
                      "unswitchable": None, "hard_hold_until": None, "excluded": [], "record_state": "final",
                      "last_result": result_dict(result("exited", "exit_timeout", step="wait_exit",
                                                        cmd="cd /x && claude --resume y")),
                      "hint": STATE_HINTS["exited"], "post_verify": None}],
        "unmanaged_sessions": [{"pid": 555, "session_id": SID2, "status": "busy"}],
        "attention": [{"pid": 777, "session_id": SID1, "updated_at": T0_ISO, "record_state": "final",
                       "last_result": result_dict(result("exited", "exit_timeout", cmd="cd /y && claude")),
                       "hint": "look"}],
    }


def win(pct, delta):
    return {"utilization": pct, "resets_at": (T0 + delta).isoformat()}


RELAUNCH_LINES = {"cd /x && claude --resume y", "cd /y && claude"}  # render_doc's commands: printed whole


def dashboard_doc():
    """Every row kind the table draws: 89% / 100% live, cache, unavailable, Korean and over-long labels."""
    doc = render_doc()
    acc = lambda n: "00000000-0000-0000-0000-0000000000%02d" % n  # noqa: E731
    doc["accounts"] = [
        {"account_id": acc(1), "label": "alice", "plan": "Max5x", "source": "live", "error": None,
         "fetched_at": (T0 - timedelta(seconds=3)).isoformat(), "five_hour": win(89.4, timedelta(hours=1, minutes=21)),
         "seven_day": win(45.0, timedelta(days=6)), "fable": win(25.0, timedelta(days=6)), "pointer": True,
         "sessions": [{"pid": n, "status": "idle"} for n in range(9)]},
        {"account_id": acc(2), "label": "bob", "plan": "Team", "source": "live", "error": None,
         "fetched_at": (T0 - timedelta(seconds=3)).isoformat(), "five_hour": win(100.0, timedelta(minutes=21)),
         "seven_day": win(37.0, timedelta(days=6)), "fable": None, "pointer": False, "sessions": []},
        {"account_id": acc(3), "label": "carol", "source": "cache", "error": "http_429",
         "fetched_at": (T0 - timedelta(days=3)).isoformat(), "five_hour": win(70.0, timedelta(hours=-2)),
         "seven_day": win(28.0, timedelta(days=2)), "fable": None, "pointer": False, "sessions": []},
        {"account_id": acc(4), "label": "dave", "source": "unavailable", "error": "token_expired;cache_missing",
         "fetched_at": T0_ISO, "five_hour": None, "seven_day": None, "fable": None, "pointer": False, "sessions": []},
        {"account_id": acc(5), "label": "홍길동", "source": "live", "error": None,
         "fetched_at": (T0 - timedelta(minutes=2)).isoformat(), "five_hour": win(0.0, timedelta(hours=4)),
         "seven_day": win(96.0, timedelta(days=1)), "fable": win(100.0, timedelta(days=1)), "pointer": False,
         "sessions": []},
        {"account_id": acc(6), "label": "a-very-long-account-name-indeed", "source": "live", "error": None,
         "fetched_at": (T0 - timedelta(seconds=40)).isoformat(), "five_hour": win(12.0, timedelta(hours=2)),
         "seven_day": win(3.0, timedelta(days=3)), "fable": None, "pointer": False, "sessions": []},
        {"account_id": acc(7), "label": "erin", "source": "cache", "error": "token_expired",
         "fetched_at": (T0 - timedelta(hours=23)).isoformat(), "five_hour": win(64.0, timedelta(hours=3, minutes=5)),
         "seven_day": win(51.0, timedelta(days=4, hours=2)), "fable": None, "pointer": False, "sessions": [],
         "refresh": {"state": "stopped", "reason": "invalid_grant", "at": None, "last_refreshed_at": None}},
        {"account_id": acc(8), "label": "frank", "source": "cache", "error": "token_expired",
         "fetched_at": (T0 - timedelta(days=12)).isoformat(), "five_hour": win(20.0, timedelta(seconds=30)),
         "seven_day": win(77.0, timedelta(days=5)), "fable": None, "pointer": False,
         "sessions": [{"pid": 900, "status": "busy"}]},
        {"account_id": acc(9), "label": "gina", "source": "unavailable", "error": "token_expired;cache_missing",
         "fetched_at": T0_ISO, "five_hour": None, "seven_day": None, "fable": None, "pointer": False, "sessions": [],
         "refresh": {"state": "failed", "reason": "write_failed", "at": None, "last_refreshed_at": None}},
    ]
    return doc


def daemon_off(doc):
    """The same document as a status run with the daemon off: every refresh record is daemon_off."""
    doc["daemon"] = {"running": False, "pid": None, "snapshot_age_s": None, "flags": None}
    for a in doc["accounts"]:
        a["refresh"] = {"state": "skipped", "reason": "daemon_off", "at": None, "last_refreshed_at": None}
    return doc


BORDERS = {False: set("│┌┬┐├┼┤└┴┘"), True: set("|+")}
SGR = re.compile(r"\x1b\[[0-9;]*m")


def tables(lines, ascii=False):
    """Consecutive runs of boxed lines (the accounts table, then the sessions table)."""
    out, cur = [], []
    for line in lines:
        if line[:1] in BORDERS[ascii]:
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def border_cols(line, amb, ascii=False):
    cols, col = [], 0
    for ch in SGR.sub("", line):
        if ch in BORDERS[ascii]:
            cols.append(col)
        col += cli.display_width(ch, ambiguous_wide=amb)
    return cols


def cells(line, ascii=False):
    return [c.strip() for c in SGR.sub("", line).split("|" if ascii else "│")[1:-1]]


def account_rows(lines, ascii=False):
    """Data rows of the accounts table, in document order."""
    return tables(lines, ascii)[0][3:-1]


class TestRender(unittest.TestCase):
    def lines(self, doc=None, now=T0, width=200, **kw):
        return render(render_doc() if doc is None else doc, now, TZ9, width, **kw)

    def row(self, lines, label):
        return next(cells(l) for l in account_rows(lines) if cells(l)[1] == label)

    def session_row(self, lines, pid):
        return next(cells(l) for l in tables(lines)[1][3:-1] if cells(l)[0] == str(pid))

    def test_title_line(self):
        first = self.lines()[0]
        self.assertTrue(first.startswith("Claude 계정 사용량"))
        self.assertTrue(first.endswith("21:00:00  데몬: 켜짐 (pid 4242)"), first)
        doc = render_doc()
        doc["daemon"]["flags"] = {"dry_run": True}
        self.assertTrue(self.lines(doc)[0].endswith("데몬: 켜짐 (pid 4242) 드라이런"))
        doc["daemon"] = {"running": False, "pid": None, "snapshot_age_s": None, "flags": None}
        self.assertTrue(self.lines(doc)[0].endswith("21:00:00  데몬: 꺼짐"))
        top = tables(self.lines())[0][0]
        self.assertEqual(cli.display_width(first), cli.display_width(top))  # the clock sits at the table's edge

    def test_header_and_legend(self):
        lines = self.lines()
        self.assertEqual(cells(tables(lines)[0][1]), ["", "계정", "플랜", "5시간", "주간", "페이블", "세션", "갱신"])
        self.assertEqual(cells(tables(lines)[1][1]), ["PID", "계정", "상태", "모델", "전환"])
        self.assertIn("* = Orca 선택 계정   노랑 ≥85%  빨강 ≥95%", lines)
        self.assertIn("* = Orca 선택 계정   노랑 ≥70%  빨강 ≥90.5%", self.lines(soft_pct=70.0, hard_pct=90.5))

    def test_pointer_marker_only_on_pointer(self):
        lines = self.lines()
        self.assertEqual(self.row(lines, "alice")[0], "*")
        self.assertEqual(self.row(lines, "bob")[0], "")

    def test_label_fallback_in_render(self):
        doc = render_doc()
        del doc["accounts"][1]["label"]
        self.assertEqual([cells(l)[1] for l in account_rows(self.lines(doc))], ["00000000", "alice"])  # sorted by label
        doc["accounts"][0]["label"] = "a\x1b[31mlice"
        self.assertNotIn("\x1b", "".join(self.lines(doc)))

    def test_label_truncated_to_18(self):
        doc = render_doc()
        doc["accounts"][0]["label"] = "a-very-long-account-name-indeed"
        # A wide terminal shows the whole label; the 18-column cap applies only once the table no longer fits.
        self.assertEqual(self.row(self.lines(doc), "a-very-long-account-name-indeed")[1],
                         "a-very-long-account-name-indeed")
        self.assertEqual(self.row(self.lines(doc, width=100), "a-very-long-accou…")[1], "a-very-long-accou…")
        self.assertEqual(cli.display_width("a-very-long-accou…"), 18)

    def test_unavailable_with_error(self):
        row = self.row(self.lines(), "bob")
        self.assertEqual(row[2:], ["-", "-  (토큰 만료·캐시 없음)", "-", "-", "0", "갱신 대기"])

    def test_live_cell_bar_percent_remaining(self):
        doc = render_doc()
        doc["accounts"][0]["five_hour"] = win(89.4, timedelta(hours=1, minutes=21, seconds=30))
        doc["accounts"][0]["fable"] = win(25.0, timedelta(days=5))
        row = self.row(self.lines(doc), "alice")
        self.assertEqual(row[3], "████████▒  89% · 1시간 21분 뒤")
        self.assertEqual(row[4], "▒▒▒▒▒▒▒▒▒  10% · 1시간 12분 뒤")
        self.assertEqual(row[5], "█▒▒▒  25%")  # the fable cell never shows a reset
        self.assertEqual(row[6], "1")

    def test_countdown(self):
        row = self.row(self.lines(), "alice")
        self.assertEqual(row[3], "▒▒▒▒▒▒▒▒▒   0%")  # the 5h window reset at 03:00 UTC: 0% and no reset text
        doc = render_doc()
        doc["accounts"][0]["five_hour"]["resets_at"] = None
        self.assertEqual(self.row(self.lines(doc), "alice")[3], "███▒▒▒▒▒▒  42%")  # floor(42 * 9 / 100) = 3 cells
        at = self.row(self.lines(now=T0 + timedelta(hours=1, minutes=12)), "alice")
        self.assertEqual(at[4], "▒▒▒▒▒▒▒▒▒   0%")
        early = self.row(self.lines(now=T0 + timedelta(hours=1, minutes=11, seconds=30)), "alice")
        self.assertEqual(early[4], "▒▒▒▒▒▒▒▒▒  10% · 1분 안")

    def test_age_from_now(self):
        self.assertEqual(self.row(self.lines(), "alice")[7], "12초 전")
        later = self.lines(now=T0 + timedelta(seconds=5))
        self.assertEqual(self.row(later, "alice")[7], "17초 전")
        self.assertEqual(self.row(later, "bob")[7], "갱신 대기")
        doc = render_doc()
        doc["accounts"][0]["fetched_at"] = (T0 + timedelta(seconds=3)).isoformat()
        self.assertEqual(self.row(self.lines(doc), "alice")[7], "0초 전")
        doc["accounts"][0]["fetched_at"] = None
        self.assertEqual(self.row(self.lines(doc), "alice")[7], "-")

    def test_rate_limited_label(self):
        cases = [({"source": "cache", "error": "http_429", "fetched_at": (T0 - timedelta(minutes=10)).isoformat(),
                   "five_hour": win(50.0, timedelta(hours=1))},
                  "████▒▒▒▒▒  50% · 1시간 뒤", "10분 전 · 캐시 · 호출 제한"),
                 ({"source": "unavailable", "error": "http_429;cache_missing"}, "-  (호출 제한·캐시 없음)", "-"),
                 ({"source": "cache", "error": "http_500", "fetched_at": T0_ISO, "five_hour": win(5.0, timedelta(0))},
                  "▒▒▒▒▒▒▒▒▒   0%", "0초 전 · 캐시 · HTTP 500"),
                 ({"source": "cache", "error": None, "fetched_at": (T0 - timedelta(days=3)).isoformat(),
                   "five_hour": win(33.0, timedelta(days=2, minutes=59))}, "██▒▒▒▒▒▒▒  33% · 2일 뒤", "3일 전 · 캐시")]
        for patch, five, age in cases:
            with self.subTest(patch=patch):
                doc = render_doc()
                doc["accounts"][1].update(patch)
                row = self.row(self.lines(doc), "bob")
                self.assertEqual((row[3], row[7]), (five, age))

    def test_fable_column(self):
        doc = render_doc()
        doc["accounts"][0]["fable"] = win(100.0, timedelta(days=5))
        lines = self.lines(doc)
        self.assertEqual(self.row(lines, "alice")[5], "████ 100%")
        self.assertEqual(self.row(lines, "bob")[5], "-")
        doc["accounts"][1]["fable"] = None
        self.assertEqual(self.row(self.lines(doc), "bob")[5], "-")

    def test_blocks_and_relaunch(self):
        lines = self.lines(details=True)
        self.assertEqual(self.session_row(lines, 101),
                         ["101", "alice", "idle", "-", "즉시 전환 → bob · 최근 실패 (종료 시간 초과)"])
        self.assertEqual(self.session_row(lines, 555), ["555", "(관리 밖)", "busy", "-", "-"])
        self.assertEqual(lines[lines.index("  pid 101 재실행:") + 1], "cd /x && claude --resume y")
        self.assertIn("주의: 확인이 필요한 전환 1건", lines)
        self.assertTrue(any(l.startswith("  pid 777 · ") and l.endswith(" · 종료 후 재실행 미확인 (종료 시간 초과)")
                            for l in lines), lines)
        self.assertIn("  pid 777 힌트: look", lines)
        self.assertEqual(lines[lines.index("  pid 777 재실행:") + 1], "cd /y && claude")
        self.assertTrue(any(l.startswith("  pid 101 힌트: Claude Code exited") for l in lines))

    def test_compact_by_default(self):
        lines = self.lines()
        self.assertFalse(any("힌트:" in l or "재실행:" in l for l in lines))
        self.assertFalse(RELAUNCH_LINES & set(lines))
        self.assertIn("주의: 확인이 필요한 전환 1건", lines)
        self.assertEqual(len([l for l in lines if l.startswith("  pid ")]), 1)
        self.assertEqual(lines[-1], "자세히 (안내, 재실행 명령): rotator status --details")
        self.assertNotIn("자세히 (안내, 재실행 명령): rotator status --details", self.lines(details=True))

    def test_details_pointer_only_when_something_is_hidden(self):
        doc = render_doc()
        doc["attention"] = []
        doc["sessions"][0]["last_result"] = {"ok": False, "state": "untouched", "reason": "screen_unreadable"}
        self.assertFalse(any("--details" in l for l in self.lines(doc)))
        doc["sessions"][0]["last_result"] = {"ok": False, "state": "relaunched", "reason": "prompt_unconfirmed"}
        self.assertIn("자세히 (안내, 재실행 명령): rotator status --details", self.lines(doc))
        doc["sessions"][0].update(last_result=None, record_state="in_progress", hint=ORPHANED_HINT)
        lines = self.lines(doc)
        self.assertIn("전환 중단됨 (창 확인 필요)", self.session_row(lines, 101)[4])
        self.assertIn("자세히 (안내, 재실행 명령): rotator status --details", lines)
        doc["sessions"][0]["hint"] = IN_PROGRESS_HINT
        lines = self.lines(doc)
        self.assertTrue(self.session_row(lines, 101)[4].startswith("전환 중"))
        self.assertFalse(any("--details" in l for l in lines))

    def test_attention_without_result(self):
        doc = render_doc()
        base = dict(doc["attention"][0], last_result=None, record_state="in_progress")
        doc["attention"] = [dict(base, pid=20, hint=ORPHANED_HINT), dict(base, pid=21, hint=IN_PROGRESS_HINT),
                            dict(base, pid=22, record_state="final", hint="")]
        lines = self.lines(doc)
        self.assertTrue(any(l.startswith("  pid 20 · ") and l.endswith("전환 중단됨 (창 확인 필요)") for l in lines), lines)
        self.assertTrue(any(l.startswith("  pid 21 · ") and l.endswith("전환 중 (입력 금지)") for l in lines), lines)
        self.assertTrue(any(l.startswith("  pid 22 · ") and l.endswith("결과 없음") for l in lines), lines)

    def test_ascii_compact(self):
        for l in self.lines(dashboard_doc(), ascii=True):
            self.assertTrue(all(ch.isascii() or "가" <= ch <= "힣" for ch in l), l)

    def test_result_text_summaries(self):
        tz, lab = TZ9, (lambda aid: "bob")
        cases = [({}, "-"),
                 ({"decision": {"action": "none"}}, "-"),
                 ({"last_result": {"ok": False, "state": "untouched", "reason": "screen_unreadable"}}, "-"),
                 ({"last_result": {"ok": True, "state": "continued"}}, "-"),
                 ({"last_result": {"ok": False, "state": "exit_pending", "reason": "exit_timeout"}},
                  "최근 실패 (종료 시간 초과)"),
                 ({"last_result": {"ok": False, "state": "relaunched", "reason": "new_reason"}}, "최근 실패 (new_reason)"),
                 ({"unswitchable": "cwd_missing", "last_result": {"ok": False, "state": "untouched",
                                                                  "reason": "cwd_missing"}}, "전환 불가 (작업 폴더 없음)"),
                 ({"in_flight": True, "decision": {"action": "soft", "target_account_id": "b"}}, "전환 중"),
                 ({"post_verify": "ok"}, "-"),
                 ({"post_verify": "mismatch"}, "검증 mismatch")]
        for r, want in cases:
            with self.subTest(r=r):
                self.assertEqual(cli._result_text(r, lab, tz), want)
        self.assertEqual(cli._result_text({"decision": {"action": "hard", "target_account_id": "b"}}, lab, tz,
                                          cli.GLYPHS_ASCII), "즉시 전환 -> bob")

    def test_every_switch_reason_has_korean_text(self):
        self.assertEqual(set(cli._SWITCH_REASONS), set(switcher.REASONS))

    def test_long_detail_lines_wrap_not_cut(self):
        hint = STATE_HINTS["exited"]
        lines = self.lines(width=60, details=True)
        start = next(i for i, l in enumerate(lines) if l.startswith("  pid 101 힌트: "))
        block = [lines[start]] + list(itertools.takewhile(lambda l: l.startswith("      "), lines[start + 1:]))
        self.assertGreater(len(block), 1)
        for l in block:
            self.assertLessEqual(cli.display_width(l), 60)
        joined = block[0][len("  pid 101 힌트: "):] + "".join(l[6:] for l in block[1:])
        self.assertEqual(joined, hint)
        self.assertEqual(lines[lines.index("  pid 101 재실행:") + 1], "cd /x && claude --resume y")

    def test_relaunch_command_is_one_unbroken_line(self):
        cmd = ("cd '/Users/user/work/한글 폴더' && env CLAUDE_CONFIG_DIR='/Users/user/.orca/profiles/"
               "00000000-0000-0000-0000-000000000001' claude --resume 00000000-0000-0000-0000-00000000abcd")
        doc = render_doc()
        doc["sessions"][0]["last_result"]["relaunch_command"] = cmd
        for width in (120, 60, 20, 1):
            with self.subTest(width=width):
                lines = self.lines(doc, width=width, details=True)
                self.assertGreater(cli.display_width(cmd), width)
                self.assertEqual(lines.count(cmd), 1)  # whole, not wrapped, indented or cut
                self.assertFalse(any(l.startswith("      ") and l.strip() in cmd for l in lines), lines)
                for l in lines:
                    if l != cmd and l not in RELAUNCH_LINES:
                        self.assertLessEqual(cli.display_width(l), max(1, width), l)

    def test_no_sessions_no_sessions_table(self):
        doc = render_doc()
        doc["sessions"], doc["unmanaged_sessions"], doc["attention"] = [], [], []
        lines = self.lines(doc)
        self.assertEqual(len(tables(lines)), 1)
        self.assertFalse(any(l.startswith("주의") for l in lines))

    def test_session_flags(self):
        doc = render_doc()
        doc["sessions"][0].update(in_flight=True, in_flight_pid=101, stuck=True, unswitchable="no_argv",
                                  hard_hold_until="2026-10-28T12:05:00+00:00", record_state="in_progress",
                                  would_switch={"action": "soft", "target_account_id": ACC_C}, post_verify="ok")
        res = self.session_row(self.lines(doc, width=1000), 101)[4]
        self.assertEqual(res, "멈춤 · 전환 중 · 드라이런: 유휴 시 전환 → 00000000 · 전환 불가 (실행 인자 없음) · 보류 ~21:05")

    def test_model_parts(self):
        doc = render_doc()
        doc["sessions"][0]["model"] = "claude-fable-5-1"
        doc["unmanaged_sessions"][0]["model"] = "claude-opus-5-5"
        lines = self.lines(doc)
        self.assertEqual(self.session_row(lines, 101)[3], "claude-fable-5-1")
        self.assertEqual(self.session_row(lines, 555)[3], "claude-opus-5-5")
        doc["sessions"][0]["model"] = None
        self.assertEqual(self.session_row(self.lines(doc), 101)[3], "-")

    def test_width_boundary(self):
        for width in (60, 20, 1, 0, -5):
            with self.subTest(width=width):
                lines = self.lines(dashboard_doc(), width=width, details=True)
                self.assertTrue(lines)
                for l in lines:
                    if l not in RELAUNCH_LINES:
                        self.assertLessEqual(cli.display_width(l), max(1, width), l)
                self.assertTrue(RELAUNCH_LINES <= set(lines))

    def test_empty(self):
        for doc in (None, dict(render_doc(), accounts=[])):
            lines = render(doc, T0, TZ9, 200)
            self.assertTrue(lines[0].startswith("Claude 계정 사용량"))
            self.assertIn("계정 없음 (Orca 프로필 폴더 설정과 데몬 실행 여부를 확인하세요)", lines)
            self.assertEqual(len(lines), 2)

    def test_pure(self):
        doc = dashboard_doc()
        before = copy.deepcopy(doc)
        for kw in ({}, {"color": True}, {"ascii": True, "ambiguous_wide": True}):
            self.assertEqual(render(doc, T0, TZ9, 200, **kw), render(doc, T0, TZ9, 200, **kw))
        self.assertEqual(doc, before)

    def test_ascii_mode(self):
        lines = self.lines(dashboard_doc(), ascii=True, details=True)
        for l in lines:
            self.assertTrue(all(ch.isascii() or "가" <= ch <= "힣" for ch in l), l)
        row = next(cells(l, ascii=True) for l in account_rows(lines, ascii=True) if cells(l, ascii=True)[1] == "alice")
        self.assertEqual(row[3], "########.  89% / 1시간 21분 뒤")
        self.assertIn("노랑 >=85%", "\n".join(lines))
        self.assertTrue(any("(토큰 만료/캐시 없음)" in l for l in lines))
        erin = next(cells(l, ascii=True) for l in account_rows(lines, ascii=True) if cells(l, ascii=True)[1] == "erin")
        self.assertEqual(erin[-1], "재로그인 필요 / 23시간 전 / 캐시")

    def test_no_reset_arrow_anywhere(self):
        for kw in ({}, {"ascii": True}, {"ambiguous_wide": True}, {"color": True}):
            for width in (200, 150, 120, 100, 80, 60):
                with self.subTest(kw=kw, width=width):
                    text = "\n".join(render(dashboard_doc(), T0, TZ9, width, **kw))
                    self.assertNotIn("↻", text)
                    self.assertNotIn("<1m", text)

    def test_cache_row_draws_bars_and_reset(self):
        lines = self.lines(dashboard_doc())
        carol = self.row(lines, "carol")
        self.assertEqual(carol[3], "▒▒▒▒▒▒▒▒▒   0%")  # past its reset: 0% and no reset text
        self.assertEqual(carol[4], "██▒▒▒▒▒▒▒  28% · 2일 뒤")
        self.assertEqual(carol[-1], "3일 전 · 캐시 · 호출 제한")
        self.assertNotIn("(캐시)", "\n".join(lines))
        frank = self.row(lines, "frank")
        self.assertEqual(frank[3], "█▒▒▒▒▒▒▒▒  20% · 1분 안")
        self.assertEqual(frank[4], "██████▒▒▒  77% · 5일 뒤")


class TestAlignment(unittest.TestCase):
    def check(self, lines, amb, ascii, width):
        blocks = tables(lines, ascii)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            hard_cut = block[0][-1] not in "┐+"
            if width >= 120:
                self.assertFalse(hard_cut, "\n".join(block))
            if hard_cut:
                continue
            widths = {cli.display_width(SGR.sub("", l), ambiguous_wide=amb) for l in block}
            self.assertEqual(len(widths), 1, "\n".join(block))
            self.assertLessEqual(widths.pop(), width)
            cols = {tuple(border_cols(l, amb, ascii)) for l in block}
            self.assertEqual(len(cols), 1, "\n".join(block))
        for l in lines:
            self.assertLessEqual(cli.display_width(SGR.sub("", l), ambiguous_wide=amb), width, l)

    def test_every_line_same_width_and_borders(self):
        for amb in (False, True):
            for ascii in (False, True):
                for width in (200, 160, 150, 120, 105, 100, 80, 65, 60, 50):
                    for color in (False, True):
                        for off in (False, True):
                            with self.subTest(amb=amb, ascii=ascii, width=width, color=color, off=off):
                                doc = daemon_off(dashboard_doc()) if off else dashboard_doc()
                                lines = render(doc, T0, TZ9, width, ambiguous_wide=amb, ascii=ascii, color=color)
                                self.check(lines, amb, ascii, width)
                                if off and width >= 150:
                                    self.assertIn("갱신 필요", "\n".join(lines))

    def test_rule_glyphs_fill_cells_exactly_when_wide(self):
        lines = render(dashboard_doc(), T0, TZ9, 300, ambiguous_wide=True)
        top, header = tables(lines)[0][:2]
        self.assertEqual(border_cols(top, True), border_cols(header, True))
        self.assertEqual(cli.display_width(top, ambiguous_wide=True) % 2, 0)


class TestFallback(unittest.TestCase):
    def level(self, lines):
        block = tables(lines)[0]
        head = cells(block[1])
        rows = "".join(block[3:-1])
        if "█" in rows or "▒" in rows:
            return 0
        if "플랜" in head:
            return 1
        if "페이블" in head:
            return 2
        if "갱신" in head:
            return 3
        if "뒤" in rows or "1분 안" in rows:
            return 4
        return 5

    def test_degradation_order(self):
        doc = dashboard_doc()
        full = cli.display_width(tables(render(doc, T0, TZ9, 500))[0][0])
        widths = {}
        for w in range(full, 30, -1):
            widths.setdefault(self.level(render(doc, T0, TZ9, w)), w)
        self.assertEqual(sorted(widths), [0, 1, 2, 3, 4, 5])
        cases = [(widths[k], k) for k in range(6)] + [(widths[k + 1] + 1, k) for k in range(5)]
        for width, want in cases:
            with self.subTest(width=width):
                lines = render(doc, T0, TZ9, width)
                self.assertEqual(self.level(lines), want)
                for l in lines:
                    self.assertLessEqual(cli.display_width(l), width)

    def test_level_never_decreases_as_width_shrinks(self):
        doc = dashboard_doc()
        levels = [self.level(render(doc, T0, TZ9, w)) for w in range(200, 39, -1)]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual((levels[0], levels[-1]), (0, 5))

    def test_full_table_kept_when_it_fits_exactly(self):
        doc = dashboard_doc()
        full = cli.display_width(tables(render(doc, T0, TZ9, 500))[0][0])
        self.assertEqual(self.level(render(doc, T0, TZ9, full)), 0)
        self.assertFalse(any("…" in l for l in tables(render(doc, T0, TZ9, full))[0]))
        # One column less: still every column, the over-long label cut first
        narrower = render(doc, T0, TZ9, full - 1)
        self.assertEqual(self.level(narrower), 0)
        self.assertIn("a-very-long-account-name-inde…", [cells(l)[1] for l in account_rows(narrower)])
        capped = max(w for w in range(full, 40, -1) if self.level(render(doc, T0, TZ9, w)) == 1)
        self.assertEqual(self.level(render(doc, T0, TZ9, capped + 1)), 0)
        self.assertIn("a-very-long-accou…", [cells(l)[1] for l in account_rows(render(doc, T0, TZ9, capped + 1))])

    def test_reset_text_dropped_before_label_shrinks(self):
        doc = dashboard_doc()
        for width in range(120, 40, -1):
            lines = render(doc, T0, TZ9, width)
            labels = [cells(l)[1] for l in account_rows(lines)]
            if any(lab.endswith("…") for lab in labels if not lab.startswith("a-very-long")):
                break
            if "a-very-long-accou…" not in labels:
                self.assertEqual(self.level(lines), 5, width)
                break
        else:
            self.fail("the label never shrank")
        five = cells(account_rows(render(doc, T0, TZ9, width))[0])
        self.assertTrue(all("뒤" not in c and "1분 안" not in c for c in five), five)

    def test_label_shrinks_after_columns_are_gone(self):
        doc = dashboard_doc()
        lines = render(doc, T0, TZ9, 60)
        self.assertEqual(self.level(lines), 5)
        labels = [cells(l)[1] for l in account_rows(lines)]
        self.assertTrue(any(lab.endswith("…") for lab in labels), labels)
        self.assertIn("alice", labels)
        widths = {cli.display_width(l) for l in tables(lines)[0]}
        self.assertEqual(len(widths), 1)

    def test_hard_cut_when_nothing_fits(self):
        lines = render(dashboard_doc(), T0, TZ9, 20)
        for l in set(lines) - RELAUNCH_LINES:
            self.assertLessEqual(cli.display_width(l), 20)
        self.assertTrue(all(l.startswith("│") or l.startswith("┌") or l.startswith("├") or l.startswith("└")
                            for l in tables(lines)[0]))

    def test_sessions_result_shrinks(self):
        lines = render(render_doc(), T0, TZ9, 60)
        res = cells(tables(lines)[1][3])[4]
        self.assertTrue(res.endswith("…"), res)
        self.assertEqual(len({cli.display_width(l) for l in tables(lines)[1]}), 1)


ID_A1 = "aaaa1111-0000-0000-0000-000000000000"
ID_B2 = "bbbb2222-0000-0000-0000-000000000000"
ID_C3 = "cccc3333-0000-0000-0000-000000000000"


def plan_doc(rows):
    """Accounts (account_id, label, plan, pointer) with the same live numbers; one session per account."""
    doc = render_doc()
    doc["accounts"] = [
        {"account_id": aid, "label": label, "plan": plan, "source": "live", "error": None,
         "fetched_at": T0_ISO, "five_hour": win(30.0, timedelta(hours=2)), "seven_day": win(20.0, timedelta(days=2)),
         "fable": None, "pointer": False, "sessions": []} for aid, label, plan in rows]
    doc["sessions"] = [dict(doc["sessions"][0], pid=100 + i, account_id=aid,
                            decision={"action": "soft", "target_account_id": rows[-1 - i][0]}, last_result=None)
                       for i, (aid, _, _) in enumerate(rows)]
    doc["unmanaged_sessions"], doc["attention"] = [], []
    return doc


class TestPlan(unittest.TestCase):
    def rows(self, doc, width=200, **kw):
        return [cells(l, kw.get("ascii", False)) for l in account_rows(render(doc, T0, TZ9, width, **kw),
                                                                         kw.get("ascii", False))]

    def test_plan_tag(self):
        cases = [("default_claude_max_5x", "Max5x"), ("default_claude_max_20x", "Max20x"), ("default_raven", "Team"),
                 ("default_enterprise_plus", "enterpri"), ("pro", "pro"), ("default_", None), ("", None),
                 (None, None), (7, None), ("\x1b[1mdefault_raven", "[1mdefau")]
        for tier, want in cases:
            with self.subTest(tier=tier):
                self.assertEqual(cli.plan_tag(tier), want)

    def test_duplicate_email_two_tiers_distinct_rows(self):
        rows = self.rows(plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Team")]))
        self.assertEqual([r[1:3] for r in rows], [["user", "Max5x"], ["user", "Team"]])

    def test_same_label_and_plan_gets_id_prefix(self):
        rows = self.rows(plan_doc([(ID_B2, "user", "Team"), (ID_A1, "user", "Team"), (ID_C3, "user", "Max5x")]))
        self.assertEqual([r[1:3] for r in rows], [["user", "Max5x"], ["user #aaaa", "Team"], ["user #bbbb", "Team"]])

    def test_sort_by_label_then_plan_then_id(self):
        doc = plan_doc([(ID_C3, "zed", None), (ID_B2, "amy", "Team"), (ID_A1, "amy", "Max5x"),
                        ("00000000-0000-0000-0000-000000000009", "홍길동", None), (ID_C3[:-1] + "1", "amy", "Team")])
        self.assertEqual([r[1:3] for r in self.rows(doc)],
                         [["amy", "Max5x"], ["amy #bbbb", "Team"], ["amy #cccc", "Team"], ["zed", "-"], ["홍길동", "-"]])
        before = copy.deepcopy(doc)
        render(doc, T0, TZ9, 200)
        self.assertEqual(doc, before)  # sorting never reorders the caller's document

    def test_plan_moves_into_label_when_column_dropped(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Team"), (ID_C3, "user", "Team"),
                        ("00000000-0000-0000-0000-000000000009", "solo", "Team")])
        lines = render(doc, T0, TZ9, 70)
        self.assertNotIn("플랜", cells(tables(lines)[0][1]))
        self.assertEqual([r[1] for r in self.rows(doc, 70)],
                         ["solo", "user Max5x", "user Team #bbbb", "user Team #cccc"])

    def test_sessions_table_tells_duplicates_apart(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Team")])
        lines = render(doc, T0, TZ9, 200)
        body = [cells(l) for l in tables(lines)[1][3:-1]]
        self.assertEqual([(r[0], r[1], r[4]) for r in body],
                         [("100", "user Max5x", "유휴 시 전환 → user Team"),
                          ("101", "user Team", "유휴 시 전환 → user Max5x")])

    def test_suffix_survives_label_cut(self):
        long = "a-very-long-account-name-indeed"
        doc = plan_doc([(ID_A1, long, "Team"), (ID_B2, long, "Team")])
        self.assertEqual([r[1] for r in self.rows(doc)], [long + " #aaaa", long + " #bbbb"])  # wide: uncut
        rows = self.rows(doc, width=70)
        self.assertEqual([r[1] for r in rows], ["a-very-long… #aaaa", "a-very-long… #bbbb"])  # suffix gives way
        self.assertTrue(all(cli.display_width(r[1]) <= 18 for r in rows))

    def test_alignment_with_plan_column(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Team"), (ID_C3, "홍길동", "Max20x")])
        for amb in (False, True):
            for ascii in (False, True):
                for width in (200, 90, 70, 60):
                    with self.subTest(amb=amb, ascii=ascii, width=width):
                        lines = render(doc, T0, TZ9, width, ambiguous_wide=amb, ascii=ascii)
                        for block in tables(lines, ascii):
                            self.assertEqual(len({cli.display_width(l, ambiguous_wide=amb) for l in block}), 1)
                            self.assertEqual(len({tuple(border_cols(l, amb, ascii)) for l in block}), 1)
                            for l in block:
                                self.assertLessEqual(cli.display_width(l, ambiguous_wide=amb), width)

    def test_missing_plan_shows_dash(self):
        doc = plan_doc([(ID_A1, "user", None)])
        del doc["accounts"][0]["plan"]
        self.assertEqual(self.rows(doc)[0][2], "-")


def grouped(doc, names=("Example Org TEAM 1", "Example Org TEAM 2")):
    """Groups the accounts round-robin under ``names``, as build_status's group_names shows them."""
    shown = cli.group_names({a["account_id"]: names[i % len(names)] for i, a in enumerate(doc["accounts"])})
    for a in doc["accounts"]:
        a["group"] = shown[a["account_id"]]
    return doc


class TestGroup(unittest.TestCase):
    def test_group_names(self):
        cases = [
            ({"a": "Example Org TEAM 1", "b": "Example Org TEAM 2"}, {"a": "TEAM 1", "b": "TEAM 2"}),
            ({"a": "Example Org TEAM 1", "b": "Example Org TEAM 2", "c": None}, {"a": "TEAM 1", "b": "TEAM 2", "c": None}),
            ({"a": "Alpha Team", "b": "Beta Team"}, {"a": "Alpha Team", "b": "Beta Team"}),  # no common prefix
            ({"a": "Example Org", "b": "Example Org"}, {"a": "Example Org", "b": "Example Org"}),  # one name: whole
            ({"a": "Example Org", "b": "Example Org TEAM 2"}, {"a": "Org", "b": "Org TEAM 2"}),  # >= 1 word kept
            ({"a": "Example", "b": "Example Org"}, {"a": "Example", "b": "Example Org"}),
            ({"a": "ExampleX TEAM", "b": "Example TEAM"}, {"a": "ExampleX TEAM", "b": "Example TEAM"}),  # words only
            ({"a": "Example Org Sales", "b": "Example Org Ops"}, {"a": "Sales", "b": "Ops"}),
            ({"a": "Example Org 1", "b": "Example Org 2"}, {"a": "Org 1", "b": "Org 2"}),  # never a bare number
            ({"a": "Example Org TEAM 1", "b": "Example Org TEAM 12", "c": "Example Org TEAM"},
             {"a": "TEAM 1", "b": "TEAM 12", "c": "TEAM"}),
            ({"a": "  ", "b": "\x1b[31mX Y", "c": None}, {"a": None, "b": "[31mX Y", "c": None}),
            ({}, {}),
        ]
        for orgs, want in cases:
            with self.subTest(orgs=orgs):
                self.assertEqual(cli.group_names(orgs), want)

    def test_build_status_group(self):
        env = standard_env()
        env.profiles = [dataclasses.replace(prof(ACC_A), organization_name="Example Org TEAM 1"),
                        dataclasses.replace(prof(ACC_B), organization_name="Example Org TEAM 2"),
                        prof(ACC_C)]
        doc = build_status(make_cfg(), env.deps(), T0)
        self.assertEqual({a["account_id"]: a["group"] for a in doc["accounts"]},
                         {ACC_A: "TEAM 1", ACC_B: "TEAM 2", ACC_C: None})

    def rows(self, doc, width=200, **kw):
        return [cells(l, kw.get("ascii", False)) for l in account_rows(render(doc, T0, TZ9, width, **kw),
                                                                       kw.get("ascii", False))]

    def test_column_after_label_and_sorting(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Max5x"), (ID_C3, "alice", "Team")])
        doc["accounts"][0]["group"], doc["accounts"][1]["group"], doc["accounts"][2]["group"] = "TEAM 2", "TEAM 1", None
        lines = render(doc, T0, TZ9, 200)
        self.assertEqual(cells(tables(lines)[0][1])[:4], ["", "계정", "그룹", "플랜"])
        # group, then label: None (no group) first, then TEAM 1, TEAM 2; same label + plan, groups differ: no #id
        self.assertEqual([r[1:4] for r in self.rows(doc)],
                         [["alice", "-", "Team"], ["user", "TEAM 1", "Max5x"], ["user", "TEAM 2", "Max5x"]])

    def test_group_sorts_before_label(self):
        doc = plan_doc([(ID_A1, "alice", "Team"), (ID_B2, "zed", "Team"), (ID_C3, "mia", "Team")])
        for a, g in zip(doc["accounts"], ("TEAM 2", "TEAM 1", "TEAM 2")):
            a["group"] = g
        self.assertEqual([r[1:3] for r in self.rows(doc)], [["zed", "TEAM 1"], ["alice", "TEAM 2"], ["mia", "TEAM 2"]])

    def test_no_group_column_without_groups(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "bob", "Team")])
        self.assertNotIn("그룹", cells(tables(render(doc, T0, TZ9, 200))[0][1]))

    def test_suffix_only_when_label_group_plan_collide(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Max5x"), (ID_C3, "user", "Max5x")])
        for a, g in zip(doc["accounts"], ("TEAM 1", "TEAM 1", "TEAM 2")):
            a["group"] = g
        self.assertEqual([r[1] for r in self.rows(doc)], ["user #aaaa", "user #bbbb", "user"])

    def test_group_truncated_to_10(self):
        doc = plan_doc([(ID_A1, "user", "Max5x")])
        doc["accounts"][0]["group"] = "A Very Long Group Name"
        self.assertEqual(self.rows(doc)[0][2], "A Very Long Group Name")  # the table fits: never cut
        narrow = next(w for w in range(200, 40, -1) if "…" in self.rows(doc, width=w)[0][2])
        self.assertEqual(self.rows(doc, width=narrow)[0][2][-1], "…")
        self.assertGreaterEqual(cli.display_width(self.rows(doc, width=narrow)[0][2]), 10)
        self.assertEqual(self.rows(doc, width=narrow, ascii=True)[0][2][-1], "~")
        cut = [w for w in range(200, 40, -1) if "그룹" in cells(tables(render(doc, T0, TZ9, w))[0][1])
               and self.rows(doc, width=w)[0][2] == "A Very Lo…"]
        self.assertTrue(cut)  # the shrink phase cuts it down to 10 columns before 그룹 is dropped

    def test_dropped_group_joins_the_label(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Max5x")])
        doc["accounts"][0]["group"], doc["accounts"][1]["group"] = "TEAM 1", "TEAM 2"
        seen = set()
        for width in range(120, 40, -1):
            lines = render(doc, T0, TZ9, width)
            head = cells(tables(lines)[0][1])
            labels = [cells(l)[1] for l in account_rows(lines)]
            if "플랜" in head:
                self.assertIn("그룹", head)  # 그룹 is dropped only after 플랜
            self.assertTrue(all(lab.startswith("user") for lab in labels), (width, labels))  # never eaten
            if "그룹" not in head and "플랜" not in head:
                self.assertEqual(len(set(labels)), 2, (width, labels))  # the rows stay apart
                seen.add(tuple(labels))
        # the group and plan join the label while they fit; narrower, "#id4" alone keeps the rows apart
        self.assertEqual(seen, {("user TEAM 1 Max5x", "user TEAM 2 Max5x"), ("user #aaaa", "user #bbbb")})

    def test_sessions_label_has_group_and_plan(self):
        doc = plan_doc([(ID_A1, "user", "Max5x"), (ID_B2, "user", "Max5x")])
        doc["accounts"][0]["group"], doc["accounts"][1]["group"] = "TEAM 1", "TEAM 2"
        lines = render(doc, T0, TZ9, 200)
        rows = [cells(l) for l in tables(lines)[1][3:-1]]
        self.assertEqual(rows[0][1], "user TEAM 1 Max5x")
        self.assertIn("→ user TEAM 1 Max5x", rows[1][4])
        rows = [cells(l) for l in tables(render(doc, T0, TZ9, 45))[1][3:-1]]
        self.assertTrue(all(cli.display_width(r[1]) <= 45 for r in rows))

    def test_alignment_with_groups(self):
        for names in (("Example Org TEAM 1", "Example Org TEAM 2"), ("Alpha", "그룹 둘")):
            for amb in (False, True):
                for ascii in (False, True):
                    for width in (200, 150, 120, 100, 80, 60):
                        with self.subTest(names=names, amb=amb, ascii=ascii, width=width):
                            lines = render(grouped(dashboard_doc(), names), T0, TZ9, width, ambiguous_wide=amb,
                                           ascii=ascii)
                            TestAlignment.check(self, lines, amb, ascii, width)
                            if width >= 150:
                                self.assertIn("그룹", cells(tables(lines, ascii)[0][1], ascii))


class TestNaturalWidths(unittest.TestCase):
    def test_wide_terminal_cuts_nothing(self):
        doc = daemon_off(dashboard_doc())
        doc["accounts"][3]["error"] = "token_expired;cache_missing;network"
        full = cli.display_width(tables(render(doc, T0, TZ9, 1000))[0][0])
        for width in (full, full + 1, 1000):
            with self.subTest(width=width):
                block = tables(render(doc, T0, TZ9, width))[0]
                self.assertFalse(any("…" in l for l in block), "\n".join(block))
                self.assertIn("a-very-long-account-name-indeed", "\n".join(block))

    def test_narrow_terminal_cuts(self):
        doc = dashboard_doc()
        doc["accounts"][2]["fetched_at"] = (T0 - timedelta(hours=16)).isoformat()
        wide = {cells(l)[1]: cells(l)[-1] for l in account_rows(render(doc, T0, TZ9, 1000))}
        self.assertEqual(wide["carol"], "16시간 전 · 캐시 · 호출 제한")
        narrow = {cells(l)[1]: cells(l)[-1] for l in account_rows(render(doc, T0, TZ9, 140))}
        self.assertTrue(narrow["carol"].startswith("16시간 전 · 캐시") and narrow["carol"].endswith("…"), narrow)
        self.assertLessEqual(cli.display_width(narrow["carol"]), cli._AGE_CAP)

    def test_unavailable_reason_uncut_when_wide(self):
        doc = render_doc()
        doc["accounts"][1]["error"] = "token_expired;cache_missing;network;http_500"
        row = TestRender.row(self, render(doc, T0, TZ9, 300), "bob")
        self.assertEqual(row[3], "-  (토큰 만료·캐시 없음·연결 실패·HTTP 500)")
        short = TestRender.row(self, render(doc, T0, TZ9, 100), "bob")
        self.assertTrue(short[3].endswith("…)"), short)


class TestLastGood(StatusBase):
    """Addendum 3: one usage poll per account across rotator processes, and the last good reading kept."""

    def test_watch_and_one_shot_status_share_one_poll(self):
        watch = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))
        for _ in range(7):  # a watch rebuilding every 10 s and a one-shot status each time, for 60 s
            self.doc(usage=watch)
            self.doc()
            self.env.advance(10.0)
        self.assertEqual(len(self.env.fetches()), 3 * 3)  # at 0, 30 and 60 s, once per account

    def test_rate_limited_keeps_last_good_numbers(self):
        self.doc()
        self.env.advance(300.0)
        self.env.usages[ACC_A] = AccountUsage(account_id=ACC_A, source="unavailable", fetched_at=self.env.now_v,
                                              error="http_429;cache_missing")
        doc = self.doc()
        a = self.account(doc, ACC_A)
        self.assertEqual((a["source"], a["error"], a["five_hour"]["utilization"]), ("live", "http_429", 20.0))
        self.assertEqual(a["fetched_at"], T0_ISO)  # the age is the good reading's
        lines = render(doc, self.env.now_v, TZ9, 200)
        rows = [cells(l) for l in account_rows(lines)]
        self.assertEqual(cells(tables(lines)[0][1])[3], "5시간")
        self.assertTrue(all("%" in r[3] and "%" in r[4] for r in rows), rows)  # never blank: numbers everywhere
        self.assertEqual(sorted(r[-1] for r in rows), ["5분 전", "5분 전", "5분 전 · 호출 제한"])

    def test_expired_token_after_a_good_reading(self):
        doc = render_doc()
        doc["accounts"][0].update(error="token_expired", fetched_at=(T0 - timedelta(minutes=5)).isoformat())
        row = TestRender.row(self, render(doc, T0, TZ9, 200), "alice")
        self.assertEqual(row[-1], "세션이 갱신 · 5분 전")  # alice has a live session
        self.assertTrue(row[4].startswith("▒▒▒▒▒▒▒▒▒  10% · "), row)
        doc["accounts"][0]["sessions"] = []
        self.assertEqual(TestRender.row(self, render(doc, T0, TZ9, 200), "alice")[-1], "갱신 대기 · 5분 전")
        doc = daemon_off(doc)
        self.assertEqual(TestRender.row(self, render(doc, T0, TZ9, 200), "alice")[-1], "갱신 필요 · 5분 전")

    def test_near_expiry_needs_refresh(self):
        def row_last(doc, aid="alice"):
            return TestRender.row(self, render(doc, T0, TZ9, 200), aid)[-1]
        doc = render_doc()
        doc["accounts"] = doc["accounts"][:1]  # alice only: bob's expired token would bring the footer anyway
        doc["accounts"][0]["sessions"] = []
        doc["accounts"][0]["token_expires_at"] = (T0 + timedelta(minutes=4)).isoformat()  # inside the 5 min margin
        self.assertEqual(row_last(doc), "갱신 대기 · 12초 전")  # live reading, daemon on
        self.assertEqual(row_last(daemon_off(doc)), "갱신 필요 · 12초 전")
        self.assertIn(cli.REFRESH_FOOTER, render(daemon_off(doc), T0, TZ9, 200))
        doc["accounts"][0]["token_expires_at"] = (T0 + timedelta(minutes=5)).isoformat()  # exactly the margin
        self.assertEqual(row_last(daemon_off(doc)), "갱신 필요 · 12초 전")
        doc["accounts"][0]["token_expires_at"] = (T0 + timedelta(minutes=5, seconds=1)).isoformat()
        self.assertEqual(row_last(daemon_off(doc)), "12초 전")
        self.assertNotIn(cli.REFRESH_FOOTER, render(daemon_off(doc), T0, TZ9, 200))
        for bad in (None, "soon", 7):
            doc["accounts"][0]["token_expires_at"] = bad
            self.assertEqual(row_last(daemon_off(doc)), "12초 전")

    def test_near_expiry_states(self):
        soon = (T0 + timedelta(minutes=2)).isoformat()
        cases = [
            ({"source": "live", "error": None, "sessions": [{"pid": 1}]}, True, "세션이 갱신"),
            ({"source": "live", "error": None, "sessions": [],
              "refresh": {"state": "skipped", "reason": "deferred"}}, True, "갱신 대기"),
            ({"source": "cache", "error": "http_429", "sessions": []}, False, "갱신 필요"),
            ({"source": "cache", "error": "http_429", "sessions": [],
              "refresh": {"state": "stopped", "reason": "invalid_grant"}}, True, "재로그인 필요"),
        ]
        for a, running, want in cases:
            with self.subTest(a=a, running=running):
                self.assertEqual(cli._token_state(dict(a, token_expires_at=soon), running, T0), want)
        self.assertIsNone(cli._token_state({"source": "live", "error": None, "token_expires_at": soon}, True))

    def test_live_row_without_error_shows_age_only(self):
        self.assertEqual(TestRender.row(self, render(render_doc(), T0, TZ9, 200), "alice")[-1], "12초 전")


def collide_doc():
    """dashboard_doc with groups and labels that repeat across groups (same label, same or other plan)."""
    doc = grouped(dashboard_doc())
    for n, (lab, g, plan) in enumerate((("alice", "TEAM 1", "Max5x"), ("alice", "TEAM 2", "Max5x"),
                                        ("alice", "TEAM 2", "Max5x"), ("team.member.10", "TEAM 1", "Team"),
                                        ("team.member.10", "TEAM 2", "Max5x"), ("team.member.10", "TEAM 2", "Team"))):
        doc["accounts"].append(dict(doc["accounts"][0], account_id="%x%x%x%x-0000-0000-0000-000000000000" % ((n + 10,) * 4),
                                    label=lab, group=g, plan=plan, pointer=False))
    return doc


class TestNarrowLabels(unittest.TestCase):
    """rework F1: the label is never eaten by its suffix, and the table keeps its right border."""

    def test_labels_stay_readable_and_distinct(self):
        for width in (60, 70, 80):
            for amb in (False, True):
                with self.subTest(width=width, amb=amb):
                    lines = render(collide_doc(), T0, TZ9, width, ambiguous_wide=amb)
                    head = cells(tables(lines)[0][1])
                    shown = [tuple(c for c, h in zip(cells(l), head) if h in ("계정", "그룹", "플랜"))
                             for l in account_rows(lines)]
                    self.assertEqual(len(set(shown)), len(shown), shown)  # every row tells itself apart
                    for row in shown:
                        self.assertNotRegex(row[0], r"^(…|~)|^ |^#", row)
                    alice = [r[0] for r in shown if r[0].startswith("alice")]
                    self.assertEqual(len(alice), 4, shown)  # "alice" itself is kept whole

    def test_suffix_is_cut_before_the_label(self):
        ctx = cli._Ctx(cli.GLYPHS_UNICODE, False, False)
        accounts = collide_doc()["accounts"]  # dashboard_doc's alice is TEAM 1 Max5x too
        aid = lambda n: "%x%x%x%x-0000-0000-0000-000000000000" % ((n + 10,) * 4)  # noqa: E731
        texts = cli._label_texts(accounts, ctx, None, False, False)
        self.assertEqual([texts[aid(i)] for i in range(6)],
                         ["alice TEAM 1 Max5x #aaaa", "alice TEAM 2 Max5x #bbbb", "alice TEAM 2 Max5x #cccc",
                          "team.member.10 TEAM 1 Team", "team.member.10 TEAM 2 Max5x", "team.member.10 TEAM 2 Team"])
        # group cut, then group left out, then plan left out (#id4 whenever the row would repeat), then label cut
        for cap, want in ((24, "alice TEAM 2 Max5x #bbbb"), (21, "alice TE… Max5x #bbbb"), (17, "alice Max5x #bbbb"),
                          (12, "alice #bbbb"), (10, "ali… #bbbb"), (7, "alice"), (4, "ali…")):
            with self.subTest(cap=cap):
                got = cli._label_texts(accounts, ctx, cap, False, False)[aid(1)]
                self.assertEqual(got, want)
                self.assertLessEqual(cli.display_width(got), cap)
        at24 = cli._label_texts(accounts, ctx, 24, False, False)
        self.assertEqual(at24[aid(4)], "team.member.10 TE… Max5x")  # unique once its plan shows: no #id
        self.assertEqual(at24[aid(3)], "team.member.10 #dddd")  # "team.member.10 Team" would repeat aid(5)
        for cap in range(4, 30):
            got = cli._label_texts(accounts, ctx, cap, False, False)
            self.assertTrue(all(cli.display_width(t) <= cap for t in got.values()), (cap, got))
            self.assertTrue(all(not t.startswith(("…", " ", "#")) for t in got.values()), (cap, got))

    def test_cut_group_that_repeats_falls_back_to_id(self):
        ctx = cli._Ctx(cli.GLYPHS_UNICODE, False, False)
        accounts = [{"account_id": "aaaa0000", "label": "user", "group": "TEAM 1", "plan": "Team"},
                    {"account_id": "bbbb0000", "label": "user", "group": "TEAM 2", "plan": "Team"}]
        self.assertEqual(sorted(cli._label_texts(accounts, ctx, None, False, False).values()),
                         ["user TEAM 1 Team", "user TEAM 2 Team"])
        self.assertEqual(sorted(cli._label_texts(accounts, ctx, 14, False, False).values()),
                         ["user #aaaa", "user #bbbb"])  # "user TE… Team" twice would read the same

    def test_unique_label_gets_no_suffix(self):
        ctx = cli._Ctx(cli.GLYPHS_UNICODE, False, False)
        texts = cli._label_texts(collide_doc()["accounts"], ctx, 8, False, False)
        self.assertEqual(texts["00000000-0000-0000-0000-000000000003"], "carol")

    def test_no_hard_cut_from_60_to_220(self):
        for amb in (False, True):
            for ascii in (False, True):
                for off in (False, True):
                    for width in range(60, 221, 7):
                        with self.subTest(amb=amb, ascii=ascii, off=off, width=width):
                            doc = daemon_off(collide_doc()) if off else collide_doc()
                            lines = render(doc, T0, TZ9, width, ambiguous_wide=amb, ascii=ascii)
                            for block in tables(lines, ascii):
                                self.assertIn(block[0][-1], "┐+", "\n".join(block))
                                self.assertEqual(len({cli.display_width(SGR.sub("", l), ambiguous_wide=amb)
                                                      for l in block}), 1)
                                self.assertEqual(len({tuple(border_cols(l, amb, ascii)) for l in block}), 1)
                            for l in set(lines) - RELAUNCH_LINES:
                                self.assertLessEqual(cli.display_width(l, ambiguous_wide=amb), width)


def realistic_doc():
    """Nine accounts like a real machine: dotted labels repeating across two groups, nine sessions."""
    doc = render_doc()
    names = ["team.member.1", "team.member.10", "team.member.11", "team.member.12", "team.member.2",
             "team.member.10", "team.member.11", "team.member.12", "other.person"]
    plans = ["Max5x", "Team", "Team", "Team", "Max5x", "Max5x", "Max5x", "Max5x", "Max5x"]
    groups = ["TEAM 1"] * 5 + ["TEAM 2"] * 3 + ["TEAM 1"]
    doc["accounts"] = [dict(render_doc()["accounts"][0], account_id="%04x0000-0000-0000-0000-000000000000" % (0xa000 + i),
                            label=n, plan=p, group=g, pointer=(i == 5)) for i, (n, p, g) in enumerate(zip(names, plans, groups))]
    doc["sessions"] = [dict(render_doc()["sessions"][0], pid=40000 + i, account_id=doc["accounts"][i % 9]["account_id"],
                            model="claude-opus-5-5", decision=None, last_result=None, hint="") for i in range(9)]
    doc["unmanaged_sessions"], doc["attention"] = [{"pid": 555, "session_id": SID2, "status": "idle",
                                                    "model": "claude-opus-5-5"}], []
    return doc


class TestSessionsTableFits(unittest.TestCase):
    """rework F2: the sessions table fits 80 columns on realistic data with a compact 계정 cell."""

    def test_fits_80_without_hard_cut(self):
        for width, amb in ((80, False), (80, True), (74, False)):
            with self.subTest(width=width, amb=amb):
                lines = render(realistic_doc(), T0, TZ9, width, ambiguous_wide=amb)
                block = tables(lines)[1]
                self.assertIn(block[0][-1], "┐", "\n".join(block))
                self.assertLessEqual(cli.display_width(block[0], ambiguous_wide=amb), width)
                names = {cells(l)[1] for l in block[3:-1]} - {"(관리 밖)"}
                self.assertEqual(len(names), 9, names)  # nine accounts, nine distinct 계정 cells
                for n in names:
                    self.assertLessEqual(cli.display_width(n, ambiguous_wide=amb), cli._SESSION_LABEL_CAP)
                    self.assertFalse(n.startswith("…"), n)

    def test_too_narrow_keeps_labels_readable(self):
        lines = render(realistic_doc(), T0, TZ9, 50)
        block = tables(lines)[1]
        self.assertTrue(all(cli.display_width(l) <= 50 for l in block))
        self.assertNotIn("┐", block[0])  # hard cut: the 모델 column alone does not fit
        names = {cells(l)[1] for l in block[3:-1]}
        self.assertIn("other.person", names)  # not cut down to "oth…" first: the cut buys nothing
        self.assertIn("team.member.10 TEAM 1 Team", names)

    def test_wide_sessions_label_shows_group_and_plan(self):
        lines = render(realistic_doc(), T0, TZ9, 200)
        names = {cells(l)[1] for l in tables(lines)[1][3:-1]}
        self.assertIn("team.member.10 TEAM 1 Team", names)  # label, group and plan when they fit 26 columns
        self.assertIn("team.member.10 TEAM… Max5x", names)  # 27 columns: its group is cut, not its label
        self.assertIn("other.person", names)  # a label no other account has needs nothing after it


class TestWrapAtSpaces(unittest.TestCase):
    """rework F4: the footer and hint lines break only at spaces."""

    def check(self, text, width, indent="      ", long_word=None):
        out = cli._wrap(text, width, False, "…")
        self.assertEqual(out[0] + "".join(l[len(indent):] for l in out[1:]), text)
        for l in out:
            self.assertLessEqual(cli.display_width(l), width, l)
        for a, b in zip(out, out[1:]):
            inside_long = long_word is not None and set(a.strip()) == {"x"} and b[len(indent):].startswith("x")
            self.assertTrue(a.endswith(" ") or b[len(indent):].startswith(" ") or inside_long, (a, b))
        return out

    def test_footer_at_narrow_widths(self):
        for width in range(20, 80):
            with self.subTest(width=width):
                out = self.check(cli.REFRESH_FOOTER, width)
                joined = " ".join(l.strip() for l in out)
                self.assertIn("rotator daemon", joined)
                self.assertIn("rotator install", joined)

    def test_hint_lines(self):
        hint = STATE_HINTS["exited"]
        for width in (40, 60, 80):
            self.check("  pid 101 힌트: " + hint, width)

    def test_word_longer_than_a_line_is_split(self):
        out = self.check("ab " + "x" * 50 + " cd", 20, long_word=True)
        self.assertGreater(len(out), 2)
        self.assertEqual(out[0], "ab ")  # the long word starts on its own line

    def test_render_footer_never_mid_word(self):
        lines = render(daemon_off(dashboard_doc()), T0, TZ9, 60)
        i = next(i for i, l in enumerate(lines) if l.startswith("토큰 만료"))
        block = [lines[i]] + list(itertools.takewhile(lambda l: l.startswith("      "), lines[i + 1:]))
        self.assertEqual(block[0] + "".join(l[6:] for l in block[1:]), cli.REFRESH_FOOTER)
        self.assertNotIn("      otator daemon)", block)


class TestColor(unittest.TestCase):
    def cell_styles(self, line):
        return [m.group(1) if m else None for m in
                (re.match(r"\x1b\[([0-9;]*)m", c.lstrip(" ")) for c in line.split("│")[1:-1])]

    def test_no_color_has_no_escapes(self):
        for ascii in (False, True):
            for l in render(dashboard_doc(), T0, TZ9, 200, ascii=ascii):
                self.assertNotIn("\x1b", l)

    def test_stripped_color_equals_plain(self):
        for width in (200, 105, 80):
            with self.subTest(width=width):
                plain = render(dashboard_doc(), T0, TZ9, width)
                colored = render(dashboard_doc(), T0, TZ9, width, color=True)
                self.assertNotEqual(plain, colored)
                self.assertEqual([SGR.sub("", l) for l in colored], plain)

    def test_threshold_colors(self):
        lines = render(dashboard_doc(), T0, TZ9, 200, color=True)
        rows = account_rows(lines)
        styles = {cells(l)[1]: self.cell_styles(l) for l in rows}
        self.assertEqual(styles["alice"][3:6], ["33", None, None])  # 89.4 >= 85 yellow
        self.assertEqual(styles["bob"][3:6], ["31", None, None])  # 100 >= 95 red
        self.assertEqual(styles["carol"], ["2"] * 8)  # cache rows dim every cell
        self.assertEqual(styles["dave"], [None] * 8)
        self.assertEqual(styles["홍길동"][3:6], [None, "31", "31"])

    def test_threshold_boundaries(self):
        for util, want in ((84.9, None), (85.0, "33"), (94.9, "33"), (95.0, "31")):
            with self.subTest(util=util):
                doc = render_doc()
                doc["accounts"][0]["seven_day"]["utilization"] = util
                row = next(l for l in account_rows(render(doc, T0, TZ9, 200, color=True)) if cells(l)[1] == "alice")
                self.assertEqual(self.cell_styles(row)[4], want)
        doc = render_doc()
        doc["accounts"][0]["seven_day"]["utilization"] = 60.0
        row = account_rows(render(doc, T0, TZ9, 200, color=True, soft_pct=50.0, hard_pct=70.0))[0]
        self.assertEqual(self.cell_styles(row)[4], "33")

    def test_sessions_and_rules_never_colored(self):
        lines = render(dashboard_doc(), T0, TZ9, 200, color=True)
        for l in tables(lines)[1] + [tables(lines)[0][i] for i in (0, 1, 2, -1)]:
            self.assertNotIn("\x1b", l)


class MainBase(unittest.TestCase):
    def setUp(self):
        self.env = standard_env()
        self.dir = scratch("cfg-" + self._testMethodName)
        self.cfg_path = os.path.join(self.dir, "config.json")
        self.cfg_seen: List[Config] = []
        # status sizes its table from the terminal; pin it so account lines are never cut in tests
        env_patch = mock.patch.dict(os.environ, {"COLUMNS": "300", "LINES": "40"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def write_cfg(self, obj):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def make_deps(self, cfg):
        self.cfg_seen.append(cfg)
        return self.env.deps()

    def main(self, *argv):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.runner = FakeRunner()
        return main(["--config", self.cfg_path] + list(argv), make_deps=self.make_deps, launchctl=self.runner,
                    uid=lambda: 501, stdout=self.out, stderr=self.err, tz=timezone.utc)

    @property
    def state_dir(self):
        return CONFIG_DIR_DEFAULT

    def stop_after(self, n, step=1.0):
        def hook(k):
            self.env.advance(step)
            if k == n:
                raise KeyboardInterrupt
        self.env.wait_hook = hook

    def frames(self):
        return [f for f in self.out.getvalue().split("---\n") if f]


class TestMainParser(MainBase):
    def test_help_each_subcommand(self):
        self.assertEqual(self.main("--help"), 0)
        for name in ("status", "daemon", "switch", "install", "uninstall"):
            self.assertIn(name, self.out.getvalue())
        for name in ("status", "daemon", "switch", "install", "uninstall"):
            with self.subTest(name=name):
                self.assertEqual(self.main(name, "--help"), 0)
        self.assertEqual(self.runner.calls, [])

    def test_install_uninstall_doctor_help_is_os_neutral(self):
        # install/uninstall/doctor now run on both OSes; their help text must not say "launchd" or "Mac" only.
        for name in ("install", "uninstall", "doctor"):
            with self.subTest(name=name):
                self.assertEqual(self.main(name, "--help"), 0)
                text = self.out.getvalue()
                self.assertNotIn("launchd", text)
                self.assertNotIn("Mac", text)

    def test_usage_errors(self):
        self.assertEqual(self.main(), 2)
        self.assertIn("usage:", self.err.getvalue())
        self.assertEqual(self.main("status", "--bogus"), 2)
        self.assertEqual(self.main("status", "--watch", "--json"), 2)
        self.assertEqual(self.main("switch", "--to", ACC_B), 2)
        self.assertEqual(self.cfg_seen, [])

    def test_config_error_exit_2(self):
        self.write_cfg({"soft_pct": 101})
        self.assertEqual(self.main("status"), 2)
        self.assertIn("soft_pct", self.err.getvalue())
        self.assertEqual(self.env.fetches(), [])
        self.assertEqual(self.cfg_seen, [])


class TestConsoleLogFallback(MainBase):
    """Under pythonw.exe sys.stdout/sys.stderr are None; an early-exit message (a config error, the Orca gate
    refusing) must land in a log file instead of vanishing."""

    def test_config_error_without_a_console_goes_to_the_log_file(self):
        self.write_cfg({"soft_pct": 101})
        log_dir = scratch("console-log-" + self._testMethodName)
        with mock.patch.object(cli, "LOG_DIR_DEFAULT", log_dir), \
                mock.patch.object(cli.sys, "stdout", None), mock.patch.object(cli.sys, "stderr", None), \
                mock.patch.object(cli.platform, "system", return_value="Windows"):
            rc = main(["--config", self.cfg_path, "daemon"])
        self.assertEqual(rc, 2)
        log_path = os.path.join(log_dir, cli.CONSOLE_LOG_FILENAME)
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("soft_pct", text)

    def test_log_file_is_appended_not_overwritten_and_closed(self):
        self.write_cfg({"soft_pct": 101})
        log_dir = scratch("console-log-" + self._testMethodName)
        log_path = os.path.join(log_dir, cli.CONSOLE_LOG_FILENAME)
        with mock.patch.object(cli, "LOG_DIR_DEFAULT", log_dir), \
                mock.patch.object(cli.sys, "stdout", None), mock.patch.object(cli.sys, "stderr", None):
            main(["--config", self.cfg_path, "daemon"])
            main(["--config", self.cfg_path, "daemon"])
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("soft_pct"), 2)  # appended, not truncated on the second run

    def test_log_file_is_closed_when_main_returns(self):
        self.write_cfg({"soft_pct": 101})
        log_dir = scratch("console-log-" + self._testMethodName)
        opened = []

        def recording_open(*a, **kw):
            f = open(*a, **kw)
            opened.append(f)
            return f

        with mock.patch.object(cli, "LOG_DIR_DEFAULT", log_dir), mock.patch.object(cli, "open", recording_open,
                                                                                   create=True), \
                mock.patch.object(cli.sys, "stdout", None), mock.patch.object(cli.sys, "stderr", None):
            main(["--config", self.cfg_path, "daemon"])
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)

    def test_a_crash_traceback_lands_in_the_log_file(self):
        # pythonw.exe has no sys.stderr, so Python's own traceback printing would drop it.
        self.write_cfg({})
        log_dir = scratch("console-log-" + self._testMethodName)

        def boom(cfg):
            raise RuntimeError("synthetic crash for the test")

        with mock.patch.object(cli, "LOG_DIR_DEFAULT", log_dir), \
                mock.patch.object(cli.sys, "stdout", None), mock.patch.object(cli.sys, "stderr", None):
            with self.assertRaises(RuntimeError):
                main(["--config", self.cfg_path, "daemon"], make_deps=boom, launchctl=FakeRunner())
        with open(os.path.join(log_dir, cli.CONSOLE_LOG_FILENAME), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Traceback", text)
        self.assertIn("synthetic crash for the test", text)

    def test_injected_streams_never_touch_the_log_file(self):
        self.write_cfg({"soft_pct": 101})
        log_dir = scratch("console-log-" + self._testMethodName)
        with mock.patch.object(cli, "LOG_DIR_DEFAULT", log_dir):
            rc = self.main("status")
        self.assertEqual(rc, 2)
        self.assertIn("soft_pct", self.err.getvalue())
        self.assertFalse(os.path.isfile(os.path.join(log_dir, cli.CONSOLE_LOG_FILENAME)))


class TestStatusCommand(MainBase):
    def test_status_json(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.assertEqual(self.main("status", "--json"), 0)
        text = self.out.getvalue()
        doc = json.loads(text)
        self.assertEqual(set(doc), set(STATUS_KEYS))
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn(ACC_A, text)
        self.assertEqual(self.cfg_seen[0].state_dir, CONFIG_DIR_DEFAULT)

    def test_status_table(self):
        self.assertEqual(self.main("status"), 0)
        lines = self.out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("Claude 계정 사용량"))
        self.assertEqual([cells(l)[1] for l in account_rows(lines)], ["user #0000"] * 3)  # same label+plan: id prefix
        self.assertNotIn("\x1b", self.out.getvalue())  # not a terminal: no color, no screen control

    def test_status_watch(self):
        self.env.wait_hook = lambda n: (_ for _ in ()).throw(KeyboardInterrupt()) if n == 2 else None
        self.assertEqual(self.main("status", "--watch"), 0)
        self.assertEqual(len(self.frames()), 2)
        self.assertEqual(self.env.waits, [1.0, 1.0])
        self.env.waits.clear()
        self.assertEqual(self.main("status", "--watch", "--interval", "2.5"), 0)
        self.assertEqual(self.env.waits, [2.5, 2.5])
        for bad in ("0", "-1"):
            self.assertEqual(self.main("status", "--watch", "--interval", bad), 2)
            self.assertIn("--interval must be > 0", self.err.getvalue())

    def test_watch_without_daemon_fetches_per_poll_not_per_redraw(self):
        self.stop_after(61)
        self.assertEqual(self.main("status", "--watch"), 0)
        self.assertEqual(len(self.frames()), 61)
        self.assertEqual(len(self.env.fetches()), 9)
        self.assertEqual(self.env.waits, [1.0] * 61)

    def test_watch_age_moves_between_fetches(self):
        self.env.usages = {a: live(a, 20.0) for a in (ACC_A, ACC_B, ACC_C)}
        self.stop_after(61)
        self.main("status", "--watch")
        frames = self.frames()
        a1 = cells(account_rows(frames[0].splitlines())[0])
        a6 = cells(account_rows(frames[5].splitlines())[0])
        self.assertEqual(a1[7], "0초 전")
        self.assertEqual(a6[7], "5초 전")

    def _watch_with_snapshot(self, alive):
        self.env.alive_pids = {4242} if alive else set()
        state = os.path.join(self.state_dir, "state.json")

        def write_snap():
            self.env.files[state] = snapshot_doc(self.env, self.env.now_v - timedelta(seconds=5))

        write_snap()

        def hook(k):
            self.env.advance(1)
            write_snap()
            if k == 10:
                raise KeyboardInterrupt
        self.env.wait_hook = hook
        self.assertEqual(self.main("status", "--watch"), 0)
        self.assertEqual(len(self.frames()), 10)

    def test_watch_fresh_daemon_snapshot_no_fetch(self):
        self._watch_with_snapshot(alive=True)
        self.assertEqual(self.env.fetches(), [])

    def test_watch_dead_daemon_fetches_once(self):
        self._watch_with_snapshot(alive=False)
        self.assertEqual(len(self.env.fetches()), 3)

    def test_watch_rereads_only_every_session_poll(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.stop_after(25)
        self.assertEqual(self.main("status", "--watch"), 0)
        frames = self.frames()
        self.assertEqual(len(frames), 25)
        kinds = [c[0] for c in self.env.calls]
        # frames at t = 0..24 with session_poll_s 10: the document is rebuilt at t = 0, 10 and 20 only
        self.assertEqual(kinds.count("discover_profiles"), 3)
        self.assertEqual(kinds.count("discover_sessions"), 3)
        self.assertEqual(kinds.count("read_pointer"), 3)
        self.assertEqual(kinds.count("list_dir"), 3)
        state = os.path.join(self.state_dir, "state.json")
        self.assertEqual(self.env.calls.count(("read", state)), 3)
        self.assertEqual(len(self.env.fetches()), 3)
        ages = [cells(account_rows(f.splitlines())[0])[7] for f in frames[:12]]
        self.assertEqual((ages[0], ages[9], ages[11]), ("0초 전", "9초 전", "11초 전"))
        self.assertIn("12:00:09  데몬: 꺼짐", frames[9].splitlines()[0])

    def test_watch_custom_session_poll(self):
        self.write_cfg({"session_poll_s": 4})
        self.stop_after(10)
        self.assertEqual(self.main("status", "--watch"), 0)
        self.assertEqual([c[0] for c in self.env.calls].count("discover_sessions"), 3)

    def test_watch_rate_limited_backoff(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        self.stop_after(121)
        self.assertEqual(self.main("status", "--watch"), 0)
        f = self.env.fetches()
        self.assertEqual(f.count(("fetch", ACC_B)), 2)
        self.assertEqual(f.count(("fetch", ACC_A)), 5)
        self.assertEqual(f.count(("fetch", ACC_C)), 5)
        b = cells(account_rows(self.frames()[-1].splitlines())[1])
        self.assertTrue(b[3].startswith("████▒▒▒▒▒  50% · "), b)
        self.assertTrue(b[7].endswith(" · 캐시 · 호출 제한"), b)

    def test_watch_follows_terminal_resize(self):
        sizes = iter(["70", "300"])

        def hook(k):
            self.env.advance(1.0)
            os.environ["COLUMNS"] = next(sizes, "300")  # the terminal is resized between redraws
            if k == 3:
                raise KeyboardInterrupt
        self.env.wait_hook = hook
        self.assertEqual(self.main("status", "--watch"), 0)
        widths = [max(cli.display_width(l) for l in tables(f.splitlines())[0]) for f in self.frames()]
        self.assertEqual(len(widths), 3)
        self.assertLessEqual(widths[1], 70)
        self.assertGreater(widths[0], 70)
        self.assertGreater(widths[2], 70)  # widened again (cells hold the ticking ages, so not byte-equal)

    def test_status_discovery_failure(self):
        self.env.profiles_raise = True
        self.assertEqual(self.main("status"), 1)
        self.assertIn("status failed: RuntimeError", self.err.getvalue())


class TtyIO(io.StringIO):
    def isatty(self):
        return True


class FakeTerm:
    """TermCtl stand-in: records the calls in order and never touches a real terminal."""

    def __init__(self, wide=False, probe_raises=False):
        self.calls: List[str] = []
        self.wide = wide
        self.probe_raises = probe_raises

    def enter_alt(self):
        self.calls.append("enter_alt")

    def leave_alt(self):
        self.calls.append("leave_alt")

    def hide_cursor(self):
        self.calls.append("hide_cursor")

    def show_cursor(self):
        self.calls.append("show_cursor")

    def probe_ambiguous_wide(self):
        self.calls.append("probe")
        if self.probe_raises:
            raise OSError("no reply")
        return self.wide


class Keys:
    """read_key stand-in: returns the scripted keys one per call, advancing the fake clock like a wait."""

    def __init__(self, env, keys):
        self.env, self.keys, self.timeouts = env, list(keys), []

    def __call__(self, timeout):
        self.timeouts.append(timeout)
        self.env.advance(timeout)
        k = self.keys.pop(0) if self.keys else None
        if isinstance(k, BaseException) or (isinstance(k, type) and issubclass(k, BaseException)):
            raise k
        if callable(k):
            return k()
        return k


class TestWatchTerminal(MainBase):
    def run_main(self, *argv, term=None, keys=None, out=None):
        self.out = out if out is not None else io.StringIO()
        self.err = io.StringIO()
        return main(["--config", self.cfg_path] + list(argv), make_deps=self.make_deps, launchctl=FakeRunner(),
                    uid=lambda: 501, stdout=self.out, stderr=self.err, tz=timezone.utc, term_ctl=term,
                    read_key=keys)

    def term_frames(self):
        return [f for f in self.out.getvalue().split("\x1b[H\x1b[2J") if f]

    @unittest.skipIf(sys.platform == "win32", "os.kill(SIGTERM) on Windows terminates without running handlers")
    def test_a_second_sigterm_does_not_cut_the_cleanup_short(self):
        # A signal to the whole process group reaches the dashboard twice when started through bin/rotator.cjs
        # (once directly, once passed on by the launcher). Both may land while the first is being handled.
        seen = []
        def outer(*_):
            seen.append("outer")
        prev = signal.signal(signal.SIGTERM, outer)
        self.addCleanup(signal.signal, signal.SIGTERM, prev)

        class SignalledTerm(FakeTerm):
            def show_cursor(self):  # the cleanup itself: a late copy of the signal lands here
                os.kill(os.getpid(), signal.SIGTERM)
                super().show_cursor()

        def hook(k):
            self.env.advance(1.0)
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
        self.env.wait_hook = hook
        term = SignalledTerm()
        self.assertEqual(self.run_main("status", "--watch", term=term), 0)
        self.assertEqual(term.calls, ["hide_cursor", "enter_alt", "probe", "show_cursor", "leave_alt"])
        self.assertEqual(seen, [])  # neither copy leaked to the handler that was there before
        self.assertIs(signal.getsignal(signal.SIGTERM), outer)  # and it is back in place afterwards

    def test_q_quits_and_restores(self):
        term, keys = FakeTerm(), Keys(self.env, [None, None, "q"])
        self.assertEqual(self.run_main("status", "--watch", term=term, keys=keys), 0)
        self.assertEqual(term.calls, ["hide_cursor", "enter_alt", "probe", "show_cursor", "leave_alt"])
        self.assertEqual(len(self.term_frames()), 3)
        self.assertEqual(keys.timeouts, [1.0, 1.0, 1.0])
        self.assertEqual(self.env.waits, [])  # the key reader is the frame clock; wait_stop is not used
        self.assertNotIn("---", self.out.getvalue())
        self.assertIn("q 또는 Ctrl-C 종료", self.term_frames()[0].splitlines())

    def test_other_keys_do_not_quit(self):
        for quit_key, frames in (("Q", 4), ("x\x1b[Aq", 4)):
            with self.subTest(quit_key=quit_key):
                term, keys = FakeTerm(), Keys(self.env, ["x", " ", "\x1b[A", quit_key])
                self.assertEqual(self.run_main("status", "--watch", term=term, keys=keys), 0)
                self.assertEqual(len(self.term_frames()), frames)

    def test_interval_passed_to_key_reader(self):
        keys = Keys(self.env, [None, "q"])
        self.assertEqual(self.run_main("status", "--watch", "--interval", "2.5", term=FakeTerm(), keys=keys), 0)
        self.assertEqual(keys.timeouts, [2.5, 2.5])

    def test_ctrl_c_restores(self):
        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--watch", term=term, keys=Keys(self.env, [None, KeyboardInterrupt])), 0)
        self.assertEqual(term.calls[-2:], ["show_cursor", "leave_alt"])
        self.assertEqual(len(self.term_frames()), 2)

    def test_exception_restores(self):
        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--watch", term=term, keys=Keys(self.env, [RuntimeError("x")])), 1)
        self.assertEqual(term.calls, ["hide_cursor", "enter_alt", "probe", "show_cursor", "leave_alt"])
        self.assertIn("status failed: RuntimeError", self.err.getvalue())
        self.env.profiles_raise = True
        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--watch", term=term, keys=Keys(self.env, [])), 1)
        self.assertEqual(term.calls[-2:], ["show_cursor", "leave_alt"])

    def test_sigterm_restores_and_handler_put_back(self):
        before = signal.getsignal(signal.SIGTERM)

        def deliver():
            handler = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(handler, before)
            handler(signal.SIGTERM, None)

        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--watch", term=term, keys=Keys(self.env, [None, deliver])), 0)
        self.assertEqual(term.calls[-2:], ["show_cursor", "leave_alt"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_sigterm_default_when_no_python_handler(self):
        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        real, seen = signal.signal, []

        def fake(sig, handler):
            prev = real(sig, handler)
            seen.append((sig, handler))
            return None if len(seen) == 1 else prev  # the previous handler was not installed from Python

        with mock.patch.object(cli.signal, "signal", side_effect=fake):
            self.assertEqual(self.run_main("status", "--watch", term=FakeTerm(), keys=Keys(self.env, ["q"])), 0)
        # install ours, ignore SIGTERM during the cleanup, then restore the default
        self.assertEqual([h for _, h in seen[1:]], [signal.SIG_IGN, signal.SIG_DFL])
        self.assertEqual(seen[-1], (signal.SIGTERM, signal.SIG_DFL))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_term_without_key_reader_uses_wait_stop(self):
        self.stop_after(2)
        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--watch", term=term), 0)
        self.assertEqual(self.env.waits, [1.0, 1.0])
        self.assertEqual(term.calls[-2:], ["show_cursor", "leave_alt"])
        self.assertIn("Ctrl-C 종료", self.term_frames()[0].splitlines())

    def test_probe_feeds_render(self):
        for wide, ascii_flag, probes in ((True, False, 1), (False, False, 1), (True, True, 0)):
            with self.subTest(wide=wide, ascii=ascii_flag):
                term = FakeTerm(wide=wide)
                argv = ["status", "--watch"] + (["--ascii"] if ascii_flag else [])
                with mock.patch.object(cli, "render", wraps=cli.render) as r:
                    self.assertEqual(self.run_main(*argv, term=term, keys=Keys(self.env, [None, "q"])), 0)
                self.assertEqual(term.calls.count("probe"), probes)
                self.assertEqual(r.call_count, 2)
                for c in r.call_args_list:
                    self.assertEqual(c.kwargs["ambiguous_wide"], wide and not ascii_flag)
                    self.assertEqual(c.kwargs["ascii"], ascii_flag)
                    self.assertEqual((c.kwargs["soft_pct"], c.kwargs["hard_pct"]), (85.0, 95.0))

    def test_probe_failure_means_narrow(self):
        term = FakeTerm(wide=True, probe_raises=True)
        with mock.patch.object(cli, "render", wraps=cli.render) as r:
            self.assertEqual(self.run_main("status", "--watch", term=term, keys=Keys(self.env, ["q"])), 0)
        self.assertFalse(r.call_args.kwargs["ambiguous_wide"])
        self.assertEqual(term.calls[-2:], ["show_cursor", "leave_alt"])

    def test_one_shot_probes_without_alt_screen(self):
        term = FakeTerm(wide=True)
        with mock.patch.object(cli, "render", wraps=cli.render) as r:
            self.assertEqual(self.run_main("status", term=term), 0)
        self.assertEqual(term.calls, ["probe"])
        self.assertTrue(r.call_args.kwargs["ambiguous_wide"])
        self.assertNotIn("\x1b[?1049h", self.out.getvalue())

    def test_non_finite_utilization_keeps_status_running(self):
        doc = json.loads('{"accounts": [{"account_id": "00000000-0000-0000-0000-000000000001", "label": "user",'
                         ' "source": "live", "five_hour": {"utilization": NaN, "resets_at": null},'
                         ' "seven_day": {"utilization": Infinity, "resets_at": null}}]}')
        with mock.patch.object(cli, "build_status", return_value=doc):
            self.assertEqual(self.run_main("status"), 0)
            self.assertEqual([c[3:5] for c in map(cells, account_rows(self.out.getvalue().splitlines()))],
                             [["-", "-"]])
            term = FakeTerm()
            self.assertEqual(self.run_main("status", "--watch", term=term, keys=Keys(self.env, [None, "q"])), 0)
            self.assertEqual(len(self.term_frames()), 2)
            self.assertEqual(term.calls[-2:], ["show_cursor", "leave_alt"])

    def test_bad_interval_before_any_probe(self):
        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--watch", "--interval", "0", term=term), 2)
        self.assertEqual(term.calls, [])
        self.assertEqual(self.out.getvalue(), "")

    def test_json_never_probes(self):
        term = FakeTerm()
        self.assertEqual(self.run_main("status", "--json", term=term), 0)
        self.assertEqual(term.calls, [])
        self.assertIn('"label": "user"', self.out.getvalue())

    def test_thresholds_from_config(self):
        self.write_cfg({"soft_pct": 70, "hard_pct": 90})
        with mock.patch.object(cli, "render", wraps=cli.render) as r:
            self.assertEqual(self.run_main("status"), 0)
        self.assertEqual((r.call_args.kwargs["soft_pct"], r.call_args.kwargs["hard_pct"]), (70.0, 90.0))
        self.assertIn("노랑 ≥70%  빨강 ≥90%", self.out.getvalue())

    def test_color_only_on_tty_without_no_color(self):
        self.env.usages[ACC_A] = live(ACC_A, 90.0)
        cases = [(io.StringIO(), {}, False), (TtyIO(), {}, True), (TtyIO(), {"NO_COLOR": "1"}, False),
                 (TtyIO(), {"NO_COLOR": ""}, True)]
        for out, env, colored in cases:
            with self.subTest(tty=out.isatty(), env=env):
                with mock.patch.dict(os.environ, env):
                    if "NO_COLOR" not in env:
                        os.environ.pop("NO_COLOR", None)
                    self.assertEqual(self.run_main("status", out=out), 0)
                self.assertEqual("\x1b[33m" in self.out.getvalue(), colored)

    @unittest.skipIf(sys.platform == "win32",
                     "TtyIO has no real console handle, so WindowsTerm cannot enable VT mode here; "
                     "see TestWindowsTerm for the Windows ANSI/fallback behaviour")
    def test_real_tty_watch_uses_alt_screen(self):
        self.stop_after(2)
        os.environ.pop("NO_COLOR", None)
        out = TtyIO()
        self.assertEqual(self.run_main("status", "--watch", out=out), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("\x1b[?25l\x1b[?1049h"), repr(text[:20]))
        self.assertTrue(text.endswith("\x1b[?25h\x1b[?1049l"), repr(text[-20:]))
        self.assertEqual(self.env.waits, [1.0, 1.0])  # stdin is not a terminal here: Ctrl-C only

    def pty_stdin(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return os.fdopen(slave, "rb", buffering=0, closefd=False)

    @unittest.skipIf(sys.platform == "win32", "pty is POSIX-only; the Windows key reader uses msvcrt")
    def test_tty_stdin_wires_real_key_reader(self):
        os.environ.pop("NO_COLOR", None)
        stdin = self.pty_stdin()
        self.stop_after(3)  # if wait_stop paced the frames instead, stop there and fail the waits assertion
        with mock.patch.object(cli.RealTerm, "read_key", autospec=True, side_effect=[None, "q"]) as rk:
            rc = main(["--config", self.cfg_path, "status", "--watch", "--interval", "2"], make_deps=self.make_deps,
                      stdout=TtyIO(), stderr=io.StringIO(), tz=timezone.utc, stdin=stdin)
        self.assertEqual(rc, 0)
        self.assertEqual([c.args[1] for c in rk.call_args_list], [2.0, 2.0])
        self.assertIs(rk.call_args.args[0].inp, stdin)
        self.assertEqual(self.env.waits, [])  # keys, not wait_stop, pace the frames

    def test_windows_console_wires_the_msvcrt_key_reader(self):
        # On Windows main() builds a WindowsTerm; its read_key must pace the frames so `q` quits.
        self.stop_after(3)  # if wait_stop paced the frames instead, stop there and fail the waits assertion
        with mock.patch.object(cli._win, "IS_WINDOWS", True), \
                mock.patch.object(cli._win, "read_key_msvcrt", side_effect=[None, "q"]) as rk:
            rc = main(["--config", self.cfg_path, "status", "--watch", "--interval", "2"], make_deps=self.make_deps,
                      stdout=TtyIO(), stderr=io.StringIO(), tz=timezone.utc, stdin=TtyIO())
        self.assertEqual(rc, 0)
        self.assertEqual([c.args[0] for c in rk.call_args_list], [2.0, 2.0])
        self.assertEqual(self.env.waits, [])  # keys, not wait_stop, pace the frames

    @unittest.skipIf(sys.platform == "win32", "pty is POSIX-only; the Windows key reader uses msvcrt")
    def test_stdin_defaults_to_sys_stdin_only_without_injected_stdout(self):
        stdin = self.pty_stdin()
        out = TtyIO()
        self.stop_after(3)
        with mock.patch.object(cli.sys, "stdin", stdin), mock.patch.object(cli.sys, "stdout", out), \
                mock.patch.object(cli.RealTerm, "read_key", autospec=True, side_effect=["q"]) as rk:
            rc = main(["--config", self.cfg_path, "status", "--watch"], make_deps=self.make_deps,
                      stderr=io.StringIO(), tz=timezone.utc)
        self.assertEqual(rc, 0)
        self.assertEqual(rk.call_count, 1)
        self.assertIs(rk.call_args.args[0].inp, stdin)
        self.assertEqual(self.env.waits, [])
        self.stop_after(1)
        self.env.waits.clear()
        with mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch.object(cli.RealTerm, "read_key", autospec=True, side_effect=["q"]) as rk:
            self.assertEqual(self.run_main("status", "--watch", out=TtyIO()), 0)
        rk.assert_not_called()  # an injected stdout owns the terminal: the real stdin is never read
        self.assertEqual(self.env.waits, [1.0])

    def test_ascii_flag_output(self):
        self.assertEqual(self.run_main("status", "--ascii"), 0)
        lines = self.out.getvalue().splitlines()
        self.assertTrue(any(l.startswith("+--+------------+------+") for l in lines), lines)
        self.assertFalse(any("│" in l or "─" in l for l in lines))

    def test_help_lists_ascii(self):
        self.assertEqual(self.run_main("status", "--help"), 0)
        self.assertIn("--ascii", self.out.getvalue())


@unittest.skipIf(sys.platform == "win32", "pty/termios are POSIX-only; see TestWindowsTerm for the Windows console")
class TestRealTerm(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(self._close)
        self.inp = os.fdopen(self.slave, "rb", buffering=0, closefd=False)
        self.out = io.TextIOWrapper(os.fdopen(self.slave, "wb", buffering=0, closefd=False), encoding="utf-8",
                                    write_through=True)

    def _close(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def attrs(self):
        a = termios.tcgetattr(self.slave)
        a[3] &= ~termios.PENDIN  # the kernel sets PENDIN on its own when the line discipline changes
        return a

    def read_master(self, until, timeout=2.0):
        buf, deadline = b"", time.monotonic() + timeout
        while until not in buf and time.monotonic() < deadline:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                buf += os.read(self.master, 1024)
        return buf

    def test_alt_screen_cursor_and_cbreak_restored(self):
        before = self.attrs()
        t = cli.RealTerm(self.out, self.inp)
        t.hide_cursor()
        t.enter_alt()
        self.assertEqual(termios.tcgetattr(self.slave)[3] & termios.ICANON, 0)  # cbreak while running
        t.show_cursor()
        t.leave_alt()
        self.assertEqual(self.attrs(), before)
        self.assertEqual(self.read_master(b"\x1b[?1049l"), b"\x1b[?25l\x1b[?1049h\x1b[?25h\x1b[?1049l")

    def test_read_key(self):
        t = cli.RealTerm(self.out, self.inp)
        t.enter_alt()
        try:
            self.assertIsNone(t.read_key(0.05))
            os.write(self.master, b"q")
            self.assertEqual(t.read_key(1.0), "q")
        finally:
            t.leave_alt()

    def test_read_key_without_terminal_sleeps(self):
        t = cli.RealTerm(io.StringIO(), io.BytesIO())
        with mock.patch.object(cli.time, "sleep") as sleep:
            self.assertIsNone(t.read_key(0.3))
        sleep.assert_called_once_with(0.3)

    def test_read_key_eof_does_not_spin(self):
        t = cli.RealTerm(self.out, self.inp)
        with mock.patch.object(cli.os, "read", side_effect=OSError(5, "EIO")), \
                mock.patch.object(cli.select, "select", return_value=([self.slave], [], [])), \
                mock.patch.object(cli.time, "sleep") as sleep:
            self.assertIsNone(t.read_key(0.4))
        sleep.assert_called_once_with(0.4)
        with mock.patch.object(cli.os, "read", return_value=b""), \
                mock.patch.object(cli.select, "select", return_value=([self.slave], [], [])), \
                mock.patch.object(cli.time, "sleep") as sleep:
            self.assertIsNone(t.read_key(0.4))
        sleep.assert_called_once_with(0.4)

    def probe_with_reply(self, reply, timeout=2.0):
        def answer():
            if self.read_master(b"\x1b[6n").endswith(b"\x1b[6n") and reply is not None:
                os.write(self.master, reply)

        before = self.attrs()
        th = threading.Thread(target=answer)
        th.start()
        try:
            return cli.RealTerm(self.out, self.inp).probe_ambiguous_wide(timeout)
        finally:
            th.join()
            self.assertEqual(self.attrs(), before)

    def test_probe_wide_narrow_and_silent(self):
        self.assertTrue(self.probe_with_reply(b"\x1b[24;3R"))
        self.assertFalse(self.probe_with_reply(b"\x1b[24;2R"))
        self.assertFalse(self.probe_with_reply(b"garbage", 0.3))
        started = time.monotonic()
        self.assertFalse(cli.RealTerm(self.out, self.inp).probe_ambiguous_wide())  # nobody answers: 0.2 s default
        self.assertLess(time.monotonic() - started, 1.5)

    def test_probe_writes_query_and_clears_line(self):
        def answer():
            self.seen = self.read_master(b"\x1b[6n")
            os.write(self.master, b"\x1b[1;2R")

        th = threading.Thread(target=answer)
        th.start()
        cli.RealTerm(self.out, self.inp).probe_ambiguous_wide()
        th.join()
        self.assertTrue(self.seen.endswith("\r─\x1b[6n".encode("utf-8")), self.seen)
        self.assertIn(b"\r\x1b[2K", self.read_master(b"\x1b[2K"))

    def test_probe_needs_terminals(self):
        self.assertFalse(cli.RealTerm(io.StringIO(), self.inp).probe_ambiguous_wide())
        self.assertFalse(cli.RealTerm(self.out, None).probe_ambiguous_wide())


class FakeFdOut(io.StringIO):
    """A writable stream that claims a real fd, so WindowsTerm's enable_vt_mode path is reachable."""

    def __init__(self, fd=7):
        super().__init__()
        self._fd = fd

    def fileno(self):
        return self._fd


class TestWindowsTerm(unittest.TestCase):
    """cli.WindowsTerm: msvcrt keys and VT-mode ANSI, with a plain fallback when VT mode cannot be enabled
    (e.g. an old console or a non-console stream) -- exercised through fakes on any OS."""

    def test_enter_leave_alt_write_ansi_when_vt_mode_succeeds(self):
        out = FakeFdOut()
        t = cli.WindowsTerm(out, None)
        with mock.patch.object(cli._win, "enable_vt_mode", return_value=True):
            t.hide_cursor()  # no-op before enter_alt: VT mode is not known yet
            t.enter_alt()
            t.hide_cursor()
            t.show_cursor()
            t.leave_alt()
        self.assertEqual(out.getvalue(), "\x1b[?1049h\x1b[?25l\x1b[?25h\x1b[?1049l")

    def test_no_ansi_when_vt_mode_fails(self):
        out = FakeFdOut()
        t = cli.WindowsTerm(out, None)
        with mock.patch.object(cli._win, "enable_vt_mode", return_value=False):
            t.enter_alt()
            t.hide_cursor()
            t.show_cursor()
            t.leave_alt()
        self.assertEqual(out.getvalue(), "")

    def test_no_ansi_without_a_real_fd(self):
        t = cli.WindowsTerm(io.StringIO(), None)  # plain StringIO: fileno() raises
        t.enter_alt()
        self.assertEqual(t.out.getvalue(), "")

    def test_read_key_uses_msvcrt_on_a_tty(self):
        t = cli.WindowsTerm(io.StringIO(), TtyIO())
        with mock.patch.object(cli._win, "read_key_msvcrt", return_value="q") as rk:
            self.assertEqual(t.read_key(1.5), "q")
        rk.assert_called_once_with(1.5)

    def test_read_key_without_a_tty_sleeps(self):
        t = cli.WindowsTerm(io.StringIO(), io.StringIO())  # not a tty
        with mock.patch.object(cli.time, "sleep") as sleep:
            self.assertIsNone(t.read_key(0.3))
        sleep.assert_called_once_with(0.3)

    def test_probe_ambiguous_wide_is_always_false(self):
        self.assertFalse(cli.WindowsTerm(io.StringIO(), None).probe_ambiguous_wide())


@unittest.skipIf(sys.platform == "win32", "daemon is macOS-only in this release; it refuses cleanly on Windows (see TestWindowsUnsupportedCommands)")
class TestDaemonCommand(MainBase):
    def test_daemon_dry_run_forces_flag(self):
        self.env.held = {os.path.join(self.state_dir, "daemon.lock")}
        self.assertEqual(self.main("daemon", "--dry-run"), 1)
        self.assertIs(self.cfg_seen[-1].dry_run, True)
        self.assertEqual(self.main("daemon"), 1)
        self.assertIs(self.cfg_seen[-1].dry_run, False)
        self.assertEqual(self.env.fetches(), [])


class _FinishedWorker:
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


@unittest.skipIf(sys.platform == "win32", "daemon/switch are macOS-only in this release; they refuse cleanly on Windows (see TestWindowsUnsupportedCommands)")
class TestCrossConfigContention(MainBase):
    """A daemon and a CLI started with different --config paths must contend on the same locks."""

    def test_daemon_and_cli_with_different_configs_share_locks(self):
        env = self.env
        env.usages = {ACC_A: live(ACC_A, 97.0), ACC_B: live(ACC_B, 20.0), ACC_C: live(ACC_C, 50.0)}
        env.sessions = [sess(101, ACC_A)]
        env.results = [result("continued", None, ok=True, step="continue_prompt")]
        other_cfg = os.path.join(scratch("cfg-other-" + self._testMethodName), "config.json")
        workers, handler, nested = [], [None], {}
        base_switch = env.deps().switch

        def run_other(*argv):
            out, err = io.StringIO(), io.StringIO()
            rc = main(["--config", other_cfg] + list(argv), make_deps=lambda cfg: env.deps(), launchctl=FakeRunner(),
                      uid=lambda: 501, stdout=out, stderr=err, tz=timezone.utc)
            return rc, err.getvalue()

        def switch(session, target, mode, **kw):
            # The daemon worker is inside switch_session and holds the session and pane locks.
            nested["switch"] = run_other("switch", "--session", "101", "--to", ACC_C, "--hard")
            nested["daemon"] = run_other("daemon")
            return base_switch(session, target, mode, **kw)

        def spawn(fn, name):
            workers.append(fn)
            return _FinishedWorker()

        def hook(n):
            workers[0]()
            handler[0]()

        env.wait_hook = hook
        # The daemon asks for held-token accounts before dispatching; the cli fake raises (fails closed).
        daemon_deps = env.deps(spawn=spawn, switch=switch, install_signals=lambda h: handler.__setitem__(0, h),
                               refresh_held=lambda due_only=False: [])
        out, err = io.StringIO(), io.StringIO()
        rc = main(["--config", self.cfg_path, "daemon"], make_deps=lambda cfg: daemon_deps, launchctl=FakeRunner(),
                  uid=lambda: 501, stdout=out, stderr=err, tz=timezone.utc)
        self.assertEqual(rc, 0)
        self.assertEqual(nested["switch"][0], 1)
        self.assertIn("switch already in progress for this session", nested["switch"][1])
        self.assertEqual(nested["daemon"][0], 1)
        self.assertEqual([c for c in env.calls if c[0] == "switch"], [("switch", 101, ACC_B, "hard")])
        self.assertTrue(any("another rotator daemon is running" in r for r in env.log_records))


class TestInstallWiring(unittest.TestCase):
    def test_kwargs(self):
        cfg = load_config(os.path.join(BASE, "missing", "config.json"))
        kw = cli._install_kwargs(cfg, "/Users/user/cfg/config.json")
        self.assertEqual(kw["config_path"], "/Users/user/cfg/config.json")
        self.assertEqual(kw["log_dir"], LOG_DIR_DEFAULT)
        self.assertEqual(kw["source_root"], REPO)
        self.assertEqual(kw["state_dir"], CONFIG_DIR_DEFAULT)
        self.assertEqual(kw["plist_path"], launchd.plist_path_default())
        self.assertEqual(kw["timeouts"], Timeouts())
        self.assertNotIn("repo_root", kw)

    @unittest.skipIf(sys.platform == "win32",
                     "install/uninstall are macOS-only in this release; they refuse cleanly on Windows "
                     "(see TestWindowsUnsupportedCommands)")
    def test_main_passes_state_dir_to_install_and_uninstall(self):
        seen = {}

        def fake(name):
            def call(**kw):
                seen[name] = kw
                return (0, name + " ok")
            return call
        cfg_path = os.path.join(scratch("wiring-" + self._testMethodName), "config.json")
        with mock.patch("rotator.launchd.install", fake("install")), \
                mock.patch("rotator.launchd.uninstall", fake("uninstall")):
            for verb in ("install", "uninstall"):
                out = io.StringIO()
                rc = main(["--config", cfg_path, verb], launchctl=FakeRunner(), uid=lambda: 501, stdout=out,
                          stderr=io.StringIO())
                self.assertEqual(rc, 0)
                self.assertEqual(out.getvalue(), verb + " ok\n")
        self.assertEqual(seen["install"]["state_dir"], CONFIG_DIR_DEFAULT)
        self.assertEqual(seen["install"]["source_root"], REPO)
        self.assertEqual(seen["uninstall"]["state_dir"], CONFIG_DIR_DEFAULT)
        self.assertEqual(seen["uninstall"]["plist_path"], launchd.plist_path_default())

    def test_schtasks_kwargs(self):
        cfg = load_config(os.path.join(BASE, "missing", "config.json"))
        kw = cli._schtasks_install_kwargs(cfg, "/Users/user/cfg/config.json")
        self.assertEqual(kw["config_path"], "/Users/user/cfg/config.json")
        self.assertEqual(kw["source_root"], REPO)
        self.assertEqual(kw["state_dir"], CONFIG_DIR_DEFAULT)
        self.assertEqual(kw["python_exe"], sys.executable)
        self.assertNotIn("plist_path", kw)
        self.assertNotIn("log_dir", kw)


class TestWindowsInstallWiring(unittest.TestCase):
    """install/uninstall dispatch to rotator.schtasks on Windows, and never touch os.getuid (TestInstallWiring
    covers the macOS/launchd dispatch)."""

    def test_install_and_uninstall_dispatch_to_schtasks_without_os_getuid(self):
        seen = {}

        def fake(name):
            def call(**kw):
                seen[name] = kw
                return (0, name + " ok")
            return call
        cfg_path = os.path.join(scratch("win-install-wiring-" + self._testMethodName), "config.json")
        with mock.patch("rotator.schtasks.install", fake("install")), \
                mock.patch("rotator.schtasks.uninstall", fake("uninstall")), \
                mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("os.getuid", create=True,
                           side_effect=AssertionError("os.getuid is not available on Windows")) as getuid:
            for verb in ("install", "uninstall"):
                out, err = io.StringIO(), io.StringIO()
                rc = main(["--config", cfg_path, verb], launchctl=FakeRunner(), uid=None, stdout=out, stderr=err)
                self.assertEqual(rc, 0)
                self.assertEqual(out.getvalue(), verb + " ok\n")
        getuid.assert_not_called()
        self.assertEqual(seen["install"]["state_dir"], CONFIG_DIR_DEFAULT)
        self.assertEqual(seen["install"]["source_root"], REPO)
        self.assertEqual(seen["install"]["python_exe"], sys.executable)
        self.assertNotIn("uid", seen["install"])
        self.assertEqual(seen["uninstall"]["state_dir"], CONFIG_DIR_DEFAULT)
        self.assertNotIn("uid", seen["uninstall"])
        self.assertNotIn("plist_path", seen["uninstall"])

    def test_install_failure_reports_the_message_on_stderr(self):
        cfg_path = os.path.join(scratch("win-install-fail-" + self._testMethodName), "config.json")
        with mock.patch("rotator.schtasks.install", return_value=(2, "USERNAME is not set")), \
                mock.patch("rotator.cli.platform.system", return_value="Windows"):
            out, err = io.StringIO(), io.StringIO()
            rc = main(["--config", cfg_path, "install"], launchctl=FakeRunner(), uid=None, stdout=out, stderr=err)
        self.assertEqual(rc, 2)
        self.assertEqual(err.getvalue(), "USERNAME is not set\n")
        self.assertEqual(out.getvalue(), "")


class TestBinEntrypoint(unittest.TestCase):
    def test_help_from_any_cwd(self):
        path = os.path.join(REPO, "bin", "rotator")
        self.assertTrue(os.access(path, os.X_OK))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.readline().rstrip("\n"), "#!/usr/bin/python3")
        p = subprocess.run([sys.executable, path, "--help"], cwd="/", capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in ("status", "daemon", "switch", "install", "uninstall", "doctor"):
            self.assertIn(name, p.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestRotatorCjsLauncher(unittest.TestCase):
    """bin/rotator.cjs: the npm "bin" entry npm's Windows shim can run (it cannot run bin/rotator's
    Python shebang directly). Finds a Python 3 per OS and forwards argv/exit code to bin/rotator."""

    def run_cjs(self, *argv):
        return subprocess.run(["node", os.path.join(REPO, "bin", "rotator.cjs")] + list(argv),
                              capture_output=True, text=True, timeout=30)

    def test_help_runs_the_real_rotator_entry(self):
        p = self.run_cjs("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in ("status", "daemon", "switch", "install", "uninstall", "doctor"):
            self.assertIn(name, p.stdout)

    def test_forwards_the_exit_code(self):
        p = self.run_cjs("status", "--bogus")
        self.assertEqual(p.returncode, 2)

    # A stand-in for bin/rotator: says "ready", then waits for a signal like `status --watch` does.
    ENTRY = (
        "import os, signal, sys, time\n"
        "marker = sys.argv[1]\n"
        "def done(name, code):\n"
        "    def handler(*_):\n"
        "        with open(marker, 'w') as f:\n"
        "            f.write(name)\n"
        "        sys.exit(code)\n"
        "    return handler\n"
        "signal.signal(signal.SIGINT, done('int', 0))\n"
        "signal.signal(signal.SIGTERM, done('term', 7))\n"
        "if len(sys.argv) > 2:\n"
        "    os.kill(os.getpid(), getattr(signal, sys.argv[2]))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
        "sys.exit(9)\n"
    )

    def start_cjs(self, name, *argv):
        d = scratch("cjs-" + name)
        entry = os.path.join(d, "entry.py")
        with open(entry, "w", encoding="utf-8") as f:
            f.write(self.ENTRY)
        marker = os.path.join(d, "marker")
        p = subprocess.Popen(["node", os.path.join(REPO, "bin", "rotator.cjs"), marker] + list(argv),
                             env=dict(os.environ, ROTATOR_LAUNCHER_ENTRY=entry), stdout=subprocess.PIPE,
                             text=True, start_new_session=True)
        self.addCleanup(self._reap, p)
        return p, marker

    @staticmethod
    def _reap(p):
        if p.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait(5)
        p.stdout.close()

    def wait_ready(self, p):
        self.assertEqual(p.stdout.readline().strip(), "ready")

    @unittest.skipIf(sys.platform == "win32", "POSIX signals and process groups")
    def test_ctrl_c_goes_to_the_child_which_finishes_and_sets_the_exit_code(self):
        p, marker = self.start_cjs("sigint")
        self.wait_ready(p)
        os.killpg(p.pid, signal.SIGINT)  # what a terminal does: the whole foreground group
        self.assertEqual(p.wait(10), 0)
        with open(marker, encoding="utf-8") as f:
            self.assertEqual(f.read(), "int")

    @unittest.skipIf(sys.platform == "win32", "POSIX signals and process groups")
    def test_sigterm_to_the_launcher_reaches_the_child(self):
        p, marker = self.start_cjs("sigterm")
        self.wait_ready(p)
        p.send_signal(signal.SIGTERM)  # the launcher only, as `kill <pid>` or `timeout` would
        self.assertEqual(p.wait(10), 7)
        with open(marker, encoding="utf-8") as f:
            self.assertEqual(f.read(), "term")

    @unittest.skipIf(sys.platform == "win32", "POSIX signals and process groups")
    def test_a_child_killed_by_a_signal_ends_the_launcher_by_the_same_signal(self):
        p, _ = self.start_cjs("sigkill", "SIGKILL")
        self.assertEqual(p.wait(10), -signal.SIGKILL)

    @unittest.skipIf(sys.platform == "win32", "POSIX signals and process groups")
    def test_a_child_killed_by_sigusr1_ends_the_launcher_with_128_plus_the_signal(self):
        # Node answers SIGUSR1 by opening its debugger instead of ending, so it is not raised again.
        p, _ = self.start_cjs("sigusr1", "SIGUSR1")
        self.assertEqual(p.wait(10), 128 + signal.SIGUSR1)


class TestPackage(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, "package.json"), encoding="utf-8") as f:
            self.pkg = json.load(f)

    def test_ships_the_app_and_runs_doctor(self):
        self.assertRegex(self.pkg["version"], r"^\d+\.\d+\.\d+$")  # launchd.release_version reads it
        self.assertEqual(self.pkg["bin"], {"rotator": "bin/rotator.cjs"})
        self.assertEqual(self.pkg["os"], ["darwin", "win32"])
        self.assertEqual(set(self.pkg["files"]),
                         {"bin/rotator", "bin/rotator.cjs", "rotator/*.py", "scripts/postinstall.js"})
        self.assertEqual(self.pkg["scripts"], {"postinstall": "node scripts/postinstall.js"})
        for rel in ("bin/rotator", "bin/rotator.cjs", "scripts/postinstall.js"):
            self.assertTrue(os.path.isfile(os.path.join(REPO, rel)), rel)

    def test_postinstall_names_the_agent_plist(self):
        with open(os.path.join(REPO, "scripts", "postinstall.js"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("'%s.plist'" % LABEL, text)
        self.assertIn("[ROTATOR, 'doctor']", text)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestPostinstall(unittest.TestCase):
    """Runs scripts/postinstall.js with a stub bin/rotator that records its arguments, under a scratch HOME."""

    def setUp(self):
        self.dir = scratch("postinstall-" + self._testMethodName)
        os.makedirs(os.path.join(self.dir, "pkg", "scripts"))
        os.makedirs(os.path.join(self.dir, "pkg", "bin"))
        shutil.copyfile(os.path.join(REPO, "scripts", "postinstall.js"),
                        os.path.join(self.dir, "pkg", "scripts", "postinstall.js"))
        self.calls = os.path.join(self.dir, "calls.txt")
        with open(os.path.join(self.dir, "pkg", "bin", "rotator"), "w", encoding="utf-8") as f:
            f.write("import os, sys\n"
                    "open(os.environ['STUB_CALLS'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    "sys.exit(int(os.environ.get('STUB_RC_' + sys.argv[1].upper(), '0')))\n")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, "Library", "LaunchAgents"))

    def run_script(self, global_install=True, plist=False, doctor_rc=0, install_rc=0):
        if plist:
            with open(os.path.join(self.home, "Library", "LaunchAgents", LABEL + ".plist"), "w") as f:
                f.write("<plist/>\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("npm_config_")}
        env.update(HOME=self.home, STUB_CALLS=self.calls, STUB_RC_DOCTOR=str(doctor_rc),
                   STUB_RC_INSTALL=str(install_rc))
        if global_install:
            env["npm_config_global"] = "true"
        p = subprocess.run(["node", os.path.join(self.dir, "pkg", "scripts", "postinstall.js")], env=env,
                           capture_output=True, text=True, timeout=60)
        try:
            with open(self.calls, encoding="utf-8") as f:
                calls = f.read().split()
        except FileNotFoundError:
            calls = []
        return p, calls

    def test_fresh_install_runs_doctor_only(self):
        p, calls = self.run_script()
        self.assertEqual((p.returncode, calls), (0, ["doctor"]), p.stderr)
        # A fresh machine has no task registered yet (real `schtasks /Query` against the live system, which
        # this harness cannot fake), so the Windows branch takes its "otherwise" next-steps line too.
        self.assertIn("rotator status --watch", p.stdout)
        self.assertIn("rotator install", p.stdout)

    def test_failed_doctor_fails_the_install(self):
        p, calls = self.run_script(plist=True, doctor_rc=1)
        self.assertEqual((p.returncode, calls), (1, ["doctor"]))
        self.assertIn("cannot run the rotator", p.stderr)

    @unittest.skipIf(sys.platform == "win32",
                     "on Windows, the fixture's `plist=True` has no effect: an upgrade is detected by a real "
                     "`schtasks /Query` against the live system, which this harness cannot fake")
    def test_global_upgrade_refreshes_the_agent(self):
        p, calls = self.run_script(plist=True)
        self.assertEqual((p.returncode, calls), (0, ["doctor", "install"]), p.stderr)

    def test_local_install_leaves_the_agent_alone(self):
        p, calls = self.run_script(global_install=False, plist=True)
        self.assertEqual((p.returncode, calls), (0, ["doctor"]), p.stderr)

    @unittest.skipIf(sys.platform == "win32",
                     "on Windows, the fixture's `plist=True` has no effect: an upgrade is detected by a real "
                     "`schtasks /Query` against the live system, which this harness cannot fake")
    def test_failed_refresh_does_not_fail_the_install(self):
        p, calls = self.run_script(plist=True, install_rc=1)
        self.assertEqual((p.returncode, calls), (0, ["doctor", "install"]))
        self.assertIn("run `rotator install`", p.stderr)


class TestOrcaGate(MainBase):
    def gated(self, *argv, reason="Orca: 1.4.200 is too old"):
        self.out, self.err = io.StringIO(), io.StringIO()
        calls = []
        rc = main(["--config", self.cfg_path] + list(argv), make_deps=self.make_deps, launchctl=FakeRunner(),
                  uid=lambda: 501, stdout=self.out, stderr=self.err, tz=timezone.utc,
                  orca_gate=lambda: calls.append(1) or reason)
        return rc, calls

    def test_refuses_every_command_that_uses_orca(self):
        # Darwin pinned: on Windows, switch refuses earlier for a different reason (see
        # TestWindowsUnsupportedCommands), before this orca_gate seam ever runs.
        with mock.patch("rotator.cli.platform.system", return_value="Darwin"):
            for argv in (["status", "--json"], ["daemon", "--dry-run"],
                        ["switch", "--session", "1", "--to", ACC_B], ["install"]):
                rc, calls = self.gated(*argv)
                self.assertEqual(rc, 2, argv)
                self.assertEqual(calls, [1])
                self.assertEqual(self.err.getvalue(),
                                 "cannot run here: Orca: 1.4.200 is too old (see: rotator doctor)\n")
                self.assertEqual(self.cfg_seen, [], argv)  # refused before any deps were built

    def test_doctor_and_uninstall_are_never_gated(self):
        # uninstall is excluded from the orca_gate check above on every OS (it must always be able to remove
        # the agent/task, even on a machine too old or missing Orca entirely).
        with mock.patch("rotator.launchd.uninstall", lambda **kw: (0, "uninstall ok")), \
                mock.patch("rotator.cli.platform.system", return_value="Darwin"):
            rc, calls = self.gated("uninstall")
        self.assertEqual((rc, calls), (0, []))
        with mock.patch("rotator.cli.doctor.orca_version", return_value="1.4.241"):
            rc, calls = self.gated("doctor")
        self.assertEqual(calls, [])
        self.assertIn("Orca", self.out.getvalue())

    def test_no_reason_runs(self):
        rc, calls = self.gated("status", "--json", reason=None)
        self.assertEqual((rc, calls), (0, [1]))

    def test_real_gate_only_without_injected_seams(self):
        with mock.patch("rotator.cli._real_orca_gate", side_effect=AssertionError("real gate")):
            self.assertEqual(self.main("status", "--json"), 0)
        with mock.patch("rotator.cli._real_orca_gate", return_value="macOS: Linux") as real, \
                mock.patch("rotator.cli.real_deps", side_effect=AssertionError("deps built")):
            err = io.StringIO()
            rc = main(["--config", self.cfg_path, "status", "--json"], stdout=io.StringIO(), stderr=err)
        self.assertEqual(rc, 2)
        real.assert_called_once_with()
        self.assertIn("macOS: Linux", err.getvalue())

    def test_real_gate_reads_system_and_orca(self):
        with mock.patch("rotator.cli.platform.system", return_value="Darwin"), \
                mock.patch("rotator.cli.doctor.orca_version", return_value="1.4.223"):
            self.assertIn("too old", cli._real_orca_gate())
        with mock.patch("rotator.cli.platform.system", return_value="Darwin"), \
                mock.patch("rotator.cli.doctor.orca_version", return_value="1.4.224"):
            self.assertIsNone(cli._real_orca_gate())

    def test_real_gate_on_windows_uses_the_cli_version_flag(self):
        which = {"orca.cmd": WIN_ORCA_CMD}.get
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.cli.shutil.which", side_effect=which), \
                mock.patch("rotator.cli.doctor.orca_version_windows", return_value="1.4.223") as ver:
            self.assertIn("too old", cli._real_orca_gate())
        ver.assert_called_once_with(WIN_ORCA_CMD)
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.cli.shutil.which", side_effect=which), \
                mock.patch("rotator.cli.doctor.orca_version_windows", return_value="1.4.241"):
            self.assertIsNone(cli._real_orca_gate())

    def test_real_gate_on_windows_without_orca_on_path(self):
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.cli.shutil.which", return_value=None):
            reason = cli._real_orca_gate()
        self.assertIn("not found", reason)
        self.assertIn(cli.WINDOWS_ORCA_CLI_WHERE, reason)


# Where Orca's Windows installer puts its CLI (fake user name).
WIN_ORCA_CMD = r"C:\Users\someone\AppData\Local\Programs\Orca\resources\bin\orca.cmd"


class TestWindowsOrcaCliPath(unittest.TestCase):
    """Which binary counts as Orca's CLI on Windows. Orca ships resources\\bin\\orca.cmd and a native
    resources\\bin\\orca.exe launcher; the app itself is Orca.exe in the install folder, which also matches
    `orca.exe` (Windows names ignore case) but is Electron: `--version` would open the app."""

    APP = r"C:\Users\someone\AppData\Local\Programs\Orca\Orca.exe"
    BIN = r"C:\Users\someone\AppData\Local\Programs\Orca\resources\bin"

    def path(self, found):
        return cli._windows_orca_cli_path(which=lambda name: found.get(name))

    def test_prefers_orca_cmd(self):
        cmd = self.BIN + r"\orca.cmd"
        self.assertEqual(self.path({"orca.cmd": cmd, "orca.exe": self.APP, "orca": self.APP}), cmd)

    def test_accepts_the_native_launcher_in_a_bin_folder(self):
        exe = self.BIN + r"\orca.exe"
        self.assertEqual(self.path({"orca.exe": exe}), exe)

    def test_skips_the_electron_app_exe(self):
        self.assertIsNone(self.path({"orca.exe": self.APP, "orca": self.APP}))

    def test_nothing_on_path(self):
        self.assertIsNone(self.path({}))

    def test_gate_never_runs_the_app_exe(self):
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.cli.shutil.which", side_effect=lambda n: self.APP if n != "orca.cmd" else None), \
                mock.patch("rotator.cli.doctor.orca_version_windows") as ver:
            reason = cli._real_orca_gate()
        ver.assert_not_called()
        self.assertIn("not found", reason)


class TestWindowsUnsupportedCommands(MainBase):
    """switch refuses cleanly on Windows (switching a running session needs rotator.switcher, macOS-only this
    release). daemon/install/uninstall/doctor/status are supported on Windows since stage 2 (see
    TestRunDaemonWindowsAutoSwitch in test_daemon.py and TestWindowsInstallWiring)."""

    def windows(self, *argv):
        self.out, self.err = io.StringIO(), io.StringIO()
        with mock.patch("rotator.cli.platform.system", return_value="Windows"):
            rc = main(["--config", self.cfg_path] + list(argv), make_deps=self.make_deps, launchctl=FakeRunner(),
                      uid=lambda: 501, stdout=self.out, stderr=self.err, tz=timezone.utc)
        return rc

    def test_switch_refuses_with_exit_2(self):
        rc = self.windows("switch", "--session", "1", "--to", ACC_B)
        self.assertEqual(rc, 2)
        self.assertIn("switch", self.err.getvalue())
        self.assertIn("not available on Windows yet", self.err.getvalue())
        self.assertEqual(self.cfg_seen, [])  # refused before any deps were built

    def test_refusal_names_the_experimental_flag(self):
        rc = self.windows("switch", "--session", "1", "--to", ACC_B)
        self.assertEqual(rc, 2)
        self.assertIn("experimental_windows_switch", self.err.getvalue())

    def test_switch_allowed_with_experimental_flag(self):
        self.write_cfg({"experimental_windows_switch": True})
        rc = self.windows("switch", "--session", "1", "--to", ACC_B)
        self.assertNotIn("not available on Windows yet", self.err.getvalue())
        # past the gate: deps were built and it fails for an unrelated reason (no such session).
        self.assertEqual(rc, 2)
        self.assertIn("no live Claude Code session", self.err.getvalue())
        self.assertEqual(len(self.cfg_seen), 1)

    def test_switch_dry_run_allowed_without_the_flag(self):
        # --dry-run sends nothing, so it always works on Windows (task brief item 3).
        rc = self.windows("switch", "--session", "1", "--to", ACC_B, "--dry-run")
        self.assertNotIn("not available on Windows yet", self.err.getvalue())
        self.assertEqual(rc, 2)
        self.assertIn("no live Claude Code session", self.err.getvalue())
        self.assertEqual(len(self.cfg_seen), 1)

    def test_status_is_not_refused_on_windows(self):
        rc = self.windows("status", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(self.err.getvalue(), "")

    # install/uninstall are no longer refused on Windows (stage 2): see TestWindowsInstallWiring, which mocks
    # rotator.schtasks.install/uninstall so the test never touches the real machine's app-copy state dir.
    # daemon is covered by TestRunDaemonWindowsAutoSwitch in test_daemon.py.


class _WinProcs:
    def __init__(self, table):
        self._table = table

    def read_env(self, pid):
        return None

    def is_alive(self, pid):
        return True

    def process_table(self):
        return self._table


class TestSwitchDryRunWindowsShell(MainBase):
    """``switch --dry-run`` on Windows previews the relaunch line in the shell detected for the session's
    claude parent process (task brief items 3, 5, 6)."""

    def setUp(self):
        super().setUp()
        self.env = standard_env()
        self.s = sess(1, ACC_A, argv=("claude",))
        self.env.sessions = [self.s]

    def _dry_run(self, table):
        self.out, self.err = io.StringIO(), io.StringIO()
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.switcher.IS_WINDOWS", True):
            rc = main(["--config", self.cfg_path, "switch", "--session", "1", "--to", ACC_B, "--dry-run"],
                      make_deps=lambda cfg: self.env.deps(procs=_WinProcs(table)), launchctl=FakeRunner(),
                      uid=lambda: 501, stdout=self.out, stderr=self.err, tz=timezone.utc)
        return rc

    def test_powershell_line_previewed_for_pwsh_parent(self):
        rc = self._dry_run({1: (20, "claude.exe"), 20: (1, "pwsh.exe")})
        self.assertEqual(rc, 0)
        self.assertIn("relaunch preview: Set-Location -LiteralPath ", self.out.getvalue())

    def test_posix_line_previewed_for_bash_parent(self):
        rc = self._dry_run({1: (20, "claude.exe"), 20: (1, "bash.exe")})
        self.assertEqual(rc, 0)
        self.assertIn("relaunch preview: cd ", self.out.getvalue())

    def test_unsupported_parent_preview_unavailable(self):
        rc = self._dry_run({1: (20, "claude.exe"), 20: (1, "cmd.exe")})
        self.assertEqual(rc, 0)
        self.assertIn("relaunch preview unavailable", self.out.getvalue())

    def test_process_table_misbehaving_seam_preview_unavailable_not_a_crash(self):
        # relaunch_shell only fails closed on OSError; a seam that breaks its own documented contract and
        # raises something else must still read as "unavailable" here, not crash the CLI (see rotator.cli._switch).
        class _Raising(_WinProcs):
            def process_table(self):
                raise RuntimeError("process table unavailable")

        self.out, self.err = io.StringIO(), io.StringIO()
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.switcher.IS_WINDOWS", True):
            rc = main(["--config", self.cfg_path, "switch", "--session", "1", "--to", ACC_B, "--dry-run"],
                      make_deps=lambda cfg: self.env.deps(procs=_Raising({})), launchctl=FakeRunner(),
                      uid=lambda: 501, stdout=self.out, stderr=self.err, tz=timezone.utc)
        self.assertEqual(rc, 0)
        self.assertIn("relaunch preview unavailable", self.out.getvalue())


class TestDoctorCommand(MainBase):
    def doctor(self, version="1.4.241", system="Darwin", cli_ok=True, stale=False):
        self.out, self.err = io.StringIO(), io.StringIO()
        with mock.patch("rotator.cli.doctor.orca_version", return_value=version), \
                mock.patch("rotator.cli.platform.system", return_value=system), \
                mock.patch("rotator.cli.os.access", return_value=cli_ok):
            return main(["--config", self.cfg_path, "doctor"], make_deps=self.make_deps, launchctl=FakeRunner(),
                        uid=lambda: 501, stdout=self.out, stderr=self.err, stale_check=lambda: stale)

    def line(self, name):
        return next(x for x in self.out.getvalue().splitlines() if (" %s " % name) in x)

    def test_all_good(self):
        self.write_cfg({"profiles_root": ROOT})
        self.assertEqual(self.doctor(), 0)
        self.assertTrue(self.line("Orca").startswith("[ok]"))
        self.assertTrue(self.line("Claude accounts").startswith("[ok]"))
        self.assertIn("%d under %s" % (len(self.env.profiles), ROOT), self.line("Claude accounts"))
        self.assertEqual(self.err.getvalue(), "")

    def test_old_orca_fails(self):
        self.assertEqual(self.doctor(version="1.4.223"), 1)
        self.assertTrue(self.line("Orca").startswith("[fail]"))

    def test_missing_cli_or_other_os_fails(self):
        self.assertEqual(self.doctor(cli_ok=False), 1)
        self.assertTrue(self.line("orca CLI").startswith("[fail]"))
        self.assertEqual(self.doctor(system="Linux"), 1)
        self.assertTrue(self.line("macOS").startswith("[fail]"))

    def test_bad_config_is_a_fail_line(self):
        self.write_cfg({"no_such_key": 1})
        self.assertEqual(self.doctor(), 1)
        self.assertTrue(self.line("config").startswith("[fail]"))
        self.assertIn("no_such_key", self.line("config"))
        self.assertTrue(self.line("Orca").startswith("[ok]"))  # the other checks still ran
        self.assertEqual(self.err.getvalue(), "")
        self.assertEqual(self.main("status", "--json"), 2)  # every other command still refuses it
        self.assertIn("config error", self.err.getvalue())

    def test_warnings_do_not_fail(self):
        self.env.profiles = []
        self.assertEqual(self.doctor(stale=True), 0)
        self.assertTrue(self.line("Claude accounts").startswith("[warn]"))

    def windows_doctor(self, cli_path=WIN_ORCA_CMD, version="1.4.241", runner=None, stale_check=None):
        self.out, self.err = io.StringIO(), io.StringIO()
        with mock.patch("rotator.cli.platform.system", return_value="Windows"), \
                mock.patch("rotator.cli.shutil.which", side_effect={"orca.cmd": cli_path}.get), \
                mock.patch("rotator.cli.doctor.orca_version_windows", return_value=version):
            return main(["--config", self.cfg_path, "doctor"], make_deps=self.make_deps,
                        launchctl=runner or FakeRunner(), uid=lambda: 501, stdout=self.out, stderr=self.err,
                        stale_check=stale_check)

    def test_windows_all_good(self):
        self.write_cfg({"profiles_root": ROOT})
        self.assertEqual(self.windows_doctor(), 0)
        self.assertTrue(self.line("Orca").startswith("[ok]"))
        self.assertTrue(self.line("orca CLI").startswith("[ok]"))
        self.assertTrue(self.line("background agent").startswith("[ok]"))
        self.assertIn("installed", self.line("background agent"))

    def test_windows_experimental_switch_off_by_default(self):
        self.assertEqual(self.windows_doctor(), 0)
        self.assertTrue(self.line("experimental switch").startswith("[warn]"))
        self.assertIn("experimental_windows_switch=true", self.line("experimental switch"))

    def test_windows_experimental_switch_on(self):
        self.write_cfg({"experimental_windows_switch": True})
        self.assertEqual(self.windows_doctor(), 0)
        self.assertTrue(self.line("experimental switch").startswith("[ok]"))

    def test_windows_background_agent_not_installed(self):
        self.assertEqual(self.windows_doctor(runner=FakeRunner({"/Query": (1, "", "")})), 0)
        self.assertTrue(self.line("background agent").startswith("[warn]"))
        self.assertIn("not installed", self.line("background agent"))

    def test_windows_background_agent_stale(self):
        self.assertEqual(self.windows_doctor(stale_check=lambda: True), 0)
        self.assertTrue(self.line("background agent").startswith("[warn]"))
        self.assertIn("older copy", self.line("background agent"))

    def test_windows_orca_not_on_path_fails(self):
        self.assertEqual(self.windows_doctor(cli_path=None, version=None), 1)
        self.assertTrue(self.line("Orca").startswith("[fail]"))
        self.assertTrue(self.line("orca CLI").startswith("[fail]"))

    def test_windows_old_orca_fails(self):
        self.assertEqual(self.windows_doctor(version="1.4.200"), 1)
        self.assertTrue(self.line("Orca").startswith("[fail]"))


# --- Task 14 ---------------------------------------------------------------------------------------------


@unittest.skipIf(sys.platform == "win32", "switch is macOS-only in this release; it refuses cleanly on Windows (see TestWindowsUnsupportedCommands)")
class TestSwitchCommand(MainBase):
    def setUp(self):
        super().setUp()
        self.env.usages = {ACC_A: live(ACC_A, 97.0), ACC_B: live(ACC_B, 20.0), ACC_C: live(ACC_C, 50.0)}
        self.s = sess(101, ACC_A)
        self.env.sessions = [self.s]

    def switches(self):
        return [c for c in self.env.calls if c[0] == "switch"]

    def sw(self, *extra, to=ACC_B, pid=101):
        return self.main("switch", "--session", str(pid), "--to", to, *extra)

    def test_unknown_pid_2(self):
        self.assertEqual(self.sw(pid=999), 2)
        self.assertIn("999", self.err.getvalue())
        self.assertEqual(self.switches(), [])

    def test_unmanaged_2(self):
        self.env.sessions = [Session(pid=101, session_id=SID1, cwd="/Users/user/proj", status="idle",
                                     config_dir="/Users/user/.claude", argv=("claude",))]
        self.assertEqual(self.sw(), 2)
        self.assertIn("unmanaged", self.err.getvalue())

    def test_unknown_and_same_account_2(self):
        self.assertEqual(self.sw(to="00000000-0000-0000-0000-0000000000ff"), 2)
        self.assertIn("unknown account", self.err.getvalue())
        self.assertEqual(self.sw(to=ACC_A), 2)
        self.assertIn("already runs on account", self.err.getvalue())
        self.assertEqual(self.switches(), [])

    def named(self):
        self.env.profiles = [dataclasses.replace(prof(ACC_A), email="rtb.team.1@example.com"),
                             dataclasses.replace(prof(ACC_B), email="rtb.team.11@example.com"),
                             dataclasses.replace(prof(ACC_C), email="Kim@other.com")]

    def test_to_accepts_account_name_and_email(self):
        for to in ("rtb.team.11", "RTB.Team.11", "rtb.team.11@example.com", "RTB.TEAM.11@Example.COM", ACC_B):
            self.env = standard_env()
            self.named()
            self.env.sessions = [sess(101, ACC_C)]
            self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
            self.assertEqual(self.sw(to=to), 0, to)
            self.assertEqual(self.switches(), [("switch", 101, ACC_B, "soft")], to)

    def test_name_is_exact_not_a_prefix(self):
        self.named()
        self.env.sessions = [sess(101, ACC_C)]
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.assertEqual(self.sw(to="rtb.team.1"), 0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_A, "soft")])

    def test_unknown_name_lists_accounts(self):
        self.named()
        self.assertEqual(self.sw(to="rtb.team"), 2)
        err = self.err.getvalue()
        self.assertIn("unknown account rtb.team", err)
        for label, email, aid in (("rtb.team.1", "rtb.team.1@example.com", ACC_A),
                                  ("rtb.team.11", "rtb.team.11@example.com", ACC_B), ("Kim", "Kim@other.com", ACC_C)):
            self.assertRegex(err, r"\n  %s +%s +%s" % (re.escape(label), re.escape(email), aid))
        self.assertEqual(self.switches(), [])

    def test_unknown_name_is_printed_without_control_characters(self):
        self.named()
        self.assertEqual(self.sw(to="x\x1b[2Jy"), 2)
        self.assertIn("unknown account x[2Jy;", self.err.getvalue())
        self.assertNotIn("\x1b", self.err.getvalue())

    def test_folder_id_wins_over_a_name(self):
        self.env.profiles = [dataclasses.replace(prof(ACC_A), email=ACC_B + "@example.com"),
                             dataclasses.replace(prof(ACC_B), email="b@example.com")]
        self.env.sessions = [sess(101, ACC_C)]
        self.env.profiles.append(prof(ACC_C))
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.assertEqual(self.sw(to=ACC_B), 0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "soft")])

    def test_display_name_is_a_name(self):
        self.env.profiles = [prof(ACC_A), dataclasses.replace(prof(ACC_B), email=None, display_name="홍길동")]
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.assertEqual(self.sw(to="홍길동"), 0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "soft")])

    def test_shared_name_is_ambiguous(self):
        other = "00000000-0000-0000-0000-00000000000d"
        self.env.profiles = [dataclasses.replace(prof(ACC_A), email="kim@a.com"),
                             dataclasses.replace(prof(ACC_B), email="kim@b.com"),
                             dataclasses.replace(prof(ACC_C), email="kim@c.com"),
                             dataclasses.replace(prof(other), email="lee@d.com")]
        self.assertEqual(self.sw(to="kim"), 2)
        err = self.err.getvalue()
        self.assertIn("account kim matches 3 accounts; name one by its folder id:", err)
        self.assertRegex(err, r"\n  kim +kim@b\.com +%s" % ACC_B)
        self.assertNotIn(other, err)
        self.assertEqual(self.switches(), [])
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.assertEqual(self.sw(to="kim@b.com"), 0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "soft")])

    def test_empty_to_and_id_prefix_are_unknown(self):
        self.named()
        self.assertEqual(self.sw(to=""), 2)
        self.assertIn("unknown account", self.err.getvalue())
        self.assertEqual(self.sw(to=ACC_B[:8]), 2)
        self.assertEqual(self.switches(), [])

    def test_folder_id_name_of_an_account_without_email(self):
        self.env.profiles = [prof(ACC_A), dataclasses.replace(prof("10000000-0000-0000-0000-00000000000b"), email=None,
                                                              display_name=None)]
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.assertEqual(self.sw(to="10000000"), 0)
        self.assertEqual([c[2] for c in self.switches()], ["10000000-0000-0000-0000-00000000000b"])

    def _assert_preview(self):
        text = self.out.getvalue()
        self.assertIn("decide: action=hard target=" + ACC_B, text)
        self.assertIn("relaunch preview: ", text)
        self.assertIn("CLAUDE_CONFIG_DIR=", text)
        self.assertIn("dry run: nothing was sent", text)
        self.assertEqual([c for c in self.env.calls if c[0] in ("switch", "lock", "write")], [])

    def test_dry_run_preview(self):
        self.assertEqual(self.sw("--dry-run"), 0)
        self._assert_preview()
        self.write_cfg({"dry_run": True})
        self.env.calls.clear()
        self.assertEqual(self.sw(), 0)
        self._assert_preview()

    def test_dry_run_unbuildable(self):
        self.env.sessions = [sess(101, ACC_A, argv=())]
        self.assertEqual(self.sw("--dry-run"), 0)
        self.assertIn("relaunch preview unavailable", self.out.getvalue())

    def test_switch_ok(self):
        self.env.results = [result("continued", None, ok=True, step="continue_prompt", new_pid=303)]
        self.assertEqual(self.sw(), 0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "soft")])
        self.assertIn("ok=True", self.out.getvalue())
        self.assertIn(record_path(self.state_dir, SID1, 101), self.env.files)
        self.assertEqual(json.loads(self.env.files[record_path(self.state_dir, SID1, 101)])["source"], "cli")
        self.env = standard_env()
        self.env.sessions = [sess(102, ACC_A, sid=SID2)]
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.assertEqual(self.sw("--hard", pid=102), 0)
        self.assertEqual(self.switches(), [("switch", 102, ACC_B, "hard")])

    def test_switch_not_ok(self):
        cmd = "cd /x && claude --resume y"
        self.env.results = [result("exited", "exit_timeout", step="wait_exit", cmd=cmd)]
        self.assertEqual(self.sw(), 1)
        self.assertIn("relaunch: " + cmd, self.out.getvalue())
        self.assertIn("hint: " + STATE_HINTS["exited"], self.out.getvalue())

    def test_switch_hint_notes_stopped_tasks(self):
        r = dataclasses.replace(result("continued", None, ok=True, step="continue_prompt"), tasks_stopped=True)
        self.env.results = [r]
        self.assertEqual(self.sw(), 0)
        self.assertIn("hint: " + switcher.TASKS_STOPPED_HINT, self.out.getvalue())

    def test_busy(self):
        self.env.held = {daemon.lock_paths(self.state_dir, self.s)[0]}
        self.assertEqual(self.sw(), 1)
        self.assertIn("switch already in progress for this session", self.err.getvalue())
        self.assertEqual(self.switches(), [])

    def test_new_pid_of_inflight_session_busy(self):
        self.env.held = {daemon.lock_paths(self.state_dir, self.s)[0]}
        self.env.sessions = [sess(202, ACC_A)]
        self.assertEqual(self.sw(pid=202), 1)
        self.assertEqual(self.switches(), [])

    def test_refused_touched(self):
        rec = stored_record(101, res=result("exit_pending", "exit_timeout", step="wait_exit"), touched=True,
                            hint=STATE_HINTS["exit_pending"])
        self.env.files[record_path(self.state_dir, SID1, 101)] = json.dumps(rec)
        self.assertEqual(self.sw(), 1)
        self.assertIn("never switched again", self.err.getvalue())
        self.assertIn("state: exit_pending", self.err.getvalue())
        self.assertEqual(self.switches(), [])

    def test_hard_after_a_ctrl_c_runs_soft_and_says_so(self):
        rec = stored_record(101, res=result("interrupted", "interrupt_timeout", step="interrupt"), touched=True)
        self.env.files[record_path(self.state_dir, SID1, 101)] = json.dumps(rec)
        self.env.results = [result("continued", None, ok=True, step="continue_prompt", new_pid=303)]
        self.assertEqual(self.sw("--hard"), 0)
        self.assertEqual([c[3] for c in self.switches()], ["soft"])
        self.assertIn("ran as soft", self.out.getvalue())

    def test_plain_hard_has_no_soft_note(self):
        self.env.results = [result("continued", None, ok=True, step="continue_prompt", new_pid=303)]
        self.sw("--hard")
        self.assertEqual([c[3] for c in self.switches()], ["hard"])
        self.assertNotIn("ran as soft", self.out.getvalue())

    def test_intent_failed(self):
        self.env.fail_write = {os.sep + "sessions" + os.sep}
        self.assertEqual(self.sw(), 1)
        self.assertIn("nothing was sent", self.err.getvalue())
        self.assertEqual(self.switches(), [])

    def test_switch_raises_unknown_outcome(self):
        self.env.results = [RuntimeError()]
        self.assertEqual(self.sw(), 1)
        self.assertIn("outcome is unknown", self.err.getvalue())
        self.assertEqual(json.loads(self.env.files[record_path(self.state_dir, SID1, 101)])["record_state"], "in_progress")

    def test_auto_switch_false_still_switches(self):
        self.write_cfg({"auto_switch": False})
        self.env.results = [result()]
        self.assertEqual(self.sw(), 1)
        self.assertEqual(len(self.switches()), 1)



# --- Task 15 ---------------------------------------------------------------------------------------------


OFF = {"state": "skipped", "reason": "daemon_off", "at": None, "last_refreshed_at": None}
REFRESHED = {"state": "refreshed", "reason": "ok", "at": T0_ISO, "last_refreshed_at": T0_ISO}


class TestStatusRefresh(StatusBase):
    def snapshot(self, written_at, entries=None, accounts=(ACC_A, ACC_B, ACC_C)):
        doc = json.loads(snapshot_doc(self.env, written_at, accounts=accounts))
        for a in doc["accounts"]:
            if entries and a["account_id"] in entries:
                a["refresh"] = entries[a["account_id"]]
        self.env.files[self.state_path] = json.dumps(doc)
        self.env.alive_pids = {4242}

    def assert_no_refresh_call(self):
        # The cli fakes for refresh_token / refresh_held raise; reaching here means neither ran.
        self.assertIs(self.deps.refresh_token, self.deps.refresh_held)
        with self.assertRaises(AssertionError):
            self.deps.refresh_token()

    def test_daemon_off(self):
        doc = self.doc()
        for a in doc["accounts"]:
            self.assertEqual(a["refresh"], OFF)
        doc["accounts"][0]["refresh"]["reason"] = "mutated"
        self.assertEqual(refresh.DAEMON_OFF_RECORD["reason"], "daemon_off")
        self.assert_no_refresh_call()

    def test_fresh_snapshot_record_passes_through(self):
        self.snapshot(T0 - timedelta(seconds=5), {ACC_A: REFRESHED})
        doc = self.doc()
        self.assertEqual(self.account(doc, ACC_A)["refresh"], REFRESHED)
        self.assertIsNone(self.account(doc, ACC_B)["refresh"])  # older daemon: no key

    def test_running_daemon_missing_account_is_null(self):
        self.snapshot(T0 - timedelta(seconds=5), {ACC_A: REFRESHED}, accounts=(ACC_A, ACC_B))
        doc = self.doc()
        self.assertIsNone(self.account(doc, ACC_C)["refresh"])
        self.assertEqual(self.account(doc, ACC_C)["source"], "live")

    def test_stale_snapshot_is_null(self):
        self.snapshot(T0 - timedelta(minutes=10), {ACC_A: REFRESHED})
        doc = self.doc()
        for a in doc["accounts"]:
            self.assertIsNone(a["refresh"])

    def test_status_json_has_no_secret(self):
        self.snapshot(T0 - timedelta(seconds=5), {ACC_A: REFRESHED})
        text = json.dumps(self.doc())
        # The fixture profiles carry planted identity values; none may reach the status JSON.
        for planted in (UUID_SECRET, EMAIL, "SECRET-", "accessToken", "refreshToken"):
            self.assertNotIn(planted, text)
        self.assertIn(ACC_A, text)  # negative control: the document holds the accounts
        self.assertIn('"refresh"', text)


class TestRefreshReasons(unittest.TestCase):
    def test_every_display_token_has_text(self):
        for tok in sorted(refresh.REFRESH_REASONS - {"fresh", "ok", "write_recovered", "claude_code_warmup",
                                                      "claude_code_request"}):
            with self.subTest(tok=tok):
                self.assertIn(tok, cli._REASONS)

    def tokens(self, source, error, rec):
        return cli._reason_tokens({"source": source, "error": error, "refresh": rec})

    def test_joined_reasons_refresh_first(self):
        self.assertEqual(self.tokens("cache", "token_expired", {"state": "stopped", "reason": "invalid_grant"}),
                         "invalid_grant;token_expired")
        self.assertEqual(cli.reason_text(self.tokens("cache", "token_expired",
                                                     {"state": "stopped", "reason": "invalid_grant"})),
                         "재로그인 필요·토큰 만료")
        self.assertEqual(cli.reason_text(self.tokens("unavailable", "token_missing",
                                                     {"state": "skipped", "reason": "no_credential"})),
                         "토큰 없음")
        self.assertEqual(self.tokens("cache", None, {"state": "failed", "reason": "write_failed"}), "write_failed")

    def test_daemon_off_reason_stays_out_but_needs_refresh_shows(self):
        # daemon_off itself is never a cell text; an expired token under a stopped daemon reads 갱신 필요.
        for source in ("cache", "unavailable"):
            with self.subTest(source=source):
                self.assertEqual(self.tokens(source, "token_expired",
                                             {"state": "skipped", "reason": "daemon_off"}), "token_expired")
                self.assertEqual(self.state(source, "token_expired", OFF, running=False), "갱신 필요")
        lines = render(daemon_off(dashboard_doc()), T0, TZ9, 200)
        self.assertNotIn("데몬 꺼짐", "\n".join(lines[1:]))
        self.assertIn("갱신 필요", "\n".join(lines))

    def state(self, source, error, rec, running=True, sessions=()):
        return cli._token_state({"source": source, "error": error, "refresh": rec,
                                 "sessions": [{"pid": p} for p in sessions]}, running)

    def rec(self, state, reason):
        return {"state": state, "reason": reason, "at": None, "last_refreshed_at": None}

    def test_token_states(self):
        cases = [
            # expired, no session: daemon off -> 갱신 필요; daemon on and waiting -> 갱신 대기
            (("cache", "token_expired", OFF, False, ()), "갱신 필요"),
            (("unavailable", "token_expired;cache_missing", OFF, False, ()), "갱신 필요"),
            (("cache", "token_expired", None, True, ()), "갱신 대기"),
            (("cache", "token_expired", self.rec("skipped", "deferred"), True, ()), "갱신 대기"),
            (("cache", "token_expired", self.rec("not_needed", "fresh"), True, ()), "갱신 대기"),
            # a live Claude session on the account refreshes it (daemon on or off)
            (("cache", "token_expired", OFF, False, (7,)), "세션이 갱신"),
            (("cache", "token_expired", self.rec("skipped", "live_session"), True, (7,)), "세션이 갱신"),
            (("unavailable", "token_expired;cache_missing", self.rec("skipped", "live_session"), True, ()),
             "세션이 갱신"),
            # stopped until the item changes -> re-login
            (("cache", "token_expired", self.rec("stopped", "invalid_grant"), True, ()), "재로그인 필요"),
            (("cache", "token_expired", self.rec("stopped", "outcome_unknown"), True, ()), "재로그인 필요"),
            (("cache", "token_expired", self.rec("stopped", "rejected"), True, ()), "재로그인 필요"),
            (("unavailable", "token_missing;cache_missing", self.rec("skipped", "no_refresh_token"), True, ()),
             "재로그인 필요"),
            # write_failed (held) and the other failures keep the Korean map
            (("cache", "token_expired", self.rec("failed", "write_failed"), True, ()), "키체인 저장 실패"),
            (("cache", "token_expired", self.rec("failed", "server_error"), True, ()), "갱신 실패"),
            (("cache", "token_expired", self.rec("skipped", "cc_lock_busy"), True, ()), "갱신 중"),
            (("cache", "token_expired", self.rec("skipped", "disabled"), True, ()), "자동 갱신 꺼짐"),
            (("cache", "token_expired", self.rec("skipped", "dry_run"), True, ()), "드라이런"),
            (("unavailable", "token_missing;cache_missing", OFF, False, ()), "토큰 없음"),
            # usable token: no state
            (("live", None, self.rec("skipped", "live_session"), True, (7,)), None),
            (("live", None, OFF, False, ()), None),
            # the daemon's warm-up of a live-session account
            (("cache", "token_expired", self.rec("failed", "warmup_failed"), True, (7,)), "세션 계정 갱신 실패"),
            (("cache", "token_expired", self.rec("refreshed", "claude_code_warmup"), True, (7,)), "세션이 갱신"),
            (("live", None, self.rec("refreshed", "claude_code_request"), True, (7,)), None),
            (("cache", "http_429", self.rec("not_needed", "fresh"), True, ()), None),
            (("cache", "http_429", OFF, False, ()), None),
            (("cache", None, None, True, ()), None),
            (("cache", "http_429", self.rec("skipped", "disabled"), True, ()), None),
        ]
        for (source, error, rec, running, sessions), want in cases:
            with self.subTest(source=source, error=error, rec=rec, running=running, sessions=sessions):
                self.assertEqual(self.state(source, error, rec, running, sessions), want)

    def test_unchanged_cases(self):
        self.assertEqual(self.tokens("live", "token_expired", {"state": "skipped", "reason": "live_session"}),
                         "token_expired")
        self.assertEqual(self.tokens("cache", "token_expired", None), "token_expired")
        self.assertEqual(self.tokens("cache", "token_expired", {"state": "not_needed", "reason": "fresh"}),
                         "token_expired")
        self.assertEqual(self.tokens("cache", "token_expired", {"state": "refreshed", "reason": "ok"}),
                         "token_expired")
        self.assertEqual(self.tokens("cache", None, None), "")


class TestRenderRefresh(unittest.TestCase):
    lines = TestRender.lines
    row = TestRender.row

    def doc_with(self, **acc):
        doc = render_doc()
        doc["accounts"][0].update(acc)
        return doc

    def test_daemon_off_only_in_json(self):
        # The daemon is off: every record is daemon_off in the JSON, never in a table cell.
        env = standard_env()
        env.usages[ACC_A] = AccountUsage(account_id=ACC_A, source="cache", fetched_at=T0 - timedelta(minutes=12),
                                         five_hour=UsageWindow(50.0, T0 + timedelta(hours=1)), error="token_expired")
        env.usages[ACC_B] = AccountUsage(account_id=ACC_B, source="unavailable", fetched_at=T0,
                                         error="token_expired;cache_missing")
        doc = build_status(make_cfg(), env.deps(), T0)
        self.assertEqual([a["refresh"]["reason"] for a in doc["accounts"]], ["daemon_off"] * 3)
        self.assertIn("daemon_off", json.dumps(doc))
        lines = self.lines(doc, width=260)
        for row in account_rows(lines):
            self.assertNotIn("데몬 꺼짐", row)
        a, b = (cells(l) for l in account_rows(lines)[:2])  # rows sort by label, plan, id: ACC_A, ACC_B
        self.assertEqual(a[-1], "갱신 필요 · 12분 전 · 캐시")
        self.assertEqual(a[3], "████▒▒▒▒▒  50% · 1시간 뒤")  # floor(50 * 9 / 100) = 4 cells
        self.assertEqual(b[-1], "갱신 필요")
        self.assertIn(cli.REFRESH_FOOTER, lines)
        self.assertEqual(cli.REFRESH_FOOTER, "토큰 만료 계정은 데몬이 자동 갱신해: rotator install (또는 rotator daemon)")

    def test_footer_only_with_daemon_off_and_needs_refresh(self):
        self.assertIn(cli.REFRESH_FOOTER, render(daemon_off(dashboard_doc()), T0, TZ9, 200))
        self.assertNotIn(cli.REFRESH_FOOTER, render(dashboard_doc(), T0, TZ9, 200))  # daemon on
        doc = daemon_off(dashboard_doc())
        doc["accounts"] = [a for a in doc["accounts"] if a.get("source") == "live" or a["label"] == "frank"]
        lines = render(doc, T0, TZ9, 200)
        self.assertNotIn(cli.REFRESH_FOOTER, lines)  # daemon off, but no row needs a refresh
        self.assertIn("세션이 갱신", "\n".join(lines))
        for width in (80, 40, 10):
            with self.subTest(width=width):
                out = render(daemon_off(dashboard_doc()), T0, TZ9, width)
                for l in set(out) - RELAUNCH_LINES:
                    self.assertLessEqual(cli.display_width(l), width, l)
                self.assertTrue(any(l.startswith("토큰 만료") for l in out))

    def test_state_cells_in_table(self):
        lines = render(dashboard_doc(), T0, TZ9, 200)
        last = {cells(l)[1]: cells(l)[-1] for l in account_rows(lines)}
        self.assertEqual(last["dave"], "갱신 대기")
        self.assertEqual(last["erin"], "재로그인 필요 · 23시간 전 · 캐시")
        self.assertEqual(last["frank"], "세션이 갱신 · 12일 전 · 캐시")
        self.assertEqual(last["gina"], "키체인 저장 실패")
        self.assertEqual(last["alice"], "3초 전")
        off = {cells(l)[1]: cells(l)[-1] for l in account_rows(render(daemon_off(dashboard_doc()), T0, TZ9, 200))}
        self.assertEqual(off["dave"], "갱신 필요")
        self.assertEqual(off["erin"], "갱신 필요 · 23시간 전 · 캐시")
        self.assertEqual(off["frank"], "세션이 갱신 · 12일 전 · 캐시")
        self.assertEqual(off["carol"], "3일 전 · 캐시 · 호출 제한")

    def test_state_cell_clipped_with_ellipsis(self):
        doc = render_doc()
        doc["accounts"][0].update(source="cache", error="token_expired", fetched_at=(T0 - timedelta(hours=23)).isoformat(),
                                  refresh={"state": "failed", "reason": "write_failed", "at": None,
                                           "last_refreshed_at": None})
        self.assertEqual(self.row(self.lines(doc), "alice")[-1], "키체인 저장 실패 · 23시간 전 · 캐시")  # wide: whole
        cell = self.row(self.lines(doc, width=110), "alice")[-1]
        self.assertTrue(cell.startswith("키체인 저장 실패 · "), cell)
        self.assertTrue(cell.endswith("…"), cell)
        self.assertEqual(cli.display_width(cell), cli._AGE_CAP)

    def test_cache_past_reset_shows_zero_and_reason(self):
        doc = self.doc_with(source="cache", error="token_expired", five_hour=win(90.0, timedelta(minutes=-1)),
                            refresh={"state": "stopped", "reason": "invalid_grant", "at": None,
                                     "last_refreshed_at": None})
        row = self.row(self.lines(doc), "alice")
        joined = " ".join(row)
        self.assertEqual(row[3], "▒▒▒▒▒▒▒▒▒   0%")
        self.assertNotIn("90%", joined)
        self.assertEqual(row[-1], "재로그인 필요 · 12초 전 · 캐시")

    def test_unavailable_shows_refresh_reason(self):
        doc = self.doc_with(source="unavailable", error="token_expired;cache_missing", five_hour=None,
                            seven_day=None, refresh={"state": "failed", "reason": "write_failed", "at": None,
                                                     "last_refreshed_at": None})
        row = self.row(self.lines(doc, width=260), "alice")
        self.assertTrue(row[3].startswith("-  (키체인 저장 실패·토큰"), row)
        self.assertEqual(row[-1], "키체인 저장 실패")

    def test_cache_age_cell_leads_with_refresh_reason(self):
        doc = self.doc_with(source="cache", error="token_expired", five_hour=win(90.0, timedelta(minutes=-1)),
                            refresh={"state": "failed", "reason": "write_failed", "at": None,
                                     "last_refreshed_at": None})
        row = self.row(self.lines(doc), "alice")
        self.assertEqual(row[-1], "키체인 저장 실패 · 12초 전 · 캐시")

    def test_live_account_hides_live_session(self):
        doc = self.doc_with(refresh={"state": "skipped", "reason": "live_session", "at": None,
                                     "last_refreshed_at": None})
        self.assertNotIn("사용 중", " ".join(self.row(self.lines(doc), "alice")))


class TestReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            cls.text = f.read()

    def section(self, heading):
        start = self.text.index(heading + "\n")
        nxt = re.search(r"^#{1,3} ", self.text[start + len(heading):], re.M)
        return self.text[start:start + len(heading) + (nxt.start() if nxt else len(self.text))]

    def test_every_config_key_documented(self):
        for k in TOP_KEYS + TIMEOUT_KEYS:
            self.assertIn(k, self.text)
        self.assertIn(DEFAULT_CONTINUE_PROMPT, self.text)

    def test_token_refresh_documented(self):
        sec = self.section("## Idle-account token refresh")
        for needle in ("token_refresh", "2.1.296", "1.4.241", "security -i", ".oauth_refresh.lock", "restart",
                       "invalid_grant", "dry-run", "status --watch", "re-login"):
            self.assertIn(needle, sec)
        for tok in sorted(refresh.REFRESH_REASONS - {"fresh", "ok", "write_recovered", "claude_code_warmup",
                                                      "claude_code_request"}):
            self.assertIn(cli._REASONS[tok], sec, tok)
            self.assertIn("`%s`" % tok, sec, tok)
        self.assertIn("refresh", self.section("## Status JSON (schema 1)"))

    def test_refresh_residual_risks(self):
        sec = self.section("## Known residual risks")
        for needle in ("lock_changed", "reuse", "44", "restart", "2.1.296"):
            self.assertIn(needle, sec)

    def test_status_schema_documented(self):
        for k in STATUS_KEYS:
            self.assertIn(k, self.text)
        self.assertIn("schema", self.text)

    def test_label_and_commands(self):
        for needle in (LABEL, "rotator uninstall", "--dry-run", "relaunch_command"):
            self.assertIn(needle, self.text)

    def test_placeholders_only(self):
        for m in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", self.text):
            self.assertEqual(set(m), {"0", "-"}, m)
        for m in re.finditer("/Users/", self.text):
            self.assertTrue(self.text.startswith("<you>", m.end()), self.text[m.start():m.end() + 20])
        self.assertIsNone(re.search(r"term_[0-9A-Za-z]", self.text))
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", self.text))

    def test_fable_documented(self):
        section = self.section("## Fable usage")
        for needle in ("max(5h, 7d, fable)", "fable unknown", "1 MiB", "<synthetic>", "--model", "selected-host"):
            self.assertIn(needle, section)
        cadence = self.section("### Live usage cadence")
        for needle in ("Retry-After", "RFC 9110", "%d" % int(daemon.RETRY_AFTER_MAX_S)):
            self.assertIn(needle, cadence)
        rows = self.text.splitlines()
        self.assertIn("model", next(l for l in rows if l.startswith("| `unmanaged_sessions`")))
        self.assertIn("fable", next(l for l in rows if l.startswith("| `accounts`")))

    def test_cadence_documented(self):
        for needle in ("### Live usage cadence", "rate_limited", "--interval", "ago"):
            self.assertIn(needle, self.text)
        row = next(l for l in self.text.splitlines() if l.startswith("| `usage_poll_s`"))
        self.assertIn("30", row)
        self.assertIn(">= 15", row)
        cadence = self.section("### Live usage cadence")
        self.assertIn(str(int(USAGE_BACKOFF_MAX_S)), cadence)
        self.assertIn("%g" % cli.WATCH_INTERVAL_S, cadence)

    def test_dashboard_documented(self):
        self.assertNotIn("↻", self.text)
        self.assertNotIn("(캐시)", self.text)
        for needle in ("Claude 계정 사용량", "--ascii", "NO_COLOR", "q 또는 Ctrl-C 종료", "· 캐시", "토큰 만료", "시간 21분 뒤",
                       "1분 안", "갱신 필요", "갱신 대기", "세션이 갱신", "재로그인 필요", cli.REFRESH_FOOTER,
                       "`label`", "alternate screen", "플랜", "`plan`", "Max5x", "` #`"):
            self.assertIn(needle, self.text)
        block = self.text[self.text.index("```text\nClaude 계정 사용량"):]
        block = block[len("```text\n"):block.index("\n```", 8)].splitlines()
        for table in tables(block):
            self.assertEqual(len({cli.display_width(l) for l in table}), 1, "\n".join(table))
            self.assertEqual(len({tuple(border_cols(l, False)) for l in table}), 1)
        self.assertNotIn("in 1h12m", self.text)  # the old countdown wording is gone

    def test_residual_risks_listed(self):
        section = self.section("## Known residual risks")
        self.assertGreaterEqual(len(re.findall(r"^\d+\. ", section, re.M)), 13)


class TestReadmeWarmUp(unittest.TestCase):
    """The session warm-up is documented where the token refresh is."""

    setUpClass = classmethod(TestReadme.setUpClass.__func__)
    section = TestReadme.section

    def test_warmup_documented(self):
        sec = self.section("## Idle-account token refresh")
        for needle in ("session_token_warmup", "session_token_warmup_request", "Step A", "no model request",
                       "Step B", "haiku", "--no-session-persistence", "CLAUDE_CONFIG_DIR", "10 min", "6 h",
                       "`claude_code_warmup`", "`claude_code_request`", "`warmup_failed`", "세션 계정 갱신 실패",
                       "never in dry-run"):
            self.assertIn(needle, sec, needle)

    def test_warmup_config_rows(self):
        sec = self.section("## Configuration")
        self.assertIn("| `session_token_warmup` | `true` |", sec)
        self.assertIn("| `session_token_warmup_request` | `true` |", sec)

    def test_warmup_residual_risks(self):
        sec = self.section("## Known residual risks")
        for needle in ("warm-up", "hooks"):
            self.assertIn(needle, sec, needle)

    def test_warmup_label(self):
        self.assertEqual(cli._REASONS["warmup_failed"], "세션 계정 갱신 실패")


if __name__ == "__main__":
    unittest.main()
