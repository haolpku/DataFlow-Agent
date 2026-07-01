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
from dataflow_agent.sandbox import (
    MockSandboxClient,
    AgentFlowSandboxClient,
    CodingSandboxClient,
    SandboxClientABC,
    ToolResult,
    ToolSchema,
)
from dataflow_agent.generate.agent_explore_generator import (
    AgentExploreGenerator,
)
from dataflow_agent.generate.agent_explore_tree_generator import (
    AgentExploreTreeGenerator,
)
from dataflow_agent.eval.trajectory_quality_evaluator import (
    TrajectoryQualityEvaluator,
)
from dataflow_agent.filter.trajectory_filter import (
    TrajectoryFilter,
)
from dataflow_agent.refine.trajectory_refiner import (
    TrajectoryRefiner,
)
from dataflow_agent.select.trajectory_selector import (
    TrajectorySelector,
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


# --------------------------------------------------------------------------- #
# TrajectoryRefiner (re-explore to repair low-quality / failed trajectories)
# --------------------------------------------------------------------------- #
def _failed_traj(task="needs repair"):
    """A trajectory that ran out of steps without ever calling finish."""
    return {
        "task": task,
        "steps": [
            {"thought": "search", "action": {"tool": "search", "args": {"query": task}},
             "observation": {"results": ["..."]}, "ok": True, "error": None},
        ],
        "final_answer": None, "num_steps": 1, "success": False,
    }


class _RepairLLM(LLMServingABC):
    """On a refine episode, searches once then finishes successfully.

    The refiner injects the original task under a 'Task: <task>' line inside a
    longer prompt; this LLM just drives search->finish regardless of preamble.
    """

    def generate_from_input(self, user_inputs, system_prompt=""):
        out = []
        for ui in user_inputs:
            if "observation:" in ui:
                out.append(json.dumps({"thought": "now I can answer", "tool": "finish",
                                       "args": {"answer": "REPAIRED"}}))
            else:
                out.append(json.dumps({"thought": "retry properly", "tool": "search",
                                       "args": {"query": "capital of france"}}))
        return out

    def start_serving(self):
        pass

    def cleanup(self):
        pass


def test_refiner_repairs_failed_trajectory():
    """A failed trajectory is re-explored and becomes a successful one."""
    storage = _make_storage([{"trajectory": _failed_traj()}])
    op = TrajectoryRefiner(
        llm_serving=_RepairLLM(), sandbox=MockSandboxClient(), domain="mock",
        max_steps=5, max_workers=1, score_threshold=None,  # only the failed trigger
    )
    op.run(storage.step(), input_key="trajectory", output_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    row = out.iloc[0]
    refined = row["trajectory"]
    if isinstance(refined, str):
        refined = json.loads(refined)
    assert row["_refined"] is True or row["_refined"] == True  # noqa: E712
    assert refined["success"] is True
    assert refined["final_answer"] == "REPAIRED"
    # task string restored to the original (not the augmented prompt)
    assert refined["task"] == "needs repair"
    # original preserved for lineage / comparison
    orig = row["trajectory_original"]
    if isinstance(orig, str):
        orig = json.loads(orig)
    assert orig["success"] is False


def test_refiner_skips_good_trajectory():
    """A successful, high-scoring trajectory is passed through untouched."""
    good = _traj(success=True, final_answer="already good")
    storage = _make_storage([{"trajectory": good, "traj_overall": 0.95}])
    op = TrajectoryRefiner(
        llm_serving=_RepairLLM(), sandbox=MockSandboxClient(), domain="mock",
        score_threshold=0.6, max_workers=1,
    )
    op.run(storage.step(), input_key="trajectory", output_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    row = out.iloc[0]
    assert bool(row["_refined"]) is False
    refined = row["trajectory"]
    if isinstance(refined, str):
        refined = json.loads(refined)
    assert refined["final_answer"] == "already good"  # unchanged


def test_refiner_triggers_on_low_score():
    """A trajectory that 'succeeded' but scored below threshold is refined."""
    low = _traj(success=True, final_answer="weak answer")
    storage = _make_storage([{"trajectory": low, "traj_overall": 0.3}])
    op = TrajectoryRefiner(
        llm_serving=_RepairLLM(), sandbox=MockSandboxClient(), domain="mock",
        refine_failed=False,            # disable the failed trigger
        score_threshold=0.6, score_key="traj_overall", max_workers=1,
    )
    op.run(storage.step(), input_key="trajectory", output_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    row = out.iloc[0]
    assert bool(row["_refined"]) is True
    refined = row["trajectory"]
    if isinstance(refined, str):
        refined = json.loads(refined)
    assert refined["final_answer"] == "REPAIRED"


def test_refiner_diagnosis_detects_failure_modes():
    # never-finished -> "never produced a final answer"
    d1 = TrajectoryRefiner._diagnose(_failed_traj())
    assert "final answer" in d1

    # invalid tool -> diagnosis mentions the bogus tool
    bad_tool = {"task": "t", "success": True, "final_answer": "x", "num_steps": 1,
                "steps": [{"action": {"tool": "magic", "args": {}}, "invalid_tool": True,
                           "observation": None}]}
    d2 = TrajectoryRefiner._diagnose(bad_tool)
    assert "magic" in d2

    # repeated action -> diagnosis mentions a loop
    loop = {"task": "t", "success": True, "final_answer": "x", "num_steps": 2,
            "steps": [
                {"action": {"tool": "search", "args": {"q": "z"}}, "observation": {}, "ok": True},
                {"action": {"tool": "search", "args": {"q": "z"}}, "observation": {}, "ok": True},
            ]}
    d3 = TrajectoryRefiner._diagnose(loop)
    assert "loop" in d3


def test_refiner_is_registered():
    from dataflow.utils.registry import OPERATOR_REGISTRY
    assert OPERATOR_REGISTRY.get("TrajectoryRefiner") is TrajectoryRefiner


def test_refiner_closes_the_loop_with_evaluator():
    """Evaluator scores low -> Refiner repairs -> re-Evaluator scores high.

    Exercises the full Generator-less slice Evaluate->Refine->Evaluate on a
    hand-built failed trajectory, proving the refined column is judge-ready.
    """
    storage = _make_storage([{"trajectory": _failed_traj()}])

    # 1) judge the failed trajectory -> low overall
    low_verdict = {"goal_achievement": 1, "efficiency": 2, "coherence": 2,
                   "tool_use": 2, "overall": 0.2, "rationale": "no answer"}
    TrajectoryQualityEvaluator(llm_serving=_JudgeLLM(low_verdict), max_workers=1).run(
        storage.step(), input_key="trajectory", output_key="traj_overall")

    # 2) refine the low-scoring trajectory
    TrajectoryRefiner(
        llm_serving=_RepairLLM(), sandbox=MockSandboxClient(), domain="mock",
        score_threshold=0.6, score_key="traj_overall", max_workers=1,
    ).run(storage.step(), input_key="trajectory", output_key="trajectory")

    # 3) re-judge the refined trajectory -> high overall
    high_verdict = {"goal_achievement": 5, "efficiency": 5, "coherence": 5,
                    "tool_use": 5, "overall": 0.95, "rationale": "now correct"}
    TrajectoryQualityEvaluator(llm_serving=_JudgeLLM(high_verdict), max_workers=1).run(
        storage.step(), input_key="trajectory", output_key="traj_overall")

    out = storage.step().read(output_type="dataframe")
    refined = out["trajectory"].iloc[0]
    if isinstance(refined, str):
        refined = json.loads(refined)
    assert refined["success"] is True
    assert out["traj_overall"].iloc[0] == pytest.approx(0.95)


# --------------------------------------------------------------------------- #
# CodingSandboxClient (coding / working-agent environment)
# --------------------------------------------------------------------------- #
def test_coding_sandbox_file_roundtrip():
    sb = CodingSandboxClient(allow_shell=False)
    wid = sb.new_worker_id()
    sb.create_session("coding", worker_id=wid)
    w = sb.execute("write_file", {"path": "hello.txt", "content": "hi there"}, worker_id=wid)
    assert w.ok and w.observation["bytes_written"] == 8
    r = sb.execute("read_file", {"path": "hello.txt"}, worker_id=wid)
    assert r.ok and r.observation["content"] == "hi there"
    ls = sb.execute("list_files", {"path": "."}, worker_id=wid)
    assert ls.ok and any(e["name"] == "hello.txt" for e in ls.observation["entries"])
    sb.destroy_session("coding", worker_id=wid)


def test_coding_sandbox_run_python():
    sb = CodingSandboxClient(allow_shell=False)
    wid = sb.new_worker_id()
    sb.create_session("coding", worker_id=wid)
    res = sb.execute("run_python", {"code": "print(6 * 7)"}, worker_id=wid)
    assert res.ok
    assert res.observation["exit_code"] == 0
    assert "42" in res.observation["stdout"]
    sb.destroy_session("coding", worker_id=wid)


def test_coding_sandbox_seed_files_and_tests():
    """Seed a failing test + buggy module, run pytest (fails), fix it, re-run (passes)."""
    seed = {
        "mymath.py": "def add(a, b):\n    return a - b  # BUG\n",
        "test_mymath.py": "from mymath import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    }
    sb = CodingSandboxClient(seed_files=seed, allow_shell=False, timeout=60)
    wid = sb.new_worker_id()
    sb.create_session("coding", worker_id=wid)

    failing = sb.execute("run_tests", {"path": "."}, worker_id=wid)
    assert failing.ok and failing.observation["exit_code"] != 0  # bug -> test fails

    # the agent fixes the bug
    sb.execute("write_file", {"path": "mymath.py",
                              "content": "def add(a, b):\n    return a + b\n"}, worker_id=wid)
    passing = sb.execute("run_tests", {"path": "."}, worker_id=wid)
    assert passing.ok and passing.observation["exit_code"] == 0  # fixed -> test passes
    sb.destroy_session("coding", worker_id=wid)


def test_coding_sandbox_path_escape_rejected():
    sb = CodingSandboxClient(allow_shell=False)
    wid = sb.new_worker_id()
    sb.create_session("coding", worker_id=wid)
    bad = sb.execute("read_file", {"path": "../../../../etc/passwd"}, worker_id=wid)
    assert not bad.ok and bad.code == 4030  # escape blocked
    sb.destroy_session("coding", worker_id=wid)


def test_coding_sandbox_shell_toggle():
    off = CodingSandboxClient(allow_shell=False)
    names = {t.name for t in off.list_tools()}
    assert "run_shell" not in names
    wid = off.new_worker_id()
    off.create_session("coding", worker_id=wid)
    blocked = off.execute("run_shell", {"command": "echo hi"}, worker_id=wid)
    assert not blocked.ok and blocked.code == 4030

    on = CodingSandboxClient(allow_shell=True)
    assert "run_shell" in {t.name for t in on.list_tools()}


def test_coding_sandbox_workspace_isolation():
    """Two workers must not see each other's files."""
    sb = CodingSandboxClient(allow_shell=False)
    a, b = sb.new_worker_id(), sb.new_worker_id()
    sb.create_session("coding", worker_id=a)
    sb.create_session("coding", worker_id=b)
    sb.execute("write_file", {"path": "secret.txt", "content": "A"}, worker_id=a)
    # worker b should not find worker a's file
    r = sb.execute("read_file", {"path": "secret.txt"}, worker_id=b)
    assert not r.ok and r.code == 4040
    sb.destroy_session("coding", worker_id=a)
    sb.destroy_session("coding", worker_id=b)


def test_coding_agent_end_to_end_fix_bug():
    """Full agent loop: scripted LLM drives read->write->run_tests->finish."""
    seed = {
        "calc.py": "def square(x):\n    return x + x  # BUG\n",
        "test_calc.py": "from calc import square\n\ndef test_square():\n    assert square(3) == 9\n",
    }
    task = "fix the bug in calc.py so the tests pass"
    scripts = {task: [
        json.dumps({"thought": "see the buggy file", "tool": "read_file",
                    "args": {"path": "calc.py"}}),
        json.dumps({"thought": "fix it", "tool": "write_file",
                    "args": {"path": "calc.py", "content": "def square(x):\n    return x * x\n"}}),
        json.dumps({"thought": "verify", "tool": "run_tests", "args": {"path": "."}}),
        json.dumps({"thought": "tests pass", "tool": "finish",
                    "args": {"answer": "fixed square to use multiplication"}}),
    ]}
    storage = _make_storage([{"query": task}])
    op = AgentExploreGenerator(
        llm_serving=FakeLLMServing(scripts),
        sandbox=CodingSandboxClient(seed_files=seed, allow_shell=False, timeout=60),
        domain="coding", max_steps=8, max_workers=1,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")
    traj = storage.step().read(output_type="dataframe")["trajectory"].iloc[0]
    if isinstance(traj, str):
        traj = json.loads(traj)
    assert traj["success"] is True
    tools_used = [s["action"]["tool"] for s in traj["steps"]]
    assert tools_used == ["read_file", "write_file", "run_tests", "finish"]
    # the run_tests step observed a passing suite (exit_code 0)
    test_step = traj["steps"][2]
    assert test_step["observation"]["exit_code"] == 0


def test_coding_sandbox_in_registry_via_import():
    # CodingSandboxClient is exported at the package top level
    import dataflow_agent
    assert dataflow_agent.CodingSandboxClient is CodingSandboxClient


# --------------------------------------------------------------------------- #
# TrajectorySelector (ported from AgentFlow: top-N diverse selection)
# --------------------------------------------------------------------------- #
def _traj_with(tools_and_obs, task="t", success=True):
    """Build a trajectory with given (tool, observation) per step."""
    steps = []
    for tool, obs in tools_and_obs:
        steps.append({"thought": "x", "action": {"tool": tool, "args": {"q": obs[:3]}},
                      "observation": obs, "ok": True})
    steps.append({"thought": "done", "action": {"tool": "finish", "args": {"answer": "a"}},
                  "observation": {"answer": "a"}})
    return {"task": task, "steps": steps, "final_answer": "a",
            "num_steps": len(steps), "success": success}


def test_selector_scores_and_picks_topn():
    # deep+diverse+long should outrank shallow ones
    deep = _traj_with([("search", "X" * 100), ("read", "Y" * 100), ("exec", "Z" * 100)])
    shallow = _traj_with([("search", "s")])
    mid = _traj_with([("search", "m" * 50), ("search", "m" * 50)])
    rows = [{"trajectory": shallow}, {"trajectory": deep}, {"trajectory": mid}]
    storage = _make_storage(rows)
    op = TrajectorySelector(max_selected=2, min_depth=2, mode="rows")
    op.run(storage.step(), input_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    assert len(out) == 2  # top-2 kept
    kept = [json.loads(t) if isinstance(t, str) else t for t in out["trajectory"]]
    # the deep/diverse trajectory must be among the selected
    assert any(len(t["steps"]) == 4 for t in kept)


def test_selector_jaccard_dedup():
    # two near-identical trajectories: only one should survive
    a = _traj_with([("search", "AAAA" * 30), ("read", "BBBB" * 30)])
    b = _traj_with([("search", "AAAA" * 30), ("read", "BBBB" * 30)])  # identical actions
    c = _traj_with([("exec", "CCCC" * 30), ("inspect", "DDDD" * 30)])  # different
    rows = [{"trajectory": a}, {"trajectory": b}, {"trajectory": c}]
    storage = _make_storage(rows)
    op = TrajectorySelector(max_selected=3, min_depth=2,
                            path_similarity_threshold=0.7, mode="rows")
    op.run(storage.step(), input_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    # a and b collapse (Jaccard=1.0 > 0.7) -> at most 2 distinct survive
    assert len(out) == 2


def test_selector_min_depth_filter():
    short = _traj_with([("search", "s")])         # 2 steps incl finish
    ok = _traj_with([("search", "s"), ("read", "r"), ("exec", "e")])  # 4 steps
    rows = [{"trajectory": short}, {"trajectory": ok}]
    storage = _make_storage(rows)
    op = TrajectorySelector(max_selected=5, min_depth=4, mode="rows")
    op.run(storage.step(), input_key="trajectory")
    out = storage.step().read(output_type="dataframe")
    assert len(out) == 1  # only the >=4-step trajectory qualifies


def test_selector_tree_mode_on_real_tree():
    """Feed a real AgentExploreTreeGenerator tree; select from its paths."""
    storage = _make_storage([{"query": "explore me"}])
    AgentExploreTreeGenerator(
        llm_serving=_TreeLLM(), sandbox=MockSandboxClient(), domain="mock",
        max_depth=3, branching_factor=2, max_children=2, max_workers=1,
    ).run(storage.step(), input_key="query", output_key="tree")
    sel = TrajectorySelector(max_selected=1, min_depth=1, mode="tree")
    sel.run(storage.step(), input_key="tree", output_key="selected")
    out = storage.step().read(output_type="dataframe")
    assert "selected" in out.columns
    chosen = out["selected"].iloc[0]
    if isinstance(chosen, str):
        chosen = json.loads(chosen)
    assert isinstance(chosen, list) and len(chosen) <= 1
    assert out["selected_count"].iloc[0] == len(chosen)


def test_selector_registered():
    from dataflow.utils.registry import OPERATOR_REGISTRY
    assert OPERATOR_REGISTRY.get("TrajectorySelector") is TrajectorySelector


def test_selector_score_matches_agentflow_formula():
    """Faithful-port check: score equals AgentFlow's depth+info+diversity."""
    # single trajectory pool -> info normalization is 0 (min==max), so only
    # depth(40 capped at 5 steps) + diversity apply.
    t = _traj_with([("search", "x"), ("read", "y"), ("exec", "z")])  # 4 steps, 4 tools
    op = TrajectorySelector(max_selected=1, min_depth=1, total_tools=4, mode="rows")
    # depth: min(4/5,1)*40 = 32 ; info: 0 (single-item pool) ;
    # diversity: distinct tools {search,read,exec,finish}=4 / 4 * 30 = 30
    score = op._score(t, avg_obs_length=0, min_length=0, length_range=1, total_tools=4)
    assert abs(score - (min(4 / 5.0, 1.0) * 40 + 0 + 4 / 4 * 30)) < 1e-9


def test_tree_depth_threshold_collapses_branching():
    """With depth_threshold, deep levels keep a single child -> fewer nodes."""
    wide = _make_storage([{"query": "explore me"}])
    AgentExploreTreeGenerator(
        llm_serving=_TreeLLM(), sandbox=MockSandboxClient(), domain="mock",
        max_depth=3, branching_factor=2, max_children=2, max_workers=1,
    ).run(wide.step(), input_key="query", output_key="tree")
    wide_rec = wide.step().read(output_type="dataframe")["tree"].iloc[0]
    if isinstance(wide_rec, str):
        wide_rec = json.loads(wide_rec)

    narrow = _make_storage([{"query": "explore me"}])
    AgentExploreTreeGenerator(
        llm_serving=_TreeLLM(), sandbox=MockSandboxClient(), domain="mock",
        max_depth=3, branching_factor=2, max_children=2, max_workers=1,
        depth_threshold=1,  # from depth 1 on, only 1 child
    ).run(narrow.step(), input_key="query", output_key="tree")
    narrow_rec = narrow.step().read(output_type="dataframe")["tree"].iloc[0]
    if isinstance(narrow_rec, str):
        narrow_rec = json.loads(narrow_rec)

    # collapsing branching at depth>=1 must not produce more nodes than the wide tree
    assert narrow_rec["num_nodes"] <= wide_rec["num_nodes"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))