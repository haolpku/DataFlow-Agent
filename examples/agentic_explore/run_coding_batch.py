"""
Run a batch of diverse coding-agent tasks against a real LLM, producing real
trajectories (workspace file ops + pytest), and write them to
DataFlow-Agent/cache/coding_batch/ so the showcase site can pick them up.

Each task seeds a workspace, the agent reads/edits/tests, and we run the full
Generator -> Evaluator pipeline so every trajectory carries quality scores.

Run:
    conda run -n dataflow-agent \
      DF_API_KEY=sk-... API_URL=http://host:port/v1/chat/completions \
      MODEL=claude-haiku-4-5-20251001 \
      python examples/agentic_explore/run_coding_batch.py
"""
import json
import os

import pandas as pd

import dataflow_agent  # noqa: F401
from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow_agent.sandbox import CodingSandboxClient
from dataflow_agent.generate.agent_explore_generator import AgentExploreGenerator
from dataflow_agent.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(REPO, "cache", "coding_batch")
API_URL = os.environ.get("API_URL", "http://your-gateway:port/v1/chat/completions")
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")


# Each task: a prompt + the workspace it starts from. Diverse coding skills:
# bug-fix, implement-from-spec, write-tests, refactor, debug-runtime-error.
TASKS = [
    {
        "query": "Fix the bug in fizzbuzz.py so all tests pass. Read the code and "
                 "tests, edit the file, and run the tests to verify.",
        "seed": {
            "fizzbuzz.py": (
                "def fizzbuzz(n):\n"
                "    # BUG: order of checks is wrong and 15 case missing\n"
                "    if n % 3 == 0:\n"
                "        return 'Fizz'\n"
                "    if n % 5 == 0:\n"
                "        return 'Buzz'\n"
                "    return str(n)\n"
            ),
            "test_fizzbuzz.py": (
                "from fizzbuzz import fizzbuzz\n\n"
                "def test_fizzbuzz():\n"
                "    assert fizzbuzz(3) == 'Fizz'\n"
                "    assert fizzbuzz(5) == 'Buzz'\n"
                "    assert fizzbuzz(15) == 'FizzBuzz'\n"
                "    assert fizzbuzz(7) == '7'\n"
            ),
        },
    },
    {
        "query": "Implement the function `flatten` in flatten.py so the tests pass. "
                 "It should flatten an arbitrarily nested list of ints into a flat "
                 "list. Read the test file first, then implement and verify.",
        "seed": {
            "flatten.py": "def flatten(xs):\n    # TODO: implement\n    pass\n",
            "test_flatten.py": (
                "from flatten import flatten\n\n"
                "def test_flatten():\n"
                "    assert flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5]\n"
                "    assert flatten([]) == []\n"
                "    assert flatten([[], [1]]) == [1]\n"
            ),
        },
    },
    {
        "query": "There is a runtime error in stats.py: running the tests raises an "
                 "exception. Find and fix it so the tests pass. Use run_tests to see "
                 "the traceback, then fix the code.",
        "seed": {
            "stats.py": (
                "def mean(xs):\n"
                "    # BUG: ZeroDivisionError on empty input not handled\n"
                "    return sum(xs) / len(xs)\n"
            ),
            "test_stats.py": (
                "from stats import mean\n\n"
                "def test_mean_basic():\n"
                "    assert mean([2, 4, 6]) == 4\n\n"
                "def test_mean_empty():\n"
                "    assert mean([]) == 0\n"
            ),
        },
    },
    {
        "query": "Write a pytest test file test_palindrome.py for the function in "
                 "palindrome.py (it checks if a string is a palindrome, ignoring "
                 "case and spaces). Add at least 3 assertions, then run the tests to "
                 "make sure they pass.",
        "seed": {
            "palindrome.py": (
                "def is_palindrome(s):\n"
                "    s = ''.join(c.lower() for c in s if not c.isspace())\n"
                "    return s == s[::-1]\n"
            ),
        },
    },
    {
        "query": "Refactor slow_dedup.py: the function removes duplicates from a list "
                 "but is O(n^2). Make it O(n) while preserving order, keeping the "
                 "existing tests green. Read the tests, refactor, and verify.",
        "seed": {
            "slow_dedup.py": (
                "def dedup(xs):\n"
                "    out = []\n"
                "    for x in xs:\n"
                "        if x not in out:  # O(n) membership -> O(n^2) total\n"
                "            out.append(x)\n"
                "    return out\n"
            ),
            "test_slow_dedup.py": (
                "from slow_dedup import dedup\n\n"
                "def test_dedup():\n"
                "    assert dedup([1, 1, 2, 3, 3, 3, 2]) == [1, 2, 3]\n"
                "    assert dedup([]) == []\n"
            ),
        },
    },
    {
        "query": "Implement `roman_to_int` in roman.py to convert a Roman numeral "
                 "string to an integer, so the tests pass. Read the tests, implement, "
                 "and verify with run_tests.",
        "seed": {
            "roman.py": "def roman_to_int(s):\n    # TODO\n    pass\n",
            "test_roman.py": (
                "from roman import roman_to_int\n\n"
                "def test_roman():\n"
                "    assert roman_to_int('III') == 3\n"
                "    assert roman_to_int('IV') == 4\n"
                "    assert roman_to_int('IX') == 9\n"
                "    assert roman_to_int('LVIII') == 58\n"
                "    assert roman_to_int('MCMXCIV') == 1994\n"
            ),
        },
    },
]


def main():
    os.makedirs(CACHE, exist_ok=True)
    llm = APILLMServing_request(
        api_url=API_URL, key_name_of_api_key="DF_API_KEY",
        model_name=MODEL, max_workers=4,
    )

    all_traj_rows = []
    for i, t in enumerate(TASKS):
        sub = os.path.join(CACHE, f"task_{i}")
        os.makedirs(sub, exist_ok=True)
        src = os.path.join(sub, "task.jsonl")
        pd.DataFrame([{"query": t["query"]}]).to_json(
            src, orient="records", lines=True, force_ascii=False)
        storage = FileStorage(first_entry_file_name=src, cache_path=sub, cache_type="jsonl")

        # fresh sandbox per task (its own seeded workspace)
        sandbox = CodingSandboxClient(seed_files=t["seed"], allow_shell=True, timeout=60)
        AgentExploreGenerator(
            llm_serving=llm, sandbox=sandbox, domain="coding",
            max_steps=12, max_workers=1,
        ).run(storage.step(), input_key="query", output_key="trajectory")
        TrajectoryQualityEvaluator(llm_serving=llm, max_workers=1).run(
            storage.step(), input_key="trajectory", output_key="traj_overall")

        df = storage.step().read(output_type="dataframe")
        row = df.iloc[0].to_dict()
        all_traj_rows.append(row)
        traj = row["trajectory"]
        if isinstance(traj, str):
            traj = json.loads(traj)
        tools = ">".join(s["action"]["tool"] for s in traj["steps"])
        print(f"[task {i}] success={traj['success']} steps={traj['num_steps']} "
              f"overall={row.get('traj_overall')}  {tools}")

    # write a combined jsonl the showcase builder can read
    combined = os.path.join(CACHE, "coding_trajectories.jsonl")
    pd.DataFrame(all_traj_rows).to_json(combined, orient="records", lines=True, force_ascii=False)
    print(f"\nwrote {len(all_traj_rows)} coding trajectories -> {combined}")


if __name__ == "__main__":
    main()
