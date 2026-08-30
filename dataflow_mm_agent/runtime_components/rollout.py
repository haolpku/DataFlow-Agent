"""Multimodal JSON-action rollouts over lightweight environments."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

from ..contracts import (
    ContentLimits,
    EnvironmentSpec,
    ImageContent,
    Message,
    Task,
    ToolResult,
    ToolSpec,
    close_env,
    start_env,
    validate_content,
)
from ..contracts.trajectory import EpisodeStep, Trajectory, utc_now
from ..env.registry import get_environment_spec, make_env
from ..serving import ModelServing
from .host import HostPolicy
from .tool_loop import ToolLoop


_SYSTEM_PROMPT = """You are an autonomous multimodal agent solving a task in a controlled environment.

Environment:
{environment_context}

Available tools:
{tool_catalog}

At every step respond with exactly one JSON object:
{{"thought":"brief reasoning","tool":"tool name","args":{{...}}}}

When the task is complete, use:
{{"thought":"why complete","tool":"finish","args":{{"answer":"final answer"}}}}

Use only listed tools or finish. Images returned by tools are visible in the
observation message where they appear. Do not invent paths or inspect hidden
environment state. Follow the language requested by the task messages.
"""


EnvResolver = Callable[[str], Any]
SpecResolver = Callable[[str], EnvironmentSpec]
ResponseProvider = Callable[
    [Sequence[Message], Mapping[str, Any] | None],
    tuple[str, float] | None,
]
WorkspaceRetention = Literal["ephemeral", "full"]


@dataclass(frozen=True)
class RolloutConfig:
    max_steps: int = 64
    system_prompt: str = _SYSTEM_PROMPT
    include_host_tools: bool = True
    include_tool_catalog: bool = True
    validate_tool_names: bool = True
    structured_actions: bool = False
    action_format_retries: int = 0
    max_observation_chars: int = 8000
    workspace_root: Path | None = None
    workspace_retention: WorkspaceRetention = "ephemeral"
    content_limits: ContentLimits = ContentLimits()

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps < 1
        ):
            raise ValueError("max_steps must be a positive integer")
        if self.max_observation_chars < 1:
            raise ValueError("max_observation_chars must be positive")
        if (
            isinstance(self.action_format_retries, bool)
            or not isinstance(self.action_format_retries, int)
            or self.action_format_retries < 0
        ):
            raise ValueError("action_format_retries must be a non-negative integer")
        if self.workspace_retention not in ("ephemeral", "full"):
            raise ValueError("workspace_retention must be 'ephemeral' or 'full'")
        if self.workspace_retention == "full" and self.workspace_root is None:
            raise ValueError(
                "workspace_root is required when workspace_retention='full'"
            )


class AgentRollout:
    """Run one required Task; verification is intentionally out of scope."""

    def __init__(
        self,
        *,
        serving: ModelServing,
        config: RolloutConfig = RolloutConfig(),
        env_resolver: EnvResolver = make_env,
        spec_resolver: SpecResolver = get_environment_spec,
    ):
        self.serving = serving
        self.config = config
        self.env_resolver = env_resolver
        self.spec_resolver = spec_resolver

    parse_action = staticmethod(ToolLoop.parse_action)

    @staticmethod
    def _action_request_options(tools: Sequence[ToolSpec]) -> dict[str, Any]:
        branches = [{
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "tool": {"type": "string", "const": tool.name},
                "args": json.loads(json.dumps(tool.input_schema)),
            },
            "required": ["thought", "tool", "args"],
            "additionalProperties": False,
        } for tool in tools]
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_action",
                    "strict": True,
                    "schema": {"type": "object", "oneOf": branches},
                },
            },
        }

    def sample_responses(
        self,
        messages: Sequence[Message],
        count: int,
        *,
        request_options: Mapping[str, Any] | None = None,
    ) -> list[str]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        conversations = [tuple(messages) for _ in range(count)]
        responses = (
            self.serving.generate_messages_with_options(
                conversations, [request_options for _ in range(count)]
            )
            if request_options is not None
            else self.serving.generate_messages(conversations)
        )
        if len(responses) != count:
            raise RuntimeError(
                f"serving returned {len(responses)} responses for {count} requests"
            )
        return responses

    @contextmanager
    def _episode_workspace(self, *, env_id: str, episode_id: str) -> Iterator[Path]:
        root = self.config.workspace_root
        if self.config.workspace_retention == "full":
            assert root is not None
            workspace = (Path(root).resolve() / env_id / episode_id).resolve()
            workspace.mkdir(parents=True, exist_ok=False)
            yield workspace
            return
        if root is not None:
            Path(root).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"agent-mm-{env_id}-", dir=str(root) if root else None
        ) as temporary:
            yield Path(temporary).resolve()

    def run(self, task: Task) -> Trajectory:
        def live_response(
            messages: Sequence[Message],
            request_options: Mapping[str, Any] | None,
        ) -> tuple[str, float]:
            before = time.perf_counter()
            response = self.sample_responses(
                messages, 1, request_options=request_options
            )[0]
            return response, (time.perf_counter() - before) * 1000

        return self._run_with_response_provider(
            task,
            live_response,
            exhaustion_reason="max_steps",
            step_limit=self.config.max_steps,
        )

    def run_responses(
        self,
        task: Task,
        responses: Sequence[str],
        *,
        exhaustion_reason: str = "responses_exhausted",
    ) -> Trajectory:
        fixed = tuple(responses)
        if any(not isinstance(item, str) for item in fixed):
            raise TypeError("responses must contain only strings")
        iterator = iter(fixed)

        def fixed_response(
            _messages: Sequence[Message],
            _request_options: Mapping[str, Any] | None,
        ) -> tuple[str, float] | None:
            try:
                return next(iterator), 0.0
            except StopIteration:
                return None

        return self._run_with_response_provider(
            task,
            fixed_response,
            exhaustion_reason=exhaustion_reason,
            step_limit=len(fixed),
        )

    def _run_with_response_provider(
        self,
        task: Task,
        response_provider: ResponseProvider,
        *,
        exhaustion_reason: str,
        step_limit: int,
    ) -> Trajectory:
        if not isinstance(task, Task):
            raise TypeError("AgentRollout.run requires a Task")
        for message in task.messages:
            validate_content(message.content, self.config.content_limits)
        started_at = utc_now()
        episode_id = f"{task.task_id}-{uuid.uuid4().hex[:12]}"

        with self._episode_workspace(env_id=task.env_id, episode_id=episode_id) as workspace:
            env = self.env_resolver(task.env_id)
            spec = self.spec_resolver(task.env_id)
            if spec.env_id != task.env_id:
                close_env(env)
                raise ValueError("environment description does not match task.env_id")
            messages: list[Message] = []
            steps: list[EpisodeStep] = []
            final_answer: str | None = None
            termination_reason = exhaustion_reason
            format_retries_used = 0
            format_retries_recovered = 0
            tool_names: tuple[str, ...] = ()

            try:
                host_policy = HostPolicy(
                    workspace=workspace,
                    content_limits=self.config.content_limits,
                ) if self.config.include_host_tools else None
                loop = ToolLoop(
                    env,
                    host_policy=host_policy,
                    validate_tool_names=self.config.validate_tool_names,
                    content_limits=self.config.content_limits,
                    max_observation_chars=self.config.max_observation_chars,
                )
                tool_names = loop.tool_names
                request_options = (
                    self._action_request_options(loop.tools)
                    if self.config.structured_actions
                    else None
                )
                catalog = (
                    ToolLoop.catalog(loop.tools)
                    if self.config.include_tool_catalog
                    else "(tools are described by the caller)"
                )
                messages.append(Message.text(
                    "system",
                    self.config.system_prompt.format(
                        tool_catalog=catalog,
                        environment_context=json.dumps(
                            spec.solver_context(), ensure_ascii=False, indent=2
                        ),
                    ),
                ))
                messages.extend(task.messages)

                initial = start_env(
                    env,
                    task.scenario.init if task.scenario is not None else None,
                    workspace,
                )
                if initial is not None:
                    initial = loop.safe_result(initial)
                    if initial.content or not initial.ok:
                        messages.append(loop.observation(initial, "env.start"))
                    if not initial.ok:
                        code = initial.error.code if initial.error is not None else "unknown"
                        raise RuntimeError(f"Env.start failed with {code}")
                    if initial.is_final:
                        raise RuntimeError("Env.start must not terminate an episode")

                for step_index in range(1, step_limit + 1):
                    generated = response_provider(tuple(messages), request_options)
                    if generated is None:
                        break
                    response, elapsed_ms = generated
                    action = loop.parse_action(response)
                    retry_context = list(messages)
                    retries_this_step = 0
                    while (
                        action is None
                        and retries_this_step < self.config.action_format_retries
                    ):
                        retries_this_step += 1
                        format_retries_used += 1
                        retry_context.extend([
                            Message.text("assistant", response),
                            loop.observation(loop.parse_failure(), "agent.parse"),
                        ])
                        retried = response_provider(tuple(retry_context), request_options)
                        if retried is None:
                            break
                        response, retry_elapsed = retried
                        elapsed_ms += retry_elapsed
                        action = loop.parse_action(response)
                    if retries_this_step and action is not None:
                        format_retries_recovered += 1

                    messages.append(Message.text("assistant", response))
                    response_index = len(messages) - 1
                    if action is None:
                        result = loop.parse_failure()
                        messages.append(loop.observation(result, "agent.parse"))
                        steps.append(EpisodeStep(
                            index=step_index,
                            response_message_index=response_index,
                            action=None,
                            observation_message_index=len(messages) - 1,
                            parse_error=True,
                            elapsed_ms=elapsed_ms,
                            tool_ok=False,
                            error_code=result.error.code if result.error else None,
                            retryable=result.error.retryable if result.error else None,
                        ))
                        continue

                    execution = loop.execute(action)
                    observation_index = None
                    if execution.emit_observation:
                        messages.append(loop.observation(
                            execution.result, str(action.get("tool") or "agent.action")
                        ))
                        observation_index = len(messages) - 1
                    steps.append(EpisodeStep(
                        index=step_index,
                        response_message_index=response_index,
                        action=dict(action),
                        observation_message_index=observation_index,
                        elapsed_ms=elapsed_ms,
                        tool_ok=execution.result.ok,
                        error_code=(
                            execution.result.error.code
                            if execution.result.error is not None
                            else None
                        ),
                        retryable=(
                            execution.result.error.retryable
                            if execution.result.error is not None
                            else None
                        ),
                        is_final=execution.result.is_final,
                    ))
                    if execution.terminal_kind is not None:
                        termination_reason = execution.terminal_kind
                        final_answer = execution.final_answer
                        break
            except Exception as exc:
                termination_reason = "infrastructure_error"
                messages.append(Message.text(
                    "observation",
                    f"FATAL [{type(exc).__name__}]: {exc}",
                    name="runtime",
                ))
            finally:
                try:
                    close_env(env)
                except Exception as exc:
                    if termination_reason != "infrastructure_error":
                        termination_reason = "infrastructure_error"
                        messages.append(Message.text(
                            "observation",
                            f"FATAL [close {type(exc).__name__}]: {exc}",
                            name="runtime",
                        ))

            return Trajectory(
                episode_id=episode_id,
                task_id=task.task_id,
                env_id=task.env_id,
                messages=tuple(messages),
                steps=tuple(steps),
                final_answer=final_answer,
                termination_reason=termination_reason,
                started_at=started_at,
                completed_at=utc_now(),
                metadata={
                    "tools": list(tool_names),
                    "max_steps": step_limit,
                    "workspace_retention": self.config.workspace_retention,
                    **(
                        {"workspace": str(workspace)}
                        if self.config.workspace_retention == "full"
                        else {}
                    ),
                    "task_image_count": sum(
                        isinstance(item, ImageContent)
                        for message in task.messages
                        for item in message.content
                    ),
                    "structured_actions": self.config.structured_actions,
                    "action_format_retries": self.config.action_format_retries,
                    "format_retries_used": format_retries_used,
                    "format_retries_recovered": format_retries_recovered,
                    "host_tools": self.config.include_host_tools,
                    "validate_tool_names": self.config.validate_tool_names,
                },
            )

    def run_batch(self, tasks: Sequence[Task], *, max_workers: int = 1) -> list[Trajectory]:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_workers == 1:
            return [self.run(task) for task in tasks]
        results: list[Trajectory | None] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.run, task): index
                for index, task in enumerate(tasks)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [result for result in results if result is not None]


__all__ = ["AgentRollout", "RolloutConfig"]
