import dataclasses
import hashlib
import json
import os
import pickle
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from typing import Any, Dict

from rotator.accounts import (
    Credential,
    _read_text_utf8,
    discover_profiles,
    keychain_service_for,
    profile_for_config_dir,
    read_credential,
)
from rotator.models import Profile, Session

ACC1 = "00000000-0000-4000-8000-000000000001"
ACC2 = "00000000-0000-4000-8000-000000000002"
ACC3 = "00000000-0000-4000-8000-000000000003"
ACC4 = "00000000-0000-4000-8000-000000000004"
ACC5 = "00000000-0000-4000-8000-000000000005"
ACC6 = "00000000-0000-4000-8000-000000000006"

IDENTITY = {
    "emailAddress": "user@example.com",
    "displayName": "User Name",
    "accountUuid": "00000000-0000-4000-8000-0000000000aa",
    "organizationName": "Example Org",
    "userRateLimitTier": "default_tier",
    "fullName": "Full Name",
}


def make_profile(root, account_id, profile_json=None, claude_json=None, with_home=True):
    entry = os.path.join(root, account_id)
    os.makedirs(entry)
    if profile_json is None:
        profile_json = json.dumps({"version": 1, "accountId": account_id, "runtime": "host"})
    with open(os.path.join(entry, "profile.json"), "w", encoding="utf-8") as f:
        f.write(profile_json)
    home = os.path.join(entry, "home")
    if with_home:
        os.makedirs(home)
        if claude_json is not None:
            with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as f:
                f.write(claude_json)
    return home


def tree_snapshot(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            out.append((p, st.st_size, st.st_mtime_ns))
    return sorted(out)


class ModelsProfileSessionTest(unittest.TestCase):
    def test_profile_repr_hides_identity(self):
        home = "/x/" + ACC1 + "/home"
        p = Profile(
            account_id=ACC1,
            home=home,
            keychain_service=keychain_service_for(home),
            email="user@example.com",
            display_name="User Name",
            account_uuid="00000000-0000-4000-8000-0000000000aa",
            organization_name="Example Org",
        )
        r = repr(p)
        self.assertNotIn("user@example.com", r)
        self.assertNotIn("User Name", r)
        self.assertNotIn("00000000-0000-4000-8000-0000000000aa", r)
        self.assertNotIn("Example Org", r)
        self.assertIn(ACC1, r)

    def test_profile_relative_home_rejected(self):
        with self.assertRaises(ValueError):
            Profile(account_id=ACC1, home="rel/home", keychain_service="svc")

    def test_session_constructible_minimal_and_full(self):
        minimal = Session(pid=1, session_id="s1", cwd="/w", status="idle", config_dir="/c", argv=())
        self.assertIsNone(minimal.terminal_handle)
        self.assertIsNone(minimal.started_at)
        self.assertEqual(minimal.argv, ())
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        full = Session(
            pid=42,
            session_id="s2",
            cwd="/w2",
            status="busy",
            config_dir="/c2",
            argv=("claude", "--resume"),
            terminal_handle="term_abc",
            started_at=started,
        )
        self.assertEqual(full.pid, 42)
        self.assertEqual(full.status, "busy")
        self.assertEqual(full.terminal_handle, "term_abc")
        self.assertEqual(full.started_at, started)
        self.assertIsInstance(full.argv, tuple)
        self.assertEqual(full.argv, ("claude", "--resume"))

    def test_session_rejects_bad_values(self):
        base: Dict[str, Any] = dict(pid=1, session_id="s", cwd="/w", status="idle", config_dir="/c", argv=())
        with self.assertRaises(ValueError):
            Session(**dict(base, status="running"))
        with self.assertRaises(TypeError):
            Session(**dict(base, argv=["claude"]))
        with self.assertRaises(ValueError):
            Session(**dict(base, config_dir="rel/home"))
        with self.assertRaises(TypeError):
            Session(**dict(base, pid=True))
        with self.assertRaises(ValueError):
            Session(**dict(base, pid=0))
        s = Session(**base)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.status = "busy"


class KeychainServiceTest(unittest.TestCase):
    def test_keychain_service_formula(self):
        home = "/x/claude-profiles/" + ACC1 + "/home"
        expected = "Claude Code-credentials-" + hashlib.sha256(home.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(keychain_service_for(home), expected)

    def test_keychain_service_trailing_slash_differs(self):
        home = "/x/claude-profiles/" + ACC1 + "/home"
        self.assertNotEqual(keychain_service_for(home), keychain_service_for(home + "/"))


class DiscoverProfilesTest(unittest.TestCase):
    def test_discover_one_complete_profile(self):
        with tempfile.TemporaryDirectory() as root:
            home = make_profile(root, ACC1)
            got = discover_profiles(root)
            self.assertEqual(len(got), 1)
            p = got[0]
            self.assertEqual(p.account_id, ACC1)
            self.assertEqual(p.home, os.path.join(root, ACC1, "home"))
            self.assertEqual(p.home, home)
            self.assertEqual(p.keychain_service, keychain_service_for(home))
            self.assertIsNone(p.email)
            self.assertIsNone(p.display_name)
            self.assertIsNone(p.account_uuid)
            self.assertIsNone(p.organization_name)
            self.assertIsNone(p.rate_limit_tier)

    def test_discover_mixed_root(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1)
            make_profile(root, ACC2, with_home=False)
            make_profile(root, ACC3, profile_json="not json")
            make_profile(root, ACC4, profile_json="[" * 100000)
            with open(os.path.join(root, "selected-host"), "w", encoding="utf-8") as f:
                f.write(ACC1)
            os.makedirs(os.path.join(root, "notes"))
            make_profile(root, ACC5, profile_json=json.dumps({"accountId": ACC1}))
            make_profile(root, ACC6)
            denied = os.path.join(root, ACC6, "profile.json")

            def read_text(path):
                if path == denied:
                    raise PermissionError(path)
                with open(path, encoding="utf-8") as f:
                    return f.read()

            got = discover_profiles(root, read_text=read_text)
            self.assertEqual([p.account_id for p in got], [ACC1])

    def test_discover_missing_root_returns_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(discover_profiles(os.path.join(root, "absent")), [])

    def test_discover_sorted(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC2)
            make_profile(root, ACC1)
            self.assertEqual([p.account_id for p in discover_profiles(root)], [ACC1, ACC2])

    def test_discover_root_trailing_slash_same_service(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1)
            plain = discover_profiles(root)[0]
            slashed = discover_profiles(root + "/")[0]
            self.assertEqual(slashed.home, plain.home)
            self.assertEqual(slashed.keychain_service, plain.keychain_service)

    def test_discover_does_not_modify_tree(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1, claude_json=json.dumps({"oauthAccount": IDENTITY}))
            make_profile(root, ACC3, profile_json="not json")
            before = tree_snapshot(root)
            discover_profiles(root)
            self.assertEqual(tree_snapshot(root), before)


class IdentityTest(unittest.TestCase):
    def test_identity_present(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1, claude_json=json.dumps({"oauthAccount": IDENTITY, "numStartups": 3}))
            p = discover_profiles(root)[0]
            self.assertEqual(p.email, "user@example.com")
            self.assertEqual(p.display_name, "User Name")
            self.assertEqual(p.account_uuid, "00000000-0000-4000-8000-0000000000aa")
            self.assertEqual(p.organization_name, "Example Org")
            self.assertEqual(p.rate_limit_tier, "default_tier")
            self.assertFalse(hasattr(p, "full_name"))

    def test_identity_truncated_json_all_none(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1, claude_json="{")
            got = discover_profiles(root)
            self.assertEqual([p.account_id for p in got], [ACC1])
            p = got[0]
            self.assertIsNone(p.email)
            self.assertIsNone(p.display_name)
            self.assertIsNone(p.account_uuid)
            self.assertIsNone(p.organization_name)
            self.assertIsNone(p.rate_limit_tier)

    def test_identity_non_string_values_none(self):
        with tempfile.TemporaryDirectory() as root:
            ident = {"emailAddress": 5, "displayName": None, "organizationName": "Example Org"}
            make_profile(root, ACC1, claude_json=json.dumps({"oauthAccount": ident}))
            p = discover_profiles(root)[0]
            self.assertIsNone(p.email)
            self.assertIsNone(p.display_name)
            self.assertEqual(p.organization_name, "Example Org")

    def test_default_read_text_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1, claude_json=json.dumps({"oauthAccount": {"displayName": "Zoë"}}, ensure_ascii=False))
            p = discover_profiles(root)[0]
            self.assertEqual(p.display_name, "Zoë")

    def test_default_read_text_non_utf8_raises_oserror(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.json")
            with open(path, "wb") as f:
                f.write(b"\xff\xfe")
            with self.assertRaises(OSError) as ctx:
                _read_text_utf8(path)
        self.assertNotIsInstance(ctx.exception, UnicodeDecodeError)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_discover_non_utf8_profile_json_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            make_profile(root, ACC1)
            make_profile(root, ACC2)
            with open(os.path.join(root, ACC2, "profile.json"), "wb") as f:
                f.write(b"\xff\xfe")
            with self.assertLogs("rotator", level="DEBUG") as cap:
                got = discover_profiles(root)
        self.assertEqual([p.account_id for p in got], [ACC1])
        self.assertIn("skip profile entry %s: profile_unreadable" % ACC2, [r.getMessage() for r in cap.records])

    def test_identity_non_utf8_claude_json_all_none(self):
        with tempfile.TemporaryDirectory() as root:
            home = make_profile(root, ACC1)
            with open(os.path.join(home, ".claude.json"), "wb") as f:
                f.write(b"\xff\x7b")
            got = discover_profiles(root)
        self.assertEqual([p.account_id for p in got], [ACC1])
        p = got[0]
        self.assertIsNone(p.email)
        self.assertIsNone(p.display_name)
        self.assertIsNone(p.account_uuid)
        self.assertIsNone(p.organization_name)
        self.assertIsNone(p.rate_limit_tier)


class ProfileForConfigDirTest(unittest.TestCase):
    def setUp(self):
        self.h1 = "/x/" + ACC1 + "/home"
        self.h2 = "/x/" + ACC2 + "/home"
        self.p1 = Profile(account_id=ACC1, home=self.h1, keychain_service=keychain_service_for(self.h1))
        self.p2 = Profile(account_id=ACC2, home=self.h2, keychain_service=keychain_service_for(self.h2))

    def test_exact_match(self):
        self.assertIs(profile_for_config_dir([self.p1, self.p2], self.h2), self.p2)

    def test_trailing_slash_matches(self):
        self.assertIs(profile_for_config_dir([self.p1, self.p2], self.h1 + "/"), self.p1)

    def test_no_match_returns_none(self):
        self.assertIsNone(profile_for_config_dir([self.p1, self.p2], "/Volumes/other/.claude"))

    def test_empty_list_returns_none(self):
        self.assertIsNone(profile_for_config_dir([], self.h1))


TOKEN = "tok-SECRET-123"
RT = "rt-SECRET-456"
EXP = 4102444800000
CRED_HOME = "/x/" + ACC1 + "/home"
CRED_PROFILE = Profile(account_id=ACC1, home=CRED_HOME, keychain_service=keychain_service_for(CRED_HOME))


def cred_json(token=TOKEN, exp=EXP):
    return json.dumps(
        {
            "claudeAiOauth": {"accessToken": token, "expiresAt": exp, "refreshToken": RT, "scopes": ["user:inference"]},
            "mcpOAuth": {},
        }
    )


class FakeKeychain:
    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc
        self.services = []

    def __call__(self, service):
        self.services.append(service)
        if self.exc is not None:
            raise self.exc
        return self.value


# (label, FakeKeychain) for every way a Keychain read must come back as None.
FAILURE_SEAMS = [
    ("raises CalledProcessError", lambda: FakeKeychain(exc=subprocess.CalledProcessError(44, "security"))),
    ("raises TimeoutExpired", lambda: FakeKeychain(exc=subprocess.TimeoutExpired("security", 10))),
    ("raises OSError", lambda: FakeKeychain(exc=OSError())),
    ("returns None", lambda: FakeKeychain(value=None)),
    ("returns empty", lambda: FakeKeychain(value="")),
    ("not json", lambda: FakeKeychain(value="not json")),
    ("empty object", lambda: FakeKeychain(value="{}")),
    ("deeply nested", lambda: FakeKeychain(value="[" * 100000)),
    ("expiresAt bool", lambda: FakeKeychain(value=cred_json(exp=True))),
    ("expiresAt str", lambda: FakeKeychain(value=cred_json(exp="x"))),
    ("expiresAt NaN", lambda: FakeKeychain(value='{"claudeAiOauth": {"accessToken": "tok-SECRET-123", "expiresAt": NaN}}')),
    ("expiresAt 1e400", lambda: FakeKeychain(value='{"claudeAiOauth": {"accessToken": "tok-SECRET-123", "expiresAt": 1e400}}')),
]

UNSAFE_TOKENS = [TOKEN + "\r\nX: y", "tok SECRET", "tok-SECRET-\u00e9", "a" * 4097, ""]


def fake_run(returncode=0, stdout=""):
    return unittest.mock.Mock(returncode=returncode, stdout=stdout, stderr="")


class CredentialTest(unittest.TestCase):
    def test_read_credential_ok(self):
        kc = FakeKeychain(value=cred_json())
        cred = read_credential(CRED_PROFILE, keychain=kc)
        self.assertIsInstance(cred, Credential)
        self.assertEqual(cred.access_token, TOKEN)
        self.assertEqual(cred.expires_at_ms, EXP)
        self.assertEqual(kc.services, [CRED_PROFILE.keychain_service])

    def test_read_credential_float_expiry(self):
        cred = read_credential(CRED_PROFILE, keychain=FakeKeychain(value=cred_json(exp=4102444800000.0)))
        self.assertEqual(cred.expires_at_ms, EXP)
        self.assertIs(type(cred.expires_at_ms), int)

    def test_read_credential_failures(self):
        for label, make in FAILURE_SEAMS:
            with self.subTest(label):
                self.assertIsNone(read_credential(CRED_PROFILE, keychain=make()))

    def test_read_credential_rejects_unsafe_token(self):
        for token in UNSAFE_TOKENS:
            with self.subTest(length=len(token)):
                self.assertIsNone(read_credential(CRED_PROFILE, keychain=FakeKeychain(value=cred_json(token=token))))
        longest = read_credential(CRED_PROFILE, keychain=FakeKeychain(value=cred_json(token="a" * 4096)))
        self.assertIsNotNone(longest)
        self.assertEqual(longest.access_token, "a" * 4096)

    def test_credential_not_picklable(self):
        cred = Credential(TOKEN, EXP)
        with self.assertRaises(TypeError):
            pickle.dumps(cred)

    def test_credential_has_no_setter(self):
        cred = Credential(TOKEN, EXP)
        with self.assertRaises(AttributeError):
            cred.access_token = "x"
        self.assertEqual(cred.access_token, TOKEN)


class DefaultKeychainSeamTest(unittest.TestCase):
    def test_default_keychain_seam_argv(self):
        with unittest.mock.patch("rotator.accounts.subprocess.run", return_value=fake_run(0, cred_json() + "\n")) as run:
            cred = read_credential(CRED_PROFILE)
        self.assertEqual(cred.access_token, TOKEN)
        self.assertEqual(cred.expires_at_ms, EXP)
        run.assert_called_once_with(
            ["security", "find-generic-password", "-s", CRED_PROFILE.keychain_service, "-w"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=10.0,
            check=False,
        )

    def test_default_keychain_seam_nonzero_rc(self):
        with unittest.mock.patch("rotator.accounts.subprocess.run", return_value=fake_run(44, cred_json())):
            self.assertIsNone(read_credential(CRED_PROFILE))

    def test_default_keychain_seam_timeout(self):
        exc = subprocess.TimeoutExpired("security", 10, output=TOKEN)
        with unittest.mock.patch("rotator.accounts.subprocess.run", side_effect=exc):
            self.assertIsNone(read_credential(CRED_PROFILE))


class SecretAbsenceTest(unittest.TestCase):
    def scenarios(self):
        yield "success", lambda: read_credential(CRED_PROFILE, keychain=FakeKeychain(value=cred_json()))
        for label, make in FAILURE_SEAMS:
            yield label, (lambda make=make: read_credential(CRED_PROFILE, keychain=make()))
        for token in UNSAFE_TOKENS:
            yield "unsafe token len %d" % len(token), (
                lambda token=token: read_credential(CRED_PROFILE, keychain=FakeKeychain(value=cred_json(token=token)))
            )

        def default_seam_timeout():
            exc = subprocess.TimeoutExpired("security", 10, output=TOKEN)
            with unittest.mock.patch("rotator.accounts.subprocess.run", side_effect=exc):
                return read_credential(CRED_PROFILE)

        yield "default seam timeout", default_seam_timeout

    def test_token_absent_from_every_channel(self):
        for label, call in self.scenarios():
            with self.subTest(label):
                with self.assertLogs("rotator", level="DEBUG") as cap:
                    result = call()
                self.assertNotIn(TOKEN, repr(result))
                self.assertNotIn(TOKEN, str(result))
                self.assertNotIn(RT, repr(result))
                for record in cap.records:
                    self.assertNotIn(TOKEN, record.getMessage())
                    self.assertNotIn(RT, record.getMessage())
                    self.assertIsNone(record.exc_info)
                self.assertTrue(any(CRED_PROFILE.keychain_service in r.getMessage() for r in cap.records))

    def test_success_repr_positive_controls(self):
        with self.assertLogs("rotator", level="DEBUG"):
            cred = read_credential(CRED_PROFILE, keychain=FakeKeychain(value=cred_json()))
        self.assertIn("<redacted>", repr(cred))
        self.assertIn(str(EXP), repr(cred))
        self.assertIn("<redacted>", str(cred))


if __name__ == "__main__":
    unittest.main()
