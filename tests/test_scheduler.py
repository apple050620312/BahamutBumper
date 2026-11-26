import json
import tempfile
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from .support import BumperTestCase
from .support import PAGE
from bahamut_bumper.errors import ConfigurationError
from bahamut_bumper.errors import MobileLoginUnavailable
from bahamut_bumper.forum import Post
from bahamut_bumper.forum import parse_thread_snapshot
from bahamut_bumper.scheduler import run_check
from bahamut_bumper.scheduler import run_daemon
from bahamut_bumper.scheduler import run_once
from bahamut_bumper.scheduler import seconds_until
from bahamut_bumper.config import parse_wall_time
from bahamut_bumper.cli import console_help
from bahamut_bumper.cli import execute_console_command


class SchedulerTests(BumperTestCase):
    def test_accounts_file_requires_unique_accounts_threads_and_cookies(self):
        first = {"account": "bump_test", "target_url": self.url, "cookie_file": "first.json"}
        second = {"account": "other", "target_url": self.other_url, "cookie_file": "second.json"}
        for field, value in (
            ("account", "BUMP_TEST"),
            ("target_url", self.url + "&page=2"),
            ("target_url", self.url.replace("bsn=100", "bsn=0100")),
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
                        "account": "bump_test",
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
                (self.url, "bump_test", "first"),
                (self.other_url, "other", "second"),
            ):
                snapshot = parse_thread_snapshot(
                    PAGE.replace("bump_test", account), url, account, now
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
                patch("bahamut_bumper.workflow.datetime", wraps=datetime) as clock,
                patch("bahamut_bumper.auth._password_login", side_effect=AssertionError("unexpected login")),
                patch("bahamut_bumper.auth.fetch_snapshot", side_effect=fetch),
                patch("bahamut_bumper.workflow.post_reply", side_effect=publish),
            ):
                clock.now.return_value = now
                checks = run_check(config)
                result = run_once(config)

            self.assertTrue(checks["ok"])
            self.assertEqual(published, [self.url, self.other_url])
            self.assertEqual(
                [(item["account"], item["target"]) for item in result["results"]],
                [("bump_test", self.url), ("other", self.other_url)],
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
                    {"account": "bump_test", "target_url": self.url},
                    {"account": "other", "target_url": self.other_url},
                ],
            )
            config = replace(config, bump_at=parse_wall_time("00:00"))
            calls = []

            def attempt(profile):
                calls.append(profile.account)
                if profile.account == "bump_test":
                    raise MobileLoginUnavailable("HTTP 403", retryable=False)
                if calls.count("other") == 1:
                    raise MobileLoginUnavailable("HTTP 503")
                return {"ok": True, "posted_new": True}

            stopped = False

            def wait(seconds):
                nonlocal stopped
                if seconds != config.retry_minutes * 60:
                    stopped = True

            with (
                patch("bahamut_bumper.scheduler.bump_account", side_effect=attempt),
                patch(
                    "bahamut_bumper.scheduler.next_scheduled_at", return_value=datetime.now(self.tz) + timedelta(days=1)
                ),
                patch("bahamut_bumper.scheduler.notify_failure"),
                self.assertLogs("bahamut-bumper", level="INFO"),
            ):
                run_daemon(config, wait=wait, stopped=lambda: stopped)

            self.assertEqual(calls, ["bump_test", "other", "other"])
            result = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertEqual([item["ok"] for item in result["results"]], [False, True])
            self.assertEqual(result["results"][0]["error"], "HTTP 403")

    def test_time_helpers(self):
        self.assertEqual(parse_wall_time("20:30").hour, 20)
        start = datetime(2026, 8, 28, 20, 30, tzinfo=self.tz)
        end = start + timedelta(minutes=1)
        self.assertEqual(seconds_until(end, start), 60)
        self.assertEqual(seconds_until(start, end), 0)

    def test_pterodactyl_console_commands(self):
        self.assertIn("check", console_help())
        self.assertIn("login-test", console_help())
        self.assertFalse(execute_console_command("help", self.app_config()))
        self.assertTrue(execute_console_command("stop", self.app_config()))

    def test_daemon_stops_blocked_login_retries_but_retries_transient_failures(self):
        for status, retryable, attempts in ((403, False, 1), (429, False, 1), (503, True, 3)):
            with self.subTest(status=status):
                config = replace(self.app_config(), bump_at=parse_wall_time("00:00"))

                stopped = False

                def wait(seconds):
                    nonlocal stopped
                    if seconds != config.retry_minutes * 60:
                        stopped = True

                with (
                    patch(
                        "bahamut_bumper.scheduler.bump_account",
                        side_effect=MobileLoginUnavailable(
                            f"HTTP {status}", retryable=retryable
                        ),
                    ) as run,
                    patch(
                        "bahamut_bumper.scheduler.next_scheduled_at",
                        return_value=datetime.now(self.tz) + timedelta(days=1),
                    ),
                    patch("bahamut_bumper.scheduler.write_status") as write,
                    patch("bahamut_bumper.scheduler.notify_failure") as notify,
                    self.assertLogs("bahamut-bumper", level="INFO"),
                ):
                    run_daemon(config, wait=wait, stopped=lambda: stopped)
                self.assertEqual(run.call_count, attempts)
                self.assertEqual(write.call_args.args[1]["retryable"], retryable)
                self.assertEqual(notify.call_count, attempts)
