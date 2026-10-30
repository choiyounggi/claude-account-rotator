"""Per-account usage: live usage API with fallback to the .claude.json cache."""
from __future__ import annotations

import dataclasses
import email.utils
import json
import logging
import math
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
# Imported by name: fetch_usage's ``http`` parameter shadows any module ``http``.
from http.client import HTTPException
from typing import Callable, Mapping, Optional, Tuple

from rotator.accounts import _read_text_utf8, read_credential
from rotator.models import USAGE_BETA_HEADER, USAGE_URL, AccountUsage, Http, Keychain, Now, Profile, ReadText, UsageWindow

log = logging.getLogger("rotator.usage")

# urllib's timeout bounds each blocking socket operation, not the whole
# request; callers must not assume fetch_usage returns within this many seconds.
USAGE_TIMEOUT_S: float = 10.0
_MAX_BODY_BYTES = 1_048_576
RETRY_AFTER_MAX_LEN = 64
NETWORK_JSON_MAX_DEPTH = 32
_DELAY_SECONDS_RE = re.compile(r"[0-9]{1,10}")

# Python 3.9 fromisoformat rejects "Z", "+0000" and fractions that are not
# exactly 3 or 6 digits, so strings are normalised before parsing.
_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_resets_at(value: object) -> Optional[datetime]:
    """Aware-UTC datetime from an ISO-8601 string; None for anything unparseable. Zoneless strings are UTC."""
    if not isinstance(value, str):
        return None
    m = _ISO_RE.fullmatch(value.strip())
    if m is None:
        return None
    base, frac, off = m.groups()
    text = base
    if frac:
        text += "." + frac[:6].ljust(6, "0")
    if off in ("Z", "z"):
        text += "+00:00"
    elif off:
        text += off[:3] + ":" + off[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_window(obj: object) -> Optional[UsageWindow]:
    """UsageWindow from one ``{"utilization", "resets_at"}`` object; utilization clamped to 0..100; None if unusable."""
    if not isinstance(obj, dict):
        return None
    u = obj.get("utilization")
    if isinstance(u, bool) or not isinstance(u, (int, float)):
        return None
    try:
        u = float(u)
    except OverflowError:
        return None
    if not math.isfinite(u):
        return None
    u = min(100.0, max(0.0, u))
    return UsageWindow(utilization=u, resets_at=parse_resets_at(obj.get("resets_at")))


def parse_windows(obj: object) -> Optional[Tuple[Optional[UsageWindow], Optional[UsageWindow]]]:
    """``(five_hour, seven_day)`` from a usage object; None when it is not a dict or neither window is usable."""
    if not isinstance(obj, dict):
        return None
    five = parse_window(obj.get("five_hour"))
    seven = parse_window(obj.get("seven_day"))
    if five is None and seven is None:
        return None
    return (five, seven)


def parse_fable(obj: object) -> Optional[UsageWindow]:
    """The Fable weekly_scoped window from a usage object's limits[]; None when absent or unusable. Never raises."""
    if not isinstance(obj, dict):
        return None
    limits = obj.get("limits")
    if not isinstance(limits, list):
        return None
    best = None
    for item in limits:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        if not isinstance(scope, dict):
            continue
        model = scope.get("model")
        if not isinstance(model, dict):
            continue
        name = model.get("display_name")
        if not isinstance(name, str) or name.strip().casefold() != "fable":
            continue
        p = item.get("percent")
        if isinstance(p, bool) or not isinstance(p, (int, float)):
            continue
        try:
            pf = float(p)
        except OverflowError:
            continue
        if not math.isfinite(pf):
            continue
        pf = min(100.0, max(0.0, pf))
        w = UsageWindow(utilization=pf, resets_at=parse_resets_at(item.get("resets_at")))
        if best is None or w.utilization > best.utilization:
            best = w
    return best


def parse_retry_after(value: object, now: datetime) -> Optional[float]:
    """Seconds to wait from a Retry-After value (RFC 9110 10.2.3: delay-seconds or HTTP-date); None if unusable."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > RETRY_AFTER_MAX_LEN:
        return None
    if _DELAY_SECONDS_RE.fullmatch(v):
        return float(int(v))
    # An HTTP-date always names a weekday and month; this keeps "12.5" / "-5" away from the date parser.
    if re.search(r"[A-Za-z]", v) is None:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt.utcoffset() is None:
        # A "-0000" zone parses as naive on 3.9.
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - now).total_seconds())


def read_cached_usage(
    profile: Profile, *, read_text: Optional[ReadText] = None
) -> Tuple[Optional[AccountUsage], Optional[str]]:
    """``(usage, None)`` from ``<home>/.claude.json`` cachedUsageUtilization, or ``(None, reason)``.

    ``reason`` is one of ``missing``, ``bad_json``, ``bad_shape``; the usage has ``error=None``.
    """
    rt = read_text if read_text is not None else _read_text_utf8
    try:
        text = rt(os.path.join(profile.home, ".claude.json"))
    except OSError:
        return None, "missing"
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(data, dict):
        return None, "bad_shape"
    if "cachedUsageUtilization" not in data:
        return None, "missing"
    cache = data["cachedUsageUtilization"]
    if not isinstance(cache, dict):
        return None, "bad_shape"
    ms = cache.get("fetchedAtMs")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None, "bad_shape"
    try:
        fetched_at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, "bad_shape"
    windows = parse_windows(cache.get("utilization"))
    if windows is None:
        return None, "bad_shape"
    return (
        AccountUsage(
            account_id=profile.account_id,
            source="cache",
            fetched_at=fetched_at,
            five_hour=windows[0],
            seven_day=windows[1],
            error=None,
            fable=parse_fable(cache.get("utilization")),
        ),
        None,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib's default handler copies Authorization onto the redirected
    # request, sending the bearer token to whatever host Location names.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: str
    retry_after: Optional[str] = None  # raw Retry-After header of an HTTPError response, <= RETRY_AFTER_MAX_LEN chars


HttpEx = Callable[[str, Mapping[str, str], float], HttpResult]


def _urllib_http_ex(url: str, headers: Mapping[str, str], timeout: float) -> HttpResult:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
            data = resp.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as e:
        code = e.code
        raw = None
        try:
            raw = e.headers.get("Retry-After")
        except (AttributeError, TypeError):
            raw = None
        if not isinstance(raw, str) or len(raw) > RETRY_AFTER_MAX_LEN:
            raw = None
        e.close()
        return HttpResult(code, "", raw)
    return HttpResult(status, data.decode("utf-8", errors="replace"), None)


def _urllib_http(url: str, headers: Mapping[str, str], timeout: float) -> Tuple[int, str]:
    r = _urllib_http_ex(url, headers, timeout)
    return r.status, r.body


def loads_network_json(text: object) -> object:
    """``json.loads`` of a network response body, refusing hostile shapes first.

    Raises ValueError, like malformed JSON, for a non-str, a text longer than
    ``_MAX_BODY_BYTES`` or bracket nesting deeper than ``NETWORK_JSON_MAX_DEPTH``;
    the message never contains the body.
    """
    # CPython 3.9's C scanner can abort the whole process ("Cannot recover from
    # stack overflow") on deep nesting instead of raising RecursionError, so
    # catching RecursionError around json.loads is not enough.
    if not isinstance(text, str) or len(text) > _MAX_BODY_BYTES:
        raise ValueError("network JSON body rejected")
    if text.count("[") + text.count("{") > NETWORK_JSON_MAX_DEPTH:
        depth = 0
        in_str = escaped = False
        for ch in text:
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "[" or ch == "{":
                depth += 1
                if depth > NETWORK_JSON_MAX_DEPTH:
                    raise ValueError("network JSON nesting too deep")
            elif ch == "]" or ch == "}":
                depth -= 1
                if depth < 0:
                    raise ValueError("network JSON body rejected")
    return json.loads(text)


def _status_kind(code: object) -> str:
    return "http_%d" % code if type(code) is int and 100 <= code <= 999 else "network"


def _live_kind_for_exception(exc: BaseException) -> str:
    # By type only: str(exc)/exc.args can carry URLs, server text or the
    # Authorization header value.
    if isinstance(exc, urllib.error.HTTPError):
        return _status_kind(exc.code)
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        return "timeout" if isinstance(exc.reason, socket.timeout) else "network"
    if isinstance(exc, (OSError, HTTPException)):
        return "network"
    return "request_error"


def fetch_usage(
    profile: Profile,
    *,
    http: Optional[Http] = None,
    keychain: Optional[Keychain] = None,
    read_text: Optional[ReadText] = None,
    now: Optional[Now] = None,
    timeout: float = USAGE_TIMEOUT_S,
    http_ex: Optional[HttpEx] = None,
) -> AccountUsage:
    """Live usage for ``profile``, else its cached usage, else ``source="unavailable"``.

    Expected failures (no/expired token, HTTP or network errors, bad bodies,
    missing cache) are reported in ``error`` and never raised. One request per
    call, no retry, no redirects, never renews the token. A non-Profile
    argument raises TypeError.

    ``http`` and ``http_ex`` are alternative seams (passing both raises
    ValueError before any I/O). ``retry_after_s`` is set only for a live 429
    answered through ``http_ex`` or the default seam.
    """
    if not isinstance(profile, Profile):
        raise TypeError("fetch_usage expects a Profile")
    if http is not None and http_ex is not None:
        raise ValueError("pass http or http_ex, not both")
    now_fn = now if now is not None else _utc_now
    ts = now_fn()
    now_ms = int(ts.timestamp() * 1000)
    live_kind = None
    result = None
    raw_retry: Optional[str] = None

    cred = read_credential(profile, keychain=keychain)
    if cred is None:
        live_kind = "token_missing"
    elif cred.expires_at_ms <= now_ms:
        live_kind = "token_expired"
    else:
        headers = {
            "Authorization": "Bearer " + cred.access_token,
            "anthropic-beta": USAGE_BETA_HEADER,
            "Accept": "application/json",
        }
        status, body = None, ""
        try:
            if http is not None:
                status, body = http(USAGE_URL, headers, timeout)
            else:
                r = (http_ex if http_ex is not None else _urllib_http_ex)(USAGE_URL, headers, timeout)
                status, body, raw_retry = r.status, r.body, r.retry_after
        except (OSError, HTTPException, ValueError, UnicodeError) as exc:
            # Only assign here: anything raised later inside this block would
            # chain exc (which may embed the header) as __context__.
            live_kind = _live_kind_for_exception(exc)
        if live_kind is None:
            if status != 200:
                live_kind = _status_kind(status)
            else:
                obj = None
                try:
                    obj = loads_network_json(body)
                except (ValueError, RecursionError):
                    live_kind = "bad_json"
                if live_kind is None:
                    windows = parse_windows(obj)
                    if windows is None:
                        live_kind = "bad_shape"
                    else:
                        result = AccountUsage(
                            account_id=profile.account_id,
                            source="live",
                            fetched_at=ts,
                            five_hour=windows[0],
                            seven_day=windows[1],
                            error=None,
                            fable=parse_fable(obj),
                        )

    ra = parse_retry_after(raw_retry, ts) if live_kind == "http_429" else None
    if result is None:
        usage, reason = read_cached_usage(profile, read_text=read_text)
        if usage is not None:
            result = dataclasses.replace(usage, error=live_kind, retry_after_s=ra)
        else:
            result = AccountUsage(
                account_id=profile.account_id,
                source="unavailable",
                fetched_at=ts,
                five_hour=None,
                seven_day=None,
                error="%s;cache_%s" % (live_kind, reason),
                retry_after_s=ra,
            )
    log.debug("usage account=%s source=%s error=%s", profile.account_id, result.source, result.error)
    return result
