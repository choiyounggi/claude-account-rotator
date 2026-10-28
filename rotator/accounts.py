# contract: t1 owns the implementation
"""Profile discovery and Keychain credential lookup (contract stub: signatures only)."""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from rotator.models import Keychain, Profile, ReadText

KEYCHAIN_TIMEOUT_S: float = 10.0

log = logging.getLogger("rotator.accounts")


def keychain_service_for(home: str) -> str:
    """KEYCHAIN_SERVICE_PREFIX + sha256(home utf-8).hexdigest()[:8], on ``home`` exactly as given."""
    raise NotImplementedError("contract stub: t1 implements")


def discover_profiles(root: Optional[str] = None, *, read_text: Optional[ReadText] = None) -> List[Profile]:
    """Profiles under ``root`` (default PROFILES_ROOT_DEFAULT), sorted by account_id. Read-only; never raises for bad entries."""
    raise NotImplementedError("contract stub: t1 implements")


def profile_for_config_dir(profiles: Sequence[Profile], config_dir: str) -> Optional[Profile]:
    """First profile whose normpath(home) == normpath(config_dir); None = unmanaged session. Pure."""
    raise NotImplementedError("contract stub: t1 implements")


class Credential:
    """In-memory OAuth access token holder. Not a dataclass; repr/str redact the token; not picklable."""

    __slots__ = ("_access_token", "expires_at_ms")

    def __init__(self, access_token: str, expires_at_ms: int) -> None:
        raise NotImplementedError("contract stub: t1 implements")


def read_credential(profile: Profile, *, keychain: Optional[Keychain] = None) -> Optional[Credential]:
    """Read accessToken/expiresAt from the profile's Keychain item; None when unavailable. Never raises for expected failures."""
    raise NotImplementedError("contract stub: t1 implements")
