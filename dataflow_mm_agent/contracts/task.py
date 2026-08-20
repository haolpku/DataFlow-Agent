"""Materialized task artifact and verifier-spec contracts.

Task JSON is data, never executable code. The storage backend and verifier
interpreters live outside this contract module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .environment import Scenario


TASK_ARTIFACT_SCHEMA_VERSION = 1
STATE_PREDICATE_VERIFIER_KIND = "state_predicate"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
CHECK_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
PREDICATE_OPERATORS = frozenset(
    {"equals", "not_equals", "gte", "lte", "empty", "not_empty"}
)


TASK_ARTIFACT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://dataflow-mm-agent.local/schemas/task-artifact-v1.json",
    "title": "DataFlow-MM-Agent materialized task artifact v1",
    "type": "object",
    "required": [
        "schema_version",
        "task_id",
        "env_id",
        "seed",
        "task_name",
        "task_version",
        "instruction",
        "public_config",
        "init_config",
        "task_data",
        "episode_config",
        "verifier",
        "judge_ref",
        "provenance",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "task_id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
        },
        "env_id": {"type": "string", "minLength": 1},
        "seed": {"type": "integer"},
        "task_name": {"type": "string", "minLength": 1},
        "task_version": {"type": "integer", "minimum": 1},
        "instruction": {"type": "string", "minLength": 1},
        "public_config": {"type": "object"},
        "init_config": {"type": "object"},
        "task_data": {"type": "object"},
        "episode_config": {
            "type": "object",
            "required": ["max_steps"],
            "additionalProperties": False,
            "properties": {
                "max_steps": {"type": "integer", "minimum": 1},
            },
        },
        "verifier": {
            "type": "object",
            "required": ["kind", "aggregation", "checks"],
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "pattern": r"^[A-Za-z][A-Za-z0-9._-]{0,127}$",
                },
                "aggregation": {"const": "all"},
                "checks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["check_id", "description", "predicate"],
                        "additionalProperties": False,
                        "properties": {
                            "check_id": {
                                "type": "string",
                                "pattern": r"^[A-Za-z][A-Za-z0-9._-]{0,127}$",
                            },
                            "description": {"type": "string", "minLength": 1},
                            "predicate": {
                                "type": "object",
                                "minProperties": 1,
                            },
                        },
                    },
                },
            },
            "allOf": [{
                "if": {
                    "properties": {
                        "kind": {"const": STATE_PREDICATE_VERIFIER_KIND},
                    },
                    "required": ["kind"],
                },
                "then": {
                    "properties": {
                        "checks": {
                            "items": {
                                "properties": {
                                    "predicate": {
                                        "required": ["path", "op"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "path": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {
                                                    "type": ["string", "integer"],
                                                },
                                            },
                                            "op": {
                                                "enum": sorted(PREDICATE_OPERATORS),
                                            },
                                            "value": {},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            }],
        },
        "judge_ref": {"type": "object"},
        "provenance": {"type": "object"},
    },
}


def json_copy(value: Any) -> Any:
    """Return a detached JSON value and reject non-serializable objects."""
    return json.loads(json.dumps(value, ensure_ascii=False))


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"unsafe task_id={task_id!r}")


def validate_task_artifact(value: Mapping[str, Any]) -> None:
    """Validate the serialized task artifact contract."""
    try:
        import jsonschema

        jsonschema.validate(instance=dict(value), schema=TASK_ARTIFACT_SCHEMA)
    except ImportError:
        required = set(TASK_ARTIFACT_SCHEMA["required"])
        missing = required.difference(value)
        if missing:
            raise ValueError(f"task artifact missing fields: {sorted(missing)}")
    except Exception as exc:
        raise ValueError(f"invalid task artifact: {exc}") from exc

    task_id = str(value.get("task_id") or "")
    validate_task_id(task_id)
    public = dict(value.get("public_config") or {})
    task_name = str(value.get("task_name") or "")
    task_version = int(value.get("task_version") or 0)
    if "task_name" in public and public["task_name"] != task_name:
        raise ValueError("public_config.task_name does not match task_name")
    if int(public.get("task_version", task_version)) != task_version:
        raise ValueError("public_config.task_version does not match task_version")

    verifier = dict(value.get("verifier") or {})
    verifier_kind = str(verifier.get("kind") or "")
    if not CHECK_ID_PATTERN.fullmatch(verifier_kind):
        raise ValueError(f"invalid verifier kind: {verifier_kind!r}")
    if verifier.get("aggregation") != "all":
        raise ValueError("state-predicate verifier aggregation must be 'all'")
    check_ids: list[str] = []
    for check in verifier.get("checks") or []:
        check_id = str(check.get("check_id") or "")
        if not CHECK_ID_PATTERN.fullmatch(check_id):
            raise ValueError(f"invalid verifier check_id: {check_id!r}")
        check_ids.append(check_id)
        predicate = check.get("predicate") or {}
        if verifier_kind == STATE_PREDICATE_VERIFIER_KIND:
            if set(predicate).difference({"path", "op", "value"}):
                raise ValueError("state predicate contains unsupported fields")
            path = predicate.get("path")
            op = predicate.get("op")
            if not isinstance(path, list) or not path:
                raise ValueError("state predicate path must be a non-empty list")
            if op not in PREDICATE_OPERATORS:
                raise ValueError(f"unsupported predicate operator: {op!r}")
            if op in {"equals", "not_equals", "gte", "lte"} and "value" not in predicate:
                raise ValueError(f"predicate operator {op!r} requires value")
            if op in {"empty", "not_empty"} and "value" in predicate:
                raise ValueError(f"predicate operator {op!r} does not accept value")
    if len(set(check_ids)) != len(check_ids):
        raise ValueError("verifier check_id values must be unique within a task")


@dataclass(frozen=True)
class TaskArtifact:
    schema_version: int
    task_id: str
    env_id: str
    seed: int
    task_name: str
    task_version: int
    instruction: str
    public_config: Mapping[str, Any]
    init_config: Mapping[str, Any]
    task_data: Mapping[str, Any]
    episode_config: Mapping[str, Any]
    verifier: Mapping[str, Any]
    judge_ref: Mapping[str, Any]
    provenance: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskArtifact":
        validate_task_artifact(value)
        return cls(
            schema_version=int(value["schema_version"]),
            task_id=str(value["task_id"]),
            env_id=str(value["env_id"]),
            seed=int(value["seed"]),
            task_name=str(value["task_name"]),
            task_version=int(value["task_version"]),
            instruction=str(value["instruction"]),
            public_config=json_copy(value["public_config"]),
            init_config=json_copy(value["init_config"]),
            task_data=json_copy(value["task_data"]),
            episode_config=json_copy(value["episode_config"]),
            verifier=json_copy(value["verifier"]),
            judge_ref=json_copy(value["judge_ref"]),
            provenance=json_copy(value["provenance"]),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "env_id": self.env_id,
            "seed": self.seed,
            "task_name": self.task_name,
            "task_version": self.task_version,
            "instruction": self.instruction,
            "public_config": json_copy(self.public_config),
            "init_config": json_copy(self.init_config),
            "task_data": json_copy(self.task_data),
            "episode_config": json_copy(self.episode_config),
            "verifier": json_copy(self.verifier),
            "judge_ref": json_copy(self.judge_ref),
            "provenance": json_copy(self.provenance),
        }
        validate_task_artifact(value)
        return value

    def to_scenario(self) -> Scenario:
        return Scenario(
            task_id=self.task_id,
            env_id=self.env_id,
            seed=self.seed,
            instruction=self.instruction,
            public_config=json_copy(self.public_config),
            private_config={
                "task_name": self.task_name,
                "task_version": self.task_version,
                "init_config": json_copy(self.init_config),
                "task_data": json_copy(self.task_data),
                "episode_config": json_copy(self.episode_config),
                "verifier": json_copy(self.verifier),
                "judge_ref": json_copy(self.judge_ref),
            },
        )

    def references(self) -> dict[str, dict[str, Any]]:
        verifier = dict(self.verifier)
        checks = verifier.get("checks") or []
        return {
            "verifier_ref": {
                "kind": verifier["kind"],
                "contract": "binary_reached_goal",
                "aggregation": verifier["aggregation"],
                "check_ids": [str(check["check_id"]) for check in checks],
                "task_name": self.task_name,
                "task_version": self.task_version,
            },
            "judge_ref": json_copy(self.judge_ref),
        }


def state_predicate_verifier(
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the canonical checklist-based final-state verifier spec."""
    return {
        "kind": STATE_PREDICATE_VERIFIER_KIND,
        "aggregation": "all",
        "checks": [json_copy(item) for item in checks],
    }


__all__ = [
    "CHECK_ID_PATTERN",
    "PREDICATE_OPERATORS",
    "STATE_PREDICATE_VERIFIER_KIND",
    "TASK_ARTIFACT_SCHEMA",
    "TASK_ARTIFACT_SCHEMA_VERSION",
    "TASK_ID_PATTERN",
    "TaskArtifact",
    "json_copy",
    "state_predicate_verifier",
    "validate_task_artifact",
    "validate_task_id",
]
