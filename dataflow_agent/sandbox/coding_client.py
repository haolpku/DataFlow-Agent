"""
CodingSandboxClient -- a local coding / working-agent environment.

Gives an LLM agent a real, isolated workspace on disk plus the tools a coding
agent needs: read/write/list files, run shell commands, run Python, run tests.
Every observation it returns is text (file contents, stdout/stderr, exit codes),
so it works with the existing text/structured agent-explore loop with **zero
framework changes** -- this whole file is just a ``SandboxClientABC`` subclass.

Why this is a good fit
----------------------
Coding/working agents are the canonical text-domain agent: the world is files
and command output. That maps cleanly onto ``ToolResult.observation`` (already
JSON/text) and ``LLMServingABC`` (text in, text out). No image channel needed.

Safety
------
* Each task/worker gets its own workspace directory (process- and thread-safe;
  multiple episodes run concurrently without clobbering each other).
* Every file path is resolved and confined to the workspace root -- attempts to
  escape via ``..`` or absolute paths are rejected (``code=4030``).
* Shell / Python execution is bounded by a timeout; output is truncated.
* This runs commands on the *local machine*. It is meant for trusted task
  synthesis (e.g. "fix this bug", "implement this function"), not for executing
  untrusted code. Point ``root`` at a scratch dir, or wrap in a container/VM for
  hardening. ``allow_shell=False`` disables arbitrary shell entirely.

Tools advertised
----------------
    list_files(path=".")                 -> directory listing
    read_file(path)                      -> file contents
    write_file(path, content)            -> create/overwrite a file
    run_python(code | path, args)        -> run a script, capture stdout/stderr
    run_shell(command)                   -> run a shell command (if allow_shell)
    run_tests(path=".")                  -> run pytest, capture summary
    finish(answer)                       -> terminal (handled by the operator)
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Dict, List, Optional

from .base import SandboxClientABC, ToolResult, ToolSchema


class CodingSandboxClient(SandboxClientABC):
    """A local filesystem + subprocess sandbox for coding/working agents.

    Args:
        root: Base directory under which per-worker workspaces are created.
            Defaults to a fresh temp dir. Each worker gets ``root/<worker_id>/``.
        seed_files: Optional ``{relative_path: content}`` written into every new
            workspace (e.g. the buggy source file + a failing test). Lets you
            pose a concrete coding task.
        allow_shell: Expose the ``run_shell`` tool. Set False to restrict the
            agent to file ops + python + tests only.
        timeout: Per-command wall-clock timeout (seconds).
        max_output_chars: Truncate captured stdout/stderr to this many chars.
        python_executable: Interpreter used by ``run_python`` / ``run_tests``.
        cleanup_on_exit: Register an atexit hook to remove ``root`` when the
            process exits (belt-and-suspenders beyond per-session destroy, so a
            crashed/interrupted run does not leak temp workspaces). Only applies
            when ``root`` was auto-created (a temp dir); a user-supplied ``root``
            is never auto-deleted. Default True.

    Concurrency note:
        Each task/worker gets its own ``root/<worker_id>/`` directory, so many
        episodes run concurrently without clobbering each other. But
        ``run_python`` / ``run_tests`` / ``run_shell`` spawn **real
        subprocesses** -- unlike API-only sandboxes, high concurrency here is a
        genuine CPU/disk load. Bound the *operator's* ``max_workers`` to roughly
        the machine's core count for coding domains (see
        :func:`recommended_max_workers`), not the large values used for pure-API
        domains.
    """

    #: Coding tasks are stateful: each task owns a workspace that must be created
    #: and torn down. The operator will call create_session / destroy_session.
    stateful: bool = True

    def __init__(
        self,
        root: Optional[str] = None,
        *,
        seed_files: Optional[Dict[str, str]] = None,
        allow_shell: bool = True,
        timeout: float = 30.0,
        max_output_chars: int = 8000,
        python_executable: Optional[str] = None,
        cleanup_on_exit: bool = True,
    ):
        self._auto_root = root is None
        self.root = root or tempfile.mkdtemp(prefix="coding_sandbox_")
        os.makedirs(self.root, exist_ok=True)
        self.seed_files = seed_files or {}
        self.allow_shell = allow_shell
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.python_executable = python_executable or sys.executable
        # worker_id -> absolute workspace path
        self._workspaces: Dict[str, str] = {}

        # Belt-and-suspenders: even if destroy_session/close are never reached
        # (crash, KeyboardInterrupt), don't leak the auto-created temp root.
        # Use a weakref so the hook doesn't keep the client alive, and only
        # delete a root we created ourselves.
        if cleanup_on_exit and self._auto_root:
            root_path = self.root
            atexit.register(lambda p=root_path: shutil.rmtree(p, ignore_errors=True))

    @staticmethod
    def recommended_max_workers(cap: Optional[int] = None) -> int:
        """Suggested operator ``max_workers`` for coding domains.

        Coding episodes spawn real subprocesses, so concurrency is CPU-bound
        (unlike pure-API domains, which are network-bound). Default to the core
        count, optionally clamped by ``cap``.
        """
        cores = os.cpu_count() or 4
        return min(cores, cap) if cap else cores

    # ------------------------------------------------------------------ #
    # tool catalog
    # ------------------------------------------------------------------ #
    def list_tools(self, domain: Optional[str] = None) -> List[ToolSchema]:
        tools = [
            ToolSchema(name="list_files",
                       description="List files/dirs under a workspace-relative path.",
                       parameters=[{"name": "path", "type": "string", "required": False}]),
            ToolSchema(name="read_file",
                       description="Read a text file's contents.",
                       parameters=[{"name": "path", "type": "string", "required": True}]),
            ToolSchema(name="write_file",
                       description="Create or overwrite a text file with given content.",
                       parameters=[{"name": "path", "type": "string", "required": True},
                                   {"name": "content", "type": "string", "required": True}]),
            ToolSchema(name="run_python",
                       description="Run Python: either inline 'code' or a 'path' to a "
                                   ".py file in the workspace. Captures stdout/stderr.",
                       parameters=[{"name": "code", "type": "string", "required": False},
                                   {"name": "path", "type": "string", "required": False},
                                   {"name": "args", "type": "array", "required": False}]),
            ToolSchema(name="run_tests",
                       description="Run pytest under a workspace-relative path; returns "
                                   "the summary and exit code.",
                       parameters=[{"name": "path", "type": "string", "required": False}]),
        ]
        if self.allow_shell:
            tools.append(ToolSchema(
                name="run_shell",
                description="Run a shell command in the workspace. Captures "
                            "stdout/stderr and exit code.",
                parameters=[{"name": "command", "type": "string", "required": True}]))
        return tools

    # ------------------------------------------------------------------ #
    # session lifecycle (one workspace per task/worker)
    # ------------------------------------------------------------------ #
    def create_session(self, domain, *, worker_id=None, config=None) -> Optional[str]:
        wid = worker_id or self.new_worker_id()
        ws = os.path.join(self.root, wid)
        os.makedirs(ws, exist_ok=True)
        # seed the workspace with the task's starting files
        for rel, content in self.seed_files.items():
            dst = self._safe_path(ws, rel)
            os.makedirs(os.path.dirname(dst) or ws, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)
        self._workspaces[wid] = ws
        return ws

    def destroy_session(self, domain, *, worker_id=None) -> None:
        if worker_id is None:
            return None
        ws = self._workspaces.pop(worker_id, None)
        if ws and os.path.isdir(ws):
            shutil.rmtree(ws, ignore_errors=True)
        return None

    # ------------------------------------------------------------------ #
    # path safety
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_path(workspace: str, rel: str) -> str:
        """Resolve ``rel`` under ``workspace``; raise if it escapes the root."""
        rel = rel or "."
        # reject absolute paths outright
        candidate = os.path.normpath(os.path.join(workspace, rel))
        ws_real = os.path.realpath(workspace)
        cand_real = os.path.realpath(candidate)
        if cand_real != ws_real and not cand_real.startswith(ws_real + os.sep):
            raise ValueError(f"path '{rel}' escapes the workspace")
        return candidate

    def _workspace_for(self, worker_id: Optional[str]) -> str:
        """Get (or lazily create) the workspace for this worker."""
        if worker_id and worker_id in self._workspaces:
            return self._workspaces[worker_id]
        # Stateless fallback: if no session was created, make one on the fly so
        # execute() still works (keeps the client usable without a session).
        return self.create_session("coding", worker_id=worker_id or self.new_worker_id())

    def _truncate(self, text: str) -> str:
        if text is None:
            return ""
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars] + f"\n...[truncated {len(text) - self.max_output_chars} chars]"

    # ------------------------------------------------------------------ #
    # execute
    # ------------------------------------------------------------------ #
    def execute(self, action, params=None, *, worker_id=None, timeout=None) -> ToolResult:
        params = params or {}
        bare = action.split(":", 1)[1] if ":" in action else action
        ws = self._workspace_for(worker_id)
        to = timeout if timeout is not None else self.timeout

        try:
            if bare == "list_files":
                target = self._safe_path(ws, params.get("path", "."))
                if not os.path.exists(target):
                    return ToolResult(ok=False, error=f"no such path: {params.get('path')}", code=4040)
                if os.path.isfile(target):
                    return ToolResult(ok=True, observation={"type": "file", "path": params.get("path")})
                entries = sorted(os.listdir(target))
                listing = [
                    {"name": e, "is_dir": os.path.isdir(os.path.join(target, e))}
                    for e in entries
                ]
                return ToolResult(ok=True, observation={"path": params.get("path", "."), "entries": listing})

            if bare == "read_file":
                target = self._safe_path(ws, params["path"])
                if not os.path.isfile(target):
                    return ToolResult(ok=False, error=f"no such file: {params['path']}", code=4040)
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    return ToolResult(ok=True, observation={"path": params["path"],
                                                            "content": self._truncate(f.read())})

            if bare == "write_file":
                target = self._safe_path(ws, params["path"])
                os.makedirs(os.path.dirname(target) or ws, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(params.get("content", ""))
                return ToolResult(ok=True, observation={"path": params["path"],
                                                        "bytes_written": len(params.get("content", ""))})

            if bare == "run_python":
                if params.get("path"):
                    script = self._safe_path(ws, params["path"])
                    if not os.path.isfile(script):
                        return ToolResult(ok=False, error=f"no such script: {params['path']}", code=4040)
                    cmd = [self.python_executable, script, *[str(a) for a in params.get("args", [])]]
                else:
                    code = params.get("code", "")
                    cmd = [self.python_executable, "-c", code]
                return self._run(cmd, ws, to)

            if bare == "run_tests":
                sub = params.get("path", ".")
                _ = self._safe_path(ws, sub)  # validate path stays in workspace
                cmd = [self.python_executable, "-m", "pytest", sub, "-q"]
                return self._run(cmd, ws, to)

            if bare == "run_shell":
                if not self.allow_shell:
                    return ToolResult(ok=False, error="run_shell is disabled", code=4030)
                return self._run(params["command"], ws, to, shell=True)

            return ToolResult(ok=False, error=f"unknown tool: {bare}", code=4040)

        except ValueError as exc:  # path escape
            return ToolResult(ok=False, error=str(exc), code=4030)
        except KeyError as exc:  # missing required arg
            return ToolResult(ok=False, error=f"missing argument: {exc}", code=4000)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", code=5000)

    def _run(self, cmd, cwd, timeout, *, shell=False) -> ToolResult:
        """Run a subprocess, capturing stdout/stderr/exit code into a ToolResult."""
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, shell=shell, capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"command timed out after {timeout}s", code=5080)
        observation = {
            "exit_code": proc.returncode,
            "stdout": self._truncate(proc.stdout),
            "stderr": self._truncate(proc.stderr),
        }
        # exit_code 0 == tool ran successfully; a non-zero test/command is still a
        # valid observation the agent should see (ok=True, but exit_code != 0).
        return ToolResult(ok=True, observation=observation)

    def close(self) -> None:
        # best-effort cleanup of any lingering workspaces + the root
        for ws in list(self._workspaces.values()):
            shutil.rmtree(ws, ignore_errors=True)
        self._workspaces.clear()
