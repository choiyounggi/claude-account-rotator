"""The rotator command line: status, daemon, switch, install, uninstall."""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
import unicodedata
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from rotator import launchd
from rotator.accounts import profile_for_config_dir
from rotator.config import CONFIG_PATH_DEFAULT, LOG_DIR_DEFAULT, Config, ConfigError, load_config
from rotator.daemon import (
    IN_PROGRESS_HINT, ORPHANED_HINT, STATE_FILENAME, Deps, UsageCache, identity_of,
    load_records, real_deps, run_daemon, run_switch, touched, usage_summary,
)
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


# --- display width and cells -----------------------------------------------------------------------------


def _char_width(c: str, amb: bool) -> int:
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf"):
        return 0
    ea = unicodedata.east_asian_width(c)
    if ea in ("W", "F"):
        return 2
    return 2 if ea == "A" and amb else 1


def display_width(s: str, ambiguous_wide: bool = False) -> int:
    """Terminal columns of ``s``: East Asian wide/fullwidth take 2, ambiguous ones 2 only when asked."""
    return sum(_char_width(c, ambiguous_wide) for c in s)


def clip(s: str, w: int, amb: bool = False, ell: str = "…") -> str:
    """``s`` cut to at most ``w`` columns, ending in ``ell`` when cut (no padding)."""
    if display_width(s, amb) <= w:
        return s
    ew = display_width(ell, amb)
    if ew > w:
        ell, ew = "", 0
    out: List[str] = []
    used = 0
    for c in s:
        cw = _char_width(c, amb)
        if used + cw + ew > w:
            break
        out.append(c)
        used += cw
    return "".join(out) + ell


def fit(s: str, w: int, *, amb: bool = False, ell: str = "…", align: str = "left") -> str:
    """``s`` cut or space-padded to exactly ``max(0, w)`` columns."""
    s = clip(s, w, amb, ell)
    pad = " " * max(0, w - display_width(s, amb))
    return pad + s if align == "right" else s + pad


def _printable(value: object) -> str:
    # Labels and session fields come from files other programs write: drop control and format characters so
    # a stray ESC can never restyle the terminal or shift the columns.
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if not unicodedata.category(c).startswith("C"))


def _clean(value: object) -> str:
    return _printable(value).strip()


def account_label(p: Any) -> str:
    """Short account name: the email's local part, else the display name, else the folder id's first 8."""
    local = _clean(_clean(getattr(p, "email", None)).split("@", 1)[0])
    if local:
        return local
    name = _clean(getattr(p, "display_name", None))
    if name:
        return name
    return _clean(getattr(p, "account_id", ""))[:8]


_PLANS = {"default_claude_max_5x": "Max5x", "default_claude_max_20x": "Max20x", "default_raven": "Team"}


def plan_tag(tier: object) -> Optional[str]:
    """Short seat tier for the 플랜 column: ``default_claude_max_5x`` -> ``Max5x``; None when unknown."""
    t = _clean(tier)
    if not t:
        return None
    if t in _PLANS:
        return _PLANS[t]
    return (t[len("default_"):] if t.startswith("default_") else t)[:8] or None


@dataclasses.dataclass(frozen=True)
class Glyphs:
    h: str
    v: str
    corners: str  # 9 glyphs: top l/m/r, middle l/m/r, bottom l/m/r
    fill: str
    empty: str
    ell: str
    ge: str
    join: str
    remaining: str


# The bar pair must share one East Asian width class: █ with ░ (A with N) draws bars of different lengths on
# terminals that show ambiguous glyphs double width.
GLYPHS_UNICODE = Glyphs("─", "│", "┌┬┐├┼┤└┴┘", "█", "▒", "…", "≥", "·", "↻%s")
GLYPHS_ASCII = Glyphs("-", "|", "+++++++++", "#", ".", "~", ">=", "/", "(%s)")

_REASONS = {"token_expired": "토큰 만료", "token_missing": "토큰 없음", "request_error": "요청 실패",
            "http_429": "호출 제한", "network": "연결 실패", "timeout": "연결 실패", "bad_json": "응답 이상",
            "bad_shape": "응답 이상", "cache_missing": "캐시 없음", "cache_bad_json": "캐시 손상",
            "cache_bad_shape": "캐시 손상"}


def reason_text(error: object, sep: str = "·") -> str:
    """Korean reasons for a usage error string such as ``token_expired;cache_missing``."""
    out: List[str] = []
    for tok in (error.split(";") if isinstance(error, str) else []):
        tok = tok.strip()
        if not tok:
            continue
        if tok in _REASONS:
            r = _REASONS[tok]
        elif re.fullmatch(r"http_[0-9]{3}", tok):
            r = "HTTP " + tok[5:]
        else:
            r = "확인 불가"
        if r not in out:
            out.append(r)
    return sep.join(out)


def remaining_text(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return "<1m"
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        h, m = s // 3600, s % 3600 // 60
        return "%dh%dm" % (h, m) if m else "%dh" % h
    d, h = s // 86400, s % 86400 // 3600
    return "%dd%dh" % (d, h) if h else "%dd" % d


def age_text(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return "%d초 전" % s
    if s < 3600:
        return "%d분 전" % (s // 60)
    if s < 86400:
        return "%d시간 전" % (s // 3600)
    return "%d일 전" % (s // 86400)


def _bar(util: float, cells: int, g: Glyphs) -> str:
    filled = min(cells, max(0, int(math.floor(util * cells / 100.0))))
    return g.fill * filled + g.empty * (cells - filled)


def _pct_text(util: float) -> str:
    return fit("%d%%" % int(math.floor(max(0.0, util))), 4, align="right")


def _window_state(w: object, now: datetime) -> Optional[Tuple[float, Optional[float]]]:
    """(utilization, seconds until reset) as shown; a window already past its reset shows 0% and no reset."""
    if not isinstance(w, dict):
        return None
    util = w.get("utilization")
    if isinstance(util, bool) or not isinstance(util, (int, float)) or not math.isfinite(util):
        return None
    t = _parse_iso(w.get("resets_at"))
    if t is None:
        return float(util), None
    left = (t - now).total_seconds()
    if left <= 0:
        return 0.0, None
    return float(util), left


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

    profile_by_id = {p.account_id: p for p in profiles}
    accounts = [dict(summaries[aid], label=account_label(profile_by_id[aid]),
                     plan=plan_tag(profile_by_id[aid].rate_limit_tier),
                     pointer=(aid == pointer_id), sessions=per_account.get(aid, []))
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

TITLE = "Claude 계정 사용량"
NO_ACCOUNTS = "계정 없음 (Orca 프로필 폴더 설정과 데몬 실행 여부를 확인하세요)"
QUIT_HINT = "q 또는 Ctrl-C 종료"
_LABEL_CAP = 18
_AGE_CAP = 20
_DIM, _YELLOW, _RED = "2", "33", "31"


@dataclasses.dataclass(frozen=True)
class _Ctx:
    g: Glyphs
    amb: bool
    color: bool


def _local(iso: object, fmt: str, tz: tzinfo) -> str:
    t = _parse_iso(iso)
    if t is None:
        return "-"
    return t.astimezone(tz).strftime(fmt)


def _max_width(lines: List[str], amb: bool) -> int:
    return max((display_width(line, amb) for line in lines), default=0)


def _table(headers: List[str], aligns: List[str], rows: List[List[str]], ctx: _Ctx,
           styles: Optional[List[List[Optional[str]]]] = None) -> List[str]:
    """Boxed table whose every line has the same display width and the same border columns."""
    g, amb = ctx.g, ctx.amb
    hw = display_width(g.h, amb)
    widths = []
    for i, head in enumerate(headers):
        w = max([display_width(head, amb)] + [display_width(r[i], amb) for r in rows]) + 2
        widths.append(w + w % hw)  # a 2-column rule glyph has to fill the cell exactly

    def rule(k: int) -> str:
        left, mid, right = g.corners[3 * k:3 * k + 3]
        return left + mid.join(g.h * (w // hw) for w in widths) + right

    def line(cells: List[str], al: List[str], st: Optional[List[Optional[str]]]) -> str:
        parts = []
        for i, c in enumerate(cells):
            text = fit(c, widths[i] - 2, amb=amb, ell=g.ell, align=al[i])
            if st and st[i]:
                text = "\x1b[%sm%s\x1b[0m" % (st[i], text)  # escapes wrap padded text: widths never count them
            parts.append(" " + text + " ")
        return g.v + g.v.join(parts) + g.v

    out = [rule(0), line(headers, ["left"] * len(headers), None), rule(1)]
    for j, r in enumerate(rows):
        out.append(line(r, aligns, styles[j] if styles and ctx.color else None))
    out.append(rule(2))
    return out


def _row_label(a: dict) -> str:
    return _clean(a.get("label")) or _clean(a.get("account_id"))[:8] or "-"


def _row_plan(a: dict) -> str:
    return _clean(a.get("plan")) or "-"


def _row_key(a: dict) -> Tuple[str, str, str]:
    return _row_label(a), _row_plan(a), _clean(a.get("account_id"))


def _label_suffixes(accounts: List[dict], plan_visible: bool) -> Dict[str, str]:
    """Per account id, what tells rows with the same label apart: the plan when no 플랜 column shows it, then
    ``#`` and the account id's first 4 when label and plan still collide."""
    labels = collections.Counter(_row_label(a) for a in accounts)
    pairs = collections.Counter((_row_label(a), _row_plan(a)) for a in accounts)
    out: Dict[str, str] = {}
    for a in accounts:
        label, plan = _row_label(a), _row_plan(a)
        suffix = " " + plan if not plan_visible and labels[label] > 1 else ""
        if pairs[(label, plan)] > 1:
            suffix += " #" + _clean(a.get("account_id"))[:4]
        out[_clean(a.get("account_id"))] = suffix
    return out


def _label_cell(a: dict, suffixes: Dict[str, str], cap: int, ctx: _Ctx) -> str:
    suffix = suffixes.get(_clean(a.get("account_id")), "")
    return clip(_row_label(a), cap - display_width(suffix, ctx.amb), ctx.amb, ctx.g.ell) + suffix


def _usage_cell(a: dict, key: str, now: datetime, ctx: _Ctx, *, bars: bool, cells: int, remaining: bool,
                reason_cap: Optional[int]) -> Tuple[str, Optional[float]]:
    """Cell text and the utilization it shows (None when it shows no number)."""
    if a.get("source") == "unavailable":
        reason = reason_text(a.get("error"), ctx.g.join) if reason_cap is not None else ""
        return ("-  (%s)" % clip(reason, reason_cap or 0, ctx.amb, ctx.g.ell) if reason else "-"), None
    st = _window_state(a.get(key), now)
    if st is None:
        return "-", None
    util, left = st
    parts = []
    if a.get("source") == "cache":
        parts.append(fit("(캐시)", max(6, display_width(_bar(0, cells, ctx.g), ctx.amb)), amb=ctx.amb)
                     if bars else "(캐시)")
    elif bars:
        parts.append(_bar(util, cells, ctx.g))
    parts.append(_pct_text(util))
    if remaining and left is not None:
        parts.append(ctx.g.remaining % remaining_text(left))
    return " ".join(parts), util


def _age_cell(a: dict, now: datetime, ctx: _Ctx) -> str:
    if a.get("source") == "unavailable":
        return "-"
    fetched = _parse_iso(a.get("fetched_at"))
    if fetched is None:
        return "-"
    text = age_text((now - fetched).total_seconds())
    if a.get("source") == "cache" and a.get("error"):
        text += " (%s)" % reason_text(a.get("error"), ctx.g.join)
    return clip(text, _AGE_CAP, ctx.amb, ctx.g.ell)


def _accounts_table(accounts: List[dict], now: datetime, ctx: _Ctx, level: int, label_cap: int, soft_pct: float,
                    hard_pct: float) -> List[str]:
    bars, plan, fable, age = level == 0, level <= 1, level <= 2, level <= 3
    headers = (["", "계정"] + (["플랜"] if plan else []) + ["5시간", "주간"] + (["페이블"] if fable else []) + ["세션"]
               + (["갱신"] if age else []))
    aligns = ["left"] * (len(headers) - (2 if age else 1)) + ["right"] + (["left"] if age else [])
    windows = [("five_hour", 9, True, 24 if bars else 14), ("seven_day", 9, True, None)]
    if fable:
        windows.append(("fable", 4, False, None))
    rows: List[List[str]] = []
    styles: List[List[Optional[str]]] = []
    suffixes = _label_suffixes(accounts, plan)
    for a in accounts:
        cells = ["*" if a.get("pointer") is True else "", _label_cell(a, suffixes, label_cap, ctx)]
        st: List[Optional[str]] = [None, None]
        if plan:
            cells.append(_row_plan(a))
            st.append(None)
        for key, n, rem, reason_cap in windows:
            text, util = _usage_cell(a, key, now, ctx, bars=bars, cells=n, remaining=rem, reason_cap=reason_cap)
            cells.append(text)
            st.append(None if util is None else _RED if util >= hard_pct else _YELLOW if util >= soft_pct else None)
        cells.append(str(len(a.get("sessions") or [])))
        st.append(None)
        if age:
            cells.append(_age_cell(a, now, ctx))
            st.append(None)
        rows.append(cells)
        styles.append([_DIM] * len(cells) if a.get("source") == "cache" else st)
    return _table(headers, aligns, rows, ctx, styles)


def _fit_accounts(accounts: List[dict], now: datetime, ctx: _Ctx, width: int, soft_pct: float,
                  hard_pct: float) -> List[str]:
    """The widest layout that fits: drop bars, then 플랜, then 페이블, then 갱신, then narrow 계정; else cut every
    line. Rows are ordered by label, plan and account id."""
    accounts = sorted(accounts, key=_row_key)
    plain = dataclasses.replace(ctx, color=False)
    tries = [(level, _LABEL_CAP) for level in range(5)] + [(4, cap) for cap in range(_LABEL_CAP - 1, 3, -1)]
    for level, cap in tries:
        if _max_width(_accounts_table(accounts, now, plain, level, cap, soft_pct, hard_pct), ctx.amb) <= width:
            return _accounts_table(accounts, now, ctx, level, cap, soft_pct, hard_pct)
    return [clip(line, width, ctx.amb, "") for line in _accounts_table(accounts, now, plain, 4, 4, soft_pct, hard_pct)]


def _result_text(r: dict, label_of: Callable[[object], str], tz: tzinfo) -> str:
    parts = []
    dec = r.get("decision")
    if isinstance(dec, dict) and dec:
        target = dec.get("target_account_id")
        parts.append("decision %s%s" % (dec.get("action"), " -> " + label_of(target) if target else ""))
    ws = r.get("would_switch")
    if isinstance(ws, dict) and ws:
        parts.append("would %s -> %s" % (ws.get("action"), label_of(ws.get("target_account_id"))))
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
    if isinstance(lr, dict):
        parts.append("last %s/%s/%s" % (lr.get("step"), lr.get("reason"), lr.get("state")))
    if r.get("post_verify"):
        parts.append("verify %s" % r["post_verify"])
    return _clean(", ".join(parts)) or "-"


def _sessions_table(d: dict, label_of: Callable[[object], str], tz: tzinfo, ctx: _Ctx, width: int) -> List[str]:
    entries = []
    for r in d.get("sessions") or []:
        if isinstance(r, dict):
            entries.append((r, label_of(r.get("account_id")), _result_text(r, label_of, tz)))
    for u in d.get("unmanaged_sessions") or []:
        if isinstance(u, dict):
            entries.append((u, "(관리 밖)", "-"))
    if not entries:
        return []

    def build(cap: Optional[int]) -> List[str]:
        rows = [[str(r.get("pid")), lab, _clean(r.get("status")) or "-",
                 _clean(r.get("model")) or "-", res if cap is None else clip(res, cap, ctx.amb, ctx.g.ell)]
                for r, lab, res in entries]
        return _table(["PID", "계정", "상태", "모델", "최근 전환 결과"], ["right", "left", "left", "left", "left"],
                      rows, ctx)

    table = build(None)
    if _max_width(table, ctx.amb) <= width:
        return table
    for cap in range(max(display_width(res, ctx.amb) for _, _, res in entries) - 1, 3, -1):
        table = build(cap)
        if _max_width(table, ctx.amb) <= width:
            return table
    return [clip(line, width, ctx.amb, "") for line in build(4)]


_WRAP_INDENT = "      "


def _wrap(line: str, w: int, amb: bool, ell: str) -> List[str]:
    """Hint lines are wrapped, never cut."""
    if display_width(line, amb) <= w or w <= len(_WRAP_INDENT) + 4:
        return [clip(line, w, amb, ell)]
    out: List[str] = []
    cur, used, room = "", 0, w
    for c in line:
        cw = _char_width(c, amb)
        if used + cw > room:
            out.append(cur)
            cur, used, room = _WRAP_INDENT, len(_WRAP_INDENT), w
        cur += c
        used += cw
    out.append(cur)
    return out


def _detail_lines(pid: object, hint: object, last_result: object) -> List[Tuple[str, bool]]:
    """(line, wrap) pairs. The relaunch command sits alone on a line that is never wrapped, indented or cut:
    the terminal soft-wraps it, so a copy stays literal (its quoted CLAUDE_CONFIG_DIR names Keychain items)."""
    lines = []
    if hint:
        lines.append(("  pid %s 힌트: %s" % (pid, hint), True))
    cmd = last_result.get("relaunch_command") if isinstance(last_result, dict) else None
    if cmd:
        lines += [("  pid %s 재실행:" % pid, True), (str(cmd), False)]
    return lines


def render(doc: Optional[dict], now: datetime, tz: tzinfo, width: int, *, color: bool = False,
           soft_pct: float = 85.0, hard_pct: float = 95.0, ambiguous_wide: bool = False,
           ascii: bool = False) -> List[str]:
    """Pure table view of a status document; no line is wider than ``width`` terminal columns, except a
    relaunch command, which is printed whole on its own line."""
    w = max(1, width)
    ctx = _Ctx(GLYPHS_ASCII if ascii else GLYPHS_UNICODE, ambiguous_wide, color)
    amb, g = ctx.amb, ctx.g
    d = doc or {}
    daemon = d.get("daemon") or {}
    if daemon.get("running"):
        state = "데몬: 켜짐 (pid %s)%s" % (daemon.get("pid"),
                                       " 드라이런" if (daemon.get("flags") or {}).get("dry_run") is True else "")
    else:
        state = "데몬: 꺼짐"
    right = now.astimezone(tz).strftime("%H:%M:%S") + "  " + state

    def title(span: int) -> str:
        gap = max(2, span - display_width(TITLE, amb) - display_width(right, amb))
        return clip(TITLE + " " * gap + right, w, amb, "")

    accounts = [a for a in d.get("accounts") or [] if isinstance(a, dict)]
    if not accounts:
        return [title(0), clip(NO_ACCOUNTS, w, amb, g.ell)]
    table = _fit_accounts(accounts, now, ctx, w, soft_pct, hard_pct)
    legend = "* = Orca 선택 계정   노랑 %s%g%%  빨강 %s%g%%" % (g.ge, soft_pct, g.ge, hard_pct)
    lines = [title(display_width(table[0], amb))] + table + [clip(legend, w, amb, g.ell)]

    suffixes = _label_suffixes(accounts, plan_visible=False)  # the sessions table has no 플랜 column
    labels = {a.get("account_id"): _label_cell(a, suffixes, _LABEL_CAP, ctx) for a in accounts}

    def label_of(aid: object) -> str:
        return labels.get(aid) or _clean(aid)[:8] or "-"

    free: List[Tuple[str, bool]] = []
    sessions = _sessions_table(d, label_of, tz, dataclasses.replace(ctx, color=False), w)
    if sessions:
        lines += [""] + sessions
        for r in d.get("sessions") or []:
            if isinstance(r, dict):
                free += _detail_lines(r.get("pid"), r.get("hint"), r.get("last_result"))
    attention = [x for x in d.get("attention") or [] if isinstance(x, dict)]
    if attention:
        free += [("", True), ("주의 (최근 24시간 실패한 전환)", True)]
        for item in attention:
            lr = item.get("last_result")
            free.append(("  pid %s %s %s" % (item.get("pid"), item.get("record_state"),
                                             lr.get("state", "-") if isinstance(lr, dict) else "-"), True))
            free += _detail_lines(item.get("pid"), item.get("hint"), lr)
    for line, wrap in free:
        lines += _wrap(_printable(line), w, amb, g.ell) if wrap else [_printable(line)]
    return lines


# --- terminal --------------------------------------------------------------------------------------------


def _isatty(f: object) -> bool:
    try:
        return bool(f is not None and f.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


class RealTerm:
    """The TermCtl of a real terminal: alternate screen, cursor, cbreak keys and the ambiguous-width probe.

    ``out`` is the text stream the table is drawn on; ``inp`` the input stream keys and probe replies come
    from. Key reading and the probe need both to be terminals; anything else degrades to a plain sleep / False.
    """

    def __init__(self, out: TextIO, inp: Optional[Any]):
        self.out = out
        self.inp = inp
        self._saved: Optional[list] = None

    def _fd(self) -> Optional[int]:
        if not _isatty(self.inp):
            return None
        try:
            return self.inp.fileno()  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            return None

    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def enter_alt(self) -> None:
        self._write("\x1b[?1049h")
        fd = self._fd()
        if fd is not None and self._saved is None:
            try:
                self._saved = termios.tcgetattr(fd)
                # TCSANOW: TCSADRAIN/TCSAFLUSH would wait until the terminal has read every byte written so far
                tty.setcbreak(fd, termios.TCSANOW)  # keys arrive without Enter; ISIG keeps Ctrl-C working
            except termios.error:
                self._saved = None

    def leave_alt(self) -> None:
        fd = self._fd()
        if fd is not None and self._saved is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, self._saved)
            except termios.error:
                pass
        self._saved = None
        self._write("\x1b[?1049l")

    def hide_cursor(self) -> None:
        self._write("\x1b[?25l")

    def show_cursor(self) -> None:
        self._write("\x1b[?25h")

    def read_key(self, timeout: float) -> Optional[str]:
        """Keys typed within ``timeout`` seconds, or None; waits the full timeout when input is closed."""
        fd = self._fd()
        if fd is None:
            time.sleep(timeout)
            return None
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            time.sleep(timeout)
            return None
        if not ready:
            return None
        try:
            data = os.read(fd, 32)
        except OSError:
            data = b""
        if not data:  # end of input: sleep instead of spinning on a descriptor that is always readable
            time.sleep(timeout)
            return None
        return data.decode("utf-8", "replace")

    def probe_ambiguous_wide(self, timeout: float = 0.2) -> bool:
        """True when the terminal draws an East Asian ambiguous glyph two columns wide.

        Prints one '─' at column 1 and asks for the cursor position (ESC[6n); the reply's column is 3 on a
        wide terminal. No terminal, no reply in time or an unreadable reply all mean narrow.
        """
        fd = self._fd()
        if fd is None or not _isatty(self.out):
            return False
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return False
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            termios.tcflush(fd, termios.TCIFLUSH)  # a stale reply or typed-ahead key must not be read as the answer
            self._write("\r─\x1b[6n")
            buf = ""
            deadline = time.monotonic() + timeout
            while "R" not in buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], left)
                if not ready:
                    break
                chunk = os.read(fd, 32)
                if not chunk:
                    break
                buf += chunk.decode("ascii", "replace")
            m = re.search(r"\x1b\[(\d+);(\d+)R", buf)
            return bool(m) and int(m.group(2)) == 3
        except (OSError, ValueError, termios.error):
            return False
        finally:
            try:
                self._write("\r\x1b[2K")
            except (OSError, ValueError):
                pass
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except termios.error:
                pass


def _on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _probe(term: Any, ascii: bool) -> bool:
    if term is None or ascii:
        return False
    try:
        return bool(term.probe_ambiguous_wide())
    except Exception:
        return False


# --- command line ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rotator",
        description="Switch Orca-hosted Claude Code sessions to another account before they hit the rate limit.")
    p.add_argument("--config", metavar="PATH", default=None, help="config file (default: %s)" % CONFIG_PATH_DEFAULT)
    sub = p.add_subparsers(dest="command", metavar="{status,daemon,switch,install,uninstall}")
    st = sub.add_parser("status", help="show accounts, usage and sessions")
    mode = st.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="redraw until q or Ctrl-C")
    mode.add_argument("--json", action="store_true", help="print the status document as JSON")
    st.add_argument("--interval", type=float, default=WATCH_INTERVAL_S, metavar="S",
                    help="seconds between --watch redraws (default 1)")
    st.add_argument("--ascii", action="store_true",
                    help="draw with ASCII only (for terminals that show box glyphs double width)")
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
         stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None, tz: Optional[tzinfo] = None,
         term_ctl: Optional[Any] = None, read_key: Optional[Callable[[float], Optional[str]]] = None,
         stdin: Optional[Any] = None) -> int:
    """Entry point; returns 0 ok, 1 runtime failure, 2 usage or config error.

    ``term_ctl`` / ``read_key`` replace the terminal seams of ``status`` (RealTerm on a TTY). Keys and the
    width probe read ``stdin``, which defaults to sys.stdin only when ``stdout`` is not injected either.
    """
    out = stdout or sys.stdout
    inp = stdin if stdin is not None else (sys.stdin if stdout is None else None)
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
        return _status(args, cfg, deps, out, err, tz or datetime.now().astimezone().tzinfo, term_ctl, read_key, inp)
    return _switch(args, cfg, deps, out, err)


def _status(args: argparse.Namespace, cfg: Config, deps: Deps, out: TextIO, err: TextIO, tz: tzinfo,
            term_ctl: Optional[Any] = None, read_key: Optional[Callable[[float], Optional[str]]] = None,
            inp: Optional[Any] = None) -> int:
    interval = args.interval
    if args.watch and not interval > 0:  # before any probe: a usage error must not touch the terminal
        print("--interval must be > 0", file=err)
        return 2
    try:
        if args.json:
            out.write(json.dumps(build_status(cfg, deps, deps.now()), sort_keys=True, indent=2) + "\n")
            return 0
        term = term_ctl if term_ctl is not None else (RealTerm(out, inp) if _isatty(out) else None)
        opts: Dict[str, Any] = {"color": _isatty(out) and not os.environ.get("NO_COLOR"),  # no-color.org
                                "soft_pct": cfg.soft_pct, "hard_pct": cfg.hard_pct, "ascii": args.ascii}
        if not args.watch:
            opts["ambiguous_wide"] = _probe(term, args.ascii)
            now = deps.now()
            width = shutil.get_terminal_size((100, 40)).columns
            out.write("\n".join(render(build_status(cfg, deps, now), now, tz, width, **opts)) + "\n")
            return 0
        if read_key is None and isinstance(term, RealTerm) and _isatty(inp):
            read_key = term.read_key
        prev_sigterm: Any = False
        try:
            if term is not None:
                term.hide_cursor()
                term.enter_alt()
                if threading.current_thread() is threading.main_thread():
                    prev_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
            opts["ambiguous_wide"] = _probe(term, args.ascii)
            _watch(cfg, deps, out, tz, opts, term is not None, read_key, interval)
        finally:
            if prev_sigterm is not False:
                signal.signal(signal.SIGTERM, signal.SIG_DFL if prev_sigterm is None else prev_sigterm)
            if term is not None:
                term.show_cursor()
                term.leave_alt()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print("status failed: %s" % type(e).__name__, file=err)
        return 1


def _watch(cfg: Config, deps: Deps, out: TextIO, tz: tzinfo, opts: Dict[str, Any], screen: bool,
           read_key: Optional[Callable[[float], Optional[str]]], interval: float) -> None:
    """Redraw until q (with a key reader) or Ctrl-C / SIGTERM (KeyboardInterrupt)."""
    ev = threading.Event()
    usage = UsageCache(cfg.usage_poll_s)  # one cache per watch: fetch per usage_poll_s, never per redraw
    doc: Optional[dict] = None
    built_at = 0.0
    hint = QUIT_HINT if read_key is not None else "Ctrl-C 종료"
    while True:
        now = deps.now()
        mono = deps.monotonic()
        # Discovery and file reads run once per session_poll_s; frames in between only re-render the
        # last document with a fresh now, so ages and countdowns still tick.
        if doc is None or mono - built_at >= cfg.session_poll_s:
            doc = build_status(cfg, deps, now, usage=usage)
            built_at = mono
        width = shutil.get_terminal_size((100, 40)).columns
        lines = render(doc, now, tz, width, **opts) + [clip(hint, width, opts["ambiguous_wide"], "")]
        out.write(("\x1b[H\x1b[2J" if screen else "") + "\n".join(lines) + "\n" + ("" if screen else "---\n"))
        out.flush()
        if read_key is None:
            deps.wait_stop(ev, interval)
            continue
        key = read_key(interval)
        if key and ("q" in key.lower() or "\x03" in key):
            return


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
