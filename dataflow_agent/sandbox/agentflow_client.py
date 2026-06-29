"""
AgentFlow sandbox client -- talks to an AgentFlow sandbox server purely over
its HTTP protocol.

Decoupling note
---------------
This module imports **nothing** from AgentFlow.  It only reproduces the wire
contract of the AgentFlow sandbox HTTP server (endpoint paths + the
``{code, message, data, meta}`` envelope).  That keeps DataFlow free of any code
dependency on a competing project while still being able to drive an
already-running AgentFlow sandbox during early bring-up.  Swapping in a
different sandbox later means writing another :class:`SandboxClientABC`
subclass; these operators do not change.

Protocol (as of the server we target):
    GET  /health
    GET  /api/v1/tools
    POST /api/v1/execute              {action, params, worker_id, timeout}
    POST /api/v1/session/create       {worker_id, resource_type, session_config}
    POST /api/v1/session/destroy      {worker_id, resource_type}
Response envelope:
    {"code": 0, "message": "success", "data": <payload>, "meta": {...}}
    code == 0 means success; 4xxx == client error; 5xxx == server error.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from dataflow import get_logger

from .base import SandboxClientABC, ToolResult, ToolSchema, SandboxError

# Endpoint paths mirrored from the AgentFlow sandbox HTTPEndpoints contract.
_EP_HEALTH = "/health"
_EP_TOOLS = "/api/v1/tools"
_EP_EXECUTE = "/api/v1/execute"
_EP_SESSION_CREATE = "/api/v1/session/create"
_EP_SESSION_DESTROY = "/api/v1/session/destroy"


class AgentFlowSandboxClient(SandboxClientABC):
    """HTTP client for an AgentFlow-protocol sandbox server.

    Args:
        base_url: Root URL of the running sandbox server, e.g.
            ``"http://127.0.0.1:18890"``.
        domain: Default resource type / domain prefix (``"web"``, ``"rag"``,
            ``"vm"``, ``"sql"``, ``"doc"``, ...). Used when an ``action`` is
            passed without its own ``resource:`` prefix.
        stateful: Whether to create/destroy a per-task session. Defaults to
            False (good for the stateless API domains: web, rag). Set True for
            VM/GUI-style domains that hold desktop state.
        timeout: Per-request timeout in seconds.
        max_retries: Network-level retry attempts for transient failures.
        session_config: Passed through to ``session/create`` for stateful
            domains.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18890",
        domain: str = "web",
        *,
        stateful: bool = False,
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        session_config: Optional[Dict[str, Any]] = None,
    ):
        self.logger = get_logger()
        self.base_url = base_url.rstrip("/")
        self.domain = domain
        self.stateful = stateful
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session_config = session_config or {}

        # Lazy import so the abstraction layer stays importable without requests.
        try:
            import requests  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ImportError(
                "AgentFlowSandboxClient requires the 'requests' package. "
                "Install it with `pip install requests`."
            ) from exc
        import requests
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # low-level HTTP
    # ------------------------------------------------------------------ #
    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.post(url, json=payload, timeout=self.timeout)
                # The server encodes business errors in the JSON body too, but
                # 4xx/5xx still carry a valid envelope we want to surface.
                try:
                    body = resp.json()
                except ValueError:
                    body = {
                        "code": resp.status_code,
                        "message": resp.text[:500],
                        "data": None,
                        "meta": {},
                    }
                return body
            except requests.RequestException as exc:  # transient network error
                last_exc = exc
                self.logger.warning(
                    f"[AgentFlowSandboxClient] POST {path} attempt "
                    f"{attempt}/{self.max_retries} failed: {exc}"
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
        raise SandboxError(
            f"POST {url} failed after {self.max_retries} attempts: {last_exc}"
        )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        import requests
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=self.timeout)
            try:
                return resp.json()
            except ValueError:
                return {"code": resp.status_code, "message": resp.text[:500], "data": None}
        except requests.RequestException as exc:
            raise SandboxError(f"GET {url} failed: {exc}")

    @staticmethod
    def _qualify(action: str, domain: str) -> str:
        """Ensure the action carries a ``resource:`` prefix for the server."""
        return action if ":" in action else f"{domain}:{action}"

    # ------------------------------------------------------------------ #
    # SandboxClientABC implementation
    # ------------------------------------------------------------------ #
    def health_check(self) -> bool:
        try:
            body = self._get(_EP_HEALTH)
            return str(body.get("status", "")).lower() in {"healthy", "ok", "ready"}
        except SandboxError:
            return False

    def list_tools(self, domain: Optional[str] = None) -> List[ToolSchema]:
        """Fetch the server's tool catalog and filter by domain prefix.

        The server's ``/api/v1/tools`` returns names that may be prefixed by
        resource type (``"web:web-search"``) or bare. We keep tools whose name
        starts with ``{domain}:`` or that are unprefixed, and strip the prefix
        in the returned :class:`ToolSchema.name`.
        """
        dom = domain or self.domain
        body = self._get(_EP_TOOLS)
        data = body.get("data", body)
        # The catalog may live under data["tools"] or be the data list itself.
        raw_tools = data.get("tools") if isinstance(data, dict) else data
        if raw_tools is None:
            raw_tools = []

        schemas: List[ToolSchema] = []
        for t in raw_tools:
            if isinstance(t, str):
                name, desc, params = t, "", []
            else:
                name = t.get("name") or t.get("tool") or ""
                desc = t.get("description", "")
                params = t.get("parameters", t.get("params", []))
            if not name:
                continue
            # domain filtering: keep "{dom}:*" and bare names; drop other domains
            if ":" in name:
                prefix, bare = name.split(":", 1)
                if prefix != dom:
                    continue
                name = bare
            schemas.append(ToolSchema(name=name, description=desc, parameters=params or []))
        return schemas

    def create_session(
        self,
        domain: str,
        *,
        worker_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if not self.stateful:
            return None
        payload = {
            "worker_id": worker_id or self.new_worker_id(),
            "resource_type": domain,
            "session_config": config or self.session_config,
        }
        body = self._post(_EP_SESSION_CREATE, payload)
        data = body.get("data") or {}
        if body.get("code", 0) != 0:
            raise SandboxError(
                f"session/create failed for domain '{domain}': {body.get('message')}",
                code=body.get("code"),
                payload=body,
            )
        return data.get("session_id") or data.get("session_name")

    def destroy_session(
        self,
        domain: str,
        *,
        worker_id: Optional[str] = None,
    ) -> None:
        if not self.stateful or worker_id is None:
            return None
        payload = {"worker_id": worker_id, "resource_type": domain}
        try:
            self._post(_EP_SESSION_DESTROY, payload)
        except SandboxError as exc:  # destroy is best-effort
            self.logger.warning(f"[AgentFlowSandboxClient] destroy_session: {exc}")
        return None

    def execute(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        worker_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ToolResult:
        payload = {
            "message_id": uuid.uuid4().hex,
            "action": self._qualify(action, self.domain),
            "params": params or {},
            "worker_id": worker_id or self.new_worker_id(),
            "timeout": timeout if timeout is not None else self.timeout,
        }
        body = self._post(_EP_EXECUTE, payload)
        return self._to_result(body)

    @staticmethod
    def _to_result(body: Dict[str, Any]) -> ToolResult:
        """Map the AgentFlow ``{code,message,data,meta}`` envelope to ToolResult."""
        code = body.get("code", -1)
        meta = body.get("meta") or {}
        data = body.get("data")
        ok = code == 0
        # Heuristic: backends may flag completion under data["is_final"].
        is_final = bool(isinstance(data, dict) and data.get("is_final"))
        return ToolResult(
            ok=ok,
            observation=data if ok else body.get("message"),
            raw=body,
            error=None if ok else body.get("message"),
            code=code,
            is_final=is_final,
            elapsed_ms=meta.get("execution_time_ms"),
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass
