"""Registration hook for the self-contained PixelGames example."""

from pathlib import Path

from dataflow_mm_agent.env import (
    ENVIRONMENTS,
    EnvironmentBundle,
    register_environment,
)
from dataflow_mm_agent.storage import JsonTaskProvider

from .environment import ENV_SPEC, PixelGameEnv, PixelGameVerifier


TASK_DIR = Path(__file__).with_name("tasks")
_REGISTERED = False


def register() -> None:
    """Register the example environment once in the current process."""
    global _REGISTERED
    if _REGISTERED:
        return
    if ENV_SPEC.env_id in ENVIRONMENTS:
        raise ValueError(f"environment {ENV_SPEC.env_id!r} is already registered")
    register_environment(EnvironmentBundle(
        spec=ENV_SPEC,
        env_factory=PixelGameEnv,
        task_provider=JsonTaskProvider(
            env_id=ENV_SPEC.env_id,
            task_dir=TASK_DIR,
        ),
        verifier=PixelGameVerifier(),
    ))
    _REGISTERED = True


__all__ = [
    "ENV_SPEC",
    "PixelGameEnv",
    "PixelGameVerifier",
    "TASK_DIR",
    "register",
]
