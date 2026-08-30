"""Required task identity and optional private scenario contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .agent import Message
from .environment import ReplayVerifier, Scenario


TASK_SCHEMA_VERSION = 2
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"unsafe task_id={task_id!r}")


@dataclass(frozen=True)
class Task:
    """One reusable task definition.

    A task may be rolled out any number of times.  ``messages`` are the full
    task-authored model input and may be empty or multimodal.  ``scenario`` is
    private runtime data and is intentionally excluded from normal
    serialization and trajectory records.
    """

    task_id: str
    env_id: str
    messages: tuple[Message, ...] = ()
    scenario: Scenario | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_task_id(self.task_id)
        if not isinstance(self.env_id, str) or not TASK_ID_PATTERN.fullmatch(self.env_id):
            raise ValueError(f"invalid env_id={self.env_id!r}")
        messages = tuple(self.messages)
        if any(not isinstance(message, Message) for message in messages):
            raise TypeError("task.messages must contain Message values")
        if self.scenario is not None and not isinstance(self.scenario, Scenario):
            raise TypeError("task.scenario must be Scenario or None")
        object.__setattr__(self, "messages", messages)

    def to_dict(self, *, include_scenario_init: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "env_id": self.env_id,
            "messages": [message.to_dict() for message in self.messages],
        }
        if include_scenario_init and self.scenario is not None:
            value["scenario"] = {"init": dict(self.scenario.init)}
        return value

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        scenario: Scenario | None = None,
    ) -> "Task":
        version = value.get("schema_version")
        if version != TASK_SCHEMA_VERSION:
            raise ValueError(f"unsupported task schema_version={version!r}")
        allowed = {"schema_version", "task_id", "env_id", "messages", "scenario"}
        extra = set(value).difference(allowed)
        if extra:
            raise ValueError(f"unsupported task fields: {sorted(extra)}")
        messages = value.get("messages")
        if not isinstance(messages, list):
            raise TypeError("task.messages must be a list")
        task_id = value.get("task_id")
        env_id = value.get("env_id")
        if not isinstance(task_id, str) or not isinstance(env_id, str):
            raise TypeError("task_id and env_id must be strings")
        return cls(
            task_id=task_id,
            env_id=env_id,
            messages=tuple(Message.from_dict(item) for item in messages),
            scenario=scenario,
        )


@runtime_checkable
class TaskResolver(Protocol):
    """Explicit identity resolver used by pipelines and ReplayVerify."""

    def resolve(self, task_id: str, *, env_id: str | None = None) -> Task:
        ...


@runtime_checkable
class ReplayVerifierResolver(Protocol):
    """Resolve a lazy fresh-verifier factory for one Task identity."""

    def verifier_factory(
        self,
        task_id: str,
        *,
        env_id: str,
    ) -> "ReplayVerifierFactory | None":
        ...


ReplayVerifierFactory = Callable[[], ReplayVerifier]


__all__ = [
    "TASK_ID_PATTERN",
    "TASK_SCHEMA_VERSION",
    "Task",
    "TaskResolver",
    "ReplayVerifierFactory",
    "ReplayVerifierResolver",
    "validate_task_id",
]
