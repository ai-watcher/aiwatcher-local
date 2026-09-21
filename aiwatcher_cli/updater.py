"""Update helpers for AIWatcher Local."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import Any, Sequence

from . import __version__

PACKAGE_NAME = "aiwatcher-local"
GITHUB_REPO = "ai-watcher/aiwatcher-local"
GITHUB_BRANCH = "main"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}"


def installed_source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def install_kind() -> str:
    """source" for a Git checkout, "package" for a pip or pipx install.

    A path check, not a git call, so the dashboard summary can carry it on
    every poll without touching git or the network.
    """
    return "source" if (installed_source_root() / ".git").exists() else "package"


def install_identity() -> dict[str, object]:
    """Describe the running AIWatcher install without touching git or GitHub."""
    return {
        "install_kind": install_kind(),
        "package_manager": package_manager(),
        "source_root": str(installed_source_root()),
        "version": __version__,
        "pid": os.getpid(),
        "process_cwd": str(Path.cwd().resolve()),
    }


def package_upgrade_guidance() -> list[dict[str, str]]:
    direct = _direct_url_metadata()
    spec = _package_spec(direct)
    return [
        {"label": "pipx", "command": f"pipx upgrade {PACKAGE_NAME}"},
        {"label": "pip", "command": f"python -m pip install --upgrade {spec}"},
        {"label": "uv tool", "command": f"uv tool upgrade {PACKAGE_NAME}"},
        {
            "label": "GitHub package install",
            "command": f"python -m pip install --upgrade git+https://github.com/{GITHUB_REPO}.git",
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


def _version_key(value: object) -> tuple[object, ...]:
    """Small dependency-free comparison key for the simple public versions.

    AIWatcher does not depend on `packaging`, and the updater must work from a
    freshly installed wheel. This is intentionally modest: it handles normal
    dotted versions and keeps pre-release suffixes deterministic enough for the
    dashboard's "newer than installed?" check.
    """
    parts: list[object] = []
    for piece in re.findall(r"\d+|[A-Za-z]+", str(value or "")):
        parts.append(int(piece) if piece.isdigit() else piece.lower())
    return tuple(parts)


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": f"AIWatcher/{__version__}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _direct_url_metadata() -> dict[str, Any] | None:
    try:
        dist = metadata.distribution(PACKAGE_NAME)
        raw = dist.read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _direct_url_commit(direct: dict[str, Any] | None) -> str | None:
    vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    return str(commit).strip() if commit else None


def _direct_url_revision(direct: dict[str, Any] | None) -> str:
    vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
    revision = vcs.get("requested_revision") if isinstance(vcs, dict) else None
    return str(revision).strip() if revision else GITHUB_BRANCH


def _package_spec(direct: dict[str, Any] | None = None) -> str:
    direct = direct if direct is not None else _direct_url_metadata()
    url = direct.get("url") if isinstance(direct, dict) else None
    vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
    if isinstance(url, str) and url and isinstance(vcs, dict) and vcs.get("vcs") == "git":
        revision = _direct_url_revision(direct)
        if not url.startswith("git+"):
            url = f"git+{url}"
        return f"{url}@{revision}"
    return PACKAGE_NAME


def package_manager() -> str:
    """Best-effort manager for the running package install."""
    prefix = str(Path(sys.prefix).resolve()).lower()
    executable = str(Path(sys.executable).resolve()).lower()
    if "pipx" in prefix or "pipx" in executable:
        return "pipx"
    if "uv" in prefix or "uv" in executable or os.environ.get("UV_TOOL_DIR"):
        return "uv"
    return "pip"


def _package_update_command(manager: str | None = None, direct: dict[str, Any] | None = None) -> list[str]:
    manager = manager or package_manager()
    direct = direct if direct is not None else _direct_url_metadata()
    spec = _package_spec(direct)
    if manager == "pipx" and shutil.which("pipx"):
        return ["pipx", "upgrade", PACKAGE_NAME]
    if manager == "uv" and shutil.which("uv"):
        return ["uv", "tool", "upgrade", PACKAGE_NAME]
    return [sys.executable, "-m", "pip", "install", "--upgrade", spec]


def _package_command_payload(command: Sequence[str]) -> dict[str, object]:
    return {
        "command": list(command),
        "command_text": shlex.join(list(command)),
    }


def _package_update_status(*, fetch: bool, branch: str) -> dict[str, object]:
    direct = _direct_url_metadata()
    manager = package_manager()
    command = _package_update_command(manager, direct)
    payload: dict[str, object] = {
        "ok": True,
        "install_kind": "package",
        "package_manager": manager,
        "version": __version__,
        "repo": None,
        "remote": "github",
        "branch": branch,
        "remote_ref": f"github/{branch}",
        "update_available": False,
        "can_apply": False,
        "guidance": package_upgrade_guidance(),
        **_package_command_payload(command),
    }

    if not fetch:
        payload["message"] = (
            f"AIWatcher {__version__} is installed as a package. "
            "Check for updates to compare it with the latest release."
        )
        return payload

    commit = _direct_url_commit(direct)
    if commit:
        compare = urllib.parse.quote(f"{commit}...{branch}", safe=".")
        try:
            data = _fetch_json(f"{GITHUB_API_BASE}/compare/{compare}")
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            payload.update({
                "ok": False,
                "error_code": "package_update_check_failed",
                "message": f"Could not check GitHub for package updates: {exc}",
            })
            return payload
        behind = int(data.get("ahead_by") or 0)
        commits = data.get("commits") if isinstance(data.get("commits"), list) else []
        latest = (commits[-1].get("sha") if commits and isinstance(commits[-1], dict) else data.get("sha")) or None
        payload.update({
            "installed_commit": commit[:12],
            "latest_commit": str(latest)[:12] if latest else None,
            "behind": behind,
            "update_available": behind > 0,
            "can_apply": behind > 0 and bool(command),
            "commits": [
                {
                    "sha": str(item.get("sha") or "")[:12],
                    "subject": str(((item.get("commit") or {}).get("message") or "").splitlines()[0])[:140],
                }
                for item in commits[:5]
                if isinstance(item, dict)
            ],
        })
        payload["message"] = (
            f"{behind} update(s) available for this package install."
            if behind
            else "Already up to date."
        )
        return payload

    try:
        data = _fetch_json(PYPI_JSON_URL)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        payload.update({
            "ok": False,
            "error_code": "package_update_check_failed",
            "message": f"Could not check PyPI for package updates: {exc}",
        })
        return payload
    latest_version = str((data.get("info") or {}).get("version") or "")
    update_available = bool(latest_version) and _version_key(latest_version) > _version_key(__version__)
    payload.update({
        "latest_version": latest_version or None,
        "behind": 1 if update_available else 0,
        "update_available": update_available,
        "can_apply": update_available and bool(command),
        "message": (
            f"AIWatcher {latest_version} is available for this package install."
            if update_available
            else "Already up to date."
        ),
    })
    return payload


def _fetch_failure_message(result: subprocess.CompletedProcess[str], remote: str) -> tuple[str, str]:
    """Turn Git transport failures into short, actionable UI copy.

    In particular, Git for Windows can inherit a restricted process token from
    the app that launched AIWatcher. Schannel then reports
    SEC_E_NO_CREDENTIALS even for this public repository; that is not a GitHub
    login problem and exposing the raw `fatal:` output sends users in the wrong
    direction.
    """
    detail = _message(result)
    lowered = detail.lower()
    if "sec_e_no_credentials" in lowered or "acquirecredentialshandle failed" in lowered:
        return (
            "windows_tls_restricted",
            "Windows blocked secure GitHub access for this AIWatcher process. "
            "Restart AIWatcher from a normal PowerShell or Command Prompt window, then check again.",
        )
    if (
        "could not resolve host" in lowered
        or "failed to connect" in lowered
        or "network is unreachable" in lowered
    ):
        return ("network_unavailable", "Could not reach GitHub. Check your connection, then try again.")
    if "authentication failed" in lowered or "could not read username" in lowered:
        return (
            "authentication_failed",
            f"Git could not authenticate with {remote}. Check that remote, then try again.",
        )
    return (
        "fetch_failed",
        f"Could not refresh updates from {remote}. Run `git fetch {remote}` in the checkout for details.",
    )


def check_for_updates(
    *,
    repo: str | Path | None = None,
    remote: str = "origin",
    branch: str = "main",
    fetch: bool = True,
) -> dict[str, object]:
    using_installed_source = repo is None
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

    # A wheel/pipx installation is complete and healthy; it simply has no local
    # checkout to fast-forward. Classify it before invoking Git so Windows paths
    # under site-packages never become error messages in the UI. Package installs
    # can still be compared against PyPI or, for direct GitHub installs, the
    # recorded commit in direct_url.json.
    if using_installed_source and install_kind() != "source":
        return _package_update_status(fetch=fetch, branch=branch)

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
            "message": "This AIWatcher installation is managed as a package. Use your installer to upgrade it.",
        })
        return payload

    if fetch:
        fetched = git_capture(root, ["fetch", "--quiet", remote])
        if fetched.returncode != 0:
            error_code, message = _fetch_failure_message(fetched, remote)
            payload.update({"error_code": error_code, "message": message})
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
    # `git pull` fast-forwards whatever is checked out. A contributor on an
    # already-merged feature branch, or a detached HEAD, would have that ref
    # moved onto origin/main, so applying is only offered on the tracked
    # branch itself. Detached HEAD reports no name and is treated as "not on
    # the branch".
    checked = git_capture(root, ["symbolic-ref", "--short", "-q", "HEAD"])
    checked_out = checked.stdout.strip() if checked.returncode == 0 else None
    on_branch = checked_out == branch
    payload.update({
        "ok": True,
        "install_kind": "source",
        "current": head.stdout.strip() or "unknown",
        "checked_out": checked_out,
        "on_branch": on_branch,
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "update_available": behind > 0,
        "can_apply": behind > 0 and on_branch and ahead == 0 and not dirty,
    })
    if not on_branch:
        where = f"on {checked_out}" if checked_out else "on a detached HEAD"
        if behind == 0:
            payload["message"] = (
                f"origin/{branch} is up to date, but this source checkout is {where}. "
                f"UI updates apply only while checked out on {branch}."
            )
        else:
            payload["message"] = f"{behind} update(s) available for {branch}, but this checkout is {where}."
    elif behind == 0:
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
    if repo is None and install_kind() != "source":
        status = check_for_updates(repo=repo, remote=remote, branch=branch, fetch=fetch)
        if not status.get("ok"):
            return status
        if not status.get("update_available"):
            status.update({"applied": False, "restart_required": False})
            return status
        if not status.get("can_apply"):
            status.update({"ok": False, "applied": False})
            return status
        command = status.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            status.update({
                "ok": False,
                "applied": False,
                "message": "Could not determine a package update command for this install.",
            })
            return status
        updated = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if updated.returncode != 0:
            status.update({
                "ok": False,
                "applied": False,
                "message": f"Package update failed: {_message(updated)}",
            })
            return status
        status.update({
            "ok": True,
            "applied": True,
            "restart_required": True,
            "output": updated.stdout.strip() or updated.stderr.strip() or "Package upgraded.",
            "message": "Updated. AIWatcher processes already running keep the old code until restarted.",
        })
        return status

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
        "message": "Updated. AIWatcher processes already running keep the old code until restarted.",
    })
    return refreshed
