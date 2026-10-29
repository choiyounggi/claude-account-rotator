import contextlib
import copy
import dataclasses
import io
import itertools
import json
import logging
import os
import plistlib
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

from rotator import cli, daemon, launchd, refresh
from rotator.cli import STATUS_KEYS, build_status, main, render
from rotator.config import (
    CONFIG_DIR_DEFAULT, LOG_DIR_DEFAULT, TIMEOUT_KEYS, TOP_KEYS, Config, ConfigError, load_config, parse_config, prompt_ok,
)
from rotator.daemon import (
    IN_PROGRESS_HINT, ORPHANED_HINT, USAGE_BACKOFF_MAX_S, Deps, UsageCache, record_path, result_dict,
    usage_summary,
)
from rotator.launchd import LABEL, exit_timeout_for
from rotator.models import AccountUsage, Profile, Session, UsageWindow
from rotator.policy import DEFAULT_CONFIG
from rotator.switcher import DEFAULT_CONTINUE_PROMPT, STATE_HINTS, SwitchResult, Timeouts, switch_session

CFG = "/Users/user/Library/Application Support/claude-account-rotator-test/config.json"


def reader(files):
    def read(path):
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

    def test_empty_object(self):
        self.assertEqual(load("{}"), Config())

    def test_profiles_root_tilde_expanded(self):
        cfg = load('{"profiles_root": "~/x"}')
        self.assertTrue(os.path.isabs(cfg.profiles_root))
        self.assertTrue(cfg.profiles_root.endswith(os.sep + "x"))

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

    def test_policy_config_carries_overrides(self):
        cfg = load('{"cooldown_s": 60, "stale_max_age_s": 120}')
        pc = cfg.policy_config()
        self.assertEqual(pc.cooldown, timedelta(seconds=60))
        self.assertEqual(pc.stale_max_age, timedelta(seconds=120))


class TestConfigValidation(unittest.TestCase):
    CASES = [
        ('{"hard_pct": 101}', "hard_pct"),
        ('{"soft_pct": true}', "soft_pct"),
        ('{"soft_pct": "85"}', "soft_pct"),
        ('{"soft_pct": NaN}', "soft_pct"),
        ('{"soft_pct": 90, "hard_pct": 80}', "hard_pct"),
        ('{"soft_pct": 5, "target_headroom_pct": 10}', "target_headroom_pct"),
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
        ('{"profiles_root": "rel/x"}', "profiles_root"),
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
        self.assertTrue(str(cm.exception).startswith(CFG))

    def test_unparsable_names_file(self):
        with self.assertRaises(ConfigError) as cm:
            load("{")
        self.assertTrue(str(cm.exception).startswith(CFG))
        self.assertIn("not valid JSON", str(cm.exception))

    def test_unreadable_names_file_and_class(self):
        with self.assertRaises(ConfigError) as cm:
            load_config(CFG, read_text=raising_reader(PermissionError("denied")))
        self.assertTrue(str(cm.exception).startswith(CFG))
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
ORG = "Fixture Org Secret"
TOKEN = "sk-ant-oat01-FIXTURE-TOKEN-0000"
SECRETS = (EMAIL, UUID_SECRET, ORG, TOKEN, "Fixture User")
T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
SID1 = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"
T0_ISO = "2026-10-28T12:00:00+00:00"


def prof(aid):
    return Profile(account_id=aid, home=os.path.join(ROOT, aid, "home"),
                   keychain_service="Claude Code-credentials-0000", email=EMAIL, display_name="Fixture User",
                   account_uuid=UUID_SECRET, organization_name=ORG)


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
            discover_sessions=discover_sessions, switch=switch, make_driver=_NoDriver,
            procs=_NullProbe(), read_pointer=read_pointer, write_pointer=_not_used,
            now=lambda: env.now_v, monotonic=lambda: env.mono_v, spawn=_not_used, wait_stop=wait_stop, lock=lock,
            read_text=read_text, write_text=write_text, list_dir=list_dir, isdir=lambda p: True,
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
    def __init__(self, script=None):
        self.script = script or {}
        self.calls: List[tuple] = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        return self.script.get(argv[1], (0, "", ""))

    def verbs(self):
        return [argv[1] for argv, _ in self.calls]


class TestLaunchd(unittest.TestCase):
    all_calls: List[list] = []

    def setUp(self):
        self.agents = scratch("agents-" + self._testMethodName)
        self.plist = os.path.join(self.agents, LABEL + ".plist")
        self.logs = os.path.join(BASE, "logs-" + self._testMethodName)
        shutil.rmtree(self.logs, ignore_errors=True)

    def install(self, runner):
        rc = launchd.install(runner=runner, uid=501, plist_path=self.plist, repo_root="/Users/user/repo",
                             config_path="/Users/user/cfg/config.json", log_dir=self.logs, timeouts=Timeouts())
        TestLaunchd.all_calls.extend(argv for argv, _ in runner.calls)
        return rc

    def uninstall(self, runner):
        rc = launchd.uninstall(runner=runner, uid=501, plist_path=self.plist)
        TestLaunchd.all_calls.extend(argv for argv, _ in runner.calls)
        return rc

    def test_install_not_loaded(self):
        r = FakeRunner({"print": (113, "", "Could not find service")})
        rc, msg = self.install(r)
        self.assertEqual(rc, 0)
        self.assertIn(LABEL, msg)
        self.assertEqual([argv for argv, _ in r.calls], [
            ["/bin/launchctl", "print", "gui/501/" + LABEL],
            ["/bin/launchctl", "bootstrap", "gui/501", self.plist]])
        with open(self.plist, "rb") as f:
            pl = plistlib.load(f)
        self.assertEqual(pl["Label"], LABEL)
        self.assertEqual(pl["ExitTimeOut"], 440)
        self.assertGreaterEqual(pl["ExitTimeOut"], 380)
        self.assertEqual(pl["ProgramArguments"][0], "/usr/bin/python3")
        self.assertEqual(pl["ProgramArguments"][1], "/Users/user/repo/bin/rotator")
        self.assertEqual(pl["ProgramArguments"][-3:], ["--config", "/Users/user/cfg/config.json", "daemon"])
        self.assertIs(pl["RunAtLoad"], True)
        self.assertIs(pl["KeepAlive"], True)
        self.assertEqual(pl["StandardErrorPath"], os.path.join(self.logs, "launchd.err.log"))
        self.assertNotIn("EnvironmentVariables", pl)
        self.assertEqual(stat.S_IMODE(os.stat(self.plist).st_mode), 0o600)

    def test_install_loaded_reinstalls(self):
        r = FakeRunner()
        self.assertEqual(self.install(r)[0], 0)
        self.assertEqual(r.verbs(), ["print", "bootout", "bootstrap"])
        self.assertEqual(r.calls[1][1], 440 + 30.0)
        self.assertEqual(r.calls[1][0], ["/bin/launchctl", "bootout", "gui/501/" + LABEL])

    def test_install_bootstrap_failure(self):
        r = FakeRunner({"print": (113, "", ""), "bootstrap": (5, "", "Bootstrap failed: 5: Input/output error\n")})
        self.assertEqual(self.install(r), (1, "launchctl bootstrap failed: Bootstrap failed: 5: Input/output error"))

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
        self.assertEqual(r.calls[1][1], 440 + 30.0)
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
        self.assertEqual(r.calls[1][1], 440 + 30.0)

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
        self.assertEqual(len(TestLaunchd.all_calls), 2 + 3 + 2 + 1)
        for argv in TestLaunchd.all_calls:
            self.assertEqual(argv[0], "/bin/launchctl")
            if argv[1] == "bootstrap":
                self.assertEqual(argv[2], "gui/501")
                self.assertTrue(argv[3].endswith(LABEL + ".plist"))
            else:
                self.assertEqual(argv[2], "gui/501/" + LABEL)
            for part in argv:
                for bad in ("*", "com.apple", "com.rsquare"):
                    self.assertNotIn(bad, part)

    def test_exit_timeout_tracks_timeouts(self):
        self.assertEqual(exit_timeout_for(Timeouts()), 440)
        self.assertEqual(exit_timeout_for(Timeouts(ready_s=120.0)), exit_timeout_for(Timeouts()) + 60)

    def test_required_seams(self):
        with self.assertRaises(TypeError):
            launchd.install()
        with self.assertRaises(TypeError):
            launchd.uninstall(uid=501, plist_path=self.plist)

    def test_repo_root(self):
        self.assertEqual(launchd.repo_root(), REPO)
        self.assertTrue(launchd.plist_path_default().endswith("/Library/LaunchAgents/" + LABEL + ".plist"))


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
                                                   "seven_day", "fable", "label", "plan", "pointer", "sessions",
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
        self.put_record(stored_record(777, res=result("exited", "exit_timeout", cmd=cmd), touched=True,
                                      updated_at=hour_ago))
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

    def test_usage_warnings_not_printed(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        self.env.fetch_raises = {ACC_C}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            doc = self.doc()
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.account(doc, ACC_B)["error"], "http_429")

    def test_writes_nothing(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.doc()
        self.assertEqual([c for c in self.env.calls if c[0] in ("write", "lock")], [])

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
        cases = [(-5, "<1m"), (0, "<1m"), (59.9, "<1m"), (60, "1m"), (3599, "59m"), (3600, "1h"), (4860, "1h21m"),
                 (4890, "1h21m"), (86399, "23h59m"), (86400, "1d"), (90000, "1d1h"), (6 * 86400 + 86399, "6d23h")]
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
    ]
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
        self.assertEqual(cells(tables(lines)[1][1]), ["PID", "계정", "상태", "모델", "최근 전환 결과"])
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
        self.assertEqual(self.row(self.lines(doc), "a-very-long-accou…")[1], "a-very-long-accou…")
        self.assertEqual(cli.display_width("a-very-long-accou…"), 18)

    def test_unavailable_with_error(self):
        row = self.row(self.lines(), "bob")
        self.assertEqual(row[2:], ["-", "-  (토큰 만료·캐시 없음)", "-", "-", "0", "-"])

    def test_live_cell_bar_percent_remaining(self):
        doc = render_doc()
        doc["accounts"][0]["five_hour"] = win(89.4, timedelta(hours=1, minutes=21, seconds=30))
        doc["accounts"][0]["fable"] = win(25.0, timedelta(days=5))
        row = self.row(self.lines(doc), "alice")
        self.assertEqual(row[3], "████████▒  89% ↻1h21m")
        self.assertEqual(row[4], "▒▒▒▒▒▒▒▒▒  10% ↻1h12m")
        self.assertEqual(row[5], "█▒▒▒  25%")  # the fable cell never shows a reset
        self.assertEqual(row[6], "1")

    def test_countdown(self):
        row = self.row(self.lines(), "alice")
        self.assertEqual(row[3], "▒▒▒▒▒▒▒▒▒   0%")  # the 5h window reset at 03:00 UTC: 0% and no ↻
        doc = render_doc()
        doc["accounts"][0]["five_hour"]["resets_at"] = None
        self.assertEqual(self.row(self.lines(doc), "alice")[3], "███▒▒▒▒▒▒  42%")  # floor(42 * 9 / 100) = 3 cells
        at = self.row(self.lines(now=T0 + timedelta(hours=1, minutes=12)), "alice")
        self.assertEqual(at[4], "▒▒▒▒▒▒▒▒▒   0%")
        early = self.row(self.lines(now=T0 + timedelta(hours=1, minutes=11, seconds=30)), "alice")
        self.assertEqual(early[4], "▒▒▒▒▒▒▒▒▒  10% ↻<1m")

    def test_age_from_now(self):
        self.assertEqual(self.row(self.lines(), "alice")[7], "12초 전")
        later = self.lines(now=T0 + timedelta(seconds=5))
        self.assertEqual(self.row(later, "alice")[7], "17초 전")
        self.assertEqual(self.row(later, "bob")[7], "-")
        doc = render_doc()
        doc["accounts"][0]["fetched_at"] = (T0 + timedelta(seconds=3)).isoformat()
        self.assertEqual(self.row(self.lines(doc), "alice")[7], "0초 전")
        doc["accounts"][0]["fetched_at"] = None
        self.assertEqual(self.row(self.lines(doc), "alice")[7], "-")

    def test_rate_limited_label(self):
        cases = [({"source": "cache", "error": "http_429", "fetched_at": (T0 - timedelta(minutes=10)).isoformat(),
                   "five_hour": win(50.0, timedelta(hours=1))},
                  "(캐시)     50% ↻1h", "10분 전 (호출 제한)"),
                 ({"source": "unavailable", "error": "http_429;cache_missing"}, "-  (호출 제한·캐시 없음)", "-"),
                 ({"source": "cache", "error": "http_500", "fetched_at": T0_ISO, "five_hour": win(5.0, timedelta(0))},
                  "(캐시)      0%", "0초 전 (HTTP 500)")]
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
        lines = self.lines()
        self.assertEqual(self.session_row(lines, 101),
                         ["101", "alice", "idle", "-", "decision hard -> bob, last wait_exit/exit_timeout/exited"])
        self.assertEqual(self.session_row(lines, 555), ["555", "(관리 밖)", "busy", "-", "-"])
        self.assertEqual(lines[lines.index("  pid 101 재실행:") + 1], "cd /x && claude --resume y")
        self.assertIn("주의 (최근 24시간 실패한 전환)", lines)
        self.assertIn("  pid 777 final exited", lines)
        self.assertIn("  pid 777 힌트: look", lines)
        self.assertEqual(lines[lines.index("  pid 777 재실행:") + 1], "cd /y && claude")
        self.assertTrue(any(l.startswith("  pid 101 힌트: Claude Code exited") for l in lines))

    def test_long_detail_lines_wrap_not_cut(self):
        hint = STATE_HINTS["exited"]
        lines = self.lines(width=60)
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
                lines = self.lines(doc, width=width)
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
        self.assertNotIn("주의 (최근 24시간 실패한 전환)", lines)

    def test_session_flags(self):
        doc = render_doc()
        doc["sessions"][0].update(in_flight=True, in_flight_pid=101, stuck=True, unswitchable="no_argv",
                                  hard_hold_until="2026-10-28T12:05:00+00:00", record_state="in_progress",
                                  would_switch={"action": "soft", "target_account_id": ACC_C}, post_verify="ok")
        res = self.session_row(self.lines(doc, width=1000), 101)[4]
        for part in ("in flight (pid 101)", "STUCK", "unswitchable (no_argv)", "hard hold until 21:05",
                     "switch in progress", "would soft -> 00000000", "verify ok", "decision hard -> bob"):
            self.assertIn(part, res)

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
                lines = self.lines(dashboard_doc(), width=width)
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
        lines = self.lines(dashboard_doc(), ascii=True)
        for l in lines:
            self.assertTrue(all(ch.isascii() or "가" <= ch <= "힣" for ch in l), l)
        row = next(cells(l, ascii=True) for l in account_rows(lines, ascii=True) if cells(l, ascii=True)[1] == "alice")
        self.assertEqual(row[3], "########.  89% (1h21m)")
        self.assertIn("노랑 >=85%", "\n".join(lines))
        self.assertTrue(any("(토큰 만료/캐시 없음)" in l for l in lines))


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
                for width in (200, 160, 120, 105, 100, 80, 65, 50):
                    for color in (False, True):
                        with self.subTest(amb=amb, ascii=ascii, width=width, color=color):
                            lines = render(dashboard_doc(), T0, TZ9, width, ambiguous_wide=amb, ascii=ascii,
                                           color=color)
                            self.check(lines, amb, ascii, width)

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
        return 4

    def test_degradation_order(self):
        doc = dashboard_doc()
        cases = [(130, 0), (115, 1), (105, 2), (100, 3), (80, 4), (65, 4)]
        for width, want in cases:
            with self.subTest(width=width):
                lines = render(doc, T0, TZ9, width)
                self.assertEqual(self.level(lines), want)
                for l in lines:
                    self.assertLessEqual(cli.display_width(l), width)

    def test_level_never_decreases_as_width_shrinks(self):
        doc = dashboard_doc()
        levels = [self.level(render(doc, T0, TZ9, w)) for w in range(160, 39, -1)]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual((levels[0], levels[-1]), (0, 4))

    def test_full_table_kept_when_it_fits_exactly(self):
        doc = dashboard_doc()
        full = cli.display_width(tables(render(doc, T0, TZ9, 500))[0][0])
        self.assertEqual(self.level(render(doc, T0, TZ9, full)), 0)
        self.assertEqual(self.level(render(doc, T0, TZ9, full - 1)), 1)

    def test_label_shrinks_after_columns_are_gone(self):
        doc = dashboard_doc()
        lines = render(doc, T0, TZ9, 65)
        self.assertEqual(self.level(lines), 4)
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
                         [("100", "user Max5x", "decision soft -> user Team"),
                          ("101", "user Team", "decision soft -> user Max5x")])

    def test_suffix_survives_label_cut(self):
        long = "a-very-long-account-name-indeed"
        rows = self.rows(plan_doc([(ID_A1, long, "Team"), (ID_B2, long, "Team")]))
        self.assertEqual([r[1] for r in rows], ["a-very-long… #aaaa", "a-very-long… #bbbb"])
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
        self.assertTrue(b[3].startswith("(캐시)"), b)
        self.assertTrue(b[7].endswith("(호출 제한)"), b)

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
        self.assertEqual(len(seen), 2)
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
        daemon_deps = env.deps(spawn=spawn, switch=switch, install_signals=lambda h: handler.__setitem__(0, h))
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
        self.assertEqual(kw["repo_root"], REPO)
        self.assertEqual(kw["plist_path"], launchd.plist_path_default())
        self.assertEqual(kw["timeouts"], Timeouts())


class TestBinEntrypoint(unittest.TestCase):
    def test_help_from_any_cwd(self):
        path = os.path.join(REPO, "bin", "rotator")
        self.assertTrue(os.access(path, os.X_OK))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.readline().rstrip("\n"), "#!/usr/bin/python3")
        p = subprocess.run([sys.executable, path, "--help"], cwd="/", capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in ("status", "daemon", "switch", "install", "uninstall"):
            self.assertIn(name, p.stdout)


# --- Task 14 ---------------------------------------------------------------------------------------------


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
        rec = stored_record(101, res=result("interrupted", "interrupt_timeout"), touched=True,
                            hint=STATE_HINTS["interrupted"])
        self.env.files[record_path(self.state_dir, SID1, 101)] = json.dumps(rec)
        self.assertEqual(self.sw(), 1)
        self.assertIn("never switched again", self.err.getvalue())
        self.assertIn("state: interrupted", self.err.getvalue())
        self.assertEqual(self.switches(), [])

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
        for planted in (UUID_SECRET, EMAIL, ORG, "SECRET-", "accessToken", "refreshToken"):
            self.assertNotIn(planted, text)
        self.assertIn(ACC_A, text)  # negative control: the document holds the accounts
        self.assertIn('"refresh"', text)


class TestRefreshReasons(unittest.TestCase):
    def test_every_display_token_has_text(self):
        for tok in sorted(refresh.REFRESH_REASONS - {"fresh", "ok", "write_recovered"}):
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

    def test_daemon_off_never_in_cells(self):
        for source in ("cache", "unavailable"):
            with self.subTest(source=source):
                self.assertEqual(self.tokens(source, "token_expired",
                                             {"state": "skipped", "reason": "daemon_off"}), "token_expired")

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
        rows = [cells(l) for l in account_rows(lines)]
        self.assertTrue(any(c.endswith("(토큰 만료)") for r in rows for c in r), rows)

    def test_cache_past_reset_shows_zero_and_reason(self):
        doc = self.doc_with(source="cache", error="token_expired", five_hour=win(90.0, timedelta(minutes=-1)),
                            refresh={"state": "stopped", "reason": "invalid_grant", "at": None,
                                     "last_refreshed_at": None})
        row = self.row(self.lines(doc), "alice")
        joined = " ".join(row)
        self.assertIn("(캐시)", joined)
        five = next(c for c in row if "(캐시)" in c)
        self.assertIn("0%", five)
        self.assertNotIn("90%", joined)
        age = row[-1]
        self.assertTrue(age.startswith("12초 전 (재로그인"), age)

    def test_unavailable_shows_refresh_reason(self):
        doc = self.doc_with(source="unavailable", error="token_expired;cache_missing", five_hour=None,
                            seven_day=None, refresh={"state": "failed", "reason": "write_failed", "at": None,
                                                     "last_refreshed_at": None})
        five = self.row(self.lines(doc, width=260), "alice")[3]
        self.assertTrue(five.startswith("-  (키체인 저장 실패·토큰"), five)

    def test_cache_age_cell_leads_with_refresh_reason(self):
        doc = self.doc_with(source="cache", error="token_expired", five_hour=win(90.0, timedelta(minutes=-1)),
                            refresh={"state": "failed", "reason": "write_failed", "at": None,
                                     "last_refreshed_at": None})
        row = self.row(self.lines(doc), "alice")
        self.assertTrue(row[-1].startswith("12초 전 (키체인 저"), row)

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
        for tok in sorted(refresh.REFRESH_REASONS - {"fresh", "ok", "write_recovered"}):
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
        for needle in ("Claude 계정 사용량", "--ascii", "NO_COLOR", "q 또는 Ctrl-C 종료", "(캐시)", "토큰 만료", "↻",
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


if __name__ == "__main__":
    unittest.main()
