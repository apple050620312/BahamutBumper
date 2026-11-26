import tempfile
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from .support import BumperTestCase
from .support import PAGE
from bahamut_bumper.errors import SafetyError
from bahamut_bumper.forum import Post
from bahamut_bumper.forum import ThreadSnapshot
from bahamut_bumper.forum import parse_thread_snapshot
from bahamut_bumper.workflow import classify_daily_posts
from bahamut_bumper.workflow import live_round_trip_test
from bahamut_bumper.workflow import bump_account
from bahamut_bumper.config import parse_wall_time


class WorkflowTests(BumperTestCase):
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
                    patch("bahamut_bumper.auth.build_session", return_value=session),
                    patch("bahamut_bumper.auth.fetch_snapshot", side_effect=[before, latest, latest]),
                    patch(
                        "bahamut_bumper.auth.fetch_page_snapshot",
                        side_effect=[delete_source, after_delete],
                    ) as fetch_page,
                    patch("bahamut_bumper.workflow.post_reply"),
                    patch("bahamut_bumper.workflow.delete_post") as delete,
                    patch("bahamut_bumper.auth.persist_session_cookies"),
                    patch("bahamut_bumper.workflow.notify_cookie_expiry"),
                ):
                    result = bump_account(config)

                self.assertTrue(result["posted_new"])
                self.assertEqual([call.args[2] for call in fetch_page.call_args_list], [old_page, old_page])
                self.assertTrue(fetch_page.call_args_list[1].kwargs["allow_empty"])
                self.assertIs(delete.call_args.args[1], delete_source)
                self.assertEqual(delete.call_args.args[2], old_post)

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
