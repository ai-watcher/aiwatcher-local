"""Stable local Git repository, lineage, and checkout identity.

Linked worktrees share one machine-local repository ID, while clones have
distinct repository IDs and may share a lineage ID. Checkout identity follows
Git's administrative directory, so sibling worktrees remain distinct.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .local_state import get_or_create_identity_secret


GIT_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class GitIdentity:
    repository_id: str | None
    repository_lineage_id: str | None
    checkout_id: str
    checkout_path: str
    git_dir: str
    common_dir: str
    identity_source: str = "observed_git"


_IDENTITY_CACHE: dict[tuple[str, str], tuple[str, GitIdentity]] = {}
_BIRTH_CACHE: dict[tuple[int, int, int], str | None] = {}


def _git_text(path: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _absolute_git_path(checkout: str, value: str) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(checkout, candidate)
    try:
        return str(candidate.resolve())
    except OSError:
        return os.path.realpath(str(candidate))


def _opaque_id(secret: bytes, kind: str, material: str) -> str:
    digest = hmac.new(
        secret,
        f"{kind}-v1\0{material}".encode("utf-8", errors="surrogateescape"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{kind}-v1-{digest}"


def _filesystem_birth_marker(path: str, stat: os.stat_result) -> str | None:
    birth_ns = getattr(stat, "st_birthtime_ns", None)
    if isinstance(birth_ns, int) and birth_ns > 0:
        return str(birth_ns)
    birth = getattr(stat, "st_birthtime", None)
    if isinstance(birth, (int, float)) and birth > 0:
        return str(int(float(birth) * 1_000_000_000))
    if os.name == "nt" and stat.st_ctime_ns > 0:
        return str(stat.st_ctime_ns)
    if not sys.platform.startswith("linux"):
        return None

    cache_key = (stat.st_dev, stat.st_ino, stat.st_ctime_ns)
    if cache_key in _BIRTH_CACHE:
        return _BIRTH_CACHE[cache_key]
    try:
        result = subprocess.run(
            ["stat", "-c", "%w", "--", path],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
        value = result.stdout.strip() if result.returncode == 0 else ""
        marker = value if value and value != "-" else None
    except (OSError, subprocess.TimeoutExpired):
        marker = None
    if len(_BIRTH_CACHE) >= 4096:
        _BIRTH_CACHE.clear()
    _BIRTH_CACHE[cache_key] = marker
    return marker


def _filesystem_identity(path: str) -> str:
    """Identify a live filesystem object without exposing its path."""
    try:
        stat = os.stat(path)
        if stat.st_ino:
            birth = _filesystem_birth_marker(path, stat)
            if birth:
                return f"inode:{stat.st_dev}:{stat.st_ino}:born:{birth}"
            # ctime changes during ordinary Git metadata updates on some
            # filesystems. Device + inode is the stable local fallback; a
            # replacement repository normally receives a different inode.
            return f"inode:{stat.st_dev}:{stat.st_ino}"
    except OSError:
        pass
    return f"path:{os.path.realpath(path)}"


def _canonical_remote(value: str | None) -> str | None:
    """Credential-free hosted remote identity; local remotes use Git roots."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if "://" not in raw and ":" in raw and not raw.startswith(("/", "./", "../")):
        host_part, path = raw.split(":", 1)
        host = host_part.rsplit("@", 1)[-1].lower()
        if "." not in host:
            return None
        clean_path = path.strip("/")
    else:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if not host:
            return None
        clean_path = parsed.path.strip("/")
    if clean_path.endswith(".git"):
        clean_path = clean_path[:-4]
    return f"{host}/{clean_path}" if clean_path else None


def _lineage_material(checkout_path: str) -> str | None:
    remote = _canonical_remote(_git_text(checkout_path, ["remote", "get-url", "origin"]))
    if remote:
        return f"remote:{remote}"
    roots = _git_text(checkout_path, ["rev-list", "--max-parents=0", "HEAD"])
    values = sorted(line.strip() for line in (roots or "").splitlines() if line.strip())
    return "roots:" + "\0".join(values) if values else None


def resolve_git_identity(path: str | None) -> GitIdentity | None:
    """Resolve one checkout without persisting paths or contacting a remote."""
    if not path:
        return None
    try:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            return None
        if candidate.is_file():
            candidate = candidate.parent
        cache_key = os.path.realpath(str(candidate))
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        secret = get_or_create_identity_secret()
    except (OSError, ValueError):
        return None
    secret_marker = hashlib.sha256(secret).hexdigest()[:16]
    cached = _IDENTITY_CACHE.get((secret_marker, cache_key))
    if cached is not None:
        marker, identity = cached
        if marker == _filesystem_identity(identity.git_dir):
            lineage_material = _lineage_material(identity.checkout_path)
            if lineage_material is None:
                return identity
            lineage_id = _opaque_id(secret, "lineage", lineage_material)
            if lineage_id != identity.repository_lineage_id:
                identity = replace(identity, repository_lineage_id=lineage_id)
                _IDENTITY_CACHE[(secret_marker, cache_key)] = (marker, identity)
            return identity
        _IDENTITY_CACHE.pop((secret_marker, cache_key), None)

    checkout = _git_text(cache_key, ["rev-parse", "--show-toplevel"])
    git_dir_value = _git_text(cache_key, ["rev-parse", "--git-dir"])
    common_dir_value = _git_text(cache_key, ["rev-parse", "--git-common-dir"])
    if not checkout or not git_dir_value or not common_dir_value:
        return None

    checkout_path = os.path.realpath(checkout)
    git_dir = _absolute_git_path(checkout_path, git_dir_value)
    common_dir = _absolute_git_path(checkout_path, common_dir_value)
    repository_marker = _filesystem_identity(common_dir)
    checkout_marker = _filesystem_identity(git_dir)
    repository_id = _opaque_id(secret, "repository", repository_marker)
    lineage_material = _lineage_material(checkout_path)
    repository_lineage_id = (
        _opaque_id(secret, "lineage", lineage_material) if lineage_material else None
    )
    identity = GitIdentity(
        repository_id=repository_id,
        repository_lineage_id=repository_lineage_id,
        checkout_id=_opaque_id(secret, "checkout", checkout_marker),
        checkout_path=checkout_path,
        git_dir=git_dir,
        common_dir=common_dir,
    )
    # An unborn repository can gain lineage after its first commit.
    if repository_lineage_id is not None:
        _IDENTITY_CACHE[(secret_marker, cache_key)] = (checkout_marker, identity)
    return identity


def identity_for_session(project_path: str | None, raw_cwd: str | None) -> GitIdentity | None:
    """Choose the exact worktree only when it belongs to the grouped checkout."""
    grouped = resolve_git_identity(project_path)
    observed = resolve_git_identity(raw_cwd)
    if observed is not None and grouped is not None:
        return (
            observed
            if observed.common_dir == grouped.common_dir
            else replace(grouped, identity_source="identity_conflict")
        )
    if observed is not None:
        return observed
    return replace(grouped, identity_source="project_fallback") if grouped is not None else None


def repository_identity(path: str | None) -> str | None:
    identity = resolve_git_identity(path)
    return identity.repository_id if identity else None


def repository_lineage_identity(path: str | None) -> str | None:
    identity = resolve_git_identity(path)
    return identity.repository_lineage_id if identity else None
