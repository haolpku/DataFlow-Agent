"""
TrajectoryFilter -- deterministic, rule-based quality gate for agent
exploration trajectories.

Consumes the structured trajectories emitted by ``AgentExploreGenerator``
(``{task, steps, final_answer, num_steps, success}``) and drops rows that fail
any enabled rule. No LLM calls -- fast and reproducible. Use it as the cheap
first pass before the (expensive) LLM-as-judge ``TrajectoryQualityEvaluator``.

Rules (all opt-in via constructor flags):
    - require_success: keep only trajectories that reached a final answer.
    - min_steps / max_steps: bound trajectory length.
    - drop_parse_errors: drop trajectories containing any unparseable LLM step.
    - drop_invalid_tools: drop trajectories that hallucinated a tool name.
    - drop_tool_errors: drop trajectories with any failed tool execution.
    - max_repeated_actions: drop trajectories that repeat the same
      (tool, args) action more than this many times (loop/no-progress guard).
    - require_nonempty_answer: drop trajectories whose final_answer is empty.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Optional

import pandas as pd

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


def _as_traj(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a stored trajectory (dict or JSON string) into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


@OPERATOR_REGISTRY.register()
class TrajectoryFilter(OperatorABC):
    """Rule-based filter over agent trajectories. Removes rows that fail."""

    def __init__(
        self,
        require_success: bool = True,
        min_steps: int = 1,
        max_steps: Optional[int] = None,
        drop_parse_errors: bool = True,
        drop_invalid_tools: bool = True,
        drop_tool_errors: bool = False,
        max_repeated_actions: Optional[int] = None,
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

    @staticmethod
    def get_desc(lang: str = "zh"):
        if lang == "zh":
            return (
                "该算子对 agent 探索轨迹进行规则化质量过滤（确定性、无 LLM 调用）。\n\n"
                "输入参数：\n"
                "- require_success: 仅保留成功（有最终答案）的轨迹\n"
                "- min_steps / max_steps: 步数下/上界\n"
                "- drop_parse_errors: 丢弃含不可解析步的轨迹\n"
                "- drop_invalid_tools: 丢弃幻觉了工具名的轨迹\n"
                "- drop_tool_errors: 丢弃含工具执行失败的轨迹\n"
                "- max_repeated_actions: 同一 (tool,args) 重复超过该次数则丢弃（死循环防护）\n"
                "- require_nonempty_answer: 丢弃最终答案为空的轨迹\n\n"
                "运行参数：input_key（轨迹字段，默认 \"trajectory\"）。\n"
                "输出：原地写回质量元信息列 _traj_filter_reason；删除不合格行。"
            )
        return (
            "Deterministic rule-based quality filter over agent trajectories "
            "(no LLM calls). Drops rows failing any enabled rule "
            "(success / step bounds / parse errors / invalid tools / tool "
            "errors / repeated-action loops / empty answer). "
            "Run arg: input_key (default 'trajectory')."
        )

    # ------------------------------------------------------------------ #
    def _reject_reason(self, traj: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return a rejection reason string, or None if the trajectory passes."""
        if traj is None:
            return "unparseable_trajectory"

        steps = traj.get("steps") or []
        if self.require_success and not traj.get("success"):
            return "not_success"
        if self.min_steps is not None and len(steps) < self.min_steps:
            return f"too_few_steps(<{self.min_steps})"
        if self.max_steps is not None and len(steps) > self.max_steps:
            return f"too_many_steps(>{self.max_steps})"
        if self.require_nonempty_answer:
            ans = traj.get("final_answer")
            if ans is None or (isinstance(ans, str) and not ans.strip()):
                return "empty_answer"

        action_counter: Counter = Counter()
        for st in steps:
            if self.drop_parse_errors and st.get("parse_error"):
                return "parse_error_step"
            if self.drop_invalid_tools and st.get("invalid_tool"):
                return "invalid_tool_step"
            if self.drop_tool_errors and st.get("ok") is False:
                return "tool_error_step"
            action = st.get("action") or {}
            tool = action.get("tool")
            if tool and tool != "finish":
                # cheap canonical key for repeated-action detection
                try:
                    key = (tool, json.dumps(action.get("args", {}), sort_keys=True,
                                            ensure_ascii=False))
                except (TypeError, ValueError):
                    key = (tool, str(action.get("args")))
                action_counter[key] += 1

        if self.max_repeated_actions is not None and action_counter:
            most = action_counter.most_common(1)[0][1]
            if most > self.max_repeated_actions:
                return f"repeated_action(>{self.max_repeated_actions})"
        return None

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        reason_key: str = "_traj_filter_reason",
    ):
        df: pd.DataFrame = storage.read("dataframe")
        if input_key not in df.columns:
            raise KeyError(
                f"input_key '{input_key}' not found in columns: {list(df.columns)}"
            )

        reasons: List[Optional[str]] = []
        for value in df[input_key].tolist():
            reasons.append(self._reject_reason(_as_traj(value)))

        df[reason_key] = reasons
        kept = df[df[reason_key].isna()].drop(columns=[reason_key])
        n_in, n_out = len(df), len(kept)
        self.logger.info(
            f"[TrajectoryFilter] kept {n_out}/{n_in} trajectories "
            f"({n_in - n_out} dropped)."
        )
        if n_in - n_out:
            dropped = Counter(r for r in reasons if r is not None)
            self.logger.info(f"[TrajectoryFilter] drop reasons: {dict(dropped)}")

        storage.write(kept)
        return [input_key]
