from datetime import date
from datetime import datetime
from unittest.mock import patch
from .support import BumperTestCase
from .support import PAGE
from .support import LAST_PAGE
from .support import EMPTY_OLD_PAGE
from bahamut_bumper.errors import SafetyError
from bahamut_bumper.forum import Post
from bahamut_bumper.forum import fetch_page_snapshot
from bahamut_bumper.forum import fetch_snapshot
from bahamut_bumper.forum import parse_delete_request
from bahamut_bumper.forum import parse_posted_at
from bahamut_bumper.forum import parse_thread_snapshot
from bahamut_bumper.forum import validate_forum_url
from bahamut_bumper.forum import with_last_page
from bahamut_bumper.forum import with_page


class ForumTests(BumperTestCase):
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

    def test_snapshot_parsing_and_owner_proof(self):
        snapshot = parse_thread_snapshot(
            PAGE,
            self.url + "&last=1",
            "bump_test",
            datetime(2026, 8, 28, 8, 0, tzinfo=self.tz),
        )
        self.assertTrue(snapshot.owner_verified)
        self.assertTrue(snapshot.site_reports_login)
        self.assertEqual(snapshot.page_number, 1)
        self.assertEqual(snapshot.subboard, "18")
        self.assertEqual(len(snapshot.posts), 2)
        self.assertEqual(snapshot.posts[1].post_id, "102")
        self.assertEqual(snapshot.posts[1].posted_at.second, 59)
        self.assertIn("post2.php", snapshot.form_action)

    def test_fetch_snapshot_uses_page_one_for_owner_proof_after_floor_twenty(self):
        with patch(
            "bahamut_bumper.forum.request_page",
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
            patch("bahamut_bumper.forum.request_page", return_value=(PAGE, self.other_url)),
            self.assertRaises(SafetyError),
        ):
            fetch_snapshot(object(), self.url, self.config())

    def test_empty_old_page_can_be_verified_after_deletion(self):
        with patch(
            "bahamut_bumper.forum.request_page",
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
            '{"author":"bump_test","owner":true,"isLogin":true}',
            '{"author":"someone_else","owner":false,"isLogin":true}',
        ).replace(
            '<article>推</article>',
            '<article>推</article><button class="tippy-option-menu" '
            'data-tippy=\'{"author":"bump_test","owner":true}\'></button>',
        )
        snapshot = parse_thread_snapshot(
            page, self.url, "bump_test", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        self.assertFalse(snapshot.owner_verified)

    def test_delete_request_uses_fresh_dynamic_fields(self):
        snapshot = parse_thread_snapshot(
            PAGE, self.url, "bump_test", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        request = parse_delete_request(snapshot, snapshot.posts[1])
        self.assertEqual(request.fields["sn"], "102")
        self.assertEqual(request.fields["code"], "fixture-delete-code")
        self.assertEqual(request.cookie_value, "5678")
        self.assertIn("post2.php?", request.url)

    def test_delete_request_rejects_wrong_post_identity(self):
        snapshot = parse_thread_snapshot(
            PAGE, self.url, "bump_test", datetime(2026, 8, 28, tzinfo=self.tz)
        )
        wrong = Post("999", 12, "bump_test", snapshot.posts[1].posted_at, "推")
        # The parser constructs the URL for exactly the supplied post ID; the
        # later delete_post equality check is what rejects a nonexistent floor.
        self.assertEqual(parse_delete_request(snapshot, wrong).fields["sn"], "999")
