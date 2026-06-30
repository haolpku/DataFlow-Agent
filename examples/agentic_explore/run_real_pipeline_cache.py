"""
真实 API 流水线:用 open-dataflow 的 APILLMServing_request(真实 Claude 模型)
驱动 agentic_explore 的四个算子,把所有中间数据落盘到本仓库的 cache/ 目录。

运行:
    conda run -n dataflow-agent \
      DF_API_KEY=sk-... \
      python examples/agentic_explore/run_real_pipeline_cache.py
"""
import json
import os

import pandas as pd

from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage

import dataflow_agent  # 注册算子
from dataflow_agent.sandbox import MockSandboxClient
from dataflow_agent.generate.agent_explore_generator import AgentExploreGenerator
from dataflow_agent.generate.agent_explore_tree_generator import AgentExploreTreeGenerator
from dataflow_agent.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
from dataflow_agent.filter.trajectory_filter import TrajectoryFilter


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(REPO, "cache")
API_URL = os.environ.get("API_URL", "http://your-gateway:port/v1/chat/completions")
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")


def banner(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def make_llm():
    return APILLMServing_request(
        api_url=API_URL,
        key_name_of_api_key="DF_API_KEY",
        model_name=MODEL,
        max_workers=4,
        max_retries=4,
    )


def main():
    os.makedirs(CACHE, exist_ok=True)

    # 知识库:让 mock sandbox 能回答这批查询
    knowledge = {
        "capital of france": "Paris is the capital of France.",
        "tallest mountain": "Mount Everest is the tallest mountain at 8849m.",
        "largest ocean": "The Pacific Ocean is the largest ocean on Earth.",
        "speed of light": "The speed of light in vacuum is 299792458 m/s.",
        "author of hamlet": "William Shakespeare wrote Hamlet.",
    }
    queries = [
        {"query": "what is the capital of france"},
        {"query": "what is the tallest mountain"},
        {"query": "what is the largest ocean"},
        {"query": "what is the speed of light"},
        {"query": "who is the author of hamlet"},
    ]

    src = os.path.join(CACHE, "queries.jsonl")
    pd.DataFrame(queries).to_json(src, orient="records", lines=True, force_ascii=False)

    # cache_type=jsonl -> 每个算子的中间结果落盘到 CACHE/dataflow_cache_step_stepN.jsonl
    # 安装版 FileStorage 的 __init__ 未暴露 flush_all_steps,直接设属性让每一步都落盘
    storage = FileStorage(first_entry_file_name=src, cache_path=CACHE, cache_type="jsonl")
    storage._flush_all_steps = True

    llm = make_llm()

    banner(f"链路: Generator -> Evaluator -> Filter  (真实模型 {MODEL})")
    AgentExploreGenerator(
        llm_serving=llm, sandbox=MockSandboxClient(knowledge=knowledge),
        domain="mock", max_steps=5, max_workers=4,
    ).run(storage.step(), input_key="query", output_key="trajectory")

    TrajectoryQualityEvaluator(
        llm_serving=llm, max_workers=4,
    ).run(storage.step(), input_key="trajectory", output_key="traj_overall")

    n_before = len(storage.read(output_type="dataframe"))
    TrajectoryFilter(require_success=True).run(storage.step(), input_key="trajectory")

    df = storage.step().read(output_type="dataframe")

    # 树生成器:单独一条链,落到 CACHE/tree
    banner("树生成器: AgentExploreTreeGenerator (真实模型)")
    src2 = os.path.join(CACHE, "queries_tree.jsonl")
    pd.DataFrame(queries[:2]).to_json(src2, orient="records", lines=True, force_ascii=False)
    storage_tree = FileStorage(
        first_entry_file_name=src2,
        cache_path=os.path.join(CACHE, "tree"),
        cache_type="jsonl",
    )
    storage_tree._flush_all_steps = True
    AgentExploreTreeGenerator(
        llm_serving=llm, sandbox=MockSandboxClient(knowledge=knowledge),
        domain="mock", branching_factor=2, max_children=2,
        max_depth=3, max_nodes=12, max_workers=2,
    ).run(storage_tree.step(), input_key="query", output_key="tree")

    # 该安装版 FileStorage 在 write() 时即落盘,无需显式 flush

    # ---- 控制台摘要 ----
    banner("结果摘要")
    print(f"  生成轨迹: {n_before} 条")
    print(f"  过滤后存活(require_success=True): {len(df)} 条")
    for _, row in df.iterrows():
        traj = row["trajectory"]
        if isinstance(traj, str):
            traj = json.loads(traj)
        print(f"\n  任务: {traj['task']}")
        print(f"    success={traj['success']} steps={traj['num_steps']} "
              f"overall={row.get('traj_overall')} "
              f"goal={row.get('traj_goal_achievement')} "
              f"eff={row.get('traj_efficiency')} "
              f"coh={row.get('traj_coherence')} "
              f"tool={row.get('traj_tool_use')}")
        print(f"    final_answer: {str(traj['final_answer'])[:90]!r}")
        rationale = row.get("traj_rationale")
        if rationale:
            print(f"    rationale: {str(rationale)[:120]}")

    banner("cache 目录内容")
    for root, _, files in os.walk(CACHE):
        for f in sorted(files):
            p = os.path.join(root, f)
            print(f"  {os.path.relpath(p, REPO)}  ({os.path.getsize(p)} bytes)")


if __name__ == "__main__":
    main()
