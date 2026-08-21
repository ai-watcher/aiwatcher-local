"""Word lists and lexical signals read out of a developer's prompt.

Spec sections 2.1 and 2.2. These live in a config file rather than in code so
that tuning does not need a release: ship the defaults below, and let a machine
override them at ``$AIWATCHER_HOME/prompt-signals.json``.

Two callers share this module deliberately. The execution brief needs to know
whether a removal is the stated goal, so it stops telling an agent to avoid the
cleanup the user just asked for (spec 4.1). The risk scorer needs the same verbs
plus the sensitivity list to score blast radius (spec 2). Detecting destructive
intent twice, in two places, with two lists that drift apart, is how the brief
and the score end up disagreeing about the same prompt.

Nothing here reads a file or calls anything. It is lexical, local and free --
the cheap half of the gate that decides whether the expensive half runs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Spec 2.1. Present tense; matching is done on word boundaries against a
# lowercased prompt, and the two-word entries are matched as phrases.
DESTRUCTIVE_VERBS: tuple[str, ...] = (
    "delete", "remove", "drop", "purge", "truncate", "wipe", "clear",
    "rewrite", "replace", "rip out", "tear out", "migrate", "rename",
    "move", "consolidate", "collapse", "deprecate", "retire", "sunset",
    "revert", "roll back", "reset", "regenerate", "overwrite",
)

# Spec 3.3 defines `removals` narrowly -- "anything the prompt asks to delete,
# remove, drop or replace" -- which is a subset of 2.1. The wider list scores
# destructive intent; this one decides what gets listed as a requested removal.
# They are not the same question: "rewrite the billing module" is destructive
# enough to score, but rendering it under "Requested removals" would tell the
# agent to delete a module the user asked to rewrite.
REMOVAL_VERBS: tuple[str, ...] = (
    "delete", "remove", "drop", "replace", "purge", "wipe",
    "rip out", "tear out", "truncate",
)

# Spec 2.2.
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "billing", "invoice", "charge", "payment", "pricing", "subscription",
    "refund", "ledger", "auth", "login", "signup", "session", "token",
    "credential", "secret", "key", "password", "oauth", "permission",
    "role", "acl", "iam", "schema", "migration", "database", "prod",
    "production", "deploy", "release", "rollout", "dns", "infra",
    "terraform", "pii", "gdpr", "audit",
)

BREADTH_WORDS: tuple[str, ...] = (
    "all", "every", "entire", "across", "codebase", "repo-wide",
)

_CONFIG_KEYS = {
    "destructive_verbs": DESTRUCTIVE_VERBS,
    "removal_verbs": REMOVAL_VERBS,
    "sensitive_keywords": SENSITIVE_KEYWORDS,
    "breadth_words": BREADTH_WORDS,
}


def config_path() -> Path:
    override = os.environ.get("AIWATCHER_PROMPT_SIGNALS_FILE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("AIWATCHER_HOME", Path.home() / ".aiwatcher")).expanduser()
    return home / "prompt-signals.json"


def load_word_lists() -> dict[str, tuple[str, ...]]:
    """Defaults, with any machine-local overrides layered on top.

    A malformed or unreadable file falls back to the defaults rather than
    failing the Plan run: a tuning file is not worth breaking the product over,
    and the score it produces is reported as observed either way.
    """
    lists = {key: tuple(value) for key, value in _CONFIG_KEYS.items()}
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return lists
    if not isinstance(raw, dict):
        return lists
    for key in lists:
        value = raw.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            cleaned = tuple(item.strip().lower() for item in value if item.strip())
            if cleaned:
                lists[key] = cleaned
    return lists


def _match_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """Terms present in *text*, on word boundaries, in the order given.

    Word boundaries matter more than they look: "key" would otherwise fire on
    "monkey" and "keyboard", and "all" on "finally" and "install", which is
    exactly the kind of false positive that makes a score untrustworthy.
    """
    found: list[str] = []
    for term in terms:
        pattern = r"\b" + r"\s+".join(re.escape(part) for part in term.split()) + r"\b"
        if re.search(pattern, text):
            found.append(term)
    return found


def scan_prompt(prompt: str, *, word_lists: dict[str, tuple[str, ...]] | None = None) -> dict[str, Any]:
    """Every lexical signal this module can see, with no scoring applied.

    Scoring lives with the scorer; this returns what was observed so the brief
    and the score read the same facts.
    """
    lists = word_lists or load_word_lists()
    text = (prompt or "").lower()
    words = re.findall(r"[a-z0-9']+", text)
    return {
        "destructive_verbs": _match_terms(text, lists["destructive_verbs"]),
        "sensitive_keywords": _match_terms(text, lists["sensitive_keywords"]),
        "breadth_words": _match_terms(text, lists["breadth_words"]),
        "word_count": len(words),
        # Spec 2: a very short change request carries a point, because there is
        # not enough of it to say what "done" means.
        "terse": len(words) < 12,
    }


# A removal the prompt states as its goal, rather than one an agent might infer.
# "and delete the legacy adapter" is the goal; "without deleting anything" is
# not, and neither is "the deleted column" describing existing state.
_NEGATION_BEFORE = re.compile(
    r"\b(do not|don't|never|without|avoid|except|other than|rather than|instead of)\b[^.;]{0,40}$"
)


def requested_removals(prompt: str, *, word_lists: dict[str, tuple[str, ...]] | None = None) -> list[dict[str, Any]]:
    """Removals the prompt asks for, as {what, requested, verb}.

    Local and lexical, so it is available at M1/M2 -- before any analyst exists
    to fill in spec 3.3's richer `removals` array. The shape matches that schema
    so the render does not have to care which produced it, and so the analyst
    can replace this without the brief changing.
    """
    text = (prompt or "").strip()
    if not text:
        return []
    lists = word_lists or load_word_lists()
    lowered = text.lower()
    removals: list[dict[str, Any]] = []
    seen: set[str] = set()

    for verb in lists["removal_verbs"]:
        pattern = r"\b" + r"\s+".join(re.escape(part) for part in verb.split()) + r"\b"
        for match in re.finditer(pattern, lowered):
            preceding = lowered[:match.start()]
            if _NEGATION_BEFORE.search(preceding):
                continue
            # The object of the verb: up to the next clause boundary.
            tail = text[match.end():]
            obj = re.split(r"[.;,\n]|\band\b|\bthen\b|\bso that\b", tail, maxsplit=1)[0].strip()
            obj = re.sub(r"^(the|a|an|any|all|every)\s+", "", obj, flags=re.IGNORECASE).strip()
            if not obj or len(obj) > 120:
                continue
            key = obj.lower()
            if key in seen:
                continue
            seen.add(key)
            removals.append({"what": obj, "requested": True, "verb": verb})
    return removals[:10]
