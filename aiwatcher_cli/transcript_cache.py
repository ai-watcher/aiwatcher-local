"""Bounded, process-local incremental readers for append-only AI logs.

Only consumer aggregates are retained, never complete transcripts. Consumers
must not retain prompt/source text. Different accounting windows have separate
folds so a new commit cannot inherit the previous window's counters.
"""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

MAX_ENTRIES = 64
MAX_RECORDS_PER_ENTRY = 100_000
MAX_TOTAL_RECORDS = 250_000
MAX_LINE_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
GUARD_BYTES = 256


class _LimitExceeded(Exception):
    pass


@dataclass
class _Entry:
    value: Any
    signature: tuple[int, ...] = ()
    offset: int = 0
    records: int = 0
    head: bytes = b""
    guard: bytes = b""
    unterminated: bool = False


_CACHE: OrderedDict[tuple[Any, ...], _Entry] = OrderedDict()
_LOCK = threading.RLock()


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()


def file_signature(path: str) -> tuple[Any, ...]:
    """Metadata plus bounded guards for callers caching a whole-file result.

    Windows can report identical timestamps for rapid same-size rewrites.
    Guards catch those edits without hashing the complete historical log.
    """
    with open(path, "rb") as handle:
        sig = _signature(os.fstat(handle.fileno()))
        head = handle.read(min(GUARD_BYTES, sig[2]))
        handle.seek(max(0, sig[2] - GUARD_BYTES))
        return (*sig, head, handle.read(min(GUARD_BYTES, sig[2])))


def _guards_match(handle: Any, entry: _Entry) -> bool:
    handle.seek(0)
    if handle.read(len(entry.head)) != entry.head:
        return False
    handle.seek(max(0, entry.offset - len(entry.guard)))
    return handle.read(len(entry.guard)) == entry.guard


def _unchanged(handle: Any, entry: _Entry | None, sig: tuple[int, ...]) -> bool:
    return entry is not None and entry.signature == sig and _guards_match(handle, entry)


def _prepare(handle: Any, entry: _Entry | None, sig: tuple[int, ...], factory: Callable[[], Any]) -> _Entry:
    reset = entry is None
    if entry is not None:
        reset = (entry.signature[:2] != sig[:2] or sig[2] < entry.signature[2]
                 or sig[2] == entry.signature[2] or entry.unterminated)
        if not reset:
            reset = not _guards_match(handle, entry)
    if reset:
        entry = _Entry(factory())
    handle.seek(entry.offset)
    return entry


def _remember(key: tuple[Any, ...], entry: _Entry, handle: Any, sig: tuple[int, ...]) -> None:
    entry.signature = sig
    handle.seek(0)
    entry.head = handle.read(min(GUARD_BYTES, entry.offset))
    handle.seek(max(0, entry.offset - GUARD_BYTES))
    entry.guard = handle.read(min(GUARD_BYTES, entry.offset))
    _CACHE[key] = entry
    _CACHE.move_to_end(key)
    if entry.records > MAX_RECORDS_PER_ENTRY:
        # Still return the exact aggregate, but do not retain an oversized
        # deduplication/history state indefinitely.
        _CACHE.pop(key, None)
    while len(_CACHE) > MAX_ENTRIES or sum(item.records for item in _CACHE.values()) > MAX_TOTAL_RECORDS:
        _CACHE.popitem(last=False)


@contextmanager
def folded_jsonl(
    path: str, namespace: Any, factory: Callable[[], Any],
    consume: Callable[[Any, dict[str, Any]], None],
) -> Iterator[Any | None]:
    """Yield an aggregate after consuming only newly completed JSONL rows.

    The lock also protects the consumer's snapshot read. File replacement,
    shrinkage, same-size edits, or changed append guards rebuild the fold.
    A partial final record is retried after the next write. Oversized rows or
    unavailable files yield None, never a misleading partial measurement.
    """
    key = (os.path.abspath(path), "jsonl", namespace)
    with _LOCK:
        try:
            # Use one handle and one metadata API throughout. Windows stat()
            # and fstat() can expose different identity/change-time fields.
            with open(path, "rb") as handle:
                sig = _signature(os.fstat(handle.fileno()))
                entry = _CACHE.get(key)
                if _unchanged(handle, entry, sig):
                    _CACHE.move_to_end(key)
                else:
                    entry = _prepare(handle, entry, sig, factory)
                    while handle.tell() < sig[2]:
                        start = handle.tell()
                        raw = handle.readline(min(MAX_LINE_BYTES + 1, sig[2] - start))
                        if not raw:
                            raise OSError("Transcript changed while reading")
                        if len(raw) > MAX_LINE_BYTES:
                            raise _LimitExceeded()
                        try:
                            obj = json.loads(raw.decode("utf-8", errors="replace"))
                        except (json.JSONDecodeError, ValueError):
                            if not raw.endswith(b"\n"):
                                break
                        else:
                            if isinstance(obj, dict):
                                consume(entry.value, obj)
                        entry.offset = handle.tell()
                        entry.records += 1
                        entry.unterminated = not raw.endswith(b"\n")
                    _remember(key, entry, handle, sig)
        except (OSError, _LimitExceeded):
            _CACHE.pop(key, None)
            entry = None
        except BaseException:
            _CACHE.pop(key, None)
            raise
        yield entry.value if entry is not None else None


def count_pattern(path: str, pattern: Any, *, overlap: int = 64) -> int:
    """Incremental byte-pattern count, with bounded reads and seam overlap.

    Callers provide a pattern whose maximum match length fits the overlap.
    Matches already counted in the retained tail must not be counted twice.
    """
    key = (os.path.abspath(path), "pattern", pattern.pattern, pattern.flags)
    with _LOCK:
        try:
            with open(path, "rb") as handle:
                sig = _signature(os.fstat(handle.fileno()))
                entry = _CACHE.get(key)
                if _unchanged(handle, entry, sig):
                    _CACHE.move_to_end(key)
                    return entry.value["count"]
                entry = _prepare(handle, entry, sig, lambda: {"count": 0, "tail": b""})
                while handle.tell() < sig[2]:
                    chunk = handle.read(min(CHUNK_BYTES, sig[2] - handle.tell()))
                    if not chunk:
                        raise OSError("Transcript changed while reading")
                    tail = entry.value["tail"]
                    data = tail + chunk
                    entry.value["count"] += sum(match.end() > len(tail) for match in pattern.finditer(data))
                    entry.value["tail"] = data[-overlap:]
                    entry.offset = handle.tell()
                _remember(key, entry, handle, sig)
            return entry.value["count"]
        except OSError:
            _CACHE.pop(key, None)
            raise
        except BaseException:
            _CACHE.pop(key, None)
            raise
