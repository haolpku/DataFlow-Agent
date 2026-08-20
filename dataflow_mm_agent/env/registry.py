"""Generic registry and plugin loader for externally distributed environments."""

from __future__ import annotations

import importlib
import os
import threading
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Callable, Mapping

from ..contracts.environment import (
    Env,
    EnvironmentSpec,
    Scenario,
    ScenarioGenerator,
    TaskProvider,
    Verifier,
)

@dataclass(frozen=True)
class EnvironmentBundle:
    spec: EnvironmentSpec
    env_factory: Callable[[], Env]
    task_provider: TaskProvider
    verifier: Verifier

    def __post_init__(self) -> None:
        if self.task_provider.env_id != self.spec.env_id:
            raise ValueError(
                "EnvironmentBundle spec/task provider env_id mismatch: "
                f"{self.spec.env_id!r} != {self.task_provider.env_id!r}"
            )
        declared_env_id = getattr(self.env_factory, "env_id", None)
        if declared_env_id is not None and declared_env_id != self.spec.env_id:
            raise ValueError(
                "EnvironmentBundle spec/env factory env_id mismatch: "
                f"{self.spec.env_id!r} != {declared_env_id!r}"
            )
        declared_spec = getattr(self.env_factory, "spec", None)
        if declared_spec is not None and declared_spec != self.spec:
            raise ValueError("EnvironmentBundle spec does not match env factory spec")
        verifier_env_id = getattr(self.verifier, "env_id", None)
        if verifier_env_id is not None and verifier_env_id != self.spec.env_id:
            raise ValueError(
                "EnvironmentBundle spec/verifier env_id mismatch: "
                f"{self.spec.env_id!r} != {verifier_env_id!r}"
            )


ENVIRONMENTS: dict[str, EnvironmentBundle] = {}
ENVIRONMENT_ENTRY_POINT_GROUP = "dataflow_mm_agent.environments"
_LOADED_ENTRY_POINTS: set[str] = set()
_LOADED_MODULES: set[str] = set()
_REGISTRY_LOCK = threading.RLock()


def register_environment(bundle: EnvironmentBundle) -> EnvironmentBundle:
    """Register one Env/task-family bundle under its declared env_id."""
    env_id = bundle.spec.env_id
    with _REGISTRY_LOCK:
        if env_id in ENVIRONMENTS:
            raise ValueError(f"environment {env_id!r} is already registered")
        ENVIRONMENTS[env_id] = bundle
    return bundle


def _call_registration_hook(value: object, source: str) -> None:
    if not callable(value):
        raise TypeError(f"environment plugin {source!r} must expose a callable")
    value()


def load_environment_plugins(*, modules: tuple[str, ...] | None = None) -> None:
    """Load installed Env packs and optional source-tree plugin modules.

    Installed distributions register a callable through the
    ``dataflow_mm_agent.environments`` entry-point group. During source-tree
    development, modules can be passed explicitly or listed in the comma-
    separated ``DATAFLOW_MM_AGENT_ENV_PLUGINS`` environment variable.
    """
    with _REGISTRY_LOCK:
        requested = modules
        if requested is None:
            requested = tuple(
                item.strip()
                for item in os.environ.get(
                    "DATAFLOW_MM_AGENT_ENV_PLUGINS", ""
                ).split(",")
                if item.strip()
            )
        for module_name in requested:
            if module_name in _LOADED_MODULES:
                continue
            module = importlib.import_module(module_name)
            _call_registration_hook(getattr(module, "register", None), module_name)
            _LOADED_MODULES.add(module_name)

        plugins = entry_points(group=ENVIRONMENT_ENTRY_POINT_GROUP)
        for plugin in plugins:
            identity = f"{plugin.name}:{plugin.value}"
            if identity in _LOADED_ENTRY_POINTS:
                continue
            _call_registration_hook(plugin.load(), identity)
            _LOADED_ENTRY_POINTS.add(identity)


def _bundle(env_id: str) -> EnvironmentBundle:
    if env_id not in ENVIRONMENTS:
        load_environment_plugins()
    try:
        return ENVIRONMENTS[env_id]
    except KeyError as exc:
        raise KeyError(
            f"unknown environment {env_id!r}; available: {sorted(ENVIRONMENTS)}"
        ) from exc


def get_environment_bundle(env_id: str) -> EnvironmentBundle:
    """Return an Env pack bundle, discovering installed plugins on demand."""
    return _bundle(env_id)


def make_env(env_id: str) -> Env:
    bundle = _bundle(env_id)
    env = bundle.env_factory()
    if (
        getattr(env, "env_id", None) != bundle.spec.env_id
        or getattr(env, "spec", None) != bundle.spec
    ):
        env.close()
        raise ValueError(f"environment factory returned an invalid {env_id!r} instance")
    return env


def get_environment_spec(env_id: str) -> EnvironmentSpec:
    """Return the static specification registered for one environment."""
    return _bundle(env_id).spec


def _validate_scenario(bundle: EnvironmentBundle, scenario: Scenario) -> Scenario:
    if scenario.env_id != bundle.spec.env_id:
        raise ValueError("scenario environment does not match its registered spec")
    init_config = scenario.private_config.get("init_config")
    if not isinstance(init_config, Mapping):
        raise ValueError("resolved scenario has no JSON init_config")
    bundle.spec.validate_init_config(init_config)
    return scenario


def make_scenario(
    env_id: str,
    seed: int,
    *,
    task_name: str | None = None,
) -> Scenario:
    """Materialize a task, optionally selecting an Env-pack task family."""
    bundle = _bundle(env_id)
    generator = bundle.task_provider
    if not isinstance(generator, ScenarioGenerator):
        raise TypeError(
            f"environment {env_id!r} has a materialized task provider, "
            "not a build-time ScenarioGenerator"
        )
    if task_name is None:
        return _validate_scenario(bundle, generator.generate(seed))
    return _validate_scenario(
        bundle,
        generator.generate_task(seed, task_name),
    )


def load_scenario(env_id: str, task_id: str) -> Scenario:
    """Load a reviewed/materialized task by stable id from its Env pack."""
    bundle = _bundle(env_id)
    scenario = bundle.task_provider.load_task(task_id)
    if scenario.env_id != env_id or scenario.task_id != task_id:
        raise ValueError("loaded task identity does not match the requested reference")
    return _validate_scenario(bundle, scenario)


def resolve_scenario(reference: Scenario) -> Scenario:
    """Resolve the private task paired with a persisted public Scenario."""
    bundle = _bundle(reference.env_id)
    return _validate_scenario(bundle, bundle.task_provider.resolve(reference))


def make_verifier(env_id: str) -> Verifier:
    return _bundle(env_id).verifier
