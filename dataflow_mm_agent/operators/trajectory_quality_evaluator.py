"""Official trajectory-quality judge adapted to canonical multimodal messages."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from ..contracts import ImageContent, Message, TextContent
from ..serving import ModelServing
from .utils.trajectory import (
    as_trajectory_dict,
    normal_success,
    observation_content,
    step_error_code,
    step_thought,
    step_tool_ok,
    steps,
    task_text,
)


JUDGE_SYSTEM_PROMPT = """你是一名严格的 AI Agent 轨迹评审员。

Agent 在受控环境中逐步调用工具完成任务，最后给出回答。每一步之后展示的图片
是该次工具调用返回的真实多模态 observation。请依据工具调用与 observation
中的证据进行评审，不能仅凭 final answer 的自我陈述判断任务已完成。

请分别对以下四个维度给出 1 到 5 的整数分（5 分最好）：
- goal_achievement：任务是否被正确、完整地解决，真实证据是否足够。
- efficiency：步骤是否有目的，是否存在浪费、重复或循环操作。
- coherence：推理是否紧跟 observation，是否存在幻觉或前后矛盾。
- tool_use：工具选择是否合理，调用参数是否正确。

务必先在 rationale 中完成任务要求、计算、工具参数和 observation 的核验，再
输出任何数值评分。所有分数必须建立在完整分析之上，并与 rationale 的最终结论
一致；不要在 rationale 中推翻已经给出的判断。

overall 为 0 到 1 的浮点数。只输出一个 JSON 对象，不要输出 Markdown 代码块
或额外文字，并严格按照以下字段顺序输出：先 rationale，再输出四项评分，最后
输出 overall。rationale 使用中文：
{"rationale": "<完整核验过程与最终结论>",
 "goal_achievement": <1-5>, "efficiency": <1-5>, "coherence": <1-5>,
 "tool_use": <1-5>, "overall": <0-1 float>}
"""

AXES = ("goal_achievement", "efficiency", "coherence", "tool_use")


@OPERATOR_REGISTRY.register()
class AgentMMTrajectoryQualityEvaluator(OperatorABC):
    """LLM/VLM-as-judge scoring with the upstream four-axis rubric."""

    def __init__(
        self,
        llm_serving: ModelServing | None = None,
        max_workers: int = 8,
        max_observation_chars: int = 1500,
        system_prompt: str | None = None,
        score_prefix: str = "traj_",
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.max_workers = max_workers
        self.max_observation_chars = max_observation_chars
        self.system_prompt = system_prompt or JUDGE_SYSTEM_PROMPT
        self.score_prefix = score_prefix

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return "使用 VLM-as-judge 从目标达成、效率、连贯性和工具使用四维评价多模态轨迹。"
        return "VLM-as-judge trajectory scorer using the official four-axis rubric."

    def _judge_messages(
        self,
        trajectory: dict[str, Any],
        rubric: Any | None = None,
    ) -> tuple[Message, ...]:
        content: list[TextContent | ImageContent] = [
            TextContent(f"任务：{task_text(trajectory)}\n\n轨迹步骤：\n")
        ]
        if rubric is not None:
            verification = trajectory.get("verification")
            content.append(TextContent(
                "任务专用评审参考：\n"
                f"{json.dumps(rubric, ensure_ascii=False, sort_keys=True)}\n"
            ))
            if verification is not None:
                content.append(TextContent(
                    "确定性回放验证（任务完成情况的权威依据）：\n"
                    f"{json.dumps(verification, ensure_ascii=False, sort_keys=True)}\n"
                    "评估过程质量时请结合上述参考。不要根据 Agent 在 final answer "
                    "中的自我陈述推断任务完成，也不要覆盖确定性的 reached_goal "
                    "结果。\n\n"
                ))
            else:
                content.append(TextContent(
                    "本次评审没有提供确定性 verifier 结果。请根据任务、工具调用和"
                    "真实 observation 判断是否完成，不能只相信 final answer 的"
                    "自我陈述。\n\n"
                ))
        for index, step in enumerate(steps(trajectory), start=1):
            action = step.get("action") or {}
            tool = action.get("tool")
            args = action.get("args", {})
            flag = ""
            if step.get("parse_error"):
                flag = " [PARSE_ERROR]"
            elif step_error_code(step) == "unknown_tool":
                flag = " [INVALID_TOOL]"
            elif step_tool_ok(step) is False:
                flag = " [TOOL_ERROR]"
            content.append(TextContent(
                f"{index}. 推理={step_thought(step)!r} 工具={tool} "
                f"参数={json.dumps(args, ensure_ascii=False)}{flag}\n"
                "真实 observation：\n"
            ))
            for item in observation_content(trajectory, step):
                if isinstance(item, TextContent):
                    text = item.text
                    if len(text) > self.max_observation_chars:
                        text = text[:self.max_observation_chars] + "...[truncated]"
                    content.append(TextContent(text + "\n"))
                else:
                    content.append(item)
            content.append(TextContent("\n"))
        content.append(TextContent(
            f"最终回答：{trajectory.get('final_answer')}\n"
            f"（轨迹报告 success={normal_success(trajectory)}，"
            f"步骤数={len(steps(trajectory))}）"
        ))
        return (
            Message.text("system", self.system_prompt),
            Message.of("user", content),
        )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            start = text.find("{")
            if start < 0:
                return None
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(text)):
                character = text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == '"':
                        in_string = False
                    continue
                if character == '"':
                    in_string = True
                elif character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:index + 1]
                        break
        if candidate is None:
            return None
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _empty_verdict(rationale: str) -> dict[str, Any]:
        result = {axis: None for axis in AXES}
        result.update({"overall": None, "rationale": rationale})
        return result

    def _judge_one(self, value: Any, rubric: Any | None = None) -> dict[str, Any]:
        trajectory = as_trajectory_dict(value)
        if trajectory is None:
            return self._empty_verdict("unparseable_trajectory")
        try:
            raw = self.llm_serving.generate(self._judge_messages(trajectory, rubric))
        except Exception as exc:  # judge errors remain row-local
            self.logger.warning(
                f"[AgentMMTrajectoryQualityEvaluator] judge call failed: {exc}"
            )
            return self._empty_verdict(f"judge_error: {exc}")
        verdict = self._extract_json(raw)
        if verdict is None:
            return self._empty_verdict("unparseable_verdict")
        result: dict[str, Any] = {}
        for axis in AXES:
            try:
                result[axis] = (
                    int(verdict.get(axis)) if verdict.get(axis) is not None else None
                )
            except (TypeError, ValueError):
                result[axis] = None
        try:
            result["overall"] = (
                float(verdict.get("overall"))
                if verdict.get("overall") is not None
                else None
            )
        except (TypeError, ValueError):
            result["overall"] = None
        result["rationale"] = str(verdict.get("rationale", ""))
        return result

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        output_key: str = "traj_overall",
        rubric_key: str | None = None,
    ):
        if self.llm_serving is None:
            raise ValueError(
                "AgentMMTrajectoryQualityEvaluator requires an llm_serving instance"
            )
        dataframe = storage.read(output_type="dataframe")
        if input_key not in dataframe.columns:
            raise KeyError(
                f"input_key {input_key!r} not found in columns: "
                f"{list(dataframe.columns)}"
            )
        if rubric_key is not None and rubric_key not in dataframe.columns:
            raise KeyError(
                f"rubric_key {rubric_key!r} not found in columns: "
                f"{list(dataframe.columns)}"
            )
        values = dataframe[input_key].tolist()
        rubrics = (
            dataframe[rubric_key].tolist()
            if rubric_key is not None
            else [None] * len(values)
        )
        verdicts: list[dict[str, Any] | None] = [None] * len(values)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._judge_one, value, rubrics[index]): index
                for index, value in enumerate(values)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    verdicts[index] = future.result()
                except Exception as exc:
                    verdicts[index] = self._empty_verdict(f"error: {exc}")
        complete = [item or self._empty_verdict("missing_verdict") for item in verdicts]
        for axis in AXES:
            dataframe[f"{self.score_prefix}{axis}"] = [item[axis] for item in complete]
        dataframe[output_key] = [item["overall"] for item in complete]
        dataframe[f"{self.score_prefix}rationale"] = [
            item["rationale"] for item in complete
        ]
        storage.write(dataframe)
        return [output_key]
