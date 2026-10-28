import dataclasses
import unittest
from datetime import datetime, timedelta, timezone

from rotator.models import AccountUsage, Profile, Session, UsageWindow
from rotator.policy import ACTIONS, DEFAULT_CONFIG, Decision, PointerDecision, PolicyConfig, decide, decide_pointer

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _pid(n):
    return "00000000-0000-4000-8000-%012d" % n


def _profile(n):
    pid = _pid(n)
    return Profile(account_id=pid, home="/x/claude-profiles/%s/home" % pid,
                   keychain_service="Claude Code-credentials-test%d" % n)


def _usage(n, five, seven=None, *, source="live", fetched_at=NOW,
           five_resets_at=None, seven_resets_at=None, error=None):
    if source == "unavailable":
        return AccountUsage(account_id=_pid(n), source="unavailable", fetched_at=fetched_at,
                            error=error or "network;cache_missing")
    return AccountUsage(account_id=_pid(n), source=source, fetched_at=fetched_at,
                        five_hour=UsageWindow(utilization=five, resets_at=five_resets_at),
                        seven_day=None if seven is None else UsageWindow(utilization=seven, resets_at=seven_resets_at),
                        error=error)


def _usages(*items):
    return {u.account_id: u for u in items}


def _session(status, n=1, config_dir=None):
    return Session(pid=1000 + n, session_id="sess-%d" % n, cwd="/x/work", status=status,
                   config_dir=config_dir or _profile(n).home, argv=("claude",))


class TestPolicyConfig(unittest.TestCase):
    def test_defaults(self):
        c = PolicyConfig()
        self.assertEqual(c.soft_pct, 85.0)
        self.assertEqual(c.hard_pct, 95.0)
        self.assertEqual(c.target_headroom_pct, 10.0)
        self.assertEqual(c.stale_max_age, timedelta(minutes=15))
        self.assertEqual(c.cooldown, timedelta(minutes=5))
        self.assertEqual(c.clock_skew_tolerance, timedelta(minutes=5))
        self.assertEqual(DEFAULT_CONFIG, PolicyConfig())

    def test_int_pcts_coerced_to_float(self):
        c = PolicyConfig(soft_pct=70, hard_pct=80)
        self.assertEqual(c.soft_pct, 70.0)
        self.assertEqual(c.hard_pct, 80.0)
        self.assertIs(type(c.soft_pct), float)
        self.assertIs(type(c.hard_pct), float)
        self.assertIs(type(c.target_headroom_pct), float)

    def test_soft_above_hard_rejected(self):
        with self.assertRaisesRegex(ValueError, "soft_pct <= hard_pct <= 100"):
            PolicyConfig(soft_pct=96, hard_pct=95)

    def test_hard_above_100_rejected(self):
        with self.assertRaisesRegex(ValueError, "soft_pct <= hard_pct <= 100"):
            PolicyConfig(hard_pct=101)

    def test_negative_soft_rejected(self):
        with self.assertRaisesRegex(ValueError, "0 <= soft_pct <= hard_pct"):
            PolicyConfig(soft_pct=-1)

    def test_negative_headroom_rejected(self):
        with self.assertRaisesRegex(ValueError, "0 <= target_headroom_pct <= soft_pct"):
            PolicyConfig(target_headroom_pct=-1)

    def test_headroom_above_soft_rejected(self):
        with self.assertRaisesRegex(ValueError, "0 <= target_headroom_pct <= soft_pct"):
            PolicyConfig(soft_pct=10, target_headroom_pct=20)

    def test_bool_pct_rejected(self):
        # target_headroom_pct=False and soft_pct=True with headroom 0 pass both range
        # chains, so only the type check can reject them.
        cases = [
            ("soft_pct", {"soft_pct": True}),
            ("soft_pct", {"soft_pct": True, "target_headroom_pct": 0}),
            ("hard_pct", {"hard_pct": True, "soft_pct": 0, "target_headroom_pct": 0}),
            ("target_headroom_pct", {"target_headroom_pct": False}),
        ]
        for name, kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "PolicyConfig.%s must be a number" % name):
                    PolicyConfig(**kwargs)

    def test_non_number_pct_rejected(self):
        with self.assertRaisesRegex(ValueError, "PolicyConfig.hard_pct must be a number"):
            PolicyConfig(hard_pct="95")

    def test_nan_rejected(self):
        for name in ("soft_pct", "hard_pct"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "soft_pct <= hard_pct <= 100"):
                    PolicyConfig(**{name: float("nan")})

    def test_nan_headroom_rejected(self):
        with self.assertRaisesRegex(ValueError, "target_headroom_pct <= soft_pct"):
            PolicyConfig(target_headroom_pct=float("nan"))

    def test_negative_timedeltas_rejected(self):
        for name in ("stale_max_age", "cooldown", "clock_skew_tolerance"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "PolicyConfig.%s must be a non-negative timedelta" % name):
                    PolicyConfig(**{name: timedelta(seconds=-1)})

    def test_non_timedelta_rejected(self):
        with self.assertRaisesRegex(ValueError, "PolicyConfig.cooldown must be a non-negative timedelta"):
            PolicyConfig(cooldown=300)

    def test_soft_equal_hard_allowed(self):
        c = PolicyConfig(soft_pct=90, hard_pct=90)
        self.assertEqual((c.soft_pct, c.hard_pct), (90.0, 90.0))

    def test_headroom_equal_soft_allowed(self):
        c = PolicyConfig(soft_pct=10, target_headroom_pct=10)
        self.assertEqual((c.soft_pct, c.target_headroom_pct), (10.0, 10.0))

    def test_pct_range_ends_allowed(self):
        top = PolicyConfig(soft_pct=100, hard_pct=100)
        self.assertEqual((top.soft_pct, top.hard_pct), (100.0, 100.0))
        bottom = PolicyConfig(soft_pct=0, hard_pct=0, target_headroom_pct=0)
        self.assertEqual((bottom.soft_pct, bottom.hard_pct, bottom.target_headroom_pct), (0.0, 0.0, 0.0))

    def test_zero_timedeltas_allowed(self):
        z = timedelta(0)
        c = PolicyConfig(stale_max_age=z, cooldown=z, clock_skew_tolerance=z)
        self.assertEqual((c.stale_max_age, c.cooldown, c.clock_skew_tolerance), (z, z, z))

    def test_frozen(self):
        c = PolicyConfig()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            c.soft_pct = 50.0


class TestDecision(unittest.TestCase):
    def test_actions_set(self):
        self.assertEqual(ACTIONS, frozenset({"none", "soft", "hard"}))

    def test_valid_soft(self):
        d = Decision("soft", _profile(2), "why", 86.0)
        self.assertEqual(d.action, "soft")
        self.assertEqual(d.target, _profile(2))
        self.assertEqual(d.reason, "why")
        self.assertEqual(d.utilization, 86.0)

    def test_valid_none(self):
        d = Decision("none", None, "why", None)
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIsNone(d.utilization)

    def test_unknown_action_rejected(self):
        with self.assertRaisesRegex(ValueError, "Decision.action must be one of ACTIONS"):
            Decision("go", _profile(2), "why", 86.0)

    def test_empty_reason_rejected(self):
        with self.assertRaisesRegex(ValueError, "Decision.reason must be a non-empty str"):
            Decision("none", None, "", None)

    def test_non_str_reason_rejected(self):
        # 123 and ["x"] are truthy, so only the str check can reject them.
        for reason in (None, 123, ["x"]):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, "Decision.reason must be a non-empty str"):
                    Decision("none", None, reason, None)

    def test_soft_without_target_rejected(self):
        with self.assertRaisesRegex(ValueError, "Decision.target is required when action is soft or hard"):
            Decision("soft", None, "why", 86.0)

    def test_hard_without_target_rejected(self):
        with self.assertRaisesRegex(ValueError, "Decision.target is required when action is soft or hard"):
            Decision("hard", None, "why", 96.0)

    def test_none_with_target_rejected(self):
        with self.assertRaisesRegex(ValueError, "Decision.target must be None when action is none"):
            Decision("none", _profile(2), "why", 50.0)

    def test_equal_by_value(self):
        self.assertEqual(Decision("hard", _profile(2), "why", 96.0),
                         Decision("hard", _profile(2), "why", 96.0))


class TestPointerDecision(unittest.TestCase):
    def test_valid_with_target(self):
        d = PointerDecision(_profile(2), "why")
        self.assertEqual(d.target, _profile(2))
        self.assertEqual(d.reason, "why")

    def test_valid_without_target(self):
        d = PointerDecision(None, "why")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "why")

    def test_empty_reason_rejected(self):
        with self.assertRaisesRegex(ValueError, "PointerDecision.reason must be a non-empty str"):
            PointerDecision(None, "")

    def test_non_str_reason_rejected(self):
        for reason in (None, 123, ["x"]):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, "PointerDecision.reason must be a non-empty str"):
                    PointerDecision(_profile(2), reason)


def _num(account_id):
    return int(account_id[-12:])


def _decide(status, *usages, profiles=None, session=None, now=NOW, **kw):
    """decide() for own = _profile(1); profiles default to own + every candidate usage given."""
    if profiles is None:
        profiles = [_profile(1)] + [_profile(_num(u.account_id)) for u in usages if u.account_id != _pid(1)]
    return decide(session or _session(status), profiles, _usages(*usages), now, **kw)


H2 = timedelta(hours=2)
D3 = timedelta(days=3)


class TestDecideUsability(unittest.TestCase):
    def test_past_reset_window_counts_zero(self):
        d = _decide("idle", _usage(1, 90, 85, five_resets_at=NOW), _usage(2, 40))
        self.assertEqual(d.action, "soft")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertEqual(d.utilization, 85.0)
        self.assertIn("max(5h,7d)=85.0% >= soft 85.0%", d.reason)

    def test_own_missing_usage(self):
        d = _decide("idle", _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "current account %s: no usage data" % _pid(1))
        self.assertIsNone(d.utilization)

    def test_own_unavailable(self):
        d = _decide("idle", _usage(1, None, source="unavailable"), _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "current account %s: usage unavailable (network;cache_missing)" % _pid(1))
        self.assertIsNone(d.utilization)

    def test_stale_own_with_resets_triggers(self):
        own = _usage(1, 96, 50, source="cache", fetched_at=NOW - timedelta(minutes=20),
                     five_resets_at=NOW + H2, seven_resets_at=NOW + D3)
        d = _decide("busy", own, _usage(2, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertEqual(d.utilization, 96.0)

    def test_stale_own_without_reset_time_not_known(self):
        own = _usage(1, 96, 50, source="cache", fetched_at=NOW - timedelta(minutes=20),
                     five_resets_at=None, seven_resets_at=NOW + D3)
        d = _decide("busy", own, _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "current account %s: usage stale: age 1200s > max 900s; "
                                   "a window has no reset time, not a lower bound" % _pid(1))
        self.assertIsNone(d.utilization)

    def test_stale_own_without_seven_day_reset_not_known(self):
        # every present window needs a reset time, not just the five-hour one
        own = _usage(1, 96, 50, source="cache", fetched_at=NOW - timedelta(minutes=20),
                     five_resets_at=NOW + H2, seven_resets_at=None)
        d = _decide("busy", own, _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "current account %s: usage stale: age 1200s > max 900s; "
                                   "a window has no reset time, not a lower bound" % _pid(1))
        self.assertIsNone(d.utilization)

    def test_future_own_without_seven_day_reset_not_known(self):
        own = _usage(1, 96, 50, fetched_at=NOW + timedelta(minutes=6),
                     five_resets_at=NOW + H2, seven_resets_at=None)
        d = _decide("busy", own, _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("in the future", d.reason)
        self.assertIn("no reset time", d.reason)
        self.assertIsNone(d.utilization)

    def test_stale_own_with_passed_reset_reads_lower(self):
        own = _usage(1, 96, 10, source="cache", fetched_at=NOW - timedelta(minutes=20),
                     five_resets_at=NOW - timedelta(minutes=1), seven_resets_at=NOW + D3)
        d = _decide("idle", own, _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("below soft", d.reason)
        self.assertEqual(d.utilization, 10.0)

    def test_future_own_with_resets_triggers(self):
        own = _usage(1, 96, fetched_at=NOW + timedelta(minutes=6), five_resets_at=NOW + H2)
        d = _decide("busy", own, _usage(2, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertEqual(d.utilization, 96.0)

    def test_future_own_without_reset_time_not_known(self):
        own = _usage(1, 96, fetched_at=NOW + timedelta(minutes=6), five_resets_at=None)
        d = _decide("busy", own, _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "current account %s: usage fetched_at 360s in the future > tolerance 300s; "
                                   "a window has no reset time, not a lower bound" % _pid(1))
        self.assertIsNone(d.utilization)


class TestDecideCandidateFreshness(unittest.TestCase):
    def test_candidate_at_stale_limit_chosen(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40, fetched_at=NOW - timedelta(minutes=15)))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))

    def test_candidate_past_stale_limit_excluded(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40, fetched_at=NOW - timedelta(minutes=15, seconds=1)))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("no fresh candidate data", d.reason)
        self.assertIn("%s: usage stale: age 901s > max 900s" % _pid(2), d.reason)
        self.assertEqual(d.utilization, 96.0)

    def test_candidate_at_skew_limit_chosen(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40, fetched_at=NOW + timedelta(minutes=5)))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))

    def test_candidate_past_skew_limit_excluded(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40, fetched_at=NOW + timedelta(minutes=5, seconds=1)))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("%s: usage fetched_at 301s in the future > tolerance 300s" % _pid(2), d.reason)

    def test_fresh_beats_stale_lower(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40),
                    _usage(3, 10, fetched_at=NOW - timedelta(minutes=20)))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn("target %s at 40.0%%" % _pid(2), d.reason)

    def test_stale_candidate_with_resets_excluded(self):
        # a stale candidate is never a lower bound, even when every window has a reset time
        stale = _usage(2, 10, source="cache", fetched_at=NOW - timedelta(minutes=20), five_resets_at=NOW + H2)
        d = _decide("busy", _usage(1, 96), stale, _usage(3, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(3))

    def test_all_candidates_unusable(self):
        stale = _usage(3, 10, fetched_at=NOW - timedelta(minutes=20))
        d = _decide("busy", _usage(1, 96), _usage(2, None, source="unavailable"), stale,
                    profiles=[_profile(1), _profile(3), _profile(2)])
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        # rejected candidates are listed sorted by account id, whatever the profile order
        self.assertEqual(d.reason, "max(5h,7d)=96.0%% >= hard 95.0%%; session busy; no target: no fresh candidate "
                                   "data (%s: usage unavailable (network;cache_missing); "
                                   "%s: usage stale: age 1200s > max 900s)" % (_pid(2), _pid(3)))


class TestDecideThresholds(unittest.TestCase):
    def test_exactly_soft(self):
        d = _decide("idle", _usage(1, 85.0), _usage(2, 40))
        self.assertEqual(d.action, "soft")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertEqual(d.utilization, 85.0)

    def test_just_below_soft(self):
        d = _decide("idle", _usage(1, 84.999), _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "max(5h,7d)=84.999% below soft 85.0%")
        self.assertEqual(d.utilization, 84.999)

    def test_exactly_hard(self):
        d = _decide("idle", _usage(1, 95.0), _usage(2, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn(">= hard 95.0%", d.reason)

    def test_just_below_hard(self):
        d = _decide("idle", _usage(1, 94.999), _usage(2, 40))
        self.assertEqual(d.action, "soft")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn("max(5h,7d)=94.999% >= soft 85.0%", d.reason)

    def test_display_never_contradicts(self):
        d = _decide("idle", _usage(1, 84.9996), _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("84.9996%", d.reason)
        self.assertIn("below soft 85.0%", d.reason)

    def test_non_default_thresholds(self):
        config = PolicyConfig(soft_pct=70, hard_pct=80)
        for u, action in ((69.999, "none"), (75, "soft"), (80, "hard")):
            with self.subTest(u=u):
                d = _decide("idle", _usage(1, u), _usage(2, 40), config=config)
                self.assertEqual(d.action, action)
                self.assertEqual(d.target is None, action == "none")


class TestDecideStatusAndIdentity(unittest.TestCase):
    def test_shell_never_switches(self):
        d = _decide("shell", _usage(1, 99), _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "session status shell: no running Claude turn to switch")
        self.assertNotIn("hard", d.reason)
        self.assertEqual(d.utilization, 99.0)

    def test_unknown_beats_unavailable(self):
        d = _decide("unknown", _usage(1, None, source="unavailable"), _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("unknown", d.reason)
        self.assertNotIn("unavailable", d.reason)

    def test_unmanaged(self):
        d = _decide("idle", _usage(1, 99), _usage(2, 40), session=_session("idle", config_dir="/Users/x/.claude"))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "unmanaged session: config_dir maps to no profile")
        self.assertIsNone(d.utilization)

    def test_trailing_slash_config_dir_resolves(self):
        session = _session("idle", config_dir=_profile(1).home + "/")
        d = _decide("idle", _usage(1, 86), _usage(2, 40), session=session)
        self.assertEqual(d.action, "soft")
        self.assertEqual(d.target.account_id, _pid(2))


class TestDecideTiers(unittest.TestCase):
    def test_busy_soft_waits(self):
        d = _decide("busy", _usage(1, 86), _usage(2, 40))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "max(5h,7d)=86.0% >= soft 85.0%; session busy, waiting for idle")
        self.assertEqual(d.utilization, 86.0)

    def test_idle_soft_switches_exact_reason(self):
        d = _decide("idle", _usage(1, 86), _usage(2, 40))
        self.assertEqual(d.action, "soft")
        self.assertEqual(d.target, _profile(2))
        self.assertEqual(d.reason, "max(5h,7d)=86.0% >= soft 85.0%; session idle; "
                                   "target 00000000-0000-4000-8000-000000000002 at 40.0%")
        self.assertEqual(d.utilization, 86.0)

    def test_busy_hard_switches(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn("session busy", d.reason)

    def test_idle_hard_switches(self):
        d = _decide("idle", _usage(1, 96), _usage(2, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn("session idle", d.reason)

    def test_busy_wait_beats_no_target(self):
        d = _decide("busy", _usage(1, 86), profiles=[_profile(1)])
        self.assertEqual(d.action, "none")
        self.assertIn("waiting for idle", d.reason)
        self.assertNotIn("no target", d.reason)


class TestDecideTargetSelection(unittest.TestCase):
    def test_tie_broken_by_account_id(self):
        d = _decide("busy", _usage(1, 96), _usage(4, 70), _usage(3, 40), _usage(2, 40),
                    profiles=[_profile(1), _profile(4), _profile(3), _profile(2)])
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))

    def test_lowest_utilization_beats_lower_account_id(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 70), _usage(3, 40))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(3))
        self.assertIn("target %s at 40.0%%" % _pid(3), d.reason)

    def test_at_headroom_line_chosen(self):
        d = _decide("busy", _usage(1, 96), _usage(3, 75.0))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(3))

    def test_above_headroom_line_rejected(self):
        d = _decide("busy", _usage(1, 96), _usage(3, 75.001))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("no target: best candidate %s at 75.001%% > headroom line 75.0%%" % _pid(3), d.reason)

    def test_headroom_line_follows_config(self):
        d = _decide("busy", _usage(1, 96), _usage(3, 75.0), config=PolicyConfig(target_headroom_pct=20))
        self.assertEqual(d.action, "none")
        self.assertIn("headroom line 65.0%", d.reason)

    def test_all_hot(self):
        d = _decide("busy", _usage(1, 97), _usage(2, 80), _usage(3, 90))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("no target", d.reason)
        self.assertIn("%s at 80.0%%" % _pid(2), d.reason)
        self.assertEqual(d.utilization, 97.0)

    def test_equal_utilization_not_a_target(self):
        d = _decide("idle", _usage(1, 85), _usage(2, 85), config=PolicyConfig(target_headroom_pct=0))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertIn("healthiest", d.reason)

    def test_own_healthiest(self):
        d = _decide("idle", _usage(1, 86), _usage(2, 90), _usage(3, 95))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "max(5h,7d)=86.0%% >= soft 85.0%%; session idle; no target: current account is "
                                   "the healthiest (86.0%% <= best candidate %s at 90.0%%)" % _pid(2))

    def test_single_profile(self):
        d = _decide("busy", _usage(1, 96), profiles=[_profile(1)])
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "max(5h,7d)=96.0% >= hard 95.0%; session busy; no target: no other account")


class TestDecideCooldown(unittest.TestCase):
    def _hot(self, last_switch_at, own=96, status="busy"):
        return _decide(status, _usage(1, own), _usage(2, 40), last_switch_at=last_switch_at)

    def test_inside_cooldown(self):
        d = self._hot(NOW - timedelta(minutes=2))
        self.assertEqual(d.action, "none")
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "max(5h,7d)=96.0% >= hard 95.0%; cooldown: last switch 120s ago < 300s")
        self.assertEqual(d.utilization, 96.0)

    def test_exactly_cooldown_acts(self):
        d = self._hot(NOW - timedelta(minutes=5))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))

    def test_just_inside_cooldown(self):
        d = self._hot(NOW - timedelta(minutes=5) + timedelta(microseconds=1))
        self.assertEqual(d.action, "none")
        self.assertIn("cooldown", d.reason)

    def test_after_cooldown_acts(self):
        d = self._hot(NOW - timedelta(minutes=6))
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))

    def test_no_last_switch_acts(self):
        d = self._hot(None)
        self.assertEqual(d.action, "hard")
        self.assertEqual(d.target.account_id, _pid(2))

    def test_cooldown_beats_busy_wait(self):
        d = self._hot(NOW - timedelta(minutes=1), own=86)
        self.assertEqual(d.action, "none")
        self.assertIn("cooldown", d.reason)
        self.assertNotIn("waiting", d.reason)

    def test_cooldown_beats_no_target(self):
        d = _decide("busy", _usage(1, 96), profiles=[_profile(1)], last_switch_at=NOW - timedelta(minutes=1))
        self.assertEqual(d.action, "none")
        self.assertIn("cooldown", d.reason)
        self.assertNotIn("no target", d.reason)

    def test_below_soft_beats_cooldown(self):
        d = self._hot(NOW - timedelta(minutes=1), own=50, status="idle")
        self.assertEqual(d.action, "none")
        self.assertIn("below soft", d.reason)
        self.assertNotIn("cooldown", d.reason)

    def test_status_gate_beats_cooldown(self):
        d = self._hot(NOW - timedelta(minutes=1), status="shell")
        self.assertEqual(d.action, "none")
        self.assertIn("session status shell", d.reason)
        self.assertNotIn("cooldown", d.reason)

    def test_cooldown_follows_config(self):
        d = _decide("busy", _usage(1, 96), _usage(2, 40), last_switch_at=NOW - timedelta(minutes=6),
                    config=PolicyConfig(cooldown=timedelta(minutes=10)))
        self.assertEqual(d.action, "none")
        self.assertIn("cooldown: last switch 360s ago < 600s", d.reason)

    def test_naive_last_switch_rejected(self):
        with self.assertRaisesRegex(ValueError, "last_switch_at must be timezone-aware"):
            self._hot(datetime(2026, 1, 1, 11, 0))


class TestDecidePurity(unittest.TestCase):
    def test_naive_now_rejected(self):
        naive = datetime(2026, 1, 1, 12, 0)
        # the unmanaged and no-usage paths never call max_utilization, so only decide's own
        # check can reject a naive now there
        cases = [
            ("usage", lambda: _decide("busy", _usage(1, 96), _usage(2, 40), now=naive)),
            ("unmanaged", lambda: _decide("busy", _usage(1, 96), now=naive,
                                          session=_session("busy", config_dir="/Users/x/.claude"))),
            ("no usage", lambda: _decide("busy", profiles=[_profile(1)], now=naive)),
        ]
        for label, call in cases:
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "^now must be timezone-aware$"):
                    call()

    def test_deterministic(self):
        args = ("busy", _usage(1, 96), _usage(3, 40), _usage(2, 40))
        first = _decide(*args)
        self.assertEqual(first, _decide(*args))
        self.assertEqual(first.target.account_id, _pid(2))

    def test_reasons_carry_no_identity(self):
        decisions = [
            _decide("idle", _usage(1, 86), _usage(2, 40)),
            _decide("busy", _usage(1, 96), _usage(2, 40)),
            _decide("idle", _usage(1, 86), _usage(2, 40), session=_session("idle", config_dir="/Users/x/.claude")),
            _decide("busy", _usage(1, 97), _usage(2, 80)),
            _decide("busy", _usage(1, 96), _usage(2, 40), last_switch_at=NOW - timedelta(minutes=2)),
        ]
        for d in decisions:
            with self.subTest(reason=d.reason):
                self.assertNotIn("@", d.reason)
                self.assertNotIn("/x/claude-profiles", d.reason)
                self.assertNotIn("Profile(", d.reason)


def _pointer(*usages, pointer=None, profiles=None, now=NOW, **kw):
    """decide_pointer() for pointer A = _profile(1); profiles default to [A, B, C]."""
    if pointer is None:
        pointer = _profile(1)
    if profiles is None:
        profiles = [_profile(1), _profile(2), _profile(3)]
    return decide_pointer(pointer, profiles, _usages(*usages), now, **kw)


class TestDecidePointer(unittest.TestCase):
    def test_hot_pointer_repoints_to_healthiest(self):
        d = _pointer(_usage(1, 90), _usage(2, 30), _usage(3, 50))
        self.assertEqual(d.target, _profile(2))
        self.assertEqual(d.reason, "pointer account %s max(5h,7d)=90.0%% >= soft 85.0%%; "
                                   "repoint to 00000000-0000-4000-8000-000000000002 at 30.0%%" % _pid(1))

    def test_lowest_utilization_beats_lower_account_id(self):
        d = _pointer(_usage(1, 90), _usage(2, 70), _usage(3, 40))
        self.assertEqual(d.target.account_id, _pid(3))
        self.assertIn("repoint to %s at 40.0%%" % _pid(3), d.reason)

    def test_tie_broken_by_account_id_no_headroom_line(self):
        d = _pointer(_usage(1, 90), _usage(2, 80), _usage(3, 80),
                     profiles=[_profile(1), _profile(3), _profile(2)])
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn("at 80.0%", d.reason)

    def test_ignores_headroom_line(self):
        d = _pointer(_usage(1, 86), _usage(2, 80), profiles=[_profile(1), _profile(2)])
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertNotIn("headroom", d.reason)

    def test_exactly_soft_repoints(self):
        d = _pointer(_usage(1, 85.0), _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertEqual(d.target.account_id, _pid(2))
        self.assertIn("max(5h,7d)=85.0% >= soft 85.0%", d.reason)

    def test_just_below_soft_keeps(self):
        d = _pointer(_usage(1, 84.999), _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "pointer account %s max(5h,7d)=84.999%% below soft 85.0%%" % _pid(1))

    def test_healthy_pointer_keeps(self):
        d = _pointer(_usage(1, 50), _usage(2, 30), _usage(3, 10))
        self.assertIsNone(d.target)
        self.assertIn("below soft", d.reason)

    def test_soft_follows_config(self):
        config = PolicyConfig(soft_pct=60)
        hot = _pointer(_usage(1, 60), _usage(2, 30), profiles=[_profile(1), _profile(2)], config=config)
        self.assertEqual(hot.target.account_id, _pid(2))
        self.assertIn(">= soft 60.0%", hot.reason)
        cool = _pointer(_usage(1, 59.999), _usage(2, 30), profiles=[_profile(1), _profile(2)], config=config)
        self.assertIsNone(cool.target)
        self.assertIn("below soft 60.0%", cool.reason)

    def test_pointer_healthiest(self):
        d = _pointer(_usage(1, 90), _usage(2, 95), _usage(3, 95))
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "pointer account %s max(5h,7d)=90.0%% >= soft 85.0%%; no target: pointer account "
                                   "is the healthiest (90.0%% <= best candidate %s at 95.0%%)" % (_pid(1), _pid(2)))
        self.assertNotIn("current account", d.reason)

    def test_no_candidate_strictly_below(self):
        d = _pointer(_usage(1, 90), _usage(2, 90), _usage(3, 90))
        self.assertIsNone(d.target)
        self.assertIn("no target", d.reason)
        self.assertIn("healthiest", d.reason)

    def test_stale_candidate_excluded(self):
        d = _pointer(_usage(1, 90), _usage(2, 30, fetched_at=NOW - timedelta(minutes=20)),
                     profiles=[_profile(1), _profile(2)])
        self.assertIsNone(d.target)
        self.assertIn("no target", d.reason)
        self.assertIn("%s: usage stale: age 1200s > max 900s" % _pid(2), d.reason)

    def test_stale_candidate_with_resets_excluded(self):
        stale = _usage(2, 30, source="cache", fetched_at=NOW - timedelta(minutes=20), five_resets_at=NOW + H2)
        d = _pointer(_usage(1, 90), stale, _usage(3, 50))
        self.assertEqual(d.target.account_id, _pid(3))

    def test_stale_pointer_with_resets_triggers(self):
        own = _usage(1, 90, source="cache", fetched_at=NOW - timedelta(minutes=20),
                     five_resets_at=NOW + timedelta(hours=1))
        d = _pointer(own, _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertEqual(d.target.account_id, _pid(2))

    def test_stale_pointer_without_reset_time_not_known(self):
        own = _usage(1, 90, source="cache", fetched_at=NOW - timedelta(minutes=20), five_resets_at=None)
        d = _pointer(own, _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "pointer account %s: usage stale: age 1200s > max 900s; "
                                   "a window has no reset time, not a lower bound" % _pid(1))

    def test_stale_pointer_without_seven_day_reset_not_known(self):
        own = _usage(1, 90, 50, source="cache", fetched_at=NOW - timedelta(minutes=20),
                     five_resets_at=NOW + H2, seven_resets_at=None)
        d = _pointer(own, _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertIsNone(d.target)
        self.assertIn("no reset time", d.reason)

    def test_future_pointer_with_resets_triggers(self):
        own = _usage(1, 90, fetched_at=NOW + timedelta(minutes=6), five_resets_at=NOW + H2)
        d = _pointer(own, _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertEqual(d.target.account_id, _pid(2))

    def test_future_pointer_without_reset_time_not_known(self):
        own = _usage(1, 90, fetched_at=NOW + timedelta(minutes=6), five_resets_at=None)
        d = _pointer(own, _usage(2, 30), profiles=[_profile(1), _profile(2)])
        self.assertIsNone(d.target)
        self.assertIn("in the future", d.reason)
        self.assertIn("no reset time", d.reason)

    def test_unmanaged_pointer(self):
        d = decide_pointer(None, [_profile(1), _profile(2)], _usages(_usage(1, 90), _usage(2, 30)), NOW)
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "unmanaged pointer: selected-host names no profile")

    def test_unavailable_pointer(self):
        d = _pointer(_usage(1, None, source="unavailable"), _usage(2, 30))
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "pointer account %s: usage unavailable (network;cache_missing)" % _pid(1))

    def test_missing_pointer_usage(self):
        d = _pointer(_usage(2, 30))
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "pointer account %s: no usage data" % _pid(1))

    def test_single_profile(self):
        d = _pointer(_usage(1, 90), profiles=[_profile(1)])
        self.assertIsNone(d.target)
        self.assertEqual(d.reason, "pointer account %s max(5h,7d)=90.0%% >= soft 85.0%%; "
                                   "no target: no other account" % _pid(1))

    def test_naive_now_rejected(self):
        naive = datetime(2026, 1, 1, 12, 0)
        # the unmanaged and no-usage paths never call max_utilization, so only
        # decide_pointer's own check can reject a naive now there
        cases = [
            ("usage", lambda: _pointer(_usage(1, 90), _usage(2, 30), now=naive)),
            ("unmanaged", lambda: decide_pointer(None, [_profile(1)], {}, naive)),
            ("no usage", lambda: _pointer(profiles=[_profile(1)], now=naive)),
        ]
        for label, call in cases:
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, "^now must be timezone-aware$"):
                    call()

    def test_deterministic(self):
        args = (_usage(1, 90), _usage(3, 30), _usage(2, 30))
        first = _pointer(*args)
        self.assertEqual(first, _pointer(*args))
        self.assertEqual(first.target.account_id, _pid(2))

    def test_reasons_carry_no_identity(self):
        decisions = [
            _pointer(_usage(1, 90), _usage(2, 30)),
            _pointer(_usage(1, 50)),
            _pointer(_usage(1, 90), _usage(2, 95)),
            decide_pointer(None, [_profile(1)], {}, NOW),
        ]
        for d in decisions:
            with self.subTest(reason=d.reason):
                self.assertNotIn("@", d.reason)
                self.assertNotIn("/x/claude-profiles", d.reason)
                self.assertNotIn("Profile(", d.reason)


if __name__ == "__main__":
    unittest.main()
