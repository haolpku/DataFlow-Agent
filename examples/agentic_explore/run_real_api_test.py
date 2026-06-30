"""
Real-API smoke test for the four agentic_explore operators.

LLM  = REAL Claude model via an OpenAI-compatible gateway (requests transport).
Sandbox = offline MockSandboxClient (no sandbox server needed).

This exercises the full Generator -> Evaluator -> Filter loop, plus the tree
generator, with a genuine model in the decision/judge seats.

Run:
    PYTHONPATH=/Users/lianghao/Desktop/OpenDCAI/DataFlow \
    DF_API_KEY=sk-... API_URL=http://host:port/v1/chat/completions \
    MODEL=claude-haiku-4-5-20251001 \
    python examples/agentic_explore/run_real_api_test.py
"""
import json
import os
import re
import sys
import tempfile
import time

import requests
import pandas as pd

from dataflow.core import LLMServingABC
from dataflow.utils.storage import FileStorage

from dataflow_agent.sandbox import MockSandboxClient
from dataflow_agent.generate.agent_explore_generator import AgentExploreGenerator
from dataflow_agent.generate.agent_explore_tree_generator import AgentExploreTreeGenerator
from dataflow_agent.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
from dataflow_agent.filter.trajectory_filter import TrajectoryFilter


API_URL = os.environ["API_URL"]
API_KEY = os.environ["DF_API_KEY"]
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")


class RealLLMServing(LLMServingABC):
    """Minimal real-LLM serving: hits an OpenAI-compatible /chat/completions.

    Mirrors APILLMServing_request's request shape but with no torch-importing
    package __init__ in the path. Sequential (small batches), with retries.
    """

    def __init__(self, api_url, api_key, model, temperature=0.0, max_retries=4):
        self.api_url = api_url
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.n_calls = 0

    def _chat(self, system_prompt, user_input):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
            "temperature": self.temperature,
            "max_tokens": 1024,
        }
        for i in range(self.max_retries):
            try:
                r = requests.post(self.api_url, headers=self.headers,
                                  data=json.dumps(payload), timeout=(10, 120))
                if r.status_code == 200:
                    data = r.json()
                    msg = data.get("choices", [{}])[0].get("message", {})
                    return msg.get("content", "") or ""
                print(f"    [llm] status={r.status_code} body={r.text[:200]}")
            except Exception as exc:  # noqa: BLE001
                print(f"    [llm] error: {exc}")
            time.sleep(2 ** i)
        return ""

    def generate_from_input(self, user_inputs, system_prompt="You are a helpful assistant"):
        out = []
        for ui in user_inputs:
            self.n_calls += 1
            out.append(self._chat(system_prompt, ui))
        return out

    def start_serving(self):
        pass

    def cleanup(self):
        pass


def banner(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def main():
    banner("0. CONNECTIVITY CHECK")
    llm = RealLLMServing(API_URL, API_KEY, MODEL)
    ping = llm.generate_from_input(["Reply with exactly: PONG"], "You are terse.")
    print(f"  model={MODEL}  ping -> {ping[0]!r}")
    if not ping[0]:
        print("  [FAIL] no response from API; aborting.")
        sys.exit(1)

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "queries.jsonl")
    pd.DataFrame([
        {"query": "what is the capital of france"},
        {"query": "what is the tallest mountain"},
    ]).to_json(src, orient="records", lines=True, force_ascii=False)
    storage = FileStorage(first_entry_file_name=src,
                          cache_path=os.path.join(tmp, "cache"),
                          cache_type="jsonl")
    sandbox = MockSandboxClient()

    banner("1-3. GENERATOR -> EVALUATOR -> FILTER  (chained, one step() each)")
    gen = AgentExploreGenerator(llm_serving=llm, sandbox=sandbox,
                                domain="mock", max_steps=5, max_workers=2)
    gen.run(storage.step(), input_key="query", output_key="trajectory")

    judge = TrajectoryQualityEvaluator(llm_serving=llm, max_workers=2)
    judge.run(storage.step(), input_key="trajectory", output_key="traj_overall")

    # peek the post-judge buffer (before filter) for the row count
    before = len(storage.read(output_type="dataframe"))

    TrajectoryFilter(require_success=True).run(storage.step(), input_key="trajectory")

    df = storage.step().read(output_type="dataframe")

    print("\n  --- [1] generator trajectories + [2] judge scores ---")
    for _, row in df.iterrows():
        traj = row["trajectory"]
        if isinstance(traj, str):
            traj = json.loads(traj)
        print(f"\n  task: {traj['task']}")
        print(f"  success={traj['success']} steps={traj['num_steps']} "
              f"overall_score={row.get('traj_overall')} answer={traj['final_answer']!r}")
        for i, st in enumerate(traj["steps"], 1):
            obs = json.dumps(st["observation"], ensure_ascii=False)
            print(f"    step{i} tool={st['action']['tool']} obs={obs[:80]}")
    print(f"\n  --- [3] filter: {before} -> {len(df)} survived (require_success=True) ---")

    banner("4. AgentExploreTreeGenerator (branching tree, real LLM)")
    src2 = os.path.join(tmp, "q2.jsonl")
    pd.DataFrame([{"query": "what is the capital of france"}]).to_json(
        src2, orient="records", lines=True, force_ascii=False)
    storage2 = FileStorage(first_entry_file_name=src2,
                           cache_path=os.path.join(tmp, "cache2"),
                           cache_type="jsonl")
    tree = AgentExploreTreeGenerator(llm_serving=llm, sandbox=MockSandboxClient(),
                                     domain="mock", branching_factor=2,
                                     max_children=2, max_depth=3, max_nodes=10,
                                     max_workers=1)
    out_keys = tree.run(storage2.step(), input_key="query", output_key="tree")
    dft = storage2.step().read(output_type="dataframe")
    print(f"  output columns added: {out_keys}")
    row0 = dft.iloc[0]
    for col in dft.columns:
        val = row0[col]
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        print(f"    {col}: {str(val)[:120]}")

    banner(f"DONE  (total real LLM calls: {llm.n_calls})")


if __name__ == "__main__":
    main()
