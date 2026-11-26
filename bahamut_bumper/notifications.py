"""Discord alerts and their per-account deduplication."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .auth import load_cookie_records
from .config import AccountConfig
from .errors import AuthenticationError, ConfigurationError, SafetyError
from .forum import import_http_dependencies
from .state import write_json


LOG = logging.getLogger("bahamut-bumper")


def _discord_webhook_url(config: AccountConfig) -> str | None:
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


def _notification_state(config: AccountConfig) -> set[str]:
    path = config.data_dir / "notification_state.json"
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(item) for item in payload.get("sent", [])}
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def _save_notification_state(config: AccountConfig, sent: set[str]) -> None:
    try:
        write_json(config.data_dir / "notification_state.json", {"sent": sorted(sent)[-200:]})
    except OSError as exc:
        LOG.warning("無法保存 Discord 通知去重狀態：%s", exc)


def send_discord_notification(
    config: AccountConfig,
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


def auth_cookie_expiry(config: AccountConfig) -> datetime | None:
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


def notify_cookie_expiry(config: AccountConfig) -> None:
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
        f"而正常讀頁後仍未收到續期 Cookie。可重新登入後匯出至 {config.cookie_file}。"
    )
    send_discord_notification(
        config,
        message,
        attention=True,
        dedupe_key=f"cookie-expired-{expiry_key}",
    )


def notify_daily_success(config: AccountConfig, result: dict[str, Any]) -> None:
    date_key = datetime.now(config.timezone).date().isoformat()
    new_post = result.get("new_post") or {}
    deleted = result.get("deleted_post")
    action = "已發布新回覆" if result.get("posted_new") else "今日回覆已存在，未重複發布"
    cleanup = (
        f"；已刪除舊回覆 {deleted.get('floor')} 樓"
        if isinstance(deleted, dict)
        else "；無待刪除的舊回覆"
    )
    send_discord_notification(
        config,
        f"✅ 巴哈每日流程完成：{action}（{new_post.get('floor', '?')} 樓）{cleanup}\n{config.target_url}",
        dedupe_key=f"daily-success-{date_key}",
    )


def notify_failure(config: AccountConfig, exc: Exception, *, scope: str) -> None:
    date_key = datetime.now(config.timezone).date().isoformat()
    detail = str(exc).replace("`", "'")[:800]
    send_discord_notification(
        config,
        f"❌ 巴哈自推需要處理：{scope}失敗（{type(exc).__name__}）\n{detail}\n{config.target_url}",
        attention=True,
        dedupe_key=f"failure-{scope}-{date_key}-{type(exc).__name__}",
    )


def notify_login_test(config: AccountConfig) -> None:
    send_discord_notification(
        config,
        "✅ 巴哈 mobile API 帳密登入測試成功，並已驗證論壇帳號；沒有發文或刪文。",
        dedupe_key=f"mobile-login-test-{datetime.now(config.timezone).date().isoformat()}",
    )


def test_notification(config: AccountConfig) -> None:
    if _discord_webhook_url(config) is None:
        raise ConfigurationError("尚未設定 Discord Webhook。")
    if not send_discord_notification(
        config, "✅ Bahamut Bumper Discord Webhook 測試成功；一般成功通知不會標註你。"
    ):
        raise SafetyError("Discord 測試通知發送失敗。")
