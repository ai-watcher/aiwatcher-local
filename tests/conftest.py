"""Give every test its own local state file.

Without this, any test that drives a real code path -- a hook entrypoint, a
baseline refresh, a heartbeat -- writes into the developer's own
~/.aiwatcher/local-state.json. That is not hypothetical: a single suite run
seeds 9 hook events plus baselines, survival summaries, brief tokens, heartbeat
and receipt baseline into it. On a real machine that was consuming more than
half of the 50-event hook ring buffer with cwd=/repo fixtures, evicting genuine
evidence and leaving surface coverage reading test data back as proof.

Isolating here rather than in each test is deliberate: the leak came from tests
that never intended to touch state at all, so the fix has to cover the ones
nobody thought to audit. Tests that manage their own state file keep working --
their patch.dict simply overrides these values for the duration of the test.

state_path() reads AIWATCHER_STATE_FILE first and falls back to AIWATCHER_HOME,
so both are set; overriding only the first would leave the fallback pointing at
the real home.

Note this file is a no-op under `python -m unittest`. CI runs pytest for that
reason -- see .github/workflows/ci.yml.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_local_state(tmp_path, monkeypatch):
    home = tmp_path / "aiwatcher-home"
    home.mkdir()
    monkeypatch.setenv("AIWATCHER_HOME", str(home))
    monkeypatch.setenv("AIWATCHER_STATE_FILE", str(home / "local-state.json"))
