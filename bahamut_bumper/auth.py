"""Password login and persistent forum sessions."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from .config import AccountConfig
from .errors import AuthenticationError, ConfigurationError, MobileLoginUnavailable, SafetyError
from .forum import (
    ThreadSnapshot,
    assert_owner_login,
    fetch_page_snapshot,
    fetch_snapshot,
    import_http_dependencies,
)
from .state import write_json


LOG = logging.getLogger("bahamut-bumper")


MOBILE_LOGIN_URL = "https://api.gamer.com.tw/mobile_app/user/v3/do_login.php"


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


def build_session(config: AccountConfig) -> Any:
    session = new_forum_session()
    add_cookie_records(session, load_cookie_records(config.cookie_file))
    if not session.cookies:
        raise AuthenticationError("Cookie 檔沒有任何有效 Cookie。")
    return session


def persist_session_cookies(session: Any, config: AccountConfig) -> None:
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
    try:
        backup = config.cookie_file.with_suffix(config.cookie_file.suffix + ".backup")
        if config.cookie_file.is_file() and not backup.exists():
            backup.write_bytes(config.cookie_file.read_bytes())
            if os.name != "nt":
                backup.chmod(0o600)
        write_json(config.cookie_file, records, private=True)
        setattr(session, "_bahamut_cookie_dirty", False)
    except OSError as exc:
        LOG.warning("無法保存網站更新後的 Cookie：%s", exc)


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


def record_mobile_login_attempt(config: AccountConfig) -> None:
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
    write_json(
        path,
        {
            "attempted_at": datetime.now(config.timezone).isoformat(),
            "attempted_at_epoch": now_epoch,
        },
    )


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


def _password_login(
    config: AccountConfig, *, enforce_rate_limit: bool = False
) -> tuple[Any, ThreadSnapshot, dict[str, Any]]:
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
    except requests.RequestException as exc:
        raise MobileLoginUnavailable(
            f"mobile API 登入連線失敗（{type(exc).__name__}）。"
        ) from exc
    if response.status_code >= 400:
        detail = f"mobile API 登入請求失敗（HTTP {response.status_code}）。"
        if response.status_code == 403:
            if response.headers.get("cf-mitigated", "").casefold() == "challenge":
                detail += "Cloudflare 要求人機驗證；腳本不會繞過。"
            else:
                detail += (
                    "登入端點拒絕存取，可能是主機出口 IP／存取限制或額外驗證；"
                    "不能據此判定密碼錯誤。"
                )
        elif response.status_code == 429:
            detail += "登入端點限制請求頻率，停止本日自動重試。"
        ray_id = response.headers.get("cf-ray", "")
        if re.fullmatch(r"[A-Za-z0-9-]{1,100}", ray_id):
            detail += f" Cloudflare Ray ID：{ray_id}。"
        raise MobileLoginUnavailable(
            detail,
            retryable=response.status_code in {408, 425} or response.status_code >= 500,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        lower = response.text.casefold()
        if "recaptcha" in lower or "captcha" in lower or "驗證" in response.text:
            raise AuthenticationError(
                "mobile API 要求額外的人機／新裝置驗證；腳本不會繞過。",
                retryable=False,
            ) from exc
        raise AuthenticationError("mobile API 未回傳 JSON 登入結果。") from exc

    userid = str(payload.get("userid", "")) if isinstance(payload, dict) else ""
    rune = next(
        (cookie.value for cookie in mobile.cookies if cookie.name == "BAHARUNE"), None
    )
    if userid.casefold() != config.account.casefold() or not rune:
        raise AuthenticationError(
            f"mobile API 登入失敗：{_mobile_error_message(payload)}", retryable=False
        )

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
    snapshot = verified_snapshot(forum, config.target_url, config)
    result = {
        "ok": True,
        "tested_at": datetime.now(config.timezone).isoformat(),
        "api_account": userid,
        "forum_owner_verified": snapshot.owner_verified,
        "received_cookie_names": sorted(set(received_names)),
        "cookie_saved": not forum._bahamut_cookie_dirty,
        "posted": False,
        "deleted": False,
    }
    return forum, snapshot, result


def mobile_login(config: AccountConfig, *, enforce_rate_limit: bool = False) -> dict[str, Any]:
    _, _, result = _password_login(config, enforce_rate_limit=enforce_rate_limit)
    return result


def verified_snapshot(
    session: Any,
    url: str,
    config: AccountConfig,
    *,
    page: int | None = None,
    allow_empty: bool = False,
) -> ThreadSnapshot:
    if page is None:
        snapshot = fetch_snapshot(session, url, config)
    else:
        snapshot = fetch_page_snapshot(session, url, page, config, allow_empty=allow_empty)
    assert_owner_login(snapshot, config)
    persist_session_cookies(session, config)
    return snapshot


def authenticated_forum_snapshot(
    config: AccountConfig, target_url: str
) -> tuple[Any, ThreadSnapshot]:
    """Use cached cookies on API outages only after proving forum ownership."""
    mobile_error: MobileLoginUnavailable | None = None
    if config.auto_mobile_login:
        LOG.info("使用 mobile API 登入帳號 %s。", config.account)
        try:
            session, snapshot, _ = _password_login(config)
        except MobileLoginUnavailable as exc:
            mobile_error = exc
            LOG.warning("%s 將唯讀驗證現有 Cookie 是否仍可登入論壇。", exc)
        else:
            if target_url != config.target_url:
                snapshot = verified_snapshot(session, target_url, config)
            return session, snapshot

    try:
        session = build_session(config)
        snapshot = verified_snapshot(session, target_url, config)
    except (AuthenticationError, ConfigurationError, SafetyError) as exc:
        if mobile_error is None:
            raise
        raise MobileLoginUnavailable(
            f"{mobile_error} 現有 Cookie 也無法通過論壇驗證：{exc} "
            f"可重新登入後將 Cookie 匯出至 {config.cookie_file}。"
            "若容器持續收到 HTTP 403，請主機商檢查出口 IP 與巴哈存取限制。",
            retryable=mobile_error.retryable,
        ) from exc

    if mobile_error is not None:
        LOG.warning("現有 Cookie 已通過論壇帳號與回覆表單驗證，繼續本次流程。")
    return session, snapshot
