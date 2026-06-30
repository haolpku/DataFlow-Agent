"""
TrajectoryRefiner -- rewrite/repair low-quality agent trajectories.

This is the final stage of the agent-data-synthesis loop:

    Generator -> Evaluator -> Filter -> **Refiner**

``TrajectoryFilter`` simply *drops* failing trajectories. That is wasteful: a
trajectory that scored low (or never reached an answer) may be salvageable -- the
agent might just have taken a wrong turn. ``TrajectoryRefiner`` gives those
trajectories a second chance: it re-runs the agent loop on the same task, but
primes the agent with a short diagnosis of what went wrong last time, so it can
avoid the previous mistakes.

Design (kept consistent with the other operators):

* **Selective & cheap-first.** Only trajectories that fail a *trigger* are
  refined (``success == False`` or ``overall < score_threshold``). Good
  trajectories are passed through untouched -- no LLM cost.
* **Reuses the Generator.** The repair episode is a normal exploration episode,
  so the Refiner delegates to an internal ``AgentExploreGenerator`` rather than
  re-implementing the loop. Same sandbox, same parsing, same step semantics.
* **Backend-agnostic.** Depends only on ``LLMServingABC`` + ``SandboxClientABC``,
  exactly like the Generator.
* **Traceable.** Each row gets ``_refined`` (bool) and ``_refine_note`` (why it
  was/ wasn't refined) so the lineage is auditable. The original trajectory is
  preserved under ``original_key`` when a refine is attempted.

The Refiner does **not** score the result itself -- re-running the Evaluator on
the refined column is the clean way to decide whether the repair actually helped
(and to pick the better of original vs refined downstream).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from dataflow_agent.sandbox import SandboxClientABC
from dataflow_agent.generate.agent_explore_generator import (
    AgentExploreGenerator,
    _FINISH_TOOL,
    _DEFAULT_SYSTEM_PROMPT,
)


def _as_traj(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a stored trajectory (dict or JSON string) into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# Prepended to the task when re-running, so the agent sees its previous attempt
# and a concrete instruction to fix it.
_REFINE_PREAMBLE = """You previously attempted this task and the result was judged low quality.

--- YOUR PREVIOUS ATTEMPT ---
{prior}
--- END PREVIOUS ATTEMPT ---

Diagnosis of what went wrong: {diagnosis}

Now solve the task again, AVOIDING the mistakes above. Be more direct: choose the
right tool, pass correct arguments, do not loop, and call "finish" with a complete
final answer as soon as you can support it.

Task: {task}"""


@OPERATOR_REGISTRY.register()
class TrajectoryRefiner(OperatorABC):
    """Repair low-quality / failed agent trajectories by re-exploring.

    Trigger logic (a trajectory is refined when ANY enabled trigger fires):
        - refine_failed:   ``success`` is falsy.
        - score_threshold: an ``overall`` score column exists and is below this.

    Args:
        llm_serving: LLM driving the repair episode (``LLMServingABC``).
        sandbox: Sandbox backend (``SandboxClientABC``) -- same contract as the
            Generator.
        domain: Sandbox domain to explore.
        max_steps: Max tool calls per repair episode.
        max_workers: Thread-pool size for the internal Generator.
        refine_failed: Refine trajectories whose ``success`` is falsy.
        score_threshold: If set, refine trajectories whose score (read from
            ``score_key``) is strictly below this value. ``None`` disables the
            score trigger.
        score_key: Column holding the quality score (default ``traj_overall``,
            matching ``TrajectoryQualityEvaluator``'s ``output_key``).
        original_key: Column to stash the pre-refine trajectory under (for
            lineage / original-vs-refined comparison). ``None`` to skip.
        system_prompt: Optional override of the agent system prompt (must contain
            a ``{tool_catalog}`` placeholder, same as the Generator).
        max_prior_chars: Cap on the rendered previous-attempt transcript injected
            into the repair prompt (guards the context window).
        validate_tool_names: Forwarded to the internal Generator.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        sandbox: SandboxClientABC = None,
        domain: str = "web",
        max_steps: int = 10,
        max_workers: int = 8,
        refine_failed: bool = True,
        score_threshold: Optional[float] = 0.6,
        score_key: str = "traj_overall",
        original_key: Optional[str] = "trajectory_original",
        system_prompt: Optional[str] = None,
        max_prior_chars: int = 2000,
        validate_tool_names: bool = True,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.sandbox = sandbox
        self.domain = domain
        self.max_steps = max_steps
        self.max_workers = max_workers
        self.refine_failed = refine_failed
        self.score_threshold = score_threshold
        self.score_key = score_key
        self.original_key = original_key
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self.max_prior_chars = max_prior_chars
        self.validate_tool_names = validate_tool_names
        # Internal generator used to run the repair episodes (DRY: reuse the loop).
        self._gen = AgentExploreGenerator(
            llm_serving=llm_serving,
            sandbox=sandbox,
            domain=domain,
            max_steps=max_steps,
            max_workers=max_workers,
            system_prompt=self.system_prompt,
            validate_tool_names=validate_tool_names,
        )

    @staticmethod
    def get_desc(lang: str = "zh"):
        if lang == "zh":
            return (
                "该算子对低质量/失败的 agent 轨迹进行重写修复（Generator→Evaluator→"
                "Filter→Refiner 闭环的最后一环）。\n\n"
                "触发条件（满足任一则修复）：\n"
                "- refine_failed: success 为假\n"
                "- score_threshold: 质量分（score_key 列）低于该阈值\n\n"
                "做法：对命中的轨迹,带上「上一次尝试 + 失败诊断」重新跑一遍探索循环;"
                "好的轨迹原样保留(不调用 LLM)。\n\n"
                "输入参数：llm_serving、sandbox、domain、max_steps、score_threshold、"
                "score_key、original_key。\n"
                "运行参数：input_key(默认 \"trajectory\")、output_key(默认 \"trajectory\")。\n"
                "输出：原地写回修复后的轨迹,并加 _refined / _refine_note 列;"
                "原轨迹存到 original_key 列。Refiner 不自评分,建议下游重跑 Evaluator 择优。"
            )
        return (
            "Repairs low-quality / failed agent trajectories by re-running the "
            "exploration loop primed with a diagnosis of the previous attempt "
            "(the 'Refiner' stage of Generator->Evaluator->Filter->Refiner). "
            "Triggers: refine_failed (success falsy) and/or score_threshold "
            "(score_key column below threshold). Good trajectories pass through "
            "untouched (no LLM cost). Run args: input_key (default 'trajectory'), "
            "output_key (default 'trajectory'). Adds _refined / _refine_note "
            "columns and stashes the original under original_key."
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _diagnose(traj: Dict[str, Any]) -> str:
        """Heuristic, no-LLM diagnosis of why a trajectory was poor.

        Cheap and deterministic: scans the steps for the same failure modes the
        Filter knows about, and turns them into a short instruction the repair
        agent can act on.
        """
        if not traj.get("success"):
            ans = traj.get("final_answer")
            if ans is None or (isinstance(ans, str) and not ans.strip()):
                return ("the agent never produced a final answer (it ran out of "
                        "steps or stopped without calling finish).")
        notes: List[str] = []
        seen_actions: Dict[str, int] = {}
        for st in traj.get("steps") or []:
            if st.get("parse_error"):
                notes.append("one step produced an unparseable (non-JSON) response")
            if st.get("invalid_tool"):
                tool = (st.get("action") or {}).get("tool")
                notes.append(f"a non-existent tool '{tool}' was called")
            if st.get("ok") is False:
                err = st.get("error")
                notes.append(f"a tool call failed ({err})")
            action = st.get("action") or {}
            tool = action.get("tool")
            if tool and tool != _FINISH_TOOL:
                try:
                    key = f"{tool}:{json.dumps(action.get('args', {}), sort_keys=True, ensure_ascii=False)}"
                except (TypeError, ValueError):
                    key = f"{tool}:{action.get('args')}"
                seen_actions[key] = seen_actions.get(key, 0) + 1
        repeats = [k for k, c in seen_actions.items() if c > 1]
        if repeats:
            notes.append("the same action was repeated without progress (a loop)")
        if not notes:
            return ("the answer was judged incomplete or low quality; produce a "
                    "more correct and complete answer.")
        # dedupe while preserving order
        seen = set()
        uniq = [n for n in notes if not (n in seen or seen.add(n))]
        return "; ".join(uniq) + "."

    def _render_prior(self, traj: Dict[str, Any]) -> str:
        """Render the previous attempt compactly for the repair prompt."""
        lines = []
        for i, st in enumerate(traj.get("steps") or [], 1):
            action = st.get("action") or {}
            tool = action.get("tool")
            args = action.get("args", {})
            obs = st.get("observation")
            try:
                obs_str = obs if isinstance(obs, str) else json.dumps(obs, ensure_ascii=False)
            except (TypeError, ValueError):
                obs_str = str(obs)
            flag = ""
            if st.get("parse_error"):
                flag = " [PARSE_ERROR]"
            elif st.get("invalid_tool"):
                flag = " [INVALID_TOOL]"
            elif st.get("ok") is False:
                flag = " [TOOL_ERROR]"
            lines.append(
                f"  {i}. tool={tool} args={json.dumps(args, ensure_ascii=False)}{flag}"
            )
            lines.append(f"     observation: {obs_str}")
        lines.append(f"  final_answer: {traj.get('final_answer')}")
        text = "\n".join(lines)
        if len(text) > self.max_prior_chars:
            text = text[: self.max_prior_chars] + "\n  ...[truncated]"
        return text

    def _should_refine(self, traj: Optional[Dict[str, Any]], score: Any) -> bool:
        if traj is None:
            return False  # unparseable -> nothing to repair against
        if self.refine_failed and not traj.get("success"):
            return True
        if self.score_threshold is not None and score is not None:
            try:
                if float(score) < self.score_threshold:
                    return True
            except (TypeError, ValueError):
                return False
        return False

    # ------------------------------------------------------------------ #
    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        output_key: str = "trajectory",
    ):
        if self.llm_serving is None:
            raise ValueError("TrajectoryRefiner requires an llm_serving instance.")
        if self.sandbox is None:
            raise ValueError("TrajectoryRefiner requires a sandbox instance.")

        df: pd.DataFrame = storage.read("dataframe")
        if input_key not in df.columns:
            raise KeyError(
                f"input_key '{input_key}' not found in columns: {list(df.columns)}"
            )

        scores = df[self.score_key].tolist() if self.score_key in df.columns \
            else [None] * len(df)

        # Build the agent's system prompt + tool whitelist once (same as Generator).
        tools = self._gen._list_tools()
        known_tools = {t.name for t in tools} | {_FINISH_TOOL} if tools else None
        system_prompt = self.system_prompt.format(
            tool_catalog=self._gen._render_tool_catalog(tools)
        )

        new_trajs: List[Any] = []
        originals: List[Any] = []
        refined_flags: List[bool] = []
        notes: List[str] = []
        n_refined = 0
        n_improved_success = 0

        for value, score in zip(df[input_key].tolist(), scores):
            traj = _as_traj(value)
            if not self._should_refine(traj, score):
                new_trajs.append(value)
                originals.append(None)
                refined_flags.append(False)
                notes.append("kept (passed quality / unparseable)")
                continue

            diagnosis = self._diagnose(traj)
            prior = self._render_prior(traj)
            augmented_task = _REFINE_PREAMBLE.format(
                prior=prior, diagnosis=diagnosis, task=traj.get("task", ""),
            )
            try:
                repaired = self._gen._run_episode(
                    augmented_task, system_prompt, known_tools
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-row failure
                self.logger.error(f"[TrajectoryRefiner] refine failed: {exc}")
                new_trajs.append(value)
                originals.append(None)
                refined_flags.append(False)
                notes.append(f"refine_error: {exc}")
                continue

            # The repair episode's task is the augmented prompt; restore the
            # original task string so the refined trajectory is comparable.
            repaired["task"] = traj.get("task", "")
            new_trajs.append(repaired)
            originals.append(traj)
            refined_flags.append(True)
            notes.append(f"refined: {diagnosis}")
            n_refined += 1
            if repaired.get("success") and not traj.get("success"):
                n_improved_success += 1

        df[output_key] = new_trajs
        if self.original_key is not None:
            df[self.original_key] = originals
        df["_refined"] = refined_flags
        df["_refine_note"] = notes

        self.logger.info(
            f"[TrajectoryRefiner] refined {n_refined}/{len(df)} trajectories "
            f"(domain={self.domain}); {n_improved_success} went failed->success."
        )
        storage.write(df)
        return [output_key]
