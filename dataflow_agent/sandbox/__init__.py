"""
Pluggable sandbox-client abstraction for agentic exploration operators.

The agent-explore operators in DataFlow never talk to a concrete sandbox
directly.  They only depend on :class:`SandboxClientABC` defined here, which
exposes the minimal contract an exploration loop needs:

    create_session(domain)  ->  session_id
    list_tools(domain)      ->  [tool schema, ...]
    execute(action, params) ->  ToolResult
    destroy_session(...)    ->  None

Concrete backends implement this ABC.  One backend is
:class:`~dataflow_agent.sandbox.http_client.HTTPSandboxClient`, which drives a
remote sandbox server over a generic HTTP protocol *over the wire only* (plain
``requests`` POSTs) -- it imports nothing from any external sandbox package, so
DataFlow keeps no code dependency on one.  Adding a new sandbox (your own, an
OpenAI/Anthropic computer-use server, a local Docker harness, ...) is a matter
of writing another subclass; the operators are untouched.
"""

from .base import SandboxClientABC, ToolResult, ToolSchema, SandboxError
from .http_client import HTTPSandboxClient
from .mock_client import MockSandboxClient
from .coding_client import CodingSandboxClient

__all__ = [
    "SandboxClientABC",
    "ToolResult",
    "ToolSchema",
    "SandboxError",
    "HTTPSandboxClient",
    "MockSandboxClient",
    "CodingSandboxClient",
]
