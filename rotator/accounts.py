# contract: t1 owns the implementation
"""Profile discovery and Keychain credential lookup (contract stub: signatures only)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import re
import subprocess
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from rotator.models import KEYCHAIN_SERVICE_PREFIX, PROFILES_ROOT_DEFAULT, Keychain, Profile, ReadText

KEYCHAIN_TIMEOUT_S: float = 10.0
SECURITY_BIN = "/usr/bin/security"
# `security -i` reads one command line into a fixed buffer; a longer line is
# truncated, so the writer refuses it instead of falling back to argv.
SECURITY_STDIN_MAX = 4032
KEYCHAIN_WRITE_TIMEOUT_S: float = 5.0
KEYCHAIN_ACCOUNT_RE = re.compile(r"[a-zA-Z0-9._-]+")
KEYCHAIN_ACCOUNT_FALLBACK = "claude-code-user"
SECURITY_RC_NOT_FOUND = 44
_SERVICE_RE = re.compile(re.escape(KEYCHAIN_SERVICE_PREFIX) + "[0-9a-f]{8}")

log = logging.getLogger("rotator.accounts")

# Header-safe printable ASCII: a token with CR/LF, space or non-ASCII makes
# http.client raise an exception whose message embeds the header value.
# Always applied with .fullmatch().
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,4096}")

def _read_text_utf8(path: str) -> str:
    # UnicodeDecodeError is a ValueError: re-raised as OSError so every
    # `except OSError` read site (the ReadText seam contract) still catches it.
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise OSError("file is not valid UTF-8") from None


def keychain_service_for(home: str) -> str:
    """KEYCHAIN_SERVICE_PREFIX + sha256(home utf-8).hexdigest()[:8], on ``home`` exactly as given."""
    # Claude Code hashes the path string it was given; realpath/normpath here
    # would name a different Keychain item.
    return KEYCHAIN_SERVICE_PREFIX + hashlib.sha256(home.encode("utf-8")).hexdigest()[:8]


def _read_identity(home: str, read_text: ReadText) -> Dict[str, Optional[str]]:
    identity: Dict[str, Optional[str]] = {
        "email": None,
        "display_name": None,
        "account_uuid": None,
        "organization_name": None,
        "rate_limit_tier": None,
    }
    try:
        text = read_text(os.path.join(home, ".claude.json"))
    except OSError:
        return identity
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return identity
    if not isinstance(data, dict) or not isinstance(data.get("oauthAccount"), dict):
        return identity
    account = data["oauthAccount"]
    for key, field in (
        ("emailAddress", "email"),
        ("displayName", "display_name"),
        ("accountUuid", "account_uuid"),
        ("organizationName", "organization_name"),
        ("userRateLimitTier", "rate_limit_tier"),
    ):
        value = account.get(key)
        if isinstance(value, str):
            identity[field] = value
    return identity


def discover_profiles(root: Optional[str] = None, *, read_text: Optional[ReadText] = None) -> List[Profile]:
    """Profiles under ``root`` (default PROFILES_ROOT_DEFAULT), sorted by account_id. Read-only; never raises for bad entries."""
    rt = read_text if read_text is not None else _read_text_utf8
    root = PROFILES_ROOT_DEFAULT if root is None else root
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        root = os.path.join(os.getcwd(), root)
    # A trailing slash would change every home string and therefore every
    # Keychain service name (see keychain_service_for).
    root = root.rstrip("/") or "/"
    try:
        names = os.listdir(root)
    except OSError:
        return []
    profiles = []
    for name in names:
        try:
            canonical = str(uuid.UUID(name))
        except ValueError:
            canonical = None
        if canonical != name:
            log.debug("skip profile entry %s: %s", name, "not_uuid")
            continue
        entry = os.path.join(root, name)
        home = os.path.join(entry, "home")
        if not os.path.isdir(entry) or not os.path.isdir(home):
            log.debug("skip profile entry %s: %s", name, "no_home")
            continue
        try:
            text = rt(os.path.join(entry, "profile.json"))
        except OSError:
            log.debug("skip profile entry %s: %s", name, "profile_unreadable")
            continue
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            log.debug("skip profile entry %s: %s", name, "profile_bad_json")
            continue
        if not isinstance(data, dict) or data.get("accountId") != name:
            log.debug("skip profile entry %s: %s", name, "profile_mismatch")
            continue
        profiles.append(
            Profile(
                account_id=name,
                home=home,
                keychain_service=keychain_service_for(home),
                **_read_identity(home, rt),
            )
        )
    return sorted(profiles, key=lambda p: p.account_id)


def profile_for_config_dir(profiles: Sequence[Profile], config_dir: str) -> Optional[Profile]:
    """First profile whose normpath(home) == normpath(config_dir); None = unmanaged session. Pure."""
    target = os.path.normpath(config_dir)
    for p in profiles:
        if os.path.normpath(p.home) == target:
            return p
    return None


class Credential:
    """In-memory OAuth access token holder. Not a dataclass; repr/str redact the token; not picklable."""

    __slots__ = ("_access_token", "expires_at_ms")

    def __init__(self, access_token: str, expires_at_ms: int) -> None:
        self._access_token = access_token
        self.expires_at_ms = expires_at_ms

    @property
    def access_token(self) -> str:
        return self._access_token

    def __repr__(self) -> str:
        return "Credential(access_token=<redacted>, expires_at_ms=%d)" % self.expires_at_ms

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Credential is not picklable")


def _security_find_password(service: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=KEYCHAIN_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        # TimeoutExpired carries partial stdout (the secret): it must not escape.
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def read_credential(profile: Profile, *, keychain: Optional[Keychain] = None) -> Optional[Credential]:
    """Read accessToken/expiresAt from the profile's Keychain item; None when unavailable. Never raises for expected failures."""
    kc = keychain if keychain is not None else _security_find_password
    svc = profile.keychain_service
    kind = None
    raw = None
    token = None
    exp_ms = 0
    try:
        raw = kc(svc)
    except (subprocess.SubprocessError, OSError):
        # Only assign here: work done inside the except block would chain the
        # seam's exception (which may hold the secret) as __context__.
        kind = "keychain_error"
    if kind is None and (not isinstance(raw, str) or raw == ""):
        kind = "not_found"
    data = None
    if kind is None:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            kind = "bad_json"
    if kind is None:
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        exp = oauth.get("expiresAt") if isinstance(oauth, dict) else None
        if not (
            isinstance(token, str)
            and _TOKEN_RE.fullmatch(token)
            and isinstance(exp, (int, float))
            and not isinstance(exp, bool)
        ):
            kind = "bad_shape"
        else:
            try:
                exp_ms = int(exp)
            except (OverflowError, ValueError):
                kind = "bad_shape"
    if kind is not None:
        log.debug("credential unavailable for %s: %s", svc, kind)
        return None
    log.debug("credential loaded for %s (expires_at_ms=%d)", svc, exp_ms)
    return Credential(token, exp_ms)


def _passwd_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return ""


def claude_code_keychain_account(*, environ: Mapping[str, str] = os.environ,
                                 passwd_name: Callable[[], str] = _passwd_name) -> str:
    """The ``-a`` account name Claude Code stores its credentials item under.

    A set, non-empty ``$USER`` wins before the name check, so an invalid
    ``$USER`` gives the fallback rather than the passwd name (Claude Code's order).
    """
    name = environ.get("USER") or passwd_name()
    return name if isinstance(name, str) and KEYCHAIN_ACCOUNT_RE.fullmatch(name) else KEYCHAIN_ACCOUNT_FALLBACK


def _check_names(service: str, account: str) -> None:
    if not (isinstance(service, str) and _SERVICE_RE.fullmatch(service)
            and isinstance(account, str) and KEYCHAIN_ACCOUNT_RE.fullmatch(account)):
        raise ValueError("bad keychain item name")


def read_keychain_item(service: str, account: str, *, run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """The item's stored text (one trailing newline removed), or None on any failure."""
    _check_names(service, account)
    try:
        proc = run([SECURITY_BIN, "find-generic-password", "-a", account, "-s", service, "-w"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   encoding="utf-8", timeout=KEYCHAIN_TIMEOUT_S, check=False)
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        # TimeoutExpired carries partial stdout (the secret): never let it escape.
        proc = None
    if proc is None or proc.returncode != 0 or not isinstance(proc.stdout, str):
        return None
    out = proc.stdout
    return out[:-1] if out.endswith("\n") else out


def keychain_item_exists(service: str, account: str, *, run: Callable[..., Any] = subprocess.run) -> Optional[bool]:
    """Attributes-only lookup: True found, False rc 44 (not found), None unknown."""
    _check_names(service, account)
    try:
        proc = run([SECURITY_BIN, "find-generic-password", "-a", account, "-s", service],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=KEYCHAIN_TIMEOUT_S, check=False)
    except (subprocess.SubprocessError, OSError):
        proc = None
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == SECURITY_RC_NOT_FOUND:
        return False
    return None


def security_add_line(service: str, account: str, secret_json: str) -> str:
    """The ``security -i`` stdin line that upserts the item with hex-encoded ``secret_json``."""
    _check_names(service, account)
    return 'add-generic-password -U -a "%s" -s "%s" -X "%s"\n' % (
        account, service, secret_json.encode("utf-8").hex())


def write_keychain_item(service: str, account: str, secret_json: str, *,
                        run: Callable[..., Any] = subprocess.run) -> bool:
    """Upsert the item through ``security -i`` stdin (never argv); True only on rc 0."""
    line = security_add_line(service, account, secret_json)
    if len(line) > SECURITY_STDIN_MAX:
        return False
    try:
        # Output is discarded so no `security` message can echo the hex back.
        proc = run([SECURITY_BIN, "-i"], input=line, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   encoding="ascii", timeout=KEYCHAIN_WRITE_TIMEOUT_S, check=False)
    except (subprocess.SubprocessError, OSError):
        proc = None
    return proc is not None and proc.returncode == 0
