"""Pure account-switching policy: no I/O, no clock reads; now is always a parameter."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import FrozenSet, Mapping, Optional, Sequence, Tuple

from rotator.accounts import profile_for_config_dir
from rotator.models import AccountUsage, Profile, Session, UsageWindow, is_fable_model

ACTIONS: FrozenSet[str] = frozenset({"none", "soft", "hard"})

_PCT_FIELDS = ("soft_pct", "hard_pct", "target_headroom_pct")
# Measured on Max 5x accounts: one percent of the weekly window used about this many percent of a 5-hour window.
WEEKLY_TO_5H_RATIO_DEFAULT = 6.0
WEEKLY_TO_5H_RATIO_MAX = 50.0
LIMIT_PCT = 100.0
_DURATION_FIELDS = ("stale_max_age", "cooldown", "clock_skew_tolerance")


@dataclass(frozen=True)
class PolicyConfig:
    """Thresholds in percent (0..100) and non-negative durations; pcts are stored as float."""

    soft_pct: float = 85.0
    hard_pct: float = 95.0
    target_headroom_pct: float = 10.0
    stale_max_age: timedelta = timedelta(minutes=15)
    cooldown: timedelta = timedelta(minutes=5)
    clock_skew_tolerance: timedelta = timedelta(minutes=5)
    # How many percent of a 5-hour window one percent of the weekly window holds; 1.0 scores max(5h, 7d).
    weekly_to_5h_ratio: float = WEEKLY_TO_5H_RATIO_DEFAULT

    def __post_init__(self) -> None:
        for name in _PCT_FIELDS:
            v = getattr(self, name)
            # bool is an int subclass and must be refused.
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError("PolicyConfig.%s must be a number" % name)
        # A NaN fails every comparison, so the chain rejects it too.
        if not (0 <= self.soft_pct <= self.hard_pct <= 100):
            raise ValueError("PolicyConfig requires 0 <= soft_pct <= hard_pct <= 100")
        if not (0 <= self.target_headroom_pct <= self.soft_pct):
            raise ValueError("PolicyConfig requires 0 <= target_headroom_pct <= soft_pct")
        for name in _PCT_FIELDS:
            object.__setattr__(self, name, float(getattr(self, name)))
        for name in _DURATION_FIELDS:
            v = getattr(self, name)
            if not isinstance(v, timedelta) or v < timedelta(0):
                raise ValueError("PolicyConfig.%s must be a non-negative timedelta" % name)
        r = self.weekly_to_5h_ratio
        if isinstance(r, bool) or not isinstance(r, (int, float)) or not (1 <= r <= WEEKLY_TO_5H_RATIO_MAX):
            raise ValueError("PolicyConfig requires 1 <= weekly_to_5h_ratio <= %s" % WEEKLY_TO_5H_RATIO_MAX)
        object.__setattr__(self, "weekly_to_5h_ratio", float(r))


DEFAULT_CONFIG: PolicyConfig = PolicyConfig()


@dataclass(frozen=True)
class Decision:
    """Per-session verdict. target is non-None iff action != "none"; reason is display-only."""

    action: str
    target: Optional[Profile]
    reason: str
    utilization: Optional[float]

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("Decision.action must be one of ACTIONS")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("Decision.reason must be a non-empty str")
        if self.action == "none" and self.target is not None:
            raise ValueError("Decision.target must be None when action is none")
        if self.action != "none" and self.target is None:
            raise ValueError("Decision.target is required when action is soft or hard")


@dataclass(frozen=True)
class PointerDecision:
    """Selected-host pointer verdict. target None = leave the pointer as it is."""

    target: Optional[Profile]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("PointerDecision.reason must be a non-empty str")


def _pct(x: float) -> str:
    """The exact operand a comparison used, so a printed value never contradicts its comparison."""
    return repr(float(x)) + "%"


def _require_aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError("%s must be timezone-aware" % name)


def _window_u(w: Optional[UsageWindow], now: datetime) -> Optional[float]:
    if w is None:
        return None
    return 0.0 if w.resets_at is not None and w.resets_at <= now else w.utilization


def load(usage: AccountUsage, now: datetime, ratio: float, *, fable: bool = False,
         skip_limited: bool = False) -> Optional[float]:
    """How close the account is to being blocked, in percent of a 5-hour window: max(5h, 100 - ratio * (100 -
    7d)), plus the raw fable window for a fable session. The weekly window is scaled because its remaining room
    lasts ratio times longer than the same remaining room in the 5-hour window. A window at the limit still
    scores 100. skip_limited leaves out windows at the limit (the load once they reset). None when no window
    counts."""
    parts = []
    u5, u7 = _window_u(usage.five_hour, now), _window_u(usage.seven_day, now)
    uf = _window_u(usage.fable, now) if fable else None
    for u, weekly in ((u5, False), (u7, True), (uf, False)):
        if u is None or (skip_limited and u >= LIMIT_PCT):
            continue
        parts.append(max(0.0, 100.0 - ratio * (100.0 - u)) if weekly else u)
    return max(parts) if parts else None


def _usable(usage: Optional[AccountUsage], now: datetime, config: PolicyConfig, *,
            require_fresh: bool, fable: bool = False) -> Tuple[Optional[float], str]:
    """(utilization, "") when usable, else (None, cause).

    Not fresh = older than stale_max_age or fetched_at beyond clock_skew_tolerance in the
    future. With require_fresh=False such a reading still counts as a lower bound, but only
    when every present window has a reset time: usage only accumulates until resets_at, while
    a window without one may have reset since the fetch and over-state the reading.
    fable=True adds the fable window to the load and, with require_fresh, refuses a reading whose fable
    window is unknown.
    """
    if usage is None:
        return None, "no usage data"
    u = load(usage, now, config.weekly_to_5h_ratio, fable=fable)
    if usage.source == "unavailable" or u is None:
        if usage.error:
            return None, "usage unavailable (%s)" % usage.error
        return None, "usage unavailable"
    age = now - usage.fetched_at
    stale_cause = ""
    if -age > config.clock_skew_tolerance:
        stale_cause = "usage fetched_at %ds in the future > tolerance %ds" % (
            int((-age).total_seconds()), int(config.clock_skew_tolerance.total_seconds()))
    elif age > config.stale_max_age:
        stale_cause = "usage stale: age %ds > max %ds" % (
            int(age.total_seconds()), int(config.stale_max_age.total_seconds()))
    if not stale_cause:
        if fable and require_fresh and usage.fable is None:
            return None, "fable unknown"
        return u, ""
    if require_fresh:
        return None, stale_cause
    windows = [w for w in (usage.five_hour, usage.seven_day) if w is not None]
    if fable and usage.fable is not None:
        windows.append(usage.fable)
    if any(w.resets_at is None for w in windows):
        return None, stale_cause + "; a window has no reset time, not a lower bound"
    return u, ""


# Waiting out a limit moves only to an account that resets this much sooner: a relaunch costs more than a few
# minutes saved.
RESET_WAIT_MARGIN = timedelta(minutes=5)


def _limit_lifts_at(usage: AccountUsage, now: datetime, fable: bool) -> Tuple[bool, Optional[datetime]]:
    """(at the limit, when every window at the limit has reset). The time is None when no window is at the
    limit or one of them has no reset time."""
    windows = [usage.five_hour, usage.seven_day] + ([usage.fable] if fable else [])
    limited = [w for w in windows if w is not None and w.utilization >= LIMIT_PCT
               and not (w.resets_at is not None and w.resets_at <= now)]
    if not limited:
        return False, None
    if any(w.resets_at is None for w in limited):
        return True, None
    return True, max(w.resets_at for w in limited if w.resets_at is not None)


def _soonest_reset(exclude_account_id: str, own_usage: Optional[AccountUsage], profiles: Sequence[Profile],
                   usages: Mapping[str, AccountUsage], now: datetime, config: PolicyConfig, *,
                   fable: bool) -> Tuple[Optional[Profile], str]:
    """When every account is at its limit: the account whose limit lifts first, if that is at least
    RESET_WAIT_MARGIN sooner than the current account's (any known time when the current account's is unknown)
    and its load after the lift is below hard_pct. Returns (target, reason fragment)."""
    own_at = _limit_lifts_at(own_usage, now, fable)[1] if own_usage is not None else None
    best: Optional[Tuple[datetime, str, Profile]] = None
    for p in profiles:
        if p.account_id == exclude_account_id:
            continue
        u = usages.get(p.account_id)
        cu, _ = _usable(u, now, config, require_fresh=True, fable=fable)
        if cu is None or u is None:
            continue
        limited, at = _limit_lifts_at(u, now, fable)
        if not limited or at is None:
            continue
        after = load(u, now, config.weekly_to_5h_ratio, fable=fable, skip_limited=True)
        if after is not None and after >= config.hard_pct:
            # Lifting one limit leaves it at the hard tier anyway: the session would move on right after.
            continue
        if best is None or (at, p.account_id) < (best[0], best[1]):
            best = (at, p.account_id, p)
    if best is None:
        return None, "no account resets sooner"
    if own_at is not None and best[0] > own_at - RESET_WAIT_MARGIN:
        return None, "current account resets at %s, no account resets %ds sooner (best %s at %s)" % (
            own_at.isoformat(), int(RESET_WAIT_MARGIN.total_seconds()), best[1], best[0].isoformat())
    return best[2], "all accounts at the limit; %s resets first at %s" % (best[1], best[0].isoformat())


def _select_target(exclude_account_id: str, subject: str, own_u: float, line: Optional[float],
                   profiles: Sequence[Profile], usages: Mapping[str, AccountUsage], now: datetime,
                   config: PolicyConfig, *, fable: bool = False) -> Tuple[Optional[Profile], Optional[float], str]:
    """Lowest fresh utilization strictly below own_u (ties by account_id) and, when line is
    not None, at or below line. Returns (target, target_u, "") or (None, None, reason fragment).
    subject names the caller's account in the fragment.
    """
    others = [p for p in profiles if p.account_id != exclude_account_id]
    if not others:
        return None, None, "no target: no other account"
    scored = []
    rejected = []
    for p in others:
        cu, why = _usable(usages.get(p.account_id), now, config, require_fresh=True, fable=fable)
        if cu is not None:
            scored.append((cu, p.account_id, p))
        else:
            rejected.append((p.account_id, why))
    scored.sort(key=lambda t: (t[0], t[1]))
    if not scored:
        return None, None, "no target: no fresh candidate data (" + "; ".join(
            "%s: %s" % (aid, why) for aid, why in sorted(rejected)) + ")"
    best_u, best_id, best = scored[0]
    if best_u >= own_u:
        return None, None, "no target: %s is the healthiest (%s <= best candidate %s at %s)" % (
            subject, _pct(own_u), best_id, _pct(best_u))
    if line is not None and best_u > line:
        return None, None, "no target: best candidate %s at %s > headroom line %s" % (
            best_id, _pct(best_u), _pct(line))
    return best, best_u, ""


def decide(session: Session, profiles: Sequence[Profile], usages: Mapping[str, AccountUsage],
           now: datetime, *, config: PolicyConfig = DEFAULT_CONFIG,
           last_switch_at: Optional[datetime] = None) -> Decision:
    """Switch verdict for one session; usages is keyed by account_id.

    Checks run in a fixed order and the first that applies wins: unmanaged, session status,
    own usage not known, below soft, cooldown, busy on the soft tier, no target.
    Raises ValueError when now or last_switch_at is naive.
    """
    _require_aware(now, "now")
    if last_switch_at is not None:
        _require_aware(last_switch_at, "last_switch_at")
    own = profile_for_config_dir(profiles, session.config_dir)
    if own is None:
        return Decision("none", None, "unmanaged session: config_dir maps to no profile", None)
    fable = is_fable_model(session.model)
    own_usage = usages.get(own.account_id)
    u, cause = _usable(own_usage, now, config, require_fresh=False, fable=fable)
    if not fable:
        label = "load(5h,7d)"
    elif own_usage is not None and own_usage.fable is not None:
        label = "load(5h,7d,fable)"
    else:
        label = "load(5h,7d; fable unknown)"
    # "shell" is Claude Code idle at its prompt with background shells running; switch it like idle.
    if session.status not in ("idle", "busy", "shell"):
        return Decision("none", None,
                        "session status %s: no running Claude turn to switch" % session.status, u)
    if u is None:
        return Decision("none", None, "current account %s: %s" % (own.account_id, cause), u)
    if u < config.soft_pct:
        return Decision("none", None,
                        "%s=%s below soft %s" % (label, _pct(u), _pct(config.soft_pct)), u)
    tier, th = ("hard", config.hard_pct) if u >= config.hard_pct else ("soft", config.soft_pct)
    head = "%s=%s >= %s %s" % (label, _pct(u), tier, _pct(th))
    if last_switch_at is not None and now - last_switch_at < config.cooldown:
        return Decision("none", None, head + "; cooldown: last switch %ds ago < %ds" % (
            int((now - last_switch_at).total_seconds()), int(config.cooldown.total_seconds())), u)
    if tier == "soft" and session.status == "busy":
        return Decision("none", None, head + "; session busy, waiting for idle", u)
    # The hard tier is about to stop, so any account with more room will do; below the limit the target must
    # be out of the hard tier itself, or two accounts near each other would trade the session back and forth.
    if tier == "soft":
        line: Optional[float] = config.soft_pct - config.target_headroom_pct
    elif u < LIMIT_PCT:
        line = math.nextafter(config.hard_pct, -math.inf)
    else:
        line = None
    target, tu, fragment = _select_target(own.account_id, "current account", u, line,
                                          profiles, usages, now, config, fable=fable)
    if target is None and tier == "hard" and u >= LIMIT_PCT:
        wait, why = _soonest_reset(own.account_id, own_usage, profiles, usages, now, config, fable=fable)
        if wait is not None and session.status == "busy":
            return Decision("none", None, head + "; session busy, waiting for idle; " + why, u)
        if wait is not None:
            # Soft: the wait target is at its limit too, so interrupting a turn would gain nothing.
            return Decision("soft", wait, head + "; session %s; %s" % (session.status, why), u)
        fragment += "; " + why
    if target is None:
        return Decision("none", None, head + "; session %s; " % session.status + fragment, u)
    return Decision(tier, target, head + "; session %s; target %s at %s" % (
        session.status, target.account_id, _pct(tu)), u)


def decide_pointer(pointer_profile: Optional[Profile], profiles: Sequence[Profile],
                   usages: Mapping[str, AccountUsage], now: datetime, *,
                   config: PolicyConfig = DEFAULT_CONFIG) -> PointerDecision:
    """Whether the global selected-host pointer should move; usages is keyed by account_id.

    pointer_profile is the profile the pointer names, None when it names none. Repoints only
    when the pointer account is at or above soft, to the lowest fresh account strictly below
    it (no headroom line, no cooldown, no session status). Raises ValueError on a naive now.
    """
    _require_aware(now, "now")
    if pointer_profile is None:
        return PointerDecision(None, "unmanaged pointer: selected-host names no profile")
    pid = pointer_profile.account_id
    u, cause = _usable(usages.get(pid), now, config, require_fresh=False)
    if u is None:
        return PointerDecision(None, "pointer account %s: %s" % (pid, cause))
    if u < config.soft_pct:
        return PointerDecision(None, "pointer account %s load(5h,7d)=%s below soft %s" % (
            pid, _pct(u), _pct(config.soft_pct)))
    head = "pointer account %s load(5h,7d)=%s >= soft %s" % (pid, _pct(u), _pct(config.soft_pct))
    target, tu, fragment = _select_target(pid, "pointer account", u, None, profiles, usages, now, config)
    if target is None:
        return PointerDecision(None, head + "; " + fragment)
    return PointerDecision(target, head + "; repoint to %s at %s" % (target.account_id, _pct(tu)))
