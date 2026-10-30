"""Idle-account OAuth token refresh with write-back to Claude Code's Keychain item.

Only the daemon calls into this module. Secrets live in locals and in the
redacting slot classes below; nothing here logs, raises or returns a token.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from rotator import accounts, sessions, usage
from rotator.config import Config
from rotator.models import Profile

log = logging.getLogger("rotator.refresh")
log.addHandler(logging.NullHandler())

# The exchange below mirrors this Claude Code release; re-verify after it updates.
CLAUDE_CODE_VERSION = "2.1.296"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# The edge (Cloudflare error 1010) answers 403 to urllib's default Python-urllib/x.y agent.
USER_AGENT = "claude-account-rotator/0.1"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BASE_SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers",
               "user:file_upload", "user:plugins")
PROJECT_SCOPES = ("user:projects:read", "user:projects:write")
EXPIRY_MARGIN_MS = 300_000
REFRESH_TIMEOUT_S = 10.0
HOLD_BUDGET_S = 30.0
PAYLOAD_MARGIN_CHARS = 800
WRITE_ATTEMPTS = 3
BACKOFF_START_S = 300.0
BACKOFF_MAX_S = 3600.0
FLUSH_BACKOFF_START_S = 30.0
FLUSH_BACKOFF_MAX_S = 600.0
CC_LOCK_STALE_S = 60.0
ERROR_BODY_MAX = 65_536
_LOGGABLE_OAUTH_ERROR = re.compile(r"[a-z0-9_]{1,64}")
REFRESH_STATES = ("not_needed", "skipped", "refreshed", "failed", "stopped")
REFRESH_REASONS = frozenset({
    "fresh", "ok", "write_recovered", "disabled", "dry_run", "daemon_off", "no_credential", "no_refresh_token",
    "plaintext_store", "live_session", "discovery_failed", "keychain_item_unclear", "payload_too_large", "deferred",
    "locked", "cc_lock_busy", "cc_lock_stale", "changed_under_lock", "keychain_write_unavailable", "invalid_grant",
    "invalid_scope", "rejected", "malformed_response", "outcome_unknown", "network_error", "server_error",
    "rate_limited", "write_failed", "conflict", "internal_error",
})
DAEMON_OFF_RECORD = {"state": "skipped", "reason": "daemon_off", "at": None, "last_refreshed_at": None}


class PostResult:
    __slots__ = ("status", "body", "retry_after")

    def __init__(self, status: int, body: str, retry_after: Optional[str] = None) -> None:
        self.status, self.body, self.retry_after = status, body, retry_after

    def __repr__(self) -> str:
        return "PostResult(status=%d, body=<redacted>)" % self.status

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("PostResult is not picklable")


Post = Callable[[str, Mapping[str, str], bytes, float], PostResult]


def _urllib_post(url: str, headers: Mapping[str, str], data: bytes, timeout: float) -> PostResult:
    """POST without following redirects; an HTTP error status is returned, other errors propagate."""
    req = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    try:
        # Looked up on the module so tests can replace the opener.
        with usage._NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read(usage._MAX_BODY_BYTES)
    except urllib.error.HTTPError as e:
        code = e.code
        body_b = b""
        ra = None
        try:
            body_b = e.read(ERROR_BODY_MAX)
        except Exception:
            body_b = b""
        try:
            ra = e.headers.get("Retry-After")
        except (AttributeError, TypeError):
            ra = None
        if not isinstance(ra, str) or len(ra) > usage.RETRY_AFTER_MAX_LEN:
            ra = None
        e.close()
        return PostResult(code, body_b.decode("utf-8", errors="replace"), ra)
    return PostResult(status, raw.decode("utf-8", errors="replace"), None)


def request_scopes(oauth: Mapping[str, Any]) -> List[str]:
    """The scope list Claude Code sends for a stored ``claudeAiOauth`` object."""
    raw = oauth.get("scopes")
    stored = [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
    cid = oauth.get("clientId")
    sub = oauth.get("subscriptionType")
    if ("user:inference" in stored or (isinstance(sub, str) and sub)) and not (isinstance(cid, str) and cid):
        out: List[str] = []
        for s in list(BASE_SCOPES) + [s for s in stored if s in PROJECT_SCOPES]:
            if s not in out:
                out.append(s)
        return out
    return stored


class _Tokens:
    __slots__ = ("access_token", "refresh_token", "expires_at_ms", "refresh_expires_at_ms", "scopes")

    def __init__(self, access_token: str, refresh_token: str, expires_at_ms: int,
                 refresh_expires_at_ms: Optional[int], scopes: Optional[List[str]]) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at_ms = expires_at_ms
        self.refresh_expires_at_ms = refresh_expires_at_ms
        self.scopes = scopes

    def __repr__(self) -> str:
        return "_Tokens(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("_Tokens is not picklable")


def _finite_pos(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def parse_token_response(body: str, now_ms: int, posted_refresh_token: str) -> Optional[_Tokens]:
    """Strictly validated token set from a 200 body, or None."""
    try:
        obj = usage.loads_network_json(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    access = obj.get("access_token")
    if not (isinstance(access, str) and accounts._TOKEN_RE.fullmatch(access)):
        return None
    expires_in = obj.get("expires_in")
    if not _finite_pos(expires_in):
        return None
    try:
        expires_at = now_ms + int(expires_in * 1000)
    except (OverflowError, ValueError):
        return None
    if "refresh_token" in obj:
        rt = obj["refresh_token"]
        if not (isinstance(rt, str) and accounts._TOKEN_RE.fullmatch(rt)):
            return None
    else:
        rt = posted_refresh_token
    rexp_in = obj.get("refresh_token_expires_in")
    rexp: Optional[int] = None
    if _finite_pos(rexp_in):
        try:
            rexp = now_ms + int(rexp_in * 1000)
        except (OverflowError, ValueError):
            rexp = None
    scope = obj.get("scope")
    scopes: Optional[List[str]] = None
    if isinstance(scope, str):
        scopes = [s for s in scope.split(" ") if s] or None
    return _Tokens(access, rt, expires_at, rexp, scopes)


def build_document(doc: Mapping[str, Any], tokens: _Tokens) -> dict:
    """``doc`` with only the token/expiry/scope fields of ``claudeAiOauth`` replaced (order kept)."""
    out = dict(doc)
    inner = dict(doc["claudeAiOauth"])
    inner["accessToken"] = tokens.access_token
    inner["refreshToken"] = tokens.refresh_token
    inner["expiresAt"] = tokens.expires_at_ms
    if tokens.refresh_expires_at_ms is not None:
        inner["refreshTokenExpiresAt"] = tokens.refresh_expires_at_ms
    if tokens.scopes is not None and "user:inference" in tokens.scopes:
        inner["scopes"] = list(tokens.scopes)
    out["claudeAiOauth"] = inner
    return out


def dump_document(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def _dir_id(path: str) -> Optional[Tuple[int, float]]:
    """(inode, birth time) of a dir, or None when it cannot be read."""
    try:
        st = os.stat(path)
        return st.st_ino, st.st_birthtime
    except (OSError, AttributeError):
        return None


def _remove_if_ours(path: str, ident: Optional[Tuple[int, float]]) -> None:
    # A dir that Claude Code judged stale and recreated is someone else's lock now: never remove it.
    if ident is None or _dir_id(path) != ident:
        log.warning("event=cc_lock_release_skipped reason=not_ours")
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


class CcLock:
    """Claude Code's refresh lock dirs this process created, in creation order, with their identities."""

    __slots__ = ("_dirs",)

    def __init__(self, dirs: Sequence[Tuple[str, Optional[Tuple[int, float]]]]) -> None:
        self._dirs = list(dirs)

    def touch(self) -> None:
        # Claude Code treats a dir whose mtime is older than CC_LOCK_STALE_S as stale.
        for d, _ident in self._dirs:
            try:
                os.utime(d, None)
            except OSError:
                pass

    def release(self) -> None:
        """Remove each dir still ours (same inode and birth time), legacy first; leave any other in place."""
        for d, ident in reversed(self._dirs):
            _remove_if_ours(d, ident)
        self._dirs = []


def take_cc_lock(home: str, *, now_s: Callable[[], float] = time.time) -> Tuple[Optional[CcLock], Optional[str]]:
    """One non-blocking attempt at Claude Code's new then legacy lock dir; (lock, None) or (None, reason).

    Never waits, never takes over a stale dir and never touches the ``.owner`` record.
    """
    wanted = [os.path.join(home, ".oauth_refresh.lock"), os.path.realpath(home) + ".lock"]
    made: List[Tuple[str, Optional[Tuple[int, float]]]] = []
    reason: Optional[str] = None
    for d in wanted:
        try:
            os.mkdir(d)
        except FileExistsError:
            reason = _busy_or_stale(d, now_s)
        except OSError:
            reason = "cc_lock_busy"
        if reason is not None:
            break
        made.append((d, _dir_id(d)))
    if reason is not None:
        for d, ident in reversed(made):
            _remove_if_ours(d, ident)
        return None, reason
    return CcLock(made), None


def _busy_or_stale(path: str, now_s: Callable[[], float]) -> str:
    try:
        age = now_s() - os.stat(path).st_mtime
    except OSError:
        return "cc_lock_busy"
    return "cc_lock_stale" if age > CC_LOCK_STALE_S else "cc_lock_busy"


def _iso(dt: datetime) -> str:
    # Same formula as daemon._iso; this module must not import the daemon.
    return dt.astimezone(timezone.utc).isoformat()


def _fp(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _no_constant(name: str) -> Any:
    raise ValueError("non-finite constant")


def _parse_item(raw: str) -> Optional[Tuple[dict, dict]]:
    """(document, its claudeAiOauth dict) when the item text has the shape Claude Code stores, else None."""
    try:
        doc = json.loads(raw, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        return None
    exp = oauth.get("expiresAt")
    if not (isinstance(oauth.get("accessToken"), str) and isinstance(oauth.get("refreshToken"), str)
            and isinstance(exp, (int, float)) and not isinstance(exp, bool)):
        return None
    return doc, oauth


def _same_json(a: str, b: str) -> bool:
    try:
        return json.loads(a) == json.loads(b)
    except (ValueError, RecursionError):
        return False


def _oauth_error(body: Any) -> Optional[str]:
    """The OAuth ``error`` code of a response body; the body and its description are never logged."""
    try:
        obj = usage.loads_network_json(body)
    except (ValueError, RecursionError, TypeError):
        return None
    err = obj.get("error") if isinstance(obj, dict) else None
    return err if isinstance(err, str) else None


def _http_detail(status: int, err: Optional[str]) -> str:
    """``status=<n> error=<code>`` for a log line; an error code outside a short lowercase charset shows as ``?``."""
    if err is None:
        shown = "-"
    else:
        shown = err if _LOGGABLE_OAUTH_ERROR.fullmatch(err) else "?"
    return "status=%d error=%s" % (status, shown)


class _State:
    """Per-account in-memory refresh state (never persisted)."""

    __slots__ = ("until", "step_s", "stop_fp", "record", "held", "last_ok_at")

    def __init__(self) -> None:
        self.until = 0.0
        self.step_s = 0.0
        self.stop_fp: Optional[bytes] = None
        self.record: Optional[dict] = None
        self.held: Any = None
        self.last_ok_at: Optional[str] = None


class _Held:
    """A refreshed token set no write could store: the only valid credential, kept in memory with its flock."""

    __slots__ = ("tokens", "posted_fp", "lock", "flush_until", "flush_step_s")

    def __init__(self, tokens: _Tokens, posted_fp: bytes, lock: Any, flush_until: float) -> None:
        self.tokens, self.posted_fp, self.lock = tokens, posted_fp, lock
        self.flush_until, self.flush_step_s = flush_until, 0.0

    def __repr__(self) -> str:
        return "_Held(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("_Held is not picklable")


_REGISTRY_RE = re.compile(r"([0-9]+)\.json")


class Refresher:
    """Refreshes one idle account's OAuth token at a time; every OS/network/Keychain access is a seam."""

    def __init__(self, cfg: Config, *, read_item: Callable[[str, str], Optional[str]],
                 item_exists: Callable[[str, str], Optional[bool]], account_name: Callable[[], str],
                 write_item: Callable[[str, str, str], bool], post: Post,
                 discover: Callable[[Sequence[str]], Sequence[Any]], list_dir: Callable[[str], Sequence[str]],
                 pid_alive: Callable[[int], bool], exists: Callable[[str], bool], lock: Callable[[str], Any],
                 cc_lock: Callable[[str], Tuple[Optional[Any], Optional[str]]], now: Callable[[], datetime],
                 monotonic: Callable[[], float], sleep: Callable[[float], None], log: logging.Logger) -> None:
        self._cfg = cfg
        self._read_item = read_item
        self._item_exists = item_exists
        self._account_name = account_name
        self._write_item = write_item
        self._post = post
        self._discover = discover
        self._list_dir = list_dir
        self._pid_alive = pid_alive
        self._exists = exists
        self._lock = lock
        self._cc_lock = cc_lock
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._log = log
        self._states: Dict[str, _State] = {}

    def __repr__(self) -> str:
        return "Refresher(<redacted>)"

    __str__ = __repr__

    def _state(self, aid: str) -> _State:
        st = self._states.get(aid)
        if st is None:
            st = self._states[aid] = _State()
        return st

    def _now_ms(self) -> int:
        return int(self._now().timestamp() * 1000)

    def _record(self, aid: str, st: _State, state: str, reason: str, detail: str = "") -> dict:
        prev = st.record
        rec = {"state": state, "reason": reason, "at": _iso(self._now()), "last_refreshed_at": st.last_ok_at}
        st.record = rec
        if prev is None or (prev["state"], prev["reason"]) != (state, reason) or state in ("refreshed", "failed", "stopped"):
            self._log.info("event=token_refresh account=%s state=%s reason=%s%s", aid, state, reason,
                           " " + detail if detail else "")
        return dict(rec)

    def _previous(self, st: _State) -> dict:
        return dict(st.record)

    def _backoff(self, st: _State, retry_after_s: Optional[float] = None) -> None:
        step = BACKOFF_START_S if st.step_s <= 0 else min(st.step_s * 2, BACKOFF_MAX_S)
        if retry_after_s is not None:
            step = max(step, min(BACKOFF_MAX_S, retry_after_s))
        st.step_s = step
        st.until = self._monotonic() + step

    def _live(self, profile: Profile, profiles: Sequence[Profile]) -> Optional[str]:
        """"live_session" / "discovery_failed" when the account may be in use, None when certainly idle."""
        try:
            found = self._discover(sessions.session_homes(profiles))
        except Exception:
            return "discovery_failed"
        for s in found:
            p = accounts.profile_for_config_dir(profiles, s.config_dir)
            if p is not None and p.account_id == profile.account_id:
                return "live_session"
        # A session that registered but is not yet discoverable still owns its pid file.
        try:
            names = self._list_dir(os.path.join(profile.home, "sessions"))
        except FileNotFoundError:
            names = []
        except OSError:
            return "discovery_failed"
        for n in names:
            m = _REGISTRY_RE.fullmatch(n) if isinstance(n, str) else None
            if m is not None and self._pid_alive(int(m.group(1))):
                return "live_session"
        return None

    def refresh(self, profile: Profile, profiles: Sequence[Profile], *, network: bool = True) -> dict:
        """Gate, then (when ``network``) refresh one account; always returns a new 4-key record."""
        aid = profile.account_id
        st = self._state(aid)
        kind = None
        try:
            return self._refresh(profile, profiles, st, network)
        except Exception as e:
            # Assign only: the exception (which may quote a secret) is never chained or logged.
            kind = type(e).__name__
        self._log.error("event=token_refresh_error account=%s exc=%s", aid, kind)
        self._backoff(st)
        return self._record(aid, st, "failed", "internal_error")

    def _refresh(self, profile: Profile, profiles: Sequence[Profile], st: _State, network: bool) -> dict:
        aid, svc = profile.account_id, profile.keychain_service
        # A held set is a restore of tokens the server already issued, so it ignores
        # the switch, dry-run and live-session checks below.
        if st.held is not None:
            if not network or self._monotonic() < st.held.flush_until:
                return self._previous(st)
            return self._flush(profile, st)
        if not self._cfg.token_refresh:
            return self._record(aid, st, "skipped", "disabled")
        if self._cfg.dry_run:
            return self._record(aid, st, "skipped", "dry_run")
        if st.record is not None and self._monotonic() < st.until:
            return self._previous(st)
        acct = self._account_name()
        raw = self._read_item(svc, acct)
        if raw is None:
            gone = self._item_exists(svc, acct) is False
            return self._record(aid, st, "skipped", "keychain_item_unclear" if gone else "no_credential")
        parsed = _parse_item(raw)
        if parsed is None:
            return self._record(aid, st, "skipped", "no_credential")
        doc, oauth = parsed
        rt = oauth["refreshToken"]
        if st.stop_fp is not None:
            if _fp(rt) == st.stop_fp and st.record is not None:
                return self._previous(st)
            st.stop_fp = None
        if self._now_ms() + EXPIRY_MARGIN_MS < oauth["expiresAt"]:
            return self._record(aid, st, "not_needed", "fresh")
        if rt == "" or not accounts._TOKEN_RE.fullmatch(rt):
            return self._record(aid, st, "skipped", "no_refresh_token")
        if self._exists(os.path.join(profile.home, ".credentials.json")):
            return self._record(aid, st, "skipped", "plaintext_store")
        why = self._live(profile, profiles)
        if why is not None:
            return self._record(aid, st, "skipped", why)
        if len(accounts.security_add_line(svc, acct, raw)) + PAYLOAD_MARGIN_CHARS > accounts.SECURITY_STDIN_MAX:
            return self._record(aid, st, "skipped", "payload_too_large")
        if not network:
            return self._record(aid, st, "skipped", "deferred")
        return self._section(profile, profiles, st, acct, raw, oauth)

    def _section(self, profile: Profile, profiles: Sequence[Profile], st: _State, acct: str, raw: str,
                 oauth: dict) -> dict:
        aid = profile.account_id
        key = hashlib.sha256(aid.encode("utf-8")).hexdigest()[:32]  # = daemon.key_of(aid)
        rlock = self._lock(os.path.join(self._cfg.state_dir, "locks", "r-%s.lock" % key))
        if rlock is None:
            return self._record(aid, st, "skipped", "locked")
        try:
            t_lock = self._monotonic()
            cc, why = self._cc_lock(profile.home)
            if cc is None:
                return self._record(aid, st, "skipped", why or "cc_lock_busy")
            try:
                return self._locked(profile, profiles, st, acct, oauth, rlock, cc, t_lock)
            finally:
                cc.release()
        finally:
            # A held set keeps its flock so no other rotator process refreshes the account meanwhile.
            if st.held is None or st.held.lock is not rlock:
                rlock.release()

    def _locked(self, profile: Profile, profiles: Sequence[Profile], st: _State, acct: str, oauth: dict,
                rlock: Any, cc: Any, t_lock: float) -> dict:
        aid, svc = profile.account_id, profile.keychain_service
        cc.touch()
        raw2 = self._read_item(svc, acct)
        parsed = _parse_item(raw2) if raw2 is not None else None
        if parsed is None:
            return self._record(aid, st, "skipped", "changed_under_lock")
        _, o2 = parsed
        if (o2["accessToken"] != oauth["accessToken"] or o2["refreshToken"] != oauth["refreshToken"]
                or self._now_ms() + EXPIRY_MARGIN_MS < o2["expiresAt"]):
            return self._record(aid, st, "skipped", "changed_under_lock")
        cc.touch()
        why = self._live(profile, profiles)
        if why is not None:
            return self._record(aid, st, "skipped", why)
        # Probe the exact write path with the exact text before the refresh token is spent.
        cc.touch()
        ok = False
        try:
            ok = self._write_item(svc, acct, raw2) is True
        except Exception:
            ok = False
        if ok:
            cc.touch()
            back = self._read_item(svc, acct)
            ok = back is not None and _same_json(back, raw2)
        if not ok:
            self._backoff(st)
            return self._record(aid, st, "skipped", "keychain_write_unavailable")
        cc.touch()
        why = self._live(profile, profiles)
        if why is not None:
            return self._record(aid, st, "skipped", why)

        rt = o2["refreshToken"]
        stored = [s for s in o2["scopes"] if isinstance(s, str)] if isinstance(o2.get("scopes"), list) else []
        scopes = request_scopes(o2)
        cc.touch()
        res, kind = self._post_once(o2, rt, scopes)
        if (kind is None and res.status == 400 and _oauth_error(res.body) == "invalid_scope" and scopes != stored
                and "user:inference" in stored and self._monotonic() - t_lock < HOLD_BUDGET_S):
            cc.touch()
            res, kind = self._post_once(o2, rt, stored)

        if kind == "network_error":
            self._backoff(st)
            return self._record(aid, st, "failed", "network_error")
        if kind == "outcome_unknown":
            # The server may have rotated the token: retrying could replay a dead one.
            st.stop_fp = _fp(rt)
            self._log.error("event=token_refresh_outcome_unknown account=%s", aid)
            return self._record(aid, st, "stopped", "outcome_unknown")
        status = res.status
        err = _oauth_error(res.body)
        detail = _http_detail(status, err)
        if status == 200:
            tokens = None
            try:
                tokens = parse_token_response(res.body, self._now_ms(), rt)
            except Exception:
                tokens = None
            if tokens is None:
                st.stop_fp = _fp(rt)
                self._log.error("event=token_refresh_malformed_response account=%s", aid)
                return self._record(aid, st, "stopped", "malformed_response")
            return self._write_window(profile, st, acct, tokens, _fp(rt), rlock, cc)
        if status in (400, 401) and err == "invalid_grant":
            st.stop_fp = _fp(rt)
            return self._record(aid, st, "stopped", "invalid_grant")
        if status == 400 and err == "invalid_scope":
            st.stop_fp = _fp(rt)
            return self._record(aid, st, "stopped", "invalid_scope")
        if status == 429:
            self._backoff(st, usage.parse_retry_after(res.retry_after, self._now()))
            return self._record(aid, st, "failed", "rate_limited", detail)
        # A 403 without an OAuth error is an edge block, not a verdict on the token.
        if 500 <= status <= 599 or (status == 403 and err is None):
            self._backoff(st)
            return self._record(aid, st, "failed", "server_error", detail)
        st.stop_fp = _fp(rt)
        return self._record(aid, st, "stopped", "rejected", detail)

    def _post_once(self, oauth: Mapping[str, Any], rt: str, scopes: Sequence[str]
                   ) -> Tuple[Optional[PostResult], Optional[str]]:
        cid = oauth.get("clientId")
        cid = cid if isinstance(cid, str) and cid else CLIENT_ID
        data = json.dumps({"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid,
                           "scope": " ".join(scopes)}, separators=(",", ":")).encode("utf-8")
        res, kind = None, None
        try:
            res = self._post(TOKEN_URL, {"Content-Type": "application/json", "Accept": "application/json",
                                         "User-Agent": USER_AGENT}, data, REFRESH_TIMEOUT_S)
        except Exception as e:
            # Only a failure before anything was sent is safe to retry later.
            pre_send = isinstance(e, urllib.error.URLError) and isinstance(
                getattr(e, "reason", None), (socket.gaierror, ConnectionRefusedError))
            kind = "network_error" if pre_send else "outcome_unknown"
        return res, kind

    def _classify(self, svc: str, acct: str, tokens: _Tokens, posted_fp: bytes) -> Tuple[str, Optional[dict]]:
        """CAS #2: landed / writable / conflict / unreadable, plus the fresh document to write onto."""
        raw = self._read_item(svc, acct)
        if raw is None:
            if self._item_exists(svc, acct) is not False:
                return "unreadable", None
            # rc 44 also covers "found nothing in the search list"; only two in a row drop the set.
            raw = self._read_item(svc, acct)
            if raw is None:
                return ("conflict", None) if self._item_exists(svc, acct) is False else ("unreadable", None)
        try:
            doc = json.loads(raw, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return "unreadable", None
        oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
        rt = oauth.get("refreshToken") if isinstance(oauth, dict) else None
        if not isinstance(rt, str):
            return "unreadable", None
        if _parse_item(raw) is not None and oauth.get("accessToken") == tokens.access_token and rt == tokens.refresh_token:
            return "landed", doc
        if rt == "" or _fp(rt) == posted_fp:
            return "writable", doc
        return "conflict", doc

    def _on_success(self, st: _State) -> None:
        st.until, st.step_s, st.stop_fp = 0.0, 0.0, None
        st.last_ok_at = _iso(self._now())

    def _try_write(self, svc: str, acct: str, doc: dict, tokens: _Tokens, cc: Any) -> bool:
        text = dump_document(build_document(doc, tokens))
        if len(accounts.security_add_line(svc, acct, text)) > accounts.SECURITY_STDIN_MAX:
            return False
        cc.touch()
        ok = self._write_item(svc, acct, text)
        cc.touch()
        back = self._read_item(svc, acct)
        if not ok or back is None:
            return False
        try:
            return json.loads(back) == json.loads(text)
        except (ValueError, RecursionError):
            return False

    def _write_window(self, profile: Profile, st: _State, acct: str, tokens: _Tokens, posted_fp: bytes,
                      rlock: Any, cc: Any) -> dict:
        """Store a 200's token set; on total failure hold it (and ``rlock``) in memory. Never releases a lock."""
        aid, svc = profile.account_id, profile.keychain_service
        outcome = None
        try:
            for n in range(1, WRITE_ATTEMPTS + 1):
                cc.touch()
                cls, doc = self._classify(svc, acct, tokens, posted_fp)
                if cls == "landed":
                    outcome = "ok"
                    break
                if cls == "conflict":
                    outcome = "conflict"
                    break
                if cls == "writable" and self._try_write(svc, acct, doc, tokens, cc):
                    outcome = "ok"
                    break
                if n < WRITE_ATTEMPTS:
                    self._sleep(0.1 * n)
        except Exception as e:
            kind = type(e).__name__
            outcome = "error:" + kind
        if outcome == "ok":
            self._on_success(st)
            return self._record(aid, st, "refreshed", "ok")
        if outcome == "conflict":
            self._log.error("event=token_refresh_conflict account=%s", aid)
            return self._record(aid, st, "failed", "conflict")
        if outcome is not None:
            self._log.error("event=token_refresh_error account=%s exc=%s", aid, outcome[6:])
        st.held = _Held(tokens, posted_fp, rlock, self._monotonic())
        self._log.error("event=token_refresh_write_failed account=%s", aid)
        return self._record(aid, st, "failed", "write_failed")

    def _flush(self, profile: Profile, st: _State) -> dict:
        """One attempt to store a held set; never posts."""
        aid, svc, held = profile.account_id, profile.keychain_service, st.held
        outcome = "failed"
        try:
            acct = self._account_name()
            cc, _why = self._cc_lock(profile.home)
            if cc is not None:
                try:
                    cc.touch()
                    cls, doc = self._classify(svc, acct, held.tokens, held.posted_fp)
                    if cls == "landed":
                        outcome = "ok"
                    elif cls == "conflict":
                        outcome = "conflict"
                    elif cls == "writable" and self._try_write(svc, acct, doc, held.tokens, cc):
                        outcome = "ok"
                finally:
                    cc.release()
        except Exception as e:
            kind = type(e).__name__
            outcome = "error:" + kind
        if outcome in ("ok", "conflict"):
            st.held = None
            held.lock.release()
            if outcome == "ok":
                self._on_success(st)
                return self._record(aid, st, "refreshed", "write_recovered")
            self._log.error("event=token_refresh_conflict account=%s", aid)
            return self._record(aid, st, "failed", "conflict")
        if outcome.startswith("error:"):
            self._log.error("event=token_refresh_error account=%s exc=%s", aid, outcome[6:])
        held.flush_step_s = (FLUSH_BACKOFF_START_S if held.flush_step_s <= 0
                             else min(held.flush_step_s * 2, FLUSH_BACKOFF_MAX_S))
        held.flush_until = self._monotonic() + held.flush_step_s
        return self._record(aid, st, "failed", "write_failed")

    def held_accounts(self, *, due_only: bool = False) -> List[str]:
        """Account ids holding an unwritten token set (only those due for a flush when ``due_only``)."""
        now = self._monotonic()
        return sorted(aid for aid, st in self._states.items()
                      if st.held is not None and (not due_only or now >= st.held.flush_until))
