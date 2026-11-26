"""Read and update each account's own thread."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from .auth import authenticated_forum_snapshot, verified_snapshot
from .config import AccountConfig, AppConfig, select_account
from .errors import SafetyError
from .forum import (
    PDEL_PATTERN,
    Post,
    ThreadSnapshot,
    delete_post,
    own_posts,
    post_reply,
    thread_identity,
    validate_forum_url,
)
from .notifications import notify_cookie_expiry, send_discord_notification
from .state import write_status


LOG = logging.getLogger("bahamut-bumper")


def classify_daily_posts(
    snapshot: ThreadSnapshot, config: AccountConfig, now: datetime
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


def check_guard_threads(session: Any, config: AccountConfig, today: date) -> None:
    for url in config.guard_urls:
        snapshot = verified_snapshot(session, url, config)
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
    config: AccountConfig,
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


def check_account(config: AccountConfig) -> dict[str, Any]:
    _, snapshot = authenticated_forum_snapshot(config, config.target_url)
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


def bump_account(config: AccountConfig) -> dict[str, Any]:
    session, snapshot = authenticated_forum_snapshot(config, config.target_url)
    notify_cookie_expiry(config)
    now = datetime.now(config.timezone)
    today_posts, previous_posts = classify_daily_posts(snapshot, config, now)
    old_post = previous_posts[0] if previous_posts else None
    old_post_page = snapshot.page_number if old_post is not None else None
    if old_post is not None and old_post_page is None:
        raise SafetyError("無法辨識舊回覆所在頁碼；為避免留下重複推文，停止操作。")

    if not today_posts:
        posted_new = True
        check_guard_threads(session, config, now.date())
        before_ids = {post.post_id for post in snapshot.posts}
        LOG.info("今日尚未自推，準備發布：%r", config.message)
        post_reply(session, snapshot, config.message, config)
        verified = verified_snapshot(session, config.target_url, config)
        new_post = verify_new_post(
            verified, config, before_ids, datetime.now(config.timezone).date(), config.message
        )
        LOG.info("發布並驗證成功：%d 樓（%s）", new_post.floor, new_post.post_id)
    else:
        posted_new = False
        new_post = today_posts[0]
        LOG.info("今天已有自己的回覆（%d 樓），不重複發布。", new_post.floor)

    if old_post is not None:
        latest = verified_snapshot(session, config.target_url, config)
        fresh_today, _ = classify_daily_posts(latest, config, datetime.now(config.timezone))
        if len(fresh_today) != 1:
            raise SafetyError("刪文前無法確認今天恰有一則自己的回覆；不刪舊文。")
        delete_source = latest
        if not any(post.post_id == old_post.post_id for post in latest.posts):
            delete_source = verified_snapshot(
                session, config.target_url, config, page=old_post_page
            )
        LOG.info("準備刪除舊回覆 %d 樓（%s）。", old_post.floor, old_post.post_id)
        delete_post(session, delete_source, old_post, config)
        after_delete = verified_snapshot(
            session,
            config.target_url,
            config,
            page=old_post_page,
            allow_empty=True,
        )
        if any(post.post_id == old_post.post_id for post in after_delete.posts):
            raise SafetyError("刪文後舊樓層仍存在；請人工檢查。")
        LOG.info("已驗證舊回覆 %d 樓刪除成功。", old_post.floor)
    else:
        LOG.info("找不到今天以前、且文字符合刪除清單的自推回覆，無需刪除。")

    return {
        "ok": True,
        "completed_at": datetime.now(config.timezone).isoformat(),
        "posted_new": posted_new,
        "new_post": asdict_post(new_post),
        "deleted_post": asdict_post(old_post),
    }


def live_round_trip_test(
    app: AppConfig, test_url: str, *, account: str | None = None
) -> dict[str, Any]:
    test_url = validate_forum_url(test_url)
    if any(
        thread_identity(test_url) == thread_identity(profile.target_url)
        for profile in app.accounts
    ):
        raise SafetyError("即時測試禁止使用正式自推文章。")
    config = select_account(app, account)
    session, snapshot = authenticated_forum_snapshot(config, test_url)
    if snapshot.subboard is None:
        raise SafetyError("無法辨識測試文章分類；拒絕發文。")
    if snapshot.subboard == "18":
        raise SafetyError("即時測試禁止在「伺服招生」（subbsn=18）分類執行。")

    unique = datetime.now(config.timezone).strftime("%Y%m%d-%H%M%S")
    message = f"自動化連線測試 {unique}，驗證後將立即刪除。"
    before_ids = {post.post_id for post in snapshot.posts}
    LOG.warning("即時測試將公開回覆：%s", message)
    post_reply(session, snapshot, message, config)
    verified = verified_snapshot(session, test_url, config)
    test_post = verify_new_post(
        verified, config, before_ids, datetime.now(config.timezone).date(), message
    )
    fresh = verified_snapshot(session, test_url, config)
    delete_post(session, fresh, test_post, config)
    after = verified_snapshot(session, test_url, config)
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
    write_status(config.data_dir, result)
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
