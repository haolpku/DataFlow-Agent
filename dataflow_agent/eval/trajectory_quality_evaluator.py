"""
TrajectoryQualityEvaluator -- LLM-as-judge quality scoring for agent
exploration trajectories.

For each trajectory it renders a compact transcript (task + numbered
thought/action/observation steps + final answer) and asks an LLM judge to score
it on several rubric axes, returning a JSON verdict. The scores are written back
as new columns so a downstream ``TrajectoryFilter`` (on the overall score) or a
``ReduceOperator`` can keep only high-quality trajectories.

This is the "Evaluator" half of the Generator->Evaluator->Filter loop for agent
data synthesis, and the place where DataFlow's LLM-as-judge strength applies to
agentic data (vs. a sandbox that only *collects* trajectories).

Rubric axes (each 1-5, higher is better):
    - goal_achievement: did the final answer actually solve the task?
    - efficiency:        were the steps purposeful (no wasted/looping actions)?
    - coherence:         do thoughts follow from observations (no hallucination)?
    - tool_use:          were tools chosen and parameterised correctly?
The judge also returns an overall float score in [0, 1] and a short rationale.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


_JUDGE_SYSTEM_PROMPT = """You are a strict evaluator of AI agent trajectories.

An agent was given a task and solved it by calling tools in a sandbox, one step
at a time (thought -> tool call -> observation), ending with a final answer.

Score the trajectory on these axes, each an INTEGER 1-5 (5 = excellent):
- goal_achievement: does the final answer correctly and completely solve the task?
- efficiency: were steps purposeful, with no wasted, redundant, or looping actions?
- coherence: do the thoughts logically follow from observations, with no hallucinated facts?
- tool_use: were tools well chosen and called with correct arguments?

Then give:
- overall: a float in [0,1] summarizing overall quality.
- rationale: one or two sentences justifying the scores.

Respond with ONLY a JSON object, no markdown fences:
{"goal_achievement": <1-5>, "efficiency": <1-5>, "coherence": <1-5>,
 "tool_use": <1-5>, "overall": <0-1 float>, "rationale": "<text>"}
"""

_AXES = ("goal_achievement", "efficiency", "coherence", "tool_use")


def _as_traj(value: Any) -> Optional[Dict[str, Any]]:
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
class TrajectoryQualityEvaluator(OperatorABC):
    """LLM-as-judge scorer for agent trajectories.

    Args:
        llm_serving: LLM used as the judge.
        max_workers: Concurrent judging threads.
        max_observation_chars: Per-observation cap when rendering the transcript
            (keeps the judge prompt bounded).
        system_prompt: Optional override of the judge rubric prompt.
        score_prefix: Prefix for the per-axis output columns.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        max_workers: int = 8,
        max_observation_chars: int = 1500,
        system_prompt: Optional[str] = None,
        score_prefix: str = "traj_",
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.max_workers = max_workers
        self.max_observation_chars = max_observation_chars
        self.system_prompt = system_prompt or _JUDGE_SYSTEM_PROMPT
        self.score_prefix = score_prefix

    @staticmethod
    def get_desc(lang: str = "zh"):
        if lang == "zh":
            return (
                "该算子用 LLM-as-judge 对 agent 探索轨迹做质量打分（Evaluator）。\n\n"
                "评分维度（各 1-5）：goal_achievement / efficiency / coherence / tool_use；"
                "另给 overall（0-1 浮点）与 rationale。\n\n"
                "输入参数：\n"
                "- llm_serving: 作为裁判的 LLM\n"
                "- max_workers: 并发裁判线程数\n"
                "- max_observation_chars: 渲染轨迹时单步 observation 截断长度\n\n"
                "运行参数：input_key（轨迹字段，默认 \"trajectory\"）、"
                "output_key（总分字段，默认 \"traj_overall\"）。\n"
                "输出：写入 traj_goal_achievement / traj_efficiency / traj_coherence / "
                "traj_tool_use / traj_overall / traj_rationale 列。"
            )
        return (
            "LLM-as-judge quality scorer for agent trajectories. Scores each on "
            "goal_achievement / efficiency / coherence / tool_use (1-5) plus an "
            "overall float in [0,1] and a rationale. Run args: input_key "
            "(default 'trajectory'), output_key (default 'traj_overall')."
        )

    # ------------------------------------------------------------------ #
    def _render_transcript(self, traj: Dict[str, Any]) -> str:
        lines = [f"Task: {traj.get('task', '')}", "", "Steps:"]
        for i, st in enumerate(traj.get("steps") or [], 1):
            action = st.get("action") or {}
            tool = action.get("tool")
            args = action.get("args", {})
            thought = st.get("thought")
            obs = st.get("observation")
            try:
                obs_str = obs if isinstance(obs, str) else json.dumps(obs, ensure_ascii=False)
            except (TypeError, ValueError):
                obs_str = str(obs)
            if obs_str and len(obs_str) > self.max_observation_chars:
                obs_str = obs_str[: self.max_observation_chars] + "...[truncated]"
            flag = ""
            if st.get("parse_error"):
                flag = " [PARSE_ERROR]"
            elif st.get("invalid_tool"):
                flag = " [INVALID_TOOL]"
            elif st.get("ok") is False:
                flag = " [TOOL_ERROR]"
            lines.append(
                f"  {i}. thought={thought!r} tool={tool} "
                f"args={json.dumps(args, ensure_ascii=False)}{flag}"
            )
            lines.append(f"     observation: {obs_str}")
        lines.append("")
        lines.append(f"Final answer: {traj.get('final_answer')}")
        lines.append(f"(reported success={traj.get('success')}, "
                     f"num_steps={traj.get('num_steps')})")
        return "\n".join(lines)

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            start = text.find("{")
            if start == -1:
                return None
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        break
        if candidate is None:
            return None
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    def _empty_verdict(self, rationale: str) -> Dict[str, Any]:
        v = {axis: None for axis in _AXES}
        v["overall"] = None
        v["rationale"] = rationale
        return v

    def _judge_one(self, traj: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if traj is None:
            return self._empty_verdict("unparseable_trajectory")
        transcript = self._render_transcript(traj)
        try:
            responses = self.llm_serving.generate_from_input(
                [transcript], self.system_prompt
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[TrajectoryQualityEvaluator] judge call failed: {exc}")
            return self._empty_verdict(f"judge_error: {exc}")
        verdict = self._extract_json(responses[0] if responses else "")
        if verdict is None:
            return self._empty_verdict("unparseable_verdict")
        # normalize types defensively
        out: Dict[str, Any] = {}
        for axis in _AXES:
            try:
                out[axis] = int(verdict.get(axis)) if verdict.get(axis) is not None else None
            except (TypeError, ValueError):
                out[axis] = None
        try:
            out["overall"] = float(verdict.get("overall")) if verdict.get("overall") is not None else None
        except (TypeError, ValueError):
            out["overall"] = None
        out["rationale"] = str(verdict.get("rationale", ""))
        return out

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        output_key: str = "traj_overall",
    ):
        if self.llm_serving is None:
            raise ValueError("TrajectoryQualityEvaluator requires an llm_serving instance.")
        df: pd.DataFrame = storage.read("dataframe")
        if input_key not in df.columns:
            raise KeyError(
                f"input_key '{input_key}' not found in columns: {list(df.columns)}"
            )

        trajs = [_as_traj(v) for v in df[input_key].tolist()]
        verdicts: List[Optional[Dict[str, Any]]] = [None] * len(trajs)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            fut_to_idx = {pool.submit(self._judge_one, t): i for i, t in enumerate(trajs)}
            for fut in as_completed(fut_to_idx):
                idx = fut_to_idx[fut]
                try:
                    verdicts[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(f"[TrajectoryQualityEvaluator] verdict {idx}: {exc}")
                    verdicts[idx] = self._empty_verdict(f"error: {exc}")

        for axis in _AXES:
            df[f"{self.score_prefix}{axis}"] = [v.get(axis) for v in verdicts]
        # the user-facing overall score column name is the output_key
        df[output_key] = [v.get("overall") for v in verdicts]
        df[f"{self.score_prefix}rationale"] = [v.get("rationale") for v in verdicts]

        scored = [v.get("overall") for v in verdicts if v.get("overall") is not None]
        mean = sum(scored) / len(scored) if scored else float("nan")
        self.logger.info(
            f"[TrajectoryQualityEvaluator] scored {len(scored)}/{len(trajs)} "
            f"trajectories, mean overall={mean:.3f}"
        )
        storage.write(df)
        return [output_key]
