"""Bahamut forum requests, page parsing, and verified post targets."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .errors import AuthenticationError, ConfigurationError, SafetyError

if TYPE_CHECKING:
    from .config import AccountConfig


FORUM_HOST = "forum.gamer.com.tw"


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


def validate_forum_url(url: str) -> str:
    parts = urlsplit(url)
    identifiers = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key in {"bsn", "snA"}
    ]
    query = dict(identifiers)
    if (
        parts.scheme != "https"
        or parts.hostname != FORUM_HOST
        or not parts.path.endswith("/C.php")
        or len(identifiers) != 2
        or not re.fullmatch(r"[0-9]+", query.get("bsn", ""))
        or not re.fullmatch(r"[0-9]+", query.get("snA", ""))
    ):
        raise ConfigurationError(
            "文章網址必須是 https://forum.gamer.com.tw/C.php 且包含數字 bsn、snA。"
        )
    clean_query = {key: query[key].lstrip("0") or "0" for key in ("bsn", "snA")}
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


def import_http_dependencies() -> tuple[Any, Any]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ConfigurationError(
            "缺少套件；請先執行 python -m pip install --user -r requirements.txt"
        ) from exc
    return requests, BeautifulSoup


def mark_cookie_updates(session: Any, response: Any) -> None:
    responses = [*getattr(response, "history", []), response]
    if any(len(item.cookies) > 0 for item in responses):
        setattr(session, "_bahamut_cookie_dirty", True)


def request_page(session: Any, url: str, config: AccountConfig) -> tuple[str, str]:
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


def _assert_thread_url(thread_url: str, final_url: str) -> None:
    try:
        actual_url = validate_forum_url(final_url)
    except (ConfigurationError, ValueError):
        actual_url = None
    if actual_url != validate_forum_url(thread_url):
        raise SafetyError("巴哈回傳的頁面網址與目標文章不符，停止此篇操作。")


def _fetch_snapshot_at(
    session: Any,
    thread_url: str,
    request_url: str,
    config: AccountConfig,
    *,
    allow_empty: bool = False,
) -> ThreadSnapshot:
    page_text, final_url = request_page(session, request_url, config)
    _assert_thread_url(thread_url, final_url)
    snapshot = parse_thread_snapshot(page_text, final_url, config.account, datetime.now(config.timezone))
    if not snapshot.posts and not allow_empty:
        raise SafetyError("頁面中找不到任何可解析樓層；可能是頁面結構已變更。")
    if not any(post.floor == 1 for post in snapshot.posts):
        # `last=1` opens the final page. Once a thread has more than one page,
        # that page no longer contains the OP metadata used to prove that the
        # authenticated account owns the thread. Fetch page 1 only for that
        # proof while preserving the final-page posts and reply form.
        first_text, first_url = request_page(session, thread_url, config)
        _assert_thread_url(thread_url, first_url)
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


def fetch_snapshot(session: Any, url: str, config: AccountConfig) -> ThreadSnapshot:
    return _fetch_snapshot_at(session, url, with_last_page(url), config)


def fetch_page_snapshot(
    session: Any,
    url: str,
    page: int,
    config: AccountConfig,
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


def assert_owner_login(snapshot: ThreadSnapshot, config: AccountConfig) -> None:
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


def own_posts(snapshot: ThreadSnapshot, account: str) -> list[Post]:
    return [
        post
        for post in snapshot.posts
        if post.floor > 1 and post.author.casefold() == account.casefold()
    ]


def post_reply(session: Any, snapshot: ThreadSnapshot, message: str, config: AccountConfig) -> None:
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
    session: Any, snapshot: ThreadSnapshot, post: Post, config: AccountConfig
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
