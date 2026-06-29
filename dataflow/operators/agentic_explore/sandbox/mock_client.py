"""
In-memory mock sandbox for tests and offline development.

``MockSandboxClient`` implements :class:`SandboxClientABC` without any network.
It serves a tiny fixed tool set and a deterministic "knowledge base" so the
agent-explore loop can be exercised end-to-end (LLM decides tool call -> tool
runs -> observation -> ... -> finish) in CI without a live sandbox server.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import SandboxClientABC, ToolResult, ToolSchema


class MockSandboxClient(SandboxClientABC):
    """A deterministic, network-free sandbox for testing.

    Tools:
        - ``search(query)``: returns canned snippets matching the query.
        - ``finish(answer)``: terminates the episode (sets is_final).

    Args:
        knowledge: Optional mapping of keyword -> snippet used by ``search``.
        stateful: Mimic a session-stateful backend (default False).
    """

    def __init__(
        self,
        knowledge: Optional[Dict[str, str]] = None,
        *,
        stateful: bool = False,
    ):
        self.knowledge = knowledge or {
            "capital of france": "Paris is the capital of France.",
            "tallest mountain": "Mount Everest is the tallest mountain at 8849m.",
        }
        self.stateful = stateful
        self.created_sessions: List[str] = []
        self.destroyed_sessions: List[str] = []
        self.calls: List[Dict[str, Any]] = []

    def list_tools(self, domain: Optional[str] = None) -> List[ToolSchema]:
        return [
            ToolSchema(
                name="search",
                description="Search the mock knowledge base for a query string.",
                parameters=[{"name": "query", "type": "string", "required": True}],
            ),
            ToolSchema(
                name="finish",
                description="Finish the task and return the final answer.",
                parameters=[{"name": "answer", "type": "string", "required": True}],
            ),
        ]

    def create_session(self, domain, *, worker_id=None, config=None):
        if not self.stateful:
            return None
        sid = f"{domain}_{worker_id or 'w'}_{len(self.created_sessions)}"
        self.created_sessions.append(sid)
        return sid

    def destroy_session(self, domain, *, worker_id=None):
        if self.stateful and worker_id is not None:
            self.destroyed_sessions.append(f"{domain}_{worker_id}")

    def execute(self, action, params=None, *, worker_id=None, timeout=None) -> ToolResult:
        params = params or {}
        bare = action.split(":", 1)[1] if ":" in action else action
        self.calls.append({"action": bare, "params": params, "worker_id": worker_id})

        if bare == "search":
            query = str(params.get("query", "")).lower()
            hits = [v for k, v in self.knowledge.items() if k in query or query in k]
            obs = hits if hits else [f"No results for '{params.get('query', '')}'."]
            return ToolResult(ok=True, observation={"results": obs})

        if bare == "finish":
            return ToolResult(
                ok=True,
                observation={"answer": params.get("answer", "")},
                is_final=True,
            )

        return ToolResult(ok=False, error=f"Unknown tool: {bare}", code=4040)
