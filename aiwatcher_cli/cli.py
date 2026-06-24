#!/usr/bin/env python3
"""AIWatcher Local CLI.

Local-first, read-only, no network calls. This module is intentionally
standalone so it can later become the public `aiwatcher` package entrypoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable

from .pricing import is_subscription_model
from .scanner import LocalSession, discover_tools, scan_all, scan_all_events


CLOUD_URL = "https://www.getaiwatcher.com"


def money(value: float) -> str:
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def compact_int(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def compact_duration(seconds: int) -> str:
    if seconds <= 0:
        return "unknown"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, remaining = divmod(minutes, 60)
    return f"{hours}h {remaining}m" if remaining else f"{hours}h"


def short_path(path: str | None, max_len: int = 46) -> str:
    if not path:
        return "unknown"
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min).astimezone()


def format_full_date(value: datetime) -> str:
    return f"{value.strftime('%A, %B')} {value.day}, {value.year}"


def format_short_datetime(value: datetime) -> str:
    return f"{value.strftime('%b')} {value.day} {value.strftime('%H:%M')}"


def in_window(session: LocalSession, since: datetime) -> bool:
    stamp = session.updated_at or session.started_at
    return bool(stamp and stamp.astimezone() >= since)


def sessions_since(days: int) -> list[LocalSession]:
    since = datetime.now().astimezone() - timedelta(days=days)
    return [session for session in scan_all() if in_window(session, since)]


def summarize(sessions: Iterable[LocalSession]) -> dict[str, float | int]:
    rows = list(sessions)
    return {
        "sessions": len(rows),
        "tokens_in": sum(row.tokens_in for row in rows),
        "tokens_out": sum(row.tokens_out for row in rows),
        "cost_usd": sum(row.cost_usd for row in rows),
        "agent_calls": sum(row.agent_calls for row in rows),
        "tool_calls": sum(row.tool_calls for row in rows),
    }


def token_summary_label(sessions: Iterable[LocalSession]) -> str:
    rows = list(sessions)
    priced_tokens = sum(
        row.tokens_in + row.tokens_out
        for row in rows
        if row.cost_usd > 0 and not is_subscription_model(row.model)
    )
    total_tokens = sum(row.tokens_in + row.tokens_out for row in rows)
    unpriced_tokens = max(0, total_tokens - priced_tokens)
    if priced_tokens and unpriced_tokens:
        return f"{compact_int(priced_tokens)} API-priced tokens · {compact_int(unpriced_tokens)} plan/limited tokens observed"
    if priced_tokens:
        return f"{compact_int(priced_tokens)} API-priced tokens"
    return f"{compact_int(total_tokens)} tokens observed"


def top_project(sessions: Iterable[LocalSession]) -> tuple[str, float, int] | None:
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for session in sessions:
        label = session.project_path or "unknown"
        totals[label] += session.cost_usd
        counts[label] += 1
    if not totals:
        return None
    best = max(totals, key=lambda key: (totals[key], counts[key]))
    return best, totals[best], counts[best]


def reliable_session_seconds(session: LocalSession, since: datetime | None = None) -> int:
    """Return a conservative session span for display only.

    Some local tools store long-lived thread windows rather than active work
    time. Avoid turning those into fake "hours worked" claims.
    """
    if not session.started_at or not session.updated_at:
        return 0
    if since and session.started_at.astimezone() < since:
        return 0
    seconds = session.duration_seconds
    if seconds <= 0 or seconds > 8 * 60 * 60:
        return 0
    return seconds


def longest_session(sessions: Iterable[LocalSession], since: datetime | None = None) -> LocalSession | None:
    rows = [row for row in sessions if reliable_session_seconds(row, since) > 0]
    if not rows:
        return None
    return max(rows, key=lambda row: reliable_session_seconds(row, since))


def print_cloud_hint(message: str) -> None:
    print(f"\nCloud: {message}")
    print(f"       {CLOUD_URL}")


def command_start(_args: argparse.Namespace) -> int:
    detected = discover_tools()
    sessions = sessions_since(1)
    print("AIWatcher v0.1.0 - local mode")
    print("Read-only scan. No data leaves this machine.\n")
    print("Watching:")
    labels = {
        "claude-code": "Claude Code",
        "cursor": "Cursor",
        "codex-cli": "Codex CLI",
        "cline": "Cline",
        "windsurf": "Windsurf",
    }
    for key, label in labels.items():
        print(f"  {'✓' if detected.get(key) else '✗'} {label}")
    print(f"\nCollected {len(sessions)} sessions from the last 24 hours.")
    print("Run `aiwatcher today` to see your usage.")
    print("Connect Cloud later for team spend, budget guardrails, and audit evidence.")
    return 0


def command_status(_args: argparse.Namespace) -> int:
    detected = discover_tools()
    sessions = scan_all()
    print("AIWatcher Local status\n")
    for tool, installed in detected.items():
        tool_sessions = [row for row in sessions if row.tool == tool]
        print(f"{'✓' if installed else '✗'} {tool:12} {len(tool_sessions):>5} sessions")
    print("\nMode: local-only")
    print("Network: disabled unless hosted sync is configured separately")
    return 0


def command_today(_args: argparse.Namespace) -> int:
    now = datetime.now().astimezone()
    today_start = local_midnight(now.date())
    week_start = now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    all_sessions = scan_all()
    today = [row for row in all_sessions if in_window(row, today_start)]
    week = [row for row in all_sessions if in_window(row, week_start)]
    month = [row for row in all_sessions if in_window(row, month_start)]

    print(f"Today - {format_full_date(now)}")
    by_tool: dict[str, list[LocalSession]] = defaultdict(list)
    for session in today:
        by_tool[session.tool].append(session)

    if not by_tool:
        print("No local AI coding sessions detected today.")

    today_stats = summarize(today)
    week_stats = summarize(week)
    month_stats = summarize(month)
    day_of_month = max(1, now.day)
    projected_month = float(month_stats["cost_usd"]) / day_of_month * 30
    reliable_today_seconds = sum(reliable_session_seconds(row, today_start) for row in today)
    if reliable_today_seconds:
        print(f"{compact_duration(reliable_today_seconds)} of measured AI work")
    else:
        print("Active work time unavailable from local logs")
    print(f"{int(today_stats['sessions'])} sessions · {token_summary_label(today)} · {money(float(today_stats['cost_usd']))} API-equivalent value")
    print(f"Projected month: ~{money(projected_month)} API-equivalent at current pace")
    print("Note: subscription plans may not bill this as incremental spend.\n")

    if by_tool:
        print("By tool")
        print(f"{'Tool':16} {'API value':>10} {'Calls':>7} {'Tokens':>9} {'Sessions':>8}")
        print("-" * 56)
        for tool, rows in sorted(by_tool.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True):
            stats = summarize(rows)
            print(
                f"{tool:16} "
                f"{money(float(stats['cost_usd'])):>10} "
                f"{int(stats['agent_calls']):>7} "
                f"{compact_int(int(stats['tokens_in']) + int(stats['tokens_out'])):>9} "
                f"{int(stats['sessions']):>8}"
            )

        by_model: dict[str, list[LocalSession]] = defaultdict(list)
        for session in today:
            by_model[session.model or "unknown"].append(session)
        print("\nBy model")
        print(f"{'Model':28} {'API value':>10} {'Tokens':>9} {'Calls':>7}")
        print("-" * 58)
        for model, rows in sorted(by_model.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)[:8]:
            stats = summarize(rows)
            print(
                f"{model[:28]:28} "
                f"{money(float(stats['cost_usd'])):>10} "
                f"{compact_int(int(stats['tokens_in']) + int(stats['tokens_out'])):>9} "
                f"{int(stats['agent_calls']):>7}"
            )

        project = top_project(today)
        if project:
            label, spend, session_count = project
            share_base = float(today_stats["cost_usd"]) or float(today_stats["sessions"]) or 1
            share_value = spend if float(today_stats["cost_usd"]) else session_count
            share = round(share_value / share_base * 100)
            print(f"\nTop project: {short_path(label)} ({share}% of today's {'API-equivalent value' if float(today_stats['cost_usd']) else 'sessions'})")

        longest = longest_session(today, today_start)
        if longest:
            print(
                f"Longest session: {compact_duration(reliable_session_seconds(longest, today_start))} "
                f"in {short_path(longest.project_path)} "
                f"({longest.tool}, {money(longest.cost_usd)})"
            )
        else:
            print("Longest session: unavailable from reliable local timestamps")

        notes = sorted({note for row in today for note in row.notes if "limited" in note or "subscription" in note})
        for note in notes[:2]:
            print(f"Note: {note}")

    print(f"\nThis week: {money(float(week_stats['cost_usd']))}")
    print(f"This month: {money(float(month_stats['cost_usd']))}")
    if float(today_stats["cost_usd"]) > 0 or int(today_stats["sessions"]) >= 3:
        print_cloud_hint("See the same cost view for your whole team, with budget caps and anomaly alerts.")
    return 0


def command_tools(args: argparse.Namespace) -> int:
    sessions = sessions_since(args.days)
    by_tool: dict[str, list[LocalSession]] = defaultdict(list)
    for session in sessions:
        by_tool[session.tool].append(session)
    print(f"AI usage by tool - last {args.days} days")
    print("Cost is shown as API-equivalent value; subscription plans may differ.\n")
    for tool, rows in sorted(by_tool.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True):
        stats = summarize(rows)
        print(f"{tool:14} {stats['sessions']:>5} sessions  {compact_int(int(stats['tokens_in'])):>8} in  {compact_int(int(stats['tokens_out'])):>8} out  {money(float(stats['cost_usd'])):>10}")
    if args.days > 30:
        print_cloud_hint("Need retention beyond local history? Cloud keeps team history searchable.")
    return 0


def command_projects(args: argparse.Namespace) -> int:
    sessions = sessions_since(args.days)
    by_project: dict[str, list[LocalSession]] = defaultdict(list)
    for session in sessions:
        by_project[session.project_path or "unknown"].append(session)
    print(f"AI usage by project - last {args.days} days")
    print("Cost is shown as API-equivalent value; subscription plans may differ.\n")
    ranked = sorted(by_project.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    for project, rows in ranked[: args.limit]:
        stats = summarize(rows)
        print(f"{money(float(stats['cost_usd'])):>10}  {stats['sessions']:>4} sessions  {compact_int(int(stats['tokens_in']) + int(stats['tokens_out'])):>8} tokens  {project}")
    if args.days > 30:
        print_cloud_hint("Need org-wide project attribution? Cloud maps spend by user, team, and repo.")
    return 0


def command_report(args: argparse.Namespace) -> int:
    days = args.days
    rows = sessions_since(days)
    stats = summarize(rows)
    projects: dict[str, list[LocalSession]] = defaultdict(list)
    tools: dict[str, list[LocalSession]] = defaultdict(list)
    models: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        projects[row.project_path or "unknown"].append(row)
        tools[row.tool].append(row)
        models[row.model or "unknown"].append(row)

    ranked_projects = sorted(projects.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    ranked_tools = sorted(tools.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    ranked_models = sorted(models.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)

    print(f"AIWatcher Local report - last {days} days\n")
    print(f"Sessions: {stats['sessions']}")
    print(f"API-equivalent value: {money(float(stats['cost_usd']))}")
    print(f"Tokens: {compact_int(int(stats['tokens_in']) + int(stats['tokens_out']))}")
    print(f"Model calls: {stats['agent_calls']}")
    print(f"Tool calls: {stats['tool_calls']}\n")

    if ranked_projects:
        project, project_rows = ranked_projects[0]
        project_stats = summarize(project_rows)
        print(f"Top project: {short_path(project)} ({money(float(project_stats['cost_usd']))})")
    if ranked_tools:
        tool, tool_rows = ranked_tools[0]
        tool_stats = summarize(tool_rows)
        print(f"Top tool: {tool} ({tool_stats['sessions']} sessions)")
    if ranked_models:
        model, model_rows = ranked_models[0]
        model_stats = summarize(model_rows)
        print(f"Top model: {model} ({compact_int(int(model_stats['tokens_in']) + int(model_stats['tokens_out']))} tokens)")

    print("\nSuggested next checks:")
    print("- Review top project sessions for runaway or abandoned work.")
    print("- Compare API-priced tokens with plan/limited tokens before interpreting invoice impact.")
    print("- Run `aiwatcher ui` for clickable local drill-down.")
    return 0


def command_sessions(args: argparse.Namespace) -> int:
    if args.team:
        print("Team session history is a Cloud feature.")
        print("Local OSS shows your machine only; Cloud adds shared visibility, retention, and policy controls.")
        print(CLOUD_URL)
        return 0

    sessions = sorted(sessions_since(args.days), key=lambda row: row.updated_at or row.started_at or datetime.min.astimezone(), reverse=True)
    print(f"Recent AI sessions - last {args.days} days\n")
    for row in sessions[: args.limit]:
        stamp = row.updated_at or row.started_at
        when = format_short_datetime(stamp.astimezone()) if stamp else "unknown"
        print(f"{when:12} {row.tool:12} {money(row.cost_usd):>10} {compact_int(row.tokens_in + row.tokens_out):>8} tokens  {row.project_path or 'unknown'}")
    if args.days > 30:
        print_cloud_hint("Cloud adds retention, team filters, and scheduled exports for session history.")
    return 0


def command_export(args: argparse.Namespace) -> int:
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).astimezone()
        except ValueError:
            print(f"Invalid --since value: {args.since}. Use an ISO date or datetime, for example 2026-06-01.", file=sys.stderr)
            return 2
    else:
        since = datetime.now().astimezone() - timedelta(days=args.days)
    if args.format != "json":
        print("Only --format json is supported in the local MVP.", file=sys.stderr)
        return 2
    if args.level == "events":
        rows = [
            row.to_json()
            for row in scan_all_events()
            if row.timestamp and row.timestamp.astimezone() >= since
        ]
        print(json.dumps({"schema": "aiwatcher.local_events.v0", "events": rows}, indent=2))
    else:
        rows = [row.to_json() for row in scan_all() if in_window(row, since)]
        print(json.dumps({"schema": "aiwatcher.local_sessions.v0", "sessions": rows}, indent=2))
    print("Tip: Cloud can schedule exports and evidence packs for teams.", file=sys.stderr)
    return 0


def command_ui(args: argparse.Namespace) -> int:
    from .ui import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            auto_port=not args.no_port_fallback,
            port_attempts=args.port_attempts,
            restart=args.restart,
        )
    except OSError as exc:
        print(f"Could not start AIWatcher Local UI: {exc}", file=sys.stderr)
        print("Try `aiwatcher ui --restart` or omit `--no-port-fallback` to use the next available port.", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiwatcher", description="AIWatcher Local: private AI coding usage visibility")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="Detect local AI coding tools and run a one-time local scan").set_defaults(func=command_start)
    sub.add_parser("status", help="Show detected tools and local AIWatcher status").set_defaults(func=command_status)
    sub.add_parser("today", help="Show today's local AI usage").set_defaults(func=command_today)

    tools = sub.add_parser("tools", help="Rank AI usage by tool")
    tools.add_argument("--days", type=int, default=7)
    tools.set_defaults(func=command_tools)

    projects = sub.add_parser("projects", help="Rank AI usage by project")
    projects.add_argument("--days", type=int, default=7)
    projects.add_argument("--limit", type=int, default=10)
    projects.set_defaults(func=command_projects)

    report = sub.add_parser("report", help="Show a local weekly AI usage report")
    report.add_argument("--days", type=int, default=7)
    report.set_defaults(func=command_report)

    sessions = sub.add_parser("sessions", help="Show recent local AI sessions")
    sessions.add_argument("--days", type=int, default=1)
    sessions.add_argument("--limit", type=int, default=20)
    sessions.add_argument("--team", action="store_true", help="Explain team session visibility in AIWatcher Cloud")
    sessions.set_defaults(func=command_sessions)

    export = sub.add_parser("export", help="Export local session summaries")
    export.add_argument("--format", default="json", choices=["json"])
    export.add_argument("--level", default="sessions", choices=["sessions", "events"], help="Export session summaries or privacy-safe event hashes")
    export.add_argument("--since", help="ISO date/datetime, for example 2026-06-01")
    export.add_argument("--days", type=int, default=30)
    export.set_defaults(func=command_export)

    ui = sub.add_parser("ui", help="Run the local-only AIWatcher dashboard")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--port-attempts", type=int, default=20, help="How many sequential ports to try when the requested port is busy")
    ui.add_argument("--no-port-fallback", action="store_true", help="Fail instead of trying the next available port")
    ui.add_argument("--restart", action="store_true", help="Stop an existing local process on the requested port before starting")
    ui.set_defaults(func=command_ui)

    return parser


def _force_utf8_output() -> None:
    """Avoid UnicodeEncodeError when printing glyphs like checks/crosses on
    Windows consoles that default to cp1252. No-op where reconfigure is missing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
