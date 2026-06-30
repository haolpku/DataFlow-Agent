"""
DataFlow-Agent — agentic exploration & trajectory-quality operators for
`DataFlow <https://github.com/OpenDCAI/DataFlow>`_.

This is a standalone top-level package (NOT under the ``dataflow`` namespace) so
it coexists with a pip-installed ``open-dataflow``: the operator base classes
(``OperatorABC`` / ``LLMServingABC`` / ``OPERATOR_REGISTRY`` / storage) come from
``open-dataflow``, while every agent-specific operator and sandbox client lives
here.

Importing this package registers all operators into DataFlow's
``OPERATOR_REGISTRY`` (via the ``@OPERATOR_REGISTRY.register()`` decorators), so
after ``import dataflow_agent`` they are resolvable by name exactly like the
built-in DataFlow operators.
"""

# Importing the operator modules triggers their @OPERATOR_REGISTRY.register().
from dataflow_agent.generate.agent_explore_generator import AgentExploreGenerator
from dataflow_agent.generate.agent_explore_tree_generator import AgentExploreTreeGenerator
from dataflow_agent.eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
from dataflow_agent.filter.trajectory_filter import TrajectoryFilter
from dataflow_agent.refine.trajectory_refiner import TrajectoryRefiner

# Sandbox backends (the pluggable transport layer).
from dataflow_agent.sandbox import (
    SandboxClientABC,
    ToolResult,
    ToolSchema,
    SandboxError,
    MockSandboxClient,
    AgentFlowSandboxClient,
)

__all__ = [
    "AgentExploreGenerator",
    "AgentExploreTreeGenerator",
    "TrajectoryQualityEvaluator",
    "TrajectoryFilter",
    "TrajectoryRefiner",
    "SandboxClientABC",
    "ToolResult",
    "ToolSchema",
    "SandboxError",
    "MockSandboxClient",
    "AgentFlowSandboxClient",
]

__version__ = "0.1.0"
