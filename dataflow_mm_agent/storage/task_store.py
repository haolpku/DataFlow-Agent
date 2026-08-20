"""JSON task storage, task-provider, and verifier implementations.

The task artifact and verifier-spec data contracts live in
:mod:`dataflow_mm_agent.contracts.task`. This module intentionally contains
only runtime implementations.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts.environment import (
    Scenario,
    TaskProvider,
    VerificationResult,
    Verifier,
)
from ..contracts.task import (
    CHECK_ID_PATTERN,
    PREDICATE_OPERATORS,
    STATE_PREDICATE_VERIFIER_KIND,
    TaskArtifact,
    validate_task_id,
)
from ..contracts.trajectory import Trajectory


class JsonTaskStore:
    """One validated materialized task per ``<task_id>.json`` file."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def load(self, task_id: str) -> TaskArtifact:
        validate_task_id(task_id)
        path = self.root / f"{task_id}.json"
        if not path.is_file():
            raise KeyError(f"task artifact not found: {task_id!r} in {self.root}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"task artifact must be a JSON object: {path}")
        artifact = TaskArtifact.from_dict(value)
        if artifact.task_id != task_id:
            raise ValueError(f"task id/filename mismatch in {path}")
        return artifact

    def list(self, *, env_id: str | None = None) -> tuple[TaskArtifact, ...]:
        if not self.root.exists():
            return ()
        artifacts = []
        seen: set[str] = set()
        for path in sorted(self.root.glob("*.json")):
            artifact = self.load(path.stem)
            if artifact.task_id in seen:
                raise ValueError(f"duplicate task artifact id: {artifact.task_id}")
            seen.add(artifact.task_id)
            if env_id is None or artifact.env_id == env_id:
                artifacts.append(artifact)
        return tuple(artifacts)

    def find(
        self,
        *,
        env_id: str,
        seed: int,
        task_name: str,
        task_version: int = 1,
    ) -> TaskArtifact | None:
        matches = [
            item
            for item in self.list(env_id=env_id)
            if item.seed == seed
            and item.task_name == task_name
            and item.task_version == task_version
        ]
        if len(matches) > 1:
            raise ValueError(
                "multiple task artifacts match "
                f"{env_id}/{task_name}@{task_version}/seed={seed}"
            )
        return matches[0] if matches else None

    def save(self, artifact: TaskArtifact, *, overwrite: bool = False) -> Path:
        value = artifact.to_dict()
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / f"{artifact.task_id}.json"
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{artifact.task_id}.", suffix=".tmp", dir=self.root
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if destination.exists() and not overwrite:
                raise FileExistsError(destination)
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination


class JsonTaskProvider(TaskProvider):
    """Generic strict loader for one Env's materialized JSON task directory."""

    def __init__(self, *, env_id: str, task_dir: str | Path):
        self.env_id = env_id
        self.task_store = JsonTaskStore(task_dir)

    def task_names(self) -> tuple[str, ...]:
        return tuple(sorted({
            item.task_name
            for item in self.task_store.list(env_id=self.env_id)
        }))

    def load_task(self, task_id: str) -> Scenario:
        artifact = self.task_store.load(task_id)
        if artifact.env_id != self.env_id:
            raise ValueError(
                f"task {task_id!r} belongs to {artifact.env_id!r}, "
                f"not {self.env_id!r}"
            )
        return artifact.to_scenario()

    def resolve(self, reference: Scenario) -> Scenario:
        resolved = self.load_task(reference.task_id)
        if resolved.instruction != reference.instruction:
            raise ValueError("materialized task instruction does not match reference")
        if resolved.env_id != reference.env_id:
            raise ValueError("materialized task environment does not match reference")
        if dict(resolved.public_config) != dict(reference.public_config):
            raise ValueError("resolved task public config does not match reference")
        return resolved

    def author_references(
        self,
        scenario: Scenario,
    ) -> Mapping[str, Mapping[str, Any]]:
        artifact = self.task_store.load(scenario.task_id)
        if artifact.env_id != self.env_id:
            raise ValueError(
                f"task {scenario.task_id!r} belongs to {artifact.env_id!r}, "
                f"not {self.env_id!r}"
            )
        resolved = artifact.to_scenario()
        if (
            resolved.env_id != scenario.env_id
            or resolved.instruction != scenario.instruction
            or dict(resolved.public_config) != dict(scenario.public_config)
            or dict(resolved.private_config) != dict(scenario.private_config)
        ):
            raise ValueError("scenario has an invalid materialized verifier binding")
        return artifact.references()


def _path_value(snapshot: Any, path: Sequence[str | int]) -> Any:
    current = snapshot
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes)):
                raise KeyError(part)
            current = current[part]
        else:
            if not isinstance(current, Mapping) or part not in current:
                raise KeyError(part)
            current = current[part]
    return current


class StatePredicateVerifier(Verifier):
    """Interpreter for the checklist-based final-snapshot predicate DSL."""

    def __init__(self, *, env_id: str | None = None):
        self.env_id = env_id

    @staticmethod
    def evaluate(
        spec: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> tuple[bool, str]:
        if spec.get("kind") != STATE_PREDICATE_VERIFIER_KIND:
            return False, "verifier kind must be state_predicate"
        if spec.get("aggregation") != "all":
            return False, "verifier aggregation must be all"
        checks = spec.get("checks")
        if not isinstance(checks, list) or not checks:
            return False, "verifier checks must be a non-empty list"
        results: list[str] = []
        passed = True
        for index, check in enumerate(checks):
            if not isinstance(check, Mapping):
                return False, f"check[{index}] is not an object"
            check_id = str(check.get("check_id") or "")
            description = str(check.get("description") or "")
            predicate = check.get("predicate")
            if not CHECK_ID_PATTERN.fullmatch(check_id) or not description:
                return False, f"check[{index}] has invalid id/description"
            if not isinstance(predicate, Mapping):
                return False, f"check[{index}].predicate is not an object"
            path = predicate.get("path")
            op = predicate.get("op")
            if (
                not isinstance(path, list)
                or not path
                or op not in PREDICATE_OPERATORS
            ):
                return False, f"check[{index}] has invalid path/operator"
            try:
                actual = _path_value(snapshot, path)
            except (KeyError, IndexError, TypeError):
                actual = None
                item_passed = False
            else:
                expected = predicate.get("value")
                try:
                    item_passed = {
                        "equals": lambda: actual == expected,
                        "not_equals": lambda: actual != expected,
                        "gte": lambda: actual >= expected,
                        "lte": lambda: actual <= expected,
                        "empty": lambda: not actual,
                        "not_empty": lambda: bool(actual),
                    }[str(op)]()
                except (KeyError, TypeError, ValueError):
                    item_passed = False
            passed = passed and item_passed
            results.append(
                f"{check_id} ({'.'.join(map(str, path))} {op}): "
                f"{'pass' if item_passed else 'fail'}"
            )
        return passed, "; ".join(results)

    def verify(
        self,
        scenario: Scenario,
        trajectory: Trajectory,
        snapshot: Mapping[str, Any],
    ) -> VerificationResult:
        private = scenario.private_config
        spec = private.get("verifier")
        binding_valid = (
            (self.env_id is None or scenario.env_id == self.env_id)
            and isinstance(spec, Mapping)
            and spec.get("kind") == STATE_PREDICATE_VERIFIER_KIND
        )
        if not binding_valid:
            return VerificationResult.from_reached_goal(
                False,
                detail="invalid state-predicate verifier binding",
            )
        assert isinstance(spec, Mapping)
        reached, detail = self.evaluate(spec, snapshot)
        return VerificationResult.from_reached_goal(reached, detail=detail)


__all__ = [
    "JsonTaskProvider",
    "JsonTaskStore",
    "StatePredicateVerifier",
]
