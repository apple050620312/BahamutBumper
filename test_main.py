import unittest
from datetime import date, datetime, timedelta, timezone

from main import parse_posted_at, parse_wall_time, seconds_until, with_last_page


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tz = timezone(timedelta(hours=8), "Asia/Taipei")

    def test_relative_timestamps(self):
        today = date(2026, 8, 28)
        self.assertEqual(
            parse_posted_at("昨天 20:24 ", today, self.tz),
            datetime(2026, 8, 27, 20, 24, tzinfo=self.tz),
        )
        self.assertEqual(
            parse_posted_at("今天 18:00", today, self.tz),
            datetime(2026, 8, 28, 18, 0, tzinfo=self.tz),
        )

    def test_absolute_timestamp(self):
        self.assertEqual(
            parse_posted_at("2026-08-11 14:42:03", date(2026, 8, 28), self.tz),
            datetime(2026, 8, 11, 14, 42, tzinfo=self.tz),
        )

    def test_last_page_url_is_idempotent(self):
        url = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"
        expected = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415&last=1#down"
        self.assertEqual(with_last_page(url), expected)
        self.assertEqual(with_last_page(expected), expected)

    def test_time_helpers(self):
        self.assertEqual(parse_wall_time("18:00").hour, 18)
        start = datetime(2026, 8, 28, 18, 0, tzinfo=self.tz)
        end = datetime(2026, 8, 28, 18, 1, tzinfo=self.tz)
        self.assertEqual(seconds_until(end, start), 60)
        self.assertEqual(seconds_until(start, end), 0)


if __name__ == "__main__":
    unittest.main()
