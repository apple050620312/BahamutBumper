#!/usr/bin/env python3
"""Pterodactyl-friendly Bahamut daily self-bump daemon.

The process sleeps until the configured Asia/Taipei wall-clock time, then uses
Playwright with an uploaded storage-state file.  Every operation is idempotent:
an existing self-reply from today prevents another reply, and yesterday's
self-reply is deleted only after today's reply is visible.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


LOG = logging.getLogger("bahamut-bumper")
DEFAULT_URL = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"
FULL_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?::\d{2})?")
RELATIVE_TIMESTAMP = re.compile(r"(今天|昨天)\s+(\d{2}:\d{2})")
FLOOR_PATTERN = re.compile(r"^\s*(\d+)\s*樓\s*$")


class ConfigurationError(RuntimeError):
    """The daemon cannot safely run with its current configuration."""


class AuthenticationError(RuntimeError):
    """The saved Bahamut login is absent or expired."""


class SafetyError(RuntimeError):
    """A page state was ambiguous, so no destructive action was taken."""


@dataclass(frozen=True)
class Config:
    target_url: str
    account: str
    message: str
    bump_at: wall_time
    timezone: ZoneInfo
    state_file: Path
    data_dir: Path
    headless: bool
    browser_name: str
    executable_path: str | None
    retry_minutes: int
    max_retries: int
    run_missed_on_start: bool

    @classmethod
    def from_env(cls, *, force_headed: bool = False) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser().resolve()
        state_file = Path(
            os.getenv("BAHAMUT_STORAGE_STATE", str(data_dir / "storage_state.json"))
        ).expanduser().resolve()
        timezone_name = os.getenv("TZ", "Asia/Taipei")
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ConfigurationError(f"無效時區：{timezone_name}") from exc

        return cls(
            target_url=os.getenv("BAHAMUT_TARGET_URL", DEFAULT_URL).strip(),
            account=os.getenv("BAHAMUT_ACCOUNT", "sangege01").strip(),
            message=os.getenv("BAHAMUT_MESSAGE", "推"),
            bump_at=parse_wall_time(os.getenv("BUMP_TIME", "18:00")),
            timezone=timezone,
            state_file=state_file,
            data_dir=data_dir,
            headless=False if force_headed else env_bool("HEADLESS", True),
            browser_name=os.getenv("PLAYWRIGHT_BROWSER", "chromium").strip().lower(),
            executable_path=os.getenv("CHROMIUM_EXECUTABLE_PATH") or None,
            retry_minutes=max(1, int(os.getenv("RETRY_MINUTES", "10"))),
            max_retries=max(1, int(os.getenv("MAX_RETRIES", "3"))),
            run_missed_on_start=env_bool("RUN_MISSED_ON_START", True),
        )


@dataclass(frozen=True)
class Post:
    post_id: str
    floor: int
    author: str
    posted_at: datetime
    content: str


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_wall_time(value: str) -> wall_time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise ConfigurationError("BUMP_TIME 必須使用 HH:MM，例如 18:00") from exc
    return parsed.time()


def with_last_page(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["last"] = "1"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "down"))


def parse_posted_at(text: str, today: date, timezone: ZoneInfo) -> datetime | None:
    full = FULL_TIMESTAMP.search(text)
    if full:
        return datetime.strptime(
            f"{full.group(1)} {full.group(2)}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone)

    relative = RELATIVE_TIMESTAMP.search(text)
    if not relative:
        return None
    post_date = today if relative.group(1) == "今天" else today - timedelta(days=1)
    parsed_time = datetime.strptime(relative.group(2), "%H:%M").time()
    return datetime.combine(post_date, parsed_time, tzinfo=timezone)


def seconds_until(target: datetime, now: datetime) -> float:
    return max(0.0, (target - now).total_seconds())


def next_scheduled_at(config: Config, now: datetime) -> datetime:
    candidate = datetime.combine(now.date(), config.bump_at, tzinfo=config.timezone)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigurationError(
            "尚未安裝 Playwright；請先執行 pip install -r requirements.txt"
        ) from exc
    return sync_playwright


def launch_browser(playwright: Any, config: Config) -> tuple[Any, Any]:
    browser_type = getattr(playwright, config.browser_name, None)
    if browser_type is None:
        raise ConfigurationError(
            "PLAYWRIGHT_BROWSER 僅接受 chromium、firefox 或 webkit"
        )
    launch_args: dict[str, Any] = {"headless": config.headless}
    if config.executable_path:
        launch_args["executable_path"] = config.executable_path
    try:
        browser = browser_type.launch(**launch_args)
    except Exception as exc:
        raise ConfigurationError(
            "無法啟動瀏覽器。Pterodactyl 映像需包含 Chromium 相依套件，"
            "並先執行 python -m playwright install chromium。"
        ) from exc

    context_args: dict[str, Any] = {
        "locale": "zh-TW",
        "timezone_id": str(config.timezone),
    }
    if config.state_file.exists():
        context_args["storage_state"] = str(config.state_file)
    context = browser.new_context(**context_args)
    return browser, context


def save_state(context: Any, config: Config) -> None:
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(config.state_file))
    if os.name != "nt":
        config.state_file.chmod(0o600)


def logged_in(page: Any) -> bool:
    login_links = page.locator('a[href*="user.gamer.com.tw/login.php"]')
    home_links = page.locator('a[href*="home.gamer.com.tw/homeindex.php"]')
    return login_links.count() == 0 and home_links.count() > 0


def read_posts(page: Any, config: Config, now: datetime) -> list[Post]:
    raw_posts = page.locator('section.c-section[id^="post_"]').evaluate_all(
        """
        sections => sections.map(section => {
          const links = Array.from(section.querySelectorAll('a'));
          const floorLink = links.find(a => /^\\s*\\d+\\s*樓\\s*$/.test(a.textContent || ''));
          if (!floorLink) return null;
          const authorLink = links.find(a => /home\\.gamer\\.com\\.tw\\//.test(a.href || ''));
          const article = section.querySelector('article');
          return {
            post_id: section.id,
            floor_text: floorLink.textContent || '',
            author_href: authorLink ? authorLink.href : '',
            text: section.innerText || '',
            content: article ? article.innerText.trim() : ''
          };
        }).filter(Boolean)
        """
    )
    posts: list[Post] = []
    for raw in raw_posts:
        floor_match = FLOOR_PATTERN.match(raw["floor_text"])
        posted_at = parse_posted_at(raw["text"], now.date(), config.timezone)
        if not floor_match or posted_at is None:
            continue
        author = raw["author_href"].rstrip("/").rsplit("/", 1)[-1]
        posts.append(
            Post(
                post_id=raw["post_id"],
                floor=int(floor_match.group(1)),
                author=author,
                posted_at=posted_at,
                content=raw["content"],
            )
        )
    return posts


def own_posts(posts: list[Post], account: str) -> list[Post]:
    return [post for post in posts if post.author.casefold() == account.casefold()]


def ensure_no_captcha(page: Any) -> None:
    body_text = page.locator("body").inner_text()
    if "驗證碼" in body_text or "CAPTCHA" in body_text.upper():
        raise SafetyError("網站要求驗證碼，腳本不會嘗試繞過；請人工處理後再啟動。")


def post_reply(page: Any, config: Config) -> None:
    ensure_no_captcha(page)
    editor_body = page.frame_locator("iframe#editor").locator("body")
    editor_body.wait_for(state="visible", timeout=15_000)
    editor_body.fill(config.message)
    submit = page.get_by_role("button", name="送出", exact=True).last
    if not submit.is_enabled():
        raise SafetyError("送出按鈕不可用，未送出任何內容。")
    submit.click()
    page.wait_for_timeout(2_000)


def delete_post(page: Any, post: Post, account: str) -> None:
    if post.floor <= 1 or post.author.casefold() != account.casefold():
        raise SafetyError("拒絕刪除：目標不是自己的樓層回覆。")
    section = page.locator(f"#{post.post_id}")
    if section.count() != 1:
        raise SafetyError("拒絕刪除：無法唯一定位昨天的回覆。")

    menu = section.get_by_role("button", name="", exact=True)
    menu.click()
    delete_link = page.get_by_role("link", name="刪除文章", exact=True).last
    delete_link.wait_for(state="visible", timeout=5_000)
    page.once("dialog", lambda dialog: dialog.accept())
    delete_link.click()
    page.wait_for_timeout(2_000)


def refresh_last_page(page: Any, config: Config) -> None:
    page.goto(with_last_page(config.target_url), wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(800)


def run_once(config: Config, *, dry_run: bool = False) -> None:
    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        browser, context = launch_browser(playwright, config)
        try:
            page = context.new_page()
            refresh_last_page(page, config)
            if not logged_in(page):
                raise AuthenticationError(
                    f"{config.account} 的登入狀態不存在或已過期；請重新產生 storage_state.json。"
                )
            ensure_no_captcha(page)

            now = datetime.now(config.timezone)
            posts = own_posts(read_posts(page, config, now), config.account)
            today_posts = [post for post in posts if post.posted_at.date() == now.date()]
            yesterday = now.date() - timedelta(days=1)
            yesterday_posts = [post for post in posts if post.posted_at.date() == yesterday]

            if len(today_posts) > 1:
                raise SafetyError("偵測到今天已有多則自推；停止操作並請人工檢查。")
            if len(yesterday_posts) > 1:
                raise SafetyError("偵測到昨天有多則未刪自推；為避免刪錯，停止操作。")

            old_post = yesterday_posts[0] if yesterday_posts else None
            if dry_run:
                LOG.info(
                    "DRY RUN：今日自推=%d；昨日待刪=%s；不會送出或刪除。",
                    len(today_posts),
                    f"{old_post.floor} 樓 {old_post.post_id}" if old_post else "無",
                )
                return

            if not today_posts:
                before_ids = {post.post_id for post in posts}
                LOG.info("今日尚未自推，準備發布：%r", config.message)
                post_reply(page, config)
                refresh_last_page(page, config)
                verified_now = datetime.now(config.timezone)
                verified_posts = own_posts(read_posts(page, config, verified_now), config.account)
                new_today = [
                    post
                    for post in verified_posts
                    if post.posted_at.date() == verified_now.date()
                    and post.post_id not in before_ids
                    and post.content.strip() == config.message.strip()
                ]
                if len(new_today) != 1:
                    raise SafetyError("無法唯一驗證今日新回覆；保留所有舊內容。")
                LOG.info("發布成功：%d 樓（%s）", new_today[0].floor, new_today[0].post_id)
                save_state(context, config)
            else:
                LOG.info("今天已有自推（%d 樓），不重複發布。", today_posts[0].floor)

            if old_post is None:
                LOG.info("找不到昨天的自推，無需刪除。")
                save_state(context, config)
                return

            refresh_last_page(page, config)
            active_ids = {post.post_id for post in read_posts(page, config, datetime.now(config.timezone))}
            if old_post.post_id not in active_ids:
                LOG.info("昨天的自推已不存在，無需再次刪除。")
                save_state(context, config)
                return

            LOG.info("今日自推已確認，準備刪除昨天的 %d 樓。", old_post.floor)
            delete_post(page, old_post, config.account)
            refresh_last_page(page, config)
            remaining_ids = {
                post.post_id for post in read_posts(page, config, datetime.now(config.timezone))
            }
            if old_post.post_id in remaining_ids:
                raise SafetyError("刪除後仍偵測到昨天的回覆，請人工檢查。")
            LOG.info("已刪除昨天的 %d 樓；今日流程完成。", old_post.floor)
            save_state(context, config)
        finally:
            context.close()
            browser.close()


def normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
    result = dict(cookie)
    if "expirationDate" in result and "expires" not in result:
        result["expires"] = result.pop("expirationDate")
    result.pop("hostOnly", None)
    result.pop("session", None)
    result.pop("storeId", None)
    same_site = result.get("sameSite")
    if isinstance(same_site, str):
        mapping = {"no_restriction": "None", "unspecified": "Lax"}
        result["sameSite"] = mapping.get(same_site.lower(), same_site.capitalize())
    allowed = {
        "name", "value", "url", "domain", "path", "expires",
        "httpOnly", "secure", "sameSite",
    }
    return {key: value for key, value in result.items() if key in allowed}


def import_cookies(config: Config, cookie_file: Path) -> None:
    payload = json.loads(cookie_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "cookies" in payload:
        cookies = payload["cookies"]
    elif isinstance(payload, list):
        cookies = payload
    else:
        raise ConfigurationError("Cookie JSON 必須是陣列，或含有 cookies 陣列的物件。")
    normalized = [normalize_cookie(cookie) for cookie in cookies]
    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        browser, context = launch_browser(playwright, config)
        try:
            context.add_cookies(normalized)
            page = context.new_page()
            refresh_last_page(page, config)
            if not logged_in(page):
                raise AuthenticationError("匯入 Cookie 後仍未登入，請重新匯出完整 gamer.com.tw Cookie。")
            save_state(context, config)
            LOG.info("Cookie 已匯入，並驗證登入成功。")
        finally:
            context.close()
            browser.close()


def interactive_login(config: Config) -> None:
    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        browser, context = launch_browser(playwright, config)
        try:
            page = context.new_page()
            page.goto(config.target_url, wait_until="domcontentloaded", timeout=30_000)
            print("請在開啟的瀏覽器登入巴哈姆特；完成後回到終端機按 Enter。")
            input()
            refresh_last_page(page, config)
            if not logged_in(page):
                raise AuthenticationError("目前仍未登入，未寫入登入狀態。")
            save_state(context, config)
            LOG.info("登入狀態已儲存至 %s", config.state_file)
        finally:
            context.close()
            browser.close()


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError as exc:
        raise ConfigurationError("已有另一個 main.py 實例正在執行。") from exc
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def interruptible_sleep(stop: list[bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not stop[0] and time.monotonic() < deadline:
        time.sleep(min(30.0, max(0.0, deadline - time.monotonic())))


def run_daemon(config: Config) -> None:
    stop = [False]

    def request_stop(signum: int, _frame: Any) -> None:
        LOG.info("收到訊號 %s，將安全停止。", signum)
        stop[0] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    now = datetime.now(config.timezone)
    run_now = config.run_missed_on_start and now.time() >= config.bump_at
    while not stop[0]:
        if not run_now:
            target = next_scheduled_at(config, datetime.now(config.timezone))
            LOG.info("下次執行：%s", target.isoformat())
            interruptible_sleep(stop, seconds_until(target, datetime.now(config.timezone)))
            if stop[0]:
                break
        run_now = False

        run_date = datetime.now(config.timezone).date()
        for attempt in range(1, config.max_retries + 1):
            try:
                LOG.info("開始每日流程（第 %d/%d 次嘗試）。", attempt, config.max_retries)
                run_once(config)
                break
            except Exception as exc:
                LOG.exception("每日流程失敗：%s", exc)
                if attempt >= config.max_retries or datetime.now(config.timezone).date() != run_date:
                    break
                interruptible_sleep(stop, config.retry_minutes * 60)
                if stop[0]:
                    break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="巴哈姆特每日自推與昨日回覆清理")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="立即執行一次後離開")
    mode.add_argument("--dry-run", action="store_true", help="只檢查，不送出或刪除")
    mode.add_argument("--login", action="store_true", help="開啟有介面的瀏覽器並儲存登入狀態")
    mode.add_argument("--import-cookies", type=Path, metavar="FILE", help="匯入 Cookie JSON")
    return parser


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args()
    try:
        config = Config.from_env(force_headed=args.login)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with process_lock(config.data_dir / "daemon.lock"):
            if args.login:
                interactive_login(config)
            elif args.import_cookies:
                import_cookies(config, args.import_cookies.expanduser().resolve())
            elif args.dry_run:
                run_once(config, dry_run=True)
            elif args.once:
                run_once(config)
            else:
                run_daemon(config)
        return 0
    except (ConfigurationError, AuthenticationError, SafetyError) as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
