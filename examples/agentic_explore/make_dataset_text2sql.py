"""
Produce a small agentic dataset against a REAL text2sql sandbox and dump
DataFlow's per-step cache files so each operator's output is visible.

Pipeline:  Generator -> Evaluator (LLM-judge) -> Filter
Each operator writes dataflow_cache_step_step{N}.jsonl ; we print every one.
"""
import argparse
import glob
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # DataFlow root

from dataflow.core import LLMServingABC
from dataflow.utils.storage import FileStorage
from dataflow_agent.sandbox import HTTPSandboxClient
from dataflow_agent.generate.agent_explore_generator import AgentExploreGenerator
from dataflow_agent.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
from dataflow_agent.filter.trajectory_filter import TrajectoryFilter


class _SQLAgentLLM(LLMServingABC):
    """Scripted text2sql agent: get_schema -> execute -> finish, per task."""
    _Q = {
        "How many customers are in each city?":
            "SELECT city, COUNT(*) c FROM customers GROUP BY city",
        "What is the total paid order amount per customer?":
            "SELECT c.name, SUM(o.amount) total FROM customers c JOIN orders o "
            "ON o.customer_id=c.id WHERE o.status='paid' GROUP BY c.name",
        "Which orders are still pending?":
            "SELECT id, customer_id, amount FROM orders WHERE status='pending'",
    }
    def generate_from_input(self, user_inputs, system_prompt=""):
        out = []
        for ui in user_inputs:
            task = next((l[len("Task: "):] for l in ui.splitlines()
                         if l.startswith("Task: ")), "")
            n_obs = ui.count("observation:")
            if n_obs == 0:
                out.append(json.dumps({"thought": "inspect the schema first",
                                       "tool": "get_schema", "args": {"db_id": "retail"}}))
            elif n_obs == 1:
                out.append(json.dumps({"thought": "write and run the SQL",
                                       "tool": "execute",
                                       "args": {"db_id": "retail",
                                                "query": self._Q.get(task, "SELECT 1")}}))
            else:
                out.append(json.dumps({"thought": "I have the result",
                                       "tool": "finish",
                                       "args": {"answer": "see query result above"}}))
        return out
    def start_serving(self): pass
    def cleanup(self): pass


class _JudgeLLM(LLMServingABC):
    def generate_from_input(self, user_inputs, system_prompt=""):
        # score by whether the path actually ran an execute that returned rows
        res = []
        for ui in user_inputs:
            good = "rows" in ui
            res.append(json.dumps({
                "goal_achievement": 5 if good else 2, "efficiency": 4,
                "coherence": 5, "tool_use": 5 if good else 3,
                "overall": 0.9 if good else 0.4,
                "rationale": "ran correct SQL and read rows" if good
                             else "did not retrieve data"}))
        return res
    def start_serving(self): pass
    def cleanup(self): pass


def dump_steps(cache_dir, src_path):
    print("\n" + "=" * 78)
    print("PER-STEP DATA (DataFlow cache files)")
    print("=" * 78)
    files = [("step0 (input)", src_path)] + sorted(
        ((os.path.basename(f), f) for f in
         glob.glob(os.path.join(cache_dir, "dataflow_cache_step_step*.jsonl"))),
        key=lambda x: x[0])
    for label, path in files:
        if not os.path.exists(path):
            continue
        df = pd.read_json(path, lines=True)
        print(f"\n----- {label} -----  ({path})")
        print(f"      rows={len(df)}  columns={list(df.columns)}")
        # show first row, pretty
        if len(df):
            row = df.iloc[0].to_dict()
            print(json.dumps(row, ensure_ascii=False, indent=2, default=str)[:2200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:18890")
    ap.add_argument("--out", default="/tmp/sandbox_e2e/dataset")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    src = os.path.join(args.out, "queries.jsonl")
    pd.DataFrame([
        {"query": "How many customers are in each city?"},
        {"query": "What is the total paid order amount per customer?"},
        {"query": "Which orders are still pending?"},
    ]).to_json(src, orient="records", lines=True, force_ascii=False)

    cache = os.path.join(args.out, "cache")
    storage = FileStorage(first_entry_file_name=src, cache_path=cache, cache_type="jsonl")

    sandbox = HTTPSandboxClient(base_url=args.base_url, domain="text2sql", stateful=False)
    print(f"[data] sandbox health={sandbox.health_check()} "
          f"tools={[t.name for t in sandbox.list_tools('text2sql')]}")

    AgentExploreGenerator(llm_serving=_SQLAgentLLM(), sandbox=sandbox,
                          domain="text2sql", max_steps=6, max_workers=3
                          ).run(storage.step(), input_key="query", output_key="trajectory")
    TrajectoryQualityEvaluator(llm_serving=_JudgeLLM(), max_workers=3
                               ).run(storage.step(), input_key="trajectory",
                                     output_key="traj_overall")
    TrajectoryFilter(require_success=True, require_nonempty_answer=False
                     ).run(storage.step(), input_key="trajectory")

    dump_steps(cache, src)
    print("\n[data] done. cache dir:", cache)


if __name__ == "__main__":
    main()
