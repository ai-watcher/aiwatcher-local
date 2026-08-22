"""Stage 2 of the Plan brief: a second opinion from the user's own agent.

Stage 1 (`prompt_signals`) is lexical, local and free, and decides whether this
runs at all. Nothing here executes unless `score_blast_radius` reaches the gate.
A product whose thesis is "you are overspending" cannot spend on every keystroke.

The analyst is a throwaway sibling process, not a call of ours: the user's own
CLI, their own key, on their machine. It is given the prompt and a list of file
paths, and asked to describe the task rather than do it.

Everything in here was verified against the installed CLIs rather than taken
from the spec, and three of the spec's assumptions did not survive that:

1. `claude -p <file>` does not read the file. `-p/--print` is a boolean flag, so
   the path is passed as the prompt text and the analyst confidently describes
   the string "prompt.txt". It exits 0. The prompt goes on **stdin**, which also
   satisfies the requirement that it never appear in argv or a process list.
2. The model wraps its JSON in a ```json fence even when told not to, so the
   fence has to be stripped before parsing.
3. A 12 second timeout kills every run. Measured on claude 2.1.221 with
   `--model haiku`: 30s wall, 26s to first token, because the CLI loads ~34k
   tokens of its own context before it sees the prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Measured, and the measurements are not close together. The same prompt, on the
# same machine and the same small tier, has come back in 17s and in 206s -- the
# slow one spent 200s of that before its first token. Codex swings the same way:
# 17s once, 196s twice an hour later.
#
# So this number cannot be a latency budget, because there is no latency to
# budget for; it is only a bound on a hang. It is set generously on purpose. A
# ceiling that trips during an ordinary slow spell does not save anything -- the
# analyst has already consumed the tokens by then, so an early kill pays for the
# work and throws the answer away. Stage 2 never blocks the user either: zones B
# and C are complete and on screen throughout.
#
# The spec's 12s would have timed out every call ever made.
TIMEOUT_SECONDS = 240.0

# The sandbox lives inside the project so the path is stable and reviewable.
# Note this does NOT survive scanner._normalize_project_path, which folds any
# path inside a repo back to its git root -- see is_analyst_cwd and the raw cwd
# the scanner now keeps alongside the normalised project.
SANDBOX_PARTS = (".aiwatcher", "analyst")
SANDBOX_SUFFIX = "/".join(SANDBOX_PARTS)

# Which agent can host the analyst, and how each one has to be driven.
#
# Spec 3.1 says to pick the vendor the user is about to prompt, because that is
# the one installed and authenticated, and that vendor's small, fast tier --
# structured extraction does not need the expensive one, and the gate exists to
# keep this cheap.
#
# Both shapes here were run against the installed CLIs, not read off the spec.
# Neither of the spec's two invocations works: `-p` is a boolean flag and
# `codex exec`'s prompt is positional, so a file path handed to either is taken
# as the prompt itself. Both take the prompt on stdin instead.


@dataclass(frozen=True)
class Host:
    key: str
    label: str
    executable: str
    default_model: str
    # Whether the CLI tells us what the run cost in a machine-readable way.
    # Claude Code returns total_cost_usd; Codex returns nothing usable, which is
    # why the monthly ceiling is enforced on run count as well as on dollars.
    reports_cost: bool
    # Whether the CLI validates the response against a schema itself. Codex
    # does, via --output-schema, which is why its answers never arrive fenced.
    enforces_schema: bool


HOSTS: tuple[Host, ...] = (
    Host("claude-code", "Claude Code", "claude", "haiku",
         reports_cost=True, enforces_schema=False),
    Host("codex-cli", "Codex", "codex", "gpt-5.4-mini",
         reports_cost=False, enforces_schema=True),
)
HOSTS_BY_KEY = {host.key: host for host in HOSTS}

# The Plan screen's tool dropdown, mapped to whichever host can serve it. Cursor
# and the generic option have no analyst of their own yet, so they fall through
# to whatever is installed rather than claiming to be unavailable.
TOOL_TO_HOST = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
}

DEFAULT_MODEL = HOSTS[0].default_model

# How many repository paths to show the analyst. Ranked by commit activity by
# the caller; the cap is what keeps the prompt (and so the cost) bounded.
MAX_PATHS = 200

MAX_CONCURRENT = 1


class AnalystUnavailable(Exception):
    """Stage 2 cannot run. Zones B and C are always still complete."""


# --- detection ---------------------------------------------------------------

# Cached per machine rather than per call: asking a CLI for its version costs
# about a second each and the answer only changes when one is upgraded.
_DETECTION_CACHE: dict[str, Any] | None = None


def _cache_path() -> Path:
    home = Path(os.environ.get("AIWATCHER_HOME", Path.home() / ".aiwatcher")).expanduser()
    return home / "analyst-detection.json"


def detect(*, refresh: bool = False, tool: str | None = None,
           verify: bool = False) -> dict[str, Any]:
    """Which agent CLI will host the analyst, if any.

    Given the tool the user is about to prompt, prefer that vendor: it is the
    one they have installed and authenticated, and asking a different vendor for
    a second opinion on work the first one will do is a stranger thing to
    charge for. Falls back to whatever else is installed rather than declaring
    itself unavailable, because any analyst beats none.

    Returns `available: False` with a reason rather than raising -- "no agent
    CLI found" is a Settings line, not an error.
    """
    found = detect_all(refresh=refresh, verify=verify)
    preferred = TOOL_TO_HOST.get((tool or "").strip().lower())
    order = [preferred] if preferred else []
    order += [host.key for host in HOSTS if host.key != preferred]
    for key in order:
        entry = found.get(key) or {}
        if entry.get("available"):
            return {**entry, "preferred": key == preferred}
    # Nothing usable: report the preferred vendor's reason if it had one, since
    # that is the one the user was actually asking about.
    fallback = (found.get(preferred) if preferred else None) or next(iter(found.values()), {})
    return {
        "available": False,
        "cli": fallback.get("cli"),
        "reason": fallback.get("reason") or "Second opinion: unavailable, no agent CLI found",
    }


def detect_all(*, refresh: bool = False, verify: bool = False) -> dict[str, dict[str, Any]]:
    """Every known host and whether it can be used, keyed by host.

    Cheap by default. This is reached from the Plan gate, which runs on every
    preflight, and asking two CLIs for their version there cost a subprocess
    each and made an unrelated test time out. Installation is a path lookup;
    whether the CLI actually works is answered by running it, and a broken one
    already comes back as spec 8's "CLI returned an error" row.

    `verify=True` does the slow version probe, for Settings and for anywhere
    the version itself is the thing being reported.
    """
    global _DETECTION_CACHE
    if _DETECTION_CACHE is not None and not refresh:
        if not verify or all(entry.get("probed_version") or not entry.get("available")
                             for entry in _DETECTION_CACHE.values()):
            return _DETECTION_CACHE
    if not refresh:
        try:
            cached = json.loads(_cache_path().read_text(encoding="utf-8"))
            if isinstance(cached, dict) and all(key in cached for key in HOSTS_BY_KEY):
                if not verify or all(entry.get("probed_version") or not entry.get("available")
                                     for entry in cached.values()):
                    _DETECTION_CACHE = cached
                    return cached
        except (OSError, ValueError):
            pass
    result = {host.key: _probe(host, verify=verify) for host in HOSTS}
    _DETECTION_CACHE = result
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        # A cache that cannot be written is not worth failing detection over.
        pass
    return result


def _probe(host: Host, *, verify: bool = False) -> dict[str, Any]:
    executable = shutil.which(host.executable)
    if not executable:
        return {
            "available": False,
            "cli": host.key,
            "reason": f"Second opinion: unavailable, {host.label} is not installed",
        }
    base = {
        "available": True,
        "cli": host.key,
        "label": host.label,
        "executable": executable,
        "reason": "",
    }
    if not verify:
        return base
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [executable, "--version"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "cli": host.key,
                "reason": f"Could not run {host.label}: {exc}"}
    if proc.returncode != 0:
        return {"available": False, "cli": host.key,
                "reason": f"{host.label} returned an error when asked for its version."}
    return {**base, "probed_version": (proc.stdout or "").strip()[:120]}


# --- the prompt --------------------------------------------------------------

# `path` is required and nullable rather than optional. OpenAI strict structured
# output rejects the spec's version outright -- "'required' ... must include
# every key in properties. Missing 'path'" -- and keeping one schema for every
# CLI is worth more than matching the spec's punctuation.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "success_check", "scope_paths", "unresolved_nouns",
                 "removals", "ambiguities", "first_checkpoint", "confidence"],
    "properties": {
        "outcome": {"type": "string", "minLength": 1, "maxLength": 300},
        "success_check": {"type": "string", "minLength": 1, "maxLength": 300},
        "scope_paths": {"type": "array", "maxItems": 20,
                        "items": {"type": "string", "maxLength": 400}},
        "unresolved_nouns": {"type": "array", "maxItems": 10,
                             "items": {"type": "string", "maxLength": 80}},
        "removals": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["what", "requested", "path"],
            "properties": {
                "what": {"type": "string", "maxLength": 120},
                "requested": {"type": "boolean"},
                "path": {"type": ["string", "null"], "maxLength": 400},
            }}},
        "ambiguities": {"type": "array", "maxItems": 6,
                        "items": {"type": "string", "maxLength": 200}},
        "first_checkpoint": {"type": "string", "minLength": 1, "maxLength": 300},
        "confidence": {"enum": ["high", "medium", "low"]},
    },
}

# The first line is load-bearing. Without it a coding agent starts doing the
# work. Verified against claude 2.1.221 and codex 0.146.0: both described the
# task and neither called a tool (num_turns 1, permission_denials []). Re-run
# tests/test_analyst.py's prompt-shape test whenever the CLI or tier changes.
PROMPT_TEMPLATE = """You are a static analyst. You are NOT going to perform the task below. You are describing it.

You will be given a developer's prompt and a list of file paths from their repository.
You cannot read file contents and must not ask to.

Return ONLY a JSON object matching the schema below. No prose, no code fences, no preamble.

Rules:
- Every entry in scope_paths MUST appear verbatim in the PATHS list. Never invent a path.
- If a noun in the prompt has no plausible match in PATHS, put that noun in unresolved_nouns.
- removals is for anything the prompt asks to delete, remove, drop or replace.
  requested=true means the removal is the stated goal, not a side effect.
- ambiguities is for phrases with more than one reasonable reading. Return an empty array
  if the prompt is genuinely unambiguous. Do not invent ambiguity to fill the field.
- first_checkpoint is what to inspect and confirm before changing anything.
- confidence is "low" if more than half the prompt's concrete nouns are unresolved.

SCHEMA:
{schema}

PROMPT:
<<<
{prompt}
>>>

PATHS ({shown} of {total}, ranked by commit activity in the last 30 days):
{paths}
"""


def ranked_paths(cwd: str | None, paths: tuple[str, ...] | list[str],
                 *, days: int = 30) -> tuple[str, ...]:
    """Repository paths, most-recently-worked-on first.

    The list is capped before it reaches the analyst, so which 200 of a 5,000
    file repo it sees decides whether the answer names anything real. Recent
    commit activity is the best cheap proxy for "the part of the tree this
    prompt is probably about".

    One extra git call, and unlike prompt_signals.repo_paths this one is not on
    the hook path -- it only runs once the gate has already decided to spend.
    A git that fails leaves the order alone rather than failing the run.
    """
    ordered = list(paths)
    if not cwd or not ordered:
        return tuple(ordered)
    try:
        listed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(cwd), "log", f"--since={days}.days",
             "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if getattr(listed, "returncode", 1) != 0:
            return tuple(ordered)
        touched = [line.strip() for line in str(getattr(listed, "stdout", "") or "").splitlines()
                   if line.strip()]
    except Exception:
        return tuple(ordered)
    if not touched:
        return tuple(ordered)
    activity: dict[str, int] = {}
    for path in touched:
        activity[path] = activity.get(path, 0) + 1
    # Stable within an activity band, so an unchanged tree produces an unchanged
    # prompt -- which is what makes the result cache worth having.
    return tuple(sorted(ordered, key=lambda path: (-activity.get(path, 0), path)))


def build_prompt(prompt: str, paths: tuple[str, ...] | list[str]) -> str:
    shown = list(paths)[:MAX_PATHS]
    return PROMPT_TEMPLATE.format(
        schema=json.dumps(RESPONSE_SCHEMA),
        prompt=prompt.strip(),
        shown=len(shown),
        total=len(paths),
        paths="\n".join(shown) if shown else "(no indexed paths)",
    )


# --- validation --------------------------------------------------------------

# Rule 2 of spec 3.4, and the one bug class it names outright. One regex, and it
# can never ship again.
_OBJECT_STRINGIFIED = re.compile(r"\[object \w+\]")
_FENCE = re.compile(r"\A\s*```(?:json)?\s*|\s*```\s*\Z")

_REQUIRED = tuple(RESPONSE_SCHEMA["required"])
_CONFIDENCE_ORDER = ("high", "medium", "low")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value for item in _strings(item)]
    if isinstance(value, dict):
        return [item for sub in value.values() for item in _strings(sub)]
    return []


def validate(raw: str, allowed_paths: tuple[str, ...] | list[str]) -> tuple[dict[str, Any] | None, str]:
    """Spec 3.4, run in full before anything reaches the DOM.

    Returns `(object, "")` or `(None, reason)`. Never a partial object: a
    half-rendered block is worse than an honest unavailable state, because the
    reader cannot tell which half is missing.
    """
    text = _FENCE.sub("", (raw or "").strip())
    if not text:
        return None, "Second opinion unavailable."
    try:
        obj = json.loads(text)
    except ValueError:
        return None, "Second opinion unavailable."
    if not isinstance(obj, dict):
        return None, "Second opinion unavailable."

    missing = [key for key in _REQUIRED if key not in obj]
    if missing:
        return None, "Second opinion unavailable."
    extra = [key for key in obj if key not in RESPONSE_SCHEMA["properties"]]
    if extra:
        return None, "Second opinion unavailable."
    if not str(obj.get("outcome") or "").strip():
        return None, "Second opinion unavailable."
    if not str(obj.get("first_checkpoint") or "").strip():
        return None, "Second opinion unavailable."
    if obj.get("confidence") not in _CONFIDENCE_ORDER:
        return None, "Second opinion unavailable."
    for value in _strings(obj):
        if _OBJECT_STRINGIFIED.search(value):
            return None, "Second opinion unavailable."

    for key in ("scope_paths", "unresolved_nouns", "ambiguities", "removals"):
        if not isinstance(obj.get(key), list):
            return None, "Second opinion unavailable."

    # Rule 1. An invented path is the failure mode that matters here: the whole
    # value of the zone is that it names real files, so a plausible-looking
    # fabrication is worse than no answer.
    allowed = set(allowed_paths)
    claimed = [p for p in obj["scope_paths"] if isinstance(p, str)]
    known = [p for p in claimed if p in allowed]
    if claimed and len(known) * 2 < len(claimed):
        return None, "Second opinion unavailable."
    dropped = len(claimed) - len(known)
    obj["scope_paths"] = known
    if dropped:
        obj["confidence"] = _downgrade(obj["confidence"])
        obj["dropped_paths"] = dropped

    obj["removals"] = [item for item in obj["removals"]
                       if isinstance(item, dict) and "what" in item and "requested" in item]
    return obj, ""


def _downgrade(confidence: str) -> str:
    try:
        return _CONFIDENCE_ORDER[min(_CONFIDENCE_ORDER.index(confidence) + 1,
                                     len(_CONFIDENCE_ORDER) - 1)]
    except ValueError:
        return "low"


# --- the ledger marker -------------------------------------------------------

def sandbox_dir(project_root: str | os.PathLike[str]) -> Path:
    return Path(project_root).expanduser() / Path(*SANDBOX_PARTS)


def is_analyst_cwd(cwd: str | None) -> bool:
    """Whether a session's recorded working directory is an analyst sandbox.

    Read against the RAW cwd from the tool's own log, never the normalised
    project path: normalisation folds any path inside a repository back to its
    git root, so `<project>/.aiwatcher/analyst` arrives as `<project>` and the
    run becomes indistinguishable from the user's own work. Verified -- a real
    analyst run was billed into user spend before this existed.
    """
    if not cwd:
        return False
    normalised = str(cwd).replace("\\", "/").rstrip("/").lower()
    return normalised.endswith(SANDBOX_SUFFIX)


# --- running it --------------------------------------------------------------

def cache_key(prompt: str, tree_revision: str, project_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(prompt.encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(tree_revision.encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(project_id.encode("utf-8", "replace"))
    return digest.hexdigest()


def run(prompt: str, *, project_root: str | os.PathLike[str],
        paths: tuple[str, ...] | list[str],
        model: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
        detection: dict[str, Any] | None = None,
        tool: str | None = None,
        runner: Any = None) -> dict[str, Any]:
    """Spawn one analyst and return a validated result, or an unavailable state.

    Never raises for an analyst that misbehaves -- every failure in spec 8's
    matrix comes back as `available: False` with a reason, because zones B and C
    are complete and there is no state in which a Plan run returns nothing.
    """
    found = detection if detection is not None else detect(tool=tool)
    if not found.get("available"):
        return {"available": False, "reason": found.get("reason") or "Second opinion unavailable."}
    host = HOSTS_BY_KEY.get(str(found.get("cli") or ""), HOSTS[0])
    chosen_model = model or host.default_model

    sandbox = sandbox_dir(project_root)
    try:
        sandbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"available": False, "reason": f"Second opinion unavailable. {exc}"}

    text = build_prompt(prompt, paths)
    env = dict(os.environ)
    # Not read by either CLI -- it is a marker for anyone reading a process
    # list, and a second signal beside the sandbox path. The path is what the
    # ledger actually matches on, because that is what reaches the session log.
    env["AIWATCHER_ROLE"] = "analyst"

    executable = found.get("executable") or host.executable
    try:
        argv = _prepare(host, executable, chosen_model, sandbox)
    except OSError as exc:
        return {"available": False, "reason": f"Second opinion unavailable. {exc}"}

    started = time.monotonic()
    try:
        proc = (runner or _spawn)(argv, text, sandbox, env, timeout)
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "Second opinion timed out."}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"Second opinion unavailable. {exc}"}
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        return {"available": False,
                "reason": f"Second opinion unavailable. {host.label} returned an error.",
                "stderr": (proc.stderr or "")[:2000]}

    envelope, result_text = _read_result(host, proc, sandbox)
    analysis, reason = validate(result_text, paths)
    if analysis is None:
        # Spec 3.4 rule 4: the raw response goes to the local debug log so a
        # malformed analyst can be diagnosed, and the block is dropped whole.
        return {"available": False, "reason": reason,
                "raw": (result_text or proc.stdout or "")[:4000]}

    return {
        "available": True,
        "analysis": analysis,
        # None, not 0.0, when the CLI does not report it. "$0.00" would claim
        # the run was free, and for anyone on an API key it was not.
        "cost_usd": envelope.get("total_cost_usd") if host.reports_cost else None,
        "cost_reported": host.reports_cost,
        "tokens": _total_tokens(envelope.get("usage") or {}) if host.reports_cost else None,
        "session_id": envelope.get("session_id"),
        "model": chosen_model,
        "cli": host.key,
        "cli_label": host.label,
        "duration_ms": elapsed_ms,
    }


def _prepare(host: Host, executable: str, model: str, sandbox: Path) -> list[str]:
    """The argv for this host, plus any file it needs written first."""
    if host.key == "codex-cli":
        schema_file = sandbox / "schema.json"
        schema_file.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        answer_file = sandbox / "last.json"
        # A stale answer from a previous run would otherwise be read back as
        # this run's result the moment anything fails before it is rewritten.
        answer_file.unlink(missing_ok=True)
        return [
            executable, "exec",
            # The sandbox is not a git repository of its own, and read-only is
            # the tightest of Codex's sandbox policies: an analyst that is only
            # describing a task has no reason to be able to write anything.
            "--skip-git-repo-check", "-s", "read-only",
            "-m", model,
            # Codex validates the response against the schema itself, which is
            # why its answers never come back wrapped in a code fence.
            "--output-schema", str(schema_file),
            "-o", str(answer_file),
            # A bare "-" reads the prompt from stdin. Positionally it would put
            # the entire prompt in the process list.
            "-",
        ]
    return [executable, "-p", "--output-format", "json", "--model", model]


# Codex prints its session id to stderr and nowhere else. Worth keeping: it is
# what ties a line in the overhead ledger back to the analysis it produced.
_CODEX_SESSION_ID = re.compile(r"session id:\s*([0-9a-fA-F-]{8,})")


def _read_result(host: Host, proc: "subprocess.CompletedProcess[str]",
                 sandbox: Path) -> tuple[dict[str, Any], str]:
    """The CLI's own metadata, and the model's answer, however that CLI reports it."""
    if host.key == "codex-cli":
        envelope: dict[str, Any] = {}
        match = _CODEX_SESSION_ID.search(proc.stderr or "")
        if match:
            envelope["session_id"] = match.group(1)
        try:
            answer = (sandbox / "last.json").read_text(encoding="utf-8")
        except OSError:
            # Nothing written means nothing to validate. Falling back to stdout
            # would risk reading a progress line as if it were the answer.
            answer = ""
        return envelope, answer
    return _unwrap(proc.stdout or "")



# How long to wait for a killed process tree to actually go away before giving
# up on reaping it. Small: by this point the answer is already discarded.
KILL_GRACE_SECONDS = 5.0


def _spawn(argv: list[str], text: str, cwd: Path, env: dict[str, str],
           timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the analyst with a timeout that actually bounds it.

    `subprocess.run(timeout=...)` does not. On timeout it kills the direct
    child and then calls communicate() again to reap it, which blocks until the
    stdout pipe closes -- and the CLI is a launcher whose node grandchildren
    inherited that pipe. Measured before this was fixed: a 45s timeout returned
    after 3m54s, with the agent still running.

    So the whole tree gets killed, not just the child it launched, and the reap
    afterwards is itself bounded.

    The prompt goes in on stdin, never in argv: it would otherwise land in the
    process list and in shell history. That is also the only shape that works
    at all -- `-p` is a flag, so a path handed to it is read as the prompt.
    """
    creation: dict[str, Any] = {}
    if os.name == "nt":
        creation["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        creation["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=env, **creation,
    )
    try:
        out, err = proc.communicate(input=text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=KILL_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            # Reaping is best-effort once the tree is dead; the caller has
            # already been told this run produced nothing.
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _kill_tree(proc: "subprocess.Popen[str]") -> None:
    """Kill the analyst and everything it started.

    Killing only the direct child leaves the agent itself running -- it is
    launched through a wrapper -- so it keeps working, keeps spending, and keeps
    the pipe open.
    """
    if os.name == "nt":
        try:
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=KILL_GRACE_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _unwrap(stdout: str) -> tuple[dict[str, Any], str]:
    """The CLI's JSON envelope, and the model's own answer inside it."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return {}, stdout
    if not isinstance(envelope, dict):
        return {}, stdout
    return envelope, str(envelope.get("result") or "")


def _total_tokens(usage: dict[str, Any]) -> int:
    keys = ("input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens")
    return sum(int(usage.get(key) or 0) for key in keys)
