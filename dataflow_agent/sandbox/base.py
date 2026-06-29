"""
Abstract sandbox-client contract used by agent-explore operators.

This module is intentionally dependency-free (only the standard library) so it
can be imported anywhere without pulling in ``requests``/``httpx`` or any
sandbox SDK.  Concrete clients live in sibling modules and may add their own
deps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SandboxError(Exception):
    """Raised when a sandbox call fails in a non-recoverable way.

    ``code`` mirrors the backend's error code when one is available (0 means
    success and is never wrapped in an exception).
    """

    def __init__(self, message: str, code: Optional[int] = None,
                 payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


@dataclass
class ToolSchema:
    """Backend-agnostic description of a single callable tool.

    Concrete clients translate their native tool-listing format into a list of
    these.  ``name`` is what gets passed back to :meth:`SandboxClientABC.execute`
    (without any resource prefix -- the client re-adds it).
    """

    name: str
    description: str = ""
    parameters: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolResult:
    """Normalized result of a single tool execution.

    Every backend, regardless of wire format, is mapped onto this shape so the
    exploration loop is backend-independent.

    Attributes:
        ok: True when the tool executed without error.
        observation: The payload the agent should "see" next turn (already
            stringified / json-friendly).
        raw: The untouched backend response, for debugging / lineage.
        error: Human-readable error message when ``ok`` is False.
        code: Backend error code (0 == success) when available.
        is_final: Optional hint from the backend that the task is complete.
        elapsed_ms: Server-side execution time when reported.
    """

    ok: bool
    observation: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    code: Optional[int] = None
    is_final: bool = False
    elapsed_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "observation": self.observation,
            "error": self.error,
            "code": self.code,
            "is_final": self.is_final,
            "elapsed_ms": self.elapsed_ms,
        }


class SandboxClientABC(ABC):
    """Minimal contract an agent-explore loop needs from a sandbox.

    Implementations should be cheap to construct and safe to share across
    threads at the granularity of one *worker* (the exploration operator uses a
    thread pool and gives each thread its own ``worker_id`` via
    :meth:`new_worker_id` when the backend is session-stateful).

    Lifecycle (per task / per worker):

        client.create_session(domain)          # optional for stateless backends
        tools = client.list_tools(domain)       # used to build the agent prompt
        while not done:
            result = client.execute(action, params, worker_id=...)
        client.destroy_session(domain, worker_id=...)
    """

    #: Whether this backend keeps per-session state that must be created and
    #: destroyed around each task (e.g. a VM/desktop).  Stateless API backends
    #: (web search, RAG) can leave this False to skip session churn.
    stateful: bool = False

    @abstractmethod
    def list_tools(self, domain: Optional[str] = None) -> List[ToolSchema]:
        """Return the tools available for ``domain`` (or all tools)."""
        raise NotImplementedError

    @abstractmethod
    def execute(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        worker_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ToolResult:
        """Execute a single tool call and return a normalized result.

        ``action`` may be a bare tool name or a ``"resource:action"`` string;
        concrete clients decide how to route it.
        """
        raise NotImplementedError

    # ---- optional session lifecycle (no-ops for stateless backends) ----

    def create_session(
        self,
        domain: str,
        *,
        worker_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Create a session for a stateful domain. Returns a session id or None."""
        return None

    def destroy_session(
        self,
        domain: str,
        *,
        worker_id: Optional[str] = None,
    ) -> None:
        """Destroy a previously created session. No-op by default."""
        return None

    def new_worker_id(self) -> str:
        """Mint a fresh worker id for thread/session isolation."""
        import uuid
        return f"worker_{uuid.uuid4().hex[:8]}"

    def health_check(self) -> bool:
        """Return True if the backend looks reachable. Best-effort."""
        return True

    def close(self) -> None:
        """Release any client-side resources. No-op by default."""
        return None

    # context-manager sugar so operators can `with client:` if they want
    def __enter__(self) -> "SandboxClientABC":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
