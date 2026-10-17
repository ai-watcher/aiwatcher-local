"""Cross-process ownership for AIWatcher's long-lived local runtimes."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from .local_state import state_path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_LOCK_NAME = re.compile(r"^[a-z0-9-]+$")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
ROLE_ACQUIRE_TIMEOUT_SECONDS = 2.0


def _thread_lock(name: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(name, threading.Lock())


def _lock_path(name: str) -> Path:
    if not _LOCK_NAME.fullmatch(name):
        raise ValueError(f"Invalid runtime lock name: {name!r}")
    return state_path().parent / f".runtime-{name}.lock"


def _metadata_path(name: str) -> Path:
    lock_path = _lock_path(name)
    return lock_path.with_suffix(".json")


def _try_file_lock(handle: BinaryIO) -> bool:
    try:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _release_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_metadata(path: Path) -> dict[str, Any]:
    for attempt in range(3):
        try:
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8")) if payload.strip() else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(0.01)
                continue
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _write_metadata(path: Path, record: dict[str, Any]) -> None:
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()


@dataclass
class RuntimeLock:
    name: str
    handle: BinaryIO
    thread_lock: threading.Lock
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            _release_file_lock(self.handle)
        finally:
            self.handle.close()
            self.thread_lock.release()

    def __enter__(self) -> "RuntimeLock":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def acquire_runtime_lock(
    name: str,
    *,
    metadata: dict[str, Any] | None = None,
    timeout: float = 0.0,
) -> RuntimeLock | None:
    """Acquire a named OS lock, released automatically if the process exits."""
    timeout = max(0.0, float(timeout))
    deadline = time.monotonic() + timeout
    thread_lock = _thread_lock(name)
    if not thread_lock.acquire(timeout=timeout):
        return None

    path = _lock_path(name)
    metadata_path = _metadata_path(name)
    handle: BinaryIO | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        while not _try_file_lock(handle):
            if time.monotonic() >= deadline:
                handle.close()
                thread_lock.release()
                return None
            time.sleep(0.05)
        if metadata is not None:
            record = {
                **metadata,
                "role": name,
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_metadata(metadata_path, record)
        return RuntimeLock(name=name, handle=handle, thread_lock=thread_lock)
    except Exception:
        if handle is not None:
            handle.close()
        thread_lock.release()
        raise


def runtime_lock_status(name: str) -> dict[str, Any]:
    """Report whether a named role is currently owned by a live process."""
    metadata_path = _metadata_path(name)
    probe = acquire_runtime_lock(name, timeout=0)
    if probe is not None:
        metadata = _read_metadata(metadata_path)
        probe.release()
        return {**metadata, "active": False}
    return {**_read_metadata(metadata_path), "active": True}
