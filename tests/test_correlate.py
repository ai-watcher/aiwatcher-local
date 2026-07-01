from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import local_state
from aiwatcher_cli.correlate import link_recent_interventions_to_sessions
from aiwatcher_cli.scanner import LocalSession


def session(
    *,
    session_id: str = "session-1",
    tool: str = "codex-cli",
    project: str = "/repo/app",
    started_at: datetime | None = None,
) -> LocalSession:
    stamp = started_at or datetime.now(timezone.utc)
    return LocalSession(
        session_id=session_id,
        tool=tool,
        project_path=project,
        started_at=stamp,
        updated_at=stamp + timedelta(minutes=5),
        model="gpt-5.5",
        tokens_in=1000,
        tokens_out=500,
        cost_usd=0,
        agent_calls=1,
        tool_calls=1,
    )


class CorrelateTests(unittest.TestCase):
    def test_links_recent_intervention_to_matching_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                intervention_id = local_state.record_intervention(
                    tool="codex",
                    cwd="/repo",
                    risk="medium",
                    score=5,
                    findings=["Broad scope"],
                    original_prompt="Refactor the app",
                    suggested_prompt="Inspect first",
                    decision="brief_accepted",
                    selected_prompt="Inspect first",
                )

                linked = link_recent_interventions_to_sessions([session(project="/repo/app")])
                rows = local_state.recent_interventions()

        self.assertEqual(linked, 1)
        record = next(row for row in rows if row["id"] == intervention_id)
        self.assertEqual(record["session_id"], "session-1")

    def test_does_not_link_wrong_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="medium",
                    score=5,
                    findings=["Broad scope"],
                    original_prompt="Refactor the app",
                    suggested_prompt="Inspect first",
                    decision="brief_accepted",
                    selected_prompt="Inspect first",
                )

                linked = link_recent_interventions_to_sessions([session(tool="codex-cli", project="/repo/app")])

        self.assertEqual(linked, 0)


if __name__ == "__main__":
    unittest.main()
