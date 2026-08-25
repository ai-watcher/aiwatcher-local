"""Is this session doing something right now?

The dashboard can already say how much a session has cost and how much context
it is replaying. It cannot say whether anyone is still in it. `session_state`
in `ui.py` gets as far as "the log was touched in the last half hour", which is
the right answer to "could you still act on this" and the wrong answer to "is
it working" -- a session abandoned twenty-five minutes ago and one mid-edit are
the same row there.

This module subdivides that. It is a classifier over one input, the last write,
not a state machine with memory: nothing is stored between calls, so a session
that dies without saying so cannot leave a stale "working" behind.

Deliberately absent: a `blocked` state. A transcript that has been quiet for
ninety seconds is byte-for-byte identical whether the agent is waiting on a
permission prompt or running a slow test, so inferring "it needs you" here
would be a guess, and a notification that cries wolf gets muted within a day.
That state needs an event from the tool itself and belongs to the hook layer.

Scope: local files only. Sessions on another machine, or running in the cloud,
write nothing here and are invisible -- callers must not present these counts
as a total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .processes import seconds_label
# _git_root, not project_path. project_path deliberately folds Claude's
# throwaway agent worktrees back into the repository they were cut from, so
# two isolated agents would share it -- the exact opposite of what a
# same-checkout test needs. The git root of the directory the tool actually
# recorded is the real answer: a linked worktree reports itself, a subfolder
# reports its repo, and a deleted agent worktree reports nothing and falls
# back to its own unique path.
from .scanner import LocalSession, _git_root


# How long after its last write a session still counts as working.
#
# STOPGAP. The defensible way to pick this is the histogram of gaps between
# writes inside real sessions -- scripts/probe_concurrency.py prints it -- and
# that data lives on a machine we have not been able to read yet. 120s is
# chosen to fail in the safe direction: too short and a session flickers
# working/quiet/working through every long test run, and flicker in the corner
# of the eye is the one thing an always-open ambient surface must not do. Too
# long only means a stopped session lingers a couple of minutes, which nobody
# notices. Revisit against the histogram before this is treated as tuned.
WORKING_SECONDS = 120

# The outer boundary: past this, a session is not live at all. Not a new
# threshold -- it is the one `ui.session_state` has always used to call a
# session "active", moved here so both surfaces read the same number and cannot
# drift into disagreeing about what "live" means.
LIVE_WINDOW_MINUTES = 30

# Tools whose timestamp cannot answer this question, whatever it says.
#
# Cursor's rows are built from the mtime of AI-pattern files in a log
# directory, and one row is a log directory rather than a chat. That stamp
# moves when the editor writes a log, which is not the same event as an AI
# session doing work. Reading it as liveness would produce a confident,
# arithmetically real number about the wrong subject -- so it is refused here
# and reported as unmeasurable, with the reason, rather than shown as a zero.
UNMEASURABLE_TOOLS: dict[str, str] = {
    "cursor": (
        "Cursor's timestamp is a log-file mtime for the whole editor window, "
        "not activity in one chat."
    ),
}

_NO_STAMP_REASON = "This tool did not expose a session timestamp."

# Display names for the strip. Elsewhere the dashboard prints the raw tool
# id, which is fine inside a card that has already named its subject; in a
# one-line count across tools it reads as debug output. Unknown ids fall
# through unchanged rather than being guessed at.
TOOL_LABELS: dict[str, str] = {
    "claude-code": "Claude",
    "codex-cli": "Codex",
    "cursor": "Cursor",
}


def tool_label(tool: str) -> str:
    return TOOL_LABELS.get((tool or "").lower(), tool or "unknown tool")


@dataclass(frozen=True)
class SessionPresence:
    """One session's answer, with the reason when there isn't one."""

    session_id: str
    tool: str
    state: str                      # working | quiet | gone | unmeasurable
    label: str
    measurable: bool
    reason: str | None = None       # populated only when measurable is False
    idle_seconds: float | None = None
    project_path: str | None = None
    # An AIWatcher Second Opinion spawn rather than work the user started.
    # Carried, not filtered: the house rule is that AIWatcher reports what its
    # own features cost instead of hiding them. Callers can label it; what they
    # must not do is let it pass as one of the user's own live sessions.
    analyst_run: bool = False

    @property
    def live(self) -> bool:
        return self.state in {"working", "quiet"}

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "tool": self.tool,
            "state": self.state,
            "label": self.label,
            "measurable": self.measurable,
            "reason": self.reason,
            "idle_seconds": round(self.idle_seconds, 1) if self.idle_seconds is not None else None,
            "project_path": self.project_path,
            "analyst_run": self.analyst_run,
            "live": self.live,
        }


def _stamp(session: LocalSession) -> datetime | None:
    value = session.updated_at or session.started_at
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unmeasurable(session: LocalSession, reason: str) -> SessionPresence:
    return SessionPresence(
        session_id=session.session_id,
        tool=session.tool,
        state="unmeasurable",
        label="not measurable here",
        measurable=False,
        reason=reason,
        project_path=session.project_path,
        analyst_run=session.analyst_run,
    )


def presence_for_session(
    session: LocalSession,
    *,
    now: datetime | None = None,
) -> SessionPresence:
    """Classify one session by how long ago it last wrote."""
    reason = UNMEASURABLE_TOOLS.get((session.tool or "").lower())
    if reason:
        return _unmeasurable(session, reason)

    stamp = _stamp(session)
    if stamp is None:
        return _unmeasurable(session, _NO_STAMP_REASON)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Clamped at zero: a log stamped slightly in the future (clock skew, a
    # machine waking from sleep) is not evidence of anything, and a negative
    # idle time would sort ahead of every real session.
    idle = max(0.0, (current - stamp).total_seconds())

    if idle <= WORKING_SECONDS:
        state, label = "working", "working"
    elif idle <= LIVE_WINDOW_MINUTES * 60:
        # Coarse on purpose, and through the same helper the process list uses
        # so two surfaces cannot spell the same duration differently. A counter
        # ticking every second is motion, which this dashboard does not do.
        state, label = "quiet", f"quiet {seconds_label(int(idle))}"
    else:
        state, label = "gone", "ended"

    return SessionPresence(
        session_id=session.session_id,
        tool=session.tool,
        state=state,
        label=label,
        measurable=True,
        idle_seconds=idle,
        project_path=session.project_path,
        analyst_run=session.analyst_run,
    )


def presence_for_sessions(
    sessions: list[LocalSession],
    *,
    now: datetime | None = None,
) -> list[SessionPresence]:
    """Classify every session, worst-idle last so callers can take the head."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = [presence_for_session(session, now=current) for session in sessions]
    order = {"working": 0, "quiet": 1, "unmeasurable": 2, "gone": 3}
    rows.sort(key=lambda row: (order.get(row.state, 9), row.idle_seconds or 0.0))
    return rows


def presence_by_tool(rows: list[SessionPresence]) -> list[dict[str, object]]:
    """Per-tool live counts.

    A tool with nothing readable reports `measurable: False` and why, rather
    than a zero -- "we cannot see this" and "nothing is running" are different
    answers and must not render the same.
    """
    tools: dict[str, dict[str, object]] = {}
    for row in rows:
        bucket = tools.setdefault(row.tool, {
            "tool": row.tool,
            "label": tool_label(row.tool),
            "working": 0,
            "quiet": 0,
            "live": 0,
            "analyst_runs": 0,
            "measurable": False,
            "reason": None,
        })
        if row.measurable:
            bucket["measurable"] = True
            bucket["reason"] = None
        elif not bucket["measurable"] and bucket["reason"] is None:
            bucket["reason"] = row.reason
        if row.state in {"working", "quiet"}:
            bucket[row.state] = int(bucket[row.state]) + 1
            bucket["live"] = int(bucket["live"]) + 1
            if row.analyst_run:
                bucket["analyst_runs"] = int(bucket["analyst_runs"]) + 1
    return sorted(
        tools.values(),
        key=lambda item: (-int(item["live"]), str(item["tool"])),
    )


def _tree_key(session: LocalSession) -> str | None:
    """Which checkout on disk this session is editing."""
    cwd = (session.raw_cwd or "").strip()
    if not cwd:
        return None
    return _git_root(cwd) or cwd


def working_tree_collisions(
    sessions: list[LocalSession],
    rows: list[SessionPresence],
) -> list[dict[str, object]]:
    """Live sessions sharing one checkout, which can silently overwrite work.

    The failure this names: one session reads a file, another saves it, the
    first writes its stale copy back. Git never sees two versions -- there is
    one working tree and the last writer wins -- so nothing conflicts and the
    change is simply gone. Neither tool can see the other, which is why nothing
    else on the machine warns about it.

    At least one session has to be *working*. Two sessions both sitting idle in
    a repo are not writing to it, and a warning that fires on every pair of
    parked sessions is one that gets ignored before it ever catches anything
    real.

    Analyst spawns are left out: Second Opinion runs in a sandbox under the
    repository, so counting it would fire this on any project AIWatcher had
    looked at, against a session the user did not start.
    """
    live = {row.session_id: row for row in rows if row.live and not row.analyst_run}
    groups: dict[str, list[tuple[LocalSession, SessionPresence]]] = {}
    for session in sessions:
        row = live.get(session.session_id)
        if row is None:
            continue
        key = _tree_key(session)
        if key:
            groups.setdefault(key, []).append((session, row))

    found: list[dict[str, object]] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        working = [row for _, row in members if row.state == "working"]
        if not working:
            continue
        found.append({
            "path": key,
            "label": key.rstrip("/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or key,
            "live": len(members),
            "working": len(working),
            "sessions": [row.session_id for _, row in members],
            "tools": sorted({tool_label(session.tool) for session, _ in members}),
        })
    return sorted(found, key=lambda item: (-int(item["live"]), str(item["label"])))


def live_presence(
    sessions: list[LocalSession],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """The whole answer, ready for a caller to render.

    `live` counts sessions, never sub-agents: a sub-agent runs inside its
    parent's session and counting it here would inflate the one number this
    payload exists to state.
    """
    rows = presence_for_sessions(sessions, now=now)
    tools = presence_by_tool(rows)
    collisions = working_tree_collisions(sessions, rows)
    working = sum(1 for row in rows if row.state == "working")
    quiet = sum(1 for row in rows if row.state == "quiet")
    return {
        "sessions": [row.to_json() for row in rows if row.state != "gone"],
        "tools": tools,
        "collisions": collisions,
        "working": working,
        "quiet": quiet,
        "live": working + quiet,
        "analyst_runs": sum(1 for row in rows if row.live and row.analyst_run),
        # Callers must not present these counts as a total. Nothing here can
        # see another machine or a cloud session.
        "scope": "this machine",
    }
