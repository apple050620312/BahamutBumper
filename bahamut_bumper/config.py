"""Load application settings and one thread per account."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, time as wall_time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .errors import ConfigurationError
from .forum import validate_forum_url


DOTENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DOTENV_QUOTED_VALUE = re.compile(r"""(?:"([^"]*)"|'([^']*)')\s*(?:#.*)?""")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load literal KEY=value settings without replacing startup variables."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return

    settings = {}
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.removeprefix("export ").partition("=")
        key = key.strip()
        error = f"{path} 第 {number} 行格式錯誤，請使用 名稱=值。"
        if not separator or not DOTENV_KEY.fullmatch(key):
            raise ConfigurationError(error)

        value = raw_value.strip()
        if value.startswith(('"', "'")):
            quoted = DOTENV_QUOTED_VALUE.fullmatch(value)
            if quoted is None:
                raise ConfigurationError(error)
            value = next(group for group in quoted.groups() if group is not None)
        else:
            value = re.split(r"\s+#", raw_value, maxsplit=1)[0].strip()
        settings[key] = value

    for key, value in settings.items():
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class AccountConfig:
    target_url: str
    account: str
    message: str
    deletable_messages: tuple[str, ...]
    timezone: ZoneInfo
    cookie_file: Path
    data_dir: Path
    guard_urls: tuple[str, ...]
    timeout_seconds: int
    discord_webhook_file: Path
    discord_user_id: str | None
    password_file: Path
    auto_mobile_login: bool


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    timezone: ZoneInfo
    bump_at: wall_time
    retry_minutes: int
    max_retries: int
    run_missed_on_start: bool
    accounts: tuple[AccountConfig, ...]
    accounts_file: Path | None

    @classmethod
    def from_env(cls) -> AppConfig:
        raw_accounts_file = os.getenv("BAHAMUT_ACCOUNTS_FILE", "").strip()
        accounts_file = Path(raw_accounts_file).expanduser().resolve() if raw_accounts_file else None
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser().resolve()
        cookie_file = Path(
            os.getenv("BAHAMUT_COOKIE_FILE", str(data_dir / "cookies.json"))
        ).expanduser().resolve()
        timezone_name = os.getenv("TZ", "Asia/Taipei")
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ConfigurationError(f"無效時區：{timezone_name}") from exc

        target_url = ""
        guards: list[str] = []
        account = ""
        if not accounts_file:
            account = os.getenv("BAHAMUT_ACCOUNT", "").strip()
            raw_target = os.getenv("BAHAMUT_TARGET_URL", "").strip()
            if not account or not raw_target:
                raise ConfigurationError(
                    "請設定 BAHAMUT_ACCOUNTS_FILE，或同時設定 BAHAMUT_ACCOUNT 與 BAHAMUT_TARGET_URL。"
                )
            target_url = validate_forum_url(raw_target)
            for value in os.getenv("BAHAMUT_GUARD_URLS", "").split(","):
                if value.strip():
                    guard = validate_forum_url(value.strip())
                    if guard != target_url:
                        guards.append(guard)
        message = os.getenv("BAHAMUT_MESSAGE", "推").strip()
        if not message:
            raise ConfigurationError("BAHAMUT_MESSAGE 不可為空白。")
        discord_user_id = os.getenv("DISCORD_ATTENTION_USER_ID", "").strip() or None
        if discord_user_id is not None and not discord_user_id.isdigit():
            raise ConfigurationError("DISCORD_ATTENTION_USER_ID 必須是純數字。")
        raw_deletable_messages = os.getenv("BAHAMUT_DELETE_MESSAGES")
        if raw_deletable_messages is None:
            deletable_messages = (message,)
        else:
            entries = (item.strip() for item in raw_deletable_messages.split(","))
            deletable_messages = tuple(dict.fromkeys(item for item in entries if item))

        account_config = AccountConfig(
            target_url=target_url,
            account=account,
            message=message,
            deletable_messages=deletable_messages,
            timezone=timezone,
            cookie_file=cookie_file,
            data_dir=data_dir,
            guard_urls=tuple(dict.fromkeys(guards)),
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
        accounts = (
            load_account_configs(account_config, accounts_file)
            if accounts_file is not None
            else (account_config,)
        )
        return cls(
            data_dir=data_dir,
            timezone=timezone,
            bump_at=parse_wall_time(os.getenv("BUMP_TIME", "20:30")),
            retry_minutes=max(1, int(os.getenv("RETRY_MINUTES", "10"))),
            max_retries=max(1, int(os.getenv("MAX_RETRIES", "3"))),
            run_missed_on_start=env_bool("RUN_MISSED_ON_START", True),
            accounts=accounts,
            accounts_file=accounts_file,
        )


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


def _account_credential_path(
    record: dict[str, Any], field: str, path: Path, default: Path
) -> Path:
    if field not in record:
        return default.resolve()
    value = record[field]
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"帳號設定的 {field} 必須是檔案路徑。")
    return (path.parent / Path(value.strip()).expanduser()).resolve()


def load_account_configs(base: AccountConfig, path: Path) -> tuple[AccountConfig, ...]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"無法讀取帳號設定檔 {path}，請確認檔案是有效的 JSON。") from exc
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("帳號設定檔必須是至少包含一組帳號與文章的 JSON 陣列。")

    profiles: list[AccountConfig] = []
    accounts: set[str] = set()
    threads: set[str] = set()
    account_dirs = {base.data_dir}
    used_files = {path}
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"第 {number} 組帳號設定必須是 JSON 物件。")
        account = entry.get("account")
        target = entry.get("target_url")
        if not isinstance(account, str) or not account.strip() or not isinstance(target, str):
            raise ConfigurationError(f"第 {number} 組必須填寫 account 和 target_url。")
        account = account.strip()
        account_key = account.casefold()
        target_url = validate_forum_url(target.strip())
        if account_key in accounts:
            raise ConfigurationError(f"帳號 {account} 重複。每個帳號只能綁定一篇文章。")
        if target_url in threads:
            raise ConfigurationError(f"文章 {target_url} 重複。每篇文章只能綁定一個帳號。")

        folder = "account-" + quote(account_key, safe="").replace(".", "%2E")
        data_dir = (base.data_dir / "accounts" / folder).resolve()
        if data_dir in account_dirs:
            raise ConfigurationError(f"帳號 {account} 的資料夾與其他設定重複。")
        cookie_file = _account_credential_path(
            entry, "cookie_file", path, data_dir / "cookies.json"
        )
        password_file = _account_credential_path(
            entry, "password_file", path, data_dir / "bahamut_password.txt"
        )
        files = {
            cookie_file,
            cookie_file.with_suffix(cookie_file.suffix + ".tmp"),
            cookie_file.with_suffix(cookie_file.suffix + ".backup"),
            password_file,
        }
        if len(files) != 4 or not files.isdisjoint(used_files):
            raise ConfigurationError(f"帳號 {account} 的 Cookie 或密碼檔路徑重複。")

        auto_mobile_login = entry.get("auto_mobile_login", base.auto_mobile_login)
        if not isinstance(auto_mobile_login, bool):
            raise ConfigurationError(f"帳號 {account} 的 auto_mobile_login 必須是 true 或 false。")
        guards = entry.get("guard_urls", [])
        if not isinstance(guards, list) or not all(isinstance(url, str) for url in guards):
            raise ConfigurationError(f"帳號 {account} 的 guard_urls 必須是網址陣列。")
        guard_urls = dict.fromkeys(validate_forum_url(url.strip()) for url in guards)
        guard_urls.pop(target_url, None)

        profiles.append(replace(
            base,
            account=account,
            target_url=target_url,
            data_dir=data_dir,
            cookie_file=cookie_file,
            password_file=password_file,
            guard_urls=tuple(guard_urls),
            auto_mobile_login=auto_mobile_login,
        ))
        accounts.add(account_key)
        threads.add(target_url)
        account_dirs.add(data_dir)
        used_files.update(files)
    return tuple(profiles)


def select_account(config: AppConfig, account: str | None) -> AccountConfig:
    if account is None:
        if len(config.accounts) == 1:
            return config.accounts[0]
        raise ConfigurationError("請指定要使用的巴哈帳號。")
    for profile in config.accounts:
        if profile.account.casefold() == account.casefold():
            return profile
    raise ConfigurationError(f"找不到帳號設定：{account}")
