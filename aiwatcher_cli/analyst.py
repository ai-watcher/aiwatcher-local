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
from pathlib import Path
from typing import Any

# Measured, not chosen: a real analyst run took 29.9s wall / 26.2s to first
# token. The spec's 12s would time out every single call. Stage 2 never blocks
# the user -- Zone A streams in beneath a complete Zone B -- so a longer ceiling
# costs patience, not usability, where a short one costs the whole feature.
TIMEOUT_SECONDS = 45.0

# The sandbox lives inside the project so the path is stable and reviewable.
# Note this does NOT survive scanner._normalize_project_path, which folds any
# path inside a repo back to its git root -- see is_analyst_cwd and the raw cwd
# the scanner now keeps alongside the normalised project.
SANDBOX_PARTS = (".aiwatcher", "analyst")
SANDBOX_SUFFIX = "/".join(SANDBOX_PARTS)

# The small, fast tier. Structured extraction does not need the expensive one,
# and the gate exists to keep this cheap.
DEFAULT_MODEL = "haiku"

# How many repository paths to show the analyst. Ranked by commit activity by
# the caller; the cap is what keeps the prompt (and so the cost) bounded.
MAX_PATHS = 200

MAX_CONCURRENT = 1


class AnalystUnavailable(Exception):
    """Stage 2 cannot run. Zones B and C are always still complete."""


# --- detection ---------------------------------------------------------------

# Cached per machine rather than per call: `claude --version` costs about a
# second and the answer only changes when the CLI is upgraded.
_DETECTION_CACHE: dict[str, Any] | None = None


def _cache_path() -> Path:
    home = Path(os.environ.get("AIWATCHER_HOME", Path.home() / ".aiwatcher")).expanduser()
    return home / "analyst-detection.json"


def detect(*, refresh: bool = False) -> dict[str, Any]:
    """Which agent CLI can host the analyst, if any.

    Returns `available: False` with a reason rather than raising, because
    "no agent CLI found" is a Settings line, not an error.
    """
    global _DETECTION_CACHE
    if _DETECTION_CACHE is not None and not refresh:
        return _DETECTION_CACHE
    if not refresh:
        try:
            cached = json.loads(_cache_path().read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("probed_version"):
                _DETECTION_CACHE = cached
                return cached
        except (OSError, ValueError):
            pass
    result = _probe()
    _DETECTION_CACHE = result
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        # A cache that cannot be written is not worth failing detection over.
        pass
    return result


def _probe() -> dict[str, Any]:
    executable = shutil.which("claude")
    if not executable:
        return {
            "available": False,
            "cli": None,
            "reason": "Second opinion: unavailable, no agent CLI found",
        }
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [executable, "--version"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "available": False, "cli": "claude-code", "reason": f"Could not run the agent CLI: {exc}",
        }
    if proc.returncode != 0:
        return {
            "available": False, "cli": "claude-code",
            "reason": "Your agent CLI returned an error when asked for its version.",
        }
    return {
        "available": True,
        "cli": "claude-code",
        "executable": executable,
        "probed_version": (proc.stdout or "").strip()[:120],
        "reason": "",
    }


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
        model: str = DEFAULT_MODEL,
        timeout: float = TIMEOUT_SECONDS,
        detection: dict[str, Any] | None = None,
        runner: Any = None) -> dict[str, Any]:
    """Spawn one analyst and return a validated result, or an unavailable state.

    Never raises for an analyst that misbehaves -- every failure in spec 8's
    matrix comes back as `available: False` with a reason, because Zones B and C
    are complete and there is no state in which a Plan run returns nothing.
    """
    found = detection if detection is not None else detect()
    if not found.get("available"):
        return {"available": False, "reason": found.get("reason") or "Second opinion unavailable."}

    sandbox = sandbox_dir(project_root)
    try:
        sandbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"available": False, "reason": f"Second opinion unavailable. {exc}"}

    text = build_prompt(prompt, paths)
    env = dict(os.environ)
    # Not read by the CLI -- it is a marker for anyone reading a process list,
    # and a second signal beside the sandbox path. The path is what the ledger
    # actually matches on, because that is what survives into the session log.
    env["AIWATCHER_ROLE"] = "analyst"
    argv = [found.get("executable") or "claude", "-p",
            "--output-format", "json", "--model", model]

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
                "reason": "Second opinion unavailable. Your agent CLI returned an error.",
                "stderr": (proc.stderr or "")[:2000]}

    envelope, result_text = _unwrap(proc.stdout or "")
    analysis, reason = validate(result_text, paths)
    if analysis is None:
        # Spec 3.4 rule 4: the raw response goes to the local debug log so a
        # malformed analyst can be diagnosed, and the block is dropped whole.
        return {"available": False, "reason": reason, "raw": (proc.stdout or "")[:4000]}

    return {
        "available": True,
        "analysis": analysis,
        "cost_usd": envelope.get("total_cost_usd"),
        "tokens": _total_tokens(envelope.get("usage") or {}),
        "session_id": envelope.get("session_id"),
        "model": model,
        "cli": found.get("cli"),
        "duration_ms": elapsed_ms,
    }


def _spawn(argv: list[str], text: str, cwd: Path, env: dict[str, str],
           timeout: float) -> subprocess.CompletedProcess[str]:
    # The prompt goes in on stdin, never in argv: it would otherwise land in the
    # process list and in shell history. This is also the only shape that works
    # -- `-p` is a flag, so a path handed to it is read as the prompt itself.
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, input=text, capture_output=True, text=True,
        cwd=str(cwd), env=env, timeout=timeout, encoding="utf-8", errors="replace",
    )


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
