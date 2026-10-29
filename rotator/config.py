"""Load and validate the rotator's JSON config file."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from rotator.models import PROFILES_ROOT_DEFAULT, ReadText
from rotator.policy import PolicyConfig
from rotator.switcher import DEFAULT_CONTINUE_PROMPT, MAX_PROMPT_CHARS, Timeouts

CONFIG_DIR_DEFAULT = os.path.expanduser("~/Library/Application Support/claude-account-rotator")
CONFIG_FILENAME = "config.json"
CONFIG_PATH_DEFAULT = os.path.join(CONFIG_DIR_DEFAULT, CONFIG_FILENAME)
LOG_DIR_DEFAULT = os.path.expanduser("~/Library/Logs/claude-account-rotator")
# Must stay below the policy's stale_max_age so a polled account never turns stale between fetches.
MIN_USAGE_POLL_S = 15.0

TOP_KEYS = (
    "soft_pct",
    "hard_pct",
    "target_headroom_pct",
    "stale_max_age_s",
    "cooldown_s",
    "clock_skew_tolerance_s",
    "session_poll_s",
    "usage_poll_s",
    "timeouts",
    "continue_prompt",
    "dry_run",
    "auto_switch",
    "pointer_correction",
    "profiles_root",
)
TIMEOUT_KEYS = ("idle_s", "exit_s", "shell_s", "ready_s", "prompt_s", "poll_s")

_PROMPT_MESSAGE = (
    "continue_prompt: must be one non-empty line of at most %d characters, "
    "without control characters, not starting with - or /" % MAX_PROMPT_CHARS
)


class ConfigError(ValueError):
    """A config value is invalid; the message starts with the dotted key or the file path."""


@dataclass(frozen=True)
class Config:
    soft_pct: float = 85.0
    hard_pct: float = 95.0
    target_headroom_pct: float = 10.0
    stale_max_age_s: float = 900.0
    cooldown_s: float = 300.0
    clock_skew_tolerance_s: float = 300.0
    session_poll_s: float = 10.0
    usage_poll_s: float = 30.0
    timeouts: Timeouts = Timeouts()
    continue_prompt: str = DEFAULT_CONTINUE_PROMPT
    dry_run: bool = False
    auto_switch: bool = True
    pointer_correction: bool = True
    profiles_root: str = PROFILES_ROOT_DEFAULT
    # Not a JSON key: where state, locks and records live. load_config always uses CONFIG_DIR_DEFAULT
    # whatever --config names, so every daemon and CLI process contends on the same lock files.
    state_dir: str = CONFIG_DIR_DEFAULT

    def policy_config(self) -> PolicyConfig:
        return PolicyConfig(
            soft_pct=self.soft_pct,
            hard_pct=self.hard_pct,
            target_headroom_pct=self.target_headroom_pct,
            stale_max_age=timedelta(seconds=self.stale_max_age_s),
            cooldown=timedelta(seconds=self.cooldown_s),
            clock_skew_tolerance=timedelta(seconds=self.clock_skew_tolerance_s),
        )


def prompt_ok(p: object) -> bool:
    """True iff ``p`` passes the same check ``switcher.switch_session`` applies to ``continue_prompt``."""
    return (
        isinstance(p, str)
        and p.strip() != ""
        and len(p) <= MAX_PROMPT_CHARS
        and not any(ord(c) < 32 or ord(c) == 127 for c in p)
        and not p.startswith("-")
        and not p.startswith("/")
    )


def _num(key: str, value: Any) -> float:
    if type(value) is bool or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError("%s: must be a finite number" % key)
    return float(value)


def parse_config(obj: object, *, state_dir: str) -> Config:
    """Validate a decoded JSON object; the first invalid key raises ConfigError naming it."""
    if not isinstance(obj, dict):
        raise ConfigError("config: top level must be a JSON object")
    for k in sorted(obj):
        if k not in TOP_KEYS:
            raise ConfigError("%s: unknown key" % k)
    vals: Dict[str, Any] = {
        name: f.default for name, f in Config.__dataclass_fields__.items() if name != "state_dir"
    }
    for key in TOP_KEYS:
        if key not in obj:
            continue
        value = obj[key]
        if key in ("soft_pct", "hard_pct", "target_headroom_pct"):
            v = _num(key, value)
            if not 0 <= v <= 100:
                raise ConfigError("%s: must be between 0 and 100" % key)
            vals[key] = v
        elif key in ("stale_max_age_s", "cooldown_s", "clock_skew_tolerance_s"):
            v = _num(key, value)
            if v < 0:
                raise ConfigError("%s: must be >= 0" % key)
            vals[key] = v
        elif key == "session_poll_s":
            v = _num(key, value)
            if v <= 0:
                raise ConfigError("session_poll_s: must be > 0")
            vals[key] = v
        elif key == "usage_poll_s":
            v = _num(key, value)
            if v < MIN_USAGE_POLL_S:
                raise ConfigError("usage_poll_s: must be >= %g" % MIN_USAGE_POLL_S)
            vals[key] = v
        elif key == "timeouts":
            if not isinstance(value, dict):
                raise ConfigError("timeouts: must be an object")
            for k in sorted(value):
                if k not in TIMEOUT_KEYS:
                    raise ConfigError("timeouts.%s: unknown key" % k)
            base = Timeouts()
            merged = {k: getattr(base, k) for k in TIMEOUT_KEYS}
            for k in TIMEOUT_KEYS:
                if k not in value:
                    continue
                v = _num("timeouts." + k, value[k])
                if v < 0:
                    raise ConfigError("timeouts.%s: must be a finite number >= 0" % k)
                if k == "poll_s" and v <= 0:
                    raise ConfigError("timeouts.poll_s: must be > 0")
                merged[k] = v
            vals["timeouts"] = Timeouts(**merged)
        elif key == "continue_prompt":
            if not prompt_ok(value):
                raise ConfigError(_PROMPT_MESSAGE)
            vals[key] = value
        elif key in ("dry_run", "auto_switch", "pointer_correction"):
            if type(value) is not bool:
                raise ConfigError("%s: must be true or false" % key)
            vals[key] = value
        elif key == "profiles_root":
            if not isinstance(value, str) or value == "":
                raise ConfigError("profiles_root: must be an absolute path")
            e = os.path.expanduser(value)
            if not os.path.isabs(e):
                raise ConfigError("profiles_root: must be an absolute path")
            vals[key] = e
    if vals["soft_pct"] > vals["hard_pct"]:
        raise ConfigError("hard_pct: must be >= soft_pct")
    if vals["soft_pct"] - vals["target_headroom_pct"] < 0:
        raise ConfigError("target_headroom_pct: must be <= soft_pct")
    if vals["usage_poll_s"] >= vals["stale_max_age_s"]:
        raise ConfigError("usage_poll_s: must be < stale_max_age_s")
    return Config(**vals, state_dir=state_dir)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_config(path: Optional[str] = None, *, read_text: Optional[ReadText] = None) -> Config:
    """Load ``path`` (default CONFIG_PATH_DEFAULT); a missing file gives all defaults.

    The returned state_dir is always CONFIG_DIR_DEFAULT, never the directory of ``path``."""
    p = os.path.abspath(os.path.expanduser(CONFIG_PATH_DEFAULT if path is None else path))
    state_dir = CONFIG_DIR_DEFAULT
    reader = read_text or _read_text
    try:
        text = reader(p)
    except FileNotFoundError:
        return Config(state_dir=state_dir)
    except UnicodeDecodeError:
        raise ConfigError("%s: not valid UTF-8" % p) from None
    except OSError as e:
        raise ConfigError("%s: unreadable (%s)" % (p, type(e).__name__)) from None
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise ConfigError("%s: not valid JSON (line %s)" % (p, getattr(e, "lineno", "?"))) from None
    if not isinstance(obj, dict):
        raise ConfigError("%s: top level must be a JSON object" % p)
    return parse_config(obj, state_dir=state_dir)
