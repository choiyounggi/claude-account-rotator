"""Pure account-switching policy: no I/O, no clock reads; now is always a parameter."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import FrozenSet, Mapping, Optional, Sequence, Tuple

from rotator.accounts import profile_for_config_dir
from rotator.models import AccountUsage, Profile, Session, is_fable_model

ACTIONS: FrozenSet[str] = frozenset({"none", "soft", "hard"})

_PCT_FIELDS = ("soft_pct", "hard_pct", "target_headroom_pct")
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


def _usable(usage: Optional[AccountUsage], now: datetime, config: PolicyConfig, *,
            require_fresh: bool, fable: bool = False) -> Tuple[Optional[float], str]:
    """(utilization, "") when usable, else (None, cause).

    Not fresh = older than stale_max_age or fetched_at beyond clock_skew_tolerance in the
    future. With require_fresh=False such a reading still counts as a lower bound, but only
    when every present window has a reset time: usage only accumulates until resets_at, while
    a window without one may have reset since the fetch and over-state the reading.
    fable=True scores max(5h,7d,fable) and, with require_fresh, refuses a reading whose fable
    window is unknown.
    """
    if usage is None:
        return None, "no usage data"
    u = usage.max_utilization_fable(now) if fable else usage.max_utilization(now)
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
        label = "max(5h,7d)"
    elif own_usage is not None and own_usage.fable is not None:
        label = "max(5h,7d,fable)"
    else:
        label = "max(5h,7d; fable unknown)"
    if session.status not in ("idle", "busy"):
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
    target, tu, fragment = _select_target(own.account_id, "current account", u,
                                          config.soft_pct - config.target_headroom_pct,
                                          profiles, usages, now, config, fable=fable)
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
        return PointerDecision(None, "pointer account %s max(5h,7d)=%s below soft %s" % (
            pid, _pct(u), _pct(config.soft_pct)))
    head = "pointer account %s max(5h,7d)=%s >= soft %s" % (pid, _pct(u), _pct(config.soft_pct))
    target, tu, fragment = _select_target(pid, "pointer account", u, None, profiles, usages, now, config)
    if target is None:
        return PointerDecision(None, head + "; " + fragment)
    return PointerDecision(target, head + "; repoint to %s at %s" % (target.account_id, _pct(tu)))
