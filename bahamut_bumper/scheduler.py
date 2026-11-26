"""Schedule account workflows and retry failed attempts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import AccountConfig, AppConfig
from .errors import AuthenticationError
from .notifications import notify_daily_success, notify_failure
from .state import write_status
from .workflow import bump_account, check_account


LOG = logging.getLogger("bahamut-bumper")


def seconds_until(target: datetime, now: datetime) -> float:
    return max(0.0, (target - now).total_seconds())


def next_scheduled_at(config: AppConfig, now: datetime) -> datetime:
    candidate = datetime.combine(now.date(), config.bump_at, tzinfo=config.timezone)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _failure_status(config: AccountConfig, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "failed_at": datetime.now(config.timezone).isoformat(),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "retryable": not isinstance(exc, AuthenticationError) or exc.retryable,
    }


def _account_summary(config: AppConfig, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = [
        {**results[profile.account], "account": profile.account, "target": profile.target_url}
        for profile in config.accounts
        if profile.account in results
    ]
    return {
        "ok": len(rows) == len(config.accounts) and all(row["ok"] for row in rows),
        "results": rows,
    }


def _run_accounts_once(
    config: AppConfig,
    action: Callable[[AccountConfig], dict[str, Any]],
    *,
    save_status: bool = False,
) -> dict[str, Any]:
    if config.accounts_file is None:
        return action(config.accounts[0])

    results = {}
    for profile in config.accounts:
        try:
            results[profile.account] = action(profile)
        except Exception as exc:
            LOG.error("帳號 %s 執行失敗：%s", profile.account, exc)
            failure = _failure_status(profile, exc)
            results[profile.account] = failure
            if save_status:
                _write_failure_status(profile, failure)
                notify_failure(profile, exc, scope="手動執行")
    summary = _account_summary(config, results)
    if save_status:
        write_status(config.data_dir, summary)
    return summary


def _write_failure_status(profile: AccountConfig, failure: dict[str, Any]) -> None:
    try:
        write_status(profile.data_dir, failure)
    except OSError as exc:
        LOG.warning("無法保存帳號 %s 的失敗紀錄：%s", profile.account, exc)


def _run_account(profile: AccountConfig) -> dict[str, Any]:
    result = bump_account(profile)
    write_status(profile.data_dir, result)
    notify_daily_success(profile, result)
    return result


def run_check(config: AppConfig) -> dict[str, Any]:
    return _run_accounts_once(config, check_account)


def run_once(config: AppConfig) -> dict[str, Any]:
    return _run_accounts_once(config, _run_account, save_status=True)


def run_daemon(
    config: AppConfig, *, wait: Callable[[float], None], stopped: Callable[[], bool]
) -> None:
    now = datetime.now(config.timezone)
    run_now = config.run_missed_on_start and now.time() >= config.bump_at
    while not stopped():
        if not run_now:
            target = next_scheduled_at(config, datetime.now(config.timezone))
            LOG.info("下次執行：%s", target.isoformat())
            wait(seconds_until(target, datetime.now(config.timezone)))
            if stopped():
                break
        run_now = False
        run_date = datetime.now(config.timezone).date()
        pending = config.accounts
        results: dict[str, dict[str, Any]] = {}
        for attempt in range(1, config.max_retries + 1):
            retry_accounts = []
            for profile in pending:
                if stopped() or datetime.now(config.timezone).date() != run_date:
                    break
                try:
                    LOG.info(
                        "開始帳號 %s 的每日流程（第 %d/%d 次嘗試）。",
                        profile.account,
                        attempt,
                        config.max_retries,
                    )
                    results[profile.account] = _run_account(profile)
                except Exception as exc:
                    failure = _failure_status(profile, exc)
                    failure["attempt"] = attempt
                    results[profile.account] = failure
                    _write_failure_status(profile, failure)
                    LOG.exception("帳號 %s 的每日流程失敗：%s", profile.account, exc)
                    final_failure = (
                        not failure["retryable"]
                        or attempt >= config.max_retries
                        or datetime.now(config.timezone).date() != run_date
                    )
                    if isinstance(exc, AuthenticationError) or final_failure:
                        notify_failure(profile, exc, scope="每日流程")
                    if not final_failure:
                        retry_accounts.append(profile)
                    elif not failure["retryable"]:
                        LOG.info(
                            "帳號 %s 需要處理登入，停止該帳號本日剩餘重試。",
                            profile.account,
                        )
            if config.accounts_file is not None:
                write_status(config.data_dir, _account_summary(config, results))
            if stopped() or not retry_accounts or datetime.now(config.timezone).date() != run_date:
                break
            wait(config.retry_minutes * 60)
            pending = tuple(retry_accounts)
