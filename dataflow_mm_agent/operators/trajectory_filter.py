"""Official deterministic trajectory filter adapted to MM step metadata."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from .utils.trajectory import (
    as_trajectory_dict,
    normal_success,
    step_error_code,
    step_tool_ok,
    steps,
)


@OPERATOR_REGISTRY.register()
class AgentMMTrajectoryFilter(OperatorABC):
    """Rule-based gate; `require_success` means normal agent completion."""

    def __init__(
        self,
        require_success: bool = True,
        min_steps: int = 1,
        max_steps: int | None = None,
        drop_parse_errors: bool = True,
        drop_invalid_tools: bool = True,
        drop_tool_errors: bool = False,
        max_repeated_actions: int | None = None,
        require_nonempty_answer: bool = True,
    ):
        self.logger = get_logger()
        self.require_success = require_success
        self.min_steps = min_steps
        self.max_steps = max_steps
        self.drop_parse_errors = drop_parse_errors
        self.drop_invalid_tools = drop_invalid_tools
        self.drop_tool_errors = drop_tool_errors
        self.max_repeated_actions = max_repeated_actions
        self.require_nonempty_answer = require_nonempty_answer

    def _reject_reason(self, trajectory: dict[str, Any] | None) -> str | None:
        if trajectory is None:
            return "unparseable_trajectory"
        trajectory_steps = steps(trajectory)
        if self.require_success and not normal_success(trajectory):
            return "not_success"
        if self.min_steps is not None and len(trajectory_steps) < self.min_steps:
            return f"too_few_steps(<{self.min_steps})"
        if self.max_steps is not None and len(trajectory_steps) > self.max_steps:
            return f"too_many_steps(>{self.max_steps})"
        if self.require_nonempty_answer:
            answer = trajectory.get("final_answer")
            if answer is None or (isinstance(answer, str) and not answer.strip()):
                return "empty_answer"

        counter: Counter[tuple[str, str]] = Counter()
        for step in trajectory_steps:
            if self.drop_parse_errors and step.get("parse_error"):
                return "parse_error_step"
            if self.drop_invalid_tools and step_error_code(step) == "unknown_tool":
                return "invalid_tool_step"
            if self.drop_tool_errors and step_tool_ok(step) is False:
                return "tool_error_step"
            action = step.get("action") or {}
            tool = action.get("tool")
            if tool and tool != "finish":
                try:
                    args = json.dumps(
                        action.get("args", {}), sort_keys=True, ensure_ascii=False
                    )
                except (TypeError, ValueError):
                    args = str(action.get("args"))
                counter[(str(tool), args)] += 1
        if self.max_repeated_actions is not None and counter:
            repeated = counter.most_common(1)[0][1]
            if repeated > self.max_repeated_actions:
                return f"repeated_action(>{self.max_repeated_actions})"
        return None

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        reason_key: str = "_traj_filter_reason",
    ):
        dataframe = storage.read(output_type="dataframe")
        if input_key not in dataframe.columns:
            raise KeyError(
                f"input_key {input_key!r} not found in columns: "
                f"{list(dataframe.columns)}"
            )
        reasons = [
            self._reject_reason(as_trajectory_dict(value))
            for value in dataframe[input_key].tolist()
        ]
        dataframe[reason_key] = reasons
        kept = dataframe[dataframe[reason_key].isna()].drop(columns=[reason_key])
        self.logger.info(
            f"[AgentMMTrajectoryFilter] kept {len(kept)}/{len(dataframe)} trajectories"
        )
        storage.write(kept)
        return [input_key]
