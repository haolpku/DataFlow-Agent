"""Controlled Python environment, scenario, and verifier contracts."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence, TYPE_CHECKING

import jsonschema

from .agent import ToolResult, ToolSpec

if TYPE_CHECKING:
    from .trajectory import Trajectory


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_MODALITIES = {"text", "image"}


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain only JSON values") from exc
    if not isinstance(copied, dict):
        raise TypeError(f"{field_name} must be a JSON object")
    return copied


@dataclass(frozen=True)
class RuleSpec:
    """One environment-wide rule that applies to every task."""

    rule_id: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not _IDENTIFIER.fullmatch(self.rule_id):
            raise ValueError(f"invalid rule_id={self.rule_id!r}")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("rule description must be non-empty")

    def to_dict(self) -> dict[str, str]:
        return {"rule_id": self.rule_id, "description": self.description}


@dataclass(frozen=True)
class EnvironmentSpec:
    """Static, task-independent definition of one environment.

    Tool definitions intentionally do not live here. ``Env.tools()`` remains
    the single source of truth for callable operations.
    """

    env_id: str
    name: str
    description: str
    rules: tuple[RuleSpec, ...]
    init_schema: Mapping[str, Any]
    state_schema: Mapping[str, Any]
    modalities: tuple[str, ...]
    default_max_steps: int
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.env_id, str) or not _IDENTIFIER.fullmatch(self.env_id):
            raise ValueError(f"invalid env_id={self.env_id!r}")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("environment name must be non-empty")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("environment description must be non-empty")
        rules = tuple(self.rules)
        if not rules or any(not isinstance(rule, RuleSpec) for rule in rules):
            raise ValueError("environment rules must be non-empty")
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("environment rule_id values must be unique")
        modalities = tuple(self.modalities)
        if not modalities or any(
            not isinstance(item, str) or item not in _MODALITIES
            for item in modalities
        ):
            raise ValueError(
                f"modalities must be a non-empty subset of {sorted(_MODALITIES)}"
            )
        if len(set(modalities)) != len(modalities):
            raise ValueError("environment modalities must be unique")
        if (
            isinstance(self.default_max_steps, bool)
            or not isinstance(self.default_max_steps, int)
            or self.default_max_steps < 1
        ):
            raise ValueError("default_max_steps must be a positive integer")
        tags = tuple(self.tags)
        if (
            any(not isinstance(item, str) or not item.strip() for item in tags)
            or len(set(tags)) != len(tags)
        ):
            raise ValueError("environment tags must be non-empty and unique")

        init_schema = _json_object(self.init_schema, "init_schema")
        state_schema = _json_object(self.state_schema, "state_schema")
        metadata = _json_object(self.metadata, "metadata")
        for field_name, schema in (
            ("init_schema", init_schema),
            ("state_schema", state_schema),
        ):
            if schema.get("type") != "object":
                raise ValueError(f"{field_name} must describe a JSON object")
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as exc:
                raise ValueError(f"invalid {field_name}: {exc.message}") from exc

        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "modalities", modalities)
        object.__setattr__(self, "tags", tags)
        object.__setattr__(self, "init_schema", init_schema)
        object.__setattr__(self, "state_schema", state_schema)
        object.__setattr__(self, "metadata", metadata)

    def validate_init_config(self, value: Mapping[str, Any]) -> None:
        """Raise ``ValueError`` when a task cannot initialize this Env."""
        try:
            jsonschema.validate(instance=dict(value), schema=dict(self.init_schema))
        except (TypeError, jsonschema.ValidationError) as exc:
            message = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
            raise ValueError(f"invalid init_config for {self.env_id}: {message}") from exc

    def validate_snapshot(self, value: Mapping[str, Any]) -> None:
        """Raise ``ValueError`` when an Env snapshot violates its public spec."""
        try:
            jsonschema.validate(instance=dict(value), schema=dict(self.state_schema))
        except (TypeError, jsonschema.ValidationError) as exc:
            message = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
            raise ValueError(f"invalid snapshot for {self.env_id}: {message}") from exc

    def solver_context(self) -> dict[str, Any]:
        """Return only stable environment documentation visible to a solver."""
        return {
            "env_id": self.env_id,
            "name": self.name,
            "description": self.description,
            "rules": [rule.to_dict() for rule in self.rules],
            "modalities": list(self.modalities),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.solver_context(),
            "init_schema": _json_object(self.init_schema, "init_schema"),
            "state_schema": _json_object(self.state_schema, "state_schema"),
            "default_max_steps": self.default_max_steps,
            "tags": list(self.tags),
            "metadata": _json_object(self.metadata, "metadata"),
        }


@dataclass(frozen=True)
class Scenario:
    task_id: str
    env_id: str
    seed: int
    instruction: str
    public_config: Mapping[str, Any] = field(default_factory=dict)
    private_config: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def episode_config(self) -> Mapping[str, Any]:
        value = self.private_config.get("episode_config")
        return value if isinstance(value, Mapping) else {}

    @property
    def episode_max_steps(self) -> int | None:
        value = self.episode_config.get("max_steps")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        value = {
            "task_id": self.task_id,
            "env_id": self.env_id,
            "seed": self.seed,
            "instruction": self.instruction,
            "public_config": dict(self.public_config),
        }
        if include_private:
            value["private_config"] = dict(self.private_config)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Scenario":
        """Load a persisted public Scenario reference.

        Private task state is intentionally ignored here. It must be resolved
        by the owning Env pack rather than trusted from pipeline JSONL.
        """
        return cls(
            task_id=str(value["task_id"]),
            env_id=str(value["env_id"]),
            seed=int(value["seed"]),
            instruction=str(value["instruction"]),
            public_config=dict(value["public_config"]),
        )


class TaskProvider(ABC):
    """Load materialized tasks and resolve their private runtime fields."""

    env_id: str

    @abstractmethod
    def load_task(self, task_id: str) -> Scenario:
        raise NotImplementedError

    @abstractmethod
    def resolve(self, reference: Scenario) -> Scenario:
        raise NotImplementedError

    @abstractmethod
    def author_references(
        self,
        scenario: Scenario,
    ) -> Mapping[str, Mapping[str, Any]]:
        raise NotImplementedError


class ScenarioGenerator(TaskProvider):
    """Optional build-time task generator retained for generated Env packs."""

    @abstractmethod
    def generate(self, seed: int) -> Scenario:
        raise NotImplementedError

    def generate_task(self, seed: int, task_name: str | None = None) -> Scenario:
        """Generate one explicitly selected task family.

        ``None`` selects the Env pack's default family.
        Packs exposing multiple families override this method and persist the
        selected task name so that :meth:`resolve` can reproduce the exact
        task/verifier binding. Single-family packs get a clear error for
        unsupported explicit selections instead of silently changing tasks.
        """
        if task_name is not None:
            raise ValueError(
                f"{type(self).__name__} does not support explicit "
                f"task_name={task_name!r}"
            )
        return self.generate(seed)

    def load_task(self, task_id: str) -> Scenario:
        """Load one already materialized task from the provider's task store."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide a materialized task store"
        )

    def resolve(self, reference: Scenario) -> Scenario:
        """Resolve a persisted public task reference to its private task data.

        Deterministic generators can use this default seed-based resolver. An
        API-backed generator must override it and load the materialized task by
        ``reference.task_id`` instead of asking a model to generate the task again.
        """
        scenario = self.generate(reference.seed)
        if scenario.task_id != reference.task_id or scenario.env_id != reference.env_id:
            raise ValueError(
                "resolved task identity does not match its persisted reference"
            )
        if dict(scenario.public_config) != dict(reference.public_config):
            raise ValueError(
                "resolved task public config does not match its persisted reference"
            )
        return replace(
            scenario,
            instruction=reference.instruction,
        )

    def author_references(
        self,
        scenario: Scenario,
    ) -> Mapping[str, Mapping[str, Any]]:
        """Return task-specific verifier and judge references.

        Concrete Env packs own these references because they understand the
        task semantics. API-backed generators should load the reviewed,
        materialized references paired with ``scenario.task_id`` rather than ask a
        model to regenerate them during rollout or verification.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide task references"
        )


class Env(ABC):
    """One fresh, episode-scoped controlled environment.

    ``reset`` owns any backend-server startup or session connection, while
    ``close`` owns the matching teardown. Callers only use this interface and
    never manage an Env's transport directly.
    """

    env_id: str
    spec: EnvironmentSpec

    @abstractmethod
    def tools(self) -> Sequence[ToolSpec]:
        raise NotImplementedError

    @abstractmethod
    def reset(self, scenario: Scenario, workspace: Path) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    def call(self, tool_name: str, args: Mapping[str, Any]) -> ToolResult:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __enter__(self) -> "Env":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    reward: float
    checks: tuple[VerificationCheck, ...]
    reason: str = ""

    @classmethod
    def from_checks(
        cls,
        checks: Sequence[VerificationCheck],
        *,
        reason: str = "",
    ) -> "VerificationResult":
        values = tuple(checks)
        passed = bool(values) and all(item.passed for item in values)
        reward = (
            sum(1.0 for item in values if item.passed) / len(values)
            if values
            else 0.0
        )
        return cls(passed=passed, reward=reward, checks=values, reason=reason)

    @classmethod
    def from_reached_goal(
        cls,
        reached_goal: bool,
        *,
        detail: str = "",
        reason: str = "",
    ) -> "VerificationResult":
        """Build the pilot verifier's single binary task-completion signal."""
        reached = bool(reached_goal)
        return cls(
            passed=reached,
            reward=float(reached),
            checks=(VerificationCheck("reached_goal", reached, detail),),
            reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reward": self.reward,
            "checks": [item.to_dict() for item in self.checks],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerificationResult":
        return cls(
            passed=bool(value["passed"]),
            reward=float(value["reward"]),
            checks=tuple(
                VerificationCheck(
                    name=str(item["name"]),
                    passed=bool(item["passed"]),
                    detail=str(item.get("detail") or ""),
                )
                for item in value.get("checks") or []
            ),
            reason=str(value.get("reason") or ""),
        )


class Verifier(ABC):
    """Compare one task reference with one replayed environment snapshot.

    During the pilot phase implementations return exactly one binary check,
    ``reached_goal``, through ``VerificationResult.from_reached_goal``.
    """

    @abstractmethod
    def verify(
        self,
        scenario: Scenario,
        trajectory: "Trajectory",
        snapshot: Mapping[str, Any],
    ) -> VerificationResult:
        raise NotImplementedError


__all__ = [
    "Env",
    "EnvironmentSpec",
    "RuleSpec",
    "Scenario",
    "ScenarioGenerator",
    "TaskProvider",
    "VerificationCheck",
    "VerificationResult",
    "Verifier",
]
