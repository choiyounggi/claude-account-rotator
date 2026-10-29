import contextlib
import copy
import io
import json
import logging
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

from rotator import cli, daemon, launchd
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
                                                   "seven_day", "pointer", "sessions"})
        self.assertEqual(set(doc["sessions"][0]), {
            "pid", "session_id", "status", "account_id", "decision", "would_switch", "in_flight", "in_flight_pid",
            "stuck", "unswitchable", "hard_hold_until", "excluded", "record_state", "last_result", "hint",
            "post_verify"})

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
        self.assertEqual(doc["unmanaged_sessions"], [{"pid": 555, "session_id": SID2, "status": "idle"}])
        self.assertEqual([r["pid"] for r in doc["sessions"]], [101])
        self.assertEqual([x["pid"] for a in doc["accounts"] for x in a["sessions"]], [101])

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

    def test_no_secrets(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.env.pointer_text = prof(ACC_A).home
        text = json.dumps(self.doc())
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn(ACC_A, text)


# --- Task 12 ---------------------------------------------------------------------------------------------

TZ9 = timezone(timedelta(hours=9))


def render_doc():
    return {
        "schema": 1, "generated_at": T0_ISO,
        "daemon": {"running": True, "pid": 4242, "snapshot_age_s": 3.0, "flags": {"dry_run": False}},
        "accounts": [
            {"account_id": ACC_A, "source": "live", "error": None,
             "fetched_at": (T0 - timedelta(seconds=12)).isoformat(),
             "five_hour": {"utilization": 42.0, "resets_at": "2026-10-28T03:00:00+00:00"},
             "seven_day": {"utilization": 10.0, "resets_at": (T0 + timedelta(hours=1, minutes=12)).isoformat()},
             "pointer": True, "sessions": [{"pid": 101, "status": "idle"}]},
            {"account_id": ACC_B, "source": "unavailable", "error": "token_expired;cache_missing",
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


class TestRender(unittest.TestCase):
    def lines(self, doc=None, now=T0, width=200):
        return render(render_doc() if doc is None else doc, now, TZ9, width)

    def line_for(self, lines, aid):
        return next(l for l in lines if aid in l and l[1] == " " and l[2:].startswith(aid))

    def test_local_time(self):
        self.assertTrue(any("10-28 12:00" in l for l in self.lines()))

    def test_pointer_marker_only_on_pointer(self):
        lines = self.lines()
        self.assertTrue(self.line_for(lines, ACC_A).startswith("* "))
        self.assertTrue(self.line_for(lines, ACC_B).startswith("  "))

    def test_unavailable_with_error(self):
        self.assertIn("unavailable (token_expired;cache_missing)", self.line_for(self.lines(), ACC_B))

    def test_header_count(self):
        first = self.lines()[0]
        self.assertTrue(first.startswith("accounts: 2"))
        self.assertIn("running (pid 4242)", first)
        self.assertIn("at 21:00:00", first)

    def test_blocks_and_relaunch(self):
        lines = self.lines()
        for block in ("unmanaged:", "sessions:", "attention:"):
            self.assertIn(block, lines)
        self.assertIn("    relaunch: cd /x && claude --resume y", lines)
        self.assertIn("    relaunch: cd /y && claude", lines)
        self.assertTrue(any(l.startswith("    hint: ") for l in lines))
        self.assertTrue(any("decision hard -> " + ACC_B in l and "last wait_exit/exit_timeout/exited" in l
                            for l in lines))

    def test_session_flags(self):
        doc = render_doc()
        doc["sessions"][0].update(in_flight=True, in_flight_pid=101, stuck=True, unswitchable="no_argv",
                                  hard_hold_until="2026-10-28T12:05:00+00:00", record_state="in_progress",
                                  would_switch={"action": "soft", "target_account_id": ACC_C}, post_verify="ok")
        line = next(l for l in self.lines(doc, width=1000) if l.startswith("  pid 101"))
        for part in ("in flight (pid 101)", "STUCK", "unswitchable (no_argv)", "hard hold until 21:05",
                     "switch in progress", "would soft -> " + ACC_C, "verify ok"):
            self.assertIn(part, line)

    def test_width_boundary(self):
        for width in (60, 1, 0):
            with self.subTest(width=width):
                lines = self.lines(width=width)
                self.assertTrue(lines)
                for l in lines:
                    self.assertLessEqual(len(l), max(1, width))

    def test_empty(self):
        for doc in (None, dict(render_doc(), accounts=[])):
            lines = render(doc, T0, TZ9, 200)
            self.assertTrue(lines[0].startswith("accounts: 0"))
            self.assertTrue(any("no accounts" in l for l in lines))

    def test_pure(self):
        doc = render_doc()
        before = copy.deepcopy(doc)
        self.assertEqual(render(doc, T0, TZ9, 200), render(doc, T0, TZ9, 200))
        self.assertEqual(doc, before)

    def test_age_from_now(self):
        self.assertIn("live 12s ago", self.line_for(self.lines(), ACC_A))
        later = self.lines(now=T0 + timedelta(seconds=5))
        self.assertIn("live 17s ago", self.line_for(later, ACC_A))
        self.assertNotIn("ago", self.line_for(later, ACC_B))

    def test_countdown(self):
        a = self.line_for(self.lines(), ACC_A)
        self.assertIn("5h 42.0% (10-28 12:00, due)", a)
        self.assertIn("7d 10.0% (10-28 22:12, in 1h12m)", a)
        doc = render_doc()
        doc["accounts"][0]["five_hour"]["resets_at"] = None
        self.assertIn("42.0% (-, -)", self.line_for(self.lines(doc), ACC_A))
        at = self.line_for(self.lines(now=T0 + timedelta(hours=1, minutes=12)), ACC_A)
        self.assertIn("(10-28 22:12, due)", at)

    def test_dur_boundaries(self):
        cases = [(0, "0s"), (59.9, "59s"), (60, "1m00s"), (3599, "59m59s"), (3600, "1h00m"), (86399, "23h59m"),
                 (86400, "1d00h"), (-5, "0s")]
        for secs, want in cases:
            self.assertEqual(cli._dur(secs), want, secs)
        doc = render_doc()
        doc["accounts"][0]["fetched_at"] = (T0 + timedelta(seconds=3)).isoformat()
        self.assertIn("live 0s ago", self.line_for(self.lines(doc), ACC_A))

    def test_rate_limited_label(self):
        cases = [({"source": "cache", "error": "http_429", "fetched_at": (T0 - timedelta(minutes=10)).isoformat()},
                  "cache (rate_limited) 10m00s ago", True),
                 ({"source": "unavailable", "error": "http_429;cache_missing"},
                  "unavailable (rate_limited;cache_missing)", False),
                 ({"source": "cache", "error": "http_500"}, "cache (http_500)", True)]
        for patch, want, has_age in cases:
            with self.subTest(patch=patch):
                doc = render_doc()
                doc["accounts"][1].update(patch)
                line = self.line_for(self.lines(doc), ACC_B)
                self.assertIn(want, line)
                self.assertEqual("ago" in line, has_age)

    def test_ascii_only(self):
        for l in self.lines():
            self.assertTrue(l.isascii(), l)


# --- Task 13 ---------------------------------------------------------------------------------------------


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
        self.assertTrue(self.out.getvalue().splitlines()[0].startswith("accounts: 3"))

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
        a1 = next(l for l in frames[0].splitlines() if ACC_A in l)
        a6 = next(l for l in frames[5].splitlines() if ACC_A in l)
        self.assertIn("live 0s ago", a1)
        self.assertIn("live 5s ago", a6)

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
        ages = [next(l for l in f.splitlines() if ACC_A in l) for f in frames[:12]]
        self.assertIn("live 0s ago", ages[0])
        self.assertIn("live 9s ago", ages[9])
        self.assertIn("live 11s ago", ages[11])
        self.assertIn("at 12:00:09", frames[9].splitlines()[0])

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
        last = self.frames()[-1]
        self.assertIn("cache (rate_limited)", next(l for l in last.splitlines() if ACC_B in l))

    def test_status_discovery_failure(self):
        self.env.profiles_raise = True
        self.assertEqual(self.main("status"), 1)
        self.assertIn("status failed: RuntimeError", self.err.getvalue())


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

    def test_cadence_documented(self):
        for needle in ("### Live usage cadence", "rate_limited", "--interval", "ago"):
            self.assertIn(needle, self.text)
        row = next(l for l in self.text.splitlines() if l.startswith("| `usage_poll_s`"))
        self.assertIn("30", row)
        self.assertIn(">= 15", row)
        cadence = self.section("### Live usage cadence")
        self.assertIn(str(int(USAGE_BACKOFF_MAX_S)), cadence)
        self.assertIn("%g" % cli.WATCH_INTERVAL_S, cadence)

    def test_residual_risks_listed(self):
        section = self.section("## Known residual risks")
        self.assertGreaterEqual(len(re.findall(r"^\d+\. ", section, re.M)), 13)


if __name__ == "__main__":
    unittest.main()
