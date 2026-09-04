"""Source-checkout update helpers for AIWatcher Local."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence


def installed_source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def package_upgrade_guidance() -> list[dict[str, str]]:
    return [
        {"label": "pipx", "command": "pipx upgrade aiwatcher-cli"},
        {"label": "pip", "command": "python -m pip install --upgrade aiwatcher-cli"},
        {
            "label": "GitHub package install",
            "command": "python -m pip install --upgrade git+https://github.com/ai-watcher/aiwatcher-local.git",
        },
    ]


def git_capture(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _git_count(repo: Path, rev_range: str) -> int | None:
    result = git_capture(repo, ["rev-list", "--count", rev_range])
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def _message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip() or "unknown error").strip()


def check_for_updates(
    *,
    repo: str | Path | None = None,
    remote: str = "origin",
    branch: str = "main",
    fetch: bool = True,
) -> dict[str, object]:
    root = Path(repo or installed_source_root()).expanduser().resolve()
    remote = remote or "origin"
    branch = branch or "main"
    remote_ref = f"{remote}/{branch}"
    payload: dict[str, object] = {
        "ok": False,
        "repo": str(root),
        "remote": remote,
        "branch": branch,
        "remote_ref": remote_ref,
        "install_kind": "source",
        "update_available": False,
        "can_apply": False,
        "guidance": package_upgrade_guidance(),
    }

    if not root.exists():
        payload.update({
            "install_kind": "missing",
            "message": f"{root} does not exist.",
        })
        return payload

    inside = git_capture(root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        payload.update({
            "install_kind": "package",
            "message": f"{root} is not a Git checkout.",
        })
        return payload

    if fetch:
        fetched = git_capture(root, ["fetch", "--quiet", remote])
        if fetched.returncode != 0:
            payload["message"] = f"Could not fetch {remote}: {_message(fetched)}"
            return payload

    head = git_capture(root, ["rev-parse", "--short", "HEAD"])
    remote_check = git_capture(root, ["rev-parse", "--verify", "--quiet", remote_ref])
    if remote_check.returncode != 0:
        payload["message"] = f"Could not find {remote_ref}."
        return payload

    behind = _git_count(root, f"HEAD..{remote_ref}")
    ahead = _git_count(root, f"{remote_ref}..HEAD")
    if behind is None or ahead is None:
        payload["message"] = "Could not compare local HEAD with the remote branch."
        return payload

    status = git_capture(root, ["status", "--porcelain"])
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else True
    payload.update({
        "ok": True,
        "install_kind": "source",
        "current": head.stdout.strip() or "unknown",
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "update_available": behind > 0,
        "can_apply": behind > 0 and ahead == 0 and not dirty,
    })
    if behind == 0:
        payload["message"] = "Already up to date."
    elif ahead:
        payload["message"] = f"{behind} update(s) available, but this checkout has {ahead} local commit(s)."
    elif dirty:
        payload["message"] = f"{behind} update(s) available, but the working tree has local changes."
    else:
        payload["message"] = f"{behind} update(s) available."
    return payload


def apply_updates(
    *,
    repo: str | Path | None = None,
    remote: str = "origin",
    branch: str = "main",
    fetch: bool = True,
) -> dict[str, object]:
    status = check_for_updates(repo=repo, remote=remote, branch=branch, fetch=fetch)
    if not status.get("ok"):
        return status
    if not status.get("update_available"):
        status.update({"applied": False, "restart_required": False})
        return status
    if not status.get("can_apply"):
        status.update({"ok": False, "applied": False})
        return status

    root = Path(str(status["repo"]))
    pulled = git_capture(root, ["pull", "--ff-only", str(status["remote"]), str(status["branch"])])
    if pulled.returncode != 0:
        status.update({
            "ok": False,
            "applied": False,
            "message": f"Update failed: {_message(pulled)}",
        })
        return status

    refreshed = check_for_updates(repo=root, remote=str(status["remote"]), branch=str(status["branch"]), fetch=False)
    refreshed.update({
        "applied": True,
        "restart_required": True,
        "output": pulled.stdout.strip() or "Fast-forwarded to the latest version.",
        "message": "Updated. Restart AIWatcher so the dashboard and Companion use the new code.",
    })
    return refreshed
