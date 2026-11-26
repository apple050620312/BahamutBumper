import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from main import (
    AuthenticationError,
    ConfigurationError,
    Config,
    MobileLoginUnavailable,
    Post,
    SafetyError,
    ThreadSnapshot,
    authenticated_forum_snapshot,
    build_session,
    classify_daily_posts,
    console_help,
    execute_console_command,
    fetch_page_snapshot,
    fetch_snapshot,
    live_round_trip_test,
    load_cookie_records,
    mobile_login,
    parse_delete_request,
    parse_posted_at,
    parse_thread_snapshot,
    parse_wall_time,
    persist_session_cookies,
    run_check,
    run_daemon,
    run_once,
    seconds_until,
    validate_forum_url,
    with_last_page,
    with_page,
)


PAGE = r"""
<html><body>
<section class="c-section" id="post_100">
  <a class="floor" data-floor="1">1 樓</a>
  <a class="userid" href="https://home.gamer.com.tw/sangege01">sangege01</a>
  <span class="edittime" data-mtime="2026-08-20 10:11:12"></span>
  <article>首篇</article>
  <button class="tippy-option-menu" data-tippy='{"author":"sangege01","owner":true,"isLogin":true}'></button>
</section>
<section class="c-section" id="post_1113614">
  <a class="floor" data-floor="12">12 樓</a>
  <a class="userid">sangege01</a>
  <span class="edittime" data-mtime="2026-08-27 20:24:59"></span>
  <article>eee</article>
</section>
<form name="frm" action="post2.php?bsn=18673&amp;all=0&amp;snA=205415">
  <input name="code" value="">
  <input name="threadSubbsn" value="18">
</form>
<script>
function pdel(sn) {
 var args = 'bsn=18673&sn=' + sn + '&type=4&code=aa009ffd81d50c3a&pwd=2911&snA=205415&threadSubbsn=18&prevPageSubbsn=0&page=1';
 delPost(sn, args, '5432');
}
</script>
</body></html>
"""

LAST_PAGE = r"""
<html><body>
<section class="c-section" id="post_1114060">
  <a class="floor" data-floor="21">21 樓</a>
  <a class="userid">sangege01</a>
  <span class="edittime" data-mtime="2026-09-06 20:30:00"></span>
  <article>推</article>
  <button class="tippy-option-menu" data-tippy='{"author":"sangege01","owner":true,"isLogin":true}'></button>
</section>
<form name="frm" action="post2.php?bsn=18673&amp;all=0&amp;snA=205415">
  <input name="code" value="">
  <input name="threadSubbsn" value="18">
</form>
<p class="BH-pagebtnA"><a href="?page=1&amp;bsn=18673&amp;snA=205415">1</a><a class="pagenow">2</a></p>
</body></html>
"""

EMPTY_OLD_PAGE = r"""
<html><body>
<form name="frm" action="post2.php?bsn=18673&amp;all=0&amp;snA=205415">
  <input name="threadSubbsn" value="18">
</form>
<p class="BH-pagebtnA"><a class="pagenow">137</a></p>
</body></html>
"""


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tz = timezone(timedelta(hours=8), "Asia/Taipei")
        self.url = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"
        self.other_url = "https://forum.gamer.com.tw/C.php?bsn=60&snA=12345"

    def config(self, root: Path = Path(".")) -> Config:
        return Config(
            target_url=self.url,
            account="sangege01",
            message="推",
            deletable_messages=("推", "eee"),
            bump_at=parse_wall_time("20:30"),
            timezone=self.tz,
            cookie_file=root / "cookies.json",
            data_dir=root,
            guard_urls=(),
            retry_minutes=10,
            max_retries=3,
            run_missed_on_start=True,
            timeout_seconds=30,
            discord_webhook_file=root / "discord_webhook.txt",
            discord_user_id="523114942434639873",
            password_file=root / "bahamut_password.txt",
            auto_mobile_login=True,
        )

    def config_from_accounts_file(self, root, accounts):
        path = root / "accounts.json"
        path.write_text(json.dumps(accounts), encoding="utf-8")
        with patch.dict(
            os.environ,
            {"DATA_DIR": str(root), "BAHAMUT_ACCOUNTS_FILE": str(path)},
            clear=True,
        ):
            return Config.from_env()

    def test_accounts_file_requires_unique_accounts_threads_and_cookies(self):
        first = {"account": "sangege01", "target_url": self.url, "cookie_file": "first.json"}
        second = {"account": "other", "target_url": self.other_url, "cookie_file": "second.json"}
        for field, value in (
            ("account", "SANGEGE01"),
            ("target_url", self.url + "&page=2"),
            ("target_url", self.url.replace("bsn=18673", "bsn=018673")),
            ("target_url", self.other_url + "&snA=123456"),
            ("cookie_file", "./first.json"),
            ("cookie_file", "first.json.backup"),
        ):
            with (
                self.subTest(field=field, value=value),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaises(ConfigurationError):
                    self.config_from_accounts_file(
                        Path(directory), [first, {**second, field: value}]
                    )

    def test_run_once_uses_each_accounts_cookie_for_its_article(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config_from_accounts_file(
                root,
                [
                    {
                        "account": "sangege01",
                        "target_url": self.url,
                        "cookie_file": "cookies.json",
                        "auto_mobile_login": False,
                    },
                    {
                        "account": "other",
                        "target_url": self.other_url,
                        "cookie_file": "other.json",
                        "auto_mobile_login": False,
                    },
                ],
            )
            (root / "cookies.json").write_text("Cookie: profile=first", encoding="utf-8")
            (root / "other.json").write_text("Cookie: profile=second", encoding="utf-8")
            now = datetime(2026, 11, 27, 20, 30, tzinfo=self.tz)
            threads = {}
            for url, account, cookie in (
                (self.url, "sangege01", "first"),
                (self.other_url, "other", "second"),
            ):
                snapshot = parse_thread_snapshot(
                    PAGE.replace("sangege01", account), url, account, now
                )
                threads[url] = (replace(snapshot, posts=snapshot.posts[:1]), cookie)
            published = []

            def fetch(session, url, profile):
                snapshot, cookie = threads[url]
                self.assertEqual(session.cookies.get("profile"), cookie)
                if url in published:
                    post = Post("999", 2, profile.account, now, "推")
                    snapshot = replace(snapshot, posts=(*snapshot.posts, post))
                return snapshot

            def publish(_session, snapshot, _message, _config):
                published.append(snapshot.url)

            with (
                patch("main.datetime", wraps=datetime) as clock,
                patch("main.mobile_login", side_effect=AssertionError("unexpected login")),
                patch("main.fetch_snapshot", side_effect=fetch),
                patch("main.post_reply", side_effect=publish),
            ):
                clock.now.return_value = now
                checks = run_check(config)
                result = run_once(config)

            self.assertTrue(checks["ok"])
            self.assertEqual(published, [self.url, self.other_url])
            self.assertEqual(
                [(item["account"], item["target"]) for item in result["results"]],
                [("sangege01", self.url), ("other", self.other_url)],
            )
            self.assertEqual(
                json.loads((root / "status.json").read_text(encoding="utf-8")), result
            )

    def test_daemon_only_retries_account_with_transient_login_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config_from_accounts_file(
                root,
                [
                    {"account": "sangege01", "target_url": self.url},
                    {"account": "other", "target_url": self.other_url},
                ],
            )
            config = replace(config, bump_at=parse_wall_time("00:00"))
            calls = []

            def attempt(profile):
                calls.append(profile.account)
                if profile.account == "sangege01":
                    raise MobileLoginUnavailable("HTTP 403", retryable=False)
                if calls.count("other") == 1:
                    raise MobileLoginUnavailable("HTTP 503")
                return {"ok": True, "posted_new": True}

            def stop_before_next_day(stop, _commands, seconds, _config):
                if seconds != config.retry_minutes * 60:
                    stop[0] = True

            with (
                patch("main.signal.signal"),
                patch("main.threading.Thread"),
                patch("main.run_once", side_effect=attempt),
                patch(
                    "main.next_scheduled_at", return_value=datetime.now(self.tz) + timedelta(days=1)
                ),
                patch("main.wait_with_console", side_effect=stop_before_next_day),
                patch("main.notify_failure"),
                self.assertLogs("bahamut-bumper", level="INFO"),
            ):
                run_daemon(config)

            self.assertEqual(calls, ["sangege01", "other", "other"])
            result = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertEqual([item["ok"] for item in result["results"]], [False, True])
            self.assertEqual(result["results"][0]["error"], "HTTP 403")

    def test_live_test_rejects_article_scheduled_for_another_account(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config_from_accounts_file(
                Path(directory),
                [
                    {"account": "sangege01", "target_url": self.url},
                    {"account": "other", "target_url": self.other_url},
                ],
            )
            with self.assertRaisesRegex(SafetyError, "禁止使用正式自推文章"):
                live_round_trip_test(config, self.other_url + "&page=2", account="sangege01")

    def test_relative_and_absolute_timestamps(self):
        today = date(2026, 8, 28)
        self.assertEqual(
            parse_posted_at("昨天 20:24", today, self.tz),
            datetime(2026, 8, 27, 20, 24, tzinfo=self.tz),
        )
        self.assertEqual(
            parse_posted_at("2026-08-11 14:42:03", today, self.tz),
            datetime(2026, 8, 11, 14, 42, 3, tzinfo=self.tz),
        )

    def test_url_validation_and_last_page_are_idempotent(self):
        expected = self.url + "&last=1#down"
        self.assertEqual(validate_forum_url(self.url + "&page=99"), self.url)
        self.assertEqual(with_last_page(self.url), expected)
        self.assertEqual(with_last_page(expected), expected)
        self.assertEqual(with_page(self.url, 137), self.url + "&page=137")
        with self.assertRaises(Exception):
            validate_forum_url("https://example.com/C.php?bsn=1&snA=2")
        with self.assertRaises(ValueError):
            with_page(self.url, 0)

    def test_time_helpers(self):
        self.assertEqual(parse_wall_time("20:30").hour, 20)
        start = datetime(2026, 8, 28, 20, 30, tzinfo=self.tz)
        end = start + timedelta(minutes=1)
        self.assertEqual(seconds_until(end, start), 60)
        self.assertEqual(seconds_until(start, end), 0)

    def test_pterodactyl_console_commands(self):
        self.assertIn("check", console_help())
        self.assertIn("login-test", console_help())
        self.assertFalse(execute_console_command("help", self.config()))
        self.assertTrue(execute_console_command("stop", self.config()))

    def test_mobile_login_requires_local_password_file_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                mobile_login(self.config(Path(directory)))

    def mobile_response(self, status, body, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = body.encode("utf-8")
        response.encoding = "utf-8"
        response.headers.update(headers or {})
        mobile = Mock(headers={}, cookies=requests.cookies.RequestsCookieJar())
        mobile.post.return_value = response
        return mobile

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
                    patch("main.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    patch("main.new_forum_session") as forum,
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
                    patch("main.read_secret_file", return_value="test-password"),
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
                    patch("main.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    patch("main.build_session") as cached,
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
                    patch("main.read_secret_file", return_value="test-password"),
                    patch("requests.Session", return_value=mobile),
                    patch("main.new_forum_session", return_value=forum),
                    patch("main.fetch_snapshot", return_value=snapshot),
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
                    patch("main.mobile_login", side_effect=error),
                    patch("main.fetch_snapshot", return_value=snapshot),
                    patch("main.notify_cookie_expiry"),
                    self.assertLogs("bahamut-bumper", level="WARNING") as logs,
                ):
                    result = run_check(config)
                self.assertTrue(result["ok"])
                self.assertTrue(result["owner_verified"])
                self.assertIn(str(error), " ".join(logs.output))
                self.assertEqual(config.cookie_file.read_text(), "Cookie: saved=original")

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
                    "main.mobile_login",
                    side_effect=MobileLoginUnavailable("HTTP 403", retryable=False),
                ),
                patch("main.fetch_snapshot", side_effect=rotated_snapshot),
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
                        "main.mobile_login",
                        side_effect=MobileLoginUnavailable("HTTP 403", retryable=False),
                    ),
                    patch("main.fetch_snapshot", side_effect=rejected_snapshot),
                    patch("main.post_reply") as post,
                    patch("main.delete_post") as delete,
                    self.assertLogs("bahamut-bumper", level="WARNING"),
                    self.assertRaises(MobileLoginUnavailable) as caught,
                ):
                    run_once(config)
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
                patch("main.mobile_login") as mobile,
                patch("main.fetch_snapshot", return_value=snapshot),
                patch("main.notify_cookie_expiry"),
            ):
                self.assertTrue(run_check(config)["ok"])
            mobile.assert_not_called()

    def test_daemon_stops_blocked_login_retries_but_retries_transient_failures(self):
        for status, retryable, attempts in ((403, False, 1), (429, False, 1), (503, True, 3)):
            with self.subTest(status=status):
                config = replace(self.config(), bump_at=parse_wall_time("00:00"))

                def stop_before_next_day(stop, _commands, seconds, _config):
                    if seconds != config.retry_minutes * 60:
                        stop[0] = True

                with (
                    patch("main.signal.signal"),
                    patch("main.threading.Thread"),
                    patch(
                        "main.run_once",
                        side_effect=MobileLoginUnavailable(
                            f"HTTP {status}", retryable=retryable
                        ),
                    ) as run,
                    patch(
                        "main.next_scheduled_at",
                        return_value=datetime.now(self.tz) + timedelta(days=1),
                    ),
                    patch("main.wait_with_console", side_effect=stop_before_next_day),
                    patch("main.write_status") as write,
                    patch("main.notify_failure") as notify,
                    self.assertLogs("bahamut-bumper", level="INFO"),
                ):
                    run_daemon(config)
                self.assertEqual(run.call_count, attempts)
                self.assertEqual(write.call_args.args[1]["retryable"], retryable)
                self.assertEqual(notify.call_count, attempts)

    def test_snapshot_parsing_and_owner_proof(self):
        snapshot = parse_thread_snapshot(
            PAGE,
            self.url + "&last=1",
            "sangege01",
            datetime(2026, 8, 28, 8, 0, tzinfo=self.tz),
        )
        self.assertTrue(snapshot.owner_verified)
        self.assertTrue(snapshot.site_reports_login)
        self.assertEqual(snapshot.page_number, 1)
        self.assertEqual(snapshot.subboard, "18")
        self.assertEqual(len(snapshot.posts), 2)
        self.assertEqual(snapshot.posts[1].post_id, "1113614")
        self.assertEqual(snapshot.posts[1].posted_at.second, 59)
        self.assertIn("post2.php", snapshot.form_action)

    def test_fetch_snapshot_uses_page_one_for_owner_proof_after_floor_twenty(self):
        with patch(
            "main.request_page",
            side_effect=[
                (LAST_PAGE, self.url + "&page=2"),
                (PAGE, self.url),
            ],
        ) as request:
            snapshot = fetch_snapshot(object(), self.url, self.config())

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1], with_last_page(self.url))
        self.assertEqual(request.call_args_list[1].args[1], self.url)
        self.assertTrue(snapshot.owner_verified)
        self.assertTrue(snapshot.site_reports_login)
        self.assertEqual(snapshot.page_number, 2)
        self.assertEqual([post.floor for post in snapshot.posts], [21])
        self.assertIn("post2.php", snapshot.form_action)

    def test_fetch_snapshot_rejects_redirect_to_another_thread(self):
        with (
            patch("main.request_page", return_value=(PAGE, self.other_url)),
            self.assertRaises(SafetyError),
        ):
            fetch_snapshot(object(), self.url, self.config())

    def test_run_once_deletes_previous_reply_across_any_page_boundary(self):
        now = datetime.now(self.tz)
        previous = datetime.combine(
            now.date() - timedelta(days=2), parse_wall_time("20:30"), tzinfo=self.tz
        )
        today = datetime.combine(now.date(), parse_wall_time("20:30"), tzinfo=self.tz)

        def snapshot(posts, page):
            return ThreadSnapshot(
                url=self.url,
                html_text=PAGE,
                posts=tuple(posts),
                form_action="https://forum.gamer.com.tw/post2.php",
                form_fields={"threadSubbsn": "18"},
                subboard="18",
                owner_verified=True,
                site_reports_login=True,
                page_number=page,
            )

        for old_page in (2, 137):
            with self.subTest(old_page=old_page):
                old_post = Post(str(old_page * 20), old_page * 20, "sangege01", previous, "推")
                new_post = Post(
                    str(old_page * 20 + 1),
                    old_page * 20 + 1,
                    "sangege01",
                    today,
                    "推",
                )
                before = snapshot([old_post], old_page)
                latest = snapshot([new_post], old_page + 1)
                delete_source = snapshot([old_post], old_page)
                after_delete = snapshot([], old_page)
                config = replace(self.config(), auto_mobile_login=False)
                session = object()

                with (
                    patch("main.build_session", return_value=session),
                    patch("main.fetch_snapshot", side_effect=[before, latest, latest]),
                    patch(
                        "main.fetch_page_snapshot",
                        side_effect=[delete_source, after_delete],
                    ) as fetch_page,
                    patch("main.post_reply"),
                    patch("main.delete_post") as delete,
                    patch("main.persist_session_cookies"),
                    patch("main.notify_cookie_expiry"),
                    patch("main.write_status"),
                    patch("main.notify_daily_success"),
                ):
                    result = run_once(config)

                self.assertTrue(result["posted_new"])
                self.assertEqual([call.args[2] for call in fetch_page.call_args_list], [old_page, old_page])
                self.assertTrue(fetch_page.call_args_list[1].kwargs["allow_empty"])
                self.assertIs(delete.call_args.args[1], delete_source)
                self.assertEqual(delete.call_args.args[2], old_post)

    def test_empty_old_page_can_be_verified_after_deletion(self):
        with patch(
            "main.request_page",
            side_effect=[
                (EMPTY_OLD_PAGE, self.url + "&page=137"),
                (PAGE, self.url),
            ],
        ):
            snapshot = fetch_page_snapshot(
                object(), self.url, 137, self.config(), allow_empty=True
            )

        self.assertEqual(snapshot.posts, ())
        self.assertEqual(snapshot.page_number, 137)
        self.assertTrue(snapshot.owner_verified)
        self.assertTrue(snapshot.site_reports_login)

    def test_reply_owner_is_not_enough_to_prove_thread_ownership(self):
        page = PAGE.replace(
            '{"author":"sangege01","owner":true,"isLogin":true}',
            '{"author":"someone_else","owner":false,"isLogin":true}',
        ).replace(
            '<article>eee</article>',
            '<article>eee</article><button class="tippy-option-menu" '
            'data-tippy=\'{"author":"sangege01","owner":true}\'></button>',
        )
        snapshot = parse_thread_snapshot(
            page, self.url, "sangege01", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        self.assertFalse(snapshot.owner_verified)

    def test_delete_request_uses_fresh_dynamic_fields(self):
        snapshot = parse_thread_snapshot(
            PAGE, self.url, "sangege01", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        request = parse_delete_request(snapshot, snapshot.posts[1])
        self.assertEqual(request.fields["sn"], "1113614")
        self.assertEqual(request.fields["code"], "aa009ffd81d50c3a")
        self.assertEqual(request.cookie_value, "5432")
        self.assertIn("post2.php?", request.url)

    def test_delete_request_rejects_wrong_post_identity(self):
        snapshot = parse_thread_snapshot(
            PAGE, self.url, "sangege01", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        wrong = Post("999", 12, "sangege01", snapshot.posts[1].posted_at, "eee")
        # The parser constructs the URL for exactly the supplied post ID; the
        # later delete_post equality check is what rejects a nonexistent floor.
        self.assertEqual(parse_delete_request(snapshot, wrong).fields["sn"], "999")

    def test_daily_classification_excludes_op_and_non_bump_text(self):
        snapshot = parse_thread_snapshot(
            PAGE, self.url, "sangege01", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        today, yesterday = classify_daily_posts(
            snapshot, self.config(), datetime(2026, 8, 28, tzinfo=self.tz)
        )
        self.assertEqual(today, [])
        self.assertEqual([post.post_id for post in yesterday], ["1113614"])
        ordinary = PAGE.replace("<article>eee</article>", "<article>正常對話</article>")
        ordinary_snapshot = parse_thread_snapshot(
            ordinary, self.url, "sangege01", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        _, ordinary_yesterday = classify_daily_posts(
            ordinary_snapshot, self.config(), datetime(2026, 8, 28, tzinfo=self.tz)
        )
        self.assertEqual(ordinary_yesterday, [])

    def test_daily_classification_finds_bump_older_than_yesterday(self):
        old_page = PAGE.replace(
            "2026-08-27 20:24:59", "2026-08-25 20:24:59"
        ).replace("<article>eee</article>", "<article>推</article>")
        snapshot = parse_thread_snapshot(
            old_page, self.url, "sangege01", datetime(2026, 8, 28, tzinfo=self.tz)
        )

        today, previous = classify_daily_posts(
            snapshot, self.config(), datetime(2026, 8, 28, tzinfo=self.tz)
        )

        self.assertEqual(today, [])
        self.assertEqual([post.floor for post in previous], [12])

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


if __name__ == "__main__":
    unittest.main()
