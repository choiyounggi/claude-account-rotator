"""The rotator command line: status, daemon, switch, install, uninstall."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import shutil
import sys
import threading
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

from rotator import launchd
from rotator.accounts import profile_for_config_dir
from rotator.config import CONFIG_PATH_DEFAULT, LOG_DIR_DEFAULT, Config, ConfigError, load_config
from rotator.daemon import (
    IN_PROGRESS_HINT, ORPHANED_HINT, RATE_LIMITED_TOKEN, STATE_FILENAME, Deps, UsageCache, identity_of,
    load_records, real_deps, run_daemon, run_switch, touched, usage_summary,
)
from rotator.models import is_fable_model
from rotator.policy import decide
from rotator.sessions import session_homes
from rotator.switcher import STATE_HINTS, build_relaunch_command

STATUS_SCHEMA = 1
STATUS_KEYS = ("schema", "generated_at", "daemon", "accounts", "pointer_account_id", "sessions",
               "unmanaged_sessions", "attention")
ATTENTION_TTL_S = 86400.0
WATCH_INTERVAL_S = 1.0

# status logs nothing anywhere: without a handler, UsageCache's 429/fetch warnings would fall through to
# logging's last-resort stderr handler and land between --watch frames.
logging.getLogger("rotator.cli").addHandler(logging.NullHandler())


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


# --- status document -------------------------------------------------------------------------------------


def _hint(rec: Optional[dict], deps: Deps) -> str:
    if rec is None:
        return ""
    if rec.get("record_state") == "in_progress":
        owner = rec.get("owner_pid")
        return IN_PROGRESS_HINT if type(owner) is int and deps.pid_alive(owner) else ORPHANED_HINT
    return rec.get("hint") or ""


def build_status(cfg: Config, deps: Deps, now: datetime, usage: Optional[UsageCache] = None) -> dict:
    """One status document for the table and --json. Reads only: never locks, never writes.

    Usage comes from a fresh snapshot of a running daemon, else from ``usage`` (a UsageCache the caller
    keeps across calls so each account is fetched at most once per usage_poll_s) or a new one-shot cache.
    """
    log = logging.getLogger("rotator.cli")  # never deps.make_logger(): status writes nothing
    profiles = deps.discover_profiles(cfg.profiles_root)
    try:
        found = deps.discover_sessions(session_homes(profiles))
    except Exception as e:  # a reader or registry bug must not take the status view down
        log.error("event=discover_error what=sessions exc=%s", type(e).__name__)
        found = []
    snap: Optional[dict] = None
    try:
        raw = json.loads(deps.read_text(os.path.join(cfg.state_dir, STATE_FILENAME)))
        snap = raw if isinstance(raw, dict) else None
    except Exception:
        snap = None
    dpid = snap.get("daemon_pid") if snap and type(snap.get("daemon_pid")) is int else None
    written = _parse_iso(snap.get("written_at")) if snap else None
    age = (now - written).total_seconds() if written is not None else None
    running = dpid is not None and deps.pid_alive(dpid)
    flags = snap.get("flags") if snap and isinstance(snap.get("flags"), dict) else None
    fresh = running and age is not None and 0 <= age < 2 * cfg.usage_poll_s
    by_id: Dict[str, dict] = {}
    if fresh and snap is not None:
        by_id = {d["account_id"]: d for d in (snap.get("accounts") or [])
                 if isinstance(d, dict) and "account_id" in d}
    missing = [p for p in profiles if p.account_id not in by_id]
    cache = usage if usage is not None else UsageCache(cfg.usage_poll_s)
    if missing:
        cache.refresh(missing, fetch=deps.fetch_usage, monotonic=deps.monotonic, now=now, log=log)
    cached = cache.usages()
    summaries = {p.account_id: by_id[p.account_id] if p.account_id in by_id else usage_summary(cached[p.account_id])
                 for p in profiles}
    try:
        text = deps.read_pointer(cfg.profiles_root)
    except Exception:
        text = None
    pp = profile_for_config_dir(profiles, text) if text else None
    pointer_id = pp.account_id if pp else None
    records = load_records(cfg.state_dir, deps, log)
    snap_rows: Dict[Any, dict] = {}
    if running and snap is not None:
        snap_rows = {r["pid"]: r for r in (snap.get("sessions") or []) if isinstance(r, dict) and "pid" in r}

    per_account: Dict[str, List[dict]] = {}
    sessions: List[dict] = []
    unmanaged: List[dict] = []
    live_idents = set()
    for s in found:
        own = profile_for_config_dir(profiles, s.config_dir)
        if own is None:
            unmanaged.append({"pid": s.pid, "session_id": s.session_id, "status": s.status, "model": s.model})
            continue
        per_account.setdefault(own.account_id, []).append({"pid": s.pid, "status": s.status, "model": s.model})
        ident = identity_of(s)
        live_idents.add(ident)
        rec = records.get(ident)
        sr = snap_rows.get(s.pid)
        if sr is not None and sr.get("session_id") != s.session_id:
            sr = None
        sessions.append({
            "pid": s.pid,
            "session_id": s.session_id,
            "status": s.status,
            "account_id": own.account_id,
            "model": s.model,
            "decision": sr.get("decision") if sr else None,
            "would_switch": sr.get("would_switch") if sr else None,
            "in_flight": bool(sr.get("in_flight")) if sr else False,
            "in_flight_pid": sr.get("in_flight_pid") if sr else None,
            "stuck": bool(sr.get("stuck")) if sr else False,
            "unswitchable": rec.get("unswitchable") if rec else None,
            "hard_hold_until": rec.get("hard_hold_until") if rec else None,
            "excluded": list(rec.get("excluded") or []) if rec else [],
            "record_state": rec.get("record_state") if rec else None,
            "last_result": rec.get("result") if rec and isinstance(rec.get("result"), dict) else None,
            "hint": _hint(rec, deps),
            "post_verify": rec.get("post_verify") if rec else None,
        })

    attention: List[dict] = []
    for ident, rec in records.items():
        if ident in live_idents or not touched(rec):
            continue
        res = rec.get("result")
        if isinstance(res, dict) and res.get("ok") is True:
            continue
        t = _parse_iso(rec.get("updated_at"))
        if t is None or not 0 <= (now - t).total_seconds() <= ATTENTION_TTL_S:
            continue
        attention.append({"pid": rec.get("pid"), "session_id": rec.get("session_id"),
                          "updated_at": rec.get("updated_at"), "record_state": rec.get("record_state"),
                          "last_result": res if isinstance(res, dict) else None, "hint": _hint(rec, deps)})
    attention.sort(key=lambda a: a["updated_at"], reverse=True)

    accounts = [dict(summaries[aid], pointer=(aid == pointer_id), sessions=per_account.get(aid, []))
                for aid in sorted(summaries)]
    return {
        "schema": STATUS_SCHEMA,
        "generated_at": _iso(now),
        "daemon": {"running": running, "pid": dpid, "snapshot_age_s": age, "flags": flags},
        "accounts": accounts,
        "pointer_account_id": pointer_id,
        "sessions": sessions,
        "unmanaged_sessions": unmanaged,
        "attention": attention,
    }


# --- rendering -------------------------------------------------------------------------------------------


def _pct(w: Optional[dict]) -> str:
    if w is None:
        return "-"
    return "%.1f%%" % w["utilization"]


def _local(iso: object, fmt: str, tz: tzinfo) -> str:
    t = _parse_iso(iso)
    if t is None:
        return "-"
    return t.astimezone(tz).strftime(fmt)


def _dur(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    return "%dd%02dh" % (s // 86400, s % 86400 // 3600)


def _countdown(iso: object, now: datetime) -> str:
    t = _parse_iso(iso)
    if t is None:
        return "-"
    d = (t - now).total_seconds()
    if d <= 0:
        return "due"
    return "in " + _dur(d)


def _win(w: Optional[dict], now: datetime, tz: tzinfo) -> str:
    if w is None:
        return "-"
    return "%s (%s, %s)" % (_pct(w), _local(w.get("resets_at"), "%m-%d %H:%M", tz),
                            _countdown(w.get("resets_at"), now))


def _model_part(model: object) -> str:
    if not isinstance(model, str) or not model:
        return ""
    return "model " + model + (" [fable]" if is_fable_model(model) else "")


def _err_label(e: str) -> str:
    if e == RATE_LIMITED_TOKEN or e.startswith(RATE_LIMITED_TOKEN + ";"):
        return "rate_limited" + e[len(RATE_LIMITED_TOKEN):]
    return e


def _age(a: dict, now: datetime) -> str:
    # ASCII on purpose: lines are cut by code points, and a Hangul label takes two terminal columns.
    if a.get("source") == "unavailable":
        return ""
    fetched = _parse_iso(a.get("fetched_at"))
    if fetched is None:
        return ""
    return " %s ago" % _dur((now - fetched).total_seconds())


def _detail_lines(hint: Optional[str], last_result: Optional[dict]) -> List[str]:
    lines = []
    if hint:
        lines.append("    hint: " + hint)
    cmd = (last_result or {}).get("relaunch_command")
    if cmd:
        lines.append("    relaunch: " + cmd)
    return lines


def render(doc: Optional[dict], now: datetime, tz: tzinfo, width: int) -> List[str]:
    """Pure text view of a status document; every line is cut to ``width`` characters."""
    w = max(1, width)
    d = doc or {}
    accounts = d.get("accounts") or []
    daemon = d.get("daemon") or {}
    header = "accounts: %d  daemon: %s  dry_run: %s  at %s" % (
        len(accounts), "running (pid %s)" % daemon.get("pid") if daemon.get("running") else "stopped",
        (daemon.get("flags") or {}).get("dry_run", "-"), now.astimezone(tz).strftime("%H:%M:%S"))
    if not accounts:
        lines = [header, "no accounts (is the Orca profiles folder set? is the daemon running?)"]
        return [line[:w] for line in lines]
    lines = [header]
    for a in accounts:
        lines.append("%s %s  5h %s  7d %s  fable %s  %s%s%s  sessions: %s" % (
            "*" if a.get("pointer") else " ", a["account_id"], _win(a.get("five_hour"), now, tz),
            _win(a.get("seven_day"), now, tz), _win(a.get("fable"), now, tz), a.get("source"),
            " (%s)" % _err_label(a["error"]) if a.get("error") else "", _age(a, now),
            ", ".join("%d %s" % (x["pid"], x["status"]) for x in a.get("sessions") or []) or "none"))
    unmanaged = d.get("unmanaged_sessions") or []
    if unmanaged:
        lines.append("unmanaged:")
        for u in unmanaged:
            line = "  pid %d %s" % (u["pid"], u["status"])
            mp = _model_part(u.get("model"))
            if mp:
                line += "  " + mp
            lines.append(line)
    rows = d.get("sessions") or []
    if rows:
        lines.append("sessions:")
        for r in rows:
            parts = ["  pid %d %s on %s" % (r["pid"], r["status"], r["account_id"])]
            mp = _model_part(r.get("model"))
            if mp:
                parts.append(mp)
            dec = r.get("decision")
            if dec:
                target = dec.get("target_account_id")
                parts.append("decision %s%s" % (dec.get("action"), " -> " + target if target else ""))
            ws = r.get("would_switch")
            if ws:
                parts.append("would %s -> %s" % (ws.get("action"), ws.get("target_account_id")))
            if r.get("in_flight"):
                parts.append("in flight (pid %s)" % r.get("in_flight_pid"))
            if r.get("stuck"):
                parts.append("STUCK")
            if r.get("unswitchable"):
                parts.append("unswitchable (%s)" % r["unswitchable"])
            if r.get("hard_hold_until"):
                parts.append("hard hold until %s" % _local(r["hard_hold_until"], "%H:%M", tz))
            if r.get("record_state") == "in_progress":
                parts.append("switch in progress")
            lr = r.get("last_result")
            if lr:
                parts.append("last %s/%s/%s" % (lr.get("step"), lr.get("reason"), lr.get("state")))
            if r.get("post_verify"):
                parts.append("verify %s" % r["post_verify"])
            lines.append("  ".join(parts))
            lines.extend(_detail_lines(r.get("hint"), lr))
    attention = d.get("attention") or []
    if attention:
        lines.append("attention:")
        for item in attention:
            lr = item.get("last_result")
            lines.append("  pid %d %s %s" % (item["pid"], item.get("record_state"), (lr or {}).get("state", "-")))
            lines.extend(_detail_lines(item.get("hint"), lr))
    return [line[:w] for line in lines]


# --- command line ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rotator",
        description="Switch Orca-hosted Claude Code sessions to another account before they hit the rate limit.")
    p.add_argument("--config", metavar="PATH", default=None, help="config file (default: %s)" % CONFIG_PATH_DEFAULT)
    sub = p.add_subparsers(dest="command", metavar="{status,daemon,switch,install,uninstall}")
    st = sub.add_parser("status", help="show accounts, usage and sessions")
    mode = st.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="redraw until Ctrl-C")
    mode.add_argument("--json", action="store_true", help="print the status document as JSON")
    st.add_argument("--interval", type=float, default=WATCH_INTERVAL_S, metavar="S",
                    help="seconds between --watch redraws (default 1)")
    dm = sub.add_parser("daemon", help="run the rotator in the foreground")
    dm.add_argument("--dry-run", action="store_true", help="never switch and never write selected-host")
    sw = sub.add_parser("switch", help="switch one session to another account once")
    sw.add_argument("--session", type=int, required=True, metavar="PID", help="pid of the Claude Code session")
    sw.add_argument("--to", required=True, metavar="ACCOUNT_ID", help="target account folder name")
    how = sw.add_mutually_exclusive_group()
    how.add_argument("--soft", dest="mode", action="store_const", const="soft", help="switch only when idle (default)")
    how.add_argument("--hard", dest="mode", action="store_const", const="hard", help="interrupt a running turn first")
    sw.set_defaults(mode="soft")
    sw.add_argument("--dry-run", action="store_true", help="print the decision and relaunch line; send nothing")
    sub.add_parser("install", help="install and start the launchd user agent")
    sub.add_parser("uninstall", help="stop and remove the launchd user agent")
    return p


def _install_kwargs(cfg: Config, config_path: str) -> dict:
    return {"plist_path": launchd.plist_path_default(), "repo_root": launchd.repo_root(),
            "config_path": config_path, "log_dir": LOG_DIR_DEFAULT, "timeouts": cfg.timeouts}


def main(argv: Optional[Sequence[str]] = None, *, make_deps: Optional[Callable[[Config], Deps]] = None,
         launchctl: Optional[launchd.Runner] = None, uid: Optional[Callable[[], int]] = None,
         stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None, tz: Optional[tzinfo] = None) -> int:
    """Entry point; returns 0 ok, 1 runtime failure, 2 usage or config error."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = build_parser()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    if args.command is None:
        parser.print_usage(err)
        return 2
    config_path = os.path.abspath(os.path.expanduser(args.config)) if args.config else CONFIG_PATH_DEFAULT
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print("config error: %s" % e, file=err)
        return 2
    if args.command in ("install", "uninstall"):
        run = launchctl or launchd.run_launchctl
        u = (uid or os.getuid)()
        if args.command == "install":
            rc, msg = launchd.install(runner=run, uid=u, **_install_kwargs(cfg, config_path))
        else:
            rc, msg = launchd.uninstall(runner=run, uid=u, plist_path=launchd.plist_path_default())
        print(msg, file=out if rc == 0 else err)
        return rc
    if args.command == "daemon" and args.dry_run:
        cfg = dataclasses.replace(cfg, dry_run=True)
    deps = (make_deps or real_deps)(cfg)
    if args.command == "daemon":
        return run_daemon(cfg, deps)
    if args.command == "status":
        return _status(args, cfg, deps, out, err, tz or datetime.now().astimezone().tzinfo)
    return _switch(args, cfg, deps, out, err)


def _status(args: argparse.Namespace, cfg: Config, deps: Deps, out: TextIO, err: TextIO, tz: tzinfo) -> int:
    try:
        if args.json:
            out.write(json.dumps(build_status(cfg, deps, deps.now()), sort_keys=True, indent=2) + "\n")
            return 0
        width = shutil.get_terminal_size((100, 40)).columns
        if not args.watch:
            now = deps.now()
            out.write("\n".join(render(build_status(cfg, deps, now), now, tz, width)) + "\n")
            return 0
        interval = args.interval
        if not interval > 0:
            print("--interval must be > 0", file=err)
            return 2
        tty = bool(getattr(out, "isatty", lambda: False)())
        ev = threading.Event()
        usage = UsageCache(cfg.usage_poll_s)  # one cache per watch: fetch per usage_poll_s, never per redraw
        doc: Optional[dict] = None
        built_at = 0.0
        while True:
            now = deps.now()
            mono = deps.monotonic()
            # Discovery and file reads run once per session_poll_s; frames in between only re-render the
            # last document with a fresh now, so ages and countdowns still tick.
            if doc is None or mono - built_at >= cfg.session_poll_s:
                doc = build_status(cfg, deps, now, usage=usage)
                built_at = mono
            lines = render(doc, now, tz, width)
            out.write(("\x1b[H\x1b[2J" if tty else "") + "\n".join(lines) + "\n" + ("" if tty else "---\n"))
            out.flush()
            deps.wait_stop(ev, interval)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print("status failed: %s" % type(e).__name__, file=err)
        return 1


def _switch(args: argparse.Namespace, cfg: Config, deps: Deps, out: TextIO, err: TextIO) -> int:
    try:
        profiles = deps.discover_profiles(cfg.profiles_root)
        found = deps.discover_sessions(session_homes(profiles))
    except Exception as e:
        print("switch failed: %s" % type(e).__name__, file=err)
        return 1
    s = next((x for x in found if x.pid == args.session), None)
    if s is None:
        print("no live Claude Code session with pid %d" % args.session, file=err)
        return 2
    own = profile_for_config_dir(profiles, s.config_dir)
    if own is None:
        print("session %d is unmanaged: its CLAUDE_CONFIG_DIR is not an Orca account folder" % s.pid, file=err)
        return 2
    target = next((p for p in profiles if p.account_id == args.to), None)
    if target is None:
        print("unknown account %s" % args.to, file=err)
        return 2
    if target.account_id == own.account_id:
        print("session %d already runs on account %s" % (s.pid, own.account_id), file=err)
        return 2

    if args.dry_run or cfg.dry_run:
        usages = {}
        for p in profiles:
            try:
                usages[p.account_id] = deps.fetch_usage(p)
            except Exception:
                continue
        d = decide(s, profiles, usages, deps.now(), config=cfg.policy_config())
        print("decide: action=%s target=%s reason=%s (cooldown not applied in this preview)"
              % (d.action, d.target.account_id if d.target else "-", d.reason), file=out)
        try:
            print("relaunch preview: " + build_relaunch_command(s, target.home), file=out)
        except ValueError as e:
            print("relaunch preview unavailable: %s" % e, file=out)
        print("dry run: nothing was sent", file=out)
        return 0

    logger = deps.make_logger()
    try:
        o = run_switch(s, own, target, args.mode, cfg=cfg, deps=deps, logger=logger, source="cli",
                       profiles_count=len(profiles))
    except Exception as e:
        print("switch failed (%s); the outcome is unknown: look at the pane before typing anything"
              % type(e).__name__, file=err)
        return 1
    if o.status == "busy":
        print("switch already in progress for this session", file=err)
        return 1
    if o.status == "intent_failed":
        print("could not record the switch intent; nothing was sent", file=err)
        return 1
    if o.status == "refused_touched":
        print("session %d was already switched or touched; it is never switched again" % s.pid, file=err)
        res = (o.record or {}).get("result")
        if isinstance(res, dict):
            print("state: %s" % res.get("state"), file=err)
            print("hint: %s" % (o.record or {}).get("hint", ""), file=err)
            if res.get("relaunch_command"):
                print("relaunch: %s" % res["relaunch_command"], file=err)
        return 1
    r = o.result
    assert r is not None
    print("ok=%s step=%s reason=%s state=%s new_pid=%s recoverable=%s request_id=%s"
          % (r.ok, r.step, r.reason, r.state, r.new_pid, r.recoverable, r.request_id), file=out)
    hint = STATE_HINTS.get(r.state, "")
    if hint:
        print("hint: %s" % hint, file=out)
    if r.relaunch_command and not r.ok:
        print("relaunch: %s" % r.relaunch_command, file=out)
    if o.record is not None and o.record.get("post_verify"):
        print("post_verify: %s" % o.record["post_verify"], file=out)
    return 0 if r.ok else 1
