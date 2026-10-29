import copy
import dataclasses
import email.message
import hashlib
import http.client
import io
import json
import logging
import os
import pickle
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from rotator import accounts, config, refresh, usage
from rotator.config import Config
from rotator.models import Profile, Session

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, ".claude", "tmp", "test-refresh")
SECRET_A = "SECRET-ACCESS-7f3a"
SECRET_R = "SECRET-REFRESH-9c1e"
ORIG_URLLIB_POST = refresh._urllib_post
TRIPWIRES = ("subprocess.run", "rotator.refresh._urllib_post", "rotator.accounts._security_find_password",
             "rotator.accounts.read_keychain_item", "rotator.accounts.keychain_item_exists",
             "rotator.accounts.write_keychain_item")


class TripwireCase(unittest.TestCase):
    """Every real Keychain/network/process seam raises; tests inject fakes only."""

    def setUp(self):
        for target in TRIPWIRES:
            p = mock.patch(target, side_effect=AssertionError("real seam"))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(usage._NO_REDIRECT_OPENER, "open", side_effect=AssertionError("real seam"))
        p.start()
        self.addCleanup(p.stop)


T = 1_800_000_000_000
BASE_6 = ["user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers",
          "user:file_upload", "user:plugins"]
RESPONSE = '{"access_token":"A2","refresh_token":"R2","expires_in":28800,"scope":"user:inference user:profile"}'


class TestConstants(TripwireCase):
    def test_reason_count(self):
        self.assertEqual(len(refresh.REFRESH_REASONS), 30)

    def test_daemon_off_record_keys(self):
        self.assertEqual(set(refresh.DAEMON_OFF_RECORD), {"state", "reason", "at", "last_refreshed_at"})
        self.assertEqual(refresh.DAEMON_OFF_RECORD["reason"], "daemon_off")

    def test_no_daemon_import(self):
        with open(refresh.__file__, encoding="utf-8") as f:
            text = f.read()
        for needle in ("rotator.daemon", "import daemon", "HttpResult"):
            self.assertNotIn(needle, text)


class TestScopes(TripwireCase):
    def test_expands_without_client_id(self):
        got = refresh.request_scopes({"scopes": ["user:inference", "user:profile", "user:projects:read"]})
        self.assertEqual(got, BASE_6 + ["user:projects:read"])

    def test_client_id_keeps_stored(self):
        stored = ["user:inference", "user:profile"]
        self.assertEqual(refresh.request_scopes({"scopes": stored, "clientId": "c-1"}), stored)

    def test_subscription_triggers_expansion(self):
        self.assertEqual(refresh.request_scopes({"scopes": ["user:profile"], "subscriptionType": "max"}), BASE_6)

    def test_no_trigger_keeps_stored(self):
        self.assertEqual(refresh.request_scopes({"scopes": ["user:profile"]}), ["user:profile"])

    def test_absent_scopes_empty(self):
        self.assertEqual(refresh.request_scopes({}), [])
        self.assertEqual(refresh.request_scopes({"scopes": "user:inference"}), [])


class TestPostResult(TripwireCase):
    def test_redacted(self):
        r = refresh.PostResult(200, '{"access_token":"%s"}' % SECRET_A, None)
        for text in (repr(r), str(r)):
            self.assertNotIn(SECRET_A, text)
            self.assertNotIn("access_token", text)
        self.assertIn("200", repr(r))

    def test_not_picklable(self):
        with self.assertRaises(TypeError):
            pickle.dumps(refresh.PostResult(200, SECRET_A))


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestUrllibPost(TripwireCase):
    URL = "https://example.invalid/token"

    def _opener(self, result=None, exc=None):
        calls = []

        def open_(req, timeout=None):
            calls.append({"url": req.full_url, "method": req.get_method(), "data": req.data,
                          "headers": dict(req.header_items()), "timeout": timeout})
            if exc is not None:
                raise exc
            return result
        return open_, calls

    def _post(self, opener):
        with mock.patch.object(usage._NO_REDIRECT_OPENER, "open", opener):
            return ORIG_URLLIB_POST(self.URL, {"Content-Type": "application/json"}, b'{"a":1}', 10.0)

    def _http_error(self, code, body, retry_after=None):
        hdrs = email.message.Message()
        if retry_after is not None:
            hdrs["Retry-After"] = retry_after
        return urllib.error.HTTPError(self.URL, code, "x", hdrs, io.BytesIO(body))

    def test_success(self):
        opener, calls = self._opener(result=_Resp(200, b'{"ok":true}'))
        r = self._post(opener)
        self.assertEqual((r.status, r.body, r.retry_after), (200, '{"ok":true}', None))
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["data"], b'{"a":1}')
        self.assertEqual(calls[0]["url"], self.URL)
        self.assertEqual(calls[0]["timeout"], 10.0)
        self.assertEqual(calls[0]["headers"].get("Content-type"), "application/json")

    def test_http_error_body_and_retry_after(self):
        opener, _ = self._opener(exc=self._http_error(400, b'{"error":"invalid_grant"}', "120"))
        r = self._post(opener)
        self.assertEqual((r.status, r.body, r.retry_after), (400, '{"error":"invalid_grant"}', "120"))

    def test_redirect_not_followed(self):
        opener, calls = self._opener(exc=self._http_error(302, b""))
        r = self._post(opener)
        self.assertEqual(r.status, 302)
        self.assertEqual(len(calls), 1)

    def test_long_retry_after_dropped(self):
        opener, _ = self._opener(exc=self._http_error(429, b"", "1" * 65))
        self.assertIsNone(self._post(opener).retry_after)

    def test_timeout_propagates(self):
        opener, _ = self._opener(exc=socket.timeout())
        with self.assertRaises(socket.timeout):
            self._post(opener)


class TestParse(TripwireCase):
    def test_full_response(self):
        t = refresh.parse_token_response(RESPONSE, T, "R1")
        self.assertEqual((t.access_token, t.refresh_token), ("A2", "R2"))
        self.assertEqual(t.expires_at_ms, T + 28_800_000)
        self.assertEqual(t.scopes, ["user:inference", "user:profile"])
        self.assertIsNone(t.refresh_expires_at_ms)

    def test_absent_refresh_token_reuses_posted(self):
        t = refresh.parse_token_response('{"access_token":"A2","expires_in":60}', T, "R1")
        self.assertEqual(t.refresh_token, "R1")
        self.assertIsNone(t.scopes)

    def test_refresh_expiry(self):
        t = refresh.parse_token_response(
            '{"access_token":"A2","expires_in":60,"refresh_token_expires_in":3600}', T, "R1")
        self.assertEqual(t.refresh_expires_at_ms, T + 3_600_000)

    def test_empty_scope_is_none(self):
        t = refresh.parse_token_response('{"access_token":"A2","expires_in":60,"scope":""}', T, "R1")
        self.assertIsNone(t.scopes)

    def test_rejects(self):
        bad = ["not json", "[]", '{"expires_in":60}', '{"access_token":"a b","expires_in":60}',
               '{"access_token":"A2","expires_in":"x"}', '{"access_token":"A2","expires_in":0}',
               '{"access_token":"A2","expires_in":-1}', '{"access_token":"A2","expires_in":true}',
               '{"access_token":"A2","expires_in":1e400}', '{"access_token":"A2","expires_in":60,"refresh_token":7}',
               "[" * 100000]
        for body in bad:
            with self.subTest(body=body[:40]):
                self.assertIsNone(refresh.parse_token_response(body, T, "R1"))

    def test_tokens_redacted(self):
        t = refresh.parse_token_response(
            '{"access_token":"%s","refresh_token":"%s","expires_in":60}' % (SECRET_A, SECRET_R), T, "R1")
        for text in (repr(t), str(t)):
            self.assertNotIn(SECRET_A, text)
            self.assertNotIn(SECRET_R, text)
        with self.assertRaises(TypeError):
            pickle.dumps(t)


STORED = ('{"claudeAiOauth":{"accessToken":"A1","refreshToken":"R1","expiresAt":1,"scopes":["user:inference"],'
          '"subscriptionType":"max","rateLimitTier":"default_x","x-new":7},"mcpOAuth":{"s":{"k":1}}}')


class TestDocument(TripwireCase):
    def test_exact_preserved_document(self):
        tokens = refresh.parse_token_response(RESPONSE, T, "R1")
        out = refresh.dump_document(refresh.build_document(json.loads(STORED), tokens))
        self.assertEqual(
            out,
            '{"claudeAiOauth":{"accessToken":"A2","refreshToken":"R2","expiresAt":1800028800000,'
            '"scopes":["user:inference","user:profile"],"subscriptionType":"max","rateLimitTier":"default_x",'
            '"x-new":7},"mcpOAuth":{"s":{"k":1}}}')

    def test_scope_without_inference_keeps_stored(self):
        tokens = refresh.parse_token_response(
            '{"access_token":"A2","expires_in":60,"scope":"user:profile"}', T, "R1")
        doc = refresh.build_document(json.loads(STORED), tokens)
        self.assertEqual(doc["claudeAiOauth"]["scopes"], ["user:inference"])

    def test_refresh_expiry_kept_or_appended(self):
        stored = json.loads(STORED)
        stored["claudeAiOauth"]["refreshTokenExpiresAt"] = 5
        none_exp = refresh._Tokens("A2", "R2", 9, None, None)
        self.assertEqual(refresh.build_document(stored, none_exp)["claudeAiOauth"]["refreshTokenExpiresAt"], 5)
        with_exp = refresh._Tokens("A2", "R2", 9, 9, None)
        inner = refresh.build_document(json.loads(STORED), with_exp)["claudeAiOauth"]
        self.assertEqual(list(inner)[-1], "refreshTokenExpiresAt")
        self.assertEqual(inner["refreshTokenExpiresAt"], 9)

    def test_input_not_mutated(self):
        stored = json.loads(STORED)
        before = copy.deepcopy(stored)
        refresh.build_document(stored, refresh._Tokens("A2", "R2", 9, 9, ["user:inference"]))
        self.assertEqual(stored, before)

    def test_non_ascii_unescaped(self):
        stored = json.loads(STORED)
        stored["label"] = "한글"
        out = refresh.dump_document(refresh.build_document(stored, refresh._Tokens("A2", "R2", 9, None, None)))
        self.assertIn('"label":"한글"', out)


class TestCcLock(TripwireCase):
    def setUp(self):
        super().setUp()
        os.makedirs(BASE, exist_ok=True)
        td = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(td.cleanup)
        self.home = os.path.join(td.name, "p1", "home")
        os.makedirs(self.home)
        self.new_dir = os.path.join(self.home, ".oauth_refresh.lock")
        self.legacy_dir = os.path.realpath(self.home) + ".lock"

    def test_success_and_release(self):
        lock, reason = refresh.take_cc_lock(self.home)
        self.assertIsInstance(lock, refresh.CcLock)
        self.assertIsNone(reason)
        self.assertTrue(os.path.isdir(self.new_dir))
        self.assertTrue(os.path.isdir(self.legacy_dir))
        lock.release()
        self.assertFalse(os.path.exists(self.new_dir))
        self.assertFalse(os.path.exists(self.legacy_dir))
        lock.release()

    def test_existing_new_dir_is_busy(self):
        os.mkdir(self.new_dir)
        self.assertEqual(refresh.take_cc_lock(self.home), (None, "cc_lock_busy"))
        self.assertFalse(os.path.exists(self.legacy_dir))
        self.assertTrue(os.path.isdir(self.new_dir))

    def test_existing_legacy_dir_is_busy_and_new_dir_undone(self):
        os.mkdir(self.legacy_dir)
        self.assertEqual(refresh.take_cc_lock(self.home), (None, "cc_lock_busy"))
        self.assertFalse(os.path.exists(self.new_dir))
        self.assertTrue(os.path.isdir(self.legacy_dir))

    def test_old_dir_is_stale_and_kept(self):
        os.mkdir(self.new_dir)
        t = time.time() - 120
        os.utime(self.new_dir, (t, t))
        self.assertEqual(refresh.take_cc_lock(self.home), (None, "cc_lock_stale"))
        self.assertTrue(os.path.isdir(self.new_dir))
        fixed = t + 120
        self.assertEqual(refresh.take_cc_lock(self.home, now_s=lambda: fixed), (None, "cc_lock_stale"))
        self.assertEqual(refresh.take_cc_lock(self.home, now_s=lambda: t + 59), (None, "cc_lock_busy"))

    def test_owner_file_untouched(self):
        owner = self.new_dir + ".owner"
        with open(owner, "w", encoding="utf-8") as f:
            f.write("owner-record")
        old = time.time() - 500
        os.utime(owner, (old, old))
        before = os.stat(owner).st_mtime
        lock, _ = refresh.take_cc_lock(self.home)
        lock.touch()
        lock.release()
        with open(owner, encoding="utf-8") as f:
            self.assertEqual(f.read(), "owner-record")
        self.assertEqual(os.stat(owner).st_mtime, before)

    def test_replaced_dir_left_in_place(self):
        lock, _ = refresh.take_cc_lock(self.home)
        # Claude Code judged our dir stale and recreated its own in the same place.
        os.rmdir(self.new_dir)
        os.mkdir(self.new_dir)
        with self.assertLogs("rotator.refresh", "WARNING") as cm:
            lock.release()
        self.assertTrue(os.path.isdir(self.new_dir))
        self.assertFalse(os.path.exists(self.legacy_dir))
        self.assertEqual(cm.output, ["WARNING:rotator.refresh:event=cc_lock_release_skipped reason=not_ours"])
        os.rmdir(self.new_dir)

    def test_identity_mismatch_or_missing_skips(self):
        lock, _ = refresh.take_cc_lock(self.home)
        lock._dirs = [(self.new_dir, (0, 0.0)), (self.legacy_dir, lock._dirs[1][1])]
        os.rmdir(self.legacy_dir)  # gone before release
        with self.assertLogs("rotator.refresh", "WARNING") as cm:
            lock.release()
        self.assertTrue(os.path.isdir(self.new_dir))
        self.assertEqual(len(cm.output), 2)
        os.rmdir(self.new_dir)

    def test_failed_take_removes_only_own_dir(self):
        os.mkdir(self.legacy_dir)
        self.assertEqual(refresh.take_cc_lock(self.home), (None, "cc_lock_busy"))
        self.assertFalse(os.path.exists(self.new_dir))
        self.assertTrue(os.path.isdir(self.legacy_dir))

    def test_touch_moves_mtime_forward(self):
        lock, _ = refresh.take_cc_lock(self.home)
        self.addCleanup(lock.release)
        back = time.time() - 30
        for d in (self.new_dir, self.legacy_dir):
            os.utime(d, (back, back))
        before = time.time()
        lock.touch()
        for d in (self.new_dir, self.legacy_dir):
            self.assertGreaterEqual(os.stat(d).st_mtime, before - 1)


T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)
ACC_A = "00000000-0000-4000-8000-000000000001"
ACC_B = "00000000-0000-4000-8000-000000000002"
ACCT = "alice"


def prof(aid):
    home = "/p/%s/home" % aid
    return Profile(account_id=aid, home=home, keychain_service=accounts.keychain_service_for(home))


def sess(pid, home):
    return Session(pid=pid, session_id="s-%d" % pid, cwd="/w", status="idle", config_dir=home, argv=())


def item(access="A1", refresh_="R1", expires=T0_MS - 3_600_000, top=None, **inner):
    oauth = {"accessToken": access, "refreshToken": refresh_, "expiresAt": expires, "scopes": ["user:inference"]}
    oauth.update(inner)
    doc = {"claudeAiOauth": oauth}
    doc.update(top or {})
    return json.dumps(doc, separators=(",", ":"))


class _FakeLock:
    def __init__(self, world, path):
        self.world, self.path = world, path

    def release(self):
        self.world.lock_table.discard(self.path)
        self.world.calls.append(("release", self.path))


class _FakeCcLock:
    def __init__(self, world):
        self.world = world

    def touch(self):
        self.world.calls.append(("touch",))

    def release(self):
        self.world.calls.append(("cc_release",))


class FakeWorld:
    """Every seam a Refresher takes, as recording fakes over in-memory state."""

    def __init__(self, lock_table=None):
        self.calls = []
        self.items = {}
        self.read_queue = []
        self.exists_queue = []
        self.write_results = []
        self.responses = []
        self.posts = []
        self.sessions = []
        self.sessions_later = None
        self.discover_raises = None
        self.registry = {}
        self.list_dir_raises = {}
        self.alive = set()
        self.paths = set()
        self.lock_table = set() if lock_table is None else lock_table
        self.cc_reason = None
        self.now_v = T0
        self.mono_v = 1000.0

    def read_item(self, svc, acct):
        self.calls.append(("read", svc, acct))
        if self.read_queue:
            return self.read_queue.pop(0)
        return self.items.get((svc, acct))

    def item_exists(self, svc, acct):
        self.calls.append(("exists", svc, acct))
        if self.exists_queue:
            return self.exists_queue.pop(0)
        return (svc, acct) in self.items

    def account_name(self):
        self.calls.append(("acct",))
        return ACCT

    def write_item(self, svc, acct, text):
        self.calls.append(("write", svc, acct, text))
        res = self.write_results.pop(0) if self.write_results else True
        if isinstance(res, Exception):
            raise res
        if res is True:
            self.items[(svc, acct)] = text
        return res

    def post(self, url, headers, data, timeout):
        self.calls.append(("post",))
        self.posts.append({"url": url, "headers": dict(headers), "body": json.loads(data), "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected post")
        res = self.responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return res

    def discover(self, homes):
        self.calls.append(("discover",))
        if self.discover_raises is not None:
            raise self.discover_raises
        if self.sessions_later:
            return list(self.sessions_later.pop(0))
        return list(self.sessions)

    def list_dir(self, path):
        self.calls.append(("list_dir", path))
        if path in self.list_dir_raises:
            raise self.list_dir_raises[path]
        if path not in self.registry:
            raise FileNotFoundError(path)
        return list(self.registry[path])

    def pid_alive(self, pid):
        return pid in self.alive

    def exists(self, path):
        return path in self.paths

    def lock(self, path):
        self.calls.append(("lock", path))
        if path in self.lock_table:
            return None
        self.lock_table.add(path)
        return _FakeLock(self, path)

    def cc_lock(self, home):
        self.calls.append(("cc_lock", home))
        if self.cc_reason is not None:
            return None, self.cc_reason
        return _FakeCcLock(self), None

    def now(self):
        return self.now_v

    def monotonic(self):
        return self.mono_v

    def sleep(self, s):
        self.calls.append(("sleep", s))
        self.mono_v += s

    def refresher(self, cfg=None):
        return refresh.Refresher(
            cfg or dataclasses.replace(Config(), state_dir=BASE), read_item=self.read_item,
            item_exists=self.item_exists, account_name=self.account_name, write_item=self.write_item,
            post=self.post, discover=self.discover, list_dir=self.list_dir, pid_alive=self.pid_alive,
            exists=self.exists, lock=self.lock, cc_lock=self.cc_lock, now=self.now, monotonic=self.monotonic,
            sleep=self.sleep, log=logging.getLogger("rotator.refresh"))

    def kinds(self):
        return [c[0] for c in self.calls]


NO_SIDE_EFFECTS = ("post", "write", "lock", "cc_lock")


class GateCase(TripwireCase):
    def setUp(self):
        super().setUp()
        self.w = FakeWorld()
        self.p = prof(ACC_A)
        self.svc = self.p.keychain_service

    def put(self, text):
        self.w.items[(self.svc, ACCT)] = text

    def run_gate(self, cfg=None, network=True):
        return self.w.refresher(cfg).refresh(self.p, [self.p], network=network)

    def assert_quiet(self):
        for k in NO_SIDE_EFFECTS:
            self.assertNotIn(k, self.w.kinds())

    def assert_shape(self, rec):
        self.assertEqual(set(rec), {"state", "reason", "at", "last_refreshed_at"})
        self.assertEqual(rec["at"], T0.isoformat())
        self.assertIsNone(rec["last_refreshed_at"])


class TestGate(GateCase):
    def test_disabled(self):
        self.put(item())
        cfg = dataclasses.replace(Config(), state_dir=BASE, token_refresh=False)
        rec = self.run_gate(cfg)
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "disabled"))
        self.assertNotIn("read", self.w.kinds())
        self.assert_quiet()
        self.assert_shape(rec)

    def test_disabled_via_parse_config(self):
        self.put(item())
        cfg = config.parse_config({"token_refresh": False}, state_dir=BASE)
        rec = self.run_gate(cfg)
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "disabled"))
        self.assertNotIn("read", self.w.kinds())

    def test_dry_run(self):
        self.put(item())
        rec = self.run_gate(dataclasses.replace(Config(), state_dir=BASE, dry_run=True))
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "dry_run"))
        self.assertNotIn("read", self.w.kinds())
        self.assert_quiet()

    def test_missing_item_classes(self):
        for queue, reason in (([False], "keychain_item_unclear"), ([None], "no_credential"),
                              ([True], "no_credential")):
            with self.subTest(queue=queue):
                self.w = FakeWorld()
                self.w.exists_queue = list(queue)
                rec = self.run_gate()
                self.assertEqual((rec["state"], rec["reason"]), ("skipped", reason))
                self.assert_quiet()

    def test_unparsable_item(self):
        for text in ("{", '{"claudeAiOauth":{"accessToken":"A1","refreshToken":"R1","expiresAt":NaN}}', '{"x":1}',
                     '{"claudeAiOauth":{"accessToken":"A1","refreshToken":"R1","expiresAt":true}}'):
            with self.subTest(text=text):
                self.w = FakeWorld()
                self.put(text)
                rec = self.run_gate()
                self.assertEqual((rec["state"], rec["reason"]), ("skipped", "no_credential"))
                self.assert_quiet()

    def test_fresh(self):
        self.put(item(expires=T0_MS + 6 * 60_000))
        rec = self.run_gate()
        self.assertEqual((rec["state"], rec["reason"]), ("not_needed", "fresh"))
        self.assert_quiet()

    def test_near_expiry_and_expired_pass(self):
        for exp in (T0_MS + 4 * 60_000, T0_MS - 3_600_000):
            with self.subTest(exp=exp):
                self.w = FakeWorld()
                self.put(item(expires=exp))
                rec = self.run_gate(network=False)
                self.assertEqual((rec["state"], rec["reason"]), ("skipped", "deferred"))
                self.assert_quiet()

    def test_no_refresh_token(self):
        self.put(item(refresh_=""))
        rec = self.run_gate()
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "no_refresh_token"))
        self.assert_quiet()

    def test_plaintext_store(self):
        self.put(item())
        self.w.paths.add("/p/%s/home/.credentials.json" % ACC_A)
        rec = self.run_gate()
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "plaintext_store"))
        self.assert_quiet()

    def test_live_session(self):
        self.put(item())
        self.w.sessions = [sess(10, self.p.home)]
        rec = self.run_gate()
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "live_session"))
        self.assert_quiet()

    def test_discovery_failed(self):
        self.put(item())
        self.w.discover_raises = RuntimeError()
        rec = self.run_gate()
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "discovery_failed"))
        self.assert_quiet()

    def test_payload_too_large(self):
        self.put(item(pad="x" * 3000))
        rec = self.run_gate()
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "payload_too_large"))
        self.assert_quiet()

    def test_reads_use_claude_code_account(self):
        self.w.exists_queue = [False]
        self.run_gate()
        self.put(item())
        self.run_gate(network=False)
        keyed = [c for c in self.w.calls if c[0] in ("read", "exists")]
        self.assertTrue(keyed)
        for c in keyed:
            self.assertEqual(c[1:], (self.svc, "alice"))

    def test_record_is_a_copy(self):
        self.put(item(expires=T0_MS + 6 * 60_000))
        r = self.w.refresher()
        rec = r.refresh(self.p, [self.p])
        self.assert_shape(rec)
        st = r._state(ACC_A)
        self.assertIsNot(rec, st.record)
        rec["state"] = "mutated"
        self.assertEqual(st.record["state"], "not_needed")
        again = r.refresh(self.p, [self.p])
        self.assertEqual(again["state"], "not_needed")

    def test_previous_record_is_a_copy(self):
        r = self.w.refresher()
        st = r._state(ACC_A)
        st.record = {"state": "failed", "reason": "server_error", "at": "x", "last_refreshed_at": None}
        st.until = self.w.mono_v + 10
        prev = r.refresh(self.p, [self.p])
        self.assertIsNot(prev, st.record)
        prev["reason"] = "mutated"
        self.assertEqual(st.record["reason"], "server_error")

    def test_payload_margin_boundary(self):
        # The line itself fits under 4032 but not with the 800-char margin for the new tokens.
        base = item(pad="")
        fixed = len(accounts.security_add_line(self.svc, ACCT, base))
        pad_n = (4032 - 800 - fixed) // 2 + 1
        text = item(pad="x" * pad_n)
        line = len(accounts.security_add_line(self.svc, ACCT, text))
        self.assertLess(line, 4032)
        self.assertGreater(line + 800, 4032)
        self.put(text)
        rec = self.run_gate(network=False)
        self.assertEqual((rec["state"], rec["reason"]), ("skipped", "payload_too_large"))
        self.w = FakeWorld()
        self.put(item(pad="x" * (pad_n - 2)))
        self.assertEqual(self.run_gate(network=False)["reason"], "deferred")

    def test_backoff_returns_previous_without_read(self):
        self.put(item())
        r = self.w.refresher()
        st = r._state(ACC_A)
        prev = {"state": "failed", "reason": "server_error", "at": "x", "last_refreshed_at": None}
        st.record = dict(prev)
        st.until = self.w.mono_v + 10
        self.assertEqual(r.refresh(self.p, [self.p], network=False), prev)
        self.assertNotIn("read", self.w.kinds())
        self.w.mono_v += 11
        rec = r.refresh(self.p, [self.p], network=False)
        self.assertIn("read", self.w.kinds())
        self.assertEqual(rec["reason"], "deferred")

    def test_stop_fingerprint(self):
        self.put(item())
        r = self.w.refresher()
        st = r._state(ACC_A)
        stopped = {"state": "stopped", "reason": "invalid_grant", "at": "x", "last_refreshed_at": None}
        st.stop_fp = refresh._fp("R1")
        st.record = dict(stopped)
        self.assertEqual(r.refresh(self.p, [self.p]), stopped)
        self.assertNotIn("post", self.w.kinds())
        self.put(item(refresh_="R2"))
        rec = r.refresh(self.p, [self.p], network=False)
        self.assertEqual(rec["reason"], "deferred")
        self.assertIsNone(st.stop_fp)

    def test_seam_raising_is_internal_error_without_secret(self):
        self.put(item())
        r = self.w.refresher()

        def boom():
            raise OSError(SECRET_R)
        r._account_name = boom
        with self.assertLogs("rotator.refresh", "INFO") as cm:
            rec = r.refresh(self.p, [self.p])
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "internal_error"))
        self.assertIn("ERROR:rotator.refresh:event=token_refresh_error account=%s exc=OSError" % ACC_A, cm.output)
        self.assertNotIn(SECRET_R, json.dumps(rec))
        self.assertNotIn(SECRET_R, "\n".join(cm.output))
        self.assertEqual(r.refresh(self.p, [self.p]), rec)
        self.assertNotIn("read", self.w.kinds())

    def test_constructor(self):
        w = self.w
        with self.assertRaises(TypeError):
            refresh.Refresher(Config(), read_item=w.read_item)
        r = w.refresher()
        self.assertEqual(w.calls, [])
        self.assertEqual(repr(r), "Refresher(<redacted>)")
        self.assertEqual(str(r), "Refresher(<redacted>)")


class TestLive(TripwireCase):
    def setUp(self):
        super().setUp()
        self.w = FakeWorld()
        self.a, self.b = prof(ACC_A), prof(ACC_B)
        self.ps = [self.a, self.b]
        self.reg = "/p/%s/home/sessions" % ACC_A

    def live(self):
        return self.w.refresher()._live(self.a, self.ps)

    def test_session_on_account(self):
        self.w.sessions = [sess(10, self.a.home)]
        self.assertEqual(self.live(), "live_session")

    def test_session_on_other_account(self):
        self.w.sessions = [sess(10, self.b.home)]
        self.assertIsNone(self.live())

    def test_registry_alive_pid(self):
        self.w.registry = {self.reg: ["123.json"]}
        self.w.alive = {123}
        self.assertEqual(self.live(), "live_session")
        self.w.alive = set()
        self.assertIsNone(self.live())

    def test_registry_bad_names_ignored(self):
        self.w.registry = {self.reg: ["x.json", "12a.json"]}
        self.w.alive = {12}
        self.assertIsNone(self.live())

    def test_discovery_raising(self):
        self.w.discover_raises = RuntimeError("x")
        self.assertEqual(self.live(), "discovery_failed")

    def test_registry_unreadable(self):
        self.w.list_dir_raises = {self.reg: PermissionError()}
        self.assertEqual(self.live(), "discovery_failed")

    def test_registry_missing(self):
        self.assertIsNone(self.live())


class WindowCase(TripwireCase):
    def setUp(self):
        super().setUp()
        self.w = FakeWorld()
        self.p = prof(ACC_A)
        self.svc = self.p.keychain_service
        self.w.items[(self.svc, ACCT)] = item()
        self.r = self.w.refresher()
        self.st = self.r._state(ACC_A)
        self.tokens = refresh._Tokens("A2", "R2", T0_MS + 28_800_000, None, None)
        self.posted = refresh._fp("R1")

    def put(self, text):
        self.w.items[(self.svc, ACCT)] = text

    def built(self, base_text, tokens=None):
        return refresh.dump_document(refresh.build_document(json.loads(base_text), tokens or self.tokens))

    def window(self, tokens=None):
        rl = self.w.lock("/l/r")
        cc = self.w.cc_lock(self.p.home)[0]
        self.w.calls.clear()
        self.rl = rl
        return self.r._write_window(self.p, self.st, ACCT, tokens or self.tokens, self.posted, rl, cc)

    def writes(self):
        return [c for c in self.w.calls if c[0] == "write"]


class TestCas2(WindowCase):
    def classify(self, tokens=None):
        return self.r._classify(self.svc, ACCT, tokens or self.tokens, self.posted)

    def test_landed(self):
        self.put(item(access="A2", refresh_="R2"))
        self.assertEqual(self.classify()[0], "landed")

    def test_writable_on_posted_token(self):
        self.assertEqual(self.classify(), ("writable", json.loads(item())))

    def test_writable_on_empty_token(self):
        self.put(item(refresh_=""))
        self.assertEqual(self.classify()[0], "writable")

    def test_conflict_on_other_token(self):
        self.put(item(refresh_="R9"))
        self.assertEqual(self.classify()[0], "conflict")

    def test_unrotated_response_is_writable(self):
        kept = refresh._Tokens("A2", "R1", 9, None, None)
        self.assertEqual(self.classify(kept)[0], "writable")

    def test_unreadable_texts(self):
        for text in ("{", '{"claudeAiOauth":{"refreshToken":7}}'):
            with self.subTest(text=text):
                self.put(text)
                self.assertEqual(self.classify(), ("unreadable", None))

    def test_unreadable_when_existence_unclear(self):
        for flag in (None, True):
            with self.subTest(flag=flag):
                self.w.read_queue = [None]
                self.w.exists_queue = [flag]
                self.assertEqual(self.classify(), ("unreadable", None))

    def test_conflict_needs_two_not_found(self):
        self.w.read_queue = [None, None]
        self.w.exists_queue = [False, False]
        self.assertEqual(self.classify(), ("conflict", None))
        self.assertEqual(self.w.kinds().count("read"), 2)
        self.assertEqual(self.w.kinds().count("exists"), 2)

    def test_second_check_unclear_is_unreadable(self):
        self.w.read_queue = [None, None]
        self.w.exists_queue = [False, None]
        self.assertEqual(self.classify(), ("unreadable", None))

    def test_second_read_finds_item(self):
        self.w.read_queue = [None, item()]
        self.w.exists_queue = [False]
        self.assertEqual(self.classify()[0], "writable")


class TestWriteWindow(WindowCase):
    def test_success(self):
        expected = self.built(item())
        rec = self.window()
        self.assertEqual([k for k in self.w.kinds() if k in ("read", "write")], ["read", "write", "read"])
        self.assertEqual(self.writes()[0][3], expected)
        self.assertEqual((rec["state"], rec["reason"]), ("refreshed", "ok"))
        self.assertEqual(rec["last_refreshed_at"], T0.isoformat())
        self.assertIsNone(self.st.held)

    def test_all_writes_fail_holds_set(self):
        tokens = refresh._Tokens(SECRET_A, SECRET_R, T0_MS + 1, None, None)
        self.w.write_results = [False, False, False]
        with self.assertLogs("rotator.refresh", "INFO") as cm:
            rec = self.window(tokens)
        self.assertEqual(len(self.writes()), 3)
        reads_before_write = [i for i, c in enumerate(self.w.calls) if c[0] == "write"]
        for i in reads_before_write:
            self.assertIn(("touch",), self.w.calls[:i])
        self.assertIn(("sleep", 0.1), self.w.calls)
        self.assertIn(("sleep", 0.2), self.w.calls)
        self.assertEqual(len([c for c in self.w.calls if c[0] == "sleep"]), 2)
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "write_failed"))
        held = self.st.held
        self.assertIsInstance(held, refresh._Held)
        self.assertIs(held.lock, self.rl)
        self.assertEqual(held.flush_until, self.w.mono_v)
        self.assertEqual(held.flush_step_s, 0.0)
        errors = [l for l in cm.output if l.startswith("ERROR:")]
        self.assertEqual(errors, ["ERROR:rotator.refresh:event=token_refresh_write_failed account=%s" % ACC_A])
        for text in (repr(held), str(held), repr(tokens), str(tokens), repr(self.r), "\n".join(cm.output),
                     json.dumps(rec)):
            self.assertNotIn(SECRET_A, text)
            self.assertNotIn(SECRET_R, text)
        with self.assertRaises(TypeError):
            pickle.dumps(held)

    def test_each_attempt_rereads(self):
        self.w.write_results = [False, False, False]
        self.window()
        kinds = [k for k in self.w.kinds() if k in ("read", "write")]
        self.assertEqual(kinds, ["read", "write", "read"] * 3)

    def test_write_reported_false_but_landed(self):
        world = self.w
        built = self.built(item())

        def write(svc, acct, text):
            world.calls.append(("write", svc, acct, text))
            world.items[(svc, acct)] = built
            return False
        self.r._write_item = write
        rec = self.window()
        self.assertEqual((rec["state"], rec["reason"]), ("refreshed", "ok"))
        self.assertEqual(len(self.writes()), 1)

    def test_verify_mismatch_fails_attempt(self):
        world = self.w
        orig = world.read_item
        state = {"after_write": False}

        def read(svc, acct):
            if state["after_write"]:
                state["after_write"] = False
                world.calls.append(("read", svc, acct))
                return item(access="AX")
            return orig(svc, acct)

        def write(svc, acct, text):
            world.calls.append(("write", svc, acct, text))
            world.items[(svc, acct)] = text
            state["after_write"] = True
            return True
        self.r._read_item = read
        self.r._write_item = write
        rec = self.window()
        self.assertIn(("sleep", 0.1), self.w.calls)
        self.assertEqual(self.w.kinds().count("read"), 3)
        self.assertEqual((rec["state"], rec["reason"]), ("refreshed", "ok"))

    def test_write_raising_holds_set(self):
        self.w.write_results = [RuntimeError(SECRET_R)]
        with self.assertLogs("rotator.refresh", "INFO") as cm:
            rec = self.window()
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "write_failed"))
        self.assertIsInstance(self.st.held, refresh._Held)
        self.assertIn("ERROR:rotator.refresh:event=token_refresh_error account=%s exc=RuntimeError" % ACC_A, cm.output)
        self.assertNotIn(SECRET_R, "\n".join(cm.output))

    def test_conflict_drops_set(self):
        self.put(item(refresh_="R9"))
        with self.assertLogs("rotator.refresh", "INFO") as cm:
            rec = self.window()
        self.assertEqual(self.writes(), [])
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "conflict"))
        self.assertIsNone(self.st.held)
        self.assertIn("ERROR:rotator.refresh:event=token_refresh_conflict account=%s" % ACC_A, cm.output)
        self.assertEqual(self.st.until, 0.0)

    def test_item_gone_twice_is_conflict(self):
        self.w.read_queue = [None, None]
        self.w.exists_queue = [False, False]
        rec = self.window()
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "conflict"))
        self.assertIsNone(self.st.held)

    def test_unreadable_every_attempt_holds(self):
        self.w.read_queue = [None, None, None]
        self.w.exists_queue = [None, None, None]
        rec = self.window()
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "write_failed"))
        self.assertIsInstance(self.st.held, refresh._Held)
        self.assertEqual(self.writes(), [])

    def test_write_base_is_fresh_read(self):
        self.put(item(top={"mcpOAuth": {"s": {"k": 1}}}))
        queued = item(top={"mcpOAuth": {"s": {"k": 2}, "t": {}}})
        self.w.read_queue = [queued]
        self.window()
        written = self.writes()[0][3]
        self.assertEqual(written, self.built(queued))
        self.assertIn('"mcpOAuth":{"s":{"k":2},"t":{}}', written)
        self.assertIn('"accessToken":"A2"', written)

    def test_oversized_line_never_written(self):
        self.put(item(pad="x" * 2500))
        rec = self.window()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.w.kinds().count("read"), 3)
        self.assertEqual((rec["state"], rec["reason"]), ("failed", "write_failed"))
        self.assertIsInstance(self.st.held, refresh._Held)


OK200 = refresh.PostResult(200, json.dumps({"access_token": "A2", "refresh_token": "R2", "expires_in": 28800,
                                            "scope": "user:inference user:profile"}))


def err(status, error):
    return refresh.PostResult(status, json.dumps({"error": error}))


LOCK_PATH = os.path.join(BASE, "locks", "r-" + hashlib.sha256(ACC_A.encode()).hexdigest()[:32] + ".lock")


class SectionCase(TripwireCase):
    def setUp(self):
        super().setUp()
        self.w = FakeWorld()
        self.p = prof(ACC_A)
        self.svc = self.p.keychain_service
        self.put(item())
        self.r = self.w.refresher()

    def put(self, text):
        self.w.items[(self.svc, ACCT)] = text

    def go(self, r=None):
        return (r or self.r).refresh(self.p, [self.p])

    def sr(self, rec):
        return rec["state"], rec["reason"]

    def st(self):
        return self.r._state(ACC_A)

    def section_calls(self):
        kinds = self.w.kinds()
        return kinds[kinds.index("cc_lock"):]


class TestSection(SectionCase):
    def test_happy_path_order(self):
        self.w.responses = [OK200]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("refreshed", "ok"))
        core = [k for k in self.w.kinds() if k not in ("acct", "list_dir", "exists")]
        # the gate's read + discover come first
        gate = core[:core.index("lock")]
        self.assertEqual(gate, ["read", "discover"])
        self.assertEqual(core[core.index("lock"):], [
            "lock", "cc_lock",
            "touch", "read",
            "touch", "discover",
            "touch", "write", "touch", "read",
            "touch", "discover",
            "touch", "post",
            "touch", "read", "touch", "write", "touch", "read",
            "cc_release", "release"])
        probe = [c for c in self.w.calls if c[0] == "write"][0]
        self.assertEqual(probe[3], item())
        calls = self.w.calls
        start = self.w.kinds().index("cc_lock")
        end = self.w.kinds().index("cc_release")
        for i in range(start + 1, end):
            if calls[i][0] in ("read", "write", "post"):
                self.assertEqual(calls[i - 1], ("touch",), (i, calls[i]))
        self.assertEqual(self.w.lock_table, set())
        self.assertIn(("lock", LOCK_PATH), self.w.calls)

    def test_lock_taken(self):
        self.w.lock_table.add(LOCK_PATH)
        rec = self.go()
        self.assertEqual(self.sr(rec), ("skipped", "locked"))
        self.assertNotIn("cc_lock", self.w.kinds())
        self.assertNotIn("post", self.w.kinds())

    def test_concurrent_refresher_sees_locked(self):
        other = FakeWorld(lock_table=self.w.lock_table)
        other.items = dict(self.w.items)
        held = self.w.lock(LOCK_PATH)
        rec = other.refresher().refresh(self.p, [self.p])
        self.assertEqual(self.sr(rec), ("skipped", "locked"))
        self.assertNotIn("post", other.kinds())
        held.release()
        other.responses = [OK200]
        self.assertEqual(self.sr(other.refresher().refresh(self.p, [self.p])), ("refreshed", "ok"))

    def test_cc_lock_reasons_release_rotator_lock(self):
        for reason in ("cc_lock_busy", "cc_lock_stale"):
            with self.subTest(reason=reason):
                self.w = FakeWorld()
                self.put(item())
                self.w.cc_reason = reason
                rec = self.w.refresher().refresh(self.p, [self.p])
                self.assertEqual(self.sr(rec), ("skipped", reason))
                self.assertIn(("release", LOCK_PATH), self.w.calls)
                self.assertEqual(self.w.lock_table, set())
                self.assertNotIn("post", self.w.kinds())

    def test_changed_under_lock(self):
        for second in (item(access="A2", refresh_="R2"), item(refresh_="R2"), item(access="A2"),
                       item(expires=T0_MS + 3_600_000), None):
            with self.subTest(second=second):
                self.w = FakeWorld()
                self.put(item())
                self.w.read_queue = [item(), second]
                rec = self.w.refresher().refresh(self.p, [self.p])
                self.assertEqual(self.sr(rec), ("skipped", "changed_under_lock"))
                self.assertEqual(self.w.posts, [])
                self.assertNotIn("write", self.w.kinds())
                self.assertIn("cc_release", self.w.kinds())
                self.assertEqual(self.w.lock_table, set())

    def test_live_check_two(self):
        self.w.sessions_later = [[], [sess(1, self.p.home)]]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("skipped", "live_session"))
        self.assertNotIn("write", self.w.kinds())
        self.assertEqual(self.w.posts, [])
        self.assertIn("cc_release", self.w.kinds())
        self.assertEqual(self.w.lock_table, set())

    def test_live_check_three(self):
        self.w.sessions_later = [[], [], [sess(1, self.p.home)]]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("skipped", "live_session"))
        self.assertEqual(self.w.kinds().count("write"), 1)
        self.assertEqual(self.w.posts, [])

    def test_probe_failure_backs_off(self):
        self.w.write_results = [False]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("skipped", "keychain_write_unavailable"))
        self.assertEqual(self.w.posts, [])
        self.w.calls.clear()
        self.assertEqual(self.go(), rec)
        self.assertNotIn("read", self.w.kinds())
        self.w.mono_v += 301
        self.w.responses = [OK200]
        self.go()
        self.assertIn("read", self.w.kinds())

    def test_probe_raising_is_unavailable(self):
        self.w.write_results = [OSError(SECRET_R)]
        self.assertEqual(self.sr(self.go()), ("skipped", "keychain_write_unavailable"))
        self.assertEqual(self.w.posts, [])

    def test_probe_verify_mismatch(self):
        world = self.w
        orig = world.read_item
        state = {"n": 0}

        def read(svc, acct):
            state["n"] += 1
            if state["n"] == 3:  # gate, CAS #1, then the probe's verify read
                world.calls.append(("read", svc, acct))
                return item(access="AX")
            return orig(svc, acct)
        self.r._read_item = read
        self.assertEqual(self.sr(self.go()), ("skipped", "keychain_write_unavailable"))
        self.assertEqual(self.w.posts, [])

    def test_post_raising_releases_locks(self):
        self.w.responses = [RuntimeError("x")]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("stopped", "outcome_unknown"))
        self.assertIn("cc_release", self.w.kinds())
        self.assertIn(("release", LOCK_PATH), self.w.calls)
        self.assertEqual(self.w.lock_table, set())


class TestRequest(SectionCase):
    def test_request_shape(self):
        self.put(item(scopes=["user:inference", "user:profile", "user:projects:read"]))
        self.w.responses = [OK200]
        self.go()
        post = self.w.posts[0]
        self.assertEqual(post["url"], refresh.TOKEN_URL)
        self.assertEqual(post["url"], "https://platform.claude.com/v1/oauth/token")
        self.assertEqual(post["headers"], {"Content-Type": "application/json", "Accept": "application/json"})
        self.assertEqual(post["timeout"], 10.0)
        self.assertEqual(post["body"], {
            "grant_type": "refresh_token", "refresh_token": "R1", "client_id": refresh.CLIENT_ID,
            "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload "
                     "user:plugins user:projects:read"})

    def test_stored_client_id(self):
        self.put(item(scopes=["user:inference", "user:profile"], clientId="c-1"))
        self.w.responses = [OK200]
        self.go()
        body = self.w.posts[0]["body"]
        self.assertEqual(body["client_id"], "c-1")
        self.assertEqual(body["scope"], "user:inference user:profile")

    def test_invalid_scope_retried_once_with_stored(self):
        self.put(item(scopes=["user:inference", "user:profile", "user:projects:read"]))
        self.w.responses = [err(400, "invalid_scope"), OK200]
        rec = self.go()
        self.assertEqual(len(self.w.posts), 2)
        self.assertEqual(self.w.posts[1]["body"]["scope"], "user:inference user:profile user:projects:read")
        self.assertEqual(self.sr(rec), ("refreshed", "ok"))

    def test_invalid_scope_twice_stops(self):
        self.w.responses = [err(400, "invalid_scope"), err(400, "invalid_scope")]
        rec = self.go()
        self.assertEqual(len(self.w.posts), 2)
        self.assertEqual(self.sr(rec), ("stopped", "invalid_scope"))

    def test_invalid_scope_retry_skipped_past_budget(self):
        world = self.w
        orig = world.discover
        state = {"n": 0}

        def discover(homes):
            state["n"] += 1
            if state["n"] == 3:  # check #3
                world.mono_v += 31
            return orig(homes)
        self.r._discover = discover
        self.w.responses = [err(400, "invalid_scope")]
        rec = self.go()
        self.assertEqual(len(self.w.posts), 1)
        self.assertEqual(self.sr(rec), ("stopped", "invalid_scope"))


class TestFailures(SectionCase):
    def advance(self, s):
        self.w.mono_v += s
        self.w.now_v = datetime.fromtimestamp(self.w.now_v.timestamp() + s, timezone.utc)

    def test_invalid_grant_stops_until_item_changes(self):
        for status in (400, 401):
            with self.subTest(status=status):
                self.setUp()
                self.w.responses = [err(status, "invalid_grant")]
                rec = self.go()
                self.assertEqual(self.sr(rec), ("stopped", "invalid_grant"))
                for s in (30, 7200 - 30):
                    self.advance(s)
                    self.assertEqual(self.go(), rec)
                    self.assertEqual(len(self.w.posts), 1)
                self.put(item(refresh_="R5"))
                self.w.responses = [OK200]
                self.assertEqual(self.sr(self.go()), ("refreshed", "ok"))
                self.assertEqual(len(self.w.posts), 2)
                self.assertEqual(self.w.posts[1]["body"]["refresh_token"], "R5")

    def test_rejected_classes(self):
        for res in (refresh.PostResult(403, ""), err(400, "invalid_client"), refresh.PostResult(302, ""),
                    refresh.PostResult(101, "")):
            with self.subTest(status=res.status):
                self.setUp()
                self.w.responses = [res]
                self.assertEqual(self.sr(self.go()), ("stopped", "rejected"))
                self.assertEqual(self.go()["reason"], "rejected")
                self.assertEqual(len(self.w.posts), 1)

    def test_malformed_200(self):
        self.w.responses = [refresh.PostResult(200, "{}")]
        self.assertEqual(self.sr(self.go()), ("stopped", "malformed_response"))
        self.assertEqual(self.st().stop_fp, refresh._fp("R1"))
        self.assertEqual(self.w.kinds().count("write"), 1)  # the probe only

    def test_outcome_unknown(self):
        for exc in (socket.timeout(), ConnectionResetError(), http.client.RemoteDisconnected(),
                    urllib.error.URLError(socket.timeout())):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                self.w.responses = [exc]
                self.assertEqual(self.sr(self.go()), ("stopped", "outcome_unknown"))
                self.go()
                self.assertEqual(len(self.w.posts), 1)

    def test_spent_token_outcomes_logged_at_error(self):
        cases = ((refresh.PostResult(200, '{"access_token": 7, "x": "%s"}' % SECRET_R), "malformed_response"),
                 (socket.timeout(), "outcome_unknown"))
        for res, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                self.w.responses = [res]
                with self.assertLogs("rotator.refresh", "INFO") as cm:
                    self.assertEqual(self.sr(self.go()), ("stopped", reason))
                errors = [l for l in cm.output if l.startswith("ERROR:")]
                self.assertEqual(errors, ["ERROR:rotator.refresh:event=token_refresh_%s account=%s" % (reason, ACC_A)])
                self.assertNotIn(SECRET_R, "\n".join(cm.output))

    def test_network_error_backs_off(self):
        for exc in (urllib.error.URLError(socket.gaierror()), urllib.error.URLError(ConnectionRefusedError())):
            with self.subTest(exc=repr(exc.reason)):
                self.setUp()
                m = self.w.mono_v
                self.w.responses = [exc]
                self.assertEqual(self.sr(self.go()), ("failed", "network_error"))
                self.assertEqual(self.st().until, m + 300)

    def test_server_error_backoff_doubles_and_caps(self):
        m = self.w.mono_v
        self.w.responses = [refresh.PostResult(503, "")]
        self.assertEqual(self.sr(self.go()), ("failed", "server_error"))
        self.assertEqual(self.st().until, m + 300)
        self.advance(299)
        self.go()
        self.assertEqual(len(self.w.posts), 1)
        self.advance(1)
        self.w.responses = [refresh.PostResult(503, "")]
        self.go()
        self.assertEqual(len(self.w.posts), 2)
        self.assertEqual(self.st().until, m + 300 + 600)
        steps = []
        for _ in range(6):
            self.w.mono_v = self.st().until
            self.w.responses = [refresh.PostResult(503, "")]
            self.go()
            steps.append(self.st().step_s)
        self.assertEqual(steps, [1200.0, 2400.0, 3600.0, 3600.0, 3600.0, 3600.0])

    def test_rate_limited_honours_retry_after(self):
        m = self.w.mono_v
        self.w.responses = [refresh.PostResult(429, "", "1200")]
        self.assertEqual(self.sr(self.go()), ("failed", "rate_limited"))
        self.assertGreaterEqual(self.st().until, m + 1200)

    def test_success_clears_backoff(self):
        self.w.responses = [refresh.PostResult(503, "")]
        self.go()
        self.w.mono_v = self.st().until
        self.w.responses = [OK200]
        self.assertEqual(self.sr(self.go()), ("refreshed", "ok"))
        self.assertEqual(self.st().until, 0.0)
        self.assertEqual(self.st().step_s, 0.0)

    def test_restart_retries_stopped_account_once(self):
        self.w.responses = [err(400, "invalid_grant")]
        self.go()
        fresh = self.w.refresher()
        self.w.responses = [err(400, "invalid_grant")]
        self.assertEqual(self.sr(self.go(fresh)), ("stopped", "invalid_grant"))
        self.go(fresh)
        self.assertEqual(len(self.w.posts), 2)


class TestHeld(SectionCase):
    def hold(self):
        self.w.responses = [OK200]
        self.w.write_results = [True, False, False, False]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("failed", "write_failed"))
        self.assertIsNotNone(self.st().held)
        self.w.calls.clear()
        return rec

    def test_held_ids_and_flock_kept(self):
        self.hold()
        self.assertEqual(self.r.held_accounts(), [ACC_A])
        self.assertEqual(self.r.held_accounts(due_only=True), [ACC_A])
        self.assertIn(LOCK_PATH, self.w.lock_table)

    def test_other_refresher_locked_out_until_flushed(self):
        self.hold()
        w2 = FakeWorld(lock_table=self.w.lock_table)
        w2.items = self.w.items
        r2 = w2.refresher()
        self.assertEqual(self.sr(r2.refresh(self.p, [self.p])), ("skipped", "locked"))
        self.assertEqual(w2.posts, [])
        rec = self.go()
        self.assertEqual(self.sr(rec), ("refreshed", "write_recovered"))
        self.assertNotIn(LOCK_PATH, self.w.lock_table)
        tokens = refresh._Tokens("A2", "R2", T0_MS + 28_800_000, None, ["user:inference", "user:profile"])
        self.assertEqual(self.w.items[(self.svc, ACCT)],
                         refresh.dump_document(refresh.build_document(json.loads(item()), tokens)))
        self.assertEqual(self.sr(r2.refresh(self.p, [self.p])), ("not_needed", "fresh"))
        self.assertEqual(self.r.held_accounts(), [])
        self.assertNotIn("post", self.w.kinds())

    def test_network_false_answers_without_calls(self):
        rec = self.hold()
        self.assertEqual(self.r.refresh(self.p, [self.p], network=False), rec)
        self.assertEqual(self.w.calls, [])

    def test_flush_backoff(self):
        self.hold()
        m = self.w.mono_v
        self.w.write_results = [False]
        rec = self.go()
        self.assertEqual(self.sr(rec), ("failed", "write_failed"))
        self.assertEqual(self.r.held_accounts(due_only=True), [])
        self.assertEqual(self.r.held_accounts(), [ACC_A])
        self.w.calls.clear()
        self.w.mono_v = m + 29.9
        self.assertEqual(self.go(), rec)
        self.assertEqual(self.w.calls, [])
        self.w.mono_v = m + 30
        self.w.write_results = [False]
        self.go()
        self.assertEqual(self.st().held.flush_until, m + 30 + 60)
        steps = []
        for _ in range(6):
            self.w.mono_v = self.st().held.flush_until
            self.w.write_results = [False]
            self.go()
            steps.append(self.st().held.flush_step_s)
            self.assertEqual(self.r.held_accounts(), [ACC_A])
        self.assertEqual(steps, [120.0, 240.0, 480.0, 600.0, 600.0, 600.0])
        self.assertNotIn("post", self.w.kinds())

    def test_flush_ignores_live_disabled_and_dry_run(self):
        for change in ("live", "disabled", "dry_run"):
            with self.subTest(change=change):
                self.setUp()
                self.hold()
                if change == "live":
                    self.w.sessions = [sess(5, self.p.home)]
                elif change == "disabled":
                    self.r._cfg = dataclasses.replace(self.r._cfg, token_refresh=False)
                else:
                    self.r._cfg = dataclasses.replace(self.r._cfg, dry_run=True)
                self.assertEqual(self.sr(self.go()), ("refreshed", "write_recovered"))
                self.assertIn("write", self.w.kinds())

    def test_unreadable_keeps_set(self):
        for queue, exists in (([None], [None]), (["{"], [])):
            with self.subTest(queue=queue):
                self.setUp()
                self.hold()
                self.w.read_queue = list(queue)
                self.w.exists_queue = list(exists)
                self.assertEqual(self.sr(self.go()), ("failed", "write_failed"))
                self.assertIsNotNone(self.st().held)
                self.assertIn(LOCK_PATH, self.w.lock_table)
                self.assertNotIn("write", self.w.kinds())

    def test_item_gone_twice_drops_set(self):
        self.hold()
        self.w.read_queue = [None, None]
        self.w.exists_queue = [False, False]
        with self.assertLogs("rotator.refresh", "INFO") as cm:
            rec = self.go()
        self.assertEqual(self.sr(rec), ("failed", "conflict"))
        self.assertIsNone(self.st().held)
        self.assertNotIn(LOCK_PATH, self.w.lock_table)
        self.assertIn("ERROR:rotator.refresh:event=token_refresh_conflict account=%s" % ACC_A, cm.output)

    def test_item_back_on_second_read_is_written(self):
        self.hold()
        self.w.read_queue = [None, item()]
        self.w.exists_queue = [False]
        self.assertEqual(self.sr(self.go()), ("refreshed", "write_recovered"))
        self.assertIn("write", self.w.kinds())

    def test_already_landed(self):
        self.hold()
        self.put(item(access="A2", refresh_="R2", expires=T0_MS + 28_800_000))
        self.assertEqual(self.sr(self.go()), ("refreshed", "write_recovered"))
        self.assertNotIn("write", self.w.kinds())
        self.assertNotIn(LOCK_PATH, self.w.lock_table)

    def test_other_token_conflict(self):
        self.hold()
        self.put(item(refresh_="R9"))
        self.assertEqual(self.sr(self.go()), ("failed", "conflict"))
        self.assertNotIn("write", self.w.kinds())
        self.assertIsNone(self.st().held)
        self.assertNotIn(LOCK_PATH, self.w.lock_table)

    def test_flush_rebuilds_from_fresh_read(self):
        self.hold()
        changed = item(top={"mcpOAuth": {"s": {"k": 2}, "t": {}}})
        self.put(changed)
        self.go()
        tokens = refresh._Tokens("A2", "R2", T0_MS + 28_800_000, None, ["user:inference", "user:profile"])
        written = [c for c in self.w.calls if c[0] == "write"][0][3]
        self.assertEqual(written, refresh.dump_document(refresh.build_document(json.loads(changed), tokens)))

    def test_cc_lock_busy_is_failed_attempt(self):
        self.hold()
        m = self.w.mono_v
        self.w.cc_reason = "cc_lock_busy"
        self.assertEqual(self.sr(self.go()), ("failed", "write_failed"))
        self.assertNotIn("read", self.w.kinds())
        self.assertEqual(self.st().held.flush_until, m + 30)
        self.assertIsNotNone(self.st().held)

    def test_last_refreshed_at_carried(self):
        self.hold()
        t = self.w.now_v
        self.go()
        self.w.now_v = datetime.fromtimestamp(t.timestamp() + 30, timezone.utc)
        rec = self.go()
        self.assertEqual(self.sr(rec), ("not_needed", "fresh"))
        self.assertEqual(rec["last_refreshed_at"], t.isoformat())


NEW_BODY = json.dumps({"access_token": "NEW-" + SECRET_A, "refresh_token": "NEW-" + SECRET_R, "expires_in": 28800})
NEEDLES = [SECRET_A, SECRET_R, SECRET_A.encode().hex(), SECRET_R.encode().hex(), "NEW-" + SECRET_A,
           ("NEW-" + SECRET_R).encode().hex()]


class TestSecretChannels(TripwireCase):
    def setUp(self):
        super().setUp()
        self.w = FakeWorld()
        self.p = prof(ACC_A)
        self.w.items[(self.p.keychain_service, ACCT)] = item(access=SECRET_A, refresh_=SECRET_R)
        self.r = self.w.refresher()

    def _chain(self, e):
        out = []
        seen = set()
        while e is not None and id(e) not in seen:
            seen.add(id(e))
            out += [str(e), repr(e.args)]
            e = e.__context__ or e.__cause__
        return out

    def blob(self):
        parts = []
        with self.assertLogs("rotator", "DEBUG") as cm:
            for _ in range(2):
                try:
                    parts.append(json.dumps(self.r.refresh(self.p, [self.p])))
                except Exception as e:
                    parts += self._chain(e)
            logging.getLogger("rotator.refresh").debug("capture-alive")
        parts += cm.output
        parts += [repr(self.r), str(self.r)]
        held = self.r._state(ACC_A).held
        if held is not None:
            parts += [repr(held), str(held), repr(held.tokens), str(held.tokens)]
        for res in self.returned:
            parts += [repr(res), str(res)]
        return "\n".join(parts)

    def check(self, responses):
        self.returned = [r for r in responses if isinstance(r, refresh.PostResult)]
        self.w.responses = list(responses)
        text = self.blob()
        for needle in NEEDLES:
            self.assertNotIn(needle, text)
        self.assertIn(ACC_A, text)  # negative control: the capture really holds the refresher's output
        return text

    def test_success(self):
        text = self.check([refresh.PostResult(200, NEW_BODY)])
        self.assertIn("state=refreshed", text)

    def test_write_failed(self):
        # probe ok, 3 write attempts fail, then the second call's flush fails too
        self.w.write_results = [True, False, False, False, False]
        text = self.check([refresh.PostResult(200, NEW_BODY)])
        self.assertIsNotNone(self.r._state(ACC_A).held)
        self.assertIn("token_refresh_write_failed", text)

    def test_invalid_grant(self):
        self.check([err(400, "invalid_grant")])

    def test_malformed(self):
        self.check([refresh.PostResult(200, '{"access_token": 7, "x": "%s"}' % SECRET_R)])

    def test_server_error_echoing_body(self):
        self.check([refresh.PostResult(503, "upstream echoed " + SECRET_R)])

    def test_post_raising(self):
        self.check([OSError("boom " + SECRET_R)])

    def test_write_raising_after_200(self):
        self.w.write_results = [True, RuntimeError(SECRET_A)]
        text = self.check([refresh.PostResult(200, NEW_BODY)])
        self.assertIn("exc=RuntimeError", text)

    def test_account_name_raising(self):
        def boom():
            raise ValueError(SECRET_R)
        self.r._account_name = boom
        text = self.check([])
        self.assertIn("reason=internal_error", text)

    def test_needle_detection_is_live(self):
        # Proves the blob check bites: a planted secret in a log line is caught.
        with self.assertRaises(AssertionError):
            self.returned = []
            with self.assertLogs("rotator", "DEBUG") as cm:
                logging.getLogger("rotator.refresh").info("leak %s", SECRET_R)
            for needle in NEEDLES:
                self.assertNotIn(needle, "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
