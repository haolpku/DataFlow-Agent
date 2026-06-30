"""
Coding-agent demo: drive a real LLM through CodingSandboxClient to fix a bug.

The sandbox is a REAL isolated workspace on disk with file + python + pytest +
shell tools. The agent reads the buggy code, edits it, runs the tests, and
finishes once they pass. Observations are all text, so this uses the standard
agent-explore loop with no framework changes.

Run:
    conda run -n dataflow-agent \
      DF_API_KEY=sk-... \
      python examples/agentic_explore/run_coding_agent.py
"""
import json
import os

import pandas as pd

import dataflow_agent  # noqa: F401  (registers operators)
from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow_agent.sandbox import CodingSandboxClient
from dataflow_agent.generate.agent_explore_generator import AgentExploreGenerator
from dataflow_agent.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
from dataflow_agent.filter.trajectory_filter import TrajectoryFilter


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(REPO, "cache", "coding")
API_URL = os.environ.get("API_URL", "http://your-gateway:port/v1/chat/completions")
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")


# A small, self-contained bug-fix task: the buggy module + a failing test suite.
SEED_FILES = {
    "stringutils.py": (
        "def is_palindrome(s):\n"
        "    # BUG: does not ignore case or spaces\n"
        "    return s == s[::-1]\n"
    ),
    "test_stringutils.py": (
        "from stringutils import is_palindrome\n\n"
        "def test_basic():\n"
        "    assert is_palindrome('racecar')\n\n"
        "def test_case_and_space():\n"
        "    assert is_palindrome('A man a plan a canal Panama')\n"
    ),
}
TASK = (
    "Fix is_palindrome in stringutils.py so it ignores case and spaces, then "
    "make every test in the workspace pass. Use the tools to read the files, "
    "edit the code, and run the tests to verify."
)


def main():
    os.makedirs(CACHE, exist_ok=True)
    src = os.path.join(CACHE, "tasks.jsonl")
    pd.DataFrame([{"query": TASK}]).to_json(src, orient="records", lines=True, force_ascii=False)
    storage = FileStorage(first_entry_file_name=src, cache_path=CACHE, cache_type="jsonl")

    llm = APILLMServing_request(
        api_url=API_URL, key_name_of_api_key="DF_API_KEY",
        model_name=MODEL, max_workers=2,
    )
    sandbox = CodingSandboxClient(seed_files=SEED_FILES, allow_shell=True, timeout=60)

    # GENERATE: the agent works in a real workspace
    AgentExploreGenerator(
        llm_serving=llm, sandbox=sandbox, domain="coding",
        max_steps=12, max_workers=1,
    ).run(storage.step(), input_key="query", output_key="trajectory")

    # EVALUATE + FILTER (optional, shown for completeness)
    TrajectoryQualityEvaluator(llm_serving=llm, max_workers=1).run(
        storage.step(), input_key="trajectory", output_key="traj_overall")
    TrajectoryFilter(require_success=True).run(storage.step(), input_key="trajectory")

    df = storage.step().read(output_type="dataframe")
    print("\n" + "=" * 70)
    for _, row in df.iterrows():
        traj = row["trajectory"]
        if isinstance(traj, str):
            traj = json.loads(traj)
        print(f"task: {traj['task'][:60]}...")
        print(f"success={traj['success']} steps={traj['num_steps']} "
              f"overall={row.get('traj_overall')}")
        print(f"answer: {traj['final_answer']}")
        print("tool sequence:")
        for i, st in enumerate(traj["steps"], 1):
            obs = st.get("observation")
            ec = obs.get("exit_code") if isinstance(obs, dict) else None
            extra = f"  (exit_code={ec})" if ec is not None else ""
            print(f"  {i}. {st['action']['tool']}{extra}")
    print("=" * 70)


if __name__ == "__main__":
    main()
