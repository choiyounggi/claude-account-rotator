import http.client
import io
import json
import os
import socket
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from rotator.accounts import keychain_service_for
import rotator.usage
from rotator.models import USAGE_URL, AccountUsage, Profile, UsageWindow
from rotator.usage import (
    _NO_REDIRECT_OPENER,
    _NoRedirect,
    _urllib_http,
    fetch_usage,
    parse_resets_at,
    parse_window,
    parse_windows,
    read_cached_usage,
)

UTC = timezone.utc
ACC1 = "00000000-0000-4000-8000-000000000001"
HOME = "/x/claude-profiles/" + ACC1 + "/home"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROFILE = Profile(account_id=ACC1, home=HOME, keychain_service=keychain_service_for(HOME))
CLAUDE_JSON_PATH = os.path.join(HOME, ".claude.json")


class FakeFiles:
    def __init__(self, mapping):
        self.mapping = mapping

    def __call__(self, path):
        if path in self.mapping:
            return self.mapping[path]
        raise FileNotFoundError(path)


def default_cache():
    return {
        "accountUuid": ACC1,
        "fetchedAtMs": 1700000000000,
        "utilization": {
            "five_hour": {
                "utilization": 88,
                "resets_at": "2026-01-01T00:00:00.123456+00:00",
                "used_dollars": 1,
                "limit_dollars": 2,
                "remaining_dollars": 1,
                "locked_reason": None,
            },
            "seven_day": {"utilization": 10, "resets_at": None},
            "seven_day_opus": None,
            "extra_usage": {},
        },
    }


def claude_json(cache=None):
    return json.dumps(
        {
            "oauthAccount": {"emailAddress": "user@example.com", "displayName": "User Name", "accountUuid": ACC1},
            "cachedUsageUtilization": default_cache() if cache is None else cache,
        }
    )


class ModelsUsageTest(unittest.TestCase):
    def test_int_utilization_normalised_to_float(self):
        w = UsageWindow(50)
        self.assertEqual(w.utilization, 50.0)
        self.assertIs(type(w.utilization), float)

    def test_window_rejects_bad_utilization(self):
        for bad in (100.5, True, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    UsageWindow(bad)

    def test_window_bounds_accepted(self):
        self.assertEqual(UsageWindow(0).utilization, 0.0)
        self.assertEqual(UsageWindow(100).utilization, 100.0)

    def test_naive_fetched_at_rejected(self):
        with self.assertRaises(ValueError):
            AccountUsage(account_id=ACC1, source="cache", fetched_at=datetime(2026, 1, 1), five_hour=UsageWindow(1))

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            AccountUsage(account_id=ACC1, source="stale", fetched_at=NOW, five_hour=UsageWindow(1))

    def test_error_vocabulary_rejects_free_text(self):
        for bad in ("Bearer x", "network\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    AccountUsage(account_id=ACC1, source="cache", fetched_at=NOW, five_hour=UsageWindow(1), error=bad)


class MaxUtilizationTest(unittest.TestCase):
    def usage(self, five=None, seven=None):
        source = "cache" if (five or seven) else "unavailable"
        error = None if (five or seven) else "token_missing;cache_missing"
        return AccountUsage(account_id=ACC1, source=source, fetched_at=NOW, five_hour=five, seven_day=seven, error=error)

    def test_max_of_both(self):
        self.assertEqual(self.usage(UsageWindow(30), UsageWindow(85)).max_utilization(), 85.0)

    def test_only_seven_day(self):
        self.assertEqual(self.usage(None, UsageWindow(12)).max_utilization(), 12.0)

    def test_unavailable_is_none(self):
        self.assertIsNone(self.usage().max_utilization())

    def test_past_reset_counts_zero(self):
        u = self.usage(UsageWindow(30), UsageWindow(85, NOW - timedelta(seconds=1)))
        self.assertEqual(u.max_utilization(NOW), 30.0)
        self.assertEqual(u.max_utilization(), 85.0)

    def test_reset_equal_to_now_counts_zero(self):
        u = self.usage(UsageWindow(30, NOW), UsageWindow(10, NOW + timedelta(seconds=1)))
        self.assertEqual(u.max_utilization(NOW), 10.0)

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            self.usage(UsageWindow(30)).max_utilization(datetime(2026, 1, 1))


class ParseResetsAtTest(unittest.TestCase):
    def test_table(self):
        cases = [
            ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=UTC)),
            ("2026-01-01T00:00:00.5+00:00", datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)),
            ("2026-01-01T00:00:00.1234567+00:00", datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)),
            ("2026-01-01T00:00:00+0000", datetime(2026, 1, 1, tzinfo=UTC)),
            ("2026-10-26T10:39:59.610876+00:00", datetime(2026, 10, 26, 10, 39, 59, 610876, tzinfo=UTC)),
            ("2026-01-01T09:00:00+09:00", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)),
            ("2026-01-01T00:00:00", datetime(2026, 1, 1, tzinfo=UTC)),
            ("2026-13-01T00:00:00Z", None),
            (None, None),
            ("garbage", None),
            (123, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                got = parse_resets_at(value)
                self.assertEqual(got, expected)
                if got is not None:
                    self.assertEqual(got.tzinfo, UTC)


class ParseWindowTest(unittest.TestCase):
    def test_utilization_values(self):
        cases = [
            (None, None),
            (250, 100.0),
            (-3, 0.0),
            (True, None),
            ("50", None),
            (float("nan"), None),
            (10 ** 400, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                got = parse_window({"utilization": value, "resets_at": None})
                if expected is None:
                    self.assertIsNone(got)
                else:
                    self.assertEqual(got, UsageWindow(expected, None))

    def test_window_with_null_reset(self):
        self.assertEqual(parse_window({"utilization": 42.5, "resets_at": None}), UsageWindow(42.5, None))

    def test_non_dict_is_none(self):
        for value in (None, [], "x", 5):
            with self.subTest(value=value):
                self.assertIsNone(parse_window(value))

    def test_parse_windows_one_missing(self):
        got = parse_windows({"five_hour": {"utilization": 5, "resets_at": "2026-01-01T00:00:00Z"}, "seven_day": None})
        self.assertEqual(got, (UsageWindow(5.0, datetime(2026, 1, 1, tzinfo=UTC)), None))

    def test_parse_windows_both_missing(self):
        self.assertIsNone(parse_windows({"five_hour": None}))
        self.assertIsNone(parse_windows([]))

    def test_unknown_keys_ignored(self):
        got = parse_windows(
            {
                "five_hour": {"utilization": 1, "resets_at": None, "used_dollars": 9},
                "seven_day": {"utilization": 2},
                "seven_day_opus": {"utilization": 99},
                "limits": [],
            }
        )
        self.assertEqual(got, (UsageWindow(1.0, None), UsageWindow(2.0, None)))


class CacheTest(unittest.TestCase):
    def test_cache_hit(self):
        usage, reason = read_cached_usage(PROFILE, read_text=FakeFiles({CLAUDE_JSON_PATH: claude_json()}))
        self.assertIsNone(reason)
        self.assertEqual(usage.account_id, ACC1)
        self.assertEqual(usage.source, "cache")
        self.assertEqual(usage.fetched_at, datetime.fromtimestamp(1700000000, UTC))
        self.assertEqual(usage.five_hour, UsageWindow(88.0, datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)))
        self.assertEqual(usage.seven_day, UsageWindow(10.0, None))
        self.assertIsNone(usage.error)

    def test_cache_reasons(self):
        def with_cache(**changes):
            cache = default_cache()
            cache.update(changes)
            return claude_json(cache)

        no_key = json.dumps({"oauthAccount": {}})
        cases = [
            ("no file", None, "missing"),
            ("no key", no_key, "missing"),
            ("truncated", "{", "bad_json"),
            ("deeply nested", "[" * 100000, "bad_json"),
            ("top-level list", "[]", "bad_shape"),
            ("cache not object", json.dumps({"cachedUsageUtilization": 5}), "bad_shape"),
            ("fetchedAtMs str", with_cache(fetchedAtMs="x"), "bad_shape"),
            ("fetchedAtMs bool", with_cache(fetchedAtMs=True), "bad_shape"),
            ("fetchedAtMs 1e400", claude_json().replace("1700000000000", "1e400"), "bad_shape"),
            ("utilization empty", with_cache(utilization={}), "bad_shape"),
        ]
        for label, text, expected in cases:
            with self.subTest(label):
                files = FakeFiles({} if text is None else {CLAUDE_JSON_PATH: text})
                self.assertEqual(read_cached_usage(PROFILE, read_text=files), (None, expected))

    def test_cache_default_read_text(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as f:
                f.write(claude_json())
            profile = Profile(account_id=ACC1, home=home, keychain_service=keychain_service_for(home))
            usage, reason = read_cached_usage(profile)
        self.assertIsNone(reason)
        self.assertEqual(usage.source, "cache")
        self.assertEqual(usage.five_hour.utilization, 88.0)

    def test_cache_non_utf8_file_is_missing(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, ".claude.json"), "wb") as f:
                f.write(b"\xff\x7b")
            profile = Profile(account_id=ACC1, home=home, keychain_service=keychain_service_for(home))
            self.assertEqual(read_cached_usage(profile), (None, "missing"))


TOKEN = "tok-SECRET-123"
NOW_MS = int(NOW.timestamp() * 1000)
VALID_EXP = NOW_MS + 3600000
LIVE_BODY = json.dumps(
    {
        "five_hour": {"utilization": 42.5, "resets_at": "2026-01-01T00:00:00+00:00"},
        "seven_day": {"utilization": 7, "resets_at": None},
        "seven_day_opus": None,
        "limits": [],
    }
)


class FakeHttp:
    def __init__(self, status=200, body="", exc=None):
        self.status = status
        self.body = body
        self.exc = exc
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


class FakeKeychain:
    def __init__(self, value):
        self.value = value

    def __call__(self, service):
        return self.value


def cred_json(exp=VALID_EXP):
    return json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": exp}})


def run(**kw):
    return fetch_usage(
        PROFILE,
        http=kw.get("http"),
        keychain=kw.get("keychain", FakeKeychain(cred_json())),
        read_text=kw.get("read_text", FakeFiles({})),
        now=lambda: NOW,
    )


def http_error(code):
    return urllib.error.HTTPError(USAGE_URL, code, "x", {}, io.BytesIO(b""))


# (label, exception the http seam raises, expected live error kind)
EXCEPTION_KINDS = [
    ("URLError", urllib.error.URLError("x"), "network"),
    ("socket.timeout", socket.timeout(), "timeout"),
    ("URLError(timeout)", urllib.error.URLError(socket.timeout()), "timeout"),
    ("IncompleteRead", http.client.IncompleteRead(b"part"), "network"),
    ("BadStatusLine", http.client.BadStatusLine("x"), "network"),
    ("ConnectionResetError", ConnectionResetError(), "network"),
    ("TimeoutError", TimeoutError(), "network"),
    ("HTTPError 401", http_error(401), "http_401"),
    ("header ValueError", ValueError("Invalid header value b'Bearer " + TOKEN + "'"), "request_error"),
    ("UnicodeEncodeError", UnicodeEncodeError("latin-1", "x", 0, 1, "bad"), "request_error"),
]

STATUS_CODES = [401, 403, 429, 503, 302]


class FetchUsageTest(unittest.TestCase):
    def test_live_200(self):
        h = FakeHttp(200, LIVE_BODY)
        result = run(http=h)
        self.assertEqual(
            h.calls,
            [
                (
                    USAGE_URL,
                    {"Authorization": "Bearer " + TOKEN, "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"},
                    10.0,
                )
            ],
        )
        self.assertEqual(result.account_id, ACC1)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.fetched_at, NOW)
        self.assertEqual(result.five_hour, UsageWindow(42.5, datetime(2026, 1, 1, tzinfo=UTC)))
        self.assertEqual(result.seven_day, UsageWindow(7.0, None))
        self.assertIsNone(result.error)

    def test_cache_hit_429(self):
        result = run(http=FakeHttp(429, "rate limited"), read_text=FakeFiles({CLAUDE_JSON_PATH: claude_json()}))
        self.assertEqual(result.account_id, ACC1)
        self.assertEqual(result.source, "cache")
        self.assertEqual(result.fetched_at, datetime.fromtimestamp(1700000000, UTC))
        self.assertEqual(result.five_hour.utilization, 88.0)
        self.assertEqual(result.seven_day, UsageWindow(10.0, None))
        self.assertEqual(result.error, "http_429")

    def test_http_status_kinds(self):
        for code in STATUS_CODES:
            with self.subTest(code=code):
                result = run(http=FakeHttp(code, ""))
                self.assertEqual(result.source, "unavailable")
                self.assertEqual(result.error, "http_%d;cache_missing" % code)

    def test_http_status_out_of_range(self):
        for code in (0, 1000):
            with self.subTest(code=code):
                result = run(http=FakeHttp(code, ""))
                self.assertEqual(result.error, "network;cache_missing")

    def test_http_called_once_on_failure(self):
        h = FakeHttp(503, "")
        run(http=h)
        self.assertEqual(len(h.calls), 1)

    def test_expired_token_skips_http(self):
        h = FakeHttp(200, LIVE_BODY)
        result = run(http=h, keychain=FakeKeychain(cred_json(exp=NOW_MS)))
        self.assertEqual(h.calls, [])
        self.assertTrue(result.error.startswith("token_expired"), result.error)
        self.assertEqual(result.error, "token_expired;cache_missing")

    def test_expired_token_uses_cache(self):
        h = FakeHttp(200, LIVE_BODY)
        result = run(
            http=h,
            keychain=FakeKeychain(cred_json(exp=NOW_MS - 1)),
            read_text=FakeFiles({CLAUDE_JSON_PATH: claude_json()}),
        )
        self.assertEqual(h.calls, [])
        self.assertEqual(result.source, "cache")
        self.assertEqual(result.error, "token_expired")

    def test_token_valid_one_ms_before_expiry(self):
        h = FakeHttp(200, LIVE_BODY)
        result = run(http=h, keychain=FakeKeychain(cred_json(exp=NOW_MS + 1)))
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(result.source, "live")

    def test_unavailable_never_raises(self):
        result = run(http=FakeHttp(200, LIVE_BODY), keychain=FakeKeychain(None))
        self.assertEqual(result.account_id, ACC1)
        self.assertEqual(result.source, "unavailable")
        self.assertEqual(result.error, "token_missing;cache_missing")
        self.assertIsNone(result.five_hour)
        self.assertIsNone(result.seven_day)
        self.assertEqual(result.fetched_at, NOW)

    def test_live_bad_json_kind(self):
        result = run(http=FakeHttp(200, "{"))
        self.assertEqual(result.error, "bad_json;cache_missing")

    def test_live_bad_shape_kind(self):
        result = run(http=FakeHttp(200, '{"five_hour": null}'))
        self.assertEqual(result.error, "bad_shape;cache_missing")

    def test_live_deeply_nested_json(self):
        result = run(http=FakeHttp(200, "[" * 100000))
        self.assertEqual(result.error, "bad_json;cache_missing")

    def test_fetch_usage_rejects_non_profile(self):
        with self.assertRaises(TypeError):
            fetch_usage("x")


class ErrorKindTest(unittest.TestCase):
    def test_exception_kinds(self):
        for label, exc, kind in EXCEPTION_KINDS:
            with self.subTest(label):
                result = run(http=FakeHttp(exc=exc))
                self.assertEqual(result.source, "unavailable")
                self.assertEqual(result.error, kind + ";cache_missing")


class DefaultHttpSeamTest(unittest.TestCase):
    def test_no_redirect_handler_installed(self):
        redirect_handlers = [
            h for h in _NO_REDIRECT_OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
        ]
        self.assertGreaterEqual(len(redirect_handlers), 1)
        for h in redirect_handlers:
            self.assertIsInstance(h, _NoRedirect)

    def test_no_redirect_returns_none(self):
        got = _NoRedirect().redirect_request(urllib.request.Request("https://a/"), None, 302, "Found", {}, "https://b/")
        self.assertIsNone(got)

    def test_default_http_seam_maps_http_error(self):
        with unittest.mock.patch.object(rotator.usage._NO_REDIRECT_OPENER, "open", side_effect=http_error(302)):
            self.assertEqual(_urllib_http(USAGE_URL, {}, 1.0), (302, ""))

    def test_default_http_seam_reads_body(self):
        resp = unittest.mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        resp.read.return_value = LIVE_BODY.encode()
        with unittest.mock.patch.object(rotator.usage._NO_REDIRECT_OPENER, "open", return_value=resp) as opener:
            self.assertEqual(_urllib_http(USAGE_URL, {"Accept": "application/json"}, 1.0), (200, LIVE_BODY))
        resp.read.assert_called_once_with(1048576)
        req = opener.call_args[0][0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, USAGE_URL)
        self.assertEqual(opener.call_args[1], {"timeout": 1.0})

    def test_default_http_seam_propagates_incomplete_read(self):
        with unittest.mock.patch.object(
            rotator.usage._NO_REDIRECT_OPENER, "open", side_effect=http.client.IncompleteRead(b"")
        ):
            with self.assertRaises(http.client.IncompleteRead):
                _urllib_http(USAGE_URL, {}, 1.0)
            result = fetch_usage(
                PROFILE,
                keychain=FakeKeychain(cred_json()),
                read_text=FakeFiles({}),
                now=lambda: NOW,
            )
        self.assertEqual(result.error, "network;cache_missing")


class SecretAbsenceTest(unittest.TestCase):
    def scenarios(self):
        yield "live 200", lambda: run(http=FakeHttp(200, LIVE_BODY))
        for code in STATUS_CODES:
            yield "status %d" % code, (lambda code=code: run(http=FakeHttp(code, "")))
        for label, exc, _ in EXCEPTION_KINDS:
            yield label, (lambda exc=exc: run(http=FakeHttp(exc=exc)))
        yield "bad_json", lambda: run(http=FakeHttp(200, "{"))
        yield "bad_shape", lambda: run(http=FakeHttp(200, '{"five_hour": null}'))
        yield "expired", lambda: run(http=FakeHttp(200, LIVE_BODY), keychain=FakeKeychain(cred_json(exp=NOW_MS)))
        yield "missing", lambda: run(http=FakeHttp(200, LIVE_BODY), keychain=FakeKeychain(None))
        yield "cache hit", lambda: run(
            http=FakeHttp(503, ""), read_text=FakeFiles({CLAUDE_JSON_PATH: claude_json()})
        )
        yield "unavailable", lambda: run(http=FakeHttp(503, ""))

    def test_token_absent_from_every_channel(self):
        for label, call in self.scenarios():
            with self.subTest(label):
                with self.assertLogs("rotator", level="DEBUG") as cap:
                    result = call()
                self.assertNotIn(TOKEN, repr(result))
                self.assertNotIn(TOKEN, str(result))
                self.assertNotIn(TOKEN, result.error or "")
                for record in cap.records:
                    self.assertNotIn(TOKEN, record.getMessage())
                    self.assertIsNone(record.exc_info)
                self.assertIn(ACC1, repr(result))
                self.assertTrue(any(ACC1 in r.getMessage() for r in cap.records))


if __name__ == "__main__":
    unittest.main()
