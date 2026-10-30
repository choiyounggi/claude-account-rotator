"""Drive one Orca pane to restart a Claude Code session on another account."""
from __future__ import annotations

import glob
import json
import logging
import math
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Protocol, Tuple

from rotator.models import PROFILES_ROOT_DEFAULT, Profile, ReadText, Session

logger = logging.getLogger("rotator.switcher")

MODES = ("soft", "hard")
STEPS = ("preflight", "interrupt", "exit", "wait_exit", "relaunch", "wait_ready", "verify_account", "continue_prompt")
STATES = ("untouched", "interrupted", "exit_pending", "exited", "relaunched", "verified", "continued")
REASONS = ("no_terminal_handle", "terminal_handle_invalid", "no_argv", "relaunch_unbuildable", "same_account",
           "target_home_missing", "cwd_missing", "transcript_not_visible", "target_untrusted", "pid_not_alive",
           "pane_binding_unverified", "pane_not_claude", "registry_mismatch", "dialog_open", "not_idle",
           "send_refused", "interrupt_timeout", "screen_unreadable", "trust_dialog", "input_not_empty",
           "exit_timeout", "session_already_running", "ready_timeout", "launched_elsewhere",
           "account_unverified", "wrong_account", "prompt_blocked_permission", "prompt_unconfirmed")
DEFAULT_CONTINUE_PROMPT = "계정 전환으로 세션이 재시작됐어. 중단된 작업을 이어서 진행해줘."
MAX_PROMPT_CHARS = 800
ENV_BINARY = "/usr/bin/env"
PS_BINARY = "/bin/ps"
PS_TIMEOUT_S = 5.0
ORCA_BINARY_DEFAULT = "/Applications/Orca.app/Contents/Resources/bin/orca"
ORCA_CALL_TIMEOUT_S = 15.0
ORCA_SEND_TIMEOUT_S = 50.0
DELIVERIES = ("accepted", "refused", "unconfirmed")
STDERR_TAIL_CHARS = 2000
TRUST_MARKERS = ("Trust this directory?", "Yes, I trust this folder")
DIALOG_MARKERS = ("Do you want to proceed?",)
RULE_CHARS = frozenset("─━")
RULE_MIN_LEN = 10
BOTTOM_LINES = 15
PLACEHOLDER_RE = re.compile(r'^Try ".*"$')
CONFIG_DIR_RE = re.compile(r'(?:^| )CLAUDE_CONFIG_DIR=(.*?)(?= [A-Za-z_][A-Za-z0-9_]*=|$)')
HANDLE_ENV_RE = re.compile(r'(?:^| )ORCA_TERMINAL_HANDLE=(\S*)')
ENV_PRESENCE_KEYS = ("TMUX", "STY")
PRESENCE_RE = re.compile(r'(?:^| )(TMUX|STY)=')
PS_TABLE_RE = re.compile(r'^\s*(\d+)\s+(\d+)\s+(.*\S)\s*$')
MAX_ANCESTOR_HOPS = 16
SHELL_NAMES = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"})
LOGIN_NAMES = frozenset({"login"})
PANE_HOST_NAMES = frozenset({"orca-terminal-host"})
MULTIPLEXER_NAMES = frozenset({"tmux", "screen", "zellij", "abduco", "dtach", "ssh", "sshd", "mosh-server"})
BINDING_FAULTS = ("table_unavailable", "chain_broken", "multiplexer", "foreign_ancestor", "hop_limit",
                  "env_unreadable", "env_multiplexer", "handle_mismatch", "no_shell")
_RECOVERABLE_STATES = frozenset({"exited", "relaunched", "verified"})
_RELAUNCH_STATES = frozenset({"exit_pending", "exited", "relaunched", "verified", "continued"})
STATE_HINTS = {
    "untouched": "Nothing was typed; the session is unchanged.",
    "interrupted": "One Ctrl-C was sent, so the running turn may have been cancelled; Claude Code was still running in the pane and no text was typed.",
    "exit_pending": "/exit was submitted but the old process was still alive; if the pane returns to a shell prompt, run relaunch_command there.",
    "exited": "Claude Code exited, but what runs in the pane now is not confirmed and the relaunch line may already be typed there: never send relaunch_command automatically; look at the pane and run it by hand only at an empty shell prompt.",
    "relaunched": "A new Claude Code process was started but not verified; look at the pane (dialog, account) before typing anything.",
    "verified": "The session runs on the target account; the continue prompt was not confirmed: look at the pane before resending (request_id allows orca --retry-request).",
    "continued": "",
}

# Claude Code 2.1.296 option arity, frozen: an option missing here cannot be re-emitted safely.
_VALUE_OPTS = frozenset("--agent --agents --append-system-prompt --autocompact --debug-file --effort --environment --fallback-model --input-format --json-schema --max-budget-usd --model -n --name --output-format --permission-mode --permission-prompts --plugin-dir --plugin-url --remote-control-session-name-prefix --session-id --setting-sources --settings --system-prompt --system-prompt-snapshot".split())
_VARIADIC_OPTS = frozenset("--add-dir --allowedTools --allowed-tools --betas --disallowedTools --disallowed-tools --file --mcp-config --tools".split())
_OPTIONAL_OPTS = frozenset("--cloud -d --debug --from-pr --prompt-suggestions --remote-control -r --resume --teleport -w --worktree".split())
_BOOLEAN_OPTS = frozenset("--allow-dangerously-skip-permissions --ax-screen-reader --bg --background --bare --brief --chrome -c --continue --dangerously-skip-permissions --desktop --disable-slash-commands --exclude-dynamic-system-prompt-sections --fork-session --forward-subagent-text -h --help --ide --include-hook-events --include-partial-messages --no-chrome --no-session-persistence -p --print --replay-user-messages --restricted --safe-mode --strict-mcp-config --tmux --verbose -v --version".split())
_KNOWN_OPTS = _VALUE_OPTS | _VARIADIC_OPTS | _OPTIONAL_OPTS | _BOOLEAN_OPTS
_DROP_OPTS = frozenset("-r --resume -c --continue --session-id --fork-session -w --worktree".split())
_REFUSE_OPTS = frozenset("-p --print --cloud --environment --teleport --from-pr --desktop --bg --background -h --help -v --version".split())
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DIGITS_RE = re.compile(r"[0-9]+")
_DEFAULT_HOME_REL = "~/.claude"


@dataclass(frozen=True)
class Timeouts:
    """Per-wait budgets in seconds; every field finite and >= 0, ``poll_s`` > 0."""

    idle_s: float = 30.0
    exit_s: float = 30.0
    shell_s: float = 5.0
    ready_s: float = 60.0
    prompt_s: float = 15.0
    poll_s: float = 0.5

    def __post_init__(self) -> None:
        for name in ("idle_s", "exit_s", "shell_s", "ready_s", "prompt_s", "poll_s"):
            v = getattr(self, name)
            if type(v) is bool or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError("Timeouts.%s must be a finite number >= 0" % name)
        if not self.poll_s > 0:
            raise ValueError("Timeouts.poll_s must be > 0")


@dataclass(frozen=True)
class SendReceipt:
    delivery: str
    refused_reason: Optional[str] = None
    stages: Tuple[str, ...] = ()
    observation: Optional[str] = None
    request_id: Optional[str] = None
    raw: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.delivery not in DELIVERIES:
            raise ValueError("SendReceipt.delivery must be one of DELIVERIES")


@dataclass(frozen=True)
class WaitReceipt:
    satisfied: bool
    status: Optional[str] = None
    blocked_reason: Optional[str] = None
    raw: str = field(default="", repr=False)


@dataclass(frozen=True)
class ScreenReceipt:
    ok: bool
    tail: Tuple[str, ...] = field(default=(), repr=False)
    source: Optional[str] = None
    draft: Optional[str] = field(default=None, repr=False)
    agent_wait: Optional[Tuple[str, Optional[str]]] = None
    raw: str = field(default="", repr=False)


@dataclass(frozen=True)
class SwitchResult:
    """Outcome of one ``switch_session`` run; construct with keywords.

    ``recoverable`` means an operator can finish the switch by hand with ``relaunch_command``;
    it is never a licence to type that command automatically.
    """

    ok: bool
    step: str
    reason: Optional[str]
    state: str
    new_pid: Optional[int]
    relaunch_command: str = field(repr=False)
    recoverable: bool
    request_id: Optional[str] = None
    other_account_id: Optional[str] = None


@dataclass(frozen=True)
class ProcEnv:
    config_dir: Optional[str] = field(default=None, repr=False)
    terminal_handle: Optional[str] = field(default=None, repr=False)
    present: FrozenSet[str] = frozenset()


class TerminalDriver(Protocol):
    def send(self, handle: str, text: Optional[str], *, enter: bool, interrupt: bool) -> SendReceipt: ...

    def read_screen(self, handle: str) -> ScreenReceipt: ...

    def wait_for(self, handle: str, condition: str, timeout_ms: int) -> WaitReceipt: ...


class ProcessProbe(Protocol):
    def is_alive(self, pid: int) -> bool: ...

    def read_env(self, pid: int) -> Optional[ProcEnv]: ...

    def process_table(self) -> Mapping[int, Tuple[int, str]]:
        """pid -> (ppid, normalised name); raises OSError when the table cannot be read."""
        ...


def _wait_until(probe: Callable[[], Any], budget_s: float, poll_s: float,
                clock: Callable[[], float], sleep: Callable[[float], None]) -> Any:
    deadline = clock() + budget_s
    while True:
        value = probe()
        if value:
            return value
        now = clock()
        if now >= deadline:
            return None
        sleep(min(poll_s, deadline - now))
        if clock() >= deadline:
            return None


def switch_budget_s(timeouts: Timeouts) -> float:
    """Upper bound in seconds for one ``switch_session`` run with these timeouts."""
    return float(4 * ORCA_CALL_TIMEOUT_S + 3 * ORCA_SEND_TIMEOUT_S + timeouts.idle_s + timeouts.exit_s
                 + timeouts.shell_s + 5.0 + timeouts.ready_s + timeouts.prompt_s + 5 * PS_TIMEOUT_S)


# ---------------------------------------------------------------- relaunch line

def _q(s: str) -> str:
    r = shlex.quote(s)
    # An unquoted word starting with "=" is expanded by zsh (=cmd).
    return "'" + r + "'" if r.startswith("=") else r


def _is_value(tok: str) -> bool:
    return not (len(tok) > 1 and tok.startswith("-"))


def _preserved_args(argv: Tuple[str, ...]) -> List[str]:
    kept: List[str] = []
    rest = list(argv[1:])
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            break
        if tok.startswith("--") and "=" in tok:
            name = tok.split("=", 1)[0]
            if name in _REFUSE_OPTS or name not in _KNOWN_OPTS or (name in _BOOLEAN_OPTS and name != "--tmux"):
                raise ValueError("relaunch: option not re-emittable")
            if name not in _DROP_OPTS:
                kept.append(tok)
            i += 1
            continue
        if len(tok) > 1 and tok.startswith("-"):
            if tok in _REFUSE_OPTS or tok not in _KNOWN_OPTS:
                raise ValueError("relaunch: option not re-emittable")
            j = i + 1
            if tok in _VALUE_OPTS:
                if j >= len(rest):
                    raise ValueError("relaunch: option value missing")
                j += 1
            elif tok in _OPTIONAL_OPTS:
                if j < len(rest) and _is_value(rest[j]):
                    j += 1
            elif tok in _VARIADIC_OPTS:
                while j < len(rest) and _is_value(rest[j]):
                    j += 1
            if tok not in _DROP_OPTS:
                kept.extend(rest[i:j])
            i = j
            continue
        i += 1
    return kept


def _valid_handle(h: object) -> bool:
    return (isinstance(h, str) and h != "" and not h.startswith("-")
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in h))


def build_relaunch_command(session: Session, target_home: str) -> str:
    """The one shell line typed after /exit; ValueError when the session cannot be re-emitted safely."""
    if session.argv == ():
        raise ValueError("relaunch: empty argv")
    if os.path.basename(session.argv[0]) != "claude":
        raise ValueError("relaunch: argv[0] is not claude")
    if _SESSION_ID_RE.fullmatch(session.session_id) is None:
        raise ValueError("relaunch: session id not allowed")
    if not os.path.isabs(session.cwd) or not os.path.isabs(target_home):
        raise ValueError("relaunch: cwd and target home must be absolute")
    kept = _preserved_args(session.argv)
    for tok in [session.cwd, target_home, session.argv[0], session.session_id] + kept:
        if any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in tok):
            raise ValueError("relaunch: control character or backslash")
    # target_home stays verbatim: Claude Code derives the Keychain item name from the exact string.
    return ("cd " + _q(session.cwd) + " && " + ENV_BINARY + " CLAUDE_CONFIG_DIR=" + _q(target_home)
            + " ORCA_CLAUDE_INJECTED_CONFIG_DIR=" + _q(target_home) + " " + _q(session.argv[0])
            + " --resume " + _q(session.session_id) + "".join(" " + _q(t) for t in kept))


# ---------------------------------------------------------------- default seams

def _run_subprocess(argv: List[str], timeout_s: float) -> Tuple[int, str, str]:
    try:
        cp = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout_s, shell=False, check=False, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError:
        return 127, "", "not executable"
    return cp.returncode, cp.stdout or "", (cp.stderr or "")[-STDERR_TAIL_CHARS:]


def _raw(out: str, err: str) -> str:
    return out + ("\n[stderr] " + err if err else "")


def _load(out: str) -> Optional[dict]:
    try:
        obj = json.loads(out)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _result_obj(obj: Optional[dict]) -> Optional[dict]:
    if obj is None:
        return None
    res = obj.get("result")
    return res if isinstance(res, dict) else obj


def _str_or_none(v: Any) -> Optional[str]:
    return v if isinstance(v, str) else None


def _parse_send(rc: int, out: str, err: str) -> SendReceipt:
    obj = _load(out)
    send = None
    if obj is not None:
        res = obj.get("result")
        send = res.get("send") if isinstance(res, dict) else obj.get("send")
        send = send if isinstance(send, dict) else None
    stages: Tuple[str, ...] = ()
    observation = request_id = None
    p = send.get("prompt") if send else None
    if isinstance(p, dict):
        stages = tuple(s for s in p["stages"] if isinstance(s, str)) if isinstance(p.get("stages"), list) else ()
        observation = _str_or_none(p.get("observation"))
        request_id = _str_or_none(p.get("requestId"))
    raw = _raw(out, err)
    if rc == 127:
        delivery, refused = "refused", "not_executed"
    elif send is not None and send.get("accepted") is False:
        delivery = "refused"
        refused = send["refusedReason"] if isinstance(send.get("refusedReason"), str) else "refused"
    elif rc == 0 and obj is not None and obj.get("ok") is not False and send is not None \
            and send.get("accepted") is True:
        delivery, refused = "accepted", None
    else:
        delivery, refused = "unconfirmed", None
    return SendReceipt(delivery=delivery, refused_reason=refused, stages=stages, observation=observation,
                       request_id=request_id, raw=raw)


def _parse_wait(rc: int, out: str, err: str) -> WaitReceipt:
    res = _result_obj(_load(out))
    w = res.get("wait") if res else None
    w = w if isinstance(w, dict) else {}
    return WaitReceipt(satisfied=rc == 0 and w.get("satisfied") is True, status=_str_or_none(w.get("status")),
                       blocked_reason=_str_or_none(w.get("blockedReason")), raw=_raw(out, err))


def _parse_screen(rc: int, out: str, err: str) -> ScreenReceipt:
    res = _result_obj(_load(out))
    term = res.get("terminal") if res else None
    ok = rc == 0 and isinstance(term, dict)
    term = term if isinstance(term, dict) else {}
    tail = tuple(s for s in term["tail"] if isinstance(s, str)) if isinstance(term.get("tail"), list) else ()
    aw = term.get("agentWait")
    agent_wait = (aw["source"], _str_or_none(aw.get("reason"))) \
        if isinstance(aw, dict) and isinstance(aw.get("source"), str) else None
    return ScreenReceipt(ok=ok, tail=tail, source=_str_or_none(term.get("source")),
                         draft=_str_or_none(term.get("draft")), agent_wait=agent_wait, raw=_raw(out, err))


class OrcaCliDriver:
    """TerminalDriver over the ``orca terminal`` CLI. Logs nothing: its output may hold pane text."""

    def __init__(self, binary: str = ORCA_BINARY_DEFAULT,
                 run: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None) -> None:
        self._binary = binary
        self._run = _run_subprocess if run is None else run

    def send(self, handle: str, text: Optional[str], *, enter: bool, interrupt: bool) -> SendReceipt:
        if not _valid_handle(handle):
            raise ValueError("invalid terminal handle")
        if text is not None and (text == "" or text.startswith("-")):
            raise ValueError("text would be parsed as an orca option")
        argv = [self._binary, "terminal", "send", "--terminal", handle]
        if text is not None:
            argv += ["--text", text]
        if enter:
            argv.append("--enter")
        if interrupt:
            argv.append("--interrupt")
        argv.append("--json")
        timeout = ORCA_SEND_TIMEOUT_S if text is not None else ORCA_CALL_TIMEOUT_S
        return _parse_send(*self._run(argv, timeout))

    def read_screen(self, handle: str) -> ScreenReceipt:
        if not _valid_handle(handle):
            raise ValueError("invalid terminal handle")
        argv = [self._binary, "terminal", "read", "--terminal", handle, "--screen", "--json"]
        return _parse_screen(*self._run(argv, ORCA_CALL_TIMEOUT_S))

    def wait_for(self, handle: str, condition: str, timeout_ms: int) -> WaitReceipt:
        if not _valid_handle(handle):
            raise ValueError("invalid terminal handle")
        argv = [self._binary, "terminal", "wait", "--terminal", handle, "--for", condition,
                "--timeout-ms", str(timeout_ms), "--json"]
        return _parse_wait(*self._run(argv, timeout_ms / 1000 + 5.0))


def _proc_name(comm: str) -> str:
    name = comm.rsplit("/", 1)[-1]
    name = name[1:] if name.startswith("-") else name
    return name.lower()


def _parse_env(line: str) -> ProcEnv:
    cfg = [m.group(1) for m in CONFIG_DIR_RE.finditer(line)]
    hdl = [m.group(1) for m in HANDLE_ENV_RE.finditer(line)]
    present = frozenset(m.group(1) for m in PRESENCE_RE.finditer(line))
    # ps prints the environment after argv, and the relaunch sets CLAUDE_CONFIG_DIR there: the last match is the env.
    return ProcEnv(config_dir=cfg[-1] if cfg else None, terminal_handle=hdl[-1] if hdl else None, present=present)


def _parse_process_table(stdout: str) -> Dict[int, Tuple[int, str]]:
    table: Dict[int, Tuple[int, str]] = {}
    for line in stdout.splitlines():
        m = PS_TABLE_RE.match(line)
        if m is None:
            continue
        table[int(m.group(1))] = (int(m.group(2)), _proc_name(m.group(3)))
    return table


class OsProcessProbe:
    """ProcessProbe over signal 0 and ``ps``. The ``ps -E`` line holds secret env values and stays local."""

    def __init__(self, kill: Optional[Callable[[int, int], None]] = None,
                 run: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None) -> None:
        self._kill = os.kill if kill is None else kill
        self._run = _run_subprocess if run is None else run

    def is_alive(self, pid: int) -> bool:
        try:
            self._kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def read_env(self, pid: int) -> Optional[ProcEnv]:
        rc, out, _ = self._run([PS_BINARY, "-E", "-ww", "-o", "command=", "-p", str(pid)], PS_TIMEOUT_S)
        if rc != 0:
            return None
        line = out[:-1] if out.endswith("\n") else out
        return _parse_env(line)

    def process_table(self) -> Mapping[int, Tuple[int, str]]:
        rc, out, _ = self._run([PS_BINARY, "-A", "-o", "pid=,ppid=,comm="], PS_TIMEOUT_S)
        if rc != 0:
            raise OSError("process table unavailable")
        table = _parse_process_table(out)
        if not table:
            raise OSError("process table empty")
        return table


# ---------------------------------------------------------------- pure pane guards

def _is_rule(line: str) -> bool:
    s = line.strip()
    return len(s) >= RULE_MIN_LEN and set(s) <= RULE_CHARS


def classify_screen(receipt: ScreenReceipt) -> Optional[str]:
    """None when the input box is provably empty, else the reason token that forbids typing."""
    if not receipt.ok or receipt.source != "screen":
        return "screen_unreadable"
    lines = list(receipt.tail)
    rules = [i for i, line in enumerate(lines) if _is_rule(line)]
    if len(rules) >= 2:
        bottom = lines[rules[-2] + 1:]
    else:
        bottom = [line for line in lines if line.strip()][-BOTTOM_LINES:]
    text = "\n".join(bottom)
    if any(m in text for m in TRUST_MARKERS):
        return "trust_dialog"
    if any(m in text for m in DIALOG_MARKERS):
        return "dialog_open"
    if len(rules) < 2:
        return "screen_unreadable"
    s = " ".join(lines[rules[-2] + 1:rules[-1]]).replace(" ", " ").replace("│", "").strip()
    if s[:1] in ("❯", ">"):
        s = s[1:].strip()
    if s == "" or PLACEHOLDER_RE.match(s):
        return None
    return "input_not_empty"


def check_pane_binding(pid: int, handle: str, table: Mapping[int, Tuple[int, str]],
                       env: Optional[ProcEnv]) -> Optional[str]:
    """None when the pane named by ``handle`` directly hosts ``pid``, else one BINDING_FAULTS token."""
    if pid not in table:
        return "chain_broken"
    cur = table[pid][0]
    verified = False
    shells = 0
    for _ in range(MAX_ANCESTOR_HOPS):
        if cur <= 1 or cur not in table:
            return "chain_broken"
        ppid, comm = table[cur]
        name = _proc_name(comm)
        if name in MULTIPLEXER_NAMES:
            return "multiplexer"
        if name in SHELL_NAMES or name in LOGIN_NAMES:
            shells += name in SHELL_NAMES
            cur = ppid
            continue
        if name in PANE_HOST_NAMES:
            verified = True
            break
        return "foreign_ancestor"
    if not verified:
        return "hop_limit"
    if shells == 0:
        # After /exit a shell must regain the pane's foreground before the relaunch line is typed.
        return "no_shell"
    if env is None:
        return "env_unreadable"
    if env.present & frozenset(ENV_PRESENCE_KEYS):
        return "env_multiplexer"
    if env.terminal_handle != handle:
        return "handle_mismatch"
    return None


# ---------------------------------------------------------------- the switch sequence

def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise OSError("file is not valid UTF-8") from None


def _read_registry(read_text: ReadText, path: str) -> Optional[dict]:
    try:
        obj = json.loads(read_text(path))
    except (OSError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _registry_path(session: Session) -> str:
    return os.path.join(session.config_dir, "sessions", "%d.json" % session.pid)


class _GuardedDriver:
    """Fail-closed view of an injected driver: a raising or malformed call reads as the default seam's failure."""

    def __init__(self, driver: TerminalDriver) -> None:
        self._driver = driver

    def send(self, handle: str, text: Optional[str], *, enter: bool, interrupt: bool) -> SendReceipt:
        ok, r = _guarded(self._driver.send, handle, text, enter=enter, interrupt=interrupt)
        # Delivery unknown: the step's own proof decides, exactly as for an unconfirmed receipt.
        return r if ok and isinstance(r, SendReceipt) else SendReceipt("unconfirmed")

    def read_screen(self, handle: str) -> ScreenReceipt:
        ok, r = _guarded(self._driver.read_screen, handle)
        return r if ok and isinstance(r, ScreenReceipt) else ScreenReceipt(ok=False)

    def wait_for(self, handle: str, condition: str, timeout_ms: int) -> WaitReceipt:
        ok, r = _guarded(self._driver.wait_for, handle, condition, timeout_ms)
        return r if ok and isinstance(r, WaitReceipt) else WaitReceipt(False)


def _guarded_read_text(read_text: ReadText) -> ReadText:
    def read(path: str) -> str:
        ok, r = _guarded(read_text, path)
        if not ok or not isinstance(r, str):
            raise OSError("unreadable")
        return r

    return read


def _guarded_glob(glob_paths: Callable[[str], List[str]]) -> Callable[[str], List[str]]:
    # glob.glob itself reports an unreadable directory as no match.
    def listing(pattern: str) -> List[str]:
        ok, r = _guarded(lambda: [p for p in glob_paths(pattern) if isinstance(p, str)])
        return r if ok else []

    return listing


def _guarded_isdir(isdir: Callable[[str], bool]) -> Callable[[str], bool]:
    def check(path: str) -> bool:
        ok, r = _guarded(isdir, path)
        return ok and r is True

    return check


class _Run:
    def __init__(self, session: Session, target: Profile, mode: str, driver: TerminalDriver,
                 procs: ProcessProbe, read_text: ReadText, glob_paths: Callable[[str], List[str]],
                 isdir: Callable[[str], bool], clock: Callable[[], float], sleep: Callable[[float], None],
                 timeouts: Timeouts, prompt: str, profiles_root: str) -> None:
        self.session = session
        self.target = target
        self.mode = mode
        self.driver = driver
        self.procs = procs
        self.read_text = read_text
        self.glob_paths = glob_paths
        self.isdir = isdir
        self.clock = clock
        self.sleep = sleep
        self.timeouts = timeouts
        self.prompt = prompt
        self.profiles_root = profiles_root
        self.line = ""
        self.state = "untouched"
        self.status: Optional[str] = None
        self.new_pid: Optional[int] = None


def _guarded(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Tuple[bool, Any]:
    try:
        return True, fn(*args, **kwargs)
    except Exception as exc:
        # Class name only: exception messages may carry paths.
        logger.debug("switch seam error=%s", type(exc).__name__)
        return False, None


def _finish(run: _Run, step: str, reason: Optional[str], state: str, *, new_pid: Optional[int] = None,
            request_id: Optional[str] = None, other_account_id: Optional[str] = None) -> SwitchResult:
    res = SwitchResult(ok=reason is None and state == "continued", step=step, reason=reason, state=state,
                       new_pid=new_pid, relaunch_command=run.line if state in _RELAUNCH_STATES else "",
                       recoverable=state in _RECOVERABLE_STATES, request_id=request_id,
                       other_account_id=other_account_id)
    logger.info("switch step=%s pid=%d reason=%s state=%s", step, run.session.pid, reason or "ok", state)
    return res


def _step_ok(run: _Run, step: str) -> None:
    logger.info("switch step=%s pid=%d reason=%s state=%s", step, run.session.pid, "ok", run.state)


def _binding_fault(run: _Run) -> Optional[str]:
    ok, table = _guarded(run.procs.process_table)
    if not ok or not isinstance(table, Mapping):
        return "table_unavailable"
    ok, env = _guarded(run.procs.read_env, run.session.pid)
    env = env if ok and isinstance(env, ProcEnv) else None
    ok, fault = _guarded(check_pane_binding, run.session.pid, run.session.terminal_handle, table, env)
    fault = fault if ok else "table_unavailable"
    if fault is not None:
        logger.debug("switch binding fault=%s", fault)
    return fault


def _target_trusted(run: _Run) -> bool:
    s = run.session
    cfg = _read_registry(run.read_text, os.path.join(run.target.home, ".claude.json"))
    projects = cfg.get("projects") if cfg else None
    if not isinstance(projects, dict):
        return False
    # Claude Code accepts a trusted ancestor up to the git root.
    d = s.cwd
    dirs: List[str] = []
    found = False
    for _ in range(64):
        dirs.append(d)
        if run.glob_paths(os.path.join(glob.escape(d), ".git")):
            found = True
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    candidates = dirs if found else [s.cwd]
    for c in candidates:
        entry = projects.get(c)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _preflight(run: _Run) -> Optional[SwitchResult]:
    s, target = run.session, run.target

    def fail(reason: str) -> SwitchResult:
        return _finish(run, "preflight", reason, "untouched")

    if s.terminal_handle is None:
        return fail("no_terminal_handle")
    if not _valid_handle(s.terminal_handle):
        return fail("terminal_handle_invalid")
    logger.debug("switch handle=%s", s.terminal_handle)
    if s.argv == ():
        return fail("no_argv")
    try:
        run.line = build_relaunch_command(s, target.home)
    except ValueError:
        return fail("relaunch_unbuildable")
    if os.path.normpath(target.home) == os.path.normpath(s.config_dir):
        return fail("same_account")
    if not run.isdir(target.home):
        return fail("target_home_missing")
    if not run.isdir(s.cwd):
        return fail("cwd_missing")
    if not run.glob_paths(os.path.join(glob.escape(target.home), "projects", "*",
                                       glob.escape(s.session_id) + ".jsonl")):
        return fail("transcript_not_visible")
    if not _target_trusted(run):
        return fail("target_untrusted")
    ok, alive = _guarded(run.procs.is_alive, s.pid)
    if not ok or not alive:
        return fail("pid_not_alive")
    if _binding_fault(run) is not None:
        return fail("pane_binding_unverified")
    reg = _read_registry(run.read_text, _registry_path(s))
    if reg is None or reg.get("status") not in ("idle", "busy", "waiting"):
        return fail("pane_not_claude")
    if reg.get("pid") != s.pid or reg.get("sessionId") != s.session_id:
        return fail("registry_mismatch")
    if reg["status"] == "waiting":
        return fail("dialog_open")
    if run.mode == "soft" and reg["status"] == "busy":
        return fail("not_idle")
    run.status = reg["status"]
    _step_ok(run, "preflight")
    return None


def _screen_verdict(run: _Run) -> Optional[str]:
    rec = run.driver.read_screen(run.session.terminal_handle)
    logger.debug("switch screen draft=%s agent_wait=%s", rec.draft is not None, rec.agent_wait is not None)
    return classify_screen(rec)


def _interrupt(run: _Run) -> Optional[SwitchResult]:
    h, t = run.session.terminal_handle, run.timeouts
    v = _screen_verdict(run)
    if v:
        # A Ctrl-C into a non-empty input box clears the user's text instead of interrupting.
        return _finish(run, "interrupt", v, "untouched")
    r = run.driver.send(h, None, enter=False, interrupt=True)
    if r.delivery == "refused":
        return _finish(run, "interrupt", "send_refused", "untouched")
    run.state = "interrupted"

    def probe() -> Optional[str]:
        ok, alive = _guarded(run.procs.is_alive, run.session.pid)
        if not ok:
            return "error"
        if not alive:
            return "dead"
        reg = _read_registry(run.read_text, _registry_path(run.session))
        status = reg.get("status") if reg else None
        if status in ("idle", "waiting"):
            return status
        return None

    out = _wait_until(probe, t.idle_s, t.poll_s, run.clock, run.sleep)
    if out == "dead":
        # Not caused by /exit: the pane foreground is unknown, so nothing is typed.
        return _finish(run, "interrupt", "pid_not_alive", "exited")
    if out == "waiting":
        return _finish(run, "interrupt", "dialog_open", "interrupted")
    if out == "idle":
        _step_ok(run, "interrupt")
        return None
    return _finish(run, "interrupt", "interrupt_timeout", "interrupted")


def _exit(run: _Run) -> Optional[SwitchResult]:
    h = run.session.terminal_handle
    v = _screen_verdict(run)
    if v:
        return _finish(run, "exit", v, run.state)
    reg = _read_registry(run.read_text, _registry_path(run.session))
    status = reg.get("status") if reg else None
    if status == "waiting":
        return _finish(run, "exit", "dialog_open", run.state)
    if status == "busy":
        return _finish(run, "exit", "not_idle", run.state)
    if status != "idle":
        return _finish(run, "exit", "pane_not_claude", run.state)
    if _binding_fault(run) is not None:
        return _finish(run, "exit", "pane_binding_unverified", run.state)
    r = run.driver.send(h, "/exit", enter=True, interrupt=False)
    if r.delivery == "refused":
        return _finish(run, "exit", "send_refused", run.state)
    _step_ok(run, "exit")
    return None


def _wait_exit(run: _Run) -> Optional[SwitchResult]:
    h, t = run.session.terminal_handle, run.timeouts

    def gone() -> bool:
        ok, alive = _guarded(run.procs.is_alive, run.session.pid)
        return ok and not alive  # a raising probe is no proof of death

    dead = _wait_until(gone, t.exit_s, t.poll_s, run.clock, run.sleep)
    w = run.driver.wait_for(h, "exit", int(t.shell_s * 1000))
    logger.debug("switch exit settle satisfied=%s", w.satisfied)
    if not dead and not gone():
        return _finish(run, "wait_exit", "exit_timeout", "exit_pending")
    run.state = "exited"
    _step_ok(run, "wait_exit")
    return None


def _after_preflight(run: _Run) -> SwitchResult:
    if run.mode == "hard" and run.status == "busy":
        res = _interrupt(run)
        if res is not None:
            return res
    res = _exit(run)
    if res is not None:
        return res
    res = _wait_exit(run)
    if res is not None:
        return res
    return _relaunch_onward(run)


def _entry_pid(path: str) -> Optional[int]:
    name = os.path.basename(path)
    if not name.endswith(".json"):
        return None
    stem = name[:-5]
    return int(stem) if _DIGITS_RE.fullmatch(stem) else None


def _default_home() -> str:
    return os.path.expanduser(_DEFAULT_HOME_REL)


def _home_of(path: str) -> str:
    return os.path.dirname(os.path.dirname(path))


def _account_of(run: _Run, path: str) -> Optional[str]:
    home = os.path.normpath(_home_of(path))
    if home == os.path.normpath(run.target.home):
        return None
    if home == os.path.normpath(_default_home()):
        return "default"
    return os.path.basename(os.path.dirname(home))


def _pick(entries: List[Tuple[str, dict]]) -> Tuple[str, dict]:
    def key(item: Tuple[str, dict]) -> Tuple[float, int]:
        started = item[1].get("startedAt")
        started = started if isinstance(started, (int, float)) and not isinstance(started, bool) else 0
        return (started, item[1]["pid"])

    return max(entries, key=key)


def _all_registry_paths(run: _Run) -> List[str]:
    paths = set()
    for p in (os.path.join(glob.escape(run.target.home), "sessions", "*.json"),
              os.path.join(glob.escape(run.profiles_root), "*", "home", "sessions", "*.json"),
              os.path.join(glob.escape(_default_home()), "sessions", "*.json")):
        paths.update(run.glob_paths(p))
    return sorted(paths)


def _matching_entry(run: _Run, path: str, *, exclude: FrozenSet[int], dead_is_alive: bool) -> Optional[dict]:
    pid = _entry_pid(path)
    if pid is None or pid == run.session.pid or pid in exclude:
        return None
    reg = _read_registry(run.read_text, path)
    if reg is None or reg.get("pid") != pid or reg.get("sessionId") != run.session.session_id:
        return None
    ok, alive = _guarded(run.procs.is_alive, pid)
    alive = alive if ok else dead_is_alive
    return reg if alive else None


def _relaunch_onward(run: _Run) -> SwitchResult:
    h, t = run.session.terminal_handle, run.timeouts
    paths = _all_registry_paths(run)
    snapshot = frozenset(p for p in map(_entry_pid, paths) if p is not None)
    live = [(p, e) for p in paths
            for e in [_matching_entry(run, p, exclude=frozenset(), dead_is_alive=True)] if e is not None]
    if live:
        # Something else already resumed this session; the line would land in its input box.
        p, e = _pick(live)
        return _finish(run, "relaunch", "session_already_running", "relaunched", new_pid=e["pid"],
                       other_account_id=_account_of(run, p))
    r = run.driver.send(h, run.line, enter=True, interrupt=False)
    if r.delivery == "refused":
        return _finish(run, "relaunch", "send_refused", "exited")
    _step_ok(run, "relaunch")

    target_glob = os.path.join(glob.escape(run.target.home), "sessions", "*.json")
    seen: List[int] = []

    def probe() -> Optional[Tuple[str, int]]:
        found = [(p, e) for p in run.glob_paths(target_glob)
                 for e in [_matching_entry(run, p, exclude=snapshot, dead_is_alive=False)] if e is not None]
        if not found:
            return None
        _, e = _pick(found)
        seen.append(e["pid"])
        if e.get("status") == "idle":
            return ("ready", e["pid"])
        if e.get("status") in ("waiting", "shell"):
            return ("dialog", e["pid"])
        return None

    out = _wait_until(probe, t.ready_s, t.poll_s, run.clock, run.sleep)
    if out is not None and out[0] == "dialog":
        v = _screen_verdict(run)
        return _finish(run, "wait_ready", "trust_dialog" if v == "trust_dialog" else "dialog_open", "relaunched",
                       new_pid=out[1])
    if out is None:
        target_home = os.path.normpath(run.target.home)
        others = [(p, e) for p in _all_registry_paths(run) if os.path.normpath(_home_of(p)) != target_home
                  for e in [_matching_entry(run, p, exclude=snapshot, dead_is_alive=False)] if e is not None]
        if others:
            p, e = _pick(others)
            return _finish(run, "wait_ready", "launched_elsewhere", "relaunched", new_pid=e["pid"],
                           other_account_id=_account_of(run, p))
        v = _screen_verdict(run)
        if v == "trust_dialog":
            return _finish(run, "wait_ready", "trust_dialog", "relaunched", new_pid=seen[-1] if seen else None)
        if seen:
            return _finish(run, "wait_ready", "ready_timeout", "relaunched", new_pid=seen[-1])
        return _finish(run, "wait_ready", "ready_timeout", "exited")

    run.new_pid = out[1]
    run.state = "relaunched"
    _step_ok(run, "wait_ready")

    ok, env = _guarded(run.procs.read_env, run.new_pid)
    env = env if ok and isinstance(env, ProcEnv) else None
    if env is None or not isinstance(env.config_dir, str):
        return _finish(run, "verify_account", "account_unverified", "relaunched", new_pid=run.new_pid)
    if os.path.normpath(env.config_dir) != os.path.normpath(run.target.home):
        return _finish(run, "verify_account", "wrong_account", "relaunched", new_pid=run.new_pid)
    run.state = "verified"
    _step_ok(run, "verify_account")
    return _continue_prompt(run)


def _continue_prompt(run: _Run) -> SwitchResult:
    h, t = run.session.terminal_handle, run.timeouts
    v = _screen_verdict(run)
    if v:
        return _finish(run, "continue_prompt", v, "verified", new_pid=run.new_pid)
    r = run.driver.send(h, run.prompt, enter=True, interrupt=False)
    rid = r.request_id

    def fail(reason: str) -> SwitchResult:
        return _finish(run, "continue_prompt", reason, "verified", new_pid=run.new_pid, request_id=rid)

    if r.delivery == "refused":
        return fail("send_refused")
    accepted = r.delivery == "accepted"
    if accepted and r.observation == "permission":
        return fail("prompt_blocked_permission")
    if accepted and r.observation == "incarnation_replaced":
        return fail("prompt_unconfirmed")
    stages = r.stages if isinstance(r.stages, tuple) else ()
    if not (accepted and "turn_started" in stages):
        reg_path = os.path.join(run.target.home, "sessions", "%d.json" % run.new_pid)

        def busy() -> bool:
            reg = _read_registry(run.read_text, reg_path)
            return reg is not None and reg.get("status") == "busy"

        if not _wait_until(busy, t.prompt_s, t.poll_s, run.clock, run.sleep):
            return fail("prompt_unconfirmed")
    run.state = "continued"
    return _finish(run, "continue_prompt", None, "continued", new_pid=run.new_pid, request_id=rid)


def switch_session(session: Session, target: Profile, mode: str, *, driver: TerminalDriver,
                   procs: Optional[ProcessProbe] = None, read_text: Optional[ReadText] = None,
                   glob_paths: Optional[Callable[[str], List[str]]] = None,
                   isdir: Optional[Callable[[str], bool]] = None,
                   clock: Optional[Callable[[], float]] = None,
                   sleep: Optional[Callable[[float], None]] = None,
                   timeouts: Optional[Timeouts] = None,
                   continue_prompt: str = DEFAULT_CONTINUE_PROMPT,
                   profiles_root: Optional[str] = None) -> SwitchResult:
    """Restart ``session`` in its Orca pane on ``target``'s account folder.

    ValueError only for caller bugs (mode, continue_prompt, timeouts), raised before any step.
    Every other outcome is a returned SwitchResult; act on ok/state/reason/recoverable, never on hint text.
    No exception from an injected seam escapes: driver, process probe, read_text, glob_paths and isdir
    failures are logged by class name only and read as that seam's failure value (an unconfirmed send,
    an unreadable screen or file, an unsatisfied wait, an empty listing, a missing directory, an
    unverifiable process), so the run ends fail-closed with the state of the step where it happened.
    """
    if mode not in MODES:
        raise ValueError("mode must be one of MODES")
    if (not isinstance(continue_prompt, str) or continue_prompt.strip() == ""
            or len(continue_prompt) > MAX_PROMPT_CHARS
            or any(ord(c) < 32 or ord(c) == 127 for c in continue_prompt)
            or continue_prompt.startswith("-") or continue_prompt.startswith("/")):
        raise ValueError("continue_prompt must be one non-empty line of at most MAX_PROMPT_CHARS characters")
    if timeouts is not None and not isinstance(timeouts, Timeouts):
        raise ValueError("timeouts must be a Timeouts")
    run = _Run(
        session=session,
        target=target,
        mode=mode,
        driver=_GuardedDriver(driver),
        procs=OsProcessProbe() if procs is None else procs,
        read_text=_guarded_read_text(_read_text if read_text is None else read_text),
        glob_paths=_guarded_glob(glob.glob if glob_paths is None else glob_paths),
        isdir=_guarded_isdir(os.path.isdir if isdir is None else isdir),
        clock=time.monotonic if clock is None else clock,
        sleep=time.sleep if sleep is None else sleep,
        timeouts=Timeouts() if timeouts is None else timeouts,
        prompt=continue_prompt,
        profiles_root=PROFILES_ROOT_DEFAULT if profiles_root is None else profiles_root,
    )
    res = _preflight(run)
    if res is not None:
        return res
    return _after_preflight(run)
