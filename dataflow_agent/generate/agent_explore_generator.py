"""
AgentExploreGenerator -- drive an LLM agent through a sandbox to synthesize
multi-step exploration trajectories.

For each input row the operator runs an independent episode:

    1. Build a system prompt advertising the sandbox's tool catalog.
    2. Loop up to ``max_steps``:
         a. Ask the LLM for the next action (JSON ``{"tool", "args", "thought"}``).
         b. Parse it; if the tool is the terminal ``finish`` tool, stop.
         c. Otherwise call ``sandbox.execute(tool, args)`` and feed the
            observation back into the running message history.
    3. Emit a structured trajectory (list of {thought, action, observation}).

The operator depends only on :class:`SandboxClientABC` and
:class:`LLMServingABC`, so it is agnostic to which sandbox backend is wired in.
Episodes run concurrently via a thread pool because they are I/O bound (LLM +
sandbox HTTP), matching DataFlow's existing ``APILLMServing_request`` pattern.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from dataflow_agent.sandbox import SandboxClientABC, ToolResult, ToolSchema


_DEFAULT_SYSTEM_PROMPT = """You are an autonomous agent solving a task by calling tools in a sandbox.

You have access to the following tools:
{tool_catalog}

At every step respond with a SINGLE JSON object and nothing else:
{{"thought": "<your reasoning>", "tool": "<tool name>", "args": {{<arguments>}}}}

When you have enough information to answer, call the special tool "finish":
{{"thought": "<why you are done>", "tool": "finish", "args": {{"answer": "<final answer>"}}}}

Rules:
- Output ONLY the JSON object, no markdown fences, no extra prose.
- Use exactly the tool names listed above (or "finish").
- Keep "args" consistent with each tool's parameters.
"""

_FINISH_TOOL = "finish"


@OPERATOR_REGISTRY.register()
class AgentExploreGenerator(OperatorABC):
    """Generate agentic exploration trajectories against a pluggable sandbox.

    Scope: this is a **text / structured-domain** explorer. It works for any
    domain whose observations are text or JSON-serializable structured data --
    web search, RAG retrieval, SQL (text2sql), document QA, data-science
    (read_csv / run_python). It is NOT designed for image/binary observations
    (e.g. GUI/VM ``screenshot`` returns base64) because the loop feeds
    observations back as text and ``LLMServingABC`` has no image channel. Use a
    dedicated multimodal operator for those domains.

    Args:
        llm_serving: Any :class:`LLMServingABC` used to pick the next action.
        sandbox: Any :class:`SandboxClientABC` backend (AgentFlow / mock / your
            own). The operator never assumes a concrete sandbox.
        domain: Sandbox domain to explore (``"web"``, ``"rag"``, ``"sql"``,
            ``"doc"``, ``"ds"``, ...). Passed to session creation and tool listing.
        max_steps: Maximum tool calls per episode before forced termination.
        max_workers: Thread-pool size for concurrent episodes.
        system_prompt: Optional override of the agent system prompt. Must
            contain a ``{tool_catalog}`` placeholder.
        include_tool_catalog: When True, fetch ``sandbox.list_tools(domain)`` and
            render it into the system prompt. Set False if you want to advertise
            tools yourself via ``system_prompt``.
        max_observation_chars: Truncate each observation to this many chars
            before feeding it back into context (guards the context window).
        validate_tool_names: Reject tool calls not in the sandbox's catalog,
            recording them as ``invalid_tool`` steps so the agent self-corrects.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        sandbox: SandboxClientABC = None,
        domain: str = "web",
        max_steps: int = 10,
        max_workers: int = 8,
        system_prompt: Optional[str] = None,
        include_tool_catalog: bool = True,
        max_observation_chars: int = 8000,
        validate_tool_names: bool = True,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.sandbox = sandbox
        self.domain = domain
        self.max_steps = max_steps
        self.max_workers = max_workers
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self.include_tool_catalog = include_tool_catalog
        # Cap how much of an observation is fed back into the LLM context. Text
        # and structured domains (web/rag/sql/doc/ds) produce bounded text, but a
        # rogue tool (or a large table / page dump) can blow up the context
        # window; truncate defensively. (Binary/image observations from GUI/VM
        # domains are out of scope for this text-only operator -- see README.)
        self.max_observation_chars = max_observation_chars
        # When True, reject tool calls whose name is not in the sandbox's
        # advertised catalog (recorded as an invalid_tool step instead of being
        # blindly forwarded). Disable if you advertise tools out-of-band.
        self.validate_tool_names = validate_tool_names

    @staticmethod
    def get_desc(lang: str = "zh"):
        if lang == "zh":
            return (
                "该算子驱动 LLM 智能体在可插拔沙箱中进行多步探索，合成 agent 轨迹数据。\n\n"
                "输入参数：\n"
                "- llm_serving: 用于决策下一步动作的 LLM 服务（LLMServingABC）\n"
                "- sandbox: 沙箱后端（SandboxClientABC，如 AgentFlow / Mock / 自研）\n"
                "- domain: 沙箱域（web/rag/vm/sql/doc 等）\n"
                "- max_steps: 单条任务最大工具调用步数\n"
                "- max_workers: 并发 episode 线程数\n\n"
                "运行参数（run）：\n"
                "- input_key: 任务/查询字段名（默认 \"query\"）\n"
                "- output_key: 输出轨迹字段名（默认 \"trajectory\"）\n\n"
                "输出：每行写入一条结构化轨迹 {task, steps:[{thought,action,observation}], "
                "final_answer, num_steps, success}。"
            )
        return (
            "Drives an LLM agent through a pluggable sandbox to synthesize "
            "multi-step exploration trajectories.\n\n"
            "Init args: llm_serving (LLMServingABC), sandbox (SandboxClientABC), "
            "domain, max_steps, max_workers.\n"
            "Run args: input_key (default 'query'), output_key (default "
            "'trajectory').\n"
            "Output: one structured trajectory per row "
            "{task, steps, final_answer, num_steps, success}."
        )

    # ------------------------------------------------------------------ #
    # tool-call parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """Best-effort extraction of a single JSON action object from LLM text.

        Handles bare JSON, ```json fenced blocks, and trailing prose by
        scanning for the first balanced ``{...}`` object.
        """
        if not text:
            return None
        # strip markdown fences if present
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            # find first balanced brace block
            start = text.find("{")
            if start == -1:
                return None
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        break
        if candidate is None:
            return None
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    def _list_tools(self) -> List["ToolSchema"]:
        """Fetch the sandbox tool catalog, degrading to [] on failure."""
        if not (self.include_tool_catalog and self.sandbox is not None):
            return []
        try:
            return self.sandbox.list_tools(self.domain)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self.logger.warning(f"[AgentExploreGenerator] list_tools failed: {exc}")
            return []

    def _render_tool_catalog(self, tools: List["ToolSchema"]) -> str:
        if not (self.include_tool_catalog and self.sandbox is not None):
            return "(tools described above)"
        lines = []
        for t in tools:
            param_names = ", ".join(p.get("name", "?") for p in t.parameters)
            lines.append(f"- {t.name}({param_names}): {t.description}")
        return "\n".join(lines) if lines else "(no tools reported)"

    def _truncate_observation(self, observation: Any) -> Any:
        """Bound observation size before feeding it back into LLM context.

        Keeps small observations untouched; stringifies and truncates large
        ones with a marker so the agent knows content was elided.
        """
        try:
            text = observation if isinstance(observation, str) \
                else json.dumps(observation, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(observation)
        if len(text) <= self.max_observation_chars:
            return observation
        head = text[: self.max_observation_chars]
        return (
            f"{head}\n...[truncated {len(text) - self.max_observation_chars} "
            f"chars of {len(text)} total]"
        )

    # ------------------------------------------------------------------ #
    # single episode
    # ------------------------------------------------------------------ #
    def _run_episode(
        self,
        task: str,
        system_prompt: str,
        known_tools: Optional[set] = None,
    ) -> Dict[str, Any]:
        worker_id = self.sandbox.new_worker_id() if self.sandbox else None
        session_id = None
        if self.sandbox and getattr(self.sandbox, "stateful", False):
            try:
                session_id = self.sandbox.create_session(self.domain, worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"[AgentExploreGenerator] create_session: {exc}")

        steps: List[Dict[str, Any]] = []
        final_answer: Optional[str] = None
        success = False
        # running conversation context fed back to the LLM each turn
        history = f"Task: {task}\n"

        try:
            for step_idx in range(self.max_steps):
                user_input = (
                    f"{history}\n"
                    f"(step {step_idx + 1}/{self.max_steps}) "
                    f"Respond with the next action JSON."
                )
                # LLMServingABC is batch-oriented; call with a singleton list.
                responses = self.llm_serving.generate_from_input(
                    [user_input], system_prompt
                )
                raw = responses[0] if responses else ""
                action = self._extract_json(raw)

                if action is None:
                    steps.append({
                        "thought": None,
                        "action": {"tool": None, "args": {}},
                        "observation": None,
                        "parse_error": True,
                        "raw_response": raw,
                    })
                    history += f"\n[step {step_idx + 1}] (unparseable response, retrying)"
                    continue

                tool = action.get("tool")
                args = action.get("args", {}) or {}
                thought = action.get("thought")

                if tool == _FINISH_TOOL:
                    final_answer = args.get("answer")
                    success = True
                    steps.append({
                        "thought": thought,
                        "action": {"tool": tool, "args": args},
                        "observation": {"answer": final_answer},
                    })
                    break

                # Reject hallucinated tool names instead of forwarding them to
                # the sandbox (which would 4xx). Record it and let the agent
                # self-correct next turn.
                if (self.validate_tool_names and known_tools is not None
                        and tool not in known_tools):
                    steps.append({
                        "thought": thought,
                        "action": {"tool": tool, "args": args},
                        "observation": None,
                        "invalid_tool": True,
                    })
                    history += (
                        f"\n[step {step_idx + 1}] invalid tool '{tool}'. "
                        f"Available tools: {sorted(known_tools)}. Pick one of them."
                    )
                    continue

                result: ToolResult = self.sandbox.execute(
                    tool, args, worker_id=worker_id
                )
                observation = self._truncate_observation(result.observation) \
                    if result.ok else result.observation
                steps.append({
                    "thought": thought,
                    "action": {"tool": tool, "args": args},
                    "observation": observation,
                    "ok": result.ok,
                    "error": result.error,
                })
                obs_str = json.dumps(observation, ensure_ascii=False) \
                    if result.ok else f"ERROR: {result.error}"
                history += (
                    f"\n[step {step_idx + 1}] tool={tool} args={json.dumps(args, ensure_ascii=False)}"
                    f"\nobservation: {obs_str}"
                )
                if result.is_final:
                    success = True
                    break
        finally:
            if session_id is not None:
                self.sandbox.destroy_session(self.domain, worker_id=worker_id)

        return {
            "task": task,
            "steps": steps,
            "final_answer": final_answer,
            "num_steps": len(steps),
            "success": success,
        }

    # ------------------------------------------------------------------ #
    # operator entrypoint
    # ------------------------------------------------------------------ #
    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "query",
        output_key: str = "trajectory",
    ):
        if self.llm_serving is None:
            raise ValueError("AgentExploreGenerator requires an llm_serving instance.")
        if self.sandbox is None:
            raise ValueError("AgentExploreGenerator requires a sandbox instance.")

        df: pd.DataFrame = storage.read(output_type="dataframe")
        if input_key not in df.columns:
            raise KeyError(
                f"input_key '{input_key}' not found in columns: {list(df.columns)}"
            )

        if not self.sandbox.health_check():
            self.logger.warning(
                "[AgentExploreGenerator] sandbox health_check returned False; "
                "proceeding anyway (it may still serve requests)."
            )

        # Fetch the tool catalog once and reuse it: render into the prompt and
        # build the whitelist for tool-name validation.
        tools = self._list_tools()
        known_tools = {t.name for t in tools} | {_FINISH_TOOL} if tools else None
        system_prompt = self.system_prompt.format(
            tool_catalog=self._render_tool_catalog(tools)
        )
        tasks = [str(t) for t in df[input_key].tolist()]
        results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_idx = {
                pool.submit(self._run_episode, task, system_prompt, known_tools): i
                for i, task in enumerate(tasks)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001 - isolate per-episode failure
                    self.logger.error(
                        f"[AgentExploreGenerator] episode {idx} failed: {exc}"
                    )
                    results[idx] = {
                        "task": tasks[idx],
                        "steps": [],
                        "final_answer": None,
                        "num_steps": 0,
                        "success": False,
                        "error": str(exc),
                    }

        n_success = sum(1 for r in results if r and r.get("success"))
        self.logger.info(
            f"[AgentExploreGenerator] {n_success}/{len(tasks)} episodes succeeded "
            f"(domain={self.domain}, max_steps={self.max_steps})."
        )

        df[output_key] = results
        storage.write(df)
        return [output_key]
