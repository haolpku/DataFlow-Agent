"""Stable data and interface contracts for the agent runtime.

Runtime orchestration, persistence, provider adapters, and host integrations
belong in the parent package. This namespace is the canonical import boundary
for values exchanged between those implementations.
"""

from .agent import (
    Content,
    ContentLimits,
    ImageContent,
    Message,
    TextContent,
    ToolError,
    ToolResult,
    ToolSpec,
    content_from_dict,
    validate_content,
)
from .environment import (
    Env,
    EnvironmentSpec,
    RuleSpec,
    Scenario,
    ScenarioGenerator,
    TaskProvider,
    VerificationCheck,
    VerificationResult,
    Verifier,
)
from .task import (
    STATE_PREDICATE_VERIFIER_KIND,
    TASK_ARTIFACT_SCHEMA,
    TASK_ARTIFACT_SCHEMA_VERSION,
    TaskArtifact,
    state_predicate_verifier,
)
from .trajectory import EpisodeStep, Trajectory, utc_now

__all__ = [
    "Content",
    "ContentLimits",
    "Env",
    "EnvironmentSpec",
    "EpisodeStep",
    "ImageContent",
    "Message",
    "RuleSpec",
    "STATE_PREDICATE_VERIFIER_KIND",
    "Scenario",
    "ScenarioGenerator",
    "TASK_ARTIFACT_SCHEMA",
    "TASK_ARTIFACT_SCHEMA_VERSION",
    "TaskArtifact",
    "TaskProvider",
    "TextContent",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "Trajectory",
    "VerificationCheck",
    "VerificationResult",
    "Verifier",
    "content_from_dict",
    "state_predicate_verifier",
    "utc_now",
    "validate_content",
]
