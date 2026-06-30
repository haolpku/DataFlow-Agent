"""
验证 TrajectoryFilter 真的会按规则丢弃数据。

做法:把"真实生成的好轨迹"(来自 cache/dataflow_cache_step_step1.jsonl)与
一批"故意构造的坏轨迹"(每条命中一条过滤规则)混在一起,跑 TrajectoryFilter,
观察它丢弃哪些、保留哪些、报告的 drop reason。

输入/输出都落到 cache/filter_demo/。
"""
import json
import os

import pandas as pd

import dataflow_agent  # 注册算子
from dataflow.utils.storage import FileStorage
from dataflow_agent.filter.trajectory_filter import TrajectoryFilter


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(REPO, "cache")
DEMO = os.path.join(CACHE, "filter_demo")


def good_traj(task, answer):
    return {
        "task": task,
        "steps": [
            {"thought": "search", "action": {"tool": "search", "args": {"query": task}},
             "observation": {"results": [answer]}, "ok": True, "error": None},
            {"thought": "done", "action": {"tool": "finish", "args": {"answer": answer}},
             "observation": {"answer": answer}},
        ],
        "final_answer": answer, "num_steps": 2, "success": True,
    }


def main():
    os.makedirs(DEMO, exist_ok=True)

    rows = []

    # --- 真实生成的好轨迹(从主流水线 cache 读 2 条) ---
    real_path = os.path.join(CACHE, "dataflow_cache_step_step1.jsonl")
    n_real = 0
    if os.path.exists(real_path):
        real_df = pd.read_json(real_path, lines=True)
        for _, r in real_df.head(2).iterrows():
            t = r["trajectory"]
            if isinstance(t, str):
                t = json.loads(t)
            rows.append({"query": t["task"], "trajectory": t, "_label": "REAL_GOOD"})
            n_real += 1

    # --- 故意构造的坏轨迹,每条命中一条规则 ---
    bad_cases = [
        # 1) not_success: 没调用 finish,success=False
        {"query": "未完成的任务", "_label": "BAD/not_success", "trajectory": {
            "task": "未完成的任务",
            "steps": [{"thought": "搜一下", "action": {"tool": "search", "args": {"query": "x"}},
                       "observation": {"results": ["..."]}, "ok": True, "error": None}],
            "final_answer": None, "num_steps": 1, "success": False}},

        # 2) empty_answer: finish 了但答案为空
        {"query": "空答案任务", "_label": "BAD/empty_answer", "trajectory": {
            "task": "空答案任务",
            "steps": [{"thought": "done", "action": {"tool": "finish", "args": {"answer": "   "}},
                       "observation": {"answer": "   "}}],
            "final_answer": "   ", "num_steps": 1, "success": True}},

        # 3) parse_error_step: 含一步解析失败
        {"query": "解析失败任务", "_label": "BAD/parse_error", "trajectory": {
            "task": "解析失败任务",
            "steps": [
                {"thought": None, "action": {"tool": None, "args": {}}, "observation": None,
                 "parse_error": True, "raw_response": "这不是合法JSON"},
                {"thought": "done", "action": {"tool": "finish", "args": {"answer": "勉强答了"}},
                 "observation": {"answer": "勉强答了"}}],
            "final_answer": "勉强答了", "num_steps": 2, "success": True}},

        # 4) invalid_tool_step: 幻觉了不存在的工具名
        {"query": "幻觉工具任务", "_label": "BAD/invalid_tool", "trajectory": {
            "task": "幻觉工具任务",
            "steps": [
                {"thought": "用个不存在的工具", "action": {"tool": "magic_oracle", "args": {}},
                 "observation": None, "invalid_tool": True},
                {"thought": "done", "action": {"tool": "finish", "args": {"answer": "答了"}},
                 "observation": {"answer": "答了"}}],
            "final_answer": "答了", "num_steps": 2, "success": True}},

        # 5) repeated_action: 同一动作重复 4 次(死循环)
        {"query": "死循环任务", "_label": "BAD/repeated_action", "trajectory": {
            "task": "死循环任务",
            "steps": [
                {"thought": "搜", "action": {"tool": "search", "args": {"query": "同样的查询"}},
                 "observation": {"results": ["a"]}, "ok": True, "error": None},
                {"thought": "再搜", "action": {"tool": "search", "args": {"query": "同样的查询"}},
                 "observation": {"results": ["a"]}, "ok": True, "error": None},
                {"thought": "又搜", "action": {"tool": "search", "args": {"query": "同样的查询"}},
                 "observation": {"results": ["a"]}, "ok": True, "error": None},
                {"thought": "还搜", "action": {"tool": "search", "args": {"query": "同样的查询"}},
                 "observation": {"results": ["a"]}, "ok": True, "error": None},
                {"thought": "done", "action": {"tool": "finish", "args": {"answer": "终于"}},
                 "observation": {"answer": "终于"}}],
            "final_answer": "终于", "num_steps": 5, "success": True}},

        # 6) tool_error_step: 含工具执行失败(需 drop_tool_errors=True 才丢)
        {"query": "工具报错任务", "_label": "BAD/tool_error", "trajectory": {
            "task": "工具报错任务",
            "steps": [
                {"thought": "搜", "action": {"tool": "search", "args": {"query": "x"}},
                 "observation": "ERROR", "ok": False, "error": "boom"},
                {"thought": "done", "action": {"tool": "finish", "args": {"answer": "答了"}},
                 "observation": {"answer": "答了"}}],
            "final_answer": "答了", "num_steps": 2, "success": True}},
    ]
    rows.extend(bad_cases)

    # 落盘输入
    src = os.path.join(DEMO, "mixed_input.jsonl")
    pd.DataFrame(rows).to_json(src, orient="records", lines=True, force_ascii=False)

    storage = FileStorage(first_entry_file_name=src, cache_path=DEMO, cache_type="jsonl")

    print(f"\n输入: {len(rows)} 条轨迹 ({n_real} 条真实好的 + {len(bad_cases)} 条故意坏的)")
    print("=" * 72)

    # 全规则打开(含 max_repeated_actions / drop_tool_errors),逐条标注命中原因
    flt = TrajectoryFilter(
        require_success=True,
        require_nonempty_answer=True,
        drop_parse_errors=True,
        drop_invalid_tools=True,
        drop_tool_errors=True,        # 默认 False,这里打开以演示
        max_repeated_actions=3,       # 默认 None,这里设 3 以演示死循环防护
    )

    # 借用内部判定函数,先打印每条的预期判定(label vs reason)
    df_in = pd.read_json(src, lines=True)
    print(f"{'label':<22}{'filter 判定':<28}{'结果'}")
    print("-" * 72)
    for _, r in df_in.iterrows():
        t = r["trajectory"]
        if isinstance(t, str):
            t = json.loads(t)
        reason = flt._reject_reason(t)
        verdict = "✅ 保留" if reason is None else f"❌ 丢弃"
        print(f"{r['_label']:<22}{str(reason):<28}{verdict}")

    print("=" * 72)
    flt.run(storage.step(), input_key="trajectory")

    kept = storage.step().read("dataframe")
    print(f"\n过滤后存活: {len(kept)}/{len(rows)} 条")
    print("存活的 label:", kept["_label"].tolist())
    print(f"\n输出文件: {os.path.relpath(os.path.join(DEMO, 'dataflow_cache_step_step1.jsonl'), REPO)}")


if __name__ == "__main__":
    main()
