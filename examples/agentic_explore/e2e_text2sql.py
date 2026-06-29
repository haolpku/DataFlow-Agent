"""
E2E: drive the FOUR agentic_explore operators against a REAL AgentFlow
text2sql sandbox over HTTP.

LLM is scripted here (deterministic) so this run isolates the sandbox HTTP
path. Swap _SQLAgentLLM for APILLMServing_request(api_url=..., model_name=...,
api_key=...) to use a real model.

Assumes a sandbox server is already serving text2sql on --base-url.
"""
import argparse
import json
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # DataFlow root on path

from dataflow.core import LLMServingABC
from dataflow.utils.storage import FileStorage
from dataflow.operators.agentic_explore.sandbox import AgentFlowSandboxClient
from dataflow.operators.agentic_explore.generate.agent_explore_generator import AgentExploreGenerator
from dataflow.operators.agentic_explore.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
from dataflow.operators.agentic_explore.filter.trajectory_filter import TrajectoryFilter


class _SQLAgentLLM(LLMServingABC):
    """Scripted text2sql agent: get_schema -> execute -> finish."""
    def __init__(self):
        self._n = {}
    def generate_from_input(self, user_inputs, system_prompt=""):
        out = []
        for ui in user_inputs:
            # crude task id = the Task line
            task = next((l for l in ui.splitlines() if l.startswith("Task:")), "")
            i = self._n.get(task, 0); self._n[task] = i + 1
            if "observation:" not in ui and i == 0:
                out.append(json.dumps({"thought": "inspect schema", "tool": "get_schema",
                                       "args": {"db_id": "retail"}}))
            elif ui.count("observation:") == 1:
                out.append(json.dumps({"thought": "run the query", "tool": "execute",
                                       "args": {"db_id": "retail",
                                                "query": "SELECT city, COUNT(*) c FROM customers GROUP BY city"}}))
            else:
                out.append(json.dumps({"thought": "done", "tool": "finish",
                                       "args": {"answer": "Beijing has 2 customers, Shanghai 1"}}))
        return out
    def start_serving(self): pass
    def cleanup(self): pass


class _JudgeLLM(LLMServingABC):
    def generate_from_input(self, user_inputs, system_prompt=""):
        return [json.dumps({"goal_achievement": 5, "efficiency": 4, "coherence": 5,
                            "tool_use": 5, "overall": 0.9, "rationale": "correct SQL, clean path"})
                for _ in user_inputs]
    def start_serving(self): pass
    def cleanup(self): pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:18890")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "q.jsonl")
    pd.DataFrame([
        {"query": "How many customers are in each city?"},
        {"query": "List total paid order amount per customer."},
    ]).to_json(src, orient="records", lines=True, force_ascii=False)
    storage = FileStorage(first_entry_file_name=src, cache_path=os.path.join(tmp, "cache"),
                          cache_type="jsonl")

    sandbox = AgentFlowSandboxClient(base_url=args.base_url, domain="text2sql", stateful=False)
    print(f"[e2e] health_check -> {sandbox.health_check()}")
    print(f"[e2e] list_tools  -> {[t.name for t in sandbox.list_tools('text2sql')]}")

    # 1) GENERATE
    gen = AgentExploreGenerator(llm_serving=_SQLAgentLLM(), sandbox=sandbox,
                                domain="text2sql", max_steps=6, max_workers=2)
    gen.run(storage.step(), input_key="query", output_key="trajectory")

    # 2) EVALUATE (LLM-as-judge)
    judge = TrajectoryQualityEvaluator(llm_serving=_JudgeLLM(), max_workers=2)
    judge.run(storage.step(), input_key="trajectory", output_key="traj_overall")

    # 3) FILTER (deterministic)
    TrajectoryFilter(require_success=True).run(storage.step(), input_key="trajectory")

    df = storage.step().read(output_type="dataframe")
    print(f"\n[e2e] {len(df)} trajectories survived filter")
    for _, row in df.iterrows():
        traj = row["trajectory"]
        if isinstance(traj, str): traj = json.loads(traj)
        print(f"\n  task: {traj['task']}")
        print(f"  success={traj['success']} steps={traj['num_steps']} "
              f"overall_score={row.get('traj_overall')}")
        for st in traj["steps"]:
            obs = json.dumps(st["observation"], ensure_ascii=False)
            print(f"    {st['action']['tool']}: {obs[:90]}")
    print("\n[e2e] PASS" if len(df) >= 1 else "\n[e2e] FAIL: no trajectories")


if __name__ == "__main__":
    main()
