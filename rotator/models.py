# contract: t1 owns the implementation
"""Shared data model for claude-account-rotator.

Contract stub: names, fields, types and defaults are frozen. t1 adds the
``__post_init__`` validation and the ``max_utilization`` body; consumers
(t2 sessions, t3 policy, t4 switcher, t5 daemon) must not edit this file.
"""
from __future__ import annotations

import math  # noqa: F401  (used by UsageWindow.__post_init__, added by t1)
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, FrozenSet, Mapping, Optional, Pattern, Tuple

PROFILES_ROOT_DEFAULT: str = os.path.expanduser("~/Library/Application Support/orca/claude-profiles")
KEYCHAIN_SERVICE_PREFIX: str = "Claude Code-credentials-"
USAGE_URL: str = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA_HEADER: str = "oauth-2025-04-20"
USAGE_SOURCES: FrozenSet[str] = frozenset({"live", "cache", "unavailable"})
SESSION_STATUSES: FrozenSet[str] = frozenset({"idle", "busy", "shell", "unknown"})
# Always applied with .fullmatch().
USAGE_ERROR_RE: Pattern[str] = re.compile(
    r"(token_missing|token_expired|http_[0-9]{3}|network|timeout|request_error|bad_json|bad_shape)"
    r"(;cache_(missing|bad_json|bad_shape))?\Z"
)

# Injectable seams. Defaults live in rotator.accounts / rotator.usage and are
# chosen at call time when a caller passes None.
ReadText = Callable[[str], str]  # path -> text; raises OSError
Keychain = Callable[[str], Optional[str]]  # service -> secret JSON or None
Http = Callable[[str, Mapping[str, str], float], Tuple[int, str]]  # (url, headers, timeout) -> (status, body); raises OSError family or http.client.HTTPException
Now = Callable[[], datetime]  # -> aware UTC datetime


@dataclass(frozen=True)
class Profile:
    account_id: str  # == <uuid> dir name == profile.json accountId
    home: str  # absolute; os.path.join(root, account_id, "home"), not normalised
    keychain_service: str  # rotator.accounts.keychain_service_for(home)
    email: Optional[str] = field(default=None, repr=False)
    display_name: Optional[str] = field(default=None, repr=False)
    account_uuid: Optional[str] = field(default=None, repr=False)
    organization_name: Optional[str] = field(default=None, repr=False)
    rate_limit_tier: Optional[str] = None


@dataclass(frozen=True)
class UsageWindow:
    utilization: float  # 0.0..100.0 inclusive; ints are stored as float
    resets_at: Optional[datetime] = None  # aware UTC or None


@dataclass(frozen=True)
class AccountUsage:
    account_id: str
    source: str  # one of USAGE_SOURCES
    fetched_at: datetime  # aware; live/unavailable: poll time; cache: fetchedAtMs
    five_hour: Optional[UsageWindow] = None
    seven_day: Optional[UsageWindow] = None
    error: Optional[str] = None  # None, or USAGE_ERROR_RE.fullmatch(error) is not None

    def max_utilization(self, now: Optional[datetime] = None) -> Optional[float]:
        """Max utilization over present windows; None when both are absent.

        With an aware ``now``, a window whose ``resets_at`` is not None and
        ``<= now`` contributes 0.0. A naive ``now`` raises ValueError.
        """
        raise NotImplementedError("contract stub: t1 implements")


@dataclass(frozen=True)
class Session:
    """One live Claude Code session. t2 builds it; t3/t4/t5 consume it.

    Construct with keywords. ``config_dir`` is the session's CLAUDE_CONFIG_DIR
    as found; when the process env lacks it, t2 sets
    ``os.path.normpath(os.path.expanduser("~/.claude"))``. A Session whose
    config_dir matches no Profile (rotator.accounts.profile_for_config_dir
    returns None) is UNMANAGED: shown in status, never switched. ``status`` is
    one of SESSION_STATUSES; t2 maps an absent or unobserved registry status to
    "unknown". ORCA_AGENT_PANE, ORCA_WORKTREE_ID and procStart are deliberately
    not fields.
    """

    pid: int  # > 0, not bool
    session_id: str  # non-empty
    cwd: str  # non-empty
    status: str  # one of SESSION_STATUSES
    config_dir: str  # absolute path
    argv: Tuple[str, ...]  # a tuple, may be empty
    terminal_handle: Optional[str] = None  # ORCA_TERMINAL_HANDLE, e.g. "term_<id>"
    started_at: Optional[datetime] = None  # aware if present
