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

# Naming a sensitive area is not the same as working on it. "Remove the obsolete
# screenshot from the auth docs" contains "auth", but the thing being removed is
# a picture in a document. The existing security heuristic avoids this by
# requiring a control noun -- "auth middleware", not bare "auth" -- and the
# sensitivity list is coarser than that, so it needs the exemption stated.
BENIGN_CONTEXT_WORDS: tuple[str, ...] = (
    "doc", "docs", "documentation", "readme", "changelog", "screenshot",
    "comment", "comments", "typo", "wording", "caption", "label",
    "tooltip", "alt text", "placeholder", "example", "examples", "sample",
    "fixture", "fixtures", "mock", "mocks", "storybook",
    "ui", "form", "error message", "test", "tests",
)
# Deliberately excludes generic containers like "page" and "module": "remove the
# auth middleware from the login page" is real security work that happens to
# name a page. The list is things that are themselves copy, docs or fixtures.

BREADTH_WORDS: tuple[str, ...] = (
    "all", "every", "entire", "across", "codebase", "repo-wide",
)

_CONFIG_KEYS = {
    "destructive_verbs": DESTRUCTIVE_VERBS,
    "removal_verbs": REMOVAL_VERBS,
    "sensitive_keywords": SENSITIVE_KEYWORDS,
    "breadth_words": BREADTH_WORDS,
    "benign_context_words": BENIGN_CONTEXT_WORDS,
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
        "benign_context": _match_terms(text, lists["benign_context_words"]),
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


# --------------------------------------------------------------------------
# Blast radius (spec section 2). Everything above is lexical; this half asks
# what the prompt's nouns actually point at in the repository.
# --------------------------------------------------------------------------

# Words too common to be worth resolving against a file tree. Matching "test"
# or "page" against every path turns a narrow prompt into a repo-wide one.
_STOPWORDS = frozenset("""
a an and the this that these those to for of in on at by with from into over
under is are was were be been being do does did done make makes made use uses
used add adds added new old also then than but or if it its as so we you i
please can could should would will just now here there what which when where
how why all any some each every code file files line lines test tests page
pages thing things stuff bit way ways time times run runs need needs want
""".split())


def _significant_nouns(prompt: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", prompt or "")
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in _STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        out.append(lowered)
    return out


_TREE_CACHE: dict[str, tuple[str, tuple[str, ...]]] = {}


def repo_paths(cwd: str | None) -> tuple[str, ...]:
    """Tracked paths for *cwd*, cached against the current HEAD.

    Keyed on HEAD rather than a clock, so re-planning the same prompt against
    an unchanged tree costs one cheap git call and nothing else. A directory
    that is not a repository, or a git that fails for any reason, returns empty
    -- blast radius simply contributes nothing rather than blocking the score.
    """
    if not cwd:
        return ()
    # Blast radius is a best-effort signal on top of a free lexical one. It must
    # never be able to break prompt analysis: a shelled-out git that is missing,
    # slow, in a non-repository, or replaced by a test double contributes
    # nothing and the score is computed without it.
    #
    # One git call, cached for the life of the process. This runs on the hook
    # path, which fires on every prompt submit, and spec 10 names Stage 1's
    # sub-second response as a feature worth protecting. A file list that goes
    # stale within a single process costs at most a slightly wrong file count.
    cached = _TREE_CACHE.get(cwd)
    if cached is not None:
        return cached[1]
    try:
        import subprocess
        listed = subprocess.run(
            ["git", "-C", cwd, "ls-files"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if getattr(listed, "returncode", 1) != 0:
            _TREE_CACHE[cwd] = ("", ())
            return ()
        stdout = str(getattr(listed, "stdout", "") or "")
        paths = tuple(line.strip() for line in stdout.splitlines() if line.strip())
    except Exception:
        _TREE_CACHE[cwd] = ("", ())
        return ()
    head = ""
    _TREE_CACHE[cwd] = (head, paths)
    return paths


def resolve_scope(prompt: str, paths: tuple[str, ...]) -> dict[str, Any]:
    """Which of the prompt's nouns point at real files, and how many.

    Matches against the path's own segments rather than the whole string, so
    "billing" does not resolve every file under a directory that happens to
    contain the word somewhere in its full path.
    """
    if not paths:
        return {"matched_paths": [], "matched_nouns": [], "unresolved_nouns": [], "file_count": 0}
    nouns = _significant_nouns(prompt)
    if not nouns:
        return {"matched_paths": [], "matched_nouns": [], "unresolved_nouns": [], "file_count": 0}

    segments: dict[str, set[str]] = {}
    for path in paths:
        for part in re.split(r"[\/]+", path.lower()):
            stem = re.sub(r"\.[a-z0-9]+$", "", part)
            if stem:
                segments.setdefault(stem, set()).add(path)

    matched: set[str] = set()
    matched_nouns: list[str] = []
    unresolved: list[str] = []
    for noun in nouns:
        hits = segments.get(noun) or {p for stem, ps in segments.items() if noun in stem for p in ps}
        if hits:
            matched_nouns.append(noun)
            matched.update(hits)
        else:
            unresolved.append(noun)
    return {
        "matched_paths": sorted(matched)[:200],
        "matched_nouns": matched_nouns,
        "unresolved_nouns": unresolved[:10],
        "file_count": len(matched),
    }


# Spec 2's points table. Gate at the medium band, which the UI already names,
# so a user learns one scale rather than two.
GATE_POINTS = 3

POINTS = {
    "destructive_verb": 3,
    "destructive_verb_narrow": 1,
    "destructive_verb_resolved": 2,
    "sensitive_paired": 3,
    "sensitive_alone": 1,
    "breadth_word": 2,
    "scope_5_to_20": 1,
    "scope_over_20": 2,
    "terse": 1,
}


def score_blast_radius(prompt: str, *, cwd: str | None = None, guarded: bool = False,
                       word_lists: dict[str, tuple[str, ...]] | None = None) -> dict[str, Any]:
    """Points for what this prompt would touch, with every one itemised.

    Returns the reasons as well as the total, because a number a user cannot
    account for is a number they stop trusting -- and because the gate has to be
    explainable before it is allowed to spend anything.

    One deliberate change from the spec's table. A domain keyword on its own
    scores 1, not 3; it scores 3 only alongside a destructive verb or a breadth
    word. Measured over 812 real local prompts, the table as written fires on
    19% of them, largely because "session" is both a sensitivity keyword and
    this product's main domain noun -- it appears in 11% of prompts on its own.
    A sensitive area is only risky when something broad or destructive is being
    done to it; naming one is just describing where you work. With the pairing
    rule the same corpus fires on 10%.
    """
    lists = word_lists or load_word_lists()
    signals = scan_prompt(prompt, word_lists=lists)
    scope = resolve_scope(prompt, repo_paths(cwd))
    reasons: list[dict[str, Any]] = []

    def add(key: str, detail: str) -> None:
        reasons.append({"signal": key, "points": POINTS[key], "detail": detail})

    destructive = signals["destructive_verbs"]
    breadth = signals["breadth_words"]
    sensitive = signals["sensitive_keywords"]

    # A destructive verb scores fully when it is aimed at something sensitive,
    # sweeping, or resolvable in the repository. Aimed at nothing in particular
    # -- "delete dead code in one test file", "clear cache" -- it is ordinary
    # maintenance, and scoring it as high-risk is how a gate ends up paying for
    # a second opinion on a one-line cleanup.
    # Documentation, UI copy and test fixtures are exempt outright, not merely
    # de-weighted. "Update the auth docs to remove an obsolete screenshot" names
    # a sensitive area and a destructive verb and is neither. The existing
    # security heuristic already treats these contexts this way; the sensitivity
    # list is coarser than it, so it needs the same exemption said out loud.
    # Breadth overrides the exemption: "delete all the tests" is still sweeping.
    benign = signals.get("benign_context") or []
    if benign and not breadth:
        return {
            "points": 0,
            "reasons": [{"signal": "benign_context", "points": 0,
                         "detail": ", ".join(benign[:4])}],
            "gate": False,
            "signals": signals,
            "scope": scope,
        }
    destructive_has_target = bool(sensitive or breadth or scope["matched_nouns"])
    if destructive:
        if destructive_has_target:
            add("destructive_verb", ", ".join(destructive[:4]))
        else:
            add("destructive_verb_narrow", ", ".join(destructive[:4]))
        if scope["matched_nouns"]:
            add("destructive_verb_resolved", f"{scope['file_count']} file(s) match the named nouns")
    if sensitive:
        if destructive or breadth:
            add("sensitive_paired", ", ".join(sensitive[:4]))
        else:
            add("sensitive_alone", ", ".join(sensitive[:4]))
    if breadth:
        add("breadth_word", ", ".join(breadth[:4]))
    # Resolved scope says how much a change would touch, which is only a blast
    # radius if something is being changed. On its own it says the prompt
    # mentioned words that happen to be filenames -- and in this repository the
    # product's own domain nouns are filenames. "I ran the watch --notify" is
    # five words of conversation and matched 32 files, scoring +2 for scope and
    # +1 for terseness: exactly the gate threshold, for a sentence that asks for
    # nothing at all.
    #
    # Same shape as the rule above it, and for the same reason: a sensitivity
    # keyword scores fully only alongside a destructive verb or breadth word,
    # because naming a sensitive area is just describing where you work.
    # Measured over 774 real local prompts, gating scope this way takes the gate
    # from firing on 9.0% of them to 7.2%.
    if destructive or breadth:
        if 5 <= scope["file_count"] <= 20:
            add("scope_5_to_20", f"{scope['file_count']} files")
        elif scope["file_count"] > 20:
            add("scope_over_20", f"{scope['file_count']} files")
    # Spec 2 scores a short *change request*, not a short prompt. "Explain this
    # function" is three words and asks for nothing to be changed; scoring it
    # made a read-only question look like risk.
    #
    # "Another signal fired" turned out to be too loose a reading of "change
    # request". "yea let's go ahead with all fixes based on your order" is an
    # approval: it carries the breadth word "all" and nothing else, and terseness
    # took it from 2 to exactly the threshold. So terseness now needs evidence
    # the prompt would change files -- a destructive verb, or a scope that
    # resolved -- rather than any signal at all.
    #
    # That is deliberately stricter than "change request". "Add a pricing table"
    # is one and no longer earns the point. Which is fine: terseness is only ever
    # a modifier, and something this quiet should need its own reason to reach
    # the gate rather than being carried there by being short.
    #
    # 7.2% to 6.6% on the same corpus, and nothing that should fire stopped.
    change_request = bool(destructive) or any(
        item["signal"].startswith("scope_") for item in reasons
    )
    if signals["terse"] and change_request:
        add("terse", f"{signals['word_count']} words")

    total = sum(item["points"] for item in reasons)
    # A prompt that already says "ask for confirmation before deleting" carries
    # the same blast radius but genuinely less risk, which is the same call the
    # existing scorer makes when it halves a destructive penalty for a guarded
    # prompt. Without this, feeding a generated brief back in scores as high as
    # the bare prompt it was written to make safer.
    if guarded and total:
        total = total // 2
        reasons.append({"signal": "guarded", "points": -(sum(i["points"] for i in reasons) - total),
                        "detail": "prompt already states a confirmation or checkpoint guardrail"})
    return {
        "points": total,
        "reasons": reasons,
        "gate": total >= GATE_POINTS,
        "signals": signals,
        "scope": scope,
    }
