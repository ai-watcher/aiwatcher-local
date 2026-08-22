"""Stage 2 of the Plan brief.

Everything asserted here was checked against the installed CLIs first
(claude 2.1.221, codex-cli 0.146.0) rather than taken from the spec. Three of
the spec's assumptions did not survive that, and each has a test below so they
cannot come back.
"""

from __future__ import annotations

import json
import os
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
        # Named, now that there is more than one host: "your agent CLI" is not
        # much help to somebody who has both installed.
        def runner(argv, *args):
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        result = self._run(runner)
        self.assertFalse(result["available"])
        self.assertIn("Claude Code returned an error", result["reason"])

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

    def test_the_timeout_clears_the_slowest_measured_run(self):
        # The same prompt on the same tier has taken 17s and 206s. The ceiling
        # bounds a hang; it is not a latency budget, and tripping it during an
        # ordinary slow spell throws away an answer already paid for.
        self.assertGreater(analyst.TIMEOUT_SECONDS, 210)


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


class CodexHostTest(unittest.TestCase):
    """Codex as a second analyst host.

    Verified against codex-cli 0.146.0: `codex exec --skip-git-repo-check
    -s read-only -m gpt-5.4-mini --output-schema schema.json -o last.json -`
    returns schema-valid JSON with no code fence, because Codex validates the
    response itself rather than being asked nicely in a prompt.
    """

    def setUp(self):
        import tempfile
        self.sandbox = Path(tempfile.mkdtemp(prefix="aiw-codex-")) / ".aiwatcher" / "analyst"
        self.sandbox.mkdir(parents=True)
        self.host = analyst.HOSTS_BY_KEY["codex-cli"]

    def test_the_prompt_is_read_from_stdin(self):
        # `codex exec <file>` takes the prompt positionally, so a path handed to
        # it is analysed as the literal string. A bare "-" reads stdin, which
        # also keeps the prompt out of the process list.
        argv = analyst._prepare(self.host, "codex", "gpt-5.4-mini", self.sandbox)
        self.assertEqual(argv[-1], "-")
        self.assertIn("exec", argv)

    def test_it_cannot_write_and_does_not_need_a_repository(self):
        argv = analyst._prepare(self.host, "codex", "gpt-5.4-mini", self.sandbox)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")

    def test_the_schema_is_enforced_by_the_cli(self):
        argv = analyst._prepare(self.host, "codex", "gpt-5.4-mini", self.sandbox)
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        self.assertTrue(schema_path.is_file())
        self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")),
                         analyst.RESPONSE_SCHEMA)

    def test_a_stale_answer_is_never_read_back_as_this_run(self):
        # -o writes the answer to a file. Left in place, a run that fails before
        # writing would hand back the previous run's analysis as its own.
        answer = self.sandbox / "last.json"
        answer.write_text(json.dumps(_valid(outcome="STALE")), encoding="utf-8")
        analyst._prepare(self.host, "codex", "gpt-5.4-mini", self.sandbox)
        self.assertFalse(answer.exists())

    def test_the_answer_comes_from_the_file_not_from_stdout(self):
        (self.sandbox / "last.json").write_text(json.dumps(_valid()), encoding="utf-8")
        proc = subprocess.CompletedProcess(
            ["codex"], 0, "some progress chatter", "session id: 01a02840-7d05-7833-aa7e-e88f4869")
        envelope, answer = analyst._read_result(self.host, proc, self.sandbox)
        self.assertIn("outcome", json.loads(answer))
        self.assertEqual(envelope["session_id"], "01a02840-7d05-7833-aa7e-e88f4869")

    def test_no_answer_file_means_no_answer(self):
        # Falling back to stdout would risk validating a progress line.
        proc = subprocess.CompletedProcess(["codex"], 0, '{"outcome": "from stdout"}', "")
        _, answer = analyst._read_result(self.host, proc, self.sandbox)
        self.assertEqual(answer, "")

    def test_a_host_that_reports_no_cost_says_so_rather_than_zero(self):
        # AIWatcher prices Codex sessions at $0 by design, and a subscription
        # user's really is free -- but an API-key user's is not, and the local
        # logs cannot tell them apart. "$0.00" would be a claim we cannot make.
        def runner(argv, text, cwd, env, timeout):
            (Path(cwd) / "last.json").write_text(json.dumps(_valid()), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "session id: 01a0284012345678")

        result = analyst.run("x", project_root=self.sandbox.parent.parent, paths=PATHS,
                             detection={"available": True, "cli": "codex-cli",
                                        "executable": "codex"},
                             runner=runner)
        self.assertTrue(result["available"], result.get("reason"))
        self.assertIsNone(result["cost_usd"])
        self.assertFalse(result["cost_reported"])
        self.assertEqual(result["cli"], "codex-cli")
        self.assertEqual(result["model"], "gpt-5.4-mini")


class HostSelectionTest(unittest.TestCase):
    """Spec 3.1: ask the vendor the user is about to prompt."""

    BOTH = {
        "claude-code": {"available": True, "cli": "claude-code", "executable": "claude"},
        "codex-cli": {"available": True, "cli": "codex-cli", "executable": "codex"},
    }

    def _detect(self, found, tool):
        original = analyst.detect_all
        try:
            analyst.detect_all = lambda **kwargs: found
            return analyst.detect(tool=tool)
        finally:
            analyst.detect_all = original

    def test_the_selected_vendor_wins_when_it_is_installed(self):
        for tool, expected in (("codex", "codex-cli"), ("claude", "claude-code")):
            with self.subTest(tool=tool):
                chosen = self._detect(self.BOTH, tool)
                self.assertEqual(chosen["cli"], expected)
                self.assertTrue(chosen["preferred"])

    def test_a_vendor_with_no_analyst_falls_back_rather_than_refusing(self):
        # Cursor has no analyst host yet. Any second opinion beats none, but the
        # answer says it is not the vendor they picked.
        chosen = self._detect(self.BOTH, "cursor")
        self.assertTrue(chosen["available"])
        self.assertFalse(chosen["preferred"])

    def test_it_falls_back_when_the_preferred_vendor_is_missing(self):
        found = {**self.BOTH, "codex-cli": {"available": False, "cli": "codex-cli",
                                            "reason": "Codex is not installed"}}
        chosen = self._detect(found, "codex")
        self.assertEqual(chosen["cli"], "claude-code")
        self.assertFalse(chosen["preferred"])

    def test_with_nothing_installed_it_reports_the_vendor_that_was_asked_for(self):
        found = {
            "claude-code": {"available": False, "cli": "claude-code", "reason": "Claude Code is not installed"},
            "codex-cli": {"available": False, "cli": "codex-cli", "reason": "Codex is not installed"},
        }
        chosen = self._detect(found, "codex")
        self.assertFalse(chosen["available"])
        self.assertIn("Codex", chosen["reason"])


class DetectionIsCheapTest(unittest.TestCase):
    """The Plan gate asks "is there an analyst?" on every preflight."""

    def test_the_gate_does_not_shell_out(self):
        # Asking two CLIs for their version here cost a subprocess each and made
        # an unrelated test time out. Installation is a path lookup; whether the
        # CLI works is answered by running it, and a broken one already comes
        # back as spec 8's "CLI returned an error" row.
        calls = []
        original = subprocess.run
        try:
            subprocess.run = lambda *a, **k: calls.append(a) or original(*a, **k)
            analyst._DETECTION_CACHE = None
            analyst.detect_all(refresh=True)
        finally:
            subprocess.run = original
            analyst._DETECTION_CACHE = None
        self.assertEqual(calls, [], "detection spawned a process on the gate path")

    def test_verify_is_what_asks_for_a_version(self):
        found = analyst._probe(analyst.HOSTS[0], verify=False)
        if found["available"]:
            self.assertNotIn("probed_version", found)


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
        self.assertEqual(line["unpriced_runs"], 0)

    def test_a_dollar_total_never_stands_in_for_runs_it_does_not_cover(self):
        # Codex sessions are priced at $0 here by design, so on a machine using
        # both hosts a bare dollar figure describes only half the runs.
        # Measured: 8 analyst runs, 4 of them unpriced, "$0.14" covering four.
        from aiwatcher_cli import ui
        rows = [self._session("claude", "C:/p/.aiwatcher/analyst", 0.14),
                self._session("codex", "C:/p/.aiwatcher/analyst", 0.0)]
        line = ui._analyst_overhead(rows, 7)
        self.assertEqual(line["unpriced_runs"], 1)
        self.assertIn("does not price", line["label"])
        # Tokens are the denominator that holds for every host.
        self.assertIn(line["tokens_label"], line["detail"])


class ConsentAndCapTest(unittest.TestCase):
    """Nothing spawns before the user agrees, and nothing spawns past the cap.

    Both are checked before the spawn rather than after it. A product whose
    anchor story is a runaway agent bill does not get to ship a budget that is
    only noticed on the way out.
    """

    def setUp(self):
        import tempfile
        from aiwatcher_cli import local_state
        self._tmp = tempfile.TemporaryDirectory(prefix="aiw-consent-")
        self._prev = os.environ.get("AIWATCHER_STATE_FILE")
        os.environ["AIWATCHER_STATE_FILE"] = str(Path(self._tmp.name) / "state.json")
        self.local_state = local_state
        self.project = str(Path(self._tmp.name) / "proj")
        Path(self.project).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AIWATCHER_STATE_FILE", None)
        else:
            os.environ["AIWATCHER_STATE_FILE"] = self._prev
        self._tmp.cleanup()

    def test_consent_is_unset_until_it_is_answered(self):
        self.assertIsNone(self.local_state.analyst_consent(self.project))

    def test_consent_is_remembered_per_project(self):
        self.local_state.record_analyst_consent(self.project, allowed=True)
        self.assertTrue(self.local_state.analyst_consent(self.project)["allowed"])
        other = self.project + "-other"
        self.assertIsNone(self.local_state.analyst_consent(other),
                          "consent for one repository must not authorise another")

    def test_declining_is_remembered_too(self):
        # Otherwise "no" means "ask me again on the next prompt".
        self.local_state.record_analyst_consent(self.project, allowed=False)
        self.assertFalse(self.local_state.analyst_consent(self.project)["allowed"])

    def test_the_cap_counts_this_month_only(self):
        import datetime as dt
        self.local_state.record_analyst_run(project_path=self.project, cost_usd=1.25)
        spend = self.local_state.analyst_month_spend()
        self.assertEqual(spend["runs"], 1)
        self.assertAlmostEqual(spend["spent_usd"], 1.25)
        self.assertFalse(spend["capped"])
        # A run from a previous month does not eat this month's budget.
        with self.local_state._locked_state():
            data = self.local_state._load()
            data["analyst_runs"].append({
                "project_path": self.project, "cost_usd": 99.0,
                "ran_at": (dt.datetime.now(dt.timezone.utc)
                           - dt.timedelta(days=70)).isoformat()})
            self.local_state._save(data)
        self.assertAlmostEqual(self.local_state.analyst_month_spend()["spent_usd"], 1.25)

    def test_the_cap_trips_at_the_ceiling(self):
        self.local_state.record_analyst_run(
            project_path=self.project,
            cost_usd=self.local_state.ANALYST_MONTHLY_CAP_USD)
        spend = self.local_state.analyst_month_spend()
        self.assertTrue(spend["capped"])
        self.assertEqual(spend["remaining_usd"], 0.0)

    def test_neither_gate_ever_reaches_the_spawn(self):
        # The point of both is that no process starts, so this asserts on the
        # runner never being called rather than on the returned reason string.
        from aiwatcher_cli import ui
        spawned = []

        def runner(*args):
            spawned.append(args)
            raise AssertionError("a spawn happened that should not have")

        original = analyst.run
        try:
            analyst.run = lambda *a, **k: original(*a, **{**k, "runner": runner})
            first = ui.build_second_opinion(
                "delete every migration across the entire database schema and rewrite auth",
                cwd=self.project)
            self.assertTrue(first.get("needs_consent"), first)

            self.local_state.record_analyst_consent(self.project, allowed=False)
            declined = ui.build_second_opinion(
                "delete every migration across the entire database schema and rewrite auth",
                cwd=self.project)
            self.assertTrue(declined.get("declined"), declined)

            self.local_state.record_analyst_consent(self.project, allowed=True)
            self.local_state.record_analyst_run(
                project_path=self.project,
                cost_usd=self.local_state.ANALYST_MONTHLY_CAP_USD)
            capped = ui.build_second_opinion(
                "delete every migration across the entire database schema and rewrite auth",
                cwd=self.project)
            self.assertTrue(capped.get("capped"), capped)
        finally:
            analyst.run = original
        self.assertEqual(spawned, [], "no analyst may be spawned by any of the three")


class FileContentsOptInTest(unittest.TestCase):
    """Spec 7's file-contents switch, and the rule it turns on and off.

    Verified against the real CLI both ways: with contents off the analyst
    replied BLOCKED and read nothing; with contents on it read a project source
    file and quoted its first line back. A switch that cannot be shown to do
    both is decoration.
    """

    def setUp(self):
        import tempfile
        self.sandbox = Path(tempfile.mkdtemp(prefix="aiw-contents-"))
        self.host = analyst.HOSTS_BY_KEY["claude-code"]

    def _argv(self, contents):
        return analyst._prepare(self.host, "claude", "haiku", self.sandbox,
                                read_contents=contents, project_root=Path("/repo"))

    def test_off_denies_the_tools_rather_than_asking_nicely(self):
        # The permission mode already refuses paths outside the sandbox, but
        # without this the analyst could still read a file sitting inside it --
        # observed doing exactly that, quoting a planted marker back.
        argv = self._argv(False)
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Read", argv[argv.index("--disallowedTools") + 1])
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--add-dir", argv)

    def test_on_opens_the_tree_it_was_given_and_nothing_else(self):
        argv = self._argv(True)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(allowed, analyst.CONTENTS_TOOLS)
        for forbidden in ("Bash", "Edit", "Write", "WebFetch"):
            with self.subTest(tool=forbidden):
                self.assertNotIn(forbidden, allowed)
        # --add-dir is what lets it past the sandbox at all.
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(Path("/repo")))
        self.assertNotIn("--disallowedTools", argv)

    def test_the_prompt_says_which_rule_is_in_force(self):
        off = analyst.build_prompt("x", ("a.py",))
        on = analyst.build_prompt("x", ("a.py",), read_contents=True)
        self.assertIn("cannot read file contents", off)
        self.assertNotIn("cannot read file contents", on)
        self.assertIn("Read only", on)

    def test_a_host_that_cannot_enforce_does_not_claim_it_did(self):
        # Codex has no way to deny its own shell: -s read-only governs writes,
        # and a codex analyst asked for a project file really did shell out to
        # read it. Off means "we asked" there, not "we stopped it", and the
        # result says so rather than borrowing the stronger word.
        def runner(argv, text, cwd, env, timeout):
            (Path(cwd) / "last.json").write_text(json.dumps(_valid()), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        result = analyst.run("x", project_root=self.sandbox, paths=PATHS,
                             detection={"available": True, "cli": "codex-cli",
                                        "executable": "codex"},
                             read_contents=False, runner=runner)
        self.assertFalse(result["contents"])
        self.assertFalse(result["contents_enforced"],
                         "codex cannot enforce this, so it must not claim to")

    def test_claude_code_off_is_enforced(self):
        def runner(argv, text, cwd, env, timeout):
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"result": json.dumps(_valid())}), "")

        result = analyst.run("x", project_root=self.sandbox, paths=PATHS,
                             detection={"available": True, "cli": "claude-code",
                                        "executable": "claude"},
                             read_contents=False, runner=runner)
        self.assertTrue(result["contents_enforced"])

    def test_contents_are_off_until_a_project_asks(self):
        import tempfile
        from aiwatcher_cli import local_state
        prev = os.environ.get("AIWATCHER_STATE_FILE")
        tmp = tempfile.TemporaryDirectory(prefix="aiw-contents-state-")
        os.environ["AIWATCHER_STATE_FILE"] = str(Path(tmp.name) / "state.json")
        try:
            self.assertFalse(local_state.analyst_contents_allowed("/repo"))
            local_state.record_analyst_contents("/repo", allowed=True)
            self.assertTrue(local_state.analyst_contents_allowed("/repo"))
            # Per project: one repository's answer is not another's.
            self.assertFalse(local_state.analyst_contents_allowed("/other"))
            # And paying for a second opinion is not agreeing to be read.
            self.assertIsNone(local_state.analyst_consent("/repo"))
        finally:
            if prev is None:
                os.environ.pop("AIWATCHER_STATE_FILE", None)
            else:
                os.environ["AIWATCHER_STATE_FILE"] = prev
            tmp.cleanup()


class PrivacyClaimTest(unittest.TestCase):
    def test_the_no_llm_calls_claim_is_gone(self):
        # Spec 7. It stopped being true the moment this feature could spawn one,
        # and a privacy claim that is only true until a feature ships is worse
        # than no claim at all.
        from aiwatcher_cli import ui
        claims = " ".join(ui.PRIVACY_CLAIMS)
        self.assertNotIn("No LLM calls", claims)
        self.assertIn("never sends your data anywhere", claims)
        self.assertIn("your own agent", claims)
        # The contents claim has to carry its own exception now that there is
        # one. "Never file contents" full stop stopped being true the moment a
        # switch could turn it on.
        self.assertIn("Never file contents", claims)
        self.assertIn("unless you turn that on", claims)


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
