"""Privacy-safe command evidence extracted from local AI transcripts.

The parser uses command text and bounded tool output only in memory to classify
known verification runners and successful ``git commit`` calls. Persisted
records never contain command arguments, stdout, or stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SHELL_PUNCTUATION = {"&", "&&", "|", "||", ";", "<", ">", "(", ")"}
_CODEX_EXIT = re.compile(r"(?:Process exited with code\s+|Exit code:\s*)(-?\d+)", re.IGNORECASE)
_CODEX_RUNNING = re.compile(r"Process running with session ID\s+(\d+)", re.IGNORECASE)
_GIT_COMMIT_SHA = re.compile(r"^\[[^\]\r\n]+\s+([0-9a-f]{7,40})\]", re.MULTILINE)
VERIFICATION_SCOPES = {"project_default", "named_check", "targeted", "unknown"}
VERIFICATION_SCOPE_LABELS = {
    "project_default": "default project scope",
    "named_check": "named check",
    "targeted": "targeted or parameterized scope; target details not stored",
    "unknown": "scope unknown; do not infer project-wide coverage",
}


@dataclass(frozen=True)
class CommandEvidence:
    source_id: str
    session_id: str
    tool: str
    cwd: str | None
    runner: str | None
    command_kind: str
    started_at: str | None
    finished_at: str | None
    completion_state: str
    exit_code: int | None
    verification_scope: str = "unknown"
    commit_sha: str | None = None
    truncated: bool = False

    @property
    def status(self) -> str:
        if self.completion_state in {"pending", "background", "interrupted", "denied"}:
            return "incomplete"
        if self.completion_state != "completed":
            return "result unknown"
        if self.exit_code == 0:
            return "passed"
        if self.exit_code is not None:
            return "failed"
        return "result unknown"


def verification_classification(command: list[str]) -> tuple[str | None, str]:
    """Return an argument-free runner label and conservative scope enum."""
    if not command:
        return None, "unknown"
    names = [Path(part).name.lower() for part in command[:4]]
    first = names[0]
    if first in {"pytest", "py.test"}:
        return "pytest", "project_default" if len(command) == 1 else "targeted"
    if first in {"python", "python3", "py"} and len(command) >= 3 and command[1] == "-m":
        module = str(command[2]).lower()
        if module in {"pytest", "unittest"}:
            return f"python -m {module}", "project_default" if len(command) == 3 else "targeted"
    if first in {"npm", "pnpm", "yarn", "bun"}:
        action = str(command[1]).lower() if len(command) > 1 else ""
        script = str(command[2]).lower() if action == "run" and len(command) > 2 else action
        if script in {"test", "check", "lint", "build", "typecheck", "smoke"}:
            expected_length = 3 if action == "run" else 2
            scope = "named_check" if len(command) == expected_length else "targeted"
            return f"{first} {('run ' if action == 'run' else '')}{script}", scope
    if first in {"cargo", "go", "dotnet", "mvn", "mvnw", "gradle", "gradlew"}:
        action = str(command[1]).lower() if len(command) > 1 else ""
        if action in {"test", "check", "verify", "build"}:
            return f"{first} {action}", "named_check" if len(command) == 2 else "targeted"
    return None, "unknown"


def verification_runner(command: list[str]) -> str | None:
    """Compatibility wrapper returning only the fixed verifier label."""
    return verification_classification(command)[0]


def verification_scope_for_cwd(scope: str, cwd: str | None, checkout_path: str | None) -> str:
    normalized = scope if scope in VERIFICATION_SCOPES else "unknown"
    if normalized not in {"project_default", "named_check"}:
        return normalized
    if not cwd or not checkout_path:
        return "targeted"
    try:
        same_root = os.path.normcase(os.path.realpath(cwd)) == os.path.normcase(os.path.realpath(checkout_path))
    except (OSError, ValueError):
        return "targeted"
    return normalized if same_root else "targeted"


def verification_scope_label(scope: object) -> str:
    return VERIFICATION_SCOPE_LABELS.get(str(scope), VERIFICATION_SCOPE_LABELS["unknown"])


def _simple_commands(command: object) -> list[tuple[list[str], bool]] | None:
    if not isinstance(command, str) or not command.strip() or "\n" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens:
        return None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            if not commands[-1]:
                return None
            commands.append([])
            continue
        if token in _SHELL_PUNCTUATION or any(char in token for char in ";&|<>"):
            return None
        commands[-1].append(token)
    if not commands[-1]:
        return None
    prepared: list[tuple[list[str], bool]] = []
    for argv in commands:
        had_environment_assignment = False
        while argv and "=" in argv[0] and not argv[0].startswith(("/", "./", "../")):
            name, _, _ = argv[0].partition("=")
            if not name.replace("_", "a").isalnum() or name[:1].isdigit():
                break
            argv.pop(0)
            had_environment_assignment = True
        if not argv or Path(argv[0]).name.lower() in {"cd", "pushd", "popd"}:
            return None
        prepared.append((argv, had_environment_assignment))
    return prepared


def _is_git_commit(argv: list[str]) -> bool:
    if not argv or Path(argv[0]).name.lower() not in {"git", "git.exe"}:
        return False
    index = 1
    options_with_value = {"-c", "--git-dir", "--work-tree", "--namespace"}
    while index < len(argv):
        value = argv[index]
        if value == "commit":
            return True
        if value == "-C" or value.startswith("-C"):
            return False
        if value in options_with_value:
            index += 2
            continue
        if value.startswith("--git-dir=") or value.startswith("--work-tree=") or value in {"--no-pager", "--bare"}:
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return False
    return False


def _is_git_add(argv: list[str]) -> bool:
    return bool(
        len(argv) >= 2
        and Path(argv[0]).name.lower() in {"git", "git.exe"}
        and argv[1] == "add"
    )


def _command_classification(command: object) -> tuple[str | None, str | None, str]:
    commands = _simple_commands(command)
    if not commands:
        return None, None, "unknown"
    matches: list[tuple[str, str | None, str]] = []
    unmatched: list[list[str]] = []
    for argv, parameterized_by_environment in commands:
        runner, scope = verification_classification(argv)
        if runner:
            if parameterized_by_environment:
                scope = "targeted"
            matches.append(("verification", runner, scope))
        elif _is_git_commit(argv):
            matches.append(("git_commit", None, "unknown"))
        else:
            unmatched.append(argv)
    if unmatched:
        if not (
            len(matches) == 1
            and matches[0][0] == "git_commit"
            and all(_is_git_add(argv) for argv in unmatched)
        ):
            # An unrecognized setup command may alter what a later verifier
            # selects. Do not persist a misleading partial receipt.
            return None, None, "unknown"
    return matches[0] if len(matches) == 1 else (None, None, "unknown")


def _stamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _source_id(tool: str, session_id: str, call_id: str) -> str:
    raw = f"{tool}\0{session_id}\0{call_id}".encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(raw).hexdigest()[:32]


def _commit_sha(output: object) -> str | None:
    if not isinstance(output, str):
        return None
    match = _GIT_COMMIT_SHA.search(output[:32_768])
    return match.group(1) if match else None


def _evidence(
    pending: dict[str, Any],
    *,
    finished_at: str | None,
    completion_state: str,
    exit_code: int | None,
    output: object = None,
    truncated: bool = False,
) -> CommandEvidence:
    return CommandEvidence(
        source_id=_source_id(pending["tool"], pending["session_id"], pending["call_id"]),
        session_id=pending["session_id"],
        tool=pending["tool"],
        cwd=pending.get("cwd"),
        runner=pending.get("runner"),
        command_kind=pending["command_kind"],
        started_at=pending.get("started_at"),
        finished_at=finished_at,
        completion_state=completion_state,
        exit_code=exit_code,
        verification_scope=str(pending.get("verification_scope") or "unknown"),
        commit_sha=_commit_sha(output) if pending["command_kind"] == "git_commit" and exit_code == 0 else None,
        truncated=truncated,
    )


def _json_lines(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _claude_evidence(path: str, fallback_session_id: str) -> list[CommandEvidence]:
    pending: dict[str, dict[str, Any]] = {}
    results: list[CommandEvidence] = []
    seen_rows: set[str] = set()
    for row in _json_lines(path):
        row_id = str(row.get("uuid") or "")
        if row_id and row_id in seen_rows:
            continue
        if row_id:
            seen_rows.add(row_id)
        session_id = str(row.get("sessionId") or fallback_session_id)
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        if row.get("type") == "assistant":
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use" or item.get("name") != "Bash":
                    continue
                inputs = item.get("input") if isinstance(item.get("input"), dict) else {}
                command_kind, runner, verification_scope = _command_classification(inputs.get("command"))
                call_id = str(item.get("id") or "")
                if not call_id or not command_kind:
                    continue
                pending[call_id] = {
                    "call_id": call_id,
                    "session_id": session_id,
                    "tool": "claude-code",
                    "cwd": str(row.get("cwd") or "") or None,
                    "runner": runner,
                    "verification_scope": verification_scope,
                    "command_kind": command_kind,
                    "started_at": _stamp(row.get("timestamp")),
                }
            continue
        if row.get("type") != "user":
            continue
        outer_result = row.get("toolUseResult")
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            call_id = str(item.get("tool_use_id") or "")
            call = pending.pop(call_id, None)
            if call is None:
                continue
            interrupted = isinstance(outer_result, dict) and bool(
                outer_result.get("interrupted") or outer_result.get("timedOutAfterMs")
            )
            background = isinstance(outer_result, dict) and bool(outer_result.get("backgroundTaskId"))
            denied = bool(row.get("toolDenialKind"))
            is_error = item.get("is_error")
            if interrupted:
                state, exit_code = "interrupted", None
            elif background:
                state, exit_code = "background", None
            elif denied:
                state, exit_code = "denied", None
            elif is_error is False:
                state, exit_code = "completed", 0
            elif is_error is True:
                state, exit_code = "completed", 1
            else:
                state, exit_code = "result_unknown", None
            output = outer_result.get("stdout") if isinstance(outer_result, dict) else item.get("content")
            results.append(_evidence(
                call,
                finished_at=_stamp(row.get("timestamp")),
                completion_state=state,
                exit_code=exit_code,
                output=output,
                truncated=isinstance(outer_result, dict) and bool(
                    outer_result.get("persistedOutputPath") or outer_result.get("persistedOutputSize")
                ),
            ))
    for call in pending.values():
        results.append(_evidence(call, finished_at=None, completion_state="pending", exit_code=None))
    return results


def _codex_result(output: object) -> tuple[int | None, object]:
    if isinstance(output, dict):
        value = output.get("exit_code")
        return (int(value), output.get("output")) if isinstance(value, int) else (None, output.get("output"))
    if not isinstance(output, str):
        return None, None
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("exit_code"), int):
        return int(decoded["exit_code"]), decoded.get("output")
    match = _CODEX_EXIT.search(output[:8_192])
    return (int(match.group(1)), output) if match else (None, output)


def _codex_evidence(path: str, fallback_session_id: str) -> list[CommandEvidence]:
    pending: dict[str, dict[str, Any]] = {}
    async_commands: dict[str, dict[str, Any]] = {}
    polls: dict[str, tuple[str, dict[str, Any]]] = {}
    results: list[CommandEvidence] = []
    session_id = fallback_session_id
    cwd: str | None = None
    for row in _json_lines(path):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        row_type = row.get("type")
        if row_type == "session_meta":
            session_id = str(payload.get("id") or payload.get("session_id") or session_id)
            cwd = str(payload.get("cwd") or "") or cwd
            continue
        if row_type == "turn_context":
            cwd = str(payload.get("cwd") or "") or cwd
            continue
        if row_type != "response_item":
            continue
        payload_type = payload.get("type")
        if payload_type == "function_call" and payload.get("name") in {"exec_command", "shell_command"}:
            try:
                arguments = json.loads(payload.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            if not isinstance(arguments, dict):
                continue
            command_kind, runner, verification_scope = _command_classification(arguments.get("cmd") or arguments.get("command"))
            call_id = str(payload.get("call_id") or "")
            if not call_id or not command_kind:
                continue
            pending[call_id] = {
                "call_id": call_id,
                "session_id": session_id,
                "tool": "codex-cli",
                "cwd": str(arguments.get("workdir") or "") or cwd,
                "runner": runner,
                "verification_scope": verification_scope,
                "command_kind": command_kind,
                "started_at": _stamp(row.get("timestamp")),
            }
        elif payload_type == "function_call" and payload.get("name") == "write_stdin":
            try:
                arguments = json.loads(payload.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            async_id = str(arguments.get("session_id") or "") if isinstance(arguments, dict) else ""
            call_id = str(payload.get("call_id") or "")
            if async_id in async_commands and call_id:
                polls[call_id] = (async_id, async_commands[async_id])
        elif payload_type == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            poll = polls.pop(call_id, None)
            call = pending.pop(call_id, None) if poll is None else poll[1]
            if call is None:
                continue
            exit_code, output = _codex_result(payload.get("output"))
            raw_output = payload.get("output")
            running_match = _CODEX_RUNNING.search(raw_output[:8_192]) if isinstance(raw_output, str) else None
            if exit_code is None and running_match:
                async_commands[running_match.group(1)] = call
                continue
            if poll is not None and exit_code is None:
                continue
            if poll is not None:
                async_commands.pop(poll[0], None)
            state = "completed" if exit_code is not None else "result_unknown"
            results.append(_evidence(
                call,
                finished_at=_stamp(row.get("timestamp")),
                completion_state=state,
                exit_code=exit_code,
                output=output,
                truncated=isinstance(raw_output, str) and "Warning: truncated output" in raw_output,
            ))
    for call in pending.values():
        results.append(_evidence(call, finished_at=None, completion_state="pending", exit_code=None))
    for call in async_commands.values():
        results.append(_evidence(call, finished_at=None, completion_state="background", exit_code=None))
    return results


def command_evidence_for_session(session: object) -> list[CommandEvidence]:
    path = getattr(session, "source_path", None)
    session_id = str(getattr(session, "session_id", "") or "")
    tool = str(getattr(session, "tool", "") or "")
    if not isinstance(path, str) or not path or not session_id:
        return []
    if tool == "claude-code":
        observed = _claude_evidence(path, session_id)
    elif tool == "codex-cli":
        observed = _codex_evidence(path, session_id)
    else:
        observed = []
    deduped: list[CommandEvidence] = []
    seen: set[str] = set()
    for item in observed:
        if item.session_id != session_id:
            continue
        if item.source_id in seen:
            continue
        seen.add(item.source_id)
        deduped.append(item)
    return deduped


def command_evidence_coverage(session: object) -> str:
    """Describe whether this transcript exposes a structured shell lifecycle."""
    path = getattr(session, "source_path", None)
    tool = str(getattr(session, "tool", "") or "")
    if not isinstance(path, str) or not path:
        return "unavailable"
    if tool == "claude-code":
        return "structured"
    if tool != "codex-cli":
        return "unsupported"
    saw_structured = False
    saw_opaque_exec = False
    for row in _json_lines(path):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") != "response_item":
            continue
        if payload.get("type") == "function_call" and payload.get("name") in {
            "exec_command", "shell_command", "write_stdin",
        }:
            saw_structured = True
        if payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
            saw_opaque_exec = True
    if saw_opaque_exec:
        return "partial_opaque_codex_exec" if saw_structured else "opaque_codex_exec"
    return "structured" if saw_structured else "unavailable"


def environment_session_identity(explicit: str | None = None) -> tuple[str | None, str | None]:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:120], "explicit_cli"
    for name, source in (
        ("AIWATCHER_SESSION_ID", "aiwatcher_environment"),
        ("CODEX_THREAD_ID", "codex_thread_environment"),
        ("CODEX_SESSION_ID", "codex_session_environment"),
    ):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()[:120], source
    return None, None
