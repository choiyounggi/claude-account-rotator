import dataclasses
import inspect
import json
import logging
import logging.handlers
import os
import re
import shutil
import threading
import time
import stat
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

from rotator import daemon, refresh, switcher, usage
from rotator.config import Config
from rotator.daemon import (
    GONE_REASONS, IN_PROGRESS_HINT, Daemon, SharedUsage, UsageCache, UsageStore, rate_limited, shown_usage, real_deps, run_daemon, ORPHANED_HINT, STRUCTURAL_REASONS, TARGET_REASONS, TOUCHED_ONLY_REASONS, TRANSIENT_REASONS,
    Deps, FileLock, atomic_write_text, classify_untouched, identity_of, key_of, load_records, lock_paths,
    record_path, result_dict, setup_logging, touched, try_lock, usage_from_summary, usage_summary,
)
from rotator.models import AccountUsage, Profile, Session, UsageWindow
from rotator.switcher import ProcEnv, SwitchResult

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, ".claude", "tmp", "test-daemon")
ROOT = "/Users/user/Library/Application Support/orca-test/claude-profiles"
ACC_A = "00000000-0000-0000-0000-00000000000a"
ACC_B = "00000000-0000-0000-0000-00000000000b"
ACC_C = "00000000-0000-0000-0000-00000000000c"
EMAIL = "user@example.com"
UUID_SECRET = "00000000-0000-0000-0000-00000000aaaa"
ORG = "Fixture Org Secret"
TOKEN = "sk-ant-oat01-FIXTURE-TOKEN-0000"
T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
SID1 = "00000000-0000-4000-8000-000000000001"
SID2 = "00000000-0000-4000-8000-000000000002"
SECRETS = (EMAIL, UUID_SECRET, ORG, TOKEN)


def prof(aid):
    return Profile(account_id=aid, home=os.path.join(ROOT, aid, "home"),
                   keychain_service="Claude Code-credentials-0000", email=EMAIL, display_name="Fixture User",
                   account_uuid=UUID_SECRET, organization_name=ORG)


def sess(pid, aid, sid=SID1, handle="term_00000001", status="idle", started_at=T0):
    return Session(pid=pid, session_id=sid, cwd="/Users/user/proj", status=status,
                   config_dir=os.path.join(ROOT, aid, "home"), argv=("claude", "--settings", TOKEN),
                   terminal_handle=handle, started_at=started_at)


def live(aid, pct, now=T0):
    return AccountUsage(account_id=aid, source="live", fetched_at=now,
                        five_hour=UsageWindow(pct, now + timedelta(hours=1)),
                        seven_day=UsageWindow(10.0, now + timedelta(days=2)))


def result(state="untouched", reason="not_idle", ok=False, step="preflight", new_pid=None, cmd="",
           request_id=None):
    return SwitchResult(ok=ok, step=step, reason=reason, state=state, new_pid=new_pid, relaunch_command=cmd,
                        recoverable=state in ("exited", "relaunched", "verified"), request_id=request_id,
                        other_account_id=None)


def make_cfg(**kw):
    return Config(profiles_root=ROOT, state_dir=os.path.join(BASE, "state"), **kw)


class FakeWorker:
    def __init__(self, fn):
        self.fn = fn
        self.alive = True
        self.joins: List[Optional[float]] = []

    def run(self):
        try:
            self.fn()
        finally:
            self.alive = False

    def join(self, timeout=None):
        self.joins.append(timeout)

    def is_alive(self):
        return self.alive


class FakeLock:
    def __init__(self, env, path):
        self.env = env
        self.path = path

    def write_owner(self, pid):
        self.env.files[self.path] = str(pid)

    def release(self):
        self.env.held.discard(self.path)
        self.env.calls.append(("release", self.path))


class FakeProbe:
    def __init__(self):
        self.envs: Dict[int, object] = {}
        self.reads: List[int] = []

    def read_env(self, pid):
        self.reads.append(pid)
        v = self.envs.get(pid)
        if isinstance(v, Exception):
            raise v
        return v

    def is_alive(self, pid):
        return True

    def process_table(self):
        return {}


class FakeDriver:
    def __init__(self, env):
        self.env = env

    def send(self, *a, **k):
        self.env.driver_calls.append("send")
        raise AssertionError("driver used outside switch")

    def read_screen(self, *a, **k):
        self.env.driver_calls.append("read_screen")
        raise AssertionError("driver used outside switch")

    def wait_for(self, *a, **k):
        self.env.driver_calls.append("wait_for")
        raise AssertionError("driver used outside switch")


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


class FakeEnv:
    def __init__(self):
        self.profiles: List[Profile] = []
        self.sessions: List[Session] = []
        self.usages: Dict[str, AccountUsage] = {}
        self.pcts: Dict[str, float] = {}  # account -> utilization, served as a live usage fetched "now"
        self.fetch_raises: Set[str] = set()
        self.profiles_raise = False
        self.sessions_raise = False
        self.pointer_text: Optional[str] = None
        self.pointer_raises: Optional[Exception] = None
        self.files: Dict[str, str] = {}
        self.fail_write: Set[str] = set()
        self.held: Set[str] = set()
        self.results: List[object] = []
        self.calls: List[tuple] = []
        self.switch_kwargs: List[dict] = []
        self.spawned: List[FakeWorker] = []
        self.driver_calls: List[str] = []
        self.alive_pids: Set[int] = set()
        self.isdir_ok = True
        self.now_v = T0
        self.mono_v = 1000.0
        self.signal_handler = None
        self.waits: List[float] = []
        self.wait_hook: Optional[Callable[[int], bool]] = None
        self.probe = FakeProbe()
        self.log_records: List[str] = []
        self._logger: Optional[logging.Logger] = None
        self.refresh_calls: List[tuple] = []
        self.gate_results: Dict[str, dict] = {}
        self.refresh_results: Dict[str, dict] = {}
        self.held_ids: List[str] = []
        self.held_due: List[str] = []
        self.held_raises = False

    def advance(self, seconds):
        self.now_v = self.now_v + timedelta(seconds=seconds)
        self.mono_v += seconds

    def run_worker(self, i=0):
        self.spawned[i].run()

    def logger(self):
        if self._logger is None:
            lg = logging.getLogger("rotator.test.%d" % id(self))
            lg.propagate = False
            lg.setLevel(logging.INFO)
            for h in list(lg.handlers):
                lg.removeHandler(h)
            lg.addHandler(_ListHandler(self.log_records))
            self._logger = lg
        return self._logger

    def logs(self, event):
        return [r for r in self.log_records if ("event=%s" % event) in r.split()]

    def records_on_disk(self):
        return {p: json.loads(t) for p, t in self.files.items()
                if os.sep + daemon.SESSIONS_DIRNAME + os.sep in p and p.endswith(".json")}

    def deps(self, **overrides):
        env = self

        def discover_profiles(root):
            if env.profiles_raise:
                raise RuntimeError("profiles boom")
            return list(env.profiles)

        def fetch_usage(p):
            env.calls.append(("fetch", p.account_id))
            if p.account_id in env.fetch_raises:
                raise RuntimeError("fetch boom")
            if p.account_id in env.pcts:
                return live(p.account_id, env.pcts[p.account_id], now=env.now_v)
            return env.usages[p.account_id]

        def discover_sessions(homes):
            if env.sessions_raise:
                raise RuntimeError("sessions boom")
            return list(env.sessions)

        def switch(session, target, mode, **kw):
            env.calls.append(("switch", session.pid, target.account_id, mode))
            env.switch_kwargs.append(kw)
            r = env.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def read_pointer(root):
            if env.pointer_raises is not None:
                raise env.pointer_raises
            return env.pointer_text

        def write_pointer(p, root):
            env.calls.append(("write_pointer", p.account_id))
            return None

        def spawn(fn, name):
            env.calls.append(("spawn", name))
            w = FakeWorker(fn)
            env.spawned.append(w)
            return w

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
            return FakeLock(env, path)

        def read_text(path):
            if path in env.files:
                return env.files[path]
            raise FileNotFoundError(path)

        def write_text(path, text):
            if any(s in path for s in env.fail_write):
                raise OSError("fake write failure")
            env.files[path] = text
            env.calls.append(("write", path))

        def list_dir(path):
            prefix = path + os.sep
            names = sorted({k[len(prefix):] for k in env.files
                            if k.startswith(prefix) and os.sep not in k[len(prefix):]})
            if not names:
                raise FileNotFoundError(path)
            return names

        def install_signals(handler):
            env.signal_handler = handler

        def refresh_token(p, profiles, *, network=True):
            env.refresh_calls.append(("refresh_token", p.account_id, network))
            table = env.refresh_results if network else env.gate_results
            return dict(table.get(p.account_id, {"state": "not_needed", "reason": "fresh", "at": None,
                                                 "last_refreshed_at": None}))

        def refresh_held(*, due_only=False):
            env.refresh_calls.append(("refresh_held", due_only))
            if env.held_raises:
                raise RuntimeError("held boom")
            return list(env.held_due if due_only else env.held_ids)

        fields = dict(
            discover_profiles=discover_profiles, fetch_usage=fetch_usage, refresh_token=refresh_token,
            refresh_held=refresh_held, discover_sessions=discover_sessions,
            switch=switch, make_driver=lambda: FakeDriver(env), procs=env.probe, read_pointer=read_pointer,
            write_pointer=write_pointer, now=lambda: env.now_v, monotonic=lambda: env.mono_v, spawn=spawn,
            wait_stop=wait_stop, lock=lock, read_text=read_text, write_text=write_text, list_dir=list_dir,
            isdir=lambda path: env.isdir_ok, pid_alive=lambda pid: pid in env.alive_pids,
            install_signals=install_signals, make_logger=env.logger,
        )
        fields.update(overrides)
        return Deps(**fields)


def scratch(name):
    path = os.path.join(BASE, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


# --- Task 02 ---------------------------------------------------------------------------------------------


class TestReasonClasses(unittest.TestCase):
    def test_partition_is_exhaustive(self):
        sets = [STRUCTURAL_REASONS, TARGET_REASONS, TRANSIENT_REASONS, GONE_REASONS, TOUCHED_ONLY_REASONS]
        for i, a in enumerate(sets):
            for b in sets[i + 1:]:
                self.assertEqual(a & b, frozenset())
        self.assertEqual(frozenset().union(*sets), set(switcher.REASONS))
        self.assertEqual([len(s) for s in sets], [8, 2, 8, 1, 9])

    def test_classify(self):
        self.assertEqual(classify_untouched("target_untrusted"), "target")
        self.assertEqual(classify_untouched("not_idle"), "transient")
        self.assertEqual(classify_untouched("pid_not_alive"), "gone")
        self.assertEqual(classify_untouched("pane_binding_unverified"), "structural")
        for r in TOUCHED_ONLY_REASONS:
            self.assertEqual(classify_untouched(r), "structural", r)
        self.assertEqual(classify_untouched(None), "structural")
        self.assertEqual(classify_untouched("some_future_token"), "structural")


def final_rec(state="untouched", worker_error=None, res=True):
    return {"record_state": "final", "worker_error": worker_error,
            "result": result_dict(result(state=state)) if res else None}


class TestTouched(unittest.TestCase):
    def test_cases(self):
        self.assertTrue(touched({"record_state": "in_progress", "result": None}))
        self.assertFalse(touched(final_rec()))
        self.assertTrue(touched(final_rec(state="interrupted")))
        self.assertTrue(touched(final_rec(worker_error="RuntimeError")))
        self.assertTrue(touched(final_rec(res=False)))
        self.assertTrue(touched({}))


class TestIdentityAndPaths(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(identity_of(sess(101, ACC_A)), (101, SID1, "2026-10-28T12:00:00+00:00"))
        self.assertIsNone(identity_of(sess(101, ACC_A, started_at=None))[2])

    def test_key_of(self):
        k = key_of(SID1)
        self.assertRegex(k, r"^[0-9a-f]{32}$")
        self.assertEqual(k, key_of(SID1))
        self.assertNotEqual(k, key_of(SID2))

    def test_paths(self):
        state = "/state"
        paths = lock_paths(state, sess(101, ACC_A))
        self.assertEqual(len(paths), 2)
        self.assertTrue(os.path.basename(paths[0]).startswith("s-"))
        self.assertTrue(os.path.basename(paths[1]).startswith("h-"))
        for p in paths:
            self.assertEqual(os.path.dirname(p), os.path.join(state, "locks"))
            self.assertNotIn(SID1, p)
            self.assertNotIn("term_00000001", p)
        self.assertEqual(len(lock_paths(state, sess(101, ACC_A, handle=None))), 1)
        rp = record_path(state, SID1, 101)
        self.assertTrue(rp.endswith("-101.json"))
        self.assertEqual(os.path.dirname(rp), os.path.join(state, "sessions"))
        self.assertNotIn(SID1, rp)


class TestDeps(unittest.TestCase):
    def test_no_defaults(self):
        with self.assertRaises(TypeError):
            Deps()
        fields = dataclasses.fields(Deps)
        with self.assertRaises(TypeError):
            Deps(**{f.name: None for f in fields[:-1]})
        self.assertEqual(len(fields), 22)
        for f in fields:
            self.assertIs(f.default, dataclasses.MISSING, f.name)
            self.assertIs(f.default_factory, dataclasses.MISSING, f.name)

    def test_fake_env_builds(self):
        self.assertIsInstance(FakeEnv().deps(), Deps)


# --- Task 03 ---------------------------------------------------------------------------------------------


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.base = scratch("atomic-" + self._testMethodName)

    def test_writes_text_mode_0600_dir_0700(self):
        d = os.path.join(self.base, "new")
        p = os.path.join(d, "state.json")
        atomic_write_text(p, "x")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "x")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_replaces_existing(self):
        p = os.path.join(self.base, "state.json")
        atomic_write_text(p, "first")
        atomic_write_text(p, "second")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "second")

    def test_failure_leaves_no_temp(self):
        target = os.path.join(self.base, "state.json")
        os.makedirs(target)
        with self.assertRaises(OSError):
            atomic_write_text(target, "x")
        self.assertEqual([n for n in os.listdir(self.base) if n.startswith(".")], [])
        self.assertTrue(os.path.isdir(target))

    def test_empty_text(self):
        p = os.path.join(self.base, "empty.json")
        atomic_write_text(p, "")
        self.assertEqual(os.path.getsize(p), 0)


class TestFileLock(unittest.TestCase):
    def setUp(self):
        self.base = scratch("locks-test")
        self.path = os.path.join(self.base, "a.lock")

    def test_lock_cycle(self):
        first = try_lock(self.path)
        self.assertIsInstance(first, FileLock)
        self.assertIsNone(try_lock(self.path))
        first.write_owner(4242)
        with open(self.path) as f:
            self.assertEqual(f.read(), "4242")
        first.write_owner(7)
        with open(self.path) as f:
            self.assertEqual(f.read(), "7")
        first.release()
        first.release()
        self.assertTrue(os.path.exists(self.path))
        third = try_lock(self.path)
        self.assertIsInstance(third, FileLock)
        third.release()


class TestSerializers(unittest.TestCase):
    def test_usage_summary_keys(self):
        s = usage_summary(live(ACC_A, 42.0))
        self.assertEqual(set(s), {"account_id", "source", "error", "fetched_at", "five_hour", "seven_day", "fable"})
        self.assertEqual(s["fetched_at"], "2026-10-28T12:00:00+00:00")
        self.assertEqual(set(s["five_hour"]), {"utilization", "resets_at"})
        self.assertIsNone(s["fable"])

    def test_fable_round_trip(self):
        u = dataclasses.replace(live(ACC_A, 42.0), fable=UsageWindow(25.0, T0 + timedelta(days=5)))
        self.assertEqual(usage_summary(u)["fable"], {"utilization": 25.0, "resets_at": "2026-11-02T12:00:00+00:00"})
        self.assertEqual(usage_from_summary(usage_summary(u)), u)
        v = dataclasses.replace(live(ACC_A, 42.0), fable=UsageWindow(25.0))
        self.assertEqual(usage_from_summary(usage_summary(v)), v)

    def test_old_summary_without_fable(self):
        d = usage_summary(live(ACC_A, 42.0))
        del d["fable"]
        got = usage_from_summary(d)
        self.assertIsNotNone(got)
        self.assertIsNone(got.fable)

    def test_retry_after_not_serialized(self):
        u = dataclasses.replace(limited(ACC_B), retry_after_s=120.0)
        self.assertNotIn("retry_after_s", usage_summary(u))
        self.assertIsNone(usage_from_summary(usage_summary(u)).retry_after_s)

    def test_round_trip(self):
        for u in (live(ACC_A, 42.0),
                  AccountUsage(account_id=ACC_A, source="unavailable", fetched_at=T0,
                               error="token_expired;cache_missing")):
            self.assertEqual(usage_from_summary(usage_summary(u)), u)

    def test_bad_summaries(self):
        self.assertIsNone(usage_from_summary({"account_id": ACC_A}))
        self.assertIsNone(usage_from_summary({"account_id": ACC_A, "source": "live", "error": None,
                                              "fetched_at": "2026-10-28T12:00:00+00:00",
                                              "five_hour": None, "seven_day": None}))
        self.assertIsNone(usage_from_summary({"account_id": ACC_A, "source": "live", "error": None,
                                              "fetched_at": "2026-10-28T12:00:00",
                                              "five_hour": {"utilization": 1.0, "resets_at": None}}))

    def test_result_dict_and_no_secrets(self):
        r = result_dict(result("exited", "exit_timeout", cmd="cd /x && claude"))
        self.assertEqual(set(r), {"ok", "step", "reason", "state", "new_pid", "recoverable", "request_id",
                                  "other_account_id", "relaunch_command"})
        self.assertEqual(r["relaunch_command"], "cd /x && claude")
        text = daemon._dump([usage_summary(live(ACC_A, 42.0)), r])
        for s in SECRETS:
            self.assertNotIn(s, text)
        self.assertIn(ACC_A, text)


def record(pid, sid=SID1, started="2026-10-28T12:00:00+00:00", **kw):
    rec = {"schema": 1, "record_state": "final", "pid": pid, "session_id": sid, "started_at": started,
           "result": result_dict(result()), "worker_error": None}
    rec.update(kw)
    return rec


class TestLoadRecords(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.state = "/state"
        self.sdir = os.path.join(self.state, "sessions")

    def test_no_dir(self):
        self.assertEqual(load_records(self.state, self.env.deps(), self.env.logger()), {})

    def test_valid_and_invalid(self):
        f = self.env.files
        f[os.path.join(self.sdir, "a-101.json")] = json.dumps(record(101))
        f[os.path.join(self.sdir, "a-202.json")] = json.dumps(record(202, started=None))
        f[os.path.join(self.sdir, "b-1.json")] = "{"
        f[os.path.join(self.sdir, "b-2.json")] = "[]"
        f[os.path.join(self.sdir, "b-3.json")] = json.dumps(record(303, schema=2))
        f[os.path.join(self.sdir, ".tmp.json")] = "{"
        f[os.path.join(self.sdir, "note.txt")] = "{"
        out = load_records(self.state, self.env.deps(), self.env.logger())
        self.assertEqual(set(out), {(101, SID1, "2026-10-28T12:00:00+00:00"), (202, SID1, None)})
        errs = self.env.logs("record_load_error")
        self.assertEqual(len(errs), 3)
        self.assertEqual(sorted(e.split("file=")[1].split()[0] for e in errs),
                         ["b-1.json", "b-2.json", "b-3.json"])

    def test_bad_identity_skipped(self):
        self.env.files[os.path.join(self.sdir, "c.json")] = json.dumps(record(0))
        out = load_records(self.state, self.env.deps(), self.env.logger())
        self.assertEqual(out, {})
        self.assertEqual(len(self.env.logs("record_load_error")), 1)


# --- Task 04 ---------------------------------------------------------------------------------------------


class TestSetupLogging(unittest.TestCase):
    def setUp(self):
        self.log_dir = os.path.join(BASE, "logs-" + self._testMethodName)
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def tearDown(self):
        lg = logging.getLogger("rotator")
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        lg.setLevel(logging.NOTSET)

    def _rotating(self, lg):
        return [h for h in lg.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]

    def test_file_handler_bounded(self):
        lg = setup_logging(self.log_dir, False)
        hs = self._rotating(lg)
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0].maxBytes, 1_048_576)
        self.assertEqual(hs[0].backupCount, 3)
        self.assertEqual(lg.level, logging.INFO)
        self.assertEqual(lg.name, "rotator")
        self.assertEqual(stat.S_IMODE(os.stat(self.log_dir).st_mode), 0o700)

    def test_debug_handle_line_absent_info_present(self):
        lg = setup_logging(self.log_dir, False)
        logging.getLogger("rotator.switcher").debug("switch handle=%s", "term_00000001")
        logging.getLogger("rotator").info("event=switch_result pid=101")
        for h in lg.handlers:
            h.flush()
        with open(os.path.join(self.log_dir, "rotator.log"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("level=INFO event=switch_result pid=101", text)
        self.assertNotIn("term_00000001", text)
        self.assertRegex(text.splitlines()[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")

    def test_idempotent(self):
        setup_logging(self.log_dir, False)
        lg = setup_logging(self.log_dir, False)
        self.assertEqual(len(self._rotating(lg)), 1)

    def test_stderr_only_on_tty(self):
        lg = setup_logging(self.log_dir, False)
        self.assertEqual([h for h in lg.handlers if type(h) is logging.StreamHandler], [])
        lg = setup_logging(self.log_dir, True)
        self.assertEqual(len([h for h in lg.handlers if type(h) is logging.StreamHandler]), 1)

    def test_foreign_handler_kept(self):
        foreign = logging.NullHandler()
        logging.getLogger("rotator").addHandler(foreign)
        lg = setup_logging(self.log_dir, False)
        self.assertIn(foreign, lg.handlers)



# --- Task 05 ---------------------------------------------------------------------------------------------


class RunSwitchBase(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.env = FakeEnv()
        self.s = sess(101, ACC_A)
        self.logger = self.env.deps().make_logger()
        self.path = record_path(self.cfg.state_dir, SID1, 101)
        self.locks = lock_paths(self.cfg.state_dir, self.s)

    def run_switch(self, s=None, target=None, mode="soft", deps=None, **kw):
        kw.setdefault("profiles_count", 3)
        return daemon.run_switch(s or self.s, prof(ACC_A), target or prof(ACC_B), mode, cfg=self.cfg,
                                 deps=deps or self.env.deps(), logger=self.logger, source="daemon", **kw)

    def stored(self):
        return json.loads(self.env.files[self.path])

    def switch_calls(self):
        return [c for c in self.env.calls if c[0] == "switch"]

    def assert_released(self):
        self.assertEqual(self.env.held, set())


class TestRunSwitch(RunSwitchBase):
    def test_happy_one_attempt(self):
        self.env.results = [result("continued", None, ok=True, step="continue_prompt", new_pid=303,
                                   cmd="cd /x && claude")]
        self.env.probe.envs[303] = ProcEnv(config_dir=prof(ACC_B).home)
        out = self.run_switch()
        self.assertEqual(out.status, "done")
        s_lock, h_lock = self.locks
        self.assertEqual(self.env.calls, [
            ("lock", s_lock), ("lock", h_lock), ("write", self.path), ("switch", 101, ACC_B, "soft"),
            ("write", self.path), ("write", self.path), ("release", h_lock), ("release", s_lock)])
        rec = self.stored()
        self.assertEqual(rec["record_state"], "final")
        self.assertIs(rec["touched"], True)
        self.assertEqual(rec["post_verify"], "ok")
        self.assertEqual(rec["result"]["state"], "continued")
        self.assertEqual(rec["target_account_id"], ACC_B)
        kw = self.env.switch_kwargs[0]
        self.assertEqual(set(kw), {"driver", "procs", "timeouts", "continue_prompt", "profiles_root"})
        self.assertIs(kw["timeouts"], self.cfg.timeouts)
        self.assertEqual(kw["profiles_root"], ROOT)
        self.assertEqual(self.env.driver_calls, [])
        text = self.env.files[self.path] + "\n".join(self.env.log_records)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn(ACC_B, self.env.files[self.path])
        self.assertEqual(len(self.env.logs("post_verify")), 1)
        self.assertEqual(self.env.logs("relaunch_pending"), [])

    def test_intent_precedes_every_switch(self):
        self.env.results = [result()]
        self.run_switch()
        first_write = self.env.calls.index(("write", self.path))
        first_switch = next(i for i, c in enumerate(self.env.calls) if c[0] == "switch")
        self.assertLess(first_write, first_switch)

    def test_intent_failure_no_switch(self):
        self.env.fail_write = {os.sep + "sessions" + os.sep}
        out = self.run_switch()
        self.assertEqual(out.status, "intent_failed")
        self.assertEqual(self.switch_calls(), [])
        self.assert_released()
        self.assertEqual(len(self.env.logs("intent_write_failed")), 1)

    def test_final_write_failure_keeps_intent(self):
        self.env.results = [result("interrupted", "interrupt_timeout", step="interrupt")]
        base = self.env.deps()
        n = [0]

        def write_text(path, text):
            n[0] += 1
            if n[0] == 2:
                raise OSError("disk full")
            base.write_text(path, text)

        out = self.run_switch(deps=dataclasses.replace(base, write_text=write_text))
        self.assertEqual(out.status, "done")
        stored = self.stored()
        self.assertEqual(stored["record_state"], "in_progress")
        self.assertTrue(touched(stored))
        self.assertEqual(len(self.env.logs("record_write_failed")), 1)

    def test_busy_session_lock(self):
        self.env.held.add(self.locks[0])
        out = self.run_switch()
        self.assertEqual(out.status, "busy")
        self.assertEqual(self.switch_calls(), [])
        self.assertEqual([c for c in self.env.calls if c[0] == "release"], [])

    def test_busy_pane_lock(self):
        other = sess(102, ACC_A, sid=SID2)
        other_locks = lock_paths(self.cfg.state_dir, other)
        self.env.held.add(other_locks[1])
        out = self.run_switch(s=other)
        self.assertEqual(out.status, "busy")
        self.assertEqual(self.switch_calls(), [])
        self.assertIn(("release", other_locks[0]), self.env.calls)
        self.assertEqual(self.env.held, {other_locks[1]})

    def _store(self, rec):
        self.env.files[self.path] = json.dumps(rec)

    def test_refuses_touched_record(self):
        self._store(record(101, result=result_dict(result("interrupted", "interrupt_timeout"))))
        out = self.run_switch()
        self.assertEqual(out.status, "refused_touched")
        self.assertEqual(self.switch_calls(), [])
        self.assert_released()
        self.assertEqual(len(self.env.logs("switch_refused")), 1)

    def test_refuses_in_progress_record(self):
        self._store(record(101, record_state="in_progress", result=None, owner_pid=999999))
        self.assertEqual(self.run_switch().status, "refused_touched")
        self.assertEqual(self.switch_calls(), [])

    def test_refuses_is_touched_callback(self):
        out = self.run_switch(is_touched=lambda i: i == identity_of(self.s))
        self.assertEqual(out.status, "refused_touched")
        self.assertEqual(self.switch_calls(), [])

    def test_unreadable_record_refused(self):
        self.env.files[self.path] = "{"
        self.assertEqual(self.run_switch().status, "refused_touched")
        self.env.files[self.path] = "[]"
        self.assertEqual(self.run_switch().status, "refused_touched")
        self.assertEqual(self.switch_calls(), [])
        self.assert_released()

    def test_other_identity_ignored(self):
        self._store(record(101, started="2026-10-27T08:00:00+00:00",
                           result=result_dict(result("exited", "exit_timeout"))))
        self.env.results = [result()]
        out = self.run_switch()
        self.assertEqual(out.status, "done")
        self.assertEqual(len(self.switch_calls()), 1)

    def test_untouched_result_not_touched(self):
        self.env.results = [result("untouched", "not_idle")]
        out = self.run_switch()
        self.assertIs(out.record["touched"], False)
        self.assertEqual(out.record["hint"], switcher.STATE_HINTS["untouched"])
        self.assertIsNone(out.record["post_verify"])

    def test_relaunch_pending_logged(self):
        cmd = "cd /x && claude --resume y"
        self.env.results = [result("exited", "exit_timeout", step="wait_exit", cmd=cmd)]
        out = self.run_switch()
        lines = self.env.logs("relaunch_pending")
        self.assertEqual(len(lines), 1)
        self.assertIn(cmd, lines[0])
        self.assertEqual(out.record["result"]["relaunch_command"], cmd)
        self.assertEqual(out.record["hint"], switcher.STATE_HINTS["exited"])
        self.assertEqual(self.env.driver_calls, [])

    def test_switch_raises_propagates(self):
        self.env.results = [RuntimeError("boom")]
        with self.assertRaises(RuntimeError):
            self.run_switch()
        self.assertEqual(self.stored()["record_state"], "in_progress")
        self.assert_released()

    def test_on_intent_and_on_attempt_called(self):
        seen = []
        self.env.results = [result()]
        self.run_switch(on_intent=lambda i, t: seen.append(("intent", i, t, len(self.switch_calls()))),
                        on_attempt=lambda: seen.append(("attempt", len(self.switch_calls()))))
        self.assertEqual(seen, [("intent", identity_of(self.s), T0, 0), ("attempt", 0)])


class TestPostVerify(unittest.TestCase):
    def test_outcomes(self):
        probe = FakeProbe()
        target = prof(ACC_B)
        probe.envs[1] = ProcEnv(config_dir=target.home)
        probe.envs[2] = ProcEnv(config_dir=target.home + "/")
        probe.envs[3] = ProcEnv(config_dir=prof(ACC_C).home)
        probe.envs[5] = ProcEnv(config_dir=None)
        probe.envs[6] = OSError()
        self.assertEqual(daemon.post_verify(1, target, probe), "ok")
        self.assertEqual(daemon.post_verify(2, target, probe), "ok")
        self.assertEqual(daemon.post_verify(3, target, probe), "mismatch")
        self.assertEqual(daemon.post_verify(4, target, probe), "unreadable")
        self.assertEqual(daemon.post_verify(5, target, probe), "unreadable")
        self.assertEqual(daemon.post_verify(6, target, probe), "unreadable")


# --- Task 06 ---------------------------------------------------------------------------------------------


class TestRunSwitchOutcomes(RunSwitchBase):
    def writes_between_switches(self):
        counts, n = [], 0
        for c in self.env.calls:
            if c[0] == "write":
                n += 1
            elif c[0] == "switch":
                counts.append(n)
                n = 0
        return counts

    def test_target_untrusted_then_next_then_stop(self):
        self.env.results = [result("untouched", "target_untrusted"), result("untouched", "target_untrusted")]
        received = []

        def next_target(excl):
            received.append(excl)
            return ("soft", prof(ACC_C)) if excl == frozenset({ACC_B}) else None

        out = self.run_switch(next_target=next_target)
        self.assertEqual([c[2] for c in self.switch_calls()], [ACC_B, ACC_C])
        self.assertEqual(self.writes_between_switches(), [1, 1])
        self.assertEqual(received, [frozenset({ACC_B}), frozenset({ACC_B, ACC_C})])
        self.assertEqual(out.record["excluded"], [ACC_B, ACC_C])
        self.assertIs(out.record["touched"], False)
        self.assertEqual(out.record["attempt"], 2)
        self.assertEqual(out.record["target_account_id"], ACC_C)

    def test_attempts_bounded_by_profile_count(self):
        self.env.results = [result("untouched", "target_home_missing") for _ in range(5)]
        fresh = iter([prof(ACC_C), prof("00000000-0000-0000-0000-00000000000d"),
                      prof("00000000-0000-0000-0000-00000000000e")])
        self.run_switch(profiles_count=2, next_target=lambda excl: ("soft", next(fresh)))
        self.assertEqual(len(self.switch_calls()), 2)

    def test_cli_style_no_next_target(self):
        self.env.results = [result("untouched", "target_untrusted")]
        out = self.run_switch(next_target=None)
        self.assertEqual(len(self.switch_calls()), 1)
        self.assertEqual(out.record["excluded"], [ACC_B])

    def test_second_intent_failure_stops(self):
        self.env.results = [result("untouched", "target_untrusted"), result()]
        base = self.env.deps()
        n = [0]

        def write_text(path, text):
            n[0] += 1
            if n[0] == 2:
                raise OSError("disk full")
            base.write_text(path, text)

        out = self.run_switch(deps=dataclasses.replace(base, write_text=write_text),
                              next_target=lambda excl: ("soft", prof(ACC_C)))
        self.assertEqual(len(self.switch_calls()), 1)
        self.assertEqual(out.status, "done")
        self.assertEqual(out.record["result"]["reason"], "target_untrusted")
        self.assertEqual(out.record["excluded"], [ACC_B])
        self.assertEqual(out.record["target_account_id"], ACC_B)
        self.assertEqual(self.stored()["record_state"], "final")

    def test_structural_unswitchable(self):
        for reason, expected in (("pane_binding_unverified", "pane_binding_unverified"),
                                 ("some_future_token", "some_future_token"), (None, "unknown")):
            with self.subTest(reason=reason):
                self.env = FakeEnv()
                self.logger = self.env.deps().make_logger()
                self.env.results = [result("untouched", reason)]
                out = self.run_switch()
                self.assertEqual(out.record["unswitchable"], expected)
                self.assertIs(out.record["touched"], False)

    def test_hard_transient_sets_hold(self):
        for reason, mode, expected in (("input_not_empty", "hard", "2026-10-28T12:05:00+00:00"),
                                       ("pid_not_alive", "hard", "2026-10-28T12:05:00+00:00"),
                                       ("input_not_empty", "soft", None)):
            with self.subTest(reason=reason, mode=mode):
                self.env = FakeEnv()
                self.logger = self.env.deps().make_logger()
                self.env.results = [result("untouched", reason)]
                out = self.run_switch(mode=mode)
                self.assertEqual(out.record["hard_hold_until"], expected)
                self.assertIsNone(out.record["unswitchable"])

    def test_hold_carried_over_on_soft(self):
        hold = "2026-10-28T11:59:00+00:00"
        self.env.files[self.path] = json.dumps(record(101, hard_hold_until=hold, excluded=[ACC_C]))
        self.env.results = [result("untouched", "not_idle")]
        out = self.run_switch()
        self.assertEqual(out.record["hard_hold_until"], hold)
        self.assertEqual(out.record["excluded"], [ACC_C])

    def test_touched_result_no_effects(self):
        self.env.results = [result("interrupted", "input_not_empty", step="exit")]
        out = self.run_switch(mode="hard")
        self.assertIsNone(out.record["unswitchable"])
        self.assertIsNone(out.record["hard_hold_until"])
        self.assertIs(out.record["touched"], True)



# --- Task 07 ---------------------------------------------------------------------------------------------


def limited(aid, source="cache"):
    if source == "cache":
        return AccountUsage(account_id=aid, source="cache", fetched_at=T0 - timedelta(minutes=10),
                            five_hour=UsageWindow(50.0, T0 + timedelta(hours=1)),
                            seven_day=UsageWindow(10.0, T0 + timedelta(days=2)), error="http_429")
    return AccountUsage(account_id=aid, source="unavailable", fetched_at=T0, error="http_429;cache_missing")


def limited_ra(aid, ra, source="cache"):
    return dataclasses.replace(limited(aid, source), retry_after_s=ra)


class TestUsageCache(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.deps = self.env.deps()
        self.log = self.deps.make_logger()
        self.c = UsageCache(30.0)
        self.env.usages[ACC_A] = live(ACC_A, 20.0)
        self.profiles = [prof(ACC_A), prof(ACC_B)]

    def step(self, cache=None):
        out = (cache or self.c).refresh(self.profiles, fetch=self.deps.fetch_usage, monotonic=self.deps.monotonic,
                                        now=self.env.now_v, log=self.log)
        self.env.advance(10)
        return out

    def run_until(self, last_t):
        fetched = {}
        for t in range(0, last_t + 1, 10):
            fetched[t] = self.step()
        return fetched

    def test_rate_limited_predicate(self):
        self.assertTrue(rate_limited(limited(ACC_B)))
        self.assertTrue(rate_limited(limited(ACC_B, "unavailable")))
        self.assertFalse(rate_limited(live(ACC_A, 1.0)))
        self.assertFalse(rate_limited(dataclasses.replace(limited(ACC_B), error="http_500")))
        for err in ("token_expired;cache_missing", "request_error;cache_missing"):
            self.assertFalse(rate_limited(AccountUsage(account_id=ACC_A, source="unavailable", fetched_at=T0,
                                                       error=err)))

    def test_backoff_doubles_to_cap(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        intervals = []
        b_times, a_count = [], 0
        for t in range(0, 601, 10):
            got = self.step()
            if ACC_B in got:
                b_times.append(t)
                intervals.append(self.c.interval(ACC_B))
            a_count += got.count(ACC_A)
        self.assertEqual(b_times, [0, 60, 180, 420])
        self.assertEqual(intervals, [60.0, 120.0, 240.0, 300.0])
        self.assertEqual(a_count, 21)
        self.assertEqual(len([r for r in self.env.log_records
                              if r.startswith("event=usage_rate_limited account=%s" % ACC_B)]), 4)
        u = self.c.usages()[ACC_B]
        self.assertIs(u, self.env.usages[ACC_B])
        self.assertEqual((u.source, u.error), ("cache", "http_429"))

    def test_backoff_resets_on_success(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        self.step()
        self.assertEqual(self.c.interval(ACC_B), 60.0)
        self.env.usages[ACC_B] = live(ACC_B, 20.0)
        b_times = []
        for t in range(10, 101, 10):
            if ACC_B in self.step():
                b_times.append(t)
                if t == 60:
                    self.assertEqual(self.c.interval(ACC_B), 30.0)
        self.assertEqual(b_times, [60, 90])

    def test_unavailable_429_backs_off(self):
        self.env.usages[ACC_B] = limited(ACC_B, "unavailable")
        self.step()
        self.assertEqual(self.c.interval(ACC_B), 60.0)
        self.c = UsageCache(30.0)
        self.env.usages[ACC_B] = dataclasses.replace(limited(ACC_B), error="http_500")
        self.step()
        self.assertEqual(self.c.interval(ACC_B), 30.0)
        self.c = UsageCache(30.0)
        self.env.fetch_raises = {ACC_B}
        self.step()
        self.assertEqual(self.c.interval(ACC_B), 30.0)
        self.assertEqual(self.c.usages()[ACC_B].error, "request_error;cache_missing")

    def test_poll_above_cap_never_shortened(self):
        self.env.usages[ACC_B] = limited(ACC_B)
        c = UsageCache(400.0)
        self.step(c)
        self.assertEqual(c.interval(ACC_B), 400.0)

    def test_seed_uses_poll_interval(self):
        self.env.usages[ACC_B] = live(ACC_B, 20.0)
        self.c.seed(limited(ACC_B), self.env.mono_v - 5)
        self.assertEqual(self.c.interval(ACC_B), 30.0)
        self.assertIsNone(self.c.interval(ACC_C))
        fetched = self.run_until(30)
        self.assertEqual([t for t, ids in fetched.items() if ACC_B in ids], [30])
        self.assertEqual([t for t, ids in fetched.items() if ACC_A in ids], [0, 30])

    def test_retry_after_sets_interval(self):
        for ra, expected in [(120.0, 120.0), (5.0, 30.0), (3600.0, 900.0), (0.0, 30.0), (900.0, 900.0)]:
            with self.subTest(ra=ra):
                cache = UsageCache(30.0)
                self.env.usages[ACC_B] = limited_ra(ACC_B, ra)
                self.step(cache)
                self.assertEqual(cache.interval(ACC_B), expected)
                recs = [r for r in self.env.log_records if r.startswith("event=usage_rate_limited account=%s" % ACC_B)]
                self.assertTrue(recs[-1].endswith("next_s=%d" % int(expected)), recs[-1])

    def test_retry_after_unavailable(self):
        self.env.usages[ACC_B] = limited_ra(ACC_B, 120.0, "unavailable")
        self.step()
        self.assertEqual(self.c.interval(ACC_B), 120.0)

    def test_retry_after_then_success(self):
        self.env.usages[ACC_B] = limited_ra(ACC_B, 120.0)
        self.step()
        self.assertEqual(self.c.interval(ACC_B), 120.0)
        self.env.usages[ACC_B] = live(ACC_B, 20.0)
        b_times = []
        for t in range(10, 131, 10):
            if ACC_B in self.step():
                b_times.append(t)
        self.assertEqual(b_times, [120])
        self.assertEqual(self.c.interval(ACC_B), 30.0)


def rrec(reason, state="skipped"):
    return {"state": state, "reason": reason, "at": None, "last_refreshed_at": None}


class TestUsageCacheRefresh(unittest.TestCase):
    """UsageCache's side of the two refresh phases (gate before fetch; one network refresh per call)."""

    def setUp(self):
        self.env = FakeEnv()
        self.deps = self.env.deps()
        self.log = self.deps.make_logger()
        self.c = UsageCache(30.0)
        self.env.usages[ACC_A] = live(ACC_A, 20.0)
        self.env.usages[ACC_B] = live(ACC_B, 30.0)
        self.profiles = [prof(ACC_A), prof(ACC_B)]
        self.held_all: List[str] = []
        self.held_due: List[str] = []
        self.calls: List[str] = []
        self.results: Dict[str, dict] = {}

    def pre_step(self, pre):
        out = self.c.refresh(self.profiles, fetch=self.deps.fetch_usage, monotonic=self.deps.monotonic,
                             now=self.env.now_v, log=self.log, pre=pre)
        self.env.advance(10)
        return out

    def spy(self, ret=None):
        def pre(p, profiles):
            self.env.calls.append(("pre", p.account_id, [q.account_id for q in profiles]))
            return dict(ret or rrec("deferred"))
        return pre

    def held(self, *, due_only=False):
        return list(self.held_due if due_only else self.held_all)

    def call(self, p, profiles):
        self.calls.append(p.account_id)
        return dict(self.results.get(p.account_id, rrec("deferred")))

    def one(self, skip=frozenset()):
        return self.c.refresh_one(self.profiles, call=self.call, held=self.held, skip=skip)

    def test_pre_runs_right_before_each_fetch(self):
        self.pre_step(self.spy())
        both = [ACC_A, ACC_B]
        self.assertEqual(self.env.calls, [("pre", ACC_A, both), ("fetch", ACC_A), ("pre", ACC_B, both),
                                          ("fetch", ACC_B)])
        self.env.calls.clear()
        self.pre_step(self.spy())
        self.assertEqual(self.env.calls, [])

    def test_record_stored_as_copy(self):
        ret = rrec("deferred")
        self.pre_step(self.spy(ret))
        got = self.c.refresh_record(ACC_A)
        self.assertEqual(got, ret)
        got["reason"] = "mutated"
        self.assertEqual(self.c.refresh_record(ACC_A)["reason"], "deferred")

    def test_pre_raising_is_internal_error_and_fetch_runs(self):
        def pre(p, profiles):
            raise RuntimeError("x")
        self.pre_step(pre)
        self.assertEqual(self.c.refresh_record(ACC_A),
                         {"state": "failed", "reason": "internal_error", "at": T0.isoformat(),
                          "last_refreshed_at": None})
        self.assertIn(("fetch", ACC_A), self.env.calls)
        self.assertIn("event=token_refresh_error account=%s exc=RuntimeError" % ACC_A, self.env.log_records)

    def test_no_pre_keeps_old_behaviour(self):
        out = self.c.refresh(self.profiles, fetch=self.deps.fetch_usage, monotonic=self.deps.monotonic,
                             now=self.env.now_v, log=self.log)
        self.assertEqual(out, [ACC_A, ACC_B])
        self.assertIsNone(self.c.refresh_record(ACC_A))
        self.assertEqual([s["refresh"] for s in self.c.summaries()], [None, None])

    def test_pruned_with_profiles(self):
        self.pre_step(self.spy())
        self.assertEqual([s["refresh"] for s in self.c.summaries()], [rrec("deferred")] * 2)
        self.profiles = [prof(ACC_A)]
        self.pre_step(self.spy())
        self.assertIsNone(self.c.refresh_record(ACC_B))
        self.assertEqual([s["account_id"] for s in self.c.summaries()], [ACC_A])

    def test_held_id_without_record_is_picked(self):
        self.held_all = self.held_due = [ACC_B]
        self.assertEqual(self.one(), ACC_B)
        self.assertEqual(self.calls, [ACC_B])
        # also after its record was pruned and the account came back
        self.pre_step(self.spy())
        self.profiles = [prof(ACC_A)]
        self.pre_step(self.spy())
        self.profiles = [prof(ACC_A), prof(ACC_B)]
        self.assertIsNone(self.c.refresh_record(ACC_B))
        self.calls.clear()
        self.results[ACC_A] = rrec("fresh", "not_needed")
        self.c._flush_next = True
        self.assertEqual(self.one(), ACC_B)

    def test_alternation(self):
        self.profiles = [prof(ACC_A), prof(ACC_B), prof(ACC_C)]
        self.env.usages[ACC_C] = live(ACC_C, 10.0)
        self.pre_step(self.spy())
        self.held_all = self.held_due = [ACC_A]
        self.results[ACC_B] = rrec("live_session")  # B leaves the waiting list once tried
        picks = [self.one() for _ in range(4)]
        self.assertEqual(picks, [ACC_A, ACC_B, ACC_A, ACC_C])
        self.assertEqual(self.calls, picks)

    def test_held_not_due_waits(self):
        self.pre_step(self.spy())
        self.held_all = [ACC_A]
        self.held_due = []
        self.assertEqual(self.one(), ACC_B)
        self.assertEqual(self.one(), ACC_B)
        self.assertNotIn(ACC_A, self.calls)

    def test_skip_and_none(self):
        self.pre_step(self.spy())
        self.assertIsNone(self.one(skip=frozenset({ACC_A, ACC_B})))
        self.assertEqual(self.calls, [])
        self.held_all = self.held_due = [ACC_A]
        self.assertEqual(self.one(skip=frozenset({ACC_A})), ACC_B)
        self.assertEqual(self.calls, [ACC_B])

    def test_no_candidate(self):
        self.pre_step(self.spy(rrec("fresh", "not_needed")))
        self.assertIsNone(self.one())
        self.assertEqual(self.calls, [])

    def test_call_raising(self):
        self.pre_step(self.spy())

        def boom(p, profiles):
            raise RuntimeError("x")
        with self.assertLogs("rotator.daemon", "ERROR") as cm:
            self.assertEqual(self.c.refresh_one(self.profiles, call=boom, held=self.held), ACC_A)
        self.assertEqual(self.c.refresh_record(ACC_A)["reason"], "internal_error")
        self.assertEqual(cm.output, ["ERROR:rotator.daemon:event=token_refresh_error account=%s exc=RuntimeError"
                                     % ACC_A])

    def test_refreshed_marks_due(self):
        self.pre_step(self.spy())
        self.results[ACC_A] = rrec("ok", "refreshed")
        self.assertEqual(self.one(), ACC_A)
        self.env.calls.clear()
        self.pre_step(self.spy())
        self.assertIn(("fetch", ACC_A), self.env.calls)
        self.assertNotIn(("fetch", ACC_B), self.env.calls)

    def test_rate_limited_not_marked_due(self):
        self.env.usages[ACC_A] = limited(ACC_A)
        self.pre_step(self.spy())
        self.c.mark_due(ACC_A)
        self.env.calls.clear()
        self.pre_step(self.spy())
        self.assertNotIn(("fetch", ACC_A), self.env.calls)


class SharedBase(unittest.TestCase):
    """Two processes (two UsageCache over one state dir) on one fake clock."""

    def setUp(self):
        self.env = FakeEnv()
        self.deps = self.env.deps()
        self.log = self.deps.make_logger()
        self.cfg = make_cfg()
        self.store = UsageStore(self.cfg.state_dir, self.deps)
        self.a = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))
        self.b = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))
        self.profiles = [prof(ACC_A), prof(ACC_B)]
        self.env.pcts = {ACC_A: 20.0, ACC_B: 40.0}

    def step(self, cache, profiles=None):
        return cache.refresh(profiles or self.profiles, fetch=self.deps.fetch_usage, monotonic=self.deps.monotonic,
                             now=self.env.now_v, log=self.log)

    def fetches(self):
        return [c[1] for c in self.env.calls if c[0] == "fetch"]

    def usage_files(self):
        return {p: t for p, t in self.env.files.items() if os.sep + "usage" + os.sep in p}


class TestSharedUsage(SharedBase):
    def test_one_fetch_per_account_per_interval_across_processes(self):
        per_tick = []
        for t in range(0, 121, 5):  # both processes poll every 5 s for 2 minutes
            before = len(self.fetches())
            got = self.step(self.a) + self.step(self.b)
            per_tick.append((t, got))
            self.assertEqual(len(self.fetches()) - before, len(got))
            self.env.advance(5)
        fetch_times = [t for t, got in per_tick for _ in got]
        self.assertEqual(fetch_times, [0, 0, 30, 30, 60, 60, 90, 90, 120, 120])
        self.assertEqual(sorted(self.fetches()), [ACC_A] * 5 + [ACC_B] * 5)
        self.assertEqual({aid: u.five_hour.utilization for aid, u in self.b.usages().items()},
                         {ACC_A: 20.0, ACC_B: 40.0})  # b shows a's readings without fetching them

    def test_429_keeps_last_good_and_waits_for_backoff(self):
        self.step(self.a)  # live 20 % for A
        self.env.advance(30)
        self.env.pcts.pop(ACC_A)
        self.env.usages[ACC_A] = limited(ACC_A, "unavailable")
        self.assertEqual(self.step(self.a), [ACC_A, ACC_B])
        for cache in (self.a, self.b):
            if cache is self.b:
                self.step(self.b)
            shown = cache.usages()[ACC_A]
            self.assertEqual((shown.source, shown.error, shown.five_hour.utilization), ("cache", "http_429", 20.0))
            self.assertEqual(shown.fetched_at, T0)  # the age of the last good reading
            d = cache.summary(ACC_A)
            self.assertEqual((d["source"], d["error"], d["five_hour"]["utilization"]), ("live", "http_429", 20.0))
        # 429 without Retry-After: next allowed fetch after 2 x 30 s, for either process
        n = self.fetches().count(ACC_A)
        for _ in range(11):
            self.env.advance(5)
            self.step(self.a)
            self.step(self.b)
        self.assertEqual(self.fetches().count(ACC_A), n)  # 55 s: still backing off
        self.env.advance(5)
        self.step(self.b)
        self.step(self.a)
        self.assertEqual(self.fetches().count(ACC_A), n + 1)

    def test_backoff_doubles_from_the_stored_interval_in_another_process(self):
        self.env.pcts.pop(ACC_A)
        self.env.usages[ACC_A] = limited(ACC_A, "unavailable")
        self.step(self.a, [prof(ACC_A)])  # 429: next fetch in 60 s
        self.assertEqual(self.store.load(ACC_A, self.env.now_v).interval_s, 60.0)
        self.env.advance(60)
        fresh = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))  # no in-memory entry
        self.assertEqual(self.step(fresh, [prof(ACC_A)]), [ACC_A])  # 429 again
        self.assertEqual(self.store.load(ACC_A, self.env.now_v).interval_s, 120.0)
        self.assertEqual(fresh.interval(ACC_A), 120.0)

    def test_entry_stored_while_taking_the_lock_is_used(self):
        self.step(self.a)
        self.env.advance(30)
        other = live(ACC_A, 77.0, now=self.env.now_v)
        base_lock = self.deps.lock

        def lock(path):  # another process stores a fresh reading just before we get the lock
            got = base_lock(path)
            if got is not None and path.endswith("u-%s.lock" % key_of(ACC_A)):
                self.store.save(ACC_A, SharedUsage(other, other, self.env.now_v + timedelta(seconds=30), 30.0))
            return got
        c = UsageCache(30.0, UsageStore(self.cfg.state_dir, dataclasses.replace(self.deps, lock=lock)))
        self.assertEqual(self.step(c), [ACC_B])
        self.assertEqual(c.usages()[ACC_A].five_hour.utilization, 77.0)

    def test_retry_after_shared(self):
        self.env.usages[ACC_A] = limited_ra(ACC_A, 300.0, "unavailable")
        self.env.pcts.pop(ACC_A)
        self.step(self.a)
        self.env.advance(299)
        self.step(self.b)
        self.assertEqual(self.fetches().count(ACC_A), 1)
        self.env.advance(1)
        self.step(self.b)
        self.assertEqual(self.fetches().count(ACC_A), 2)

    def test_lock_busy_uses_stored_entry_without_fetching(self):
        self.step(self.a)
        self.env.advance(30)
        lock = self.store.lock(ACC_A)  # another process is fetching A right now
        self.assertIsNotNone(lock)
        self.assertEqual(self.step(self.b), [ACC_B])
        self.assertEqual(self.b.usages()[ACC_A].five_hour.utilization, 20.0)
        lock.release()
        self.assertEqual(self.step(self.b), [ACC_A])  # looked at again on the next call

    def test_lock_busy_and_nothing_stored_shows_nothing_yet(self):
        lock = self.store.lock(ACC_A)
        self.assertEqual(self.step(self.b), [ACC_B])
        self.assertNotIn(ACC_A, self.b.usages())
        lock.release()

    def test_lock_os_error_still_fetches(self):
        deps = dataclasses.replace(self.deps, lock=lambda path: (_ for _ in ()).throw(OSError("no locks")))
        c = UsageCache(30.0, UsageStore(self.cfg.state_dir, deps))
        self.assertEqual(self.step(c), [ACC_A, ACC_B])
        self.assertEqual(len(self.env.logs("usage_store_error")), 2)

    def test_corrupt_or_missing_files_are_empty(self):
        self.step(self.a)
        bad = ["{", "[]", "null", '"x"', json.dumps({"account_id": ACC_A}), "[" * 100000,
               json.dumps({"account_id": ACC_B, "latest": usage_summary(live(ACC_B, 1.0)), "last_good": None,
                           "next_fetch_at": (T0 + timedelta(hours=1)).isoformat(), "interval_s": 30.0}),
               json.dumps({"account_id": ACC_A, "latest": usage_summary(live(ACC_A, 1.0)), "last_good": None,
                           "next_fetch_at": "soon", "interval_s": 30.0}),
               json.dumps({"account_id": ACC_A, "latest": usage_summary(live(ACC_A, 1.0)), "last_good": None,
                           "next_fetch_at": (T0 + timedelta(hours=1)).isoformat(), "interval_s": float("nan")}),
               json.dumps({"account_id": ACC_A, "latest": usage_summary(live(ACC_A, 1.0)),
                           "last_good": usage_summary(limited(ACC_A)),
                           "next_fetch_at": (T0 + timedelta(hours=1)).isoformat(), "interval_s": 30.0})]
        path = self.store.path(ACC_A)
        for text in bad:
            with self.subTest(text=text[:40]):
                self.env.files[path] = text
                self.assertIsNone(self.store.load(ACC_A, self.env.now_v))
                c = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))
                self.assertIn(ACC_A, self.step(c, [prof(ACC_A)]))  # treated as empty: fetched
        del self.env.files[path]
        self.assertIsNone(self.store.load(ACC_A, self.env.now_v))

    def test_far_future_next_fetch_never_blocks(self):
        self.step(self.a)
        e = self.store.load(ACC_A, self.env.now_v)
        self.store.save(ACC_A, SharedUsage(e.latest, e.last_good, T0 + timedelta(days=365), 30.0))
        self.env.advance(30)
        self.assertIn(ACC_A, self.step(self.b))

    def test_file_holds_numbers_times_and_tokens_only(self):
        self.env.pcts.pop(ACC_B)
        self.env.usages[ACC_B] = limited(ACC_B)
        self.step(self.a)
        files = self.usage_files()
        self.assertEqual(len(files), 2)
        for path, text in files.items():
            for secret in SECRETS + ("sk-ant", "accessToken", "refreshToken", "@"):
                self.assertNotIn(secret, text)
            d = json.loads(text)
            self.assertEqual(set(d), {"account_id", "latest", "last_good", "next_fetch_at", "interval_s"})
            self.assertEqual(set(d["latest"]), set(usage_summary(live(ACC_A, 1.0))))
            self.assertTrue(os.path.basename(path).startswith("u-") and ACC_A not in path and ACC_B not in path)
        self.assertIn('"utilization": 20.0', "".join(files.values()))  # negative control: readings are there

    def test_write_error_is_logged_and_reading_kept(self):
        self.env.fail_write.add(os.sep + "usage" + os.sep)
        self.assertEqual(self.step(self.a), [ACC_A, ACC_B])
        self.assertEqual(self.a.usages()[ACC_A].five_hour.utilization, 20.0)
        self.assertEqual(len(self.env.logs("usage_store_error")), 2)

    def test_written_through_the_atomic_0600_writer(self):
        self.assertIs(real_deps(self.cfg).write_text, atomic_write_text)
        self.step(self.a)
        writes = [c[1] for c in self.env.calls if c[0] == "write"]
        self.assertEqual(sorted(writes), sorted(self.store.path(a) for a in (ACC_A, ACC_B)))

    def test_mark_due_forces_a_fetch_past_a_fresh_entry(self):
        self.step(self.a)
        self.step(self.b)
        self.env.advance(5)
        self.b.mark_due(ACC_A)  # after a token refresh
        self.assertEqual(self.step(self.b), [ACC_A])
        self.assertEqual(self.step(self.a), [])  # a's own interval has not elapsed
        self.env.advance(26)
        self.assertEqual(self.step(self.a), [ACC_B])  # A: b's forced reading (stored at 5 s) is still fresh
        self.assertEqual(self.fetches().count(ACC_A), 2)

    def test_mark_due_never_overrides_a_shared_backoff(self):
        self.env.pcts.pop(ACC_A)
        self.env.usages[ACC_A] = limited(ACC_A)
        self.step(self.a)
        self.b.seed(live(ACC_A, 5.0), self.env.mono_v)
        self.b.mark_due(ACC_A)
        self.env.advance(5)
        self.assertEqual(self.step(self.b), [])  # A: the shared back-off wins over mark_due; B: stored and fresh
        self.assertEqual(self.fetches().count(ACC_A), 1)
        self.assertTrue(rate_limited(self.b.usages()[ACC_A]))

    def test_fresh_process_carries_the_stored_last_good_forward(self):
        self.step(self.a, [prof(ACC_A)])  # a stores a live 20 % reading
        self.env.advance(30)
        self.env.pcts.pop(ACC_A)
        self.env.usages[ACC_A] = limited(ACC_A, "unavailable")
        fresh = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))  # a one-shot status: nothing in memory
        self.assertEqual(self.step(fresh, [prof(ACC_A)]), [ACC_A])  # its fetch gets 429
        shown = fresh.usages()[ACC_A]
        self.assertEqual((shown.error, shown.five_hour.utilization, shown.fetched_at), ("http_429", 20.0, T0))
        stored = self.store.load(ACC_A, self.env.now_v)
        self.assertEqual((stored.last_good.five_hour.utilization, stored.last_good.fetched_at), (20.0, T0))
        self.env.advance(5)
        self.assertEqual(self.step(self.a, [prof(ACC_A)]), [])  # a adopts it: still the 20 % reading
        self.assertEqual(self.a.usages()[ACC_A].five_hour.utilization, 20.0)

    def test_newer_in_memory_last_good_beats_an_older_stored_one(self):
        self.step(self.a, [prof(ACC_A)])  # stored last good: 20 % at T0
        self.env.advance(30)
        c = UsageCache(30.0, UsageStore(self.cfg.state_dir, self.deps))
        c.seed(live(ACC_A, 55.0, now=self.env.now_v), self.env.mono_v - 30)  # this process saw a newer one
        self.env.pcts.pop(ACC_A)
        self.env.usages[ACC_A] = limited(ACC_A, "unavailable")
        self.assertEqual(self.step(c, [prof(ACC_A)]), [ACC_A])
        self.assertEqual(c.usages()[ACC_A].five_hour.utilization, 55.0)
        self.assertEqual(self.store.load(ACC_A, self.env.now_v).last_good.five_hour.utilization, 55.0)

    def test_seeded_reading_is_kept_after_a_failed_fetch(self):
        c = UsageCache(30.0)  # no store: the daemon's snapshot seed alone must keep the numbers
        c.seed(live(ACC_A, 42.0), self.env.mono_v)
        self.env.advance(30)
        self.env.pcts.pop(ACC_A)
        self.env.usages[ACC_A] = AccountUsage(account_id=ACC_A, source="unavailable", fetched_at=self.env.now_v,
                                              error="network;cache_missing")
        self.assertEqual(self.step(c, [prof(ACC_A)]), [ACC_A])
        shown = c.usages()[ACC_A]
        self.assertEqual((shown.source, shown.error, shown.five_hour.utilization), ("cache", "network", 42.0))
        self.assertEqual(c.summary(ACC_A)["source"], "live")

    def test_never_shared_cache_unchanged(self):
        c = UsageCache(30.0)
        self.step(c)
        self.assertEqual(self.usage_files(), {})
        self.assertEqual([x for x in self.env.calls if x[0] in ("lock", "write")], [])


class TestShownUsage(unittest.TestCase):
    def test_choices(self):
        good = live(ACC_A, 30.0, now=T0 - timedelta(minutes=5))
        newer_cache = dataclasses.replace(limited(ACC_A), fetched_at=T0 - timedelta(minutes=1))
        older_cache = dataclasses.replace(limited(ACC_A), fetched_at=T0 - timedelta(minutes=9))
        unavailable = AccountUsage(account_id=ACC_A, source="unavailable", fetched_at=T0, error="network;cache_missing")
        fresh = live(ACC_A, 50.0)
        self.assertEqual(shown_usage(fresh, good), (fresh, False))
        self.assertEqual(shown_usage(newer_cache, good), (newer_cache, False))
        self.assertEqual(shown_usage(unavailable, None), (unavailable, False))
        u, kept = shown_usage(older_cache, good)
        self.assertTrue(kept)
        self.assertEqual((u.source, u.error, u.fetched_at, u.five_hour), ("cache", "http_429", good.fetched_at,
                                                                           good.five_hour))
        u, kept = shown_usage(unavailable, good)
        self.assertTrue(kept)
        self.assertEqual((u.error, u.five_hour.utilization), ("network", 30.0))
        same_time = dataclasses.replace(limited(ACC_A), fetched_at=good.fetched_at)
        self.assertTrue(shown_usage(same_time, good)[1])  # only a strictly newer cache wins


class TickBase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.env.profiles = [prof(ACC_A), prof(ACC_B), prof(ACC_C)]
        self.env.usages = {a: live(a, 20.0) for a in (ACC_A, ACC_B, ACC_C)}
        self.make(make_cfg(usage_poll_s=60.0))

    def make(self, cfg):
        self.cfg = cfg
        self.deps = self.env.deps()
        self.d = Daemon(cfg, self.deps, self.deps.make_logger())
        return self.d

    @property
    def state_path(self):
        return os.path.join(self.cfg.state_dir, "state.json")

    def snap(self):
        return json.loads(self.env.files[self.state_path])

    def fetches(self):
        return [c for c in self.env.calls if c[0] == "fetch"]

    def fetch_counts(self, ticks):
        counts = []
        for _ in range(ticks):
            before = len(self.fetches())
            self.d.tick()
            counts.append(len(self.fetches()) - before)
            self.env.advance(10)
        return counts

    def account(self, aid):
        return next(a for a in self.snap()["accounts"] if a["account_id"] == aid)


class TestTickUsage(TickBase):
    def test_cadence_3_0_3(self):
        self.assertEqual(self.fetch_counts(7), [3, 0, 0, 0, 0, 0, 3])

    def test_default_cadence_30s(self):
        self.make(make_cfg())
        self.assertEqual(self.d._cfg.usage_poll_s, 30.0)
        self.assertEqual(self.fetch_counts(7), [3, 0, 0, 3, 0, 0, 3])

    def test_rate_limited_account_backs_off_in_tick(self):
        self.make(make_cfg())
        self.env.usages[ACC_B] = limited(ACC_B)
        times = {ACC_A: [], ACC_B: [], ACC_C: []}
        for t in range(0, 61, 10):
            before = len(self.fetches())
            self.d.tick()
            for c in self.fetches()[before:]:
                times[c[1]].append(t)
            self.env.advance(10)
        self.assertEqual(times[ACC_A], [0, 30, 60])
        self.assertEqual(times[ACC_C], [0, 30, 60])
        self.assertEqual(times[ACC_B], [0, 60])
        b = self.account(ACC_B)
        self.assertEqual((b["source"], b["error"]), ("cache", "http_429"))
        self.assertEqual(b["fetched_at"], daemon._iso(T0 - timedelta(minutes=10)))

    def test_raising_fetch_contained(self):
        self.env.fetch_raises = {ACC_B}
        self.assertEqual(self.fetch_counts(2), [3, 0])
        b = self.account(ACC_B)
        self.assertEqual((b["source"], b["error"]), ("unavailable", "request_error;cache_missing"))
        self.assertEqual(len([r for r in self.env.log_records
                              if r.startswith("event=fetch_error account=%s" % ACC_B)]), 1)

    def test_vanished_account_dropped(self):
        self.d.tick()
        self.assertEqual(len(self.snap()["accounts"]), 3)
        self.env.profiles = self.env.profiles[:2]
        self.d.tick()
        self.assertEqual([a["account_id"] for a in self.snap()["accounts"]], [ACC_A, ACC_B])

    def _seed_snapshot(self, age_s):
        when = self.env.now_v - timedelta(seconds=age_s)
        doc = {"schema": 1, "accounts": [usage_summary(live(a, 20.0, now=when)) for a in (ACC_A, ACC_B, ACC_C)]}
        self.env.files[self.state_path] = json.dumps(doc)

    def test_seed_from_snapshot(self):
        self._seed_snapshot(20)
        self.d._load_state()
        self.assertEqual(len(self.d.snapshot()["accounts"]), 3)
        self.assertEqual(self.fetch_counts(1), [0])
        self.env.advance(30)
        self.assertEqual(self.fetch_counts(1), [3])

    def test_seed_rejects_old_future_and_garbage(self):
        for age in (61, 60, -5):
            with self.subTest(age=age):
                self.env.calls.clear()
                # each case starts without the shared usage files the previous case's fetches stored
                self.env.files = {k: v for k, v in self.env.files.items() if os.sep + "usage" + os.sep not in k}
                self.make(self.cfg)
                self._seed_snapshot(age)
                self.d._load_state()
                self.assertEqual(self.d.snapshot()["accounts"], [])
                self.assertEqual(self.fetch_counts(1), [3])
        self.env.calls.clear()
        self.env.files = {k: v for k, v in self.env.files.items() if os.sep + "usage" + os.sep not in k}
        self.make(self.cfg)
        self.env.files[self.state_path] = "{"
        self.d._load_state()
        self.assertEqual(self.fetch_counts(1), [3])


class TestTickPointer(TickBase):
    def setUp(self):
        super().setUp()
        self.env.pointer_text = prof(ACC_A).home
        self.env.usages = {ACC_A: live(ACC_A, 90.0), ACC_B: live(ACC_B, 20.0), ACC_C: live(ACC_C, 50.0)}

    def pointer_writes(self):
        return [c for c in self.env.calls if c[0] == "write_pointer"]

    def test_pointer_repoints(self):
        self.d.tick()
        self.assertEqual(self.pointer_writes(), [("write_pointer", ACC_B)])
        snap = self.snap()
        self.assertEqual(snap["pointer_account_id"], ACC_A)
        self.assertEqual(snap["pointer_target_account_id"], ACC_B)
        self.assertEqual(len(self.env.logs("pointer_write")), 1)

    def test_pointer_gates(self):
        for kw in ({"dry_run": True}, {"auto_switch": False, "pointer_correction": True},
                   {"pointer_correction": False}):
            with self.subTest(kw=kw):
                self.env.calls.clear()
                self.make(make_cfg(usage_poll_s=60.0, **kw))
                self.d.tick()
                self.assertEqual(self.pointer_writes(), [])
                self.assertEqual(self.snap()["pointer_target_account_id"], ACC_B)

    def test_pointer_none_or_unknown(self):
        for text in (None, "/Users/user/elsewhere"):
            with self.subTest(text=text):
                self.env.pointer_text = text
                self.d.tick()
                self.assertEqual(self.pointer_writes(), [])
                self.assertIsNone(self.snap()["pointer_account_id"])

    def test_pointer_error_contained(self):
        self.env.pointer_raises = OSError()
        self.env.sessions = [sess(101, ACC_B)]
        self.d.tick()
        self.assertEqual(len(self.env.logs("pointer_error")), 1)
        self.assertEqual([r["pid"] for r in self.snap()["sessions"]], [101])

    def test_pointer_write_error_contained(self):
        base = self.deps
        self.deps = dataclasses.replace(base, write_pointer=lambda p, root: (_ for _ in ()).throw(OSError()))
        self.d = Daemon(self.cfg, self.deps, base.make_logger())
        self.env.sessions = [sess(101, ACC_B)]
        self.d.tick()
        self.assertEqual(len(self.env.logs("pointer_error")), 1)
        self.assertEqual(len(self.snap()["sessions"]), 1)


class TestTickContainment(TickBase):
    def test_discovery_failure_contained(self):
        self.env.profiles_raise = True
        self.d.tick()
        self.assertEqual(len([r for r in self.env.log_records
                              if r.startswith("event=discover_error what=profiles")]), 1)
        self.assertIn(self.state_path, self.env.files)
        self.env.profiles_raise = False
        self.d.tick()
        self.assertEqual(len(self.snap()["accounts"]), 3)

    def test_session_discovery_failure_contained(self):
        self.env.sessions_raise = True
        self.d.tick()
        self.assertEqual(len([r for r in self.env.log_records
                              if r.startswith("event=discover_error what=sessions")]), 1)
        self.assertEqual(len(self.fetches()), 3)
        self.assertEqual(len(self.snap()["accounts"]), 3)

    def test_unmanaged_row(self):
        s = dataclasses.replace(sess(101, ACC_A), config_dir="/Users/user/.claude")
        self.env.sessions = [s]
        self.d.tick()
        row = self.snap()["sessions"][0]
        self.assertIsNone(row["account_id"])
        self.assertIsNone(row["decision"])


class TestSnapshot(TickBase):
    def test_shape_and_no_secrets(self):
        self.env.sessions = [sess(101, ACC_A)]
        self.d.tick()
        text = self.env.files[self.state_path]
        snap = json.loads(text)
        self.assertEqual(snap["schema"], 1)
        self.assertEqual(set(snap), {"schema", "written_at", "daemon_pid", "flags", "accounts", "pointer_account_id",
                                     "pointer_target_account_id", "sessions"})
        self.assertEqual(snap["daemon_pid"], os.getpid())
        self.assertEqual(set(snap["sessions"][0]), {"pid", "session_id", "status", "account_id", "decision",
                                                    "would_switch", "in_flight", "in_flight_pid", "stuck", "model"})
        for secret in SECRETS + ("Fixture User",):
            self.assertNotIn(secret, text)
        self.assertIn(ACC_A, text)
        self.assertEqual(self.d.snapshot()["sessions"], snap["sessions"])

    def test_model_in_rows(self):
        self.env.sessions = [dataclasses.replace(sess(101, ACC_A), model="claude-fable-5-1")]
        self.d.tick()
        self.assertEqual(self.snap()["sessions"][0]["model"], "claude-fable-5-1")
        self.env.sessions = [sess(101, ACC_A)]
        self.d.tick()
        self.assertIsNone(self.snap()["sessions"][0]["model"])

    def test_fable_decision_in_tick(self):
        later = T0 + timedelta(days=2)
        self.env.usages[ACC_A] = dataclasses.replace(live(ACC_A, 20.0), fable=UsageWindow(90.0, later))
        for a in (ACC_B, ACC_C):
            self.env.usages[a] = dataclasses.replace(live(a, 20.0), fable=UsageWindow(10.0, later))
        self.env.sessions = [dataclasses.replace(sess(101, ACC_A, status="busy"), model="claude-fable-5-1")]
        self.d.tick()
        row = self.snap()["sessions"][0]
        self.assertEqual(row["decision"]["action"], "none")
        self.assertTrue(row["decision"]["reason"].startswith("max(5h,7d,fable)=90.0% >= soft 85.0%"),
                        row["decision"]["reason"])
        self.assertEqual([c for c in self.env.calls if c[0] == "switch"], [])

    def test_snapshot_written_under_daemon_lock(self):
        base = self.deps
        held = []

        def write_text(path, text):
            if path == self.state_path:
                held.append(self.d._lock.locked())
            base.write_text(path, text)

        self.deps = dataclasses.replace(base, write_text=write_text)
        self.d = Daemon(self.cfg, self.deps, base.make_logger())
        self.env.sessions = [sess(101, ACC_A)]
        self.d.tick()
        self.d.drain()
        self.assertEqual(held, [True, True])

    def test_snapshot_write_failure_contained(self):
        self.env.fail_write = {"state.json"}
        self.d.tick()
        self.assertEqual(len(self.env.logs("snapshot_write_failed")), 1)


# --- Task 08 ---------------------------------------------------------------------------------------------


class DispatchBase(TickBase):
    def setUp(self):
        super().setUp()
        self.env.pcts = {ACC_A: 97.0, ACC_B: 20.0, ACC_C: 50.0}
        self.env.sessions = [sess(101, ACC_A)]
        self.make(make_cfg())

    def spawns(self):
        return [c for c in self.env.calls if c[0] == "spawn"]

    def switches(self):
        return [c for c in self.env.calls if c[0] == "switch"]

    def row(self, pid):
        return next(r for r in self.d.snapshot()["sessions"] if r["pid"] == pid)

    def tick_after(self, seconds):
        self.env.advance(seconds)
        self.d.tick()

    def rec_path(self, pid=101, sid=SID1):
        return record_path(self.cfg.state_dir, sid, pid)

    def stored(self, pid=101, sid=SID1):
        return json.loads(self.env.files[self.rec_path(pid, sid)])


class TestDispatch(DispatchBase):
    def test_unmanaged_never_decided(self):
        self.env.pcts = {a: 100.0 for a in (ACC_A, ACC_B, ACC_C)}
        self.env.sessions = [dataclasses.replace(sess(101, ACC_A), config_dir="/Users/user/.claude")]
        self.d.tick()
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.switches(), [])
        self.assertIsNone(self.row(101)["account_id"])
        self.assertIsNone(self.row(101)["decision"])

    def test_dispatch_hard(self):
        self.env.results = [result("continued", None, ok=True, step="continue_prompt", new_pid=303)]
        self.env.probe.envs[303] = ProcEnv(config_dir=prof(ACC_B).home)
        self.d.tick()
        self.assertEqual(self.spawns(), [("spawn", "rotator-switch-101")])
        self.assertTrue(self.row(101)["in_flight"])
        self.assertEqual(self.row(101)["decision"]["action"], "hard")
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "hard")])
        self.assertEqual(self.d._inflight, {})
        self.assertEqual(self.d._inflight_handles, {})
        self.assertEqual(self.env.calls[-1], ("write", self.state_path))
        self.assertEqual(self.stored()["post_verify"], "ok")

    def test_one_inflight_per_session(self):
        self.d.tick()
        self.env.sessions = [sess(202, ACC_A)]
        self.tick_after(10)
        self.assertEqual(len(self.spawns()), 1)
        r = self.row(202)
        self.assertTrue(r["in_flight"])
        self.assertEqual(r["in_flight_pid"], 101)
        self.assertIsNone(r["decision"])

    def test_same_handle_other_session_skipped(self):
        self.d.tick()
        self.env.sessions = [sess(101, ACC_A), sess(303, ACC_A, sid=SID2, handle="term_00000001")]
        self.tick_after(10)
        self.assertEqual(len(self.spawns()), 1)
        self.assertEqual(self.row(303)["in_flight_pid"], 101)

    def test_after_worker_new_pid_gets_cooldown(self):
        self.env.results = [result("continued", None, ok=True, step="continue_prompt", new_pid=202)]
        self.d.tick()
        self.env.run_worker(0)
        self.env.sessions = [sess(202, ACC_A)]
        self.tick_after(180)
        self.assertEqual(len(self.spawns()), 1)
        decision = self.row(202)["decision"]
        self.assertEqual(decision["action"], "none")
        self.assertIn("cooldown", decision["reason"])
        self.tick_after(121)
        self.assertEqual(len(self.spawns()), 2)

    def test_excluded_target_not_repicked(self):
        self.env.pcts[ACC_A] = 90.0
        self.env.files[self.rec_path()] = json.dumps(record(101, excluded=[ACC_B]))
        self.env.results = [result()]
        self.d.tick()
        self.assertEqual(self.row(101)["decision"]["target_account_id"], ACC_C)
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_C, "soft")])


class TestInFlight(DispatchBase):
    def test_lease_boundary(self):
        self.d.tick()
        deadline = self.d._inflight[SID1].deadline
        self.assertEqual(deadline, 1000.0 + switcher.switch_budget_s(self.cfg.timeouts) + 30.0)
        self.env.mono_v = deadline
        self.d.tick()
        self.assertFalse(self.d._inflight[SID1].stuck)
        self.env.mono_v = deadline + 0.001
        self.d.tick()
        self.assertTrue(self.d._inflight[SID1].stuck)
        self.assertTrue(self.row(101)["stuck"])
        for _ in range(3):
            self.d.tick()
        self.assertEqual(len(self.spawns()), 1)
        self.assertEqual(len(self.env.logs("worker_overrun")), 1)

    def test_lease_follows_configured_timeouts(self):
        self.make(make_cfg(timeouts=switcher.Timeouts(ready_s=120.0)))
        self.d.tick()
        self.assertEqual(self.d._inflight[SID1].deadline, 1000.0 + 440.0 + 30.0)

    def test_on_attempt_refreshes_deadline(self):
        self.env.results = [result("untouched", "target_untrusted"),
                            result("continued", None, ok=True, step="continue_prompt")]
        base = self.env.deps()
        seen = []

        def switch(*a, **kw):
            seen.append(self.d._inflight[SID1].deadline)
            self.env.advance(100)
            return base.switch(*a, **kw)

        self.deps = self.env.deps(switch=switch)
        self.d = Daemon(self.cfg, self.deps, self.deps.make_logger())
        self.d.tick()
        self.env.run_worker(0)
        self.assertEqual(seen, [1410.0, 1510.0])


class TestTouchedNeverRetried(DispatchBase):
    def test_touched_never_retried(self):
        self.env.results = [result("interrupted", "interrupt_timeout", step="interrupt")]
        self.d.tick()
        self.env.run_worker(0)
        for _ in range(100):
            self.tick_after(10)
        self.assertEqual(len(self.spawns()), 1)
        self.assertEqual(len(self.switches()), 1)
        self.env.sessions = [sess(202, ACC_A)]
        self.tick_after(10)
        self.assertEqual(len(self.spawns()), 2)

    def test_final_write_failure_still_blocked(self):
        self.env.results = [result("interrupted", "interrupt_timeout", step="interrupt")]
        base = self.env.deps()
        n = [0]

        def write_text(path, text):
            if os.sep + "sessions" + os.sep in path:
                n[0] += 1
                if n[0] == 2:
                    raise OSError("disk full")
            base.write_text(path, text)

        self.deps = dataclasses.replace(base, write_text=write_text)
        self.d = Daemon(self.cfg, self.deps, base.make_logger())
        self.d.tick()
        self.env.run_worker(0)
        self.assertEqual(self.stored()["record_state"], "in_progress")
        self.tick_after(400)
        self.assertEqual(len(self.spawns()), 1)
        fresh = Daemon(self.cfg, self.deps, base.make_logger())
        fresh._load_state()
        fresh.tick()
        self.assertEqual(len(self.spawns()), 1)

    def test_lost_record_still_blocked_in_memory(self):
        self.env.results = [result("interrupted", "interrupt_timeout", step="interrupt")]
        self.d.tick()
        self.env.run_worker(0)
        del self.env.files[self.rec_path()]
        self.tick_after(400)
        self.assertEqual(len(self.spawns()), 1)
        self.assertIsNone(self.row(101)["decision"])

    def test_orphaned_intent_blocks_and_logs(self):
        self.env.files[self.rec_path()] = json.dumps(
            record(101, record_state="in_progress", result=None, owner_pid=999999))
        self.d._load_state()
        self.d.tick()
        self.tick_after(10)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(len(self.env.logs("orphaned_intent")), 1)

    def test_worker_error_recorded(self):
        self.env.results = [RuntimeError("x")]
        self.d.tick()
        self.env.run_worker(0)
        rec = self.stored()
        self.assertEqual(rec["worker_error"], "RuntimeError")
        self.assertIs(rec["touched"], True)
        self.assertEqual(self.d._inflight, {})
        self.assertEqual(len(self.env.logs("worker_error")), 1)
        for _ in range(40):
            self.tick_after(10)
        self.assertEqual(len(self.spawns()), 1)

    def test_worker_error_before_intent_recorded(self):
        base = self.env.deps()
        self.deps = dataclasses.replace(base, lock=lambda path: (_ for _ in ()).throw(OSError()))
        self.d = Daemon(self.cfg, self.deps, base.make_logger())
        self.d.tick()
        self.env.run_worker(0)
        self.assertEqual(self.stored()["worker_error"], "OSError")
        self.assertEqual(self.d._touched_at[SID1], T0)
        self.assertEqual(self.switches(), [])


class TestRetryRules(DispatchBase):
    def test_untouched_restores_touched_at(self):
        self.env.pcts[ACC_A] = 90.0
        self.env.results = [result("untouched", "not_idle"), result()]
        self.d.tick()
        self.env.run_worker(0)
        self.assertNotIn(SID1, self.d._touched_at)
        self.assertNotIn(identity_of(sess(101, ACC_A)), self.d._touched)
        self.tick_after(10)
        self.assertEqual(len(self.spawns()), 2)

    def test_untouched_restores_previous_value(self):
        earlier = T0 - timedelta(hours=1)
        self.d._touched_at[SID1] = earlier
        self.env.results = [result("untouched", "not_idle")]
        self.d.tick()
        self.env.run_worker(0)
        self.assertEqual(self.d._touched_at[SID1], earlier)

    def test_next_target_uses_dispatch_last_switch(self):
        self.env.results = [result("untouched", "target_untrusted"),
                            result("continued", None, ok=True, step="continue_prompt")]
        self.d.tick()
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "hard"), ("switch", 101, ACC_C, "hard")])

    def test_next_target_none_when_stopping(self):
        self.env.results = [result("untouched", "target_untrusted")]
        self.d.tick()
        self.d.request_stop()
        self.env.run_worker(0)
        self.assertEqual(len(self.switches()), 1)

    def test_hard_hold(self):
        self.env.results = [result("untouched", "input_not_empty"), result()]
        self.d.tick()
        self.env.run_worker(0)
        self.assertEqual(self.stored()["hard_hold_until"], "2026-10-28T12:05:00+00:00")
        for _ in range(29):
            self.tick_after(10)
        self.assertEqual(len(self.spawns()), 1)
        self.assertEqual(len(self.env.logs("hard_hold_active")), 1)
        self.tick_after(10)
        self.assertEqual(self.env.now_v, T0 + timedelta(seconds=300))
        self.assertEqual(len(self.spawns()), 2)

    def test_soft_not_held(self):
        self.env.results = [result("untouched", "input_not_empty"), result()]
        self.d.tick()
        self.env.run_worker(0)
        self.env.pcts[ACC_A] = 90.0
        self.tick_after(60)
        self.assertEqual(len(self.spawns()), 2)
        self.env.run_worker(1)
        self.assertEqual(self.switches()[-1], ("switch", 101, ACC_B, "soft"))

    def test_unswitchable_skipped(self):
        self.env.results = [result("untouched", "pane_binding_unverified"), result()]
        self.d.tick()
        self.env.run_worker(0)
        for _ in range(5):
            self.tick_after(10)
        self.assertEqual(len(self.spawns()), 1)
        self.env.sessions = [sess(202, ACC_A)]
        self.tick_after(10)
        self.assertEqual(len(self.spawns()), 2)


class TestGates(DispatchBase):
    def test_dry_run(self):
        self.env.pointer_text = prof(ACC_A).home
        self.make(make_cfg(dry_run=True))
        self.d.tick()
        self.tick_after(10)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.switches(), [])
        self.assertEqual([c for c in self.env.calls if c[0] == "write_pointer"], [])
        self.assertEqual(self.row(101)["would_switch"], {"action": "hard", "target_account_id": ACC_B})
        self.assertEqual(len(self.env.logs("would_switch")), 1)
        self.env.pcts[ACC_A] = 90.0
        self.tick_after(30)
        self.assertEqual(self.row(101)["would_switch"]["action"], "soft")
        self.assertEqual(len(self.env.logs("would_switch")), 2)

    def test_auto_switch_off(self):
        self.make(make_cfg(auto_switch=False))
        self.d.tick()
        self.assertEqual(self.spawns(), [])
        self.assertIsNotNone(self.row(101)["would_switch"])


class TestSessionContainment(DispatchBase):
    def test_session_error_contained(self):
        self.env.files[self.rec_path()] = json.dumps(record(101, excluded=5))
        self.env.sessions = [sess(101, ACC_A), sess(102, ACC_A, sid=SID2, handle="term_00000002")]
        self.d.tick()
        self.assertEqual(len([r for r in self.env.log_records
                              if r.startswith("event=session_error pid=101")]), 1)
        self.assertEqual(self.spawns(), [("spawn", "rotator-switch-102")])
        self.assertIsNone(self.row(101)["decision"])
        self.assertFalse(self.row(101)["in_flight"])

    def test_spawn_error_contained(self):
        base = self.env.deps()

        def spawn(fn, name):
            if name == "rotator-switch-101":
                raise RuntimeError("no threads")
            return base.spawn(fn, name)

        self.deps = dataclasses.replace(base, spawn=spawn)
        self.d = Daemon(self.cfg, self.deps, base.make_logger())
        self.env.sessions = [sess(101, ACC_A), sess(102, ACC_A, sid=SID2, handle="term_00000002")]
        self.d.tick()
        self.assertEqual(len(self.env.logs("spawn_error")), 1)
        self.assertNotIn(SID1, self.d._inflight)
        self.assertNotIn("term_00000001", self.d._inflight_handles)
        self.assertEqual(self.spawns(), [("spawn", "rotator-switch-102")])

    def test_secret_free_channels(self):
        self.env.results = [result("exited", "exit_timeout", step="wait_exit", cmd="cd /x && claude --resume y")]
        self.d.tick()
        self.env.run_worker(0)
        texts = [self.env.files[self.state_path], self.env.files[self.rec_path()]] + self.env.log_records
        for t in texts:
            for secret in SECRETS:
                self.assertNotIn(secret, t)
        self.assertIn(ACC_A, self.env.files[self.state_path])


# --- Task 09 ---------------------------------------------------------------------------------------------


class RunDaemonBase(DispatchBase):
    @property
    def lock_path(self):
        return os.path.join(self.cfg.state_dir, "daemon.lock")

    def run_d(self, deps=None):
        return run_daemon(self.cfg, deps or self.env.deps())


class TestRunDaemon(RunDaemonBase):
    def test_second_instance_exits_1(self):
        for owner, shown in (("4242", "(pid 4242)"), ("garbage", "(pid ?)"), (None, "(pid ?)")):
            with self.subTest(owner=owner):
                self.env.calls.clear()
                self.env.log_records.clear()
                self.env.held = {self.lock_path}
                if owner is None:
                    self.env.files.pop(self.lock_path, None)
                else:
                    self.env.files[self.lock_path] = owner
                self.assertEqual(self.run_d(), 1)
                self.assertEqual(self.fetches(), [])
                self.assertEqual(self.spawns(), [])
                self.assertIsNone(self.env.signal_handler)
                self.assertTrue(any("another rotator daemon is running " + shown in r
                                    for r in self.env.log_records))

    def test_missing_profiles_root_exits_2(self):
        self.env.isdir_ok = False
        self.assertEqual(self.run_d(), 2)
        self.assertEqual([c for c in self.env.calls if c[0] == "lock"], [])
        self.assertEqual(len(self.env.logs("profiles_root_missing")), 1)

    def test_signal_stops_after_current_tick(self):
        self.env.sessions = []
        self.env.wait_hook = lambda n: self.env.signal_handler() if n == 2 else None
        self.assertEqual(self.run_d(), 0)
        self.assertEqual(self.env.waits, [self.cfg.session_poll_s, self.cfg.session_poll_s])
        self.assertEqual(self.env.files[self.lock_path], str(os.getpid()))
        self.assertIn(("release", self.lock_path), self.env.calls)
        self.assertEqual(self.env.held, set())
        self.assertEqual(len(self.env.logs("daemon_stop")), 1)
        self.assertIn(self.state_path, self.env.files)

    def _stop_on_first_wait(self, advance):
        def hook(n):
            if n == 1:
                self.env.mono_v += advance
                self.env.signal_handler()
        self.env.wait_hook = hook

    def test_drain_abandons_overrun_worker(self):
        self._stop_on_first_wait(500.0)
        self.assertEqual(self.run_d(), 0)
        self.assertEqual(len(self.env.spawned), 1)
        self.assertEqual(self.env.spawned[0].joins, [0.0])
        self.assertEqual(len(self.env.logs("worker_abandoned")), 1)

    def test_drain_waits_remaining_lease(self):
        self._stop_on_first_wait(310.0)
        self.assertEqual(self.run_d(), 0)
        self.assertEqual(self.env.spawned[0].joins, [100.0])
        self.assertEqual(len(self.env.logs("worker_abandoned")), 1)

    def test_drain_finished_worker_not_abandoned(self):
        self.env.results = [result()]

        def hook(n):
            if n == 1:
                self.env.run_worker(0)
                self.env.signal_handler()
        self.env.wait_hook = hook
        self.assertEqual(self.run_d(), 0)
        self.assertEqual(self.env.logs("worker_abandoned"), [])

    def test_tick_exception_contained(self):
        calls = [0]

        def now():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("clock")
            return self.env.now_v

        self.env.sessions = []
        self.env.wait_hook = lambda n: self.env.signal_handler() if n == 2 else None
        self.assertEqual(self.run_d(self.env.deps(now=now)), 0)
        self.assertEqual(len([r for r in self.env.log_records if r == "event=tick_error exc=RuntimeError"]), 1)
        self.assertEqual(len(self.fetches()), 3)


# --- token refresh wiring ----------------------------------------------------------------------------------

DEFERRED = {"state": "skipped", "reason": "deferred", "at": None, "last_refreshed_at": None}


class TestTickRefresh(TickBase):
    def setUp(self):
        super().setUp()
        self.env.gate_results = {ACC_A: dict(DEFERRED), ACC_B: dict(DEFERRED)}
        env = self.env
        base = env.deps()

        def discover_sessions(homes):
            env.refresh_calls.append(("discover_sessions",))
            return base.discover_sessions(homes)
        self.deps = env.deps(discover_sessions=discover_sessions)
        self.d = Daemon(self.cfg, self.deps, self.deps.make_logger())

    def network_calls(self):
        return [c for c in self.env.refresh_calls if c[0] == "refresh_token" and c[2] is True]

    def test_phase_order_and_one_per_tick(self):
        self.d.tick()
        self.assertEqual(self.env.refresh_calls, [
            ("refresh_token", ACC_A, False), ("refresh_token", ACC_B, False), ("refresh_token", ACC_C, False),
            ("discover_sessions",), ("refresh_held", True), ("refresh_held", False),
            ("refresh_token", ACC_A, True)])
        self.assertEqual(self.account(ACC_A)["refresh"],
                         {"state": "not_needed", "reason": "fresh", "at": None, "last_refreshed_at": None})
        self.assertEqual(self.account(ACC_B)["refresh"], DEFERRED)
        self.env.refresh_calls.clear()
        self.env.advance(10)
        self.d.tick()
        self.assertEqual(self.network_calls(), [("refresh_token", ACC_B, True)])
        self.assertNotIn(("refresh_token", ACC_A, False), self.env.refresh_calls)

    def test_no_phase_two_when_stopping(self):
        self.d.request_stop()
        self.d.tick()
        self.assertEqual(self.network_calls(), [])
        self.assertFalse([c for c in self.env.refresh_calls if c[0] == "refresh_held"])

    def test_no_phase_two_on_discovery_errors(self):
        for attr in ("profiles_raise", "sessions_raise"):
            with self.subTest(attr=attr):
                self.setUp()
                setattr(self.env, attr, True)
                self.d.tick()
                self.assertEqual(self.network_calls(), [])
                self.assertFalse([c for c in self.env.refresh_calls if c[0] == "refresh_held"])

    def test_cache_fallback_pinned(self):
        self.env.gate_results = {ACC_A: {"state": "stopped", "reason": "invalid_grant", "at": None,
                                         "last_refreshed_at": None}}
        self.env.usages[ACC_A] = AccountUsage(
            account_id=ACC_A, source="cache", fetched_at=T0 - timedelta(hours=2),
            five_hour=UsageWindow(90.0, T0 - timedelta(minutes=1)), error="token_expired")
        self.d.tick()
        a = self.account(ACC_A)
        self.assertEqual(a["source"], "cache")
        self.assertEqual(a["refresh"]["reason"], "invalid_grant")
        self.assertEqual(usage_from_summary(a).max_utilization(T0), 0.0)

    def test_refreshed_account_fetched_next_tick(self):
        self.env.refresh_results = {ACC_A: {"state": "refreshed", "reason": "ok", "at": None,
                                            "last_refreshed_at": None}}
        self.d.tick()
        self.env.calls.clear()
        self.env.advance(10)
        self.d.tick()
        self.assertEqual(self.fetches(), [("fetch", ACC_A)])


class TestTickRefreshSkip(DispatchBase):
    def setUp(self):
        super().setUp()
        self.env.gate_results = {a: dict(DEFERRED) for a in (ACC_A, ACC_B, ACC_C)}
        self.env.results = [result("untouched", "target_untrusted"),
                            result("continued", None, ok=True, step="continue_prompt")]
        env = self.env
        base = env.deps()
        self.seen: List[frozenset] = []

        def switch(session, target, mode, **kw):
            self.seen.append(self.d._switch_accounts())
            return base.switch(session, target, mode, **kw)
        self.deps = env.deps(switch=switch)
        self.d = Daemon(self.cfg, self.deps, self.deps.make_logger())

    def test_in_flight_accounts_never_refreshed(self):
        self.d.tick()
        self.assertEqual(self.d._switch_accounts(), frozenset({ACC_A, ACC_B}))
        net = [c[1] for c in self.env.refresh_calls if c[0] == "refresh_token" and c[2] is True]
        self.assertEqual(net, [ACC_C])
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "hard"), ("switch", 101, ACC_C, "hard")])
        self.assertEqual(self.seen[1], frozenset({ACC_A, ACC_B, ACC_C}))
        self.assertEqual(self.d._switch_accounts(), frozenset())


class TestRefreshExitLog(RunDaemonBase):
    def run_once(self):
        env = self.env
        env.wait_hook = lambda n: env.signal_handler() if n == 1 else None
        marks: List[int] = []
        base = env.deps()

        def write_text(path, text):
            base.write_text(path, text)
            if path.endswith("state.json"):
                marks.append(len(env.log_records))
        self.assertEqual(self.run_d(env.deps(write_text=write_text)), 0)
        return marks

    def test_held_ids_logged_before_drain(self):
        self.env.held_ids = ["a1"]
        marks = self.run_once()
        hits = [i for i, r in enumerate(self.env.log_records)
                if r == "event=token_refresh_held_at_exit accounts=a1"]
        self.assertEqual(len(hits), 1)
        self.assertGreater(marks[-1], hits[0])

    def test_nothing_held_no_log(self):
        self.run_once()
        self.assertEqual(self.env.logs("token_refresh_held_at_exit"), [])

    def test_held_raising_still_exits_clean(self):
        self.env.held_raises = True
        self.run_once()
        self.assertEqual(self.env.logs("token_refresh_held_at_exit"), [])


class TestRealDeps(unittest.TestCase):
    def test_identity(self):
        deps = real_deps(make_cfg())
        self.assertIs(deps.switch, switcher.switch_session)
        self.assertIs(deps.make_driver, switcher.OrcaCliDriver)
        self.assertIsInstance(deps.procs, switcher.OsProcessProbe)
        self.assertIs(deps.lock, try_lock)
        self.assertIs(deps.write_text, atomic_write_text)
        self.assertIs(deps.list_dir, os.listdir)
        self.assertIs(deps.isdir, os.path.isdir)
        self.assertIs(deps.monotonic, time.monotonic)
        from rotator import pointer, sessions
        self.assertIs(deps.read_pointer, pointer.read_selected_host)
        self.assertIs(deps.write_pointer, pointer.set_selected_host)
        self.assertIs(deps.discover_sessions, sessions.discover_sessions_with_model)
        self.assertIsInstance(deps.refresh_token.__self__, refresh.Refresher)
        self.assertIs(deps.refresh_held.__self__, deps.refresh_token.__self__)
        self.assertEqual(deps.refresh_token.__name__, "refresh")
        self.assertEqual(deps.refresh_held.__name__, "held_accounts")

    def test_building_touches_nothing(self):
        with unittest.mock.patch("subprocess.run", side_effect=AssertionError("real seam")), \
                unittest.mock.patch.object(usage._NO_REDIRECT_OPENER, "open", side_effect=AssertionError("real seam")):
            deps = real_deps(dataclasses.replace(make_cfg(), profiles_root="/nonexistent-rotator-test"))
        self.assertIsInstance(deps, Deps)

    def test_with_model_reader_bound(self):
        self.assertEqual(real_deps(make_cfg()).discover_sessions.__name__, "discover_sessions_with_model")

    def test_pid_alive_guards(self):
        self.assertIs(daemon._pid_alive(0), False)
        self.assertIs(daemon._pid_alive(-1), False)
        self.assertIs(daemon._pid_alive(True), False)
        self.assertIs(daemon._pid_alive(os.getpid()), True)

    def test_spawn_daemon_thread(self):
        t = daemon._spawn(lambda: None, "t")
        self.assertIsInstance(t, threading.Thread)
        self.assertIs(t.daemon, True)
        self.assertEqual(t.name, "t")
        t.join()


class TestHeldTargets(DispatchBase):
    """An account holding an unwritten token set is never a switch target; the session's own account is."""

    def excluded_logs(self):
        return self.env.logs("target_excluded")

    def test_held_target_skipped_for_next_best(self):
        self.env.held_ids = [ACC_B]
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.d.tick()
        decision = self.row(101)["decision"]
        self.assertEqual((decision["action"], decision["target_account_id"]), ("hard", ACC_C))
        self.assertIn("excluded %s: token_held" % ACC_B, decision["reason"])
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_C, "hard")])
        self.assertEqual(self.excluded_logs(),
                         ["event=target_excluded pid=101 account=%s reason=token_held" % ACC_B])

    def test_all_targets_held_no_dispatch(self):
        self.env.held_ids = [ACC_B, ACC_C]
        self.d.tick()
        self.tick_after(10)
        self.assertEqual(self.spawns(), [])
        decision = self.row(101)["decision"]
        self.assertEqual((decision["action"], decision["target_account_id"]), ("none", None))
        self.assertIn("excluded %s,%s: token_held" % (ACC_B, ACC_C), decision["reason"])
        self.assertEqual(len(self.excluded_logs()), 1)

    def test_dry_run_would_switch_skips_held(self):
        self.make(make_cfg(dry_run=True))
        self.env.held_ids = [ACC_B]
        self.d.tick()
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.row(101)["would_switch"], {"action": "hard", "target_account_id": ACC_C})

    def test_non_held_and_own_held_unchanged(self):
        for held in ([], [ACC_C], [ACC_A]):
            with self.subTest(held=held):
                self.setUp()
                self.env.held_ids = list(held)
                self.d.tick()
                decision = self.row(101)["decision"]
                self.assertEqual((decision["action"], decision["target_account_id"]), ("hard", ACC_B))
                self.assertNotIn("token_held", decision["reason"])
                self.assertEqual(self.excluded_logs(), [])
                self.assertEqual(len(self.spawns()), 1)

    def test_own_and_target_held_keeps_own(self):
        self.env.held_ids = [ACC_A, ACC_B]
        self.env.results = [result("continued", None, ok=True, step="continue_prompt")]
        self.d.tick()
        decision = self.row(101)["decision"]
        self.assertEqual((decision["action"], decision["target_account_id"]), ("hard", ACC_C))
        self.assertTrue(decision["reason"].endswith("; excluded %s: token_held" % ACC_B))
        self.assertNotIn(ACC_A + ",", decision["reason"])
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_C, "hard")])

    def test_held_query_error_fails_closed(self):
        self.env.held_raises = True
        # Phase 2 also queries held ids; only the decision path is under test here.
        with unittest.mock.patch.object(self.d._usage, "refresh_one", return_value=None):
            self.d.tick()
        self.assertEqual(self.spawns(), [])
        decision = self.row(101)["decision"]
        self.assertEqual(decision["action"], "none")
        self.assertTrue(decision["reason"].endswith("; excluded %s,%s: held_unknown" % (ACC_B, ACC_C)))
        self.assertIn("no target", decision["reason"])
        self.assertNotIn("unmanaged", decision["reason"])
        self.assertGreaterEqual(len(self.env.logs("held_error")), 1)

    def test_retarget_skips_held(self):
        self.env.results = [result("untouched", "target_untrusted"),
                            result("continued", None, ok=True, step="continue_prompt")]
        self.d.tick()
        self.env.held_ids = [ACC_C]
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "hard")])
        self.assertEqual(self.excluded_logs(),
                         ["event=target_excluded pid=101 account=%s reason=token_held" % ACC_C])
        self.assertEqual(self.d._inflight, {})

    def test_retarget_held_error_fails_closed(self):
        self.env.results = [result("untouched", "target_untrusted"),
                            result("continued", None, ok=True, step="continue_prompt")]
        self.d.tick()
        self.env.held_raises = True
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "hard")])
        self.assertGreaterEqual(len(self.env.logs("held_error")), 1)

    def test_retarget_non_held_unchanged(self):
        self.env.results = [result("untouched", "target_untrusted"),
                            result("continued", None, ok=True, step="continue_prompt")]
        self.d.tick()
        self.env.held_ids = [ACC_B]
        self.env.run_worker(0)
        self.assertEqual(self.switches(), [("switch", 101, ACC_B, "hard"), ("switch", 101, ACC_C, "hard")])
        self.assertEqual(self.excluded_logs(), [])


class TestHeldPointer(TickBase):
    """The selected-host pointer is never moved to an account holding an unwritten token set."""

    def setUp(self):
        super().setUp()
        self.env.pointer_text = prof(ACC_A).home
        self.env.usages = {ACC_A: live(ACC_A, 90.0), ACC_B: live(ACC_B, 20.0), ACC_C: live(ACC_C, 50.0)}

    def pointer_writes(self):
        return [c for c in self.env.calls if c[0] == "write_pointer"]

    def test_held_target_skipped_for_next_best(self):
        self.env.held_ids = [ACC_B]
        self.d.tick()
        self.assertEqual(self.pointer_writes(), [("write_pointer", ACC_C)])
        self.assertEqual(self.snap()["pointer_target_account_id"], ACC_C)
        self.assertEqual(self.env.logs("target_excluded"),
                         ["event=target_excluded what=pointer account=%s reason=token_held" % ACC_B])

    def test_all_held_no_write_logged_once(self):
        self.env.held_ids = [ACC_B, ACC_C]
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.pointer_writes(), [])
        self.assertIsNone(self.snap()["pointer_target_account_id"])
        self.assertEqual(self.snap()["pointer_account_id"], ACC_A)
        self.assertEqual(len(self.env.logs("target_excluded")), 1)

    def test_held_query_error_no_write(self):
        self.env.held_raises = True
        with unittest.mock.patch.object(self.d._usage, "refresh_one", return_value=None):
            self.d.tick()
        self.assertEqual(self.pointer_writes(), [])
        self.assertIsNone(self.snap()["pointer_target_account_id"])
        self.assertGreaterEqual(len(self.env.logs("held_error")), 1)

    def test_non_held_and_pointer_held_unchanged(self):
        for held in ([], [ACC_C], [ACC_A]):
            with self.subTest(held=held):
                self.setUp()
                self.env.held_ids = list(held)
                self.d.tick()
                self.assertEqual(self.pointer_writes(), [("write_pointer", ACC_B)])
                self.assertEqual(self.env.logs("target_excluded"), [])


class TestSwitchSignature(RunSwitchBase):
    """run_switch's deps.switch call must bind to the real switcher.switch_session signature."""

    def capture(self):
        seen = []

        def switch(*args, **kw):
            seen.append((args, kw))
            return result("continued", None, ok=True, step="continue_prompt")
        return seen, self.env.deps(switch=switch)

    def test_call_binds_real_signature(self):
        seen, deps = self.capture()
        self.run_switch(deps=deps)
        self.assertEqual(len(seen), 1)
        args, kw = seen[0]
        bound = inspect.signature(switcher.switch_session).bind(*args, **kw)
        self.assertIs(bound.arguments["session"], self.s)
        self.assertEqual(bound.arguments["target"].account_id, ACC_B)
        self.assertEqual(bound.arguments["mode"], "soft")
        self.assertIs(bound.arguments["timeouts"], self.cfg.timeouts)
        self.assertEqual(bound.arguments["profiles_root"], ROOT)
        self.assertEqual(bound.arguments["continue_prompt"], self.cfg.continue_prompt)

    def test_bind_rejects_drift(self):
        seen, deps = self.capture()
        self.run_switch(deps=deps)
        args, kw = seen[0]
        sig = inspect.signature(switcher.switch_session)
        with self.assertRaises(TypeError):
            sig.bind(*args, **dict(kw, not_a_parameter=1))
        with self.assertRaises(TypeError):
            sig.bind(*args, **{k: v for k, v in kw.items() if k != "driver"})
        with self.assertRaises(TypeError):
            sig.bind(*args[:2], **kw)


if __name__ == "__main__":
    unittest.main()
