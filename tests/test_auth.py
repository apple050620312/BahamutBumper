import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
import requests
from .support import BumperTestCase
from .support import PAGE
from bahamut_bumper.errors import AuthenticationError
from bahamut_bumper.errors import ConfigurationError
from bahamut_bumper.errors import MobileLoginUnavailable
from bahamut_bumper.errors import SafetyError
from bahamut_bumper.auth import authenticated_forum_snapshot
from bahamut_bumper.auth import build_session
from bahamut_bumper.auth import load_cookie_records
from bahamut_bumper.auth import mobile_login
from bahamut_bumper.auth import persist_session_cookies
from bahamut_bumper.forum import parse_thread_snapshot
from bahamut_bumper.workflow import bump_account
from bahamut_bumper.workflow import check_account


class AuthTests(BumperTestCase):
    def mobile_response(self, status, body, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = body.encode("utf-8")
        response.encoding = "utf-8"
        response.headers.update(headers or {})
        mobile = Mock(headers={}, cookies=requests.cookies.RequestsCookieJar())
        mobile.post.return_value = response
        return mobile

    def test_mobile_login_requires_local_password_file_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                mobile_login(self.config(Path(directory)))

    def test_mobile_http_failures_preserve_status_without_exposing_response_secrets(self):
        for status, retryable, mitigation in (
            (403, False, ""),
            (403, False, "challenge"),
            (429, False, ""),
            (503, True, ""),
        ):
            with (
                self.subTest(status=status, mitigation=mitigation),
                tempfile.TemporaryDirectory() as directory,
            ):
                config = self.config(Path(directory))
                config.cookie_file.write_text("Cookie: saved=original", encoding="utf-8")
                mobile = self.mobile_response(
                    status,
                    "<html>captcha test-password secret-cookie</html>",
                    {"cf-mitigated": mitigation, "cf-ray": "123abc-TPE"},
                )
                with (
                    patch("bahamut_bumper.auth.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    patch("bahamut_bumper.auth.new_forum_session") as forum,
                    self.assertRaises(MobileLoginUnavailable) as caught,
                ):
                    mobile_login(config)

                error = str(caught.exception)
                self.assertIn(f"HTTP {status}", error)
                self.assertIn("123abc-TPE", error)
                self.assertNotIn("test-password", error)
                self.assertNotIn("secret-cookie", error)
                self.assertEqual(caught.exception.retryable, retryable)
                if status == 403:
                    self.assertIn("人機驗證" if mitigation else "主機出口 IP", error)
                self.assertEqual(config.cookie_file.read_text(), "Cookie: saved=original")
                forum.assert_not_called()

    def test_mobile_connection_failures_allow_cached_cookie_fallback_and_retry(self):
        for error in (requests.Timeout(), requests.ConnectionError()):
            with self.subTest(error=type(error).__name__):
                mobile = self.mobile_response(200, "{}")
                mobile.post.side_effect = error
                with (
                    patch("bahamut_bumper.auth.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    self.assertRaises(MobileLoginUnavailable) as caught,
                ):
                    mobile_login(self.config())
                self.assertTrue(caught.exception.retryable)
                self.assertIn(type(error).__name__, str(caught.exception))

    def test_rejected_credentials_and_explicit_challenges_do_not_use_cached_cookies(self):
        for body in (
            '{"message":"wrong password"}',
            '{"userid":"another-account"}',
            "<html>recaptcha</html>",
        ):
            with self.subTest(body=body):
                mobile = self.mobile_response(200, body)
                mobile.cookies.set("BAHARUNE", "test-rune", domain=".gamer.com.tw")
                with (
                    patch("bahamut_bumper.auth.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    patch("bahamut_bumper.auth.build_session") as cached,
                    self.assertRaises(AuthenticationError) as caught,
                ):
                    authenticated_forum_snapshot(self.config(), self.url)
                self.assertNotIsInstance(caught.exception, MobileLoginUnavailable)
                self.assertFalse(caught.exception.retryable)
                cached.assert_not_called()

    def test_mobile_login_still_saves_cookies_only_after_forum_owner_proof(self):
        for owner in (True, False):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                config = self.config(Path(directory))
                config.cookie_file.write_text("Cookie: saved=original", encoding="utf-8")
                forum = requests.Session()
                mobile = self.mobile_response(200, '{"userid":"sangege01"}')
                mobile.cookies.set("BAHARUNE", "test-rune", domain=".gamer.com.tw")
                snapshot = replace(
                    parse_thread_snapshot(
                        PAGE, self.url, config.account, datetime.now(self.tz)
                    ),
                    owner_verified=owner,
                )
                with (
                    patch("bahamut_bumper.auth.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    patch("bahamut_bumper.auth.new_forum_session", return_value=forum),
                    patch("bahamut_bumper.auth.fetch_snapshot", return_value=snapshot),
                ):
                    if owner:
                        result = mobile_login(config)
                        self.assertTrue(result["forum_owner_verified"])
                        self.assertTrue(result["cookie_saved"])
                    else:
                        with self.assertRaises(AuthenticationError):
                            mobile_login(config)
                if owner:
                    self.assertIn(
                        "BAHARUNE",
                        {c["name"] for c in load_cookie_records(config.cookie_file)},
                    )
                else:
                    self.assertEqual(config.cookie_file.read_text(), "Cookie: saved=original")
                    self.assertFalse(config.cookie_file.with_suffix(".json.backup").exists())

    def test_run_check_uses_verified_cached_cookies_on_api_outages(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config.cookie_file.write_text("Cookie: saved=original", encoding="utf-8")
            snapshot = parse_thread_snapshot(
                PAGE, self.url, config.account, datetime.now(self.tz)
            )
            for error in (
                MobileLoginUnavailable("HTTP 403", retryable=False),
                MobileLoginUnavailable("HTTP 503"),
                MobileLoginUnavailable("Timeout"),
            ):
                with (
                    self.subTest(error=str(error)),
                    patch("bahamut_bumper.auth._password_login", side_effect=error),
                    patch("bahamut_bumper.auth.fetch_snapshot", return_value=snapshot),
                    patch("bahamut_bumper.workflow.notify_cookie_expiry"),
                    self.assertLogs("bahamut-bumper", level="WARNING") as logs,
                ):
                    result = check_account(config)
                self.assertTrue(result["ok"])
                self.assertTrue(result["owner_verified"])
                self.assertIn(str(error), " ".join(logs.output))
                self.assertEqual(config.cookie_file.read_text(), "Cookie: saved=original")

    def test_workflow_reuses_the_verified_password_login_session(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config.password_file.write_text("test-password", encoding="utf-8")
            mobile = self.mobile_response(200, '{"userid":"sangege01"}')
            mobile.cookies.set("BAHARUNE", "test-rune", domain=".gamer.com.tw")
            forum = requests.Session()
            page = parse_thread_snapshot(PAGE, self.url, config.account, datetime.now(self.tz))
            with (
                patch("requests.Session", return_value=mobile),
                patch("bahamut_bumper.auth.new_forum_session", return_value=forum) as new_session,
                patch("bahamut_bumper.auth.fetch_snapshot", return_value=page) as read,
            ):
                session, snapshot = authenticated_forum_snapshot(config, self.url)

            self.assertIs(session, forum)
            self.assertIs(snapshot, page)
            new_session.assert_called_once_with()
            read.assert_called_once_with(forum, self.url, config)
            self.assertTrue(config.cookie_file.is_file())

    def test_fallback_saves_cookie_rotations_after_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config.cookie_file.write_text("Cookie: saved=original", encoding="utf-8")
            snapshot = parse_thread_snapshot(
                PAGE, self.url, config.account, datetime.now(self.tz)
            )

            def rotated_snapshot(session, *_args):
                session.cookies.set("rotated", "new", domain=".gamer.com.tw")
                session._bahamut_cookie_dirty = True
                return snapshot

            with (
                patch(
                    "bahamut_bumper.auth._password_login",
                    side_effect=MobileLoginUnavailable("HTTP 403", retryable=False),
                ),
                patch("bahamut_bumper.auth.fetch_snapshot", side_effect=rotated_snapshot),
                self.assertLogs("bahamut-bumper", level="WARNING"),
            ):
                authenticated_forum_snapshot(config, self.url)
            self.assertIn(
                "rotated", {c["name"] for c in load_cookie_records(config.cookie_file)}
            )
            self.assertEqual(
                config.cookie_file.with_suffix(".json.backup").read_text(),
                "Cookie: saved=original",
            )

    def test_failed_cookie_fallback_preserves_403_and_prevents_forum_writes(self):
        for state in ("missing", "logged-out", "wrong-account", "challenge"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                config = self.config(Path(directory))
                if state != "missing":
                    config.cookie_file.write_text("Cookie: saved=original", encoding="utf-8")
                snapshot = replace(
                    parse_thread_snapshot(
                        PAGE, self.url, config.account, datetime.now(self.tz)
                    ),
                    owner_verified=False,
                    site_reports_login=state == "wrong-account",
                )

                def rejected_snapshot(session, *_args):
                    session.cookies.set("rotated", "unverified", domain=".gamer.com.tw")
                    session._bahamut_cookie_dirty = True
                    if state == "challenge":
                        raise SafetyError("CAPTCHA")
                    return snapshot

                with (
                    patch(
                        "bahamut_bumper.auth._password_login",
                        side_effect=MobileLoginUnavailable("HTTP 403", retryable=False),
                    ),
                    patch("bahamut_bumper.auth.fetch_snapshot", side_effect=rejected_snapshot),
                    patch("bahamut_bumper.workflow.post_reply") as post,
                    patch("bahamut_bumper.workflow.delete_post") as delete,
                    self.assertLogs("bahamut-bumper", level="WARNING"),
                    self.assertRaises(MobileLoginUnavailable) as caught,
                ):
                    bump_account(config)
                self.assertIn("HTTP 403", str(caught.exception))
                self.assertFalse(caught.exception.retryable)
                post.assert_not_called()
                delete.assert_not_called()
                if state == "missing":
                    self.assertFalse(config.cookie_file.exists())
                else:
                    self.assertEqual(config.cookie_file.read_text(), "Cookie: saved=original")
                self.assertFalse(config.cookie_file.with_suffix(".json.backup").exists())

    def test_disabling_auto_mobile_login_uses_only_verified_forum_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config(Path(directory)), auto_mobile_login=False)
            config.cookie_file.write_text("Cookie: saved=original", encoding="utf-8")
            snapshot = parse_thread_snapshot(
                PAGE, self.url, config.account, datetime.now(self.tz)
            )
            with (
                patch("bahamut_bumper.auth._password_login") as mobile,
                patch("bahamut_bumper.auth.fetch_snapshot", return_value=snapshot),
                patch("bahamut_bumper.workflow.notify_cookie_expiry"),
            ):
                self.assertTrue(check_account(config)["ok"])
            mobile.assert_not_called()

    def test_raw_cookie_header_is_loaded_into_cookie_jar(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            path.write_text("Cookie: foo=bar; abc=123", encoding="utf-8")
            config = self.config(Path(directory))
            session = build_session(config)
            self.assertEqual(session.cookies.get("foo"), "bar")
            session.cookies.set("ckFORUM_pdel", "x", domain=".gamer.com.tw", path="/")
            self.assertEqual(session.cookies.get("ckFORUM_pdel"), "x")
            session.cookies.set("rotated", "new", domain=".gamer.com.tw", path="/")
            session._bahamut_cookie_dirty = True
            persist_session_cookies(session, config)
            persisted = load_cookie_records(path)
            persisted_names = {item["name"] for item in persisted}
            self.assertIn("rotated", persisted_names)
            self.assertNotIn("ckFORUM_pdel", persisted_names)
            self.assertTrue(path.with_suffix(".json.backup").is_file())
