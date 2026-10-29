"""The rotator daemon: schedules account switches for live Claude Code sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from rotator import accounts, pointer, sessions, switcher, usage
from rotator.accounts import profile_for_config_dir
from rotator.config import LOG_DIR_DEFAULT, Config
from rotator.models import AccountUsage, Profile, Session, UsageWindow
from rotator.policy import PolicyConfig, decide, decide_pointer
from rotator.sessions import session_homes
from rotator.switcher import ProcessProbe, SwitchResult, TerminalDriver, switch_budget_s

Identity = Tuple[int, str, Optional[str]]

LEASE_MARGIN_S = 30.0
STATE_FILENAME = "state.json"
LOCK_FILENAME = "daemon.lock"
SESSIONS_DIRNAME = "sessions"
LOCKS_DIRNAME = "locks"
SNAPSHOT_SCHEMA = 1
RECORD_SCHEMA = 1
LOG_FILENAME = "rotator.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUPS = 3
RECORD_STATES = ("in_progress", "final")
IN_PROGRESS_HINT = "A switch is running for this session; do not type in the pane until it finishes."
ORPHANED_HINT = (
    "A switch was started but the rotator stopped before recording its outcome: Ctrl-C, /exit or the relaunch "
    "line may already have been typed. Outcome unknown: look at the pane before typing anything; the rotator "
    "never re-sends it."
)

# The five classes partition switcher.REASONS (pinned by a test); a token missing from all of them is
# treated as structural, so a reason added upstream fails closed instead of being retried.
STRUCTURAL_REASONS = frozenset({
    "no_terminal_handle", "terminal_handle_invalid", "no_argv", "relaunch_unbuildable", "same_account",
    "cwd_missing", "registry_mismatch", "pane_binding_unverified",
})
TARGET_REASONS = frozenset({"target_untrusted", "target_home_missing"})
TRANSIENT_REASONS = frozenset({
    "not_idle", "dialog_open", "input_not_empty", "screen_unreadable", "trust_dialog", "send_refused",
    "pane_not_claude", "transcript_not_visible",
})
GONE_REASONS = frozenset({"pid_not_alive"})
TOUCHED_ONLY_REASONS = frozenset({
    "interrupt_timeout", "exit_timeout", "session_already_running", "ready_timeout", "launched_elsewhere",
    "account_unverified", "wrong_account", "prompt_blocked_permission", "prompt_unconfirmed",
})


def classify_untouched(reason: Optional[str]) -> str:
    """Class of an untouched result's reason: "target", "transient", "gone" or "structural"."""
    if reason in TARGET_REASONS:
        return "target"
    if reason in TRANSIENT_REASONS:
        return "transient"
    if reason in GONE_REASONS:
        return "gone"
    return "structural"


def touched(record: Mapping[str, Any]) -> bool:
    """True when the record's identity may have received input (or its outcome is unknown)."""
    if record.get("record_state") == "in_progress":
        return True
    if record.get("worker_error") is not None:
        return True
    res = record.get("result")
    if not isinstance(res, dict):
        return True
    return res.get("state") != "untouched"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def identity_of(session: Session) -> Identity:
    return (session.pid, session.session_id,
            _iso(session.started_at) if session.started_at is not None else None)


def _record_identity(rec: Mapping[str, Any]) -> Identity:
    pid, sid, started = rec.get("pid"), rec.get("session_id"), rec.get("started_at")
    if not (type(pid) is int and pid > 0):
        raise ValueError("record pid")
    if not (isinstance(sid, str) and sid != ""):
        raise ValueError("record session_id")
    if not (started is None or isinstance(started, str)):
        raise ValueError("record started_at")
    return (pid, sid, started)


def key_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def lock_paths(state_dir: str, session: Session) -> List[str]:
    """Session lock first, then the pane lock when the session has a terminal handle."""
    paths = [os.path.join(state_dir, LOCKS_DIRNAME, "s-%s.lock" % key_of(session.session_id))]
    if session.terminal_handle is not None:
        paths.append(os.path.join(state_dir, LOCKS_DIRNAME, "h-%s.lock" % key_of(session.terminal_handle)))
    return paths


def record_path(state_dir: str, session_id: str, pid: int) -> str:
    return os.path.join(state_dir, SESSIONS_DIRNAME, "%s-%d.json" % (key_of(session_id), pid))


class Worker(Protocol):
    def join(self, timeout: Optional[float] = None) -> None: ...

    def is_alive(self) -> bool: ...


@dataclass(frozen=True)
class SwitchOutcome:
    status: str  # "busy" | "refused_touched" | "intent_failed" | "done"
    record: Optional[dict]
    result: Optional[SwitchResult]


@dataclass
class InFlight:
    identity: Identity
    handle: Optional[str]
    deadline: float
    worker: Optional[Worker]
    stuck: bool = False


@dataclass(frozen=True)
class Deps:
    """Every OS, clock, thread, lock, signal, Orca and network access of the daemon and CLI; no defaults so a
    test that forgets a seam fails with TypeError. Build the real one with real_deps(cfg)."""

    discover_profiles: Callable[[str], List[Profile]]
    fetch_usage: Callable[[Profile], AccountUsage]
    discover_sessions: Callable[[Sequence[str]], List[Session]]
    switch: Callable[..., SwitchResult]
    make_driver: Callable[[], TerminalDriver]
    procs: ProcessProbe
    read_pointer: Callable[[str], Optional[str]]
    write_pointer: Callable[[Profile, str], Optional[str]]
    now: Callable[[], datetime]
    monotonic: Callable[[], float]
    spawn: Callable[[Callable[[], None], str], Worker]
    wait_stop: Callable[[threading.Event, float], bool]
    lock: Callable[[str], Optional["FileLock"]]
    read_text: Callable[[str], str]
    write_text: Callable[[str, str], None]
    list_dir: Callable[[str], List[str]]
    isdir: Callable[[str], bool]
    pid_alive: Callable[[int], bool]
    install_signals: Callable[[Callable[[], None]], None]
    make_logger: Callable[[], logging.Logger]


# --- persistence -----------------------------------------------------------------------------------------


def _unlink_quiet(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


def atomic_write_text(path: str, text: str) -> None:
    """Write ``text`` to ``path`` atomically: 0700 parent dir, 0600 file, fsync, then os.replace."""
    d = os.path.dirname(path)
    os.makedirs(d, 0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
    try:
        os.fchmod(fd, 0o600)
        f = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        _unlink_quiet(tmp)
        raise
    try:
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise


class FileLock:
    """An exclusive flock held on an open fd; the lock file itself is never deleted."""

    def __init__(self, fd: int) -> None:
        self._fd: Optional[int] = fd

    def write_owner(self, pid: int) -> None:
        if self._fd is None:
            return
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, str(pid).encode("ascii"))

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def try_lock(path: str) -> Optional[FileLock]:
    """Take ``flock(LOCK_EX | LOCK_NB)`` on ``path``; None when another holder has it."""
    os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return FileLock(fd)


def _window(w: Optional[UsageWindow]) -> Optional[dict]:
    if w is None:
        return None
    return {"utilization": w.utilization,
            "resets_at": _iso(w.resets_at) if w.resets_at is not None else None}


def usage_summary(u: AccountUsage) -> dict:
    """Allowlisted, secret-free view of one account's usage."""
    return {
        "account_id": u.account_id,
        "source": u.source,
        "error": u.error,
        "fetched_at": _iso(u.fetched_at),
        "five_hour": _window(u.five_hour),
        "seven_day": _window(u.seven_day),
    }


def _window_from(w: Any) -> Optional[UsageWindow]:
    if w is None:
        return None
    return UsageWindow(w["utilization"],
                       _parse_iso(w["resets_at"]) if w.get("resets_at") is not None else None)


def usage_from_summary(d: Mapping[str, Any]) -> Optional[AccountUsage]:
    """Inverse of usage_summary; None for anything the model would reject."""
    try:
        fetched = _parse_iso(d["fetched_at"])
        if fetched is None:
            return None
        return AccountUsage(account_id=d["account_id"], source=d["source"], fetched_at=fetched,
                            five_hour=_window_from(d.get("five_hour")),
                            seven_day=_window_from(d.get("seven_day")),
                            error=d.get("error"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def result_dict(r: SwitchResult) -> dict:
    return {
        "ok": r.ok,
        "step": r.step,
        "reason": r.reason,
        "state": r.state,
        "new_pid": r.new_pid,
        "recoverable": r.recoverable,
        "request_id": r.request_id,
        "other_account_id": r.other_account_id,
        "relaunch_command": r.relaunch_command,
    }


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


def load_records(state_dir: str, deps: Deps, logger: logging.Logger) -> Dict[Identity, dict]:
    """Every readable switch record under ``<state_dir>/sessions``, keyed by identity."""
    d = os.path.join(state_dir, SESSIONS_DIRNAME)
    try:
        names = deps.list_dir(d)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error("event=record_load_error file=- exc=%s", type(e).__name__)
        return {}
    out: Dict[Identity, dict] = {}
    for name in sorted(names):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            rec = json.loads(deps.read_text(os.path.join(d, name)))
            if not (isinstance(rec, dict) and rec.get("schema") == RECORD_SCHEMA
                    and rec.get("record_state") in RECORD_STATES):
                raise ValueError("record shape")
            out[_record_identity(rec)] = rec
        except Exception as e:
            logger.error("event=record_load_error file=%s exc=%s", name, type(e).__name__)
    return out


# --- one guarded switch ----------------------------------------------------------------------------------


def post_verify(new_pid: int, target: Profile, procs: ProcessProbe) -> str:
    """Check the new process's own CLAUDE_CONFIG_DIR: "ok", "mismatch" or "unreadable"."""
    try:
        env = procs.read_env(new_pid)
    except Exception:
        return "unreadable"
    if env is None or env.config_dir is None:
        return "unreadable"
    if os.path.normpath(env.config_dir) == os.path.normpath(target.home):
        return "ok"
    return "mismatch"


def _new_record(session: Session, *, source: str, mode: str, target: Profile, attempt: int, now: datetime,
                record_state: str, excluded: Sequence[str], hold: Optional[str],
                result: Optional[SwitchResult] = None, unswitchable: Optional[str] = None) -> dict:
    rec = {
        "schema": RECORD_SCHEMA,
        "record_state": record_state,
        "owner_pid": os.getpid(),
        "attempt": attempt,
        "pid": session.pid,
        "session_id": session.session_id,
        "started_at": identity_of(session)[2],
        "updated_at": _iso(now),
        "source": source,
        "mode": mode,
        "target_account_id": target.account_id,
        "result": result_dict(result) if result is not None else None,
        "hint": switcher.STATE_HINTS.get(result.state, "") if result is not None else "",
        "touched": None,
        "unswitchable": unswitchable,
        "excluded": list(excluded),
        "hard_hold_until": hold,
        "post_verify": None,
        "worker_error": None,
    }
    rec["touched"] = touched(rec)
    return rec


def _log_result(logger: logging.Logger, session: Session, source: str, mode: str, target: Profile, attempt: int,
                r: SwitchResult) -> None:
    logger.info("event=switch_result pid=%d source=%s mode=%s target=%s attempt=%d ok=%s step=%s reason=%s "
                "state=%s new_pid=%s recoverable=%s request_id=%s other_account_id=%s",
                session.pid, source, mode, target.account_id, attempt, r.ok, r.step, r.reason, r.state,
                r.new_pid, r.recoverable, r.request_id, r.other_account_id)
    if r.relaunch_command and not r.ok:
        logger.warning("event=relaunch_pending pid=%d state=%s hint=%r command=%r",
                       session.pid, r.state, switcher.STATE_HINTS.get(r.state, ""), r.relaunch_command)


def run_switch(session: Session, own: Profile, target: Profile, mode: str, *, cfg: Config, deps: Deps,
               logger: logging.Logger, source: str, profiles_count: int,
               next_target: Optional[Callable[[FrozenSet[str]], Optional[Tuple[str, Profile]]]] = None,
               on_attempt: Optional[Callable[[], None]] = None,
               on_intent: Optional[Callable[[Identity, datetime], None]] = None,
               is_touched: Optional[Callable[[Identity], bool]] = None) -> SwitchOutcome:
    """Run one guarded switch for session; shared by the daemon worker and `rotator switch`. Never re-sends to a
    touched identity; writes an in_progress record before every switch_session call."""
    ident = identity_of(session)
    pid = session.pid
    path = record_path(cfg.state_dir, session.session_id, pid)
    held: List[Any] = []
    try:
        for p in lock_paths(cfg.state_dir, session):
            lk = deps.lock(p)
            if lk is None:
                logger.info("event=switch_busy pid=%d source=%s", pid, source)
                return SwitchOutcome("busy", None, None)
            held.append(lk)

        prev: Optional[dict] = None
        try:
            raw = json.loads(deps.read_text(path))
        except FileNotFoundError:
            raw = None
        except Exception as e:
            logger.warning("event=record_unreadable pid=%d exc=%s", pid, type(e).__name__)
            return SwitchOutcome("refused_touched", None, None)
        if raw is not None:
            if not isinstance(raw, dict):
                logger.warning("event=record_unreadable pid=%d exc=NotAnObject", pid)
                return SwitchOutcome("refused_touched", None, None)
            try:
                same = _record_identity(raw) == ident
            except ValueError:
                same = False
            # A record of another identity in the same file is an earlier process that reused this pid.
            prev = raw if same else None
        if (prev is not None and touched(prev)) or (is_touched is not None and is_touched(ident)):
            logger.warning("event=switch_refused pid=%d source=%s reason=touched", pid, source)
            return SwitchOutcome("refused_touched", prev, None)

        excluded: List[str] = sorted(set(prev.get("excluded") or [])) if prev else []
        hold: Optional[str] = prev.get("hard_hold_until") if prev else None
        attempt = 0
        calls = 0
        res: Optional[SwitchResult] = None
        unswitchable: Optional[str] = None
        cur_mode, cur_target = mode, target
        last_mode, last_target = mode, target
        while True:
            attempt += 1
            t = deps.now()
            intent = _new_record(session, source=source, mode=cur_mode, target=cur_target, attempt=attempt, now=t,
                                 record_state="in_progress", excluded=excluded, hold=hold)
            try:
                deps.write_text(path, _dump(intent))
            except Exception as e:
                logger.error("event=intent_write_failed pid=%d attempt=%d exc=%s", pid, attempt, type(e).__name__)
                if attempt == 1:
                    return SwitchOutcome("intent_failed", None, None)
                break
            if on_intent is not None:
                on_intent(ident, t)
            if on_attempt is not None:
                on_attempt()
            logger.info("event=switch_start pid=%d source=%s mode=%s target=%s own=%s attempt=%d",
                        pid, source, cur_mode, cur_target.account_id, own.account_id, attempt)
            res = deps.switch(session, cur_target, cur_mode, driver=deps.make_driver(), procs=deps.procs,
                              timeouts=cfg.timeouts, continue_prompt=cfg.continue_prompt,
                              profiles_root=cfg.profiles_root)
            calls = attempt
            last_mode, last_target = cur_mode, cur_target
            _log_result(logger, session, source, cur_mode, cur_target, attempt, res)
            if res.state == "untouched":
                cls = classify_untouched(res.reason)
                if cls == "structural":
                    unswitchable = res.reason if res.reason is not None else "unknown"
                    logger.warning("event=unswitchable pid=%d reason=%s", pid, unswitchable)
                elif cls == "target":
                    excluded = sorted(set(excluded) | {cur_target.account_id})
                    if next_target is not None and attempt < profiles_count:
                        nxt = next_target(frozenset(excluded))
                        if nxt is not None:
                            cur_mode, cur_target = nxt
                            continue
                elif cur_mode == "hard":
                    hold = _iso(deps.now() + timedelta(seconds=cfg.cooldown_s))
                    logger.warning("event=hard_hold pid=%d until=%s reason=%s", pid, hold, res.reason)
            break

        assert res is not None
        # After a failed later intent, the final record describes the last call that actually ran.
        final = _new_record(session, source=source, mode=last_mode, target=last_target, attempt=calls,
                            now=deps.now(), record_state="final", excluded=excluded, hold=hold, result=res,
                            unswitchable=unswitchable)
        try:
            deps.write_text(path, _dump(final))
        except Exception as e:
            logger.error("event=record_write_failed pid=%d exc=%s", pid, type(e).__name__)
        if res.new_pid is not None:
            pv = post_verify(res.new_pid, last_target, deps.procs)
            if pv == "ok":
                logger.info("event=post_verify pid=%d new_pid=%d outcome=ok", pid, res.new_pid)
            else:
                logger.warning("event=post_verify pid=%d new_pid=%d outcome=%s", pid, res.new_pid, pv)
            final["post_verify"] = pv
            try:
                deps.write_text(path, _dump(final))
            except Exception as e:
                logger.error("event=record_write_failed pid=%d exc=%s", pid, type(e).__name__)
        return SwitchOutcome("done", final, res)
    finally:
        for lk in reversed(held):
            lk.release()


# --- logging ---------------------------------------------------------------------------------------------


def setup_logging(log_dir: str, stderr_is_tty: bool) -> logging.Logger:
    os.makedirs(log_dir, 0o700, exist_ok=True)
    logger = logging.getLogger("rotator")
    for h in list(logger.handlers):
        if getattr(h, "_rotator_owned", False):
            logger.removeHandler(h)
            h.close()
    logger.setLevel(logging.INFO)  # never DEBUG: rotator.switcher logs the pane handle at DEBUG
    fmt = logging.Formatter("%(asctime)s level=%(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime
    handlers: List[logging.Handler] = [
        logging.handlers.RotatingFileHandler(os.path.join(log_dir, LOG_FILENAME), maxBytes=LOG_MAX_BYTES,
                                             backupCount=LOG_BACKUPS, encoding="utf-8")]
    if stderr_is_tty:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        h._rotator_owned = True  # type: ignore[attr-defined]
        logger.addHandler(h)
    return logger


# --- usage cadence ---------------------------------------------------------------------------------------

USAGE_BACKOFF_MAX_S = 300.0
RATE_LIMITED_TOKEN = "http_429"


def rate_limited(u: AccountUsage) -> bool:
    """True when the fetch behind u got HTTP 429 (rotator.usage reports error "http_429", or
    "http_429;cache_<reason>" when no cache exists)."""
    e = u.error
    return isinstance(e, str) and (e == RATE_LIMITED_TOKEN or e.startswith(RATE_LIMITED_TOKEN + ";"))


class UsageCache:
    """Per-account usage refreshed at most once per interval (D4); shared by the daemon tick and
    status --watch. Thread-safe; never calls fetch while holding its lock."""

    def __init__(self, poll_s: float) -> None:
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[float, float, AccountUsage]] = {}

    def refresh(self, profiles: Sequence[Profile], *, fetch: Callable[[Profile], AccountUsage],
                monotonic: Callable[[], float], now: datetime, log: logging.Logger) -> List[str]:
        """Fetch every account whose interval elapsed; returns the account ids fetched, in order."""
        mono = monotonic()
        ids = {p.account_id for p in profiles}
        with self._lock:
            for aid in [a for a in self._entries if a not in ids]:
                del self._entries[aid]
        fetched: List[str] = []
        for p in profiles:
            with self._lock:
                entry = self._entries.get(p.account_id)
            if entry is not None and mono - entry[0] < entry[1]:
                continue
            stamp = monotonic()
            try:
                u = fetch(p)
            except Exception as e:
                log.error("event=fetch_error account=%s exc=%s", p.account_id, type(e).__name__)
                u = AccountUsage(account_id=p.account_id, source="unavailable", fetched_at=now,
                                 error="request_error;cache_missing")
            if rate_limited(u):
                prev = entry[1] if entry is not None else self._poll_s
                interval = max(self._poll_s, min(2 * prev, USAGE_BACKOFF_MAX_S))
                log.warning("event=usage_rate_limited account=%s next_s=%d", p.account_id, int(interval))
            else:
                interval = self._poll_s
            with self._lock:
                self._entries[p.account_id] = (stamp, interval, u)
            fetched.append(p.account_id)
        return fetched

    def seed(self, u: AccountUsage, stamp: float) -> None:
        with self._lock:
            self._entries[u.account_id] = (stamp, self._poll_s, u)

    def usages(self) -> Dict[str, AccountUsage]:
        with self._lock:
            return {aid: u for aid, (_, _, u) in self._entries.items()}

    def interval(self, account_id: str) -> Optional[float]:
        with self._lock:
            entry = self._entries.get(account_id)
            return entry[1] if entry is not None else None

    def summaries(self) -> List[dict]:
        with self._lock:
            return [usage_summary(u) for _, (_, _, u) in sorted(self._entries.items())]


# --- the daemon ------------------------------------------------------------------------------------------


class Daemon:
    """One rotator daemon: tick() is called by the run loop; workers report back under _lock."""

    def __init__(self, cfg: Config, deps: Deps, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._deps = deps
        self._log = logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._usage = UsageCache(cfg.usage_poll_s)
        self._inflight: Dict[str, InFlight] = {}
        self._inflight_handles: Dict[str, str] = {}
        self._records: Dict[Identity, dict] = {}
        # Never cleared by a record reload: a failed final write must not re-open a touched identity.
        self._touched: Set[Identity] = set()
        self._touched_at: Dict[str, datetime] = {}
        self._rows: List[dict] = []
        self._pointer: Tuple[Optional[str], Optional[str]] = (None, None)
        self._orphans_logged: Set[Identity] = set()
        self._hold_logged: Dict[str, str] = {}
        self._would: Dict[str, Tuple[str, str]] = {}

    def request_stop(self) -> None:
        self._stop.set()

    def stopping(self) -> bool:
        return self._stop.is_set()

    def _may_switch(self) -> bool:
        return self._cfg.auto_switch and not self._cfg.dry_run

    def _may_write_pointer(self) -> bool:
        return self._may_switch() and self._cfg.pointer_correction

    # -- tick ----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        deps, log = self._deps, self._log
        now = deps.now()
        try:
            profiles = deps.discover_profiles(self._cfg.profiles_root)
        except Exception as e:
            log.error("event=discover_error what=profiles exc=%s", type(e).__name__)
            self._write_snapshot()
            return
        self._refresh_usage(profiles, now)
        usages = self._usage.usages()
        self._pointer_step(profiles, usages, now)
        try:
            found = deps.discover_sessions(session_homes(profiles))
        except Exception as e:
            log.error("event=discover_error what=sessions exc=%s", type(e).__name__)
            self._write_snapshot()
            return
        self._handle_sessions(profiles, usages, found, now)
        self._write_snapshot()

    def _refresh_usage(self, profiles: Sequence[Profile], now: datetime) -> None:
        self._usage.refresh(profiles, fetch=self._deps.fetch_usage, monotonic=self._deps.monotonic, now=now,
                            log=self._log)

    def _pointer_step(self, profiles: Sequence[Profile], usages: Mapping[str, AccountUsage],
                      now: datetime) -> None:
        cfg, deps = self._cfg, self._deps
        try:
            text = deps.read_pointer(cfg.profiles_root)
            pp = profile_for_config_dir(profiles, text) if text else None
            pd = decide_pointer(pp, profiles, usages, now, config=cfg.policy_config())
            with self._lock:
                self._pointer = (pp.account_id if pp else None, pd.target.account_id if pd.target else None)
            if pd.target is not None and self._may_write_pointer():
                deps.write_pointer(pd.target, cfg.profiles_root)
                self._log.info("event=pointer_write account=%s", pd.target.account_id)
        except Exception as e:
            self._log.warning("event=pointer_error exc=%s", type(e).__name__)

    def _handle_sessions(self, profiles: Sequence[Profile], usages: Mapping[str, AccountUsage],
                         found: Sequence[Session], now: datetime) -> None:
        cfg, deps = self._cfg, self._deps
        records = load_records(cfg.state_dir, deps, self._log)
        mono = deps.monotonic()
        pc = cfg.policy_config()
        with self._lock:
            self._records = records
            self._log_orphans_locked()
            last_switch = self._last_switch_locked()
            self._mark_overruns_locked(mono)
            rows = []
            for s in found:
                own = profile_for_config_dir(profiles, s.config_dir)
                try:
                    rows.append(self._session_step_locked(s, own, profiles, usages, now, pc, last_switch, mono))
                except Exception as e:
                    self._log.error("event=session_error pid=%d exc=%s", s.pid, type(e).__name__)
                    rows.append(self._basic_row(s, own))
            self._rows = rows

    def _basic_row(self, s: Session, own: Optional[Profile]) -> dict:
        return {"pid": s.pid, "session_id": s.session_id, "status": s.status,
                "account_id": own.account_id if own is not None else None, "decision": None,
                "would_switch": None, "in_flight": False, "in_flight_pid": None, "stuck": False}

    def _last_switch_locked(self) -> Dict[str, datetime]:
        out: Dict[str, datetime] = {}
        for rec in self._records.values():
            if not touched(rec):
                continue
            t = _parse_iso(rec.get("updated_at"))
            sid = rec.get("session_id")
            if t is None or not isinstance(sid, str):
                continue
            if sid not in out or t > out[sid]:
                out[sid] = t
        for sid, t in self._touched_at.items():
            if sid not in out or t > out[sid]:
                out[sid] = t
        return out

    def _mark_overruns_locked(self, mono: float) -> None:
        for inf in self._inflight.values():
            if not inf.stuck and mono > inf.deadline:
                inf.stuck = True
                self._log.warning("event=worker_overrun pid=%d", inf.identity[0])

    def _log_orphans_locked(self) -> None:
        for ident, rec in self._records.items():
            if rec.get("record_state") != "in_progress" or ident in self._orphans_logged:
                continue
            owner = rec.get("owner_pid")
            alive = type(owner) is int and self._deps.pid_alive(owner)
            if not alive:
                self._log.warning("event=orphaned_intent pid=%d", ident[0])
                self._orphans_logged.add(ident)

    def _session_step_locked(self, s: Session, own: Optional[Profile], profiles: Sequence[Profile],
                             usages: Mapping[str, AccountUsage], now: datetime, pc: PolicyConfig,
                             last_switch: Mapping[str, datetime], mono: float) -> dict:
        row = self._basic_row(s, own)
        if own is None:
            return row
        if s.session_id in self._inflight:
            sid_owner: Optional[str] = s.session_id
        elif s.terminal_handle is not None:
            sid_owner = self._inflight_handles.get(s.terminal_handle)
        else:
            sid_owner = None
        if sid_owner is not None:
            inf = self._inflight[sid_owner]
            row.update(in_flight=True, in_flight_pid=inf.identity[0], stuck=inf.stuck)
            return row
        ident = identity_of(s)
        rec = self._records.get(ident)
        if ident in self._touched or (rec is not None and touched(rec)):
            return row
        if rec is not None and rec.get("unswitchable"):
            return row
        excluded = set(rec.get("excluded") or []) if rec is not None else set()
        candidates = [p for p in profiles if p.account_id == own.account_id or p.account_id not in excluded]
        ls = last_switch.get(s.session_id)
        d = decide(s, candidates, usages, now, config=pc, last_switch_at=ls)
        row["decision"] = {"action": d.action,
                           "target_account_id": d.target.account_id if d.target is not None else None,
                           "reason": d.reason}
        if d.action == "none" or d.target is None:
            self._would.pop(s.session_id, None)
            return row
        hold = _parse_iso(rec.get("hard_hold_until")) if rec is not None else None
        if d.action == "hard" and hold is not None and now < hold:
            if self._hold_logged.get(s.session_id) != rec["hard_hold_until"]:
                self._hold_logged[s.session_id] = rec["hard_hold_until"]
                self._log.warning("event=hard_hold_active pid=%d until=%s", s.pid, rec["hard_hold_until"])
            return row
        if not self._may_switch():
            pair = (d.action, d.target.account_id)
            row["would_switch"] = {"action": pair[0], "target_account_id": pair[1]}
            if self._would.get(s.session_id) != pair:
                self._would[s.session_id] = pair
                self._log.info("event=would_switch pid=%d action=%s target=%s", s.pid, pair[0], pair[1])
            return row
        if self._dispatch_locked(s, own, d.action, d.target, profiles, usages, pc, ls, mono):
            row.update(in_flight=True, in_flight_pid=s.pid)
        return row

    def _dispatch_locked(self, s: Session, own: Profile, mode: str, target: Profile,
                         profiles: Sequence[Profile], usages: Mapping[str, AccountUsage], pc: PolicyConfig,
                         ls: Optional[datetime], mono: float) -> bool:
        ident = identity_of(s)
        self._inflight[s.session_id] = InFlight(
            identity=ident, handle=s.terminal_handle,
            deadline=mono + switch_budget_s(self._cfg.timeouts) + LEASE_MARGIN_S, worker=None)
        if s.terminal_handle is not None:
            self._inflight_handles[s.terminal_handle] = s.session_id
        prev_touched_at = self._touched_at.get(s.session_id)
        profs, uses = list(profiles), dict(usages)

        def fn() -> None:
            self._worker(s, own, target, mode, profs, uses, pc, ls, prev_touched_at)

        try:
            w = self._deps.spawn(fn, "rotator-switch-%d" % s.pid)
        except Exception as e:
            self._clear_inflight_locked(s)
            self._log.error("event=spawn_error pid=%d exc=%s", s.pid, type(e).__name__)
            return False
        self._inflight[s.session_id].worker = w
        self._log.info("event=dispatch pid=%d mode=%s target=%s", s.pid, mode, target.account_id)
        return True

    def _clear_inflight_locked(self, s: Session) -> None:
        self._inflight.pop(s.session_id, None)
        if s.terminal_handle is not None and self._inflight_handles.get(s.terminal_handle) == s.session_id:
            self._inflight_handles.pop(s.terminal_handle, None)

    def _worker(self, s: Session, own: Profile, target: Profile, mode: str, profiles: List[Profile],
                usages: Dict[str, AccountUsage], pc: PolicyConfig, ls: Optional[datetime],
                prev_touched_at: Optional[datetime]) -> None:
        cfg, deps, log = self._cfg, self._deps, self._log
        ident = identity_of(s)
        sid = s.session_id
        intent_seen = [False]
        outcome: Optional[SwitchOutcome] = None
        err: Optional[str] = None

        def on_intent(i: Identity, t: datetime) -> None:
            with self._lock:
                self._touched.add(i)
                self._touched_at[sid] = t
                intent_seen[0] = True

        def on_attempt() -> None:
            with self._lock:
                inf = self._inflight.get(sid)
                if inf is not None:
                    inf.deadline = deps.monotonic() + switch_budget_s(cfg.timeouts) + LEASE_MARGIN_S

        def is_touched(i: Identity) -> bool:
            with self._lock:
                return i in self._touched

        def next_target(excluded: FrozenSet[str]) -> Optional[Tuple[str, Profile]]:
            if self.stopping():
                return None
            try:
                cands = [p for p in profiles if p.account_id == own.account_id or p.account_id not in excluded]
                # The dispatching decide's last_switch_at: this worker's own intent must not cool down its retry.
                d2 = decide(s, cands, usages, deps.now(), config=pc, last_switch_at=ls)
            except Exception as e:
                log.error("event=decide_error pid=%d exc=%s", s.pid, type(e).__name__)
                return None
            if d2.action == "none" or d2.target is None:
                return None
            return (d2.action, d2.target)

        try:
            outcome = run_switch(s, own, target, mode, cfg=cfg, deps=deps, logger=log, source="daemon",
                                 profiles_count=len(profiles), next_target=next_target, on_attempt=on_attempt,
                                 on_intent=on_intent, is_touched=is_touched)
        except Exception as e:
            err = type(e).__name__
            log.error("event=worker_error pid=%d exc=%s", s.pid, err)
            rec = _new_record(s, source="daemon", mode=mode, target=target, attempt=0, now=deps.now(),
                              record_state="final", excluded=[], hold=None)
            rec["worker_error"] = err
            rec["touched"] = True
            try:
                deps.write_text(record_path(cfg.state_dir, sid, s.pid), _dump(rec))
            except Exception as e2:
                log.error("event=record_write_failed pid=%d exc=%s", s.pid, type(e2).__name__)
        finally:
            with self._lock:
                if err is not None:
                    self._touched.add(ident)
                    if not intent_seen[0]:
                        self._touched_at[sid] = deps.now()
                elif (outcome is not None and outcome.status == "done" and outcome.record is not None
                      and not touched(outcome.record)):
                    self._touched.discard(ident)
                    if prev_touched_at is None:
                        self._touched_at.pop(sid, None)
                    else:
                        self._touched_at[sid] = prev_touched_at
                if outcome is not None and outcome.record is not None:
                    self._records[ident] = outcome.record
                self._clear_inflight_locked(s)
            self._write_snapshot()

    def drain(self) -> None:
        """Stop scheduling and wait for in-flight workers up to their remaining lease."""
        self._stop.set()
        with self._lock:
            flights = list(self._inflight.values())
        for inf in flights:
            if inf.worker is None:
                continue
            inf.worker.join(max(0.0, inf.deadline - self._deps.monotonic()))
            if inf.worker.is_alive():
                self._log.warning("event=worker_abandoned pid=%d", inf.identity[0])
        self._write_snapshot()

    # -- snapshot and state --------------------------------------------------------------------------------

    def _snapshot_locked(self) -> dict:
        cfg = self._cfg
        return {
            "schema": SNAPSHOT_SCHEMA,
            "written_at": _iso(self._deps.now()),
            "daemon_pid": os.getpid(),
            "flags": {"dry_run": cfg.dry_run, "auto_switch": cfg.auto_switch,
                      "pointer_correction": cfg.pointer_correction},
            "accounts": self._usage.summaries(),
            "pointer_account_id": self._pointer[0],
            "pointer_target_account_id": self._pointer[1],
            "sessions": [dict(r) for r in self._rows],
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _write_snapshot(self) -> None:
        with self._lock:
            try:
                doc = self._snapshot_locked()
                self._deps.write_text(os.path.join(self._cfg.state_dir, STATE_FILENAME), _dump(doc))
            except Exception as e:
                self._log.error("event=snapshot_write_failed exc=%s", type(e).__name__)

    def _load_state(self) -> None:
        cfg, deps = self._cfg, self._deps
        recs = load_records(cfg.state_dir, deps, self._log)
        with self._lock:
            self._records = recs
        try:
            snap = json.loads(deps.read_text(os.path.join(cfg.state_dir, STATE_FILENAME)))
        except Exception:
            snap = None
        if isinstance(snap, dict):
            now = deps.now()
            mono = deps.monotonic()
            for d in snap.get("accounts") or []:
                if not isinstance(d, dict):
                    continue
                u = usage_from_summary(d)
                if u is None:
                    continue
                age = (now - u.fetched_at).total_seconds()
                if 0 <= age < cfg.usage_poll_s:
                    self._usage.seed(u, mono - age)
        with self._lock:
            self._log_orphans_locked()


# --- run loop and real seams -----------------------------------------------------------------------------


def run_daemon(cfg: Config, deps: Deps) -> int:
    """Run the daemon until SIGTERM/SIGINT; 0 on a clean stop, 1 when another daemon holds the lock,
    2 when profiles_root is missing."""
    log = deps.make_logger()
    if not deps.isdir(cfg.profiles_root):
        log.error("event=profiles_root_missing path=%s", cfg.profiles_root)
        return 2
    lock_path = os.path.join(cfg.state_dir, LOCK_FILENAME)
    lk = deps.lock(lock_path)
    if lk is None:
        try:
            owner = deps.read_text(lock_path).strip()
        except Exception:
            owner = ""
        log.error("event=already_running another rotator daemon is running (pid %s)",
                  owner if owner.isdigit() else "?")
        return 1
    try:
        lk.write_owner(os.getpid())
        d = Daemon(cfg, deps, log)
        deps.install_signals(d.request_stop)
        d._load_state()
        log.info("event=daemon_start pid=%d dry_run=%s auto_switch=%s pointer_correction=%s",
                 os.getpid(), cfg.dry_run, cfg.auto_switch, cfg.pointer_correction)
        while not d.stopping():
            try:
                d.tick()
            except Exception as e:
                log.error("event=tick_error exc=%s", type(e).__name__)
            if deps.wait_stop(d._stop, cfg.session_poll_s):
                break
        d.drain()
        log.info("event=daemon_stop")
        return 0
    finally:
        lk.release()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_text_utf8(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _spawn(fn: Callable[[], None], name: str) -> threading.Thread:
    t = threading.Thread(target=fn, name=name, daemon=True)
    t.start()
    return t


def _wait_stop(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)


def _pid_alive(pid: object) -> bool:
    # pid 0 or a negative pid would signal a whole process group.
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _install_signals(handler: Callable[[], None]) -> None:
    signal.signal(signal.SIGTERM, lambda signum, frame: handler())
    signal.signal(signal.SIGINT, lambda signum, frame: handler())


def _make_logger() -> logging.Logger:
    return setup_logging(LOG_DIR_DEFAULT, sys.stderr.isatty())


def real_deps(cfg: Config) -> Deps:
    """The only constructor of real seams; building it touches nothing."""
    return Deps(discover_profiles=accounts.discover_profiles, fetch_usage=usage.fetch_usage,
                discover_sessions=sessions.discover_sessions, switch=switcher.switch_session,
                make_driver=switcher.OrcaCliDriver, procs=switcher.OsProcessProbe(),
                read_pointer=pointer.read_selected_host, write_pointer=pointer.set_selected_host,
                now=_utc_now, monotonic=time.monotonic, spawn=_spawn, wait_stop=_wait_stop, lock=try_lock,
                read_text=_read_text_utf8, write_text=atomic_write_text, list_dir=os.listdir,
                isdir=os.path.isdir, pid_alive=_pid_alive, install_signals=_install_signals,
                make_logger=_make_logger)
