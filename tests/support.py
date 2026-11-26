import json
import os
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bahamut_bumper.config import AccountConfig, AppConfig, parse_wall_time


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


class BumperTestCase(unittest.TestCase):
    def setUp(self):
        self.tz = timezone(timedelta(hours=8), "Asia/Taipei")
        self.url = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"
        self.other_url = "https://forum.gamer.com.tw/C.php?bsn=60&snA=12345"

    def config(self, root: Path = Path(".")) -> AccountConfig:
        return AccountConfig(
            target_url=self.url,
            account="sangege01",
            message="推",
            deletable_messages=("推", "eee"),
            timezone=self.tz,
            cookie_file=root / "cookies.json",
            data_dir=root,
            guard_urls=(),
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
            return AppConfig.from_env()

    def app_config(self, root: Path = Path(".")) -> AppConfig:
        return AppConfig(
            data_dir=root, timezone=self.tz, bump_at=parse_wall_time("20:30"),
            retry_minutes=10, max_retries=3, run_missed_on_start=True,
            accounts=(self.config(root),), accounts_file=None,
        )
