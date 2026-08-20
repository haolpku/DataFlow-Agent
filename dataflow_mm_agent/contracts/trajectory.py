"""Canonical episode trajectory data contract."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from .agent import Message
from .environment import Scenario, VerificationResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EpisodeStep:
    index: int
    response_message_index: int
    action: Mapping[str, Any] | None
    observation_message_index: int | None = None
    parse_error: bool = False
    elapsed_ms: float | None = None
    tool_ok: bool | None = None
    error_code: str | None = None
    retryable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "response_message_index": self.response_message_index,
            "action": dict(self.action) if self.action is not None else None,
            "observation_message_index": self.observation_message_index,
            "parse_error": self.parse_error,
            "elapsed_ms": self.elapsed_ms,
            "tool_ok": self.tool_ok,
            "error_code": self.error_code,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EpisodeStep":
        action = value["action"]
        if action is not None and not isinstance(action, Mapping):
            raise TypeError("action must be a mapping or null")
        parse_error = value["parse_error"]
        if not isinstance(parse_error, bool):
            raise TypeError("parse_error must be a boolean")
        tool_ok = value["tool_ok"]
        if tool_ok is not None and not isinstance(tool_ok, bool):
            raise TypeError("tool_ok must be a boolean or null")
        retryable = value["retryable"]
        if retryable is not None and not isinstance(retryable, bool):
            raise TypeError("retryable must be a boolean or null")
        return cls(
            index=int(value["index"]),
            response_message_index=int(value["response_message_index"]),
            action=dict(action) if action is not None else None,
            observation_message_index=(
                int(value["observation_message_index"])
                if value["observation_message_index"] is not None
                else None
            ),
            parse_error=parse_error,
            elapsed_ms=(
                float(value["elapsed_ms"])
                if value["elapsed_ms"] is not None
                else None
            ),
            tool_ok=tool_ok,
            error_code=(
                str(value["error_code"])
                if value["error_code"] is not None
                else None
            ),
            retryable=retryable,
        )


@dataclass(frozen=True)
class Trajectory:
    episode_id: str
    scenario: Scenario
    messages: tuple[Message, ...]
    steps: tuple[EpisodeStep, ...]
    final_answer: str | None
    termination_reason: str
    started_at: str
    completed_at: str
    verification: VerificationResult | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    @property
    def success(self) -> bool:
        """Whether the agent completed normally, independent of verification."""
        return self.termination_reason in {"finish", "environment_final"}

    @property
    def verified_success(self) -> bool:
        return bool(self.verification and self.verification.passed)

    def with_verification(self, result: VerificationResult) -> "Trajectory":
        return replace(self, verification=result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "scenario": self.scenario.to_dict(include_private=False),
            "messages": [message.to_dict() for message in self.messages],
            "steps": [step.to_dict() for step in self.steps],
            "final_answer": self.final_answer,
            "termination_reason": self.termination_reason,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "success": self.success,
            "num_steps": len(self.steps),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Trajectory":
        schema_version = value["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TypeError("schema_version must be an integer")
        if schema_version != 1:
            raise ValueError(f"unsupported trajectory schema_version={schema_version}")
        success = value["success"]
        if not isinstance(success, bool):
            raise TypeError("success must be a boolean")
        num_steps = value["num_steps"]
        if isinstance(num_steps, bool) or not isinstance(num_steps, int):
            raise TypeError("num_steps must be an integer")
        verification = value["verification"]
        trajectory = cls(
            schema_version=schema_version,
            episode_id=str(value["episode_id"]),
            scenario=Scenario.from_dict(value["scenario"]),
            messages=tuple(Message.from_dict(item) for item in value["messages"]),
            steps=tuple(EpisodeStep.from_dict(item) for item in value["steps"]),
            final_answer=(
                str(value["final_answer"])
                if value["final_answer"] is not None
                else None
            ),
            termination_reason=str(value["termination_reason"]),
            started_at=str(value["started_at"]),
            completed_at=str(value["completed_at"]),
            verification=(
                VerificationResult.from_dict(verification)
                if verification is not None
                else None
            ),
            metadata=dict(value["metadata"]),
        )
        if success != trajectory.success:
            raise ValueError("success does not match termination_reason")
        if num_steps != len(trajectory.steps):
            raise ValueError("num_steps does not match steps")
        return trajectory


__all__ = ["EpisodeStep", "Trajectory", "utc_now"]
