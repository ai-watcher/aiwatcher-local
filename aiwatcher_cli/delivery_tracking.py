"""Conservative, automatic setup for delivery evidence tracking."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .git_identity import resolve_git_identity


COMMIT_HOOK_MARKER = "# >>> aiwatcher commit receipt >>>"
COMMIT_HOOK_END = "# <<< aiwatcher commit receipt <<<"
_GIT_TIMEOUT_SECONDS = 5
_SYNC_INTERVAL_SECONDS = 300
_AUTOMATIC_CANDIDATE_LIMIT = 32
_SYNC_CACHE: dict[str, dict[str, Any]] = {}
_DISCOVERY_CURSOR: dict[str, int] = {}
_DISCOVERY_SIGNATURE: dict[str, tuple[str, ...]] = {}
_SYNC_CACHE_LOCK = threading.Lock()
_HOOK_LOCKS: dict[str, threading.Lock] = {}
_HOOK_LOCKS_GUARD = threading.Lock()


def _run_git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def cli_command_for_current_install() -> str:
    """Return a shell-safe command that remains valid outside this checkout."""
    installed = shutil.which("aiwatcher")
    if installed:
        if os.name == "nt":
            installed = installed.replace("\\", "/")
        return shlex.quote(installed)
    executable = sys.executable
    package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if os.name == "nt":
        executable = executable.replace("\\", "/")
        package_root = package_root.replace("\\", "/")
    parts = [executable, "-m", "aiwatcher_cli"]
    pythonpath_prefix = f"PYTHONPATH={shlex.quote(package_root + os.pathsep)}\"${{PYTHONPATH:-}}\""
    return f"{pythonpath_prefix} " + " ".join(shlex.quote(part) for part in parts)


def commit_hook_body(command: str) -> str:
    # Receipt collection must never turn an otherwise successful commit into a failure.
    return (
        f"{COMMIT_HOOK_MARKER}\n"
        f"{command} commit-receipt --quiet-if-empty 2>/dev/null || true\n"
        f"{COMMIT_HOOK_END}\n"
    )


def _git_common_dir(repo: str) -> str | None:
    try:
        result = _run_git(repo, "rev-parse", "--git-common-dir")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(repo, path)
    return os.path.normpath(os.path.realpath(path))


def commit_hook_path(repo: str) -> str | None:
    """Return Git's standard shared hook path, ignoring custom hooksPath values."""
    common_dir = _git_common_dir(repo)
    return os.path.join(common_dir, "hooks", "post-commit") if common_dir else None


def active_commit_hook_path(repo: str) -> str | None:
    """Return the hook path Git actually invokes, including custom hooksPath."""
    try:
        result = _run_git(repo, "rev-parse", "--git-path", "hooks/post-commit")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = result.stdout.strip()
    if not os.path.isabs(path):
        path = os.path.join(repo, path)
    # Preserve the hook entry itself. Resolving a symlink here would make an
    # atomic update replace the target file while hiding that Git reaches it
    # through a symlink managed by another hook tool.
    return os.path.normpath(os.path.abspath(path))


def custom_hooks_configuration(repo: str) -> dict[str, str] | None:
    """Return the active custom hooksPath and origin, if one is configured."""
    try:
        result = _run_git(repo, "config", "--show-origin", "--get", "core.hooksPath")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"origin": "unknown", "path": "", "error": str(exc)}
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        return {
            "origin": "unknown",
            "path": "",
            "error": result.stderr.strip() or "Could not inspect core.hooksPath.",
        }
    if not result.stdout.strip():
        return None
    line = result.stdout.strip().splitlines()[-1]
    origin, separator, value = line.partition("\t")
    if not separator:
        parts = line.split(None, 1)
        origin, value = (parts[0], parts[1]) if len(parts) == 2 else ("unknown", line)
    return {"origin": origin.strip(), "path": value.strip(), "error": ""}


def _is_shell_hook(content: str) -> bool:
    lines = content.splitlines()
    first_line = lines[0].strip().lower() if lines else ""
    return first_line.startswith("#!") and any(shell in first_line for shell in ("/sh", "bash", "zsh", "dash"))


def _hook_thread_lock(path: str) -> threading.Lock:
    key = os.path.normcase(os.path.realpath(path))
    with _HOOK_LOCKS_GUARD:
        return _HOOK_LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _hook_file_lock(path: str):
    """Serialize separate AIWatcher processes without depending on platform locks."""
    lock_path = path + ".aiwatcher.lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor: int | None = None
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + 5
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{token}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    owner = Path(lock_path).read_text(encoding="ascii", errors="ignore").strip()
                    owner_pid = int(owner.partition(":")[0])
                    try:
                        os.kill(owner_pid, 0)
                        owner_alive = True
                    except ProcessLookupError:
                        owner_alive = False
                    except (PermissionError, OSError):
                        owner_alive = True
                    if not owner_alive:
                        os.unlink(lock_path)
                        continue
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if Path(lock_path).read_text(encoding="ascii", errors="ignore").strip() == token:
                os.unlink(lock_path)
        except OSError:
            pass


def _atomic_write(path: str, content: str, *, mode: int | None = None) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".post-commit.aiwatcher-", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content if content.endswith("\n") else content + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        desired_mode = mode if mode is not None else 0o755
        if os.name != "nt":
            os.chmod(temporary, desired_mode | stat.S_IXUSR)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _replace_marked_block(existing: str, body: str) -> tuple[str | None, str]:
    if COMMIT_HOOK_MARKER not in existing:
        return None, "not_installed"
    pattern = re.compile(
        re.escape(COMMIT_HOOK_MARKER) + r".*?" + re.escape(COMMIT_HOOK_END) + r"\n?",
        re.DOTALL,
    )
    matches = list(pattern.finditer(existing))
    if len(matches) != 1:
        return None, "malformed_marker"
    updated = pattern.sub(body, existing, count=1)
    return updated, "current" if updated == existing else "updated"


def _valid_receipt_block(content: str) -> bool:
    """Return whether the single marked block invokes AIWatcher receipts."""
    pattern = re.compile(
        re.escape(COMMIT_HOOK_MARKER) + r"(.*?)" + re.escape(COMMIT_HOOK_END),
        re.DOTALL,
    )
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        return False
    commands = [
        line.strip()
        for line in matches[0].group(1).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(commands) != 1:
        return False
    try:
        tokens = shlex.split(commands[0])
    except ValueError:
        return False
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./", "../")):
        tokens.pop(0)
    if not tokens:
        return False
    executable = os.path.basename(tokens[0]).lower()
    if executable in {"aiwatcher", "aiwatcher.exe"}:
        return len(tokens) >= 2 and tokens[1] == "commit-receipt"
    return len(tokens) >= 4 and tokens[1:4] == ["-m", "aiwatcher_cli", "commit-receipt"]


def install_commit_hook(
    repo: str,
    *,
    command: str | None = None,
    allow_existing: bool = False,
    allow_custom: bool = False,
) -> dict[str, Any]:
    """Install AIWatcher's block without changing custom or unfamiliar hooks.

    Automatic setup leaves every pre-existing hook untouched. The explicit CLI
    may opt into composing with an existing shell hook via ``allow_existing``.
    """
    standard_path = commit_hook_path(repo)
    if not standard_path:
        return {"ok": False, "status": "not_a_repository", "repo": repo, "path": None}
    custom = custom_hooks_configuration(repo)
    if custom and custom.get("error"):
        return {
            "ok": False,
            "status": "hook_configuration_unavailable",
            "repo": repo,
            "path": standard_path,
            "detail": custom["error"],
        }
    if custom and not allow_custom:
        return {
            "ok": False,
            "status": "custom_hooks_path",
            "repo": repo,
            "path": standard_path,
            "detail": f"core.hooksPath is set to {custom['path']} ({custom['origin']}); left unchanged.",
        }
    path = active_commit_hook_path(repo) if custom else standard_path
    if not path:
        return {
            "ok": False,
            "status": "hook_configuration_unavailable",
            "repo": repo,
            "path": standard_path,
            "detail": "Could not resolve Git's active post-commit hook path.",
        }
    body = commit_hook_body(command or cli_command_for_current_install())
    thread_lock = _hook_thread_lock(path)
    try:
        with thread_lock, _hook_file_lock(path):
            path_exists = os.path.lexists(path)
            is_symlink = os.path.islink(path)
            existing = Path(path).read_text(encoding="utf-8", errors="replace") if path_exists else ""
            existing_mode = stat.S_IMODE(os.stat(path).st_mode) if path_exists else None
            if existing.strip() and not _is_shell_hook(existing):
                return {
                    "ok": False,
                    "status": "unsupported_existing_hook",
                    "repo": repo,
                    "path": path,
                    "detail": "The existing post-commit hook is not a recognized shell script; left unchanged.",
                }
            updated, marker_status = _replace_marked_block(existing, body)
            if marker_status == "malformed_marker":
                return {
                    "ok": False,
                    "status": marker_status,
                    "repo": repo,
                    "path": path,
                    "detail": "The existing AIWatcher hook marker is incomplete or duplicated.",
                }
            if updated is not None:
                if marker_status == "current":
                    return {"ok": True, "status": "current", "changed": False, "repo": repo, "path": path}
                if is_symlink:
                    return {
                        "ok": False,
                        "status": "symlink_hook_requires_manual_update",
                        "repo": repo,
                        "path": path,
                        "detail": "The active post-commit hook is a symlink; left unchanged to preserve its owner.",
                    }
                _atomic_write(path, updated, mode=existing_mode)
                return {"ok": True, "status": "updated", "changed": True, "repo": repo, "path": path}
            if existing.strip():
                if is_symlink:
                    return {
                        "ok": False,
                        "status": "symlink_hook_requires_manual_update",
                        "repo": repo,
                        "path": path,
                        "detail": "The active post-commit hook is a symlink; left unchanged to preserve its owner.",
                    }
                if not allow_existing:
                    return {
                        "ok": False,
                        "status": "existing_hook_requires_confirmation",
                        "repo": repo,
                        "path": path,
                        "detail": "A pre-existing post-commit hook was left unchanged.",
                    }
                updated = existing.rstrip("\n") + "\n\n" + body
                status_value = "installed_alongside_existing"
            else:
                updated = "#!/bin/sh\n\n" + body
                status_value = "installed"
            _atomic_write(path, updated, mode=existing_mode)
    except (OSError, TimeoutError) as exc:
        return {"ok": False, "status": "write_failed", "repo": repo, "path": path, "detail": str(exc)}
    return {"ok": True, "status": status_value, "changed": True, "repo": repo, "path": path}


def _remove_commit_hook_at_path(repo: str, path: str) -> dict[str, Any]:
    """Remove AIWatcher's block from one concrete hook path."""
    if not path or not os.path.exists(path):
        return {"ok": True, "status": "not_installed", "repo": repo, "path": path}
    if os.path.islink(path):
        return {
            "ok": False,
            "status": "symlink_hook_requires_manual_update",
            "repo": repo,
            "path": path,
            "detail": "The post-commit hook is a symlink; left unchanged to preserve its owner.",
        }
    try:
        with _hook_thread_lock(path), _hook_file_lock(path):
            existing = Path(path).read_text(encoding="utf-8", errors="replace")
            if COMMIT_HOOK_MARKER not in existing:
                return {"ok": True, "status": "not_installed", "repo": repo, "path": path}
            pattern = re.compile(
                re.escape(COMMIT_HOOK_MARKER) + r".*?" + re.escape(COMMIT_HOOK_END) + r"\n?",
                re.DOTALL,
            )
            if len(list(pattern.finditer(existing))) != 1:
                return {
                    "ok": False,
                    "status": "malformed_marker",
                    "repo": repo,
                    "path": path,
                    "detail": "The existing AIWatcher hook marker is incomplete or duplicated.",
                }
            updated = pattern.sub("", existing, count=1).strip()
            if updated in {"", "#!/bin/sh"}:
                os.unlink(path)
                return {"ok": True, "status": "removed_file", "repo": repo, "path": path}
            _atomic_write(path, updated + "\n", mode=stat.S_IMODE(os.stat(path).st_mode))
    except (OSError, TimeoutError) as exc:
        return {"ok": False, "status": "write_failed", "repo": repo, "path": path, "detail": str(exc)}
    return {"ok": True, "status": "removed_block", "repo": repo, "path": path}


def remove_commit_hook(repo: str) -> dict[str, Any]:
    """Remove AIWatcher from active and dormant standard hooks."""
    candidates = [active_commit_hook_path(repo), commit_hook_path(repo)]
    paths: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        paths.append(candidate)
    if not paths:
        return {"ok": True, "status": "not_installed", "repo": repo, "path": None, "paths": []}

    results = [_remove_commit_hook_at_path(repo, path) for path in paths]
    failures = [result for result in results if not result.get("ok")]
    if failures:
        failure = failures[0]
        return {**failure, "paths": paths, "results": results}
    removed = [result for result in results if result.get("status") != "not_installed"]
    if not removed:
        return {"ok": True, "status": "not_installed", "repo": repo, "path": paths[0], "paths": paths}
    primary = removed[0]
    status_value = primary["status"] if len(removed) == 1 else "removed_blocks"
    return {
        "ok": True,
        "status": status_value,
        "repo": repo,
        "path": primary.get("path"),
        "paths": [result.get("path") for result in removed],
        "results": results,
    }


def _candidate_paths(item: Any) -> tuple[Any, ...]:
    return (
        getattr(item, "raw_cwd", None),
        getattr(item, "checkout_path", None),
        getattr(item, "project_path", None),
    )


def _item_recency(item: Any) -> float:
    for field in ("timestamp", "updated_at", "started_at"):
        value = getattr(item, field, None)
        if value is not None and callable(getattr(value, "timestamp", None)):
            try:
                return float(value.timestamp())
            except (OSError, OverflowError, ValueError):
                continue
    return 0.0


def _candidate_values(items: Iterable[Any]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for item in sorted(items, key=_item_recency, reverse=True):
        for candidate in _candidate_paths(item):
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            key = os.path.normcase(os.path.realpath(candidate))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates


def observed_repositories(
    items: Iterable[Any],
    *,
    candidate_limit: int | None = None,
    candidate_offset: int = 0,
) -> list[dict[str, Any]]:
    """Return unique repositories seen in session and event metadata."""
    repositories: list[dict[str, Any]] = []
    seen_hooks: set[str] = set()
    candidates = _candidate_values(items)
    if candidate_limit is not None and len(candidates) > candidate_limit:
        offset = candidate_offset % len(candidates)
        candidates = (candidates[offset:] + candidates[:offset])[:candidate_limit]
    for candidate in candidates:
        identity = resolve_git_identity(candidate)
        if identity is None:
            continue
        hook_path = commit_hook_path(identity.checkout_path)
        if not hook_path:
            continue
        hook_key = os.path.normcase(os.path.realpath(hook_path))
        if hook_key in seen_hooks:
            continue
        seen_hooks.add(hook_key)
        custom = custom_hooks_configuration(identity.checkout_path)
        active_hook_path = (
            active_commit_hook_path(identity.checkout_path)
            if custom and not custom.get("error")
            else hook_path
        )
        repositories.append({
            "repository_id": identity.repository_id,
            "checkout_path": identity.checkout_path,
            "hook_path": hook_path,
            "active_hook_path": active_hook_path,
            "custom_hooks_path": custom,
        })
    return repositories


def _inspect_installed_hook(path: str | None) -> tuple[bool, bool, str | None, str | None]:
    if not path:
        return False, False, "hook_configuration_unavailable", "Could not resolve Git's active hook path."
    try:
        hook = Path(path)
        if not hook.is_file():
            return False, False, None, None
        content = hook.read_text(encoding="utf-8", errors="replace")
        if not _is_shell_hook(content):
            return False, True, "unsupported_existing_hook", "The active post-commit hook is not a recognized shell script."
        pattern = re.compile(
            re.escape(COMMIT_HOOK_MARKER) + r".*?" + re.escape(COMMIT_HOOK_END) + r"\n?",
            re.DOTALL,
        )
        marker_count = len(list(pattern.finditer(content)))
        if COMMIT_HOOK_MARKER in content and marker_count != 1:
            return False, True, "malformed_marker", "The active AIWatcher marker is incomplete or duplicated."
        if marker_count != 1:
            return False, True, "existing_hook_requires_confirmation", "A pre-existing post-commit hook was left unchanged."
        if not _valid_receipt_block(content):
            return False, True, "invalid_aiwatcher_hook", "The marked block does not invoke AIWatcher commit receipts."
        if os.name != "nt" and not os.access(path, os.X_OK):
            return False, True, "hook_not_executable", "The active post-commit hook is not executable."
        return True, True, None, None
    except OSError as exc:
        return False, False, "hook_read_failed", str(exc)


def _repository_status(repo: dict[str, Any]) -> dict[str, Any]:
    active_path = repo.get("active_hook_path")
    installed, _existing, issue, detail = _inspect_installed_hook(
        str(active_path) if active_path else None,
    )
    custom = repo.get("custom_hooks_path")
    if custom and custom.get("error"):
        installed = False
        issue = "hook_configuration_unavailable"
        detail = f"Could not inspect core.hooksPath; automatic setup left Git hooks unchanged. {custom['error']}"
    elif custom and not installed and issue in {None, "existing_hook_requires_confirmation"}:
        issue = "custom_hooks_path"
        detail = f"core.hooksPath is set to {custom['path']} ({custom['origin']}); automatic setup left it unchanged."
    elif custom and installed:
        detail = "A valid AIWatcher block is installed in Git's active custom hook; automatic updates remain paused."
    elif not installed and issue is None:
        issue = "not_installed"
        detail = "No AIWatcher block is installed in Git's active post-commit hook."
    return {
        **repo,
        "hook_path": active_path or repo.get("hook_path"),
        "installed": installed,
        "issue": issue,
        "detail": detail,
    }


def _status_for_repositories(
    config: dict[str, Any],
    repositories: list[dict[str, Any]],
    *,
    candidate_count: int | None = None,
) -> dict[str, Any]:
    rows = [_repository_status(repo) for repo in repositories]
    installed_count = sum(1 for row in rows if row["installed"])
    return {
        "enabled": bool(config.get("enabled")),
        "enabled_at": config.get("enabled_at"),
        "observed_repositories": len(rows),
        "tracked_repositories": installed_count,
        "needs_attention": len(rows) - installed_count,
        "candidate_limit": _AUTOMATIC_CANDIDATE_LIMIT,
        "candidate_directories": candidate_count if candidate_count is not None else len(rows),
        "discovery_truncated": bool(
            candidate_count is not None and candidate_count > _AUTOMATIC_CANDIDATE_LIMIT
        ),
        "deferred_candidate_directories": max(
            0,
            (candidate_count or 0) - _AUTOMATIC_CANDIDATE_LIMIT,
        ),
        "repositories": rows,
        "scope": "Observed Git repositories; one standard repository hook covers its branches and linked worktrees.",
        "session_identity_note": (
            "Hook configuration is not execution proof. A successful receipt records commit facts; exact session "
            "linkage also requires the AI host to expose a session ID."
        ),
    }


def _candidate_signature(items: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({
        os.path.normcase(os.path.realpath(candidate))
        for item in items
        for candidate in _candidate_paths(item)
        if isinstance(candidate, str) and candidate.strip()
    }))


def _cache_status(
    cache_key: str,
    *,
    signature: tuple[str, ...],
    enabled: bool,
    status: dict[str, Any],
    synchronized: bool,
) -> None:
    with _SYNC_CACHE_LOCK:
        _SYNC_CACHE[cache_key] = {
            "created_at": time.monotonic(),
            "signature": signature,
            "enabled": enabled,
            "status": status,
            "synchronized": synchronized,
        }


def delivery_tracking_status(items: Iterable[Any], *, force: bool = True) -> dict[str, Any]:
    from .local_state import delivery_tracking_settings

    materialized = list(items)
    config = delivery_tracking_settings()
    enabled = bool(config.get("enabled"))
    signature = _candidate_signature(materialized)
    cache_key = os.environ.get("AIWATCHER_STATE_FILE", "default")
    if not force:
        with _SYNC_CACHE_LOCK:
            cached = _SYNC_CACHE.get(cache_key)
            if (
                cached
                and time.monotonic() - cached["created_at"] < _SYNC_INTERVAL_SECONDS
                and cached["signature"] == signature
                and cached["enabled"] == enabled
            ):
                return cached["status"]
    status = _status_for_repositories(
        config,
        observed_repositories(materialized, candidate_limit=_AUTOMATIC_CANDIDATE_LIMIT),
        candidate_count=len(signature),
    )
    _cache_status(
        cache_key,
        signature=signature,
        enabled=enabled,
        status=status,
        synchronized=False,
    )
    return status


def sync_delivery_tracking(
    items: Iterable[Any],
    *,
    force: bool = True,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Configure newly observed repositories when one-time setup is enabled."""
    from .local_state import delivery_tracking_settings

    materialized = list(items)
    config = config or delivery_tracking_settings()
    enabled = bool(config.get("enabled"))
    signature = _candidate_signature(materialized)
    cache_key = os.environ.get("AIWATCHER_STATE_FILE", "default")
    if not force:
        with _SYNC_CACHE_LOCK:
            cached = _SYNC_CACHE.get(cache_key)
            if (
                cached
                and cached.get("synchronized")
                and time.monotonic() - cached["created_at"] < _SYNC_INTERVAL_SECONDS
                and cached["signature"] == signature
                and cached["enabled"] == enabled
            ):
                return cached["status"]
    if _DISCOVERY_SIGNATURE.get(cache_key) != signature:
        _DISCOVERY_CURSOR[cache_key] = 0
        _DISCOVERY_SIGNATURE[cache_key] = signature
    cursor = _DISCOVERY_CURSOR.get(cache_key, 0)
    repositories = observed_repositories(
        materialized,
        candidate_limit=_AUTOMATIC_CANDIDATE_LIMIT,
        candidate_offset=cursor,
    )
    if len(signature) > _AUTOMATIC_CANDIDATE_LIMIT:
        _DISCOVERY_CURSOR[cache_key] = (cursor + _AUTOMATIC_CANDIDATE_LIMIT) % len(signature)
    if not enabled:
        status = _status_for_repositories(config, repositories, candidate_count=len(signature))
        _cache_status(
            cache_key,
            signature=signature,
            enabled=False,
            status=status,
            synchronized=True,
        )
        return status
    results = [install_commit_hook(repo["checkout_path"]) for repo in repositories]
    status = _status_for_repositories(config, repositories, candidate_count=len(signature))
    status["sync_results"] = results
    _cache_status(
        cache_key,
        signature=signature,
        enabled=True,
        status=status,
        synchronized=True,
    )
    return status
