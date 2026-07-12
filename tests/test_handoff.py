from __future__ import annotations

import unittest
from datetime import datetime, timezone

from aiwatcher_cli.handoff import build_handoff_capsule, render_handoff_capsule
from aiwatcher_cli.scanner import LocalSession


class HandoffTests(unittest.TestCase):
    def test_handoff_capsule_prioritizes_fresh_session_guidance(self) -> None:
        session = LocalSession(
            session_id="session-1",
            tool="claude-code",
            project_path="/repo/app",
            updated_at=datetime.now(timezone.utc),
            model="claude-sonnet",
            tokens_in=700_000,
            tokens_out=20_000,
            cost_usd=9.03,
            agent_calls=984,
            tool_calls=547,
        )

        capsule = build_handoff_capsule(session, [], outcome="useful")
        rendered = render_handoff_capsule(capsule)

        self.assertEqual(capsule["usage"]["tokens_label"], "720.0k")
        self.assertIn("continue with a smaller checkpoint", "\n".join(capsule["warnings"]))
        self.assertIn("fresh Claude/Codex/Cursor session", rendered)
        self.assertIn("Inspect the current git status", capsule["next_brief"])
        self.assertNotIn("source diff", rendered.lower())

    def test_handoff_capsule_formats_for_target_tool(self) -> None:
        session = LocalSession(
            session_id="session-2",
            tool="claude-code",
            project_path="/repo/app",
            updated_at=datetime.now(timezone.utc),
            model="claude-sonnet",
        )

        capsule = build_handoff_capsule(session, [], target="codex")
        rendered = render_handoff_capsule(capsule)

        self.assertEqual(capsule["target"], "codex")
        self.assertEqual(capsule["target_label"], "Codex")
        self.assertIn("fresh Codex session", rendered)
        self.assertIn("Paste this as the first prompt in a fresh Codex session", capsule["next_brief"])


if __name__ == "__main__":
    unittest.main()
