"""Command-line and Pterodactyl console entry points."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shlex
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any

from .auth import mobile_login
from .config import AppConfig, load_dotenv, select_account
from .errors import AuthenticationError, ConfigurationError, SafetyError
from .notifications import notify_failure, notify_login_test, test_notification
from .scheduler import next_scheduled_at, run_check, run_daemon, run_once
from .state import print_status, process_lock
from .workflow import live_round_trip_test


LOG = logging.getLogger("bahamut-bumper")


def _print_json(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


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
        "  login-test [帳號]      測試指定帳號的 mobile API 登入並更新 Cookie\n"
        "  live-test <URL> CONFIRM [帳號] 在指定帳號的非伺服招生文章測試發文再刪除\n"
        "  help                   顯示本說明\n"
        "  stop                   安全停止程式"
    )


def execute_console_command(line: str, config: AppConfig) -> bool:
    """Execute one stdin command. Return True when the daemon should stop."""
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        LOG.error("Console 指令格式錯誤：%s", exc)
        return False
    if not parts:
        return False
    command = parts[0].casefold()
    failure_account = None
    try:
        if command == "help":
            print(console_help(), flush=True)
        elif command == "check":
            _print_json(run_check(config))
        elif command == "once":
            if config.accounts_file is None:
                failure_account = config.accounts[0]
            _print_json(run_once(config))
        elif command == "status":
            print_status(config.data_dir)
        elif command == "next":
            print(
                f"下次排程：{next_scheduled_at(config, datetime.now(config.timezone)).isoformat()}",
                flush=True,
            )
        elif command == "test-notification":
            test_notification(config.accounts[0])
            print("Discord 測試通知已送出。", flush=True)
        elif command == "login-test":
            if len(parts) > 2:
                raise ConfigurationError("用法：login-test [帳號]")
            profile = select_account(config, parts[1] if len(parts) == 2 else None)
            result = mobile_login(profile, enforce_rate_limit=True)
            notify_login_test(profile)
            _print_json(result)
        elif command == "live-test":
            if len(parts) not in (3, 4) or parts[2] != "CONFIRM":
                raise ConfigurationError("用法：live-test <URL> CONFIRM [帳號]")
            account = parts[3] if len(parts) == 4 else None
            failure_account = select_account(config, account)
            _print_json(live_round_trip_test(config, parts[1], account=account))
        elif command in {"stop", "quit", "exit"}:
            LOG.info("收到 Console stop 指令，將安全停止。")
            return True
        else:
            LOG.error("未知 Console 指令：%s。輸入 help 查看可用指令。", parts[0])
    except (ConfigurationError, AuthenticationError, SafetyError, ValueError) as exc:
        if failure_account is not None:
            notify_failure(failure_account, exc, scope="Console 手動執行")
        LOG.error("Console 指令 %s 失敗：%s", command, exc)
    return False


def _wait_with_console(
    stop: threading.Event,
    commands: queue.Queue[str],
    seconds: float,
    config: AppConfig,
) -> None:
    deadline = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < deadline:
        timeout = min(1.0, max(0.0, deadline - time.monotonic()))
        try:
            command = commands.get(timeout=timeout)
        except queue.Empty:
            continue
        if execute_console_command(command, config):
            stop.set()


def _serve(config: AppConfig) -> None:
    stop = threading.Event()
    commands: queue.Queue[str] = queue.Queue()

    def request_stop(signum: int, _frame: Any) -> None:
        LOG.info("收到訊號 %s，將安全停止。", signum)
        stop.set()

    def wait(seconds: float) -> None:
        _wait_with_console(stop, commands, seconds, config)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    threading.Thread(
        target=_console_reader,
        args=(commands,),
        name="pterodactyl-console",
        daemon=True,
    ).start()
    LOG.info("Console 互動指令已啟用；輸入 help 查看說明。")
    run_daemon(config, wait=wait, stopped=stop.is_set)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="巴哈每日自推（Generic Python Egg）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="立即執行正式流程一次")
    mode.add_argument("--check", action="store_true", help="只讀檢查登入、文章與 token")
    mode.add_argument("--status", action="store_true", help="顯示最近一次執行紀錄")
    mode.add_argument("--test-notification", action="store_true", help="發送一則不標註使用者的 Discord 測試通知")
    mode.add_argument("--live-test", metavar="URL", help="在自己的非伺服招生文章測試發文後刪除")
    parser.add_argument("--account", help="指定 --live-test 使用的巴哈帳號")
    parser.add_argument(
        "--confirm-live-test",
        action="store_true",
        help="確認接受測試回覆會短暫公開（搭配 --live-test）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    failure_account = None
    try:
        load_dotenv()
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        config = AppConfig.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        if args.status:
            print_status(config.data_dir)
            return 0
        with process_lock(config.data_dir / "daemon.lock"):
            if args.check:
                result = run_check(config)
                _print_json(result)
                return 0 if result["ok"] else 2
            elif args.test_notification:
                test_notification(config.accounts[0])
                print("Discord 測試通知已送出。")
            elif args.live_test:
                if not args.confirm_live_test:
                    raise ConfigurationError(
                        "即時測試會公開發文；確認網址無誤後加上 --confirm-live-test。"
                    )
                failure_account = select_account(config, args.account)
                _print_json(live_round_trip_test(config, args.live_test, account=args.account))
            elif args.once:
                if config.accounts_file is None:
                    failure_account = config.accounts[0]
                result = run_once(config)
                _print_json(result)
                return 0 if result["ok"] else 2
            else:
                _serve(config)
        return 0
    except (ConfigurationError, AuthenticationError, SafetyError, ValueError) as exc:
        if failure_account is not None:
            notify_failure(failure_account, exc, scope="手動執行")
        LOG.error("%s", exc)
        return 2
    except OSError as exc:
        LOG.error("無法讀寫執行資料：%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
