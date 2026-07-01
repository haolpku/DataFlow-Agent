"""
AgentExploreTreeGenerator -- branching trajectory-tree explorer.

Where :class:`AgentExploreGenerator` produces ONE linear trajectory per task,
this operator grows a TREE: at each node it samples multiple candidate actions
from the LLM, de-duplicates them, executes each distinct action in the sandbox,
and expands the resulting children -- breadth-bounded and depth-bounded. The
output is a richer dataset (many root-to-leaf paths per task, including the
branch points), which is what trajectory-tree / best-of-N agent-data synthesis
needs.

It depends only on :class:`SandboxClientABC` + :class:`LLMServingABC` and is, by
the same reasoning as the linear generator, a **text / structured-domain**
explorer (web / rag / sql / doc / ds).

Search shape (per task):
    root = task
    expand(node, depth):
        if depth == max_depth or node is terminal: return
        sample `branching_factor` candidate actions (one LLM call, n samples)
        dedup identical (tool, args)
        keep up to `max_children` distinct actions
        for each: execute in sandbox -> child node -> expand(child, depth+1)

Output per row: {task, tree, paths, num_nodes, num_paths, num_success_paths}
where `paths` is the list of root-to-leaf linear trajectories (each in the same
shape the linear generator / filter / evaluator already understand, so the rest
of the pipeline composes unchanged).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from dataflow_agent.sandbox import (
    SandboxClientABC, ToolResult, ToolSchema,
)
# Reuse the battle-tested helpers from the linear generator.
from dataflow_agent.generate.agent_explore_generator import (
    AgentExploreGenerator, _DEFAULT_SYSTEM_PROMPT, _FINISH_TOOL,
)


@OPERATOR_REGISTRY.register()
class AgentExploreTreeGenerator(OperatorABC):
    """Branching trajectory-tree explorer over a pluggable sandbox.

    Args:
        llm_serving: LLM used to propose candidate actions.
        sandbox: Any :class:`SandboxClientABC` backend.
        domain: Sandbox domain (web / rag / sql / doc / ds).
        max_depth: Maximum tree depth (steps along any path).
        branching_factor: Candidate actions sampled per node.
        max_children: Max distinct (deduped) children kept per node.
        max_nodes: Hard cap on total executed nodes per task (cost guard).
        max_workers: Concurrent tasks (each task's tree is built sequentially;
            trees across tasks run in parallel).
        max_observation_chars: Per-observation truncation fed back to the LLM.
        validate_tool_names: Drop hallucinated tool names (recorded, not run).
        dedup_actions: De-duplicate identical (tool, args) children at a node.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        sandbox: SandboxClientABC = None,
        domain: str = "web",
        max_depth: int = 4,
        branching_factor: int = 3,
        max_children: int = 3,
        max_nodes: int = 40,
        max_workers: int = 4,
        max_observation_chars: int = 8000,
        validate_tool_names: bool = True,
        dedup_actions: bool = True,
        depth_threshold: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.sandbox = sandbox
        self.domain = domain
        self.max_depth = max_depth
        self.branching_factor = branching_factor
        self.max_children = max_children
        self.max_nodes = max_nodes
        self.max_workers = max_workers
        self.max_observation_chars = max_observation_chars
        self.validate_tool_names = validate_tool_names
        self.dedup_actions = dedup_actions
        # AgentFlow-style depth threshold: at depths >= this, collapse branching
        # to a single child (deep levels explore one path only, to save cost).
        # None keeps the original uniform-branching behavior.
        self.depth_threshold = depth_threshold
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        # Borrow the linear generator's parser / truncation / catalog helpers
        # so behavior stays consistent and we don't duplicate code.
        self._lin = AgentExploreGenerator(
            llm_serving=llm_serving, sandbox=sandbox, domain=domain,
            max_observation_chars=max_observation_chars,
            validate_tool_names=validate_tool_names,
        )

    @staticmethod
    def get_desc(lang: str = "zh"):
        if lang == "zh":
            return (
                "该算子构建分支轨迹树（每步采样多个候选动作 + 去重 + 扩展），"
                "相比线性探索器产出更丰富的 agent 数据（每任务多条根到叶路径 + 分支点）。\n\n"
                "输入参数：\n"
                "- llm_serving / sandbox / domain\n"
                "- max_depth: 树最大深度\n"
                "- branching_factor: 每节点采样候选动作数\n"
                "- max_children: 每节点去重后保留的子节点上限\n"
                "- max_nodes: 单任务执行节点硬上限（成本护栏）\n"
                "- dedup_actions: 同节点相同 (tool,args) 去重\n\n"
                "运行参数：input_key（默认 \"query\"）、output_key（默认 \"tree\"）。\n"
                "输出：{task, tree, paths, num_nodes, num_paths, num_success_paths}；"
                "paths 与线性轨迹同构，可直接接 Filter / Evaluator。"
            )
        return (
            "Branching trajectory-tree explorer: samples multiple candidate "
            "actions per node, dedups, expands (depth/breadth/node bounded). "
            "Output {task, tree, paths, num_nodes, num_paths, "
            "num_success_paths}; `paths` are linear trajectories compatible "
            "with TrajectoryFilter/Evaluator. Run args: input_key (default "
            "'query'), output_key (default 'tree')."
        )

    # ------------------------------------------------------------------ #
    def _sample_actions(
        self, task: str, history: str, step_idx: int
    ) -> List[Dict[str, Any]]:
        """Ask the LLM for `branching_factor` candidate actions at this node.

        Implemented as a batch of identical prompts (the serving layer's
        sampling/temperature yields diverse completions); robust even if the
        backend is deterministic (then dedup collapses them to one child).
        """
        prompt = (
            f"{history}\n(step {step_idx + 1}/{self.max_depth}) "
            f"Propose ONE next action as JSON."
        )
        batch = [prompt] * self.branching_factor
        try:
            responses = self.llm_serving.generate_from_input(batch, self.system_prompt)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[AgentExploreTreeGenerator] sample failed: {exc}")
            return []
        actions = []
        for raw in responses:
            parsed = self._lin._extract_json(raw)
            if parsed is not None:
                actions.append(parsed)
        return actions

    @staticmethod
    def _action_key(action: Dict[str, Any]) -> Tuple[str, str]:
        tool = str(action.get("tool"))
        try:
            args = json.dumps(action.get("args", {}), sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(action.get("args"))
        return (tool, args)

    def _build_tree(
        self, task: str, known_tools: Optional[set], worker_id: Optional[str]
    ) -> Tuple[Dict[str, Any], int]:
        """Grow the tree for one task. Returns (root_node, executed_node_count)."""
        node_budget = {"n": 0}

        def expand(history: str, depth: int) -> Dict[str, Any]:
            node: Dict[str, Any] = {"depth": depth, "children": []}
            if depth >= self.max_depth or node_budget["n"] >= self.max_nodes:
                node["terminal"] = "depth_or_budget"
                return node

            candidates = self._sample_actions(task, history, depth)
            # dedup identical actions at this node
            seen: set = set()
            distinct: List[Dict[str, Any]] = []
            # AgentFlow-style: beyond depth_threshold, keep only a single child
            child_cap = self.max_children
            if self.depth_threshold is not None and depth >= self.depth_threshold:
                child_cap = 1
            for act in candidates:
                key = self._action_key(act)
                if self.dedup_actions and key in seen:
                    continue
                seen.add(key)
                distinct.append(act)
                if len(distinct) >= child_cap:
                    break

            for act in distinct:
                if node_budget["n"] >= self.max_nodes:
                    break
                tool = act.get("tool")
                args = act.get("args", {}) or {}
                thought = act.get("thought")

                if tool == _FINISH_TOOL:
                    node["children"].append({
                        "action": {"tool": tool, "args": args},
                        "thought": thought,
                        "observation": {"answer": args.get("answer")},
                        "terminal": "finish",
                        "success": True,
                        "children": [],
                    })
                    continue

                if (self.validate_tool_names and known_tools is not None
                        and tool not in known_tools):
                    node["children"].append({
                        "action": {"tool": tool, "args": args},
                        "thought": thought,
                        "invalid_tool": True,
                        "observation": None,
                        "children": [],
                    })
                    continue

                node_budget["n"] += 1
                result: ToolResult = self.sandbox.execute(tool, args, worker_id=worker_id)
                obs = self._lin._truncate_observation(result.observation) \
                    if result.ok else result.observation
                obs_str = json.dumps(obs, ensure_ascii=False) if result.ok \
                    else f"ERROR: {result.error}"
                child_history = (
                    f"{history}\n[step {depth + 1}] tool={tool} "
                    f"args={json.dumps(args, ensure_ascii=False)}\nobservation: {obs_str}"
                )
                child = {
                    "action": {"tool": tool, "args": args},
                    "thought": thought,
                    "observation": obs,
                    "ok": result.ok,
                    "error": result.error,
                }
                if result.is_final:
                    child["terminal"] = "is_final"
                    child["success"] = True
                    child["children"] = []
                else:
                    sub = expand(child_history, depth + 1)
                    child["children"] = sub["children"]
                    if sub.get("terminal"):
                        child["terminal"] = sub["terminal"]
                node["children"].append(child)
            return node

        root = expand(f"Task: {task}\n", 0)
        root["task"] = task
        return root, node_budget["n"]

    @staticmethod
    def _flatten_paths(root: Dict[str, Any], task: str) -> List[Dict[str, Any]]:
        """Enumerate root-to-leaf paths as linear trajectories.

        Each path matches the linear generator's schema so TrajectoryFilter /
        TrajectoryQualityEvaluator consume it unchanged.
        """
        paths: List[Dict[str, Any]] = []

        def walk(node: Dict[str, Any], acc: List[Dict[str, Any]]):
            children = node.get("children") or []
            # a "step" node is any node carrying an action
            is_step = "action" in node
            new_acc = acc + [node] if is_step else acc
            if not children:
                # leaf -> emit a trajectory
                steps = [{
                    "thought": s.get("thought"),
                    "action": s.get("action"),
                    "observation": s.get("observation"),
                    **({"ok": s["ok"]} if "ok" in s else {}),
                    **({"error": s["error"]} if s.get("error") else {}),
                    **({"invalid_tool": True} if s.get("invalid_tool") else {}),
                } for s in new_acc]
                last = new_acc[-1] if new_acc else {}
                success = bool(last.get("success"))
                final_answer = None
                if success and isinstance(last.get("observation"), dict):
                    final_answer = last["observation"].get("answer")
                paths.append({
                    "task": task,
                    "steps": steps,
                    "final_answer": final_answer,
                    "num_steps": len(steps),
                    "success": success,
                })
                return
            for ch in children:
                walk(ch, new_acc)

        walk(root, [])
        return paths

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "query",
        output_key: str = "tree",
    ):
        if self.llm_serving is None:
            raise ValueError("AgentExploreTreeGenerator requires an llm_serving instance.")
        if self.sandbox is None:
            raise ValueError("AgentExploreTreeGenerator requires a sandbox instance.")
        df: pd.DataFrame = storage.read("dataframe")
        if input_key not in df.columns:
            raise KeyError(
                f"input_key '{input_key}' not found in columns: {list(df.columns)}"
            )

        tools = self._lin._list_tools()
        known_tools = {t.name for t in tools} | {_FINISH_TOOL} if tools else None
        self.system_prompt = self.system_prompt.format(
            tool_catalog=self._lin._render_tool_catalog(tools)
        ) if "{tool_catalog}" in self.system_prompt else self.system_prompt

        tasks = [str(t) for t in df[input_key].tolist()]
        results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

        def _one(task: str) -> Dict[str, Any]:
            worker_id = self.sandbox.new_worker_id()
            session_id = None
            if getattr(self.sandbox, "stateful", False):
                try:
                    session_id = self.sandbox.create_session(self.domain, worker_id=worker_id)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"[AgentExploreTreeGenerator] create_session: {exc}")
            try:
                root, n_nodes = self._build_tree(task, known_tools, worker_id)
            finally:
                if session_id is not None:
                    self.sandbox.destroy_session(self.domain, worker_id=worker_id)
            paths = self._flatten_paths(root, task)
            n_success = sum(1 for p in paths if p["success"])
            return {
                "task": task,
                "tree": root,
                "paths": paths,
                "num_nodes": n_nodes,
                "num_paths": len(paths),
                "num_success_paths": n_success,
            }

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            fut_to_idx = {pool.submit(_one, t): i for i, t in enumerate(tasks)}
            for fut in as_completed(fut_to_idx):
                idx = fut_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(f"[AgentExploreTreeGenerator] task {idx}: {exc}")
                    results[idx] = {
                        "task": tasks[idx], "tree": None, "paths": [],
                        "num_nodes": 0, "num_paths": 0, "num_success_paths": 0,
                        "error": str(exc),
                    }

        total_paths = sum(r["num_paths"] for r in results if r)
        total_success = sum(r["num_success_paths"] for r in results if r)
        self.logger.info(
            f"[AgentExploreTreeGenerator] built {len(tasks)} trees, "
            f"{total_paths} paths ({total_success} successful)."
        )
        df[output_key] = results
        storage.write(df)
        return [output_key]
