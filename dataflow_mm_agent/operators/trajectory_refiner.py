"""Official selective trajectory repair adapted to controlled MM rollouts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from ..contracts import Content, ImageContent, Message, TaskResolver, TextContent
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

RESTORED_STATE_GUIDANCE = """

The current artifact has already been restored by replaying the recorded actions.
Treat the latest Judge feedback above as unresolved. Inspect and repair this current
artifact with localized edits; do not recreate or reset it unless it is genuinely
unrecoverable. If a duplicated connector label itself causes a collision while its
destination already states the full branch meaning, removing that redundant label
is a valid localized repair. Save and inspect the result after the last edit before
finishing. In PPTX, if a larger font still renders too small because auto-fit shrinks
it, call manage_shape.update to enlarge the existing text box and/or set
auto_fit=false before applying format_runs. Use manage_shape.delete for incorrect
decorative or duplicate shapes, then inspect shape indexes again because deletion
renumbers later shapes.
"""

RESTORED_STATE_SYSTEM_CONSTRAINT = """HARD CONTINUATION CONSTRAINT:
The existing artifact has already been restored in the live environment. You
must repair that current artifact in place. Never call create_presentation,
create_presentation_from_template, create_presentation_from_templates,
auto_generate_presentation, or any other reset/recreation operation. Inspect
shape indexes, make localized edits, call view_all, save the repaired artifact,
then finish. For PPTX container/geometry defects, use manage_shape.update or
manage_shape.delete rather than layering a replacement deck. This constraint
overrides any impulse to rebuild from scratch.
"""


@OPERATOR_REGISTRY.register()
class AgentMMTrajectoryRefiner(OperatorABC):
    """Repair only failed or below-threshold trajectories by re-exploring."""

    def __init__(
        self,
        llm_serving: ModelServing | None = None,
        max_steps: int = 64,
        max_workers: int = 8,
        task_resolver: TaskResolver | None = None,
        refine_failed: bool = True,
        score_threshold: float | None = 0.6,
        score_key: str = "traj_overall",
        diagnosis_key: str | None = "traj_rationale",
        original_key: str | None = "trajectory_original",
        system_prompt: str | None = None,
        max_prior_chars: int = 2000,
        max_prior_images: int | None = 4,
        max_diagnosis_chars: int = 4000,
        validate_tool_names: bool = True,
        structured_actions: bool = False,
        action_format_retries: int = 0,
        replay_original_prefix: bool = False,
        include_host_tools: bool = True,
        workspace_root: str | Path | None = None,
        workspace_retention: str = "ephemeral",
    ):
        if max_prior_chars < 1:
            raise ValueError("max_prior_chars must be positive")
        if max_prior_images is not None and (
            isinstance(max_prior_images, bool)
            or not isinstance(max_prior_images, int)
            or max_prior_images < 1
        ):
            raise ValueError("max_prior_images must be positive or None")
        if max_diagnosis_chars < 1:
            raise ValueError("max_diagnosis_chars must be positive")
        self.logger = get_logger()
        self.llm_serving = llm_serving
        if task_resolver is None:
            raise ValueError("AgentMMTrajectoryRefiner requires task_resolver")
        self.task_resolver = task_resolver
        self.max_steps = max_steps
        self.max_workers = max_workers
        self.refine_failed = refine_failed
        self.score_threshold = score_threshold
        self.score_key = score_key
        self.diagnosis_key = diagnosis_key
        self.original_key = original_key
        self.system_prompt = system_prompt
        self.max_prior_chars = max_prior_chars
        self.max_prior_images = max_prior_images
        self.max_diagnosis_chars = max_diagnosis_chars
        self.validate_tool_names = validate_tool_names
        self.include_host_tools = include_host_tools
        self.replay_original_prefix = replay_original_prefix
        self._generator = AgentMMExploreGenerator(
            serving=llm_serving,
            task_resolver=task_resolver,
            max_steps=max_steps,
            max_workers=max_workers,
            system_prompt=system_prompt,
            validate_tool_names=validate_tool_names,
            structured_actions=structured_actions,
            action_format_retries=action_format_retries,
            include_host_tools=include_host_tools,
            workspace_root=workspace_root,
            workspace_retention=workspace_retention,
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

    @staticmethod
    def _response_prefix(trajectory: dict[str, Any]) -> tuple[str, ...]:
        """Return executable pre-finish responses from a canonical trajectory."""

        messages = trajectory.get("messages")
        if not isinstance(messages, list):
            raise ValueError("trajectory messages must be a list")
        responses: list[str] = []
        for step in steps(trajectory):
            if step.get("parse_error"):
                continue
            action = step.get("action")
            if not isinstance(action, dict):
                continue
            if action.get("tool") == "finish":
                break
            message_index = step.get("response_message_index")
            if (
                not isinstance(message_index, int)
                or not 0 <= message_index < len(messages)
            ):
                raise ValueError("trajectory step has an invalid response message")
            content = messages[message_index].get("content")
            if not isinstance(content, list):
                raise ValueError("trajectory response content must be a list")
            text = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
            if not text:
                raise ValueError("trajectory response contains no text")
            responses.append(text)
        return tuple(responses)

    def _refine_instruction_messages(
        self,
        trajectory: dict[str, Any],
        *,
        diagnosis: str,
        task: str,
        state_restored: bool = False,
    ) -> tuple[Message, ...]:
        """Build repair context without reclassifying screenshots as task refs.

        Task-authored reference images stay in the original user message.  Prior
        trajectory screenshots use the observation role so serving adapters can
        preserve the task refs first and spend the remaining request budget on
        the newest visual checkpoints, exactly as they do during rollout.
        """

        messages: list[Message] = []
        if state_restored:
            messages.append(Message.text(
                "system",
                RESTORED_STATE_SYSTEM_CONSTRAINT,
                name="trajectory_refiner.restored_state_constraint",
            ))
        messages.append(Message.text(
            "user",
            REFINE_CONTEXT_HEADER.format(
                prior=self._render_prior(trajectory),
                diagnosis=diagnosis,
            ),
            name="trajectory_refiner.diagnosis",
        ))
        # A replay-restored repair already receives the freshly replayed final
        # observation immediately before these continuation messages.  Do not
        # append older trajectory screenshots afterward, or provider-level
        # newest-image capping would evict that higher-value restored state.
        images = [] if state_restored else self._prior_images(trajectory)
        if images:
            visual_context: list[Content] = [TextContent(
                "--- SELECTED PREVIOUS OBSERVATION IMAGES "
                "(chronological order) ---"
            )]
            for step_index, image in images:
                visual_context.extend((
                    TextContent(f"Previous observation image from step {step_index}:"),
                    image,
                ))
            visual_context.append(TextContent(
                "--- END SELECTED PREVIOUS OBSERVATION IMAGES ---"
            ))
            messages.append(Message.of(
                "observation",
                visual_context,
                name="trajectory_refiner.previous_observations",
            ))
        final_instruction: list[Content] = [
            TextContent(REFINE_CONTEXT_FOOTER.format(task=task))
        ]
        if state_restored:
            final_instruction.append(TextContent(RESTORED_STATE_GUIDANCE))
        messages.append(Message.of(
            "user",
            final_instruction,
            name="trajectory_refiner.instruction",
        ))
        return tuple(messages)

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

    def _refine_one(
        self,
        value: Any,
        score: Any,
        judge_diagnosis: Any = None,
        earliest_original: Any = None,
    ) -> dict[str, Any]:
        trajectory = as_trajectory_dict(value)
        if not self._should_refine(trajectory, score):
            return {
                "trajectory": value,
                "original": None,
                "refined": False,
                "note": "kept (passed quality / unparseable)",
                "improved": False,
            }
        try:
            task = self.task_resolver.resolve(
                str(trajectory["task_id"]), env_id=str(trajectory["env_id"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "trajectory": value,
                "original": None,
                "refined": False,
                "note": f"refine_error: cannot resolve task: {exc}",
                "improved": False,
            }
        original_task = task_text(trajectory)
        diagnosis = self._diagnose(trajectory)
        if isinstance(judge_diagnosis, str) and judge_diagnosis.strip():
            feedback = judge_diagnosis.strip()
            if len(feedback) > self.max_diagnosis_chars:
                feedback = feedback[:self.max_diagnosis_chars] + "...[truncated]"
            diagnosis = f"{diagnosis}\nJudge feedback: {feedback}"
        try:
            corrections = self._refine_instruction_messages(
                trajectory,
                diagnosis=diagnosis,
                task=original_task,
                state_restored=self.replay_original_prefix,
            )
            runner = self._generator._runner()
            repaired = (
                runner.run_with_response_prefix(
                    task,
                    self._response_prefix(trajectory),
                    continuation_messages=corrections,
                    compact_live_context=True,
                )
                if self.replay_original_prefix
                else runner.run(replace(
                    task,
                    messages=(*task.messages, *corrections),
                ))
            )
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
            "original": (
                earliest_original
                if as_trajectory_dict(earliest_original) is not None
                else trajectory
            ),
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
        diagnoses = (
            dataframe[self.diagnosis_key].tolist()
            if self.diagnosis_key is not None
            and self.diagnosis_key in dataframe.columns
            else [None] * len(dataframe)
        )
        values = dataframe[input_key].tolist()
        earliest_originals = (
            dataframe[self.original_key].tolist()
            if self.original_key is not None
            and self.original_key in dataframe.columns
            else [None] * len(dataframe)
        )
        results: list[dict[str, Any] | None] = [None] * len(values)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(
                    self._refine_one,
                    value,
                    score,
                    diagnoses[index],
                    earliest_originals[index],
                ): index
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
