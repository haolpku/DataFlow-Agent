"""
End-to-end tests for the agent-explore operator against the mock sandbox.

These run fully offline:
  - MockSandboxClient replaces any real sandbox (no network).
  - A scripted FakeLLMServing replaces a real LLM, returning a fixed sequence of
    tool-call JSON strings so the loop is deterministic.

Run:  pytest test/test_agentic_explore.py -v
"""

import json
import os
import sys
import tempfile

import pandas as pd
import pytest

# Make the repo importable when run directly.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dataflow.core import LLMServingABC
from dataflow.utils.storage import FileStorage
from dataflow.operators.agentic_explore.sandbox import (
    MockSandboxClient,
    AgentFlowSandboxClient,
    SandboxClientABC,
    ToolResult,
    ToolSchema,
)
from dataflow.operators.agentic_explore.generate.agent_explore_generator import (
    AgentExploreGenerator,
)
from dataflow.operators.agentic_explore.generate.agent_explore_tree_generator import (
    AgentExploreTreeGenerator,
)
from dataflow.operators.agentic_explore.eval.trajectory_quality_evaluator import (
    TrajectoryQualityEvaluator,
)
from dataflow.operators.agentic_explore.filter.trajectory_filter import (
    TrajectoryFilter,
)


class FakeLLMServing(LLMServingABC):
    """Returns a pre-scripted response for each turn, ignoring the prompt."""

    def __init__(self, scripts):
        # scripts: dict mapping task -> list[str] of per-step responses
        self.scripts = scripts
        self._cursor = {}

    def generate_from_input(self, user_inputs, system_prompt=""):
        out = []
        for ui in user_inputs:
            # Identify the task from the prompt prefix "Task: <task>"
            task = None
            for line in ui.splitlines():
                if line.startswith("Task: "):
                    task = line[len("Task: "):].strip()
                    break
            seq = self.scripts.get(task, [])
            i = self._cursor.get(task, 0)
            resp = seq[i] if i < len(seq) else json.dumps(
                {"thought": "done", "tool": "finish", "args": {"answer": "fallback"}}
            )
            self._cursor[task] = i + 1
            out.append(resp)
        return out

    def start_serving(self):
        pass

    def cleanup(self):
        pass


def _make_storage(rows):
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "input.jsonl")
    pd.DataFrame(rows).to_json(path, orient="records", lines=True, force_ascii=False)
    return FileStorage(
        first_entry_file_name=path,
        cache_path=os.path.join(tmpdir, "cache"),
        cache_type="jsonl",
    )


# --------------------------------------------------------------------------- #
# tool-call parsing
# --------------------------------------------------------------------------- #
def test_extract_json_bare():
    obj = AgentExploreGenerator._extract_json('{"tool": "search", "args": {"query": "x"}}')
    assert obj["tool"] == "search"


def test_extract_json_fenced():
    text = 'sure!\n```json\n{"tool": "finish", "args": {"answer": "42"}}\n```\n'
    obj = AgentExploreGenerator._extract_json(text)
    assert obj["tool"] == "finish" and obj["args"]["answer"] == "42"


def test_extract_json_with_trailing_prose():
    text = '{"tool": "search", "args": {"query": "y"}} and then I will think.'
    obj = AgentExploreGenerator._extract_json(text)
    assert obj["tool"] == "search"


def test_extract_json_garbage_returns_none():
    assert AgentExploreGenerator._extract_json("no json here") is None


# --------------------------------------------------------------------------- #
# full episode loop
# --------------------------------------------------------------------------- #
def test_single_episode_search_then_finish():
    task = "what is the capital of france"
    scripts = {
        task: [
            json.dumps({"thought": "look it up", "tool": "search",
                        "args": {"query": "capital of france"}}),
            json.dumps({"thought": "answer", "tool": "finish",
                        "args": {"answer": "Paris"}}),
        ]
    }
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=MockSandboxClient(),
        domain="mock",
        max_steps=5,
        max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")

    df = storage.step().read(output_type="dataframe")
    traj = df["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    assert traj["success"] is True
    assert traj["final_answer"] == "Paris"
    assert traj["num_steps"] == 2
    assert traj["steps"][0]["action"]["tool"] == "search"
    # observation from mock should contain the canned snippet
    assert "Paris" in json.dumps(traj["steps"][0]["observation"])


def test_max_steps_termination():
    task = "loop forever"
    # always search, never finish -> must stop at max_steps
    scripts = {
        task: [json.dumps({"thought": "again", "tool": "search",
                           "args": {"query": "loop forever"}})] * 20
    }
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=MockSandboxClient(),
        domain="mock",
        max_steps=3,
        max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    traj = storage.step().read(output_type="dataframe")["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    assert traj["num_steps"] == 3
    assert traj["success"] is False


def test_unparseable_response_is_recorded():
    task = "bad model"
    scripts = {task: ["I refuse to output JSON",
                      json.dumps({"thought": "ok", "tool": "finish",
                                  "args": {"answer": "done"}})]}
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=MockSandboxClient(),
        domain="mock",
        max_steps=5,
        max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    traj = storage.step().read(output_type="dataframe")["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    assert traj["steps"][0].get("parse_error") is True
    assert traj["success"] is True  # recovered on step 2


def test_concurrent_multi_row():
    tasks = [f"task {i}" for i in range(8)]
    scripts = {
        t: [json.dumps({"thought": "done", "tool": "finish",
                        "args": {"answer": f"ans-{t}"}})]
        for t in tasks
    }
    storage = _make_storage([{"query": t} for t in tasks])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=MockSandboxClient(),
        domain="mock",
        max_steps=3,
        max_workers=4,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    df = storage.step().read(output_type="dataframe")
    assert len(df) == 8
    for i, row in df.iterrows():
        traj = row["trajectory"]
        if isinstance(traj, str):
            traj = json.loads(traj)
        # each row's answer must match its own task (no cross-talk)
        assert traj["final_answer"] == f"ans-{traj['task']}"


def test_stateful_session_lifecycle():
    task = "x"
    scripts = {task: [json.dumps({"thought": "d", "tool": "finish",
                                  "args": {"answer": "y"}})]}
    sandbox = MockSandboxClient(stateful=True)
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=sandbox,
        domain="vm",
        max_steps=3,
        max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    assert len(sandbox.created_sessions) == 1
    assert len(sandbox.destroyed_sessions) == 1


# --------------------------------------------------------------------------- #
# generality hardening: truncation, invalid-tool, non-web domains
# --------------------------------------------------------------------------- #
class _SQLLikeSandbox(SandboxClientABC):
    """A structured (non-web) domain: returns rows, not prose."""

    def list_tools(self, domain=None):
        return [
            ToolSchema(name="execute", description="run SQL",
                       parameters=[{"name": "query"}]),
            ToolSchema(name="finish", description="finish",
                       parameters=[{"name": "answer"}]),
        ]

    def execute(self, action, params=None, *, worker_id=None, timeout=None):
        bare = action.split(":", 1)[-1]
        if bare == "execute":
            return ToolResult(ok=True, observation={"rows": [{"n": 42}], "columns": ["n"]})
        return ToolResult(ok=False, error="unknown", code=4040)


def test_structured_domain_sql_like():
    task = "count rows"
    scripts = {task: [
        json.dumps({"thought": "query", "tool": "execute",
                    "args": {"query": "SELECT count(*) FROM t"}}),
        json.dumps({"thought": "done", "tool": "finish", "args": {"answer": "42"}}),
    ]}
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=_SQLLikeSandbox(),
        domain="sql", max_steps=5, max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    traj = storage.step().read(output_type="dataframe")["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    assert traj["success"] and traj["final_answer"] == "42"
    # structured rows survived into the observation
    assert traj["steps"][0]["observation"]["rows"] == [{"n": 42}]


def test_observation_truncation():
    task = "big"
    scripts = {task: [
        json.dumps({"thought": "search", "tool": "search", "args": {"query": "x"}}),
        json.dumps({"thought": "done", "tool": "finish", "args": {"answer": "ok"}}),
    ]}
    # sandbox returns a huge blob
    big = MockSandboxClient(knowledge={"x": "A" * 50000})
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=big, domain="mock", max_steps=5, max_workers=1,
        max_observation_chars=1000,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    traj = storage.step().read(output_type="dataframe")["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    obs = traj["steps"][0]["observation"]
    assert isinstance(obs, str) and "truncated" in obs
    assert len(obs) < 1200  # ~max_observation_chars + marker


def test_invalid_tool_name_is_rejected():
    task = "halluc"
    scripts = {task: [
        json.dumps({"thought": "use fake tool", "tool": "teleport", "args": {}}),
        json.dumps({"thought": "ok now finish", "tool": "finish", "args": {"answer": "z"}}),
    ]}
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=MockSandboxClient(), domain="mock",
        max_steps=5, max_workers=1, validate_tool_names=True,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    traj = storage.step().read(output_type="dataframe")["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    # the bogus tool was recorded, never executed, and the agent recovered
    assert traj["steps"][0].get("invalid_tool") is True
    assert traj["success"] is True


# --------------------------------------------------------------------------- #
# registry + AgentFlow client envelope mapping (no network)
# --------------------------------------------------------------------------- #
def test_operator_is_registered():
    from dataflow.utils.registry import OPERATOR_REGISTRY
    cls = OPERATOR_REGISTRY.get("AgentExploreGenerator")
    assert cls is AgentExploreGenerator


def test_agentflow_client_envelope_mapping():
    # _to_result is a pure function over the {code,message,data,meta} envelope.
    ok_body = {"code": 0, "message": "success",
               "data": {"results": ["a"]}, "meta": {"execution_time_ms": 12.3}}
    r = AgentFlowSandboxClient._to_result(ok_body)
    assert r.ok and r.observation == {"results": ["a"]} and r.elapsed_ms == 12.3

    err_body = {"code": 4040, "message": "tool not found", "data": None, "meta": {}}
    r2 = AgentFlowSandboxClient._to_result(err_body)
    assert not r2.ok and r2.code == 4040 and r2.error == "tool not found"

    final_body = {"code": 0, "message": "success",
                  "data": {"answer": "done", "is_final": True}, "meta": {}}
    r3 = AgentFlowSandboxClient._to_result(final_body)
    assert r3.ok and r3.is_final


def test_agentflow_client_qualify_prefix():
    assert AgentFlowSandboxClient._qualify("search", "web") == "web:search"
    assert AgentFlowSandboxClient._qualify("rag:search", "web") == "rag:search"


# --------------------------------------------------------------------------- #
# TrajectoryFilter (deterministic, no LLM)
# --------------------------------------------------------------------------- #
def _traj(success=True, steps=None, final_answer="ok", num_steps=None):
    steps = steps if steps is not None else [
        {"action": {"tool": "search", "args": {"q": "x"}}, "observation": {"r": 1}, "ok": True},
        {"action": {"tool": "finish", "args": {"answer": final_answer}},
         "observation": {"answer": final_answer}},
    ]
    return {"task": "t", "steps": steps, "final_answer": final_answer,
            "num_steps": num_steps if num_steps is not None else len(steps),
            "success": success}


def test_trajectory_filter_keeps_and_drops():
    rows = [
        {"trajectory": _traj(success=True)},                       # keep
        {"trajectory": _traj(success=False)},                      # drop: not_success
        {"trajectory": _traj(success=True, final_answer="")},      # drop: empty_answer
        {"trajectory": _traj(success=True, steps=[                 # drop: parse_error
            {"parse_error": True, "action": {"tool": None, "args": {}}, "observation": None},
        ])},
        {"trajectory": _traj(success=True, steps=[                 # drop: invalid_tool
            {"invalid_tool": True, "action": {"tool": "x", "args": {}}, "observation": None},
        ])},
    ]
    storage = _make_storage(rows)
    op = TrajectoryFilter(require_success=True, require_nonempty_answer=True,
                          drop_parse_errors=True, drop_invalid_tools=True)
    op.run(storage.step(), input_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    assert len(out) == 1  # only the clean success survives


def test_trajectory_filter_repeated_actions():
    looping = _traj(success=True, steps=[
        {"action": {"tool": "search", "args": {"q": "x"}}, "observation": {}, "ok": True},
        {"action": {"tool": "search", "args": {"q": "x"}}, "observation": {}, "ok": True},
        {"action": {"tool": "search", "args": {"q": "x"}}, "observation": {}, "ok": True},
        {"action": {"tool": "finish", "args": {"answer": "z"}}, "observation": {"answer": "z"}},
    ])
    storage = _make_storage([{"trajectory": looping}])
    op = TrajectoryFilter(max_repeated_actions=2)
    op.run(storage.step(), input_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    assert len(out) == 0  # 3 identical searches > 2 -> dropped


# --------------------------------------------------------------------------- #
# TrajectoryQualityEvaluator (LLM-as-judge, scripted)
# --------------------------------------------------------------------------- #
class _JudgeLLM(LLMServingABC):
    def __init__(self, verdict):
        self._verdict = verdict

    def generate_from_input(self, user_inputs, system_prompt=""):
        return [json.dumps(self._verdict) for _ in user_inputs]

    def start_serving(self):
        pass

    def cleanup(self):
        pass


def test_trajectory_quality_evaluator_scores():
    verdict = {"goal_achievement": 5, "efficiency": 4, "coherence": 5,
               "tool_use": 4, "overall": 0.88, "rationale": "solid"}
    rows = [{"trajectory": _traj()}, {"trajectory": _traj(final_answer="other")}]
    storage = _make_storage(rows)
    op = TrajectoryQualityEvaluator(llm_serving=_JudgeLLM(verdict), max_workers=2)
    op.run(storage.step(), input_key="trajectory", output_key="traj_overall")
    out = storage.step().read(output_type="dataframe")
    assert "traj_overall" in out.columns
    assert "traj_goal_achievement" in out.columns
    assert out["traj_overall"].iloc[0] == 0.88
    assert out["traj_goal_achievement"].iloc[0] == 5
    assert out["traj_rationale"].iloc[0] == "solid"


def test_trajectory_quality_evaluator_handles_bad_verdict():
    class _BadLLM(_JudgeLLM):
        def generate_from_input(self, user_inputs, system_prompt=""):
            return ["I won't give JSON" for _ in user_inputs]
    storage = _make_storage([{"trajectory": _traj()}])
    op = TrajectoryQualityEvaluator(llm_serving=_BadLLM({}), max_workers=1)
    op.run(storage.step(), input_key="trajectory", output_key="traj_overall")
    out = storage.step().read(output_type="dataframe")
    # unparseable verdict -> None score, rationale records the reason
    assert pd.isna(out["traj_overall"].iloc[0])
    assert out["traj_rationale"].iloc[0] == "unparseable_verdict"


# --------------------------------------------------------------------------- #
# AgentExploreTreeGenerator (branching)
# --------------------------------------------------------------------------- #
class _TreeLLM(LLMServingABC):
    """Step 0: propose two distinct searches. Step >=1: finish."""

    def generate_from_input(self, user_inputs, system_prompt=""):
        out = []
        for ui in user_inputs:
            if "observation:" in ui:  # already executed a tool -> finish
                out.append(json.dumps({"thought": "done", "tool": "finish",
                                       "args": {"answer": "A"}}))
            else:  # root node: vary by call index isn't available, so alternate
                # Return two different queries across the batch via a counter.
                out.append(None)  # placeholder, replaced below
        # fill placeholders with distinct actions so dedup keeps >1 child
        qid = 0
        for i, v in enumerate(out):
            if v is None:
                out[i] = json.dumps({"thought": "search", "tool": "search",
                                     "args": {"query": f"q{qid}"}})
                qid += 1
        return out

    def start_serving(self):
        pass

    def cleanup(self):
        pass


def test_tree_generator_branches_and_flattens():
    storage = _make_storage([{"query": "explore me"}])
    op = AgentExploreTreeGenerator(
        llm_serving=_TreeLLM(),
        sandbox=MockSandboxClient(),
        domain="mock",
        max_depth=3,
        branching_factor=2,
        max_children=2,
        max_nodes=20,
        max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="tree")
    out = storage.step().read(output_type="dataframe")
    rec = out["tree"].iloc[0]
    if isinstance(rec, str):
        rec = json.loads(rec)
    # root sampled 2 distinct searches -> at least 2 paths, each ending in finish
    assert rec["num_paths"] >= 2
    assert rec["num_success_paths"] >= 2
    # each flattened path is a linear-trajectory shape (Filter/Evaluator-ready)
    p = rec["paths"][0]
    assert set(p.keys()) >= {"task", "steps", "final_answer", "num_steps", "success"}


def test_tree_paths_feed_filter():
    """End-to-end: tree -> explode paths -> filter keeps successful ones."""
    storage = _make_storage([{"query": "explore me"}])
    op = AgentExploreTreeGenerator(
        llm_serving=_TreeLLM(), sandbox=MockSandboxClient(), domain="mock",
        max_depth=3, branching_factor=2, max_children=2, max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="tree")
    rec = storage.step().read(output_type="dataframe")["tree"].iloc[0]
    if isinstance(rec, str):
        rec = json.loads(rec)
    # explode the tree's paths into one-trajectory-per-row, then filter
    path_rows = [{"trajectory": p} for p in rec["paths"]]
    s2 = _make_storage(path_rows)
    TrajectoryFilter(require_success=True).run(s2.step(), input_key="trajectory")
    kept = s2.step().read(output_type="dataframe")
    assert len(kept) == len(path_rows)  # all paths succeeded in the mock


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
