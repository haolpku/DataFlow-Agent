"""Official selective trajectory repair adapted to controlled MM rollouts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from ..contracts import Content, ImageContent, TextContent
from ..env.registry import resolve_scenario
from ..contracts.environment import Scenario
from ..serving import ModelServing
from .utils.trajectory import (
    as_trajectory_dict,
    normal_success,
    observation_content,
    step_error_code,
    step_tool_ok,
    steps,
    task_text,
)
from .explore_generator import AgentMMExploreGenerator


REFINE_CONTEXT_HEADER = """You previously attempted this task and the result was judged low quality.

--- YOUR PREVIOUS ATTEMPT TEXT SUMMARY ---
{prior}
--- END PREVIOUS ATTEMPT TEXT SUMMARY ---

Diagnosis of what went wrong: {diagnosis}
"""

REFINE_CONTEXT_FOOTER = """
Now solve the task again, AVOIDING the mistakes above. Be more direct: choose the
right tool, pass correct arguments, do not loop, and call "finish" with a complete
final answer as soon as you can support it.

Task: {task}"""


@OPERATOR_REGISTRY.register()
class AgentMMTrajectoryRefiner(OperatorABC):
    """Repair only failed or below-threshold trajectories by re-exploring."""

    def __init__(
        self,
        llm_serving: ModelServing | None = None,
        max_steps: int | None = None,
        max_workers: int = 8,
        refine_failed: bool = True,
        score_threshold: float | None = 0.6,
        score_key: str = "traj_overall",
        original_key: str | None = "trajectory_original",
        system_prompt: str | None = None,
        max_prior_chars: int = 2000,
        max_prior_images: int | None = 4,
        validate_tool_names: bool = True,
        include_host_tools: bool = True,
    ):
        if max_prior_chars < 1:
            raise ValueError("max_prior_chars must be positive")
        if max_prior_images is not None and (
            isinstance(max_prior_images, bool)
            or not isinstance(max_prior_images, int)
            or max_prior_images < 1
        ):
            raise ValueError("max_prior_images must be positive or None")
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.max_steps = max_steps
        self.max_workers = max_workers
        self.refine_failed = refine_failed
        self.score_threshold = score_threshold
        self.score_key = score_key
        self.original_key = original_key
        self.system_prompt = system_prompt
        self.max_prior_chars = max_prior_chars
        self.max_prior_images = max_prior_images
        self.validate_tool_names = validate_tool_names
        self.include_host_tools = include_host_tools
        self._generator = AgentMMExploreGenerator(
            serving=llm_serving,
            max_steps=max_steps,
            max_workers=max_workers,
            system_prompt=system_prompt,
            validate_tool_names=validate_tool_names,
            include_host_tools=include_host_tools,
        )

    @staticmethod
    def _diagnose(trajectory: dict[str, Any]) -> str:
        if not normal_success(trajectory):
            answer = trajectory.get("final_answer")
            if answer is None or (isinstance(answer, str) and not answer.strip()):
                return (
                    "the agent never produced a final answer (it ran out of "
                    "steps or stopped without calling finish)."
                )
        notes: list[str] = []
        seen_actions: dict[str, int] = {}
        for step in steps(trajectory):
            if step.get("parse_error"):
                notes.append("one step produced an unparseable (non-JSON) response")
            if step_error_code(step) == "unknown_tool":
                tool = (step.get("action") or {}).get("tool")
                notes.append(f"a non-existent tool {tool!r} was called")
            if step_tool_ok(step) is False:
                code = step_error_code(step)
                notes.append(f"a tool call failed ({code})")
            action = step.get("action") or {}
            tool = action.get("tool")
            if tool and tool != "finish":
                try:
                    args = json.dumps(
                        action.get("args", {}), sort_keys=True, ensure_ascii=False
                    )
                except (TypeError, ValueError):
                    args = str(action.get("args"))
                key = f"{tool}:{args}"
                seen_actions[key] = seen_actions.get(key, 0) + 1
        if any(count > 1 for count in seen_actions.values()):
            notes.append("the same action was repeated without progress (a loop)")
        if not notes:
            return (
                "the answer was judged incomplete or low quality; produce a more "
                "correct and complete answer."
            )
        return "; ".join(dict.fromkeys(notes)) + "."

    def _render_prior(self, trajectory: dict[str, Any]) -> str:
        """Render a bounded text-only summary; images are attached separately."""
        lines: list[str] = []
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
            lines.append(
                f"  {index}. tool={tool} "
                f"args={json.dumps(args, ensure_ascii=False)}{flag}"
            )
            observed = observation_content(trajectory, step)
            text = " ".join(
                item.text.strip()
                for item in observed
                if isinstance(item, TextContent) and item.text.strip()
            )
            image_count = sum(isinstance(item, ImageContent) for item in observed)
            if text:
                lines.append(f"     observation_text: {text}")
            if image_count:
                lines.append(
                    f"     observation_images: {image_count} "
                    "(attached separately as multimodal image content when selected)"
                )
        lines.append(f"  final_answer: {trajectory.get('final_answer')}")
        rendered = "\n".join(lines)
        if len(rendered) > self.max_prior_chars:
            rendered = rendered[:self.max_prior_chars] + "\n  ...[truncated]"
        return rendered

    def _prior_images(
        self,
        trajectory: dict[str, Any],
    ) -> list[tuple[int, ImageContent]]:
        images = [
            (index, item)
            for index, step in enumerate(steps(trajectory), start=1)
            for item in observation_content(trajectory, step)
            if isinstance(item, ImageContent)
        ]
        if self.max_prior_images is not None:
            images = images[-self.max_prior_images:]
        return images

    def _refine_instruction_content(
        self,
        trajectory: dict[str, Any],
        *,
        diagnosis: str,
        task: str,
    ) -> tuple[Content, ...]:
        content: list[Content] = [TextContent(REFINE_CONTEXT_HEADER.format(
            prior=self._render_prior(trajectory),
            diagnosis=diagnosis,
        ))]
        images = self._prior_images(trajectory)
        if images:
            content.append(TextContent(
                "--- SELECTED PREVIOUS OBSERVATION IMAGES "
                "(chronological order) ---"
            ))
            for step_index, image in images:
                content.extend((
                    TextContent(f"Previous observation image from step {step_index}:"),
                    image,
                ))
            content.append(TextContent(
                "--- END SELECTED PREVIOUS OBSERVATION IMAGES ---"
            ))
        content.append(TextContent(REFINE_CONTEXT_FOOTER.format(task=task)))
        return tuple(content)

    def _should_refine(self, trajectory: dict[str, Any] | None, score: Any) -> bool:
        if trajectory is None:
            return False
        if self.refine_failed and not normal_success(trajectory):
            return True
        if self.score_threshold is not None and score is not None:
            try:
                return float(score) < self.score_threshold
            except (TypeError, ValueError):
                return False
        return False

    def _refine_one(self, value: Any, score: Any) -> dict[str, Any]:
        trajectory = as_trajectory_dict(value)
        if not self._should_refine(trajectory, score):
            return {
                "trajectory": value,
                "original": None,
                "refined": False,
                "note": "kept (passed quality / unparseable)",
                "improved": False,
            }
        scenario_value = trajectory.get("scenario") or {}
        try:
            scenario = resolve_scenario(Scenario.from_dict(scenario_value))
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "trajectory": value,
                "original": None,
                "refined": False,
                "note": f"refine_error: invalid scenario: {exc}",
                "improved": False,
            }
        original_task = task_text(trajectory)
        scenario = replace(scenario, instruction=original_task)
        diagnosis = self._diagnose(trajectory)
        try:
            repaired = self._generator._run_scenario(
                scenario,
                instruction_content=self._refine_instruction_content(
                    trajectory,
                    diagnosis=diagnosis,
                    task=original_task,
                ),
            )
            repaired = replace(repaired, scenario=scenario)
        except Exception as exc:
            self.logger.error(f"[AgentMMTrajectoryRefiner] refine failed: {exc}")
            return {
                "trajectory": value,
                "original": None,
                "refined": False,
                "note": f"refine_error: {exc}",
                "improved": False,
            }
        improved = repaired.success and not normal_success(trajectory)
        return {
            "trajectory": repaired.to_dict(),
            "original": trajectory,
            "refined": True,
            "note": f"refined: {diagnosis}",
            "improved": improved,
        }

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        output_key: str = "trajectory",
    ):
        if self.llm_serving is None:
            raise ValueError("AgentMMTrajectoryRefiner requires an llm_serving instance")
        dataframe = storage.read(output_type="dataframe")
        if input_key not in dataframe.columns:
            raise KeyError(
                f"input_key {input_key!r} not found in columns: "
                f"{list(dataframe.columns)}"
            )
        scores = (
            dataframe[self.score_key].tolist()
            if self.score_key in dataframe.columns
            else [None] * len(dataframe)
        )
        values = dataframe[input_key].tolist()
        results: list[dict[str, Any] | None] = [None] * len(values)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._refine_one, value, score): index
                for index, (value, score) in enumerate(zip(values, scores))
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = {
                        "trajectory": values[index],
                        "original": None,
                        "refined": False,
                        "note": f"crash: {exc}",
                        "improved": False,
                    }
        complete = [item for item in results if item is not None]
        dataframe[output_key] = [item["trajectory"] for item in complete]
        if self.original_key is not None:
            dataframe[self.original_key] = [item["original"] for item in complete]
        dataframe["_refined"] = [item["refined"] for item in complete]
        dataframe["_refine_note"] = [item["note"] for item in complete]
        storage.write(dataframe)
        return [output_key]
