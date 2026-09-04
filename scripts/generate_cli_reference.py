#!/usr/bin/env python3
"""Generate docs/CLI.md from the AIWatcher argparse command tree.

The root README stays focused on the developer's first run, so the exhaustive
command surface lives in docs/CLI.md. That file is generated, never edited by
hand -- a hand-maintained list drifts the moment someone adds a subparser.

Usage:
    python3 scripts/generate_cli_reference.py            # write docs/CLI.md
    python3 scripts/generate_cli_reference.py --check    # fail if stale (CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiwatcher_cli.cli import (  # noqa: E402
    DANGEROUS_COMMAND_PATTERNS,
    _mcp_tool_specs,
    build_parser,
)
from aiwatcher_cli.local_state import COMMAND_GATE_BLOCKED_DECISIONS  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "docs" / "CLI.md"

# Usage lines wrap here regardless of the terminal the generator runs in.
USAGE_WIDTH = 78

# Ordered command groups. Every subparser must appear in exactly one group;
# anything unlisted lands in "Other" and the generator warns, so a newly added
# command cannot silently go undocumented.
CATEGORIES: list[tuple[str, str, list[str]]] = [
    (
        "Getting started",
        "First run, detection, and health checks.",
        ["start", "setup", "update", "status", "doctor"],
    ),
    (
        "Daily loop",
        "What ran today, what it cost, and whether it was worth it.",
        ["today", "last", "timeline", "journal", "sessions", "changes", "commit-receipt",
         "outcome", "report", "tools", "projects"],
    ),
    (
        "Prompt review and launch",
        "Review work before execution, then launch the agent.",
        ["preflight", "codex", "claude"],
    ),
    (
        "Continuity",
        "Carry context between sessions and tools.",
        ["handoff", "resume", "open-session", "return-session", "log-decision"],
    ),
    (
        "Monitoring",
        "Catch expensive or looping work, and keep runtimes tidy.",
        ["watch", "companion", "statusline", "processes", "run"],
    ),
    (
        "Hooks and wrappers",
        (
            "Install or remove the integrations that let AIWatcher see prompts before "
            "they run. Every installer prints the change by default and only edits "
            "files when you pass `--write`."
        ),
        [
            "install-claude-hook",
            "uninstall-claude-hook",
            "install-claude-command-gate",
            "uninstall-claude-command-gate",
            "install-claude-activity-hook",
            "uninstall-claude-activity-hook",
            "install-claude-decision-log",
            "uninstall-claude-decision-log",
            "install-codex-hook",
            "uninstall-codex-hook",
            "install-cursor-hook",
            "uninstall-cursor-hook",
            "install-codex-wrapper",
            "uninstall-codex-wrapper",
            "install-commit-hook",
            "uninstall-commit-hook",
            "install-statusline",
            "uninstall-statusline",
            "hook-status",
        ],
    ),
    (
        "Data and integrations",
        "Get local evidence out, or plug AIWatcher into another surface.",
        ["export", "mcp", "ui"],
    ),
]

# Hand-written examples for commands whose invocation is not self-evident from
# the usage line alone. Optional -- a command with no entry simply renders
# without an example, so this never blocks a new command from being documented.
EXAMPLES: dict[str, list[str]] = {
    # Zero-argument commands get no example: it would be identical to the usage
    # line directly above it. Same for status, doctor, hook-status, tools, and
    # projects, which are already absent.
    "changes": [
        "aiwatcher changes --days 7",
        "aiwatcher changes --days 30 --repo my-service --limit 40",
    ],
    "commit-receipt": [
        "aiwatcher commit-receipt --sha HEAD~1",
        "aiwatcher commit-receipt --repo ../my-service --json",
    ],
    "install-statusline": [
        "aiwatcher install-statusline --write",
        "aiwatcher install-statusline --scope project --write",
    ],
    "install-commit-hook": [
        "aiwatcher install-commit-hook --write",
        "aiwatcher install-commit-hook --repo ../my-service --write",
    ],
    "sessions": [
        "aiwatcher sessions --days 7",
        "aiwatcher sessions --search <your-project> --days 30",
        "aiwatcher sessions --evidence churned --days 14",
    ],
    "last": ["aiwatcher last --session-id 4f2c1a9e"],
    "timeline": ["aiwatcher timeline --limit 50"],
    "outcome": [
        "aiwatcher outcome useful",
        'aiwatcher outcome rework --note "tests still failing after three prompts"',
    ],
    "journal": ["aiwatcher journal --days 7"],
    "report": ["aiwatcher report --days 7"],
    "preflight": [
        'aiwatcher preflight "refactor the whole auth module"',
        'aiwatcher preflight --text "delete unused migrations" --fail-on-high',
    ],
    "codex": ['aiwatcher codex "add a retry to the upload path"'],
    "claude": ['aiwatcher claude "add a retry to the upload path" --dry-run'],
    "handoff": ["aiwatcher handoff --target claude --copy"],
    "resume": [
        "aiwatcher resume --target codex --copy",
        "aiwatcher resume --search <your-project> --days 30",
    ],
    "open-session": [
        "aiwatcher open-session aiwatcher://session/<session-id>",
        "aiwatcher open-session <session-id> --no-open",
    ],
    "return-session": [
        "aiwatcher return-session <session-id>",
        "aiwatcher return-session aiwatcher://session/<session-id> --json",
    ],
    "log-decision": [
        'aiwatcher log-decision "Use SQLite over JSON files" \\\n'
        '    --reasoning "concurrent writes from the watcher" \\\n'
        '    --rejected "append-only JSON log"',
    ],
    "watch": ["aiwatcher watch --once", "aiwatcher watch --interval 30 --notify"],
    "processes": ["aiwatcher processes --stale-only"],
    "run": ["aiwatcher run -- npm test", "aiwatcher run -- pytest tests/"],
    "install-claude-hook": [
        "aiwatcher install-claude-hook              # print the change, write nothing",
        "aiwatcher install-claude-hook --write",
        "aiwatcher install-claude-hook --write --scope user --gate",
    ],
    "install-codex-hook": ["aiwatcher install-codex-hook --write"],
    "install-cursor-hook": ["aiwatcher install-cursor-hook --write"],
    "install-codex-wrapper": ["aiwatcher install-codex-wrapper --write --shell-rc ~/.bashrc"],
    "install-claude-activity-hook": [
        "aiwatcher install-claude-activity-hook                    # print, write nothing",
        "aiwatcher install-claude-activity-hook --write",
    ],
    "uninstall-claude-activity-hook": ["aiwatcher uninstall-claude-activity-hook --scope project"],
    "install-claude-command-gate": [
        "aiwatcher install-claude-command-gate                    # print, write nothing",
        "aiwatcher install-claude-command-gate --write --scope user",
    ],
    "install-claude-decision-log": ["aiwatcher install-claude-decision-log --write"],
    # Backing an integration out is the moment you least want to work out the
    # invocation, so every uninstall gets a copy-pasteable line of its own.
    # Claude hooks install to project scope by default, so a hook installed with
    # --scope user needs --scope user to come back out. Worth showing. The other
    # uninstallers take no argument worth demonstrating, so they get no example.
    "uninstall-claude-hook": ["aiwatcher uninstall-claude-hook --scope user"],
    "uninstall-claude-command-gate": ["aiwatcher uninstall-claude-command-gate --scope user"],
    "uninstall-codex-wrapper": ["aiwatcher uninstall-codex-wrapper --shell-rc ~/.bashrc"],
    "export": [
        "aiwatcher export --format json --days 30 > sessions.json",
        "aiwatcher export --format json --level events --since 2026-06-01",
    ],
    "ui": ["aiwatcher ui --port 9000 --restart"],
}

# Dispatched in main() ahead of argparse, so they never appear in `--help`.
# Documented because they show up in the settings files the installers write.
INTERNAL_COMMANDS: list[tuple[str, str]] = [
    ("claude-hook", "Claude Code UserPromptSubmit handler. Accepts `--text` and `--gate`."),
    ("codex-hook", "Codex prompt handler. Accepts `--text` and `--gate`."),
    ("cursor-hook", "Cursor prompt handler. Accepts `--text` and `--gate`."),
    ("claude-pretooluse-hook", "Claude Code PreToolUse dangerous-command gate. Accepts `--text` and `--gate`."),
    ("claude-activity-hook", "Claude Code Notification handler. Records that a session is waiting on you, and nothing it said."),
]

HEADER = """<!--
Generated by scripts/generate_cli_reference.py -- do not edit by hand.
Regenerate with: python3 scripts/generate_cli_reference.py
-->

# AIWatcher Local CLI Reference

Complete reference for every `aiwatcher` command. If you are setting up for the
first time, start with the [README](../README.md) instead -- it walks the first
run in order. This page is for lookup.

Run any command as `aiwatcher <command>` once installed, or
`python -m aiwatcher_cli <command>` from a clone.

Normal workflow commands read local history your AI tools already keep and send
nothing anywhere. Commands that contact GitHub or a configured reviewer say so
explicitly. See [Privacy](../README.md#privacy) for the full contract.
"""


def _iter_subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API for this
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def _subcommand_help(parser: argparse.ArgumentParser) -> dict[str, str]:
    """Map subcommand name -> help string, which lives on the action, not the parser."""
    helps: dict[str, str] = {}
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for choice in action._choices_actions:  # noqa: SLF001
                helps[choice.dest] = choice.help or ""
    return helps


def _format_default(action: argparse.Action) -> str:
    if action.default in (None, False) or action.default == []:
        return ""
    if action.default is argparse.SUPPRESS:
        return ""
    return f"`{action.default}`"


def _format_accepts(action: argparse.Action) -> str:
    if action.choices:
        return ", ".join(f"`{choice}`" for choice in action.choices)
    if isinstance(action, argparse._StoreTrueAction):  # noqa: SLF001
        return "flag"
    if isinstance(action, argparse._AppendAction):  # noqa: SLF001
        return "repeatable"
    # Custom validators like positive_int are callables, not the int builtin.
    type_name = getattr(action.type, "__name__", "")
    if action.type is int or "int" in type_name:
        return "integer"
    if action.type is float or "float" in type_name:
        return "number"
    if action.nargs == argparse.REMAINDER:
        return "everything after `--`"
    if action.nargs == "*":
        return "zero or more"
    return "text"


def _dangerous_command_pattern_table() -> str:
    """Built from DANGEROUS_COMMAND_PATTERNS so a new pattern cannot go undocumented."""
    lines = ["| Pattern id | What it flags |", "| --- | --- |"]
    for entry in DANGEROUS_COMMAND_PATTERNS:
        lines.append(f"| `{entry['id']}` | {entry['reason']} |")
    return "\n".join(lines)


def _mcp_tool_table() -> str:
    """Built from _mcp_tool_specs() so a new MCP tool cannot go undocumented."""
    lines = ["| Tool | Arguments | Description |", "| --- | --- | --- |"]
    for spec in _mcp_tool_specs():
        schema = spec.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        args = ", ".join(
            f"`{key}`*" if key in required else f"`{key}`" for key in properties
        ) or "none"
        lines.append(f"| `{spec['name']}` | {args} | {spec['description']} |")
    lines.append("")
    lines.append("`*` marks a required argument.")
    return "\n".join(lines)


def _notes() -> dict[str, str]:
    """Longer prose for commands whose behavior is not obvious from their flags.

    Hand-written, like EXAMPLES, but anything derived from the code (the
    pattern list, the blocked-decision set) is interpolated rather than typed
    out, so it tracks the implementation.
    """
    blocked = ", ".join(f"`{d}`" for d in sorted(COMMAND_GATE_BLOCKED_DECISIONS))
    return {
        "install-claude-command-gate": f"""**Scope: the `Bash` tool only.** The gate inspects shell commands the agent is
about to run. Tool calls that are not `Bash` are passed through untouched, so a
destructive change made through the `Write` or `Edit` tool is not covered by
this gate.

**It is a blocklist, not analysis.** A command is flagged only if it matches one
of these patterns:

{_dangerous_command_pattern_table()}

Anything else passes silently. A destructive command assembled through a shell
variable, an alias, or unusual quoting will not match. Treat this as a guardrail
against the common accident, not as a security boundary.

**On a match**, the gate opens a local decision screen — a browser page, falling
back to a terminal prompt when there is no display. Your answer is returned to
Claude Code using its own `allow` / `deny` vocabulary. Choosing **always allow**
for a pattern is remembered, and that pattern is allowed instantly from then on.

**It fails closed.** These decisions deny the command: {blocked}. That includes
the two you do not make yourself — a decision screen that opens but is never
answered before the timeout, and an environment with no display and no TTY,
which is resolved immediately rather than left to hang. Set
`AIWATCHER_COMMAND_GATE_DEFAULT=allow` to reverse the headless direction. The
practical consequence: running Claude Code somewhere headless, such as CI or a
container, means a matching command is denied by default rather than queued for
review.

**What is stored.** Command text is redacted before it is persisted or echoed
back: connection-string credentials, `*_SECRET` / `*_TOKEN` / `*_PASSWORD` /
`*_API_KEY` style assignments, secret-bearing flags, and token-shaped values are
replaced with `[redacted]`. AIWatcher keeps the redacted preview, the pattern
id, the decision, and a SHA-256 hash of the original command — not the raw
command text.""",
        "mcp": f"""Speaks the Model Context Protocol over stdin/stdout, so an MCP-capable client
can ask AIWatcher about local usage directly. Point your client at
`aiwatcher mcp` as the command; there is nothing to configure here.

It exposes these tools:

{_mcp_tool_table()}

Unlike the hooks, this is pull-based — the agent asks, rather than AIWatcher
intercepting. It covers surfaces where no prompt hook is available, but only
when the agent chooses to call it.""",
        "install-claude-hook": """`--gate` adds an interactive review step; it does not replace the hook's
default behavior. Without it, low-risk prompts pass unchanged, medium-risk
prompts get a scoped execution brief added alongside them, and high-risk prompts
are blocked. With it, medium- and high-risk prompts first open a local decision
screen — and if that screen times out or no display is available, AIWatcher
falls back to exactly the policy above, so nothing is skipped.""",
    }


def _format_usage(parser: argparse.ArgumentParser) -> str:
    """Usage line without the `usage: ` prefix, the noisy `-h`, or its indentation.

    Formats at a fixed width rather than calling parser.format_usage(), which
    wraps to the caller's terminal size. Otherwise the same commit regenerates
    differently on a wide terminal and the staleness check fails for a reason
    that has nothing to do with the CLI.
    """
    formatter = argparse.HelpFormatter(prog=parser.prog, width=USAGE_WIDTH)
    formatter.add_usage(parser.usage, parser._actions, parser._mutually_exclusive_groups)  # noqa: SLF001
    prefix = "usage: "
    raw = formatter.format_help().rstrip()
    dedented = [
        line[len(prefix):] if line.startswith(prefix) else line.removeprefix(" " * len(prefix))
        for line in raw.splitlines()
    ]
    return "\n".join(dedented).replace("[-h] ", "").replace(" [-h]", "")


def _render_command(name: str, parser: argparse.ArgumentParser, summary: str, note: str = "") -> str:
    lines = [f"### `aiwatcher {name}`", ""]
    if summary:
        lines.extend([summary, ""])

    lines.extend(["```sh", _format_usage(parser), "```", ""])

    # Examples sit directly under the usage line: for commands like `run`, whose
    # argparse usage renders as a bare `...`, the example is the only thing that
    # actually shows you how to call it.
    examples = EXAMPLES.get(name)
    if examples:
        lines.append("Example:" if len(examples) == 1 else "Examples:")
        lines.extend(["", "```sh", *examples, "```", ""])

    positionals = [
        a for a in parser._actions  # noqa: SLF001
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    ]
    options = [
        a for a in parser._actions  # noqa: SLF001
        if a.option_strings and not isinstance(a, argparse._HelpAction)  # noqa: SLF001
    ]

    if positionals:
        lines.extend(["| Argument | Accepts | Description |", "| --- | --- | --- |"])
        for action in positionals:
            lines.append(
                f"| `{action.dest}` | {_format_accepts(action)} | {(action.help or '').strip()} |"
            )
        lines.append("")

    if options:
        lines.extend(["| Option | Accepts | Default | Description |", "| --- | --- | --- | --- |"])
        for action in options:
            flags = ", ".join(f"`{flag}`" for flag in action.option_strings)
            lines.append(
                f"| {flags} | {_format_accepts(action)} | {_format_default(action)} "
                f"| {(action.help or '').strip()} |"
            )
        lines.append("")

    nested_subparsers = _iter_subparsers(parser)
    if nested_subparsers:
        nested_helps = _subcommand_help(parser)
        lines.extend(["Subcommands:", "", "| Subcommand | Purpose |", "| --- | --- |"])
        for sub_name, sub_parser in nested_subparsers.items():
            lines.append(f"| `{sub_name}` | {nested_helps.get(sub_name, '')} |")
        lines.append("")
        for sub_name, sub_parser in nested_subparsers.items():
            nested_summary = nested_helps.get(sub_name, "")
            lines.extend([f"#### `aiwatcher {name} {sub_name}`", ""])
            if nested_summary:
                lines.extend([nested_summary, ""])
            lines.extend(["```sh", _format_usage(sub_parser), "```", ""])
            nested_positionals = [
                a for a in sub_parser._actions  # noqa: SLF001
                if not a.option_strings and not isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
            ]
            nested_options = [
                a for a in sub_parser._actions  # noqa: SLF001
                if a.option_strings and not isinstance(a, argparse._HelpAction)  # noqa: SLF001
            ]
            if nested_positionals:
                lines.extend(["| Argument | Accepts | Description |", "| --- | --- | --- |"])
                for action in nested_positionals:
                    lines.append(
                        f"| `{action.dest}` | {_format_accepts(action)} | {(action.help or '').strip()} |"
                    )
                lines.append("")
            if nested_options:
                lines.extend(["| Option | Accepts | Default | Description |", "| --- | --- | --- | --- |"])
                for action in nested_options:
                    flags = ", ".join(f"`{flag}`" for flag in action.option_strings)
                    lines.append(
                        f"| {flags} | {_format_accepts(action)} | {_format_default(action)} "
                        f"| {(action.help or '').strip()} |"
                    )
                lines.append("")
            if not nested_positionals and not nested_options:
                lines.extend(["Takes no arguments.", ""])

    if not positionals and not options and not nested_subparsers:
        lines.extend(["Takes no arguments.", ""])

    if note:
        lines.extend([note, ""])

    return "\n".join(lines)


def render() -> str:
    parser = build_parser()
    subparsers = _iter_subparsers(parser)
    helps = _subcommand_help(parser)

    categorized = {name for _, _, names in CATEGORIES for name in names}
    uncategorized = sorted(set(subparsers) - categorized)
    if uncategorized:
        print(
            "warning: these commands are not in CATEGORIES and were filed under "
            f"'Other': {', '.join(uncategorized)}",
            file=sys.stderr,
        )

    notes = _notes()
    for label, mapping in (("EXAMPLES", EXAMPLES), ("NOTES", notes)):
        stale = sorted(set(mapping) - set(subparsers))
        if stale:
            print(
                f"warning: {label} has entries for commands that no longer exist: "
                f"{', '.join(stale)}",
                file=sys.stderr,
            )

    groups = list(CATEGORIES)
    if uncategorized:
        groups.append(("Other", "Not yet categorized in the generator.", uncategorized))

    parts = [HEADER, "## Contents", ""]
    for title, _, names in groups:
        anchor = title.lower().replace(" ", "-")
        parts.append(f"- [{title}](#{anchor}) -- {', '.join(f'`{n}`' for n in names)}")
    parts.append("- [Internal hook commands](#internal-hook-commands)")
    parts.append("")

    for title, blurb, names in groups:
        parts.append(f"## {title}\n")
        if blurb:
            parts.append(f"{blurb}\n")
        for name in names:
            subparser = subparsers.get(name)
            if subparser is None:
                print(f"warning: '{name}' is in CATEGORIES but not in the parser", file=sys.stderr)
                continue
            parts.append(_render_command(name, subparser, helps.get(name, ""), notes.get(name, "")))

    parts.append("## Internal hook commands\n")
    parts.append(
        "These are dispatched before argument parsing and are hidden from `--help`.\n"
        "You do not run them by hand -- the installers above write them into your\n"
        "tool's settings, and they are listed here so the entries in those files are\n"
        "recognizable.\n"
    )
    parts.append("| Command | Purpose |")
    parts.append("| --- | --- |")
    for name, purpose in INTERNAL_COMMANDS:
        parts.append(f"| `aiwatcher {name}` | {purpose} |")
    parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if docs/CLI.md is out of date instead of rewriting it",
    )
    args = arg_parser.parse_args()

    generated = render()

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"{OUTPUT_PATH} does not exist. Run: python3 scripts/generate_cli_reference.py", file=sys.stderr)
            return 1
        current = OUTPUT_PATH.read_text(encoding="utf-8")
        if current != generated:
            print(
                f"{OUTPUT_PATH} is out of date with the CLI. "
                "Run: python3 scripts/generate_cli_reference.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT_PATH} is up to date.")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(generated, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
