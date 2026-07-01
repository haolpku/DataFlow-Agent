"""
Minimal runnable pipeline for the agent-explore operator.

This uses the offline MockSandboxClient + a tiny scripted LLM so you can run it
with zero external dependencies:

    python examples/agentic_explore/run_mock_pipeline.py

To point at a REAL sandbox instead, swap the two marked lines:

    from dataflow.serving import APILLMServing_request
    from dataflow_agent.sandbox import HTTPSandboxClient

    llm = APILLMServing_request(api_url="https://.../v1/chat/completions",
                                model_name="gpt-4o")
    sandbox = HTTPSandboxClient(base_url="http://127.0.0.1:18890",
                                     domain="web")     # web / rag / vm / sql ...

Everything else stays the same -- the operator only depends on the
SandboxClientABC / LLMServingABC abstractions, not on any concrete backend.
"""

import json
import os
import tempfile

import pandas as pd

from dataflow.core import LLMServingABC
from dataflow.utils.storage import FileStorage
from dataflow_agent.sandbox import MockSandboxClient
from dataflow_agent.generate.agent_explore_generator import (
    AgentExploreGenerator,
)


class _ScriptedLLM(LLMServingABC):
    """Stand-in LLM: search once, then finish. Replace with a real serving."""

    def generate_from_input(self, user_inputs, system_prompt=""):
        out = []
        for ui in user_inputs:
            if "observation:" in ui:  # we've already searched -> finish
                out.append(json.dumps({
                    "thought": "I have the answer now.",
                    "tool": "finish",
                    "args": {"answer": "Paris"},
                }))
            else:  # first turn -> search
                out.append(json.dumps({
                    "thought": "Let me look this up.",
                    "tool": "search",
                    "args": {"query": "capital of france"},
                }))
        return out

    def start_serving(self):
        pass

    def cleanup(self):
        pass


def main():
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "queries.jsonl")
    pd.DataFrame([
        {"query": "what is the capital of france"},
        {"query": "what is the tallest mountain"},
    ]).to_json(src, orient="records", lines=True, force_ascii=False)

    storage = FileStorage(
        first_entry_file_name=src,
        cache_path=os.path.join(tmp, "cache"),
        cache_type="jsonl",
    )

    op = AgentExploreGenerator(
        llm_serving=_ScriptedLLM(),       # <-- swap for APILLMServing_request(...)
        sandbox=MockSandboxClient(),      # <-- swap for HTTPSandboxClient(...)
        domain="mock",
        max_steps=5,
        max_workers=2,
    )
    op.run(storage.step(), input_key="query", output_key="trajectory")

    df = storage.step().read(output_type="dataframe")
    for _, row in df.iterrows():
        traj = row["trajectory"]
        if isinstance(traj, str):
            traj = json.loads(traj)
        print(f"\n=== task: {traj['task']} ===")
        print(f"success={traj['success']} steps={traj['num_steps']} "
              f"answer={traj['final_answer']!r}")
        for i, step in enumerate(traj["steps"], 1):
            print(f"  step {i}: tool={step['action']['tool']} "
                  f"obs={json.dumps(step['observation'], ensure_ascii=False)[:80]}")


if __name__ == "__main__":
    main()
