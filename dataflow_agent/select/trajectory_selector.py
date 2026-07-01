"""
TrajectorySelector -- deterministic top-N diverse trajectory selection.

A tree-search-style selection algorithm brought into the DataFlow-Agent operator
framework. Where ``TrajectoryFilter`` keeps or drops each trajectory by boolean
rules, and ``TrajectoryQualityEvaluator`` scores with an LLM, this operator does
something neither does: from a *pool* of candidate trajectories it **picks the
best N while enforcing diversity** -- no LLM calls, fully deterministic.

Scoring:
    - depth_score     = min(len(steps) / 5.0, 1.0) * 40
    - info_score      = normalized(avg observation length) * 30   # min/max over the pool
    - diversity_score = (distinct tool count / total_tools) * 30
    total = depth + info + diversity   (max 100)

Selection: sort by score desc, greedily take the top ones, but skip a candidate
whose action-set Jaccard similarity to an already-selected trajectory exceeds
``path_similarity_threshold`` (default 0.7). Keep at most ``max_selected``.

Jaccard is computed over each trajectory's set of per-step action signatures
``(tool, canonical_json(args))`` -- a measure of how much two trajectories
overlap in what they *did*.

Two input modes (auto-detected from the column contents):

* **mode "tree"** -- ``input_key`` points at the ``AgentExploreTreeGenerator``
  output (a dict carrying a ``paths`` list). For each row (one task's tree) we
  select the top-N of its ``paths`` and write the list to ``output_key``. This
  is the canonical usage: one seed -> one tree -> N chosen paths.
* **mode "rows"** -- ``input_key`` points at a linear-trajectory column (e.g.
  the Generator's ``trajectory``). The whole DataFrame is one candidate pool; we
  keep the selected rows and drop the rest (like a Filter, but by score +
  diversity).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pandas as pd

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


def _as_obj(value: Any) -> Optional[Any]:
    """Coerce a stored dict/list (or its JSON string) back into an object."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _action_signature(step: Dict[str, Any]) -> str:
    """Canonical (tool, args) signature for one step; used for Jaccard sets.

    Mirrors AgentExploreTreeGenerator._action_key's canonicalization so two
    identical actions collapse to the same signature regardless of key order.
    """
    action = step.get("action") or {}
    tool = str(action.get("tool"))
    try:
        args = json.dumps(action.get("args", {}), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(action.get("args"))
    return f"{tool}({args})"


@OPERATOR_REGISTRY.register()
class TrajectorySelector(OperatorABC):
    """Deterministic top-N diverse trajectory selector.

    Args:
        max_selected: Max trajectories to keep (per tree in "tree" mode, or from
            the whole pool in "rows" mode).
        min_depth: Drop trajectories with fewer than this many steps before
            scoring (valid-leaf depth filter).
        path_similarity_threshold: Jaccard similarity (over per-step action
            signatures) above which a candidate is considered a near-duplicate
            of an already-selected trajectory and skipped. Default 0.7.
        total_tools: Denominator for the diversity score (size of the tool
            catalog). If None, inferred from the distinct tools seen across the
            candidate pool.
        mode: "tree" | "rows" | "auto". "auto" inspects the column: dict-with-
            ``paths`` -> tree; otherwise rows.
        selected_key: In "tree" mode, the column to write the selected-paths
            list into (defaults to output_key).
    """

    def __init__(
        self,
        max_selected: int = 3,
        min_depth: int = 2,
        path_similarity_threshold: float = 0.7,
        total_tools: Optional[int] = None,
        mode: str = "auto",
    ):
        self.logger = get_logger()
        self.max_selected = max_selected
        self.min_depth = min_depth
        self.path_similarity_threshold = path_similarity_threshold
        self.total_tools = total_tools
        self.mode = mode

    @staticmethod
    def get_desc(lang: str = "zh"):
        if lang == "zh":
            return (
                "该算子从候选轨迹中确定性地挑选 top-N 条高质量且多样的轨迹"
                "(确定性树搜索选择算法,无 LLM 调用)。\n\n"
                "打分(满分 100):\n"
                "- 深度分 = min(步数/5, 1) * 40\n"
                "- 信息量分 = 归一化(平均 observation 长度) * 30\n"
                "- 多样性分 = (使用的不同工具数 / total_tools) * 30\n\n"
                "选择:按分数降序贪心选取,若与已选轨迹的动作集合 Jaccard 相似度 > "
                "path_similarity_threshold(默认 0.7)则跳过,最多选 max_selected 条。\n\n"
                "输入参数:max_selected、min_depth、path_similarity_threshold、"
                "total_tools、mode(tree/rows/auto)。\n"
                "运行参数:input_key、output_key。\n"
                "两种模式:tree=从 AgentExploreTreeGenerator 的 paths 中每棵树选 N 条;"
                "rows=把整个表的线性轨迹当候选池选 N 行(其余丢弃)。"
            )
        return (
            "Deterministically selects the top-N diverse trajectories from a "
            "candidate pool (deterministic tree-search selection; no LLM). "
            "Scores by depth(40) + info-length(30) + tool-diversity(30), then "
            "greedily picks best-first while skipping near-duplicates by action-set "
            "Jaccard similarity (> path_similarity_threshold). Modes: 'tree' selects "
            "N paths per AgentExploreTreeGenerator tree; 'rows' selects N rows from "
            "the whole linear-trajectory pool. Run args: input_key, output_key."
        )

    # ------------------------------------------------------------------ #
    # scoring: depth + info-length + tool-diversity
    # ------------------------------------------------------------------ #
    @staticmethod
    def _avg_obs_length(traj: Dict[str, Any]) -> float:
        steps = traj.get("steps") or []
        if not steps:
            return 0.0
        total = 0
        for st in steps:
            obs = st.get("observation")
            try:
                total += len(obs if isinstance(obs, str)
                             else json.dumps(obs, ensure_ascii=False))
            except (TypeError, ValueError):
                total += len(str(obs))
        return total / len(steps)

    def _score(
        self, traj: Dict[str, Any], avg_obs_length: float,
        min_length: float, length_range: float, total_tools: int,
    ) -> float:
        steps = traj.get("steps") or []
        # depth
        depth_score = min(len(steps) / 5.0, 1.0) * 40
        # info (relative normalization over the pool)
        normalized = (avg_obs_length - min_length) / length_range if length_range > 0 else 0.0
        info_score = normalized * 30
        # diversity: distinct tool names / total tool catalog size
        tools = set()
        for st in steps:
            action = st.get("action") or {}
            tool = action.get("tool")
            if tool:
                tools.add(tool)
        diversity_score = len(tools) / max(total_tools, 1) * 30
        return depth_score + info_score + diversity_score

    @staticmethod
    def _action_set(traj: Dict[str, Any]) -> set:
        return {_action_signature(st) for st in (traj.get("steps") or []) if st.get("action")}

    def _select_from_pool(self, trajs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Score + greedily select top-N with Jaccard de-dup. Returns chosen trajs."""
        chosen_idx = self._select_indices(trajs)
        out = []
        for i in chosen_idx:
            out.append({**trajs[i], "_select_score": self._last_scores[i]})
        return out

    def _select_indices(self, trajs: List[Dict[str, Any]]) -> List[int]:
        """Core selection. Returns the indices (into ``trajs``) that are chosen,
        so callers can map back to exact rows even when trajectories are equal.
        Also stashes per-index scores in ``self._last_scores``.
        """
        self._last_scores: Dict[int, float] = {}
        # depth filter first (drop shallow trajectories)
        cand_idx = [i for i, t in enumerate(trajs)
                    if len(t.get("steps") or []) >= self.min_depth]
        if not cand_idx:
            return []

        # total_tools denominator: explicit, else inferred from the pool
        if self.total_tools is not None:
            total_tools = self.total_tools
        else:
            seen_tools = set()
            for i in cand_idx:
                for st in (trajs[i].get("steps") or []):
                    tool = (st.get("action") or {}).get("tool")
                    if tool:
                        seen_tools.add(tool)
            total_tools = len(seen_tools) or 1

        # info-length normalization range over the candidate pool
        avg_lengths = {i: self._avg_obs_length(trajs[i]) for i in cand_idx}
        vals = list(avg_lengths.values())
        min_length = min(vals) if vals else 0.0
        max_length = max(vals) if vals else 1.0
        length_range = max_length - min_length if max_length > min_length else 1.0

        scored = []
        for i in cand_idx:
            s = self._score(trajs[i], avg_lengths[i], min_length, length_range, total_tools)
            self._last_scores[i] = round(s, 2)
            scored.append((s, i))
        # sort by score desc, tie-break by original index for determinism
        scored.sort(key=lambda x: (-x[0], x[1]))

        selected_idx: List[int] = []
        selected_sets: List[set] = []
        for score, i in scored:
            if len(selected_idx) >= self.max_selected:
                break
            cur = self._action_set(trajs[i])
            too_similar = False
            for sel in selected_sets:
                inter = len(cur & sel)
                union = len(cur | sel)
                jacc = inter / union if union > 0 else 0.0
                if jacc > self.path_similarity_threshold:
                    too_similar = True
                    break
            if not too_similar:
                selected_idx.append(i)
                selected_sets.append(cur)
        return selected_idx

    # ------------------------------------------------------------------ #
    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "tree",
        output_key: str = "selected_trajectories",
    ):
        df: pd.DataFrame = storage.read("dataframe")
        if input_key not in df.columns:
            raise KeyError(
                f"input_key '{input_key}' not found in columns: {list(df.columns)}"
            )

        # detect mode from the first non-null cell
        mode = self.mode
        if mode == "auto":
            sample = next((v for v in df[input_key].tolist() if v is not None), None)
            obj = _as_obj(sample)
            mode = "tree" if isinstance(obj, dict) and "paths" in obj else "rows"

        if mode == "tree":
            out_lists: List[Any] = []
            counts: List[int] = []
            for value in df[input_key].tolist():
                tree = _as_obj(value)
                paths = (tree or {}).get("paths") if isinstance(tree, dict) else None
                chosen = self._select_from_pool(paths) if paths else []
                out_lists.append(chosen)
                counts.append(len(chosen))
            df[output_key] = out_lists
            df[f"{output_key}_count"] = counts
            self.logger.info(
                f"[TrajectorySelector] tree mode: selected "
                f"{sum(counts)} trajectories across {len(df)} trees "
                f"(max {self.max_selected}/tree)."
            )
            storage.write(df)
            return [output_key]

        # rows mode: whole column is one candidate pool
        trajs, idx_map = [], []
        for i, value in enumerate(df[input_key].tolist()):
            t = _as_obj(value)
            if isinstance(t, dict):
                trajs.append(t)
                idx_map.append(i)
        chosen_local = self._select_indices(trajs)          # indices into `trajs`
        keep_idx = {idx_map[i] for i in chosen_local}        # back to DataFrame rows
        kept = df.iloc[sorted(keep_idx)].reset_index(drop=True)
        self.logger.info(
            f"[TrajectorySelector] rows mode: kept {len(kept)}/{len(df)} "
            f"trajectories (max {self.max_selected})."
        )
        storage.write(kept)
        return [input_key]
