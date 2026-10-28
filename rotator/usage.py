"""Per-account usage: live usage API with fallback to the .claude.json cache."""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
# Imported by name: fetch_usage's ``http`` parameter shadows any module ``http``.
from http.client import HTTPException
from typing import Mapping, Optional, Tuple

from rotator.accounts import _read_text_utf8, read_credential
from rotator.models import USAGE_BETA_HEADER, USAGE_URL, AccountUsage, Http, Keychain, Now, Profile, ReadText, UsageWindow

log = logging.getLogger("rotator.usage")

# urllib's timeout bounds each blocking socket operation, not the whole
# request; callers must not assume fetch_usage returns within this many seconds.
USAGE_TIMEOUT_S: float = 10.0
_MAX_BODY_BYTES = 1_048_576

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


def _urllib_http(url: str, headers: Mapping[str, str], timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
            data = resp.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        return code, ""
    return status, data.decode("utf-8", errors="replace")


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
) -> AccountUsage:
    """Live usage for ``profile``, else its cached usage, else ``source="unavailable"``.

    Expected failures (no/expired token, HTTP or network errors, bad bodies,
    missing cache) are reported in ``error`` and never raised. One request per
    call, no retry, no redirects, never renews the token. A non-Profile
    argument raises TypeError.
    """
    if not isinstance(profile, Profile):
        raise TypeError("fetch_usage expects a Profile")
    http_fn = http if http is not None else _urllib_http
    now_fn = now if now is not None else _utc_now
    ts = now_fn()
    now_ms = int(ts.timestamp() * 1000)
    live_kind = None
    result = None

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
            status, body = http_fn(USAGE_URL, headers, timeout)
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
                    obj = json.loads(body)
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
                        )

    if result is None:
        usage, reason = read_cached_usage(profile, read_text=read_text)
        if usage is not None:
            result = dataclasses.replace(usage, error=live_kind)
        else:
            result = AccountUsage(
                account_id=profile.account_id,
                source="unavailable",
                fetched_at=ts,
                five_hour=None,
                seven_day=None,
                error="%s;cache_%s" % (live_kind, reason),
            )
    log.debug("usage account=%s source=%s error=%s", profile.account_id, result.source, result.error)
    return result
