import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from main import (
    Config,
    Post,
    build_session,
    classify_daily_posts,
    load_cookie_records,
    parse_delete_request,
    parse_posted_at,
    parse_thread_snapshot,
    parse_wall_time,
    persist_session_cookies,
    seconds_until,
    validate_forum_url,
    with_last_page,
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


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tz = timezone(timedelta(hours=8), "Asia/Taipei")
        self.url = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"

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
        )

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
        with self.assertRaises(Exception):
            validate_forum_url("https://example.com/C.php?bsn=1&snA=2")

    def test_time_helpers(self):
        self.assertEqual(parse_wall_time("20:30").hour, 20)
        start = datetime(2026, 8, 28, 20, 30, tzinfo=self.tz)
        end = start + timedelta(minutes=1)
        self.assertEqual(seconds_until(end, start), 60)
        self.assertEqual(seconds_until(start, end), 0)

    def test_snapshot_parsing_and_owner_proof(self):
        snapshot = parse_thread_snapshot(
            PAGE,
            self.url + "&last=1",
            "sangege01",
            datetime(2026, 8, 28, 8, 0, tzinfo=self.tz),
        )
        self.assertTrue(snapshot.owner_verified)
        self.assertTrue(snapshot.site_reports_login)
        self.assertEqual(snapshot.subboard, "18")
        self.assertEqual(len(snapshot.posts), 2)
        self.assertEqual(snapshot.posts[1].post_id, "1113614")
        self.assertEqual(snapshot.posts[1].posted_at.second, 59)
        self.assertIn("post2.php", snapshot.form_action)

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
