"""Stage 2 of the Plan brief.

Everything asserted here was checked against the installed CLIs first
(claude 2.1.221, codex-cli 0.146.0) rather than taken from the spec. Three of
the spec's assumptions did not survive that, and each has a test below so they
cannot come back.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from aiwatcher_cli import analyst


def _valid(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "outcome": "Rewrite the billing module and delete the legacy adapter.",
        "success_check": "Billing uses the pricing table and the adapter is gone.",
        "scope_paths": ["src/billing/charges.py"],
        "unresolved_nouns": [],
        "removals": [{"what": "legacy adapter", "requested": True,
                      "path": "src/billing/legacy_adapter.py"}],
        "ambiguities": [],
        "first_checkpoint": "Inspect the adapter's callers.",
        "confidence": "high",
    }
    body.update(overrides)
    return body


PATHS = ("src/billing/charges.py", "src/billing/legacy_adapter.py", "tests/test_billing.py")


class PromptShapeTest(unittest.TestCase):
    """The analyst is a coding agent and will do the task unless stopped."""

    def test_the_refusal_is_the_first_line(self):
        # Verified against both installed CLIs: claude returned num_turns 1 with
        # no tool calls, codex made no edits under -s read-only. Re-run that
        # check whenever the CLI or the model tier changes -- this test only
        # guards that the line is still there and still first.
        first = analyst.PROMPT_TEMPLATE.strip().splitlines()[0]
        self.assertIn("NOT going to perform", first)
        self.assertIn("describing it", first)

    def test_the_prompt_carries_the_schema_and_the_paths(self):
        built = analyst.build_prompt("delete the adapter", PATHS)
        self.assertIn("delete the adapter", built)
        for path in PATHS:
            self.assertIn(path, built)
        self.assertIn('"confidence"', built)
        self.assertIn("PATHS (3 of 3", built)

    def test_the_path_list_is_capped(self):
        many = tuple(f"src/file{i}.py" for i in range(analyst.MAX_PATHS + 50))
        built = analyst.build_prompt("x", many)
        self.assertIn(f"PATHS ({analyst.MAX_PATHS} of {len(many)}", built)
        self.assertNotIn(f"src/file{analyst.MAX_PATHS + 10}.py", built)


class SchemaTest(unittest.TestCase):
    def test_every_property_is_required(self):
        # codex --output-schema rejects the spec's schema outright: "'required'
        # is required to be supplied and to be an array including every key in
        # properties. Missing 'path'." Optional properties are not allowed, so
        # path is required and nullable instead.
        removals = analyst.RESPONSE_SCHEMA["properties"]["removals"]["items"]
        self.assertEqual(sorted(removals["required"]), sorted(removals["properties"]))
        self.assertIn("null", removals["properties"]["path"]["type"])


class ValidationTest(unittest.TestCase):
    """Spec 3.4. Any failure drops the whole block -- never half of one."""

    def test_a_fenced_response_still_parses(self):
        # The model wraps its JSON in a ```json fence despite being told not to.
        # Observed on claude 2.1.221 with --model haiku on the first real run.
        raw = "```json\n" + json.dumps(_valid()) + "\n```"
        obj, reason = analyst.validate(raw, PATHS)
        self.assertIsNotNone(obj, reason)
        self.assertEqual(obj["confidence"], "high")

    def test_an_invented_path_is_dropped_and_confidence_falls(self):
        raw = json.dumps(_valid(scope_paths=["src/billing/charges.py", "src/invented.py"]))
        obj, _ = analyst.validate(raw, PATHS)
        self.assertEqual(obj["scope_paths"], ["src/billing/charges.py"])
        self.assertEqual(obj["confidence"], "medium")
        self.assertEqual(obj["dropped_paths"], 1)

    def test_mostly_invented_paths_reject_the_whole_response(self):
        raw = json.dumps(_valid(scope_paths=["a.py", "b.py", "src/billing/charges.py"]))
        obj, reason = analyst.validate(raw, PATHS)
        self.assertIsNone(obj)
        self.assertTrue(reason)

    def test_a_stringified_object_is_rejected(self):
        # The exact bug class that shipped last time, and one regex prevents it
        # forever. It is checked on every string anywhere in the response.
        for field, value in (("outcome", "Rewrite [object Object] now"),
                             ("ambiguities", ["scope of [object Promise]"])):
            with self.subTest(field=field):
                obj, _ = analyst.validate(json.dumps(_valid(**{field: value})), PATHS)
                self.assertIsNone(obj)

    def test_an_empty_conclusion_is_rejected(self):
        for field in ("outcome", "first_checkpoint"):
            with self.subTest(field=field):
                self.assertIsNone(analyst.validate(json.dumps(_valid(**{field: "  "})), PATHS)[0])

    def test_an_extra_property_is_rejected(self):
        raw = json.dumps({**_valid(), "injected": "anything"})
        self.assertIsNone(analyst.validate(raw, PATHS)[0])

    def test_a_missing_property_is_rejected(self):
        body = _valid()
        del body["success_check"]
        self.assertIsNone(analyst.validate(json.dumps(body), PATHS)[0])

    def test_an_unknown_confidence_is_rejected(self):
        self.assertIsNone(analyst.validate(json.dumps(_valid(confidence="certain")), PATHS)[0])

    def test_garbage_is_rejected_without_raising(self):
        for raw in ("", "   ", "not json", "[1,2,3]", '"a string"'):
            with self.subTest(raw=raw):
                obj, reason = analyst.validate(raw, PATHS)
                self.assertIsNone(obj)
                self.assertTrue(reason)


class LedgerMarkerTest(unittest.TestCase):
    """Spec 6, and the reason it needed re-deciding.

    `<project>/.aiwatcher/analyst` normalises to the project's git root, so the
    marker has to be read off the raw cwd the tool logged, not off the project
    path the scanner derives from it.
    """

    def test_the_sandbox_is_recognised_on_both_separators(self):
        cases = [
            "C:/proj/.aiwatcher/analyst",
            "C:" + chr(92) + "proj" + chr(92) + ".aiwatcher" + chr(92) + "analyst",
            "/home/u/proj/.aiwatcher/analyst/",
            "/home/u/proj/.aiwatcher/Analyst",
        ]
        for cwd in cases:
            with self.subTest(cwd=cwd):
                self.assertTrue(analyst.is_analyst_cwd(cwd))

    def test_a_real_project_is_not_mistaken_for_one(self):
        for cwd in ("C:/proj", "/home/u/proj", "/home/u/.aiwatcher", None, "",
                    "/home/u/proj/.aiwatcher/analyst-notes"):
            with self.subTest(cwd=cwd):
                self.assertFalse(analyst.is_analyst_cwd(cwd))

    def test_the_normalised_project_path_cannot_be_used_as_the_marker(self):
        # The finding this whole design turns on, pinned so it cannot regress
        # into "just match project_path" later.
        #
        # The sandbox has to exist for this to reproduce: _normalize_project_path
        # folds a path to its git root, and the root lookup only succeeds on a
        # directory that is really there. run() creates the sandbox before it
        # spawns, so the live case is always the folded one.
        import subprocess as sp
        import tempfile
        from aiwatcher_cli import scanner

        with tempfile.TemporaryDirectory(prefix="aiw-marker-") as tmp:
            repo = Path(tmp) / "proj"
            (repo / "src").mkdir(parents=True)
            try:
                sp.run(["git", "init", "-q", str(repo)], check=True,
                       capture_output=True, timeout=60)
            except (OSError, sp.SubprocessError):
                self.skipTest("git is not available")
            sandbox = analyst.sandbox_dir(repo)
            sandbox.mkdir(parents=True, exist_ok=True)
            scanner.PROJECT_PATH_CACHE.clear()
            normalised = scanner._normalize_project_path(str(sandbox))
            scanner.PROJECT_PATH_CACHE.clear()

        self.assertTrue(analyst.is_analyst_cwd(str(sandbox)),
                        "the raw cwd is what carries the marker")
        self.assertFalse(
            analyst.is_analyst_cwd(normalised),
            "normalisation folds the sandbox into the repo, so the raw cwd is the marker",
        )


class RunTest(unittest.TestCase):
    """Spec 8's failure matrix. Every row leaves Zones B and C intact."""

    def setUp(self):
        self.detection = {"available": True, "cli": "claude-code", "executable": "claude"}

    def _run(self, runner, **kwargs):
        return analyst.run("delete the adapter", project_root=self.tmp(),
                           paths=PATHS, detection=self.detection, runner=runner, **kwargs)

    def tmp(self):
        import tempfile
        return tempfile.mkdtemp(prefix="aiw-analyst-test-")

    def test_the_prompt_goes_in_on_stdin_and_never_in_argv(self):
        # `claude -p <file>` does not read the file: -p is a boolean flag, so
        # the path is taken as the prompt and the analyst describes the string.
        # It exits 0, which is what makes it dangerous. stdin is also what keeps
        # the prompt out of the process list.
        seen: dict[str, object] = {}

        def runner(argv, text, cwd, env, timeout):
            seen.update(argv=argv, text=text, cwd=cwd, env=env, timeout=timeout)
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"result": json.dumps(_valid()), "total_cost_usd": 0.037,
                 "session_id": "abc", "usage": {"input_tokens": 9, "output_tokens": 3030}}), "")

        result = self._run(runner)
        self.assertTrue(result["available"], result.get("reason"))
        self.assertIn("-p", seen["argv"])
        self.assertIn("delete the adapter", seen["text"])
        for arg in seen["argv"]:
            self.assertNotIn("delete the adapter", arg)
        self.assertEqual(seen["env"]["AIWATCHER_ROLE"], "analyst")

    def test_it_runs_from_the_sandbox_not_the_project(self):
        seen: dict[str, object] = {}

        def runner(argv, text, cwd, env, timeout):
            seen["cwd"] = cwd
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"result": json.dumps(_valid())}), "")

        root = self.tmp()
        analyst.run("x", project_root=root, paths=PATHS,
                    detection=self.detection, runner=runner)
        self.assertTrue(analyst.is_analyst_cwd(str(seen["cwd"])))
        self.assertTrue(Path(seen["cwd"]).is_dir())

    def test_the_cost_and_tokens_come_back_for_the_chip(self):
        def runner(argv, text, cwd, env, timeout):
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "result": json.dumps(_valid()), "total_cost_usd": 0.0370409,
                "session_id": "26772cb6", "usage": {
                    "input_tokens": 9, "output_tokens": 3030,
                    "cache_read_input_tokens": 24999, "cache_creation_input_tokens": 9115}}), "")

        result = self._run(runner)
        self.assertAlmostEqual(result["cost_usd"], 0.0370409)
        self.assertEqual(result["tokens"], 9 + 3030 + 24999 + 9115)
        self.assertEqual(result["session_id"], "26772cb6")

    def test_a_timeout_keeps_stage_one(self):
        def runner(*args):
            raise subprocess.TimeoutExpired("claude", analyst.TIMEOUT_SECONDS)
        result = self._run(runner)
        self.assertFalse(result["available"])
        self.assertIn("timed out", result["reason"])

    def test_a_non_zero_exit_reports_the_cli_not_the_analysis(self):
        def runner(argv, *args):
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        result = self._run(runner)
        self.assertFalse(result["available"])
        self.assertIn("agent CLI returned an error", result["reason"])

    def test_a_malformed_response_drops_the_block_and_keeps_the_raw(self):
        def runner(argv, *args):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "{oh no"}), "")
        result = self._run(runner)
        self.assertFalse(result["available"])
        self.assertNotIn("analysis", result)
        self.assertIn("raw", result)

    def test_no_cli_is_a_settings_line_not_an_error(self):
        result = analyst.run("x", project_root=self.tmp(), paths=PATHS,
                             detection={"available": False, "reason": "no agent CLI found"})
        self.assertFalse(result["available"])
        self.assertIn("no agent CLI found", result["reason"])

    def test_the_timeout_clears_a_measured_run(self):
        # A real run took 29.9s wall / 26.2s to first token on the small tier,
        # because the CLI loads ~34k tokens of its own context first. The spec's
        # 12s would have timed out every call ever made.
        self.assertGreater(analyst.TIMEOUT_SECONDS, 30)


class TimeoutActuallyBoundsTest(unittest.TestCase):
    """The timeout has to bound the call, not just the direct child.

    subprocess.run(timeout=...) does not. It kills the child it launched and
    then reaps it with a second, unbounded communicate() -- which waits for the
    stdout pipe to close, and the agent CLI is a launcher whose grandchildren
    inherited that pipe. Measured against the real CLI before this was fixed: a
    45s timeout returned after 3m54s with the agent still running and still
    spending.
    """

    def test_a_leaked_process_tree_does_not_outlive_the_timeout(self):
        import sys
        import tempfile
        import time

        launcher = Path(tempfile.mkdtemp(prefix="aiw-timeout-")) / "slow.py"
        launcher.write_text(
            "import subprocess, sys, time\n"
            # A grandchild that inherits stdout and long outlives the parent:
            # the exact shape that made the old timeout leak.
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            analyst._spawn([sys.executable, str(launcher)], "x", Path.cwd(), None, 5.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0 + analyst.KILL_GRACE_SECONDS + 10.0,
                        f"the timeout leaked: returned after {elapsed:.1f}s")


class OverheadLineTest(unittest.TestCase):
    """Spec 6. AIWatcher spawns the same kind of session it measures."""

    @staticmethod
    def _session(session_id: str, cwd: str, cost: float):
        from aiwatcher_cli.scanner import LocalSession
        return LocalSession(session_id=session_id, tool="claude-code",
                            raw_cwd=cwd, cost_usd=cost, tokens_in=100, tokens_out=50)

    def test_analyst_spend_leaves_the_user_totals_alone(self):
        from aiwatcher_cli import ui
        rows = [
            self._session("user-1", "C:/proj", 10.0),
            self._session("analyst-1", "C:/proj/.aiwatcher/analyst", 0.04),
            self._session("user-2", "C:/proj", 5.0),
            self._session("analyst-2", "C:/proj/.aiwatcher/analyst", 0.03),
        ]
        user, overhead = ui._split_analyst_overhead(rows)
        self.assertEqual([r.session_id for r in user], ["user-1", "user-2"])
        self.assertEqual([r.session_id for r in overhead], ["analyst-1", "analyst-2"])
        self.assertEqual(sum(r.cost_usd for r in user), 15.0)

    def test_the_line_is_there_even_when_nothing_ran(self):
        # A line that only appears once it has something to confess is one
        # nobody believes when it does appear.
        from aiwatcher_cli import ui
        empty = ui._analyst_overhead([], 7)
        self.assertEqual(empty["runs"], 0)
        self.assertIn("nothing this window", empty["label"])
        self.assertIn("No second opinions", empty["detail"])

    def test_the_line_counts_what_ran(self):
        from aiwatcher_cli import ui
        rows = [self._session("a", "C:/p/.aiwatcher/analyst", 0.04),
                self._session("b", "C:/p/.aiwatcher/analyst", 0.03)]
        line = ui._analyst_overhead(rows, 7)
        self.assertEqual(line["runs"], 2)
        self.assertEqual(line["cost_label"], "$0.07")
        self.assertIn("2 second opinions", line["detail"])
        self.assertIn("last 7 days", line["detail"])


class CacheKeyTest(unittest.TestCase):
    def test_the_same_question_is_free_the_second_time(self):
        a = analyst.cache_key("prompt", "rev1", "proj")
        self.assertEqual(a, analyst.cache_key("prompt", "rev1", "proj"))
        for changed in (("other", "rev1", "proj"), ("prompt", "rev2", "proj"),
                        ("prompt", "rev1", "other")):
            with self.subTest(changed=changed):
                self.assertNotEqual(a, analyst.cache_key(*changed))


if __name__ == "__main__":
    unittest.main()
