#!/usr/bin/env python3
"""Generic-Python Pterodactyl daemon for a daily Bahamut self-bump.

The program uses ordinary HTTPS requests and an exported cookie file.  It
publishes at most one reply per calendar day, verifies the new reply, and only
then removes the previous day's reply.  Ambiguous states fail closed.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import queue
import re
import shlex
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time as wall_time, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


LOG = logging.getLogger("bahamut-bumper")
DEFAULT_URL = "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"
FORUM_HOST = "forum.gamer.com.tw"
MOBILE_LOGIN_URL = "https://api.gamer.com.tw/mobile_app/user/v3/do_login.php"
FULL_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?::(\d{2}))?"
)
RELATIVE_TIMESTAMP = re.compile(r"(今天|昨天)\s+(\d{2}:\d{2})")
POST_ID_PATTERN = re.compile(r"(\d+)$")
PDEL_PATTERN = re.compile(
    r"function\s+pdel\s*\(\s*sn\s*\)\s*\{.*?"
    r"var\s+args\s*=\s*'([^']*)'\s*\+\s*sn\s*\+\s*'([^']*)'\s*;.*?"
    r"delPost\s*\(\s*sn\s*,\s*args\s*,\s*'([^']+)'\s*\)",
    re.DOTALL,
)


class ConfigurationError(RuntimeError):
    """The daemon cannot safely run with its current configuration."""


class AuthenticationError(RuntimeError):
    """The exported Bahamut login is absent, expired, or is the wrong user."""


class SafetyError(RuntimeError):
    """Page state is ambiguous, so a write operation must not continue."""


@dataclass(frozen=True)
class Config:
    target_url: str
    account: str
    message: str
    deletable_messages: tuple[str, ...]
    bump_at: wall_time
    timezone: ZoneInfo
    cookie_file: Path
    data_dir: Path
    guard_urls: tuple[str, ...]
    retry_minutes: int
    max_retries: int
    run_missed_on_start: bool
    timeout_seconds: int
    discord_webhook_file: Path
    discord_user_id: str
    password_file: Path
    auto_mobile_login: bool

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser().resolve()
        cookie_file = Path(
            os.getenv("BAHAMUT_COOKIE_FILE", str(data_dir / "cookies.json"))
        ).expanduser().resolve()
        timezone_name = os.getenv("TZ", "Asia/Taipei")
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ConfigurationError(f"無效時區：{timezone_name}") from exc

        target_url = validate_forum_url(
            os.getenv("BAHAMUT_TARGET_URL", DEFAULT_URL).strip()
        )
        guards: list[str] = []
        for value in os.getenv("BAHAMUT_GUARD_URLS", "").split(","):
            if value.strip():
                guard = validate_forum_url(value.strip())
                if thread_identity(guard) != thread_identity(target_url):
                    guards.append(guard)

        account = os.getenv("BAHAMUT_ACCOUNT", "sangege01").strip()
        message = os.getenv("BAHAMUT_MESSAGE", "推").strip()
        if not account:
            raise ConfigurationError("BAHAMUT_ACCOUNT 不可為空白。")
        if not message:
            raise ConfigurationError("BAHAMUT_MESSAGE 不可為空白。")
        discord_user_id = os.getenv(
            "DISCORD_ATTENTION_USER_ID", "523114942434639873"
        ).strip()
        if not discord_user_id.isdigit():
            raise ConfigurationError("DISCORD_ATTENTION_USER_ID 必須是純數字。")
        deletable_messages = tuple(
            dict.fromkeys(
                item.strip()
                for item in os.getenv("BAHAMUT_DELETE_MESSAGES", f"{message},eee").split(",")
                if item.strip()
            )
        )

        return cls(
            target_url=target_url,
            account=account,
            message=message,
            deletable_messages=deletable_messages,
            bump_at=parse_wall_time(os.getenv("BUMP_TIME", "20:30")),
            timezone=timezone,
            cookie_file=cookie_file,
            data_dir=data_dir,
            guard_urls=tuple(dict.fromkeys(guards)),
            retry_minutes=max(1, int(os.getenv("RETRY_MINUTES", "10"))),
            max_retries=max(1, int(os.getenv("MAX_RETRIES", "3"))),
            run_missed_on_start=env_bool("RUN_MISSED_ON_START", True),
            timeout_seconds=max(5, int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))),
            discord_webhook_file=Path(
                os.getenv(
                    "DISCORD_WEBHOOK_FILE", str(data_dir / "discord_webhook.txt")
                )
            ).expanduser().resolve(),
            discord_user_id=discord_user_id,
            password_file=Path(
                os.getenv(
                    "BAHAMUT_PASSWORD_FILE", str(data_dir / "bahamut_password.txt")
                )
            ).expanduser().resolve(),
            auto_mobile_login=env_bool("AUTO_MOBILE_LOGIN", True),
        )


@dataclass(frozen=True)
class Post:
    post_id: str
    floor: int
    author: str
    posted_at: datetime
    content: str


@dataclass(frozen=True)
class ThreadSnapshot:
    url: str
    html_text: str
    posts: tuple[Post, ...]
    form_action: str | None
    form_fields: dict[str, str]
    subboard: str | None
    owner_verified: bool
    site_reports_login: bool | None
    page_number: int | None


@dataclass(frozen=True)
class DeleteRequest:
    url: str
    cookie_value: str
    fields: dict[str, str]


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_wall_time(value: str) -> wall_time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise ConfigurationError("BUMP_TIME 必須使用 HH:MM，例如 20:30") from exc
    return parsed.time()


def validate_forum_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if (
        parts.scheme != "https"
        or parts.hostname != FORUM_HOST
        or not parts.path.endswith("/C.php")
        or not query.get("bsn", "").isdigit()
        or not query.get("snA", "").isdigit()
    ):
        raise ConfigurationError(
            "文章網址必須是 https://forum.gamer.com.tw/C.php 且包含數字 bsn、snA。"
        )
    clean_query = {"bsn": query["bsn"], "snA": query["snA"]}
    return urlunsplit(("https", FORUM_HOST, "/C.php", urlencode(clean_query), ""))


def thread_identity(url: str) -> tuple[str, str]:
    query = dict(parse_qsl(urlsplit(url).query))
    return query["bsn"], query["snA"]


def with_last_page(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["last"] = "1"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "down"))


def with_page(url: str, page: int) -> str:
    if page < 1:
        raise ValueError("頁碼必須大於等於 1。")
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("last", None)
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def parse_posted_at(text: str, today: date, timezone: ZoneInfo) -> datetime | None:
    full = FULL_TIMESTAMP.search(text)
    if full:
        seconds = full.group(3) or "00"
        return datetime.strptime(
            f"{full.group(1)} {full.group(2)}:{seconds}", "%Y-%m-%d %H:%M:%S"
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


def import_http_dependencies() -> tuple[Any, Any]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ConfigurationError(
            "缺少套件；請先執行 python -m pip install --user -r requirements.txt"
        ) from exc
    return requests, BeautifulSoup


def _cookie_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        return payload["cookies"]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [{"name": key, "value": value} for key, value in payload.items()]
    raise ConfigurationError("Cookie JSON 格式不支援。")


def load_cookie_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise AuthenticationError(
            f"找不到 Cookie 檔：{path}；請依 README 匯出後上傳。"
        )
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise AuthenticationError(f"Cookie 檔是空的：{path}")
    try:
        return _cookie_records(json.loads(raw))
    except json.JSONDecodeError:
        cookie = SimpleCookie()
        try:
            cookie.load(raw.removeprefix("Cookie:").strip())
        except Exception as exc:
            raise ConfigurationError("Cookie 檔既不是 JSON，也不是 Cookie header。") from exc
        records = [
            {"name": name, "value": morsel.value, "domain": ".gamer.com.tw"}
            for name, morsel in cookie.items()
        ]
        if not records:
            raise ConfigurationError("Cookie header 中沒有可解析的 Cookie。")
        return records


def new_forum_session() -> Any:
    requests, _ = import_http_dependencies()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
            "Cache-Control": "no-cache",
        }
    )
    return session


def add_cookie_records(session: Any, records: list[dict[str, Any]]) -> None:
    for record in records:
        name = str(record.get("name", "")).strip()
        value = str(record.get("value", ""))
        if not name:
            continue
        domain = str(record.get("domain") or ".gamer.com.tw")
        path = str(record.get("path") or "/")
        expires = record.get("expirationDate", record.get("expires"))
        try:
            expires = int(float(expires)) if expires not in (None, "", 0, -1) else None
        except (TypeError, ValueError):
            expires = None
        session.cookies.set(
            name,
            value,
            domain=domain,
            path=path,
            expires=expires,
            secure=bool(record.get("secure", False)),
        )


def build_session(config: Config) -> Any:
    session = new_forum_session()
    add_cookie_records(session, load_cookie_records(config.cookie_file))
    if not session.cookies:
        raise AuthenticationError("Cookie 檔沒有任何有效 Cookie。")
    return session


def persist_session_cookies(session: Any, config: Config) -> None:
    """Atomically preserve any Set-Cookie rotations received from Bahamut."""
    if not getattr(session, "_bahamut_cookie_dirty", False):
        return
    records = []
    for cookie in session.cookies:
        if cookie.name == "ckFORUM_pdel":
            continue
        records.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or ".gamer.com.tw",
                "path": cookie.path or "/",
                "expires": cookie.expires,
                "secure": bool(cookie.secure),
            }
        )
    if not records:
        return
    config.cookie_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.cookie_file.with_suffix(config.cookie_file.suffix + ".tmp")
    try:
        backup = config.cookie_file.with_suffix(config.cookie_file.suffix + ".backup")
        if config.cookie_file.is_file() and not backup.exists():
            backup.write_bytes(config.cookie_file.read_bytes())
            if os.name != "nt":
                backup.chmod(0o600)
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(config.cookie_file)
        setattr(session, "_bahamut_cookie_dirty", False)
        if os.name != "nt":
            config.cookie_file.chmod(0o600)
    except OSError as exc:
        LOG.warning("無法保存網站更新後的 Cookie：%s", exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def mark_cookie_updates(session: Any, response: Any) -> None:
    responses = [*getattr(response, "history", []), response]
    if any(len(item.cookies) > 0 for item in responses):
        setattr(session, "_bahamut_cookie_dirty", True)


def request_page(session: Any, url: str, config: Config) -> tuple[str, str]:
    try:
        response = session.get(url, timeout=config.timeout_seconds, allow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        raise SafetyError(f"讀取巴哈頁面失敗：{exc}") from exc
    ensure_no_challenge(response.text, response.url)
    mark_cookie_updates(session, response)
    return response.text, response.url


def ensure_no_challenge(text: str, final_url: str = "") -> None:
    lower = text.casefold()
    markers = (
        "captcha",
        "cf-chl-",
        "cloudflare ray id",
        "驗證碼",
        "確認您不是機器人",
    )
    if any(marker.casefold() in lower for marker in markers):
        raise SafetyError("網站要求 CAPTCHA/人機驗證；腳本不會嘗試繞過。")
    if "login.php" in final_url:
        raise AuthenticationError("Cookie 已失效，網站把請求導向登入頁。")


def _post_author(section: Any) -> str:
    author_link = section.select_one("a.userid")
    if author_link:
        return author_link.get_text(" ", strip=True)
    for link in section.select('a[href*="home.gamer.com.tw"]'):
        href = link.get("href", "").rstrip("/")
        if href:
            return href.rsplit("/", 1)[-1]
    return ""


def _first_post_metadata(soup: Any) -> dict[str, Any] | None:
    first_post = None
    for section in soup.select('section.c-section[id^="post_"]'):
        floor = section.select_one("a.floor[data-floor]") or section.select_one("a.floor")
        if floor and re.search(
            r"\b1\b", str(floor.get("data-floor") or floor.get_text(" ", strip=True))
        ):
            first_post = section
            break
    if first_post is None:
        return None
    for element in first_post.select(".tippy-option-menu[data-tippy]"):
        raw = html.unescape(element.get("data-tippy", ""))
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if "author" in data:
            return data
    return None


def parse_thread_snapshot(
    page_text: str, url: str, account: str, now: datetime
) -> ThreadSnapshot:
    _, BeautifulSoup = import_http_dependencies()
    soup = BeautifulSoup(page_text, "html.parser")
    posts: list[Post] = []
    for section in soup.select('section.c-section[id^="post_"]'):
        id_match = POST_ID_PATTERN.search(str(section.get("id", "")))
        floor_link = section.select_one("a.floor[data-floor]") or section.select_one(
            "a.floor"
        )
        time_node = section.select_one(".edittime[data-mtime]") or section.select_one(
            ".edittime"
        )
        if not id_match or not floor_link or not time_node:
            continue
        floor_text = str(floor_link.get("data-floor") or floor_link.get_text(" ", strip=True))
        floor_match = re.search(r"\d+", floor_text)
        timestamp_text = str(time_node.get("data-mtime") or time_node.get_text(" ", strip=True))
        posted_at = parse_posted_at(timestamp_text, now.date(), now.tzinfo)
        if not floor_match or posted_at is None:
            continue
        article = section.select_one("article")
        posts.append(
            Post(
                post_id=id_match.group(1),
                floor=int(floor_match.group()),
                author=_post_author(section),
                posted_at=posted_at,
                content=article.get_text("\n", strip=True) if article else "",
            )
        )

    form = soup.select_one('form[name="frm"]')
    fields: dict[str, str] = {}
    form_action: str | None = None
    if form:
        form_action = urljoin(url, str(form.get("action") or "post2.php"))
        for node in form.select("input[name]"):
            fields[str(node.get("name"))] = str(node.get("value") or "")

    owner_metadata = _first_post_metadata(soup)
    owner_verified = bool(
        owner_metadata
        and str(owner_metadata.get("author", "")).casefold() == account.casefold()
        and owner_metadata.get("owner") is True
    )
    site_reports_login = None
    if owner_metadata and "isLogin" in owner_metadata:
        site_reports_login = owner_metadata.get("isLogin") is True
    metadata_subboard = None
    if owner_metadata and owner_metadata.get("subbsn") is not None:
        metadata_subboard = str(owner_metadata["subbsn"])
    subboard = fields.get("threadSubbsn") or fields.get("subbsn") or metadata_subboard
    if not subboard:
        match = re.search(r"threadSubbsn=(\d+)", page_text)
        subboard = match.group(1) if match else None

    page_number = None
    current_page = soup.select_one(".BH-pagebtnA .pagenow") or soup.select_one("a.pagenow")
    if current_page:
        page_match = re.search(r"\d+", current_page.get_text(" ", strip=True))
        if page_match:
            page_number = int(page_match.group())
    if page_number is None and any(post.floor == 1 for post in posts):
        page_number = 1

    return ThreadSnapshot(
        url=url,
        html_text=page_text,
        posts=tuple(posts),
        form_action=form_action,
        form_fields=fields,
        subboard=subboard,
        owner_verified=owner_verified,
        site_reports_login=site_reports_login,
        page_number=page_number,
    )


def _fetch_snapshot_at(
    session: Any,
    thread_url: str,
    request_url: str,
    config: Config,
    *,
    allow_empty: bool = False,
) -> ThreadSnapshot:
    page_text, final_url = request_page(session, request_url, config)
    snapshot = parse_thread_snapshot(page_text, final_url, config.account, datetime.now(config.timezone))
    if not snapshot.posts and not allow_empty:
        raise SafetyError("頁面中找不到任何可解析樓層；可能是頁面結構已變更。")
    if not any(post.floor == 1 for post in snapshot.posts):
        # `last=1` opens the final page. Once a thread has more than one page,
        # that page no longer contains the OP metadata used to prove that the
        # authenticated account owns the thread. Fetch page 1 only for that
        # proof while preserving the final-page posts and reply form.
        first_text, first_url = request_page(session, thread_url, config)
        first_page = parse_thread_snapshot(
            first_text, first_url, config.account, datetime.now(config.timezone)
        )
        if not first_page.posts or not any(
            post.floor == 1 for post in first_page.posts
        ):
            raise SafetyError("第一頁中找不到文章首樓；可能是頁面結構已變更。")
        snapshot = replace(
            snapshot,
            owner_verified=first_page.owner_verified,
            site_reports_login=first_page.site_reports_login,
        )
    return snapshot


def fetch_snapshot(session: Any, url: str, config: Config) -> ThreadSnapshot:
    return _fetch_snapshot_at(session, url, with_last_page(url), config)


def fetch_page_snapshot(
    session: Any,
    url: str,
    page: int,
    config: Config,
    *,
    allow_empty: bool = False,
) -> ThreadSnapshot:
    return _fetch_snapshot_at(
        session,
        url,
        with_page(url, page),
        config,
        allow_empty=allow_empty,
    )


def assert_owner_login(snapshot: ThreadSnapshot, config: Config) -> None:
    if not snapshot.owner_verified:
        if snapshot.site_reports_login is False:
            raise AuthenticationError(
                "巴哈頁面明確回報目前未登入；Cookie 未被 Pterodactyl 端接受，"
                "或已在伺服器端失效。請確認帳密檔後於 Pterodactyl Console 輸入 login-test。"
            )
        if snapshot.site_reports_login is True:
            raise AuthenticationError(
                f"巴哈頁面顯示已登入，但不是文章作者 {config.account}；請確認匯出的帳號。"
            )
        raise AuthenticationError(
            f"無法證明目前 Cookie 是文章作者 {config.account}；可能已登出、帳號錯誤或頁面改版。"
        )
    if not snapshot.form_action or "post2.php" not in snapshot.form_action:
        raise AuthenticationError("找不到快速回覆表單；Cookie 可能失效或帳號不能回覆。")


def read_secret_file(path: Path, label: str) -> str:
    if not path.is_file():
        raise ConfigurationError(f"找不到 {label} 檔：{path}")
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise ConfigurationError(f"無法讀取 {label} 檔。") from exc
    if not value:
        raise ConfigurationError(f"{label} 檔是空的。")
    if len(value) > 1024:
        raise ConfigurationError(f"{label} 檔內容異常過長。")
    return value


def record_mobile_login_attempt(config: Config) -> None:
    """Limit manual login tests to one request per five minutes."""
    path = config.data_dir / "mobile_login_attempt.json"
    now_epoch = time.time()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            elapsed = now_epoch - float(payload.get("attempted_at_epoch", 0))
            if elapsed < 300:
                wait_seconds = max(1, int(300 - elapsed))
                raise SafetyError(
                    f"mobile API 登入測試五分鐘內只能一次；請等待 {wait_seconds} 秒。"
                )
        except json.JSONDecodeError:
            pass
    config.data_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "attempted_at": datetime.now(config.timezone).isoformat(),
                "attempted_at_epoch": now_epoch,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _mobile_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "mobile API 未回傳可辨識的登入結果"
    message = payload.get("message")
    error = payload.get("error")
    if not message and isinstance(error, dict):
        message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return "mobile API 未核發登入 Cookie"
    return re.sub(r"[\r\n]+", " ", message.strip())[:200]


def mobile_login(
    config: Config, *, enforce_rate_limit: bool = False, notify_success: bool = False
) -> dict[str, Any]:
    """Perform password-only mobile login, then prove forum ownership."""
    password = read_secret_file(config.password_file, "巴哈密碼")
    if enforce_rate_limit:
        record_mobile_login_attempt(config)
    requests, _ = import_http_dependencies()
    mobile = requests.Session()
    mobile.headers.update(
        {
            "User-Agent": "Bahadroid (https://www.gamer.com.tw/)",
            "Accept-Language": "zh-TW,zh;q=0.9",
        }
    )
    mobile.cookies.set("ckAPP_VCODE", "7045", domain="api.gamer.com.tw", path="/")
    try:
        response = mobile.post(
            MOBILE_LOGIN_URL,
            data={"uid": config.account, "passwd": password, "vcode": "7045"},
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
    except Exception as exc:
        raise AuthenticationError(
            f"mobile API 登入連線失敗（{type(exc).__name__}）。"
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        lower = response.text.casefold()
        if "recaptcha" in lower or "captcha" in lower or "驗證" in response.text:
            raise AuthenticationError(
                "mobile API 要求額外的人機／新裝置驗證；腳本不會繞過。"
            ) from exc
        raise AuthenticationError("mobile API 未回傳 JSON 登入結果。") from exc

    userid = str(payload.get("userid", "")) if isinstance(payload, dict) else ""
    rune = next(
        (cookie.value for cookie in mobile.cookies if cookie.name == "BAHARUNE"), None
    )
    if userid.casefold() != config.account.casefold() or not rune:
        raise AuthenticationError(f"mobile API 登入失敗：{_mobile_error_message(payload)}")

    forum = new_forum_session()
    if config.cookie_file.is_file():
        try:
            add_cookie_records(forum, load_cookie_records(config.cookie_file))
        except (AuthenticationError, ConfigurationError):
            pass
    received_names: list[str] = []
    for cookie in mobile.cookies:
        received_names.append(cookie.name)
        forum.cookies.set(
            cookie.name,
            cookie.value,
            domain=".gamer.com.tw",
            path=cookie.path or "/",
            expires=cookie.expires,
            secure=bool(cookie.secure),
        )
    forum.cookies.set(
        "BAHARUNE", rune, domain=".gamer.com.tw", path="/", secure=True
    )
    setattr(forum, "_bahamut_cookie_dirty", True)
    snapshot = fetch_snapshot(forum, config.target_url, config)
    assert_owner_login(snapshot, config)
    persist_session_cookies(forum, config)
    result = {
        "ok": True,
        "tested_at": datetime.now(config.timezone).isoformat(),
        "api_account": userid,
        "forum_owner_verified": snapshot.owner_verified,
        "received_cookie_names": sorted(set(received_names)),
        "cookie_saved": True,
        "posted": False,
        "deleted": False,
    }
    if notify_success:
        send_discord_notification(
            config,
            "✅ 巴哈 mobile API 帳密登入測試成功，並已驗證論壇帳號；沒有發文或刪文。",
            dedupe_key=f"mobile-login-test-{datetime.now(config.timezone).date().isoformat()}",
        )
    return result


def refresh_login_for_production(config: Config) -> None:
    if not config.auto_mobile_login:
        return
    LOG.info("使用 mobile API 更新本次流程的巴哈登入 Cookie。")
    mobile_login(config)


def own_posts(snapshot: ThreadSnapshot, account: str) -> list[Post]:
    return [
        post
        for post in snapshot.posts
        if post.floor > 1 and post.author.casefold() == account.casefold()
    ]


def post_reply(session: Any, snapshot: ThreadSnapshot, message: str, config: Config) -> None:
    if not snapshot.form_action:
        raise SafetyError("找不到回覆端點，未送出內容。")
    fields = dict(snapshot.form_fields)
    fields["rtecontent"] = message
    try:
        response = session.post(
            snapshot.form_action,
            data=fields,
            headers={"Referer": snapshot.url, "Origin": "https://forum.gamer.com.tw"},
            timeout=config.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        raise SafetyError(f"送出回覆時發生網路錯誤：{exc}；將重新讀頁確認，不會刪舊文。") from exc
    ensure_no_challenge(response.text, response.url)
    mark_cookie_updates(session, response)


def parse_delete_request(snapshot: ThreadSnapshot, post: Post) -> DeleteRequest:
    match = PDEL_PATTERN.search(snapshot.html_text)
    if not match:
        raise SafetyError("找不到刪文 token；網站腳本可能已改版。")
    raw_query = html.unescape(f"{match.group(1)}{post.post_id}{match.group(2)}")
    fields = dict(parse_qsl(raw_query, keep_blank_values=True))
    expected_bsn, expected_sna = thread_identity(snapshot.url)
    required = {"bsn", "sn", "type", "code", "pwd", "snA"}
    if not required.issubset(fields):
        raise SafetyError("刪文參數不完整，拒絕刪除。")
    if (
        fields["bsn"] != expected_bsn
        or fields["snA"] != expected_sna
        or fields["sn"] != post.post_id
        or fields["type"] != "4"
        or not fields["code"]
        or not fields["pwd"]
        or not match.group(3)
    ):
        raise SafetyError("刪文參數與目標樓層不一致，拒絕刪除。")
    delete_url = urljoin(snapshot.url, f"post2.php?{urlencode(fields)}")
    return DeleteRequest(delete_url, match.group(3), fields)


def delete_post(
    session: Any, snapshot: ThreadSnapshot, post: Post, config: Config
) -> None:
    if post.floor <= 1 or post.author.casefold() != config.account.casefold():
        raise SafetyError("拒絕刪除：目標不是自己的回覆樓層。")
    current = next((item for item in snapshot.posts if item.post_id == post.post_id), None)
    if current != post:
        raise SafetyError("拒絕刪除：重新讀頁後，目標日期、作者或內容已不一致。")
    delete_request = parse_delete_request(snapshot, post)
    session.cookies.set(
        "ckFORUM_pdel", delete_request.cookie_value, domain=".gamer.com.tw", path="/"
    )
    try:
        response = session.get(
            delete_request.url,
            headers={"Referer": snapshot.url},
            timeout=config.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        raise SafetyError(f"刪文請求失敗：{exc}；請人工確認網站狀態。") from exc
    ensure_no_challenge(response.text, response.url)
    mark_cookie_updates(session, response)


def classify_daily_posts(
    snapshot: ThreadSnapshot, config: Config, now: datetime
) -> tuple[list[Post], list[Post]]:
    posts = own_posts(snapshot, config.account)
    today_posts = [post for post in posts if post.posted_at.date() == now.date()]
    previous_posts = [
        post
        for post in posts
        if post.posted_at.date() < now.date()
        and post.content.strip() in config.deletable_messages
    ]
    if len(today_posts) > 1:
        raise SafetyError("偵測到今天已有多則自己的回覆；停止並請人工檢查。")
    if len(previous_posts) > 1:
        raise SafetyError("偵測到多則過去的自推回覆；為避免刪錯，停止並請人工檢查。")
    return today_posts, previous_posts


def check_guard_threads(session: Any, config: Config, today: date) -> None:
    for url in config.guard_urls:
        snapshot = fetch_snapshot(session, url, config)
        assert_owner_login(snapshot, config)
        persist_session_cookies(session, config)
        guarded_today = [
            post
            for post in own_posts(snapshot, config.account)
            if post.posted_at.date() == today
        ]
        if guarded_today:
            raise SafetyError(
                f"同帳號今天已在另一篇受監控文章回覆：{url}；依板規不再自推。"
            )


def verify_new_post(
    snapshot: ThreadSnapshot,
    config: Config,
    before_ids: set[str],
    today: date,
    expected_message: str,
) -> Post:
    candidates = [
        post
        for post in own_posts(snapshot, config.account)
        if post.posted_at.date() == today
        and post.post_id not in before_ids
        and post.content.strip() == expected_message.strip()
    ]
    if len(candidates) != 1:
        raise SafetyError("無法唯一驗證新回覆；保留所有舊內容並停止。")
    return candidates[0]


def run_check(config: Config) -> dict[str, Any]:
    refresh_login_for_production(config)
    session = build_session(config)
    snapshot = fetch_snapshot(session, config.target_url, config)
    assert_owner_login(snapshot, config)
    persist_session_cookies(session, config)
    notify_cookie_expiry(config)
    now = datetime.now(config.timezone)
    today_posts, previous_posts = classify_daily_posts(snapshot, config, now)
    yesterday = now.date() - timedelta(days=1)
    return {
        "ok": True,
        "checked_at": now.isoformat(),
        "target": config.target_url,
        "account": config.account,
        "owner_verified": snapshot.owner_verified,
        "site_reports_login": snapshot.site_reports_login,
        "subboard": snapshot.subboard,
        "parsed_posts": len(snapshot.posts),
        "today_own_replies": [asdict_post(post) for post in today_posts],
        "previous_own_bump_replies": [
            asdict_post(post) for post in previous_posts
        ],
        "yesterday_own_replies": [
            asdict_post(post)
            for post in previous_posts
            if post.posted_at.date() == yesterday
        ],
        "reply_form": bool(snapshot.form_action),
        "delete_token": bool(PDEL_PATTERN.search(snapshot.html_text)),
        "guard_urls": list(config.guard_urls),
    }


def run_once(config: Config) -> dict[str, Any]:
    refresh_login_for_production(config)
    session = build_session(config)
    snapshot = fetch_snapshot(session, config.target_url, config)
    assert_owner_login(snapshot, config)
    persist_session_cookies(session, config)
    notify_cookie_expiry(config)
    now = datetime.now(config.timezone)
    today_posts, previous_posts = classify_daily_posts(snapshot, config, now)
    old_post = previous_posts[0] if previous_posts else None
    old_post_page = snapshot.page_number if old_post is not None else None
    if old_post is not None and old_post_page is None:
        raise SafetyError("無法辨識昨天回覆所在頁碼；為避免留下重複推文，停止操作。")

    if not today_posts:
        posted_new = True
        check_guard_threads(session, config, now.date())
        before_ids = {post.post_id for post in snapshot.posts}
        LOG.info("今日尚未自推，準備發布：%r", config.message)
        post_reply(session, snapshot, config.message, config)
        verified = fetch_snapshot(session, config.target_url, config)
        assert_owner_login(verified, config)
        persist_session_cookies(session, config)
        new_post = verify_new_post(
            verified, config, before_ids, datetime.now(config.timezone).date(), config.message
        )
        LOG.info("發布並驗證成功：%d 樓（%s）", new_post.floor, new_post.post_id)
    else:
        posted_new = False
        new_post = today_posts[0]
        LOG.info("今天已有自己的回覆（%d 樓），不重複發布。", new_post.floor)

    if old_post is not None:
        latest = fetch_snapshot(session, config.target_url, config)
        assert_owner_login(latest, config)
        persist_session_cookies(session, config)
        fresh_today, _ = classify_daily_posts(latest, config, datetime.now(config.timezone))
        if len(fresh_today) != 1:
            raise SafetyError("刪文前無法確認今天恰有一則自己的回覆；不刪舊文。")
        delete_source = latest
        if not any(post.post_id == old_post.post_id for post in latest.posts):
            delete_source = fetch_page_snapshot(
                session, config.target_url, old_post_page, config
            )
            assert_owner_login(delete_source, config)
            persist_session_cookies(session, config)
        LOG.info("準備刪除昨天的 %d 樓（%s）。", old_post.floor, old_post.post_id)
        delete_post(session, delete_source, old_post, config)
        after_delete = fetch_page_snapshot(
            session,
            config.target_url,
            old_post_page,
            config,
            allow_empty=True,
        )
        assert_owner_login(after_delete, config)
        persist_session_cookies(session, config)
        if any(post.post_id == old_post.post_id for post in after_delete.posts):
            raise SafetyError("刪文後舊樓層仍存在；請人工檢查。")
        LOG.info("已驗證昨天的 %d 樓刪除成功。", old_post.floor)
    else:
        LOG.info("找不到今天以前、且文字符合刪除清單的自推回覆，無需刪除。")

    result = {
        "ok": True,
        "completed_at": datetime.now(config.timezone).isoformat(),
        "posted_new": posted_new,
        "new_post": asdict_post(new_post),
        "deleted_post": asdict_post(old_post) if old_post else None,
    }
    write_status(config, result)
    notify_daily_success(config, result)
    return result


def live_round_trip_test(config: Config, test_url: str) -> dict[str, Any]:
    test_url = validate_forum_url(test_url)
    if thread_identity(test_url) == thread_identity(config.target_url):
        raise SafetyError("即時測試禁止使用正式自推文章。")
    refresh_login_for_production(config)
    session = build_session(config)
    snapshot = fetch_snapshot(session, test_url, config)
    assert_owner_login(snapshot, config)
    persist_session_cookies(session, config)
    if snapshot.subboard is None:
        raise SafetyError("無法辨識測試文章分類；拒絕發文。")
    if snapshot.subboard == "18":
        raise SafetyError("即時測試禁止在「伺服招生」（subbsn=18）分類執行。")

    unique = datetime.now(config.timezone).strftime("%Y%m%d-%H%M%S")
    message = f"自動化連線測試 {unique}，驗證後將立即刪除。"
    before_ids = {post.post_id for post in snapshot.posts}
    LOG.warning("即時測試將公開回覆：%s", message)
    post_reply(session, snapshot, message, config)
    verified = fetch_snapshot(session, test_url, config)
    assert_owner_login(verified, config)
    persist_session_cookies(session, config)
    test_post = verify_new_post(
        verified, config, before_ids, datetime.now(config.timezone).date(), message
    )
    fresh = fetch_snapshot(session, test_url, config)
    assert_owner_login(fresh, config)
    persist_session_cookies(session, config)
    delete_post(session, fresh, test_post, config)
    after = fetch_snapshot(session, test_url, config)
    assert_owner_login(after, config)
    persist_session_cookies(session, config)
    if any(post.post_id == test_post.post_id for post in after.posts):
        raise SafetyError("測試回覆未能驗證刪除；請立即人工處理。")
    result = {
        "ok": True,
        "tested_at": datetime.now(config.timezone).isoformat(),
        "test_url": test_url,
        "message": message,
        "post_id": test_post.post_id,
        "deleted": True,
    }
    write_status(config, result)
    send_discord_notification(
        config,
        f"✅ 巴哈非伺服招生文章的發文／刪文往返測試成功。\n{test_url}",
        dedupe_key=f"live-test-{test_post.post_id}",
    )
    return result


def asdict_post(post: Post | None) -> dict[str, Any] | None:
    if post is None:
        return None
    return {
        "post_id": post.post_id,
        "floor": post.floor,
        "author": post.author,
        "posted_at": post.posted_at.isoformat(),
        "content": post.content,
    }


def _discord_webhook_url(config: Config) -> str | None:
    value = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not value and config.discord_webhook_file.is_file():
        value = config.discord_webhook_file.read_text(encoding="utf-8-sig").strip()
    if not value:
        return None
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.hostname != "discord.com"
        or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[^/]+", parts.path)
    ):
        raise ConfigurationError("Discord Webhook URL 格式無效。")
    return value


def _notification_state(config: Config) -> set[str]:
    path = config.data_dir / "notification_state.json"
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(item) for item in payload.get("sent", [])}
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def _save_notification_state(config: Config, sent: set[str]) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    path = config.data_dir / "notification_state.json"
    temporary = config.data_dir / "notification_state.json.tmp"
    try:
        temporary.write_text(
            json.dumps({"sent": sorted(sent)[-200:]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        LOG.warning("無法保存 Discord 通知去重狀態：%s", exc)


def send_discord_notification(
    config: Config,
    message: str,
    *,
    attention: bool = False,
    dedupe_key: str | None = None,
) -> bool:
    """Send a Discord webhook without ever logging its secret URL."""
    try:
        webhook_url = _discord_webhook_url(config)
    except (OSError, ConfigurationError) as exc:
        LOG.warning("Discord Webhook 設定無法使用：%s", exc)
        return False
    if webhook_url is None:
        return False
    sent = _notification_state(config)
    if dedupe_key and dedupe_key in sent:
        return True
    if attention:
        content = f"<@{config.discord_user_id}> {message}"
        allowed_mentions = {"users": [config.discord_user_id]}
    else:
        content = message
        allowed_mentions = {"parse": []}
    requests, _ = import_http_dependencies()
    try:
        response = requests.post(
            webhook_url,
            params={"wait": "true"},
            json={
                "content": content[:2000],
                "allowed_mentions": allowed_mentions,
                "username": "Bahamut Bumper",
            },
            timeout=config.timeout_seconds,
        )
        if response.status_code not in {200, 204}:
            LOG.warning("Discord 通知失敗（HTTP %d）。", response.status_code)
            return False
    except Exception as exc:
        LOG.warning("Discord 通知連線失敗（%s）。", type(exc).__name__)
        return False
    if dedupe_key:
        sent.add(dedupe_key)
        _save_notification_state(config, sent)
    return True


def auth_cookie_expiry(config: Config) -> datetime | None:
    critical_names = {
        "BAHAENUR",
        "BAHAHASHID",
        "BAHAID",
        "BAHARUNE",
        "MB_BAHAID",
        "MB_BAHARUNE",
    }
    expirations: list[float] = []
    for record in load_cookie_records(config.cookie_file):
        if str(record.get("name", "")) not in critical_names:
            continue
        raw = record.get("expirationDate", record.get("expires"))
        try:
            if raw not in (None, "", 0, -1):
                expirations.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not expirations:
        return None
    return datetime.fromtimestamp(min(expirations), config.timezone)


def notify_cookie_expiry(config: Config) -> None:
    try:
        expiry = auth_cookie_expiry(config)
    except (ConfigurationError, AuthenticationError):
        return
    if expiry is None:
        return
    now = datetime.now(config.timezone)
    remaining = expiry - now
    if remaining.total_seconds() > 0:
        return
    expiry_text = expiry.strftime("%Y-%m-%d %H:%M:%S %Z")
    expiry_key = str(int(expiry.timestamp()))
    message = (
        f"⚠️ 巴哈主要登入 Cookie 的標示期限已過（{expiry_text}），"
        "而正常讀頁後仍未收到續期 Cookie。請重新登入並匯出 data/cookies.json。"
    )
    send_discord_notification(
        config,
        message,
        attention=True,
        dedupe_key=f"cookie-expired-{expiry_key}",
    )


def notify_daily_success(config: Config, result: dict[str, Any]) -> None:
    date_key = datetime.now(config.timezone).date().isoformat()
    new_post = result.get("new_post") or {}
    deleted = result.get("deleted_post")
    action = "已發布新回覆" if result.get("posted_new") else "今日回覆已存在，未重複發布"
    cleanup = (
        f"；已刪除昨日 {deleted.get('floor')} 樓"
        if isinstance(deleted, dict)
        else "；昨日無待刪推文"
    )
    send_discord_notification(
        config,
        f"✅ 巴哈每日流程完成：{action}（{new_post.get('floor', '?')} 樓）{cleanup}\n{config.target_url}",
        dedupe_key=f"daily-success-{date_key}",
    )


def notify_failure(config: Config, exc: Exception, *, scope: str) -> None:
    date_key = datetime.now(config.timezone).date().isoformat()
    detail = str(exc).replace("`", "'")[:800]
    send_discord_notification(
        config,
        f"❌ 巴哈自推需要處理：{scope}失敗（{type(exc).__name__}）\n{detail}\n{config.target_url}",
        attention=True,
        dedupe_key=f"failure-{scope}-{date_key}-{type(exc).__name__}",
    )


def write_status(config: Config, payload: dict[str, Any]) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    destination = config.data_dir / "status.json"
    temporary = config.data_dir / "status.json.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(destination)


def print_status(config: Config) -> None:
    path = config.data_dir / "status.json"
    if not path.is_file():
        print("尚無執行紀錄。")
        return
    print(path.read_text(encoding="utf-8"))


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


def _console_reader(commands: queue.Queue[str]) -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        command = line.strip()
        if command:
            commands.put(command)


def console_help() -> str:
    return (
        "可用 Console 指令：\n"
        "  check                  唯讀檢查登入、文章與 token\n"
        "  once                   立即執行正式發文／刪文流程一次\n"
        "  status                 顯示最近一次執行結果\n"
        "  next                   顯示下次排程時間\n"
        "  test-notification      發送不 @ 使用者的 Discord 測試通知\n"
        "  login-test             只測試 mobile API 登入並更新 Cookie\n"
        "  live-test <URL> CONFIRM 在自己的非伺服招生文章測試發文後刪除\n"
        "  help                   顯示本說明\n"
        "  stop                   安全停止程式"
    )


def execute_console_command(line: str, config: Config) -> bool:
    """Execute one stdin command. Return True when the daemon should stop."""
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        LOG.error("Console 指令格式錯誤：%s", exc)
        return False
    if not parts:
        return False
    command = parts[0].casefold()
    try:
        if command == "help":
            print(console_help(), flush=True)
        elif command == "check":
            print(json.dumps(run_check(config), ensure_ascii=False, indent=2), flush=True)
        elif command == "once":
            print(json.dumps(run_once(config), ensure_ascii=False, indent=2), flush=True)
        elif command == "status":
            print_status(config)
        elif command == "next":
            print(
                f"下次排程：{next_scheduled_at(config, datetime.now(config.timezone)).isoformat()}",
                flush=True,
            )
        elif command == "test-notification":
            if _discord_webhook_url(config) is None:
                raise ConfigurationError("尚未設定 Discord Webhook。")
            if not send_discord_notification(
                config,
                "✅ Bahamut Bumper Discord Webhook 測試成功；一般成功通知不會標註你。",
            ):
                raise SafetyError("Discord 測試通知發送失敗。")
            print("Discord 測試通知已送出。", flush=True)
        elif command == "login-test":
            print(
                json.dumps(
                    mobile_login(
                        config, enforce_rate_limit=True, notify_success=True
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
        elif command == "live-test":
            if len(parts) != 3 or parts[2] != "CONFIRM":
                raise ConfigurationError("用法：live-test <URL> CONFIRM")
            print(
                json.dumps(
                    live_round_trip_test(config, parts[1]), ensure_ascii=False, indent=2
                ),
                flush=True,
            )
        elif command in {"stop", "quit", "exit"}:
            LOG.info("收到 Console stop 指令，將安全停止。")
            return True
        else:
            LOG.error("未知 Console 指令：%s。輸入 help 查看可用指令。", parts[0])
    except (ConfigurationError, AuthenticationError, SafetyError, ValueError) as exc:
        if command in {"once", "live-test"}:
            notify_failure(config, exc, scope="Console 手動執行")
        LOG.error("Console 指令 %s 失敗：%s", command, exc)
    return False


def wait_with_console(
    stop: list[bool], commands: queue.Queue[str], seconds: float, config: Config
) -> None:
    deadline = time.monotonic() + seconds
    while not stop[0] and time.monotonic() < deadline:
        timeout = min(1.0, max(0.0, deadline - time.monotonic()))
        try:
            command = commands.get(timeout=timeout)
        except queue.Empty:
            continue
        if execute_console_command(command, config):
            stop[0] = True


def run_daemon(config: Config) -> None:
    stop = [False]
    commands: queue.Queue[str] = queue.Queue()

    def request_stop(signum: int, _frame: Any) -> None:
        LOG.info("收到訊號 %s，將安全停止。", signum)
        stop[0] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    threading.Thread(
        target=_console_reader,
        args=(commands,),
        name="pterodactyl-console",
        daemon=True,
    ).start()
    LOG.info("Console 互動指令已啟用；輸入 help 查看說明。")
    now = datetime.now(config.timezone)
    run_now = config.run_missed_on_start and now.time() >= config.bump_at
    while not stop[0]:
        if not run_now:
            target = next_scheduled_at(config, datetime.now(config.timezone))
            LOG.info("下次執行：%s", target.isoformat())
            wait_with_console(
                stop,
                commands,
                seconds_until(target, datetime.now(config.timezone)),
                config,
            )
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
                failure = {
                    "ok": False,
                    "failed_at": datetime.now(config.timezone).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "attempt": attempt,
                }
                write_status(config, failure)
                LOG.exception("每日流程失敗：%s", exc)
                if isinstance(exc, AuthenticationError):
                    notify_failure(config, exc, scope="每日流程")
                final_failure = (
                    attempt >= config.max_retries
                    or datetime.now(config.timezone).date() != run_date
                )
                if final_failure:
                    notify_failure(config, exc, scope="每日流程")
                    break
                wait_with_console(stop, commands, config.retry_minutes * 60, config)
                if stop[0]:
                    break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="巴哈每日自推（Generic Python Egg）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="立即執行正式流程一次")
    mode.add_argument("--check", action="store_true", help="只讀檢查登入、文章與 token")
    mode.add_argument("--status", action="store_true", help="顯示最近一次執行紀錄")
    mode.add_argument("--test-notification", action="store_true", help="發送一則不標註使用者的 Discord 測試通知")
    mode.add_argument("--live-test", metavar="URL", help="在自己的非伺服招生文章測試發文後刪除")
    parser.add_argument(
        "--confirm-live-test",
        action="store_true",
        help="確認接受測試回覆會短暫公開（搭配 --live-test）",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args()
    config: Config | None = None
    try:
        config = Config.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        if args.status:
            print_status(config)
            return 0
        with process_lock(config.data_dir / "daemon.lock"):
            if args.check:
                print(json.dumps(run_check(config), ensure_ascii=False, indent=2))
            elif args.test_notification:
                if _discord_webhook_url(config) is None:
                    raise ConfigurationError("尚未設定 Discord Webhook。")
                if not send_discord_notification(
                    config,
                    "✅ Bahamut Bumper Discord Webhook 測試成功；一般成功通知不會標註你。",
                ):
                    raise SafetyError("Discord 測試通知發送失敗。")
                print("Discord 測試通知已送出。")
            elif args.live_test:
                if not args.confirm_live_test:
                    raise ConfigurationError(
                        "即時測試會公開發文；確認網址無誤後加上 --confirm-live-test。"
                    )
                print(
                    json.dumps(
                        live_round_trip_test(config, args.live_test),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.once:
                print(json.dumps(run_once(config), ensure_ascii=False, indent=2))
            else:
                run_daemon(config)
        return 0
    except (ConfigurationError, AuthenticationError, SafetyError, ValueError) as exc:
        if config is not None and (args.once or args.live_test):
            notify_failure(config, exc, scope="手動執行")
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
