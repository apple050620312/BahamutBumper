"""Atomic runtime state and the daemon lock."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Iterator

from .errors import ConfigurationError


def write_json(path: Path, payload: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if private and os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def write_status(data_dir: Path, payload: dict[str, Any]) -> None:
    write_json(data_dir / "status.json", payload)


def print_status(data_dir: Path) -> None:
    path = data_dir / "status.json"
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
    except OSError as exc:
        handle.close()
        raise ConfigurationError("已有另一個 main.py 實例正在執行。") from exc
    try:
        yield
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
