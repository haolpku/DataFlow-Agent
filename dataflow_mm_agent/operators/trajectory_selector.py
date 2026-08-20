"""Official deterministic top-N diversity selector for canonical trajectories."""

from __future__ import annotations

import json
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from .utils.trajectory import as_trajectory_dict, observation_text, steps


def _action_signature(step: dict[str, Any]) -> str:
    action = step.get("action") or {}
    tool = str(action.get("tool"))
    try:
        args = json.dumps(action.get("args", {}), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(action.get("args"))
    return f"{tool}({args})"


@OPERATOR_REGISTRY.register()
class AgentMMTrajectorySelector(OperatorABC):
    """Score by depth/information/tool-diversity, then Jaccard de-duplicate."""

    def __init__(
        self,
        max_selected: int = 3,
        min_depth: int = 2,
        path_similarity_threshold: float = 0.7,
        total_tools: int | None = None,
        mode: str = "auto",
    ):
        self.logger = get_logger()
        self.max_selected = max_selected
        self.min_depth = min_depth
        self.path_similarity_threshold = path_similarity_threshold
        self.total_tools = total_tools
        self.mode = mode

    @staticmethod
    def _avg_observation_length(trajectory: dict[str, Any]) -> float:
        trajectory_steps = steps(trajectory)
        if not trajectory_steps:
            return 0.0
        return sum(
            len(observation_text(trajectory, step)) for step in trajectory_steps
        ) / len(trajectory_steps)

    @staticmethod
    def _score(
        trajectory: dict[str, Any],
        average_length: float,
        minimum_length: float,
        length_range: float,
        total_tools: int,
    ) -> float:
        trajectory_steps = steps(trajectory)
        depth_score = min(len(trajectory_steps) / 5.0, 1.0) * 40
        normalized = (
            (average_length - minimum_length) / length_range
            if length_range > 0
            else 0.0
        )
        info_score = normalized * 30
        tools = {
            (step.get("action") or {}).get("tool")
            for step in trajectory_steps
            if (step.get("action") or {}).get("tool")
        }
        diversity_score = len(tools) / max(total_tools, 1) * 30
        return depth_score + info_score + diversity_score

    @staticmethod
    def _action_set(trajectory: dict[str, Any]) -> set[str]:
        return {
            _action_signature(step)
            for step in steps(trajectory)
            if step.get("action")
        }

    def _select_indices(self, trajectories: list[dict[str, Any]]) -> list[int]:
        self._last_scores: dict[int, float] = {}
        candidates = [
            index
            for index, trajectory in enumerate(trajectories)
            if len(steps(trajectory)) >= self.min_depth
        ]
        if not candidates:
            return []
        if self.total_tools is not None:
            total_tools = self.total_tools
        else:
            seen_tools = {
                (step.get("action") or {}).get("tool")
                for index in candidates
                for step in steps(trajectories[index])
                if (step.get("action") or {}).get("tool")
            }
            total_tools = len(seen_tools) or 1
        average_lengths = {
            index: self._avg_observation_length(trajectories[index])
            for index in candidates
        }
        minimum = min(average_lengths.values())
        maximum = max(average_lengths.values())
        length_range = maximum - minimum if maximum > minimum else 1.0
        scored: list[tuple[float, int]] = []
        for index in candidates:
            score = self._score(
                trajectories[index],
                average_lengths[index],
                minimum,
                length_range,
                total_tools,
            )
            self._last_scores[index] = round(score, 2)
            scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected: list[int] = []
        selected_sets: list[set[str]] = []
        for _, index in scored:
            if len(selected) >= self.max_selected:
                break
            current = self._action_set(trajectories[index])
            too_similar = False
            for prior in selected_sets:
                union = len(current | prior)
                jaccard = len(current & prior) / union if union else 0.0
                if jaccard > self.path_similarity_threshold:
                    too_similar = True
                    break
            if not too_similar:
                selected.append(index)
                selected_sets.append(current)
        return selected

    def _select_from_pool(
        self, trajectories: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        indices = self._select_indices(trajectories)
        return [
            {**trajectories[index], "_select_score": self._last_scores[index]}
            for index in indices
        ]

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "tree",
        output_key: str = "selected_trajectories",
    ):
        dataframe = storage.read(output_type="dataframe")
        if input_key not in dataframe.columns:
            raise KeyError(
                f"input_key {input_key!r} not found in columns: "
                f"{list(dataframe.columns)}"
            )
        mode = self.mode
        if mode == "auto":
            sample = next(
                (value for value in dataframe[input_key].tolist() if value is not None),
                None,
            )
            if isinstance(sample, str):
                try:
                    sample = json.loads(sample)
                except json.JSONDecodeError:
                    pass
            mode = "tree" if isinstance(sample, dict) and "paths" in sample else "rows"
        if mode == "tree":
            selected_lists: list[list[dict[str, Any]]] = []
            for value in dataframe[input_key].tolist():
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = None
                raw_paths = value.get("paths") if isinstance(value, dict) else None
                paths = [
                    parsed
                    for path in (raw_paths or [])
                    if (parsed := as_trajectory_dict(path)) is not None
                ]
                selected_lists.append(self._select_from_pool(paths) if paths else [])
            dataframe[output_key] = selected_lists
            dataframe[f"{output_key}_count"] = [
                len(items) for items in selected_lists
            ]
            storage.write(dataframe)
            return [output_key]
        trajectories: list[dict[str, Any]] = []
        row_indices: list[int] = []
        for index, value in enumerate(dataframe[input_key].tolist()):
            trajectory = as_trajectory_dict(value)
            if trajectory is not None:
                trajectories.append(trajectory)
                row_indices.append(index)
        selected_local = self._select_indices(trajectories)
        keep = sorted(row_indices[index] for index in selected_local)
        storage.write(dataframe.iloc[keep].reset_index(drop=True))
        return [input_key]
