"""Replay trajectories in fresh environments and verify their resulting state."""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from ..contracts import ContentLimits
from ..contracts.environment import VerificationResult
from ..contracts.trajectory import Trajectory
from ..env.registry import ENVIRONMENTS, resolve_scenario
from ..runtime_components import AgentRollout, HostPolicy, HostTools
from .utils.trajectory import as_trajectory


@OPERATOR_REGISTRY.register()
class AgentMMTrajectoryVerifier(OperatorABC):
    """Verify each trajectory by replaying its actions in a fresh Env."""

    def __init__(
        self,
        *,
        max_workers: int = 8,
        include_host_tools: bool = True,
        workspace_root: str | Path | None = None,
        content_limits: ContentLimits = ContentLimits(),
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.logger = get_logger()
        self.max_workers = max_workers
        self.include_host_tools = include_host_tools
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.content_limits = content_limits

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return "在 fresh Env 中重放轨迹工具调用，并以环境终态执行确定性验证。"
        return "Replays trajectory actions in a fresh Env and verifies the final state."

    @staticmethod
    def _replay_failure(detail: str) -> VerificationResult:
        return VerificationResult.from_reached_goal(
            False,
            detail=detail,
            reason=detail,
        )

    @classmethod
    def _enforce_binary_goal(
        cls,
        result: VerificationResult,
    ) -> VerificationResult:
        valid = (
            isinstance(result, VerificationResult)
            and len(result.checks) == 1
            and result.checks[0].name == "reached_goal"
            and result.passed == result.checks[0].passed
            and result.reward == float(result.passed)
            and result.reward in {0.0, 1.0}
        )
        if valid:
            return result
        return cls._replay_failure(
            "verifier contract violation: expected one binary reached_goal check"
        )

    def _verify_one(self, value: Any) -> dict[str, Any]:
        trajectory = as_trajectory(value)
        if trajectory is None:
            raise ValueError("trajectory is not a canonical Agent-MM trajectory")

        scenario_value = trajectory.scenario
        try:
            scenario = resolve_scenario(scenario_value)
            bundle = ENVIRONMENTS[scenario.env_id]
        except (KeyError, TypeError, ValueError) as exc:
            return trajectory.with_verification(
                self._replay_failure(f"cannot resolve trajectory task: {exc}")
            ).to_dict()

        root = self.workspace_root
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)

        env = bundle.env_factory()
        snapshot: dict[str, Any] = {}
        try:
            if (
                getattr(env, "env_id", None) != bundle.spec.env_id
                or getattr(env, "spec", None) != bundle.spec
            ):
                raise ValueError("environment factory returned an instance outside its spec")
            with tempfile.TemporaryDirectory(
                prefix=f"agent-mm-verify-{scenario.env_id}-",
                dir=str(root) if root else None,
            ) as temporary:
                workspace = Path(temporary).resolve()
                host = (
                    HostTools(HostPolicy(
                        workspace=workspace,
                        content_limits=self.content_limits,
                    ))
                    if self.include_host_tools
                    else None
                )
                env_tools = {tool.name: tool for tool in env.tools()}
                host_tools = {tool.name: tool for tool in host.tools()} if host else {}
                tool_map = {**env_tools, **host_tools}

                reset_result = env.reset(scenario, workspace)
                if not reset_result.ok:
                    error = reset_result.error
                    detail = error.message if error else "environment reset failed"
                    return trajectory.with_verification(
                        self._replay_failure(detail)
                    ).to_dict()

                for step in trajectory.steps:
                    action = step.action
                    if not action:
                        continue
                    tool_name = str(action.get("tool") or "")
                    if tool_name == "finish":
                        break
                    args = action.get("args") or {}
                    spec = tool_map.get(tool_name)
                    if spec is None or not isinstance(args, dict):
                        continue
                    if AgentRollout._validate_args(spec, args) is not None:
                        continue
                    try:
                        result = (
                            host.call(tool_name, args)
                            if host and tool_name in host_tools
                            else env.call(tool_name, args)
                        )
                    except Exception:
                        # Rollout converts tool exceptions into failed observations and
                        # continues, so replay must preserve that state-transition rule.
                        continue
                    if result.is_final:
                        break

                snapshot = dict(env.snapshot())
                bundle.spec.validate_snapshot(snapshot)
        except Exception as exc:
            result = self._replay_failure(
                f"replay crashed ({type(exc).__name__}): {exc}"
            )
        else:
            try:
                verified = bundle.verifier.verify(scenario, trajectory, snapshot)
            except Exception as exc:
                result = self._replay_failure(
                    f"verifier crashed ({type(exc).__name__}): {exc}"
                )
            else:
                result = self._enforce_binary_goal(verified)
        finally:
            env.close()
        return trajectory.with_verification(result).to_dict()

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "trajectory",
        output_key: str = "trajectory",
    ):
        dataframe = storage.read(output_type="dataframe")
        if input_key not in dataframe.columns:
            raise KeyError(
                f"input_key {input_key!r} not found in columns: "
                f"{list(dataframe.columns)}"
            )
        values = dataframe[input_key].tolist()
        results: list[dict[str, Any] | None] = [None] * len(values)

        if self.max_workers == 1:
            results = [self._verify_one(value) for value in values]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(self._verify_one, value): index
                    for index, value in enumerate(values)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:
                        trajectory = as_trajectory(values[index])
                        if trajectory is None:
                            raise
                        results[index] = trajectory.with_verification(
                            self._replay_failure(
                                f"replay worker crashed ({type(exc).__name__}): {exc}"
                            )
                        ).to_dict()

        verified = [item for item in results if item is not None]
        dataframe[output_key] = verified
        storage.write(dataframe)
        passed = sum(
            bool(item.get("verification", {}).get("passed")) for item in verified
        )
        self.logger.info(
            f"[AgentMMTrajectoryVerifier] {passed}/{len(verified)} "
            "replayed trajectories verified successfully"
        )
        return [output_key]
