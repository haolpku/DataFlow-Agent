"""Multimodal JSON-action agent loop over controlled Python environments."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..contracts import (
    Content,
    ContentLimits,
    ImageContent,
    Message,
    TextContent,
    ToolResult,
    ToolSpec,
    validate_content,
)
from ..contracts.environment import Env, Scenario, Verifier
from ..contracts.trajectory import EpisodeStep, Trajectory, utc_now
from .finish import FINISH_TOOL_SPEC
from .host import HostPolicy, HostTools
from ..serving import ModelServing


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
environment state. Follow the language requested by the task instruction. If
the task requests Chinese, write the thought and final answer in Chinese while
keeping tool names, argument keys, and enum values exactly as defined. In
particular, the finish tool's answer argument must contain Chinese text.
"""


EnvFactory = Callable[[], Env]
ResponseProvider = Callable[
    [Sequence[Message]],
    tuple[str, float] | None,
]


@dataclass(frozen=True)
class RolloutConfig:
    max_steps: int | None = None
    system_prompt: str = _SYSTEM_PROMPT
    include_host_tools: bool = True
    include_tool_catalog: bool = True
    validate_tool_names: bool = True
    max_observation_chars: int = 8000
    workspace_root: Path | None = None
    content_limits: ContentLimits = ContentLimits()

    def __post_init__(self) -> None:
        if self.max_steps is not None and (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps < 1
        ):
            raise ValueError("max_steps must be positive")
        if self.max_observation_chars < 1:
            raise ValueError("max_observation_chars must be positive")


class AgentRollout:
    def __init__(
        self,
        *,
        serving: ModelServing,
        env_factory: EnvFactory,
        verifier: Verifier | None = None,
        config: RolloutConfig = RolloutConfig(),
    ):
        self.serving = serving
        self.env_factory = env_factory
        self.verifier = verifier
        self.config = config

    @staticmethod
    def parse_action(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        candidates: list[str] = []
        start: int | None = None
        depth = 0
        in_string = False
        escaped = False
        for index, character in enumerate(text):
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
                if depth == 0:
                    start = index
                depth += 1
            elif character == "}" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:index + 1])
                    start = None

        # Thinking models commonly quote an illustrative or previous action in
        # their reasoning before emitting the actual action after </think>.
        # The last syntactically and structurally valid object is therefore the
        # intended action; earlier examples must not become environment calls.
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            tool = value.get("tool")
            args = value.get("args", {})
            if not isinstance(tool, str) or not isinstance(args, dict):
                continue
            value["args"] = args
            return value
        return None

    @staticmethod
    def _tool_catalog(tools: Sequence[ToolSpec]) -> str:
        return json.dumps(
            [tool.to_dict() for tool in tools],
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _validate_args(tool: ToolSpec, args: Mapping[str, Any]) -> str | None:
        try:
            import jsonschema

            jsonschema.validate(instance=dict(args), schema=dict(tool.input_schema))
        except ImportError:
            required = tool.input_schema.get("required") or []
            missing = [name for name in required if name not in args]
            return f"missing required arguments: {missing}" if missing else None
        except Exception as exc:
            return str(exc).splitlines()[0]
        return None

    def _safe_result(self, result: ToolResult) -> ToolResult:
        try:
            validate_content(result.content, self.config.content_limits)
            content = []
            for item in result.content:
                if (
                    isinstance(item, TextContent)
                    and len(item.text) > self.config.max_observation_chars
                ):
                    omitted = len(item.text) - self.config.max_observation_chars
                    content.append(TextContent(
                        item.text[:self.config.max_observation_chars]
                        + f"\n...[truncated {omitted} chars of {len(item.text)} total]"
                    ))
                else:
                    content.append(item)
            return ToolResult(
                ok=result.ok,
                content=tuple(content),
                error=result.error,
                is_final=result.is_final,
                metadata=result.metadata,
            )
        except ValueError as exc:
            return ToolResult.failure("invalid_content", str(exc))

    @staticmethod
    def _observation(result: ToolResult, tool_name: str) -> Message:
        content = list(result.content)
        if result.error is not None:
            content.insert(0, TextContent(
                f"ERROR [{result.error.code}]: {result.error.message}"
            ))
        if not content:
            content.append(TextContent("OK" if result.ok else "ERROR"))
        return Message.of("observation", content, name=tool_name)

    def sample_responses(
        self,
        messages: Sequence[Message],
        count: int,
    ) -> list[str]:
        """Sample several next responses from one canonical message history."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        conversation = tuple(messages)
        responses = self.serving.generate_messages(
            [conversation for _ in range(count)]
        )
        if len(responses) != count:
            raise RuntimeError(
                f"serving returned {len(responses)} responses for {count} requests"
            )
        return responses

    def run(
        self,
        scenario: Scenario,
        *,
        instruction_content: Sequence[Content] | None = None,
    ) -> Trajectory:
        """Run one live model-driven episode."""

        def live_response(messages: Sequence[Message]) -> tuple[str, float]:
            before = time.perf_counter()
            response = self.sample_responses(messages, 1)[0]
            return response, (time.perf_counter() - before) * 1000

        return self._run_with_response_provider(
            scenario,
            live_response,
            exhaustion_reason="max_steps",
            instruction_content=instruction_content,
        )

    def run_responses(
        self,
        scenario: Scenario,
        responses: Sequence[str],
        *,
        exhaustion_reason: str = "max_steps",
    ) -> Trajectory:
        """Replay a fixed response prefix in a fresh Env.

        This uses exactly the same parsing, argument validation, tool routing,
        observation construction, termination, and optional verifier path as a
        live rollout. Tree exploration uses it to reconstruct every branch from
        the task's deterministic reset state instead of mutating one shared Env
        across sibling nodes.
        """
        fixed = tuple(responses)
        if any(not isinstance(item, str) for item in fixed):
            raise TypeError("responses must contain only strings")
        if not isinstance(exhaustion_reason, str) or not exhaustion_reason:
            raise ValueError("exhaustion_reason must be a non-empty string")
        iterator = iter(fixed)

        def fixed_response(
            _messages: Sequence[Message],
        ) -> tuple[str, float] | None:
            try:
                return next(iterator), 0.0
            except StopIteration:
                return None

        return self._run_with_response_provider(
            scenario,
            fixed_response,
            exhaustion_reason=exhaustion_reason,
            step_limit=len(fixed),
        )

    def _run_with_response_provider(
        self,
        scenario: Scenario,
        response_provider: ResponseProvider,
        *,
        exhaustion_reason: str,
        step_limit: int | None = None,
        instruction_content: Sequence[Content] | None = None,
    ) -> Trajectory:
        prepared_instruction = (
            tuple(instruction_content) if instruction_content is not None else None
        )
        if prepared_instruction is not None:
            if not prepared_instruction:
                raise ValueError("instruction_content must not be empty")
            validate_content(prepared_instruction, self.config.content_limits)
        started_at = utc_now()
        episode_id = f"{scenario.task_id}-{uuid.uuid4().hex[:12]}"
        root = self.config.workspace_root
        if root is not None:
            Path(root).mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix=f"agent-mm-{scenario.env_id}-",
            dir=str(root) if root else None,
        ) as temporary:
            workspace = Path(temporary).resolve()
            env = self.env_factory()
            env_spec = getattr(env, "spec", None)
            if (
                getattr(env, "env_id", None) != scenario.env_id
                or env_spec is None
                or env_spec.env_id != scenario.env_id
            ):
                env.close()
                raise ValueError(
                    "environment factory/spec does not match scenario env_id"
                )
            max_steps = (
                step_limit
                if step_limit is not None
                else (
                    self.config.max_steps
                    or scenario.episode_max_steps
                    or env_spec.default_max_steps
                )
            )
            host = HostTools(HostPolicy(
                workspace=workspace,
                content_limits=self.config.content_limits,
            )) if self.config.include_host_tools else None
            messages: list[Message] = []
            steps: list[EpisodeStep] = []
            final_answer: str | None = None
            termination_reason = exhaustion_reason
            snapshot: Mapping[str, Any] | None = None

            try:
                env_tools = tuple(env.tools())
                host_tools = tuple(host.tools()) if host else ()
                runtime_tools = (FINISH_TOOL_SPEC,)
                tools = env_tools + host_tools + runtime_tools
                names = [tool.name for tool in tools]
                if len(set(names)) != len(names):
                    raise ValueError(
                        "environment, host, and runtime tool names must be unique"
                    )
                tool_map = {tool.name: tool for tool in tools}
                catalog = (
                    self._tool_catalog(tools)
                    if self.config.include_tool_catalog
                    else "(tools are described by the caller)"
                )
                environment_context = json.dumps(
                    env_spec.solver_context(),
                    ensure_ascii=False,
                    indent=2,
                )
                system_prompt = self.config.system_prompt.format(
                    tool_catalog=catalog,
                    environment_context=environment_context,
                )
                instruction_message = (
                    Message.of("user", prepared_instruction)
                    if prepared_instruction is not None
                    else Message.text("user", scenario.instruction)
                )
                messages.extend([
                    Message.text("system", system_prompt),
                    instruction_message,
                ])
                initial = self._safe_result(env.reset(scenario, workspace))
                if initial.content or not initial.ok:
                    messages.append(self._observation(initial, "env.reset"))

                for step_index in range(1, max_steps + 1):
                    generated = response_provider(tuple(messages))
                    if generated is None:
                        termination_reason = exhaustion_reason
                        break
                    response, elapsed_ms = generated
                    messages.append(Message.text("assistant", response))
                    response_index = len(messages) - 1
                    action = self.parse_action(response)
                    if action is None:
                        result = ToolResult.failure(
                            "invalid_action",
                            "response must contain one JSON action object",
                        )
                        messages.append(self._observation(result, "agent.parse"))
                        steps.append(EpisodeStep(
                            index=step_index,
                            response_message_index=response_index,
                            action=None,
                            observation_message_index=len(messages) - 1,
                            parse_error=True,
                            elapsed_ms=elapsed_ms,
                            tool_ok=False,
                            error_code=result.error.code,
                            retryable=result.error.retryable,
                        ))
                        continue

                    tool_name = action["tool"]
                    args = action["args"]
                    if tool_name == "finish":
                        validation_error = self._validate_args(FINISH_TOOL_SPEC, args)
                        if validation_error:
                            result = self._safe_result(ToolResult.failure(
                                "invalid_arguments", validation_error
                            ))
                            messages.append(self._observation(result, tool_name))
                            steps.append(EpisodeStep(
                                index=step_index,
                                response_message_index=response_index,
                                action=action,
                                observation_message_index=len(messages) - 1,
                                elapsed_ms=elapsed_ms,
                                tool_ok=False,
                                error_code=result.error.code,
                                retryable=result.error.retryable,
                            ))
                            continue
                        final_answer = str(args["answer"])
                        termination_reason = "finish"
                        steps.append(EpisodeStep(
                            index=step_index,
                            response_message_index=response_index,
                            action=action,
                            elapsed_ms=elapsed_ms,
                            tool_ok=True,
                        ))
                        break

                    spec = tool_map.get(tool_name)
                    if spec is None and self.config.validate_tool_names:
                        result = ToolResult.failure(
                            "unknown_tool",
                            f"unknown tool {tool_name!r}; available: {sorted(tool_map)}",
                        )
                    elif spec is None:
                        try:
                            result = env.call(tool_name, args)
                        except Exception as exc:
                            result = ToolResult.failure(
                                "environment_error",
                                f"{type(exc).__name__}: {exc}",
                                retryable=False,
                            )
                    else:
                        validation_error = self._validate_args(spec, args)
                        if validation_error:
                            result = ToolResult.failure(
                                "invalid_arguments", validation_error
                            )
                        else:
                            try:
                                result = (
                                    host.call(tool_name, args)
                                    if host and tool_name.startswith("host.")
                                    else env.call(tool_name, args)
                                )
                            except Exception as exc:
                                result = ToolResult.failure(
                                    "environment_error",
                                    f"{type(exc).__name__}: {exc}",
                                    retryable=False,
                                )
                    result = self._safe_result(result)
                    messages.append(self._observation(result, tool_name))
                    steps.append(EpisodeStep(
                        index=step_index,
                        response_message_index=response_index,
                        action=action,
                        observation_message_index=len(messages) - 1,
                        elapsed_ms=elapsed_ms,
                        tool_ok=result.ok,
                        error_code=result.error.code if result.error else None,
                        retryable=result.error.retryable if result.error else None,
                    ))
                    if result.is_final:
                        termination_reason = "environment_final"
                        break
            except Exception as exc:
                termination_reason = "infrastructure_error"
                messages.append(Message.text(
                    "observation",
                    f"FATAL [{type(exc).__name__}]: {exc}",
                    name="runtime",
                ))
            finally:
                if self.verifier is not None:
                    try:
                        snapshot = dict(env.snapshot())
                        env_spec.validate_snapshot(snapshot)
                    except Exception as exc:
                        snapshot = {
                            "snapshot_error": f"{type(exc).__name__}: {exc}"
                        }
                env.close()

            trajectory = Trajectory(
                episode_id=episode_id,
                scenario=scenario,
                messages=tuple(messages),
                steps=tuple(steps),
                final_answer=final_answer,
                termination_reason=termination_reason,
                started_at=started_at,
                completed_at=utc_now(),
                metadata={
                    "tools": names if "names" in locals() else [],
                    "max_steps": max_steps,
                    "instruction_image_count": sum(
                        isinstance(item, ImageContent)
                        for item in (prepared_instruction or ())
                    ),
                },
            )
            if self.verifier is None:
                return trajectory
            verification = self.verifier.verify(scenario, trajectory, snapshot or {})
            return trajectory.with_verification(verification)

    def run_batch(
        self,
        scenarios: Sequence[Scenario],
        *,
        max_workers: int = 1,
    ) -> list[Trajectory]:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_workers == 1:
            return [self.run(scenario) for scenario in scenarios]
        results: list[Trajectory | None] = [None] * len(scenarios)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.run, scenario): index
                for index, scenario in enumerate(scenarios)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [result for result in results if result is not None]
