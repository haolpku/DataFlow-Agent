"""DataFlow exploration operator over required v2 Tasks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from ..contracts import Message, Task, TaskResolver
from ..contracts.trajectory import Trajectory, utc_now
from ..runtime_components import AgentRollout, RolloutConfig
from ..serving import ModelServing
from ..storage import TrajectoryStore


@OPERATOR_REGISTRY.register()
class AgentMMExploreGenerator(OperatorABC):
    """Generate one tool-loop trajectory per explicitly resolved Task."""

    def __init__(
        self,
        serving: ModelServing | None = None,
        *,
        task_resolver: TaskResolver,
        max_steps: int = 64,
        max_workers: int = 8,
        system_prompt: str | None = None,
        include_tool_catalog: bool = True,
        max_observation_chars: int = 8000,
        validate_tool_names: bool = True,
        structured_actions: bool = False,
        action_format_retries: int = 0,
        include_host_tools: bool = False,
        trajectory_dir: str | Path | None = None,
        workspace_root: str | Path | None = None,
        workspace_retention: str = "ephemeral",
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.logger = get_logger()
        self.serving = serving
        self.task_resolver = task_resolver
        self.max_workers = max_workers
        defaults = RolloutConfig()
        self.config = RolloutConfig(
            max_steps=max_steps,
            system_prompt=system_prompt or defaults.system_prompt,
            include_host_tools=include_host_tools,
            include_tool_catalog=include_tool_catalog,
            validate_tool_names=validate_tool_names,
            structured_actions=structured_actions,
            action_format_retries=action_format_retries,
            max_observation_chars=max_observation_chars,
            workspace_root=Path(workspace_root) if workspace_root else None,
            workspace_retention=workspace_retention,  # type: ignore[arg-type]
        )
        self.trajectory_dir = Path(trajectory_dir) if trajectory_dir else None

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return "解析必需 Task，在轻量 Env 的统一 ToolLoop 中生成多模态轨迹。"
        return "Resolves required Tasks and generates multimodal ToolLoop trajectories."

    def _runner(self) -> AgentRollout:
        if self.serving is None:
            raise ValueError("AgentMMExploreGenerator requires a serving instance")
        return AgentRollout(serving=self.serving, config=self.config)

    def _task(
        self,
        record: dict[str, Any],
        *,
        env_key: str,
        task_key: str,
        input_key: str | None,
    ) -> Task:
        task = self.task_resolver.resolve(
            str(record[task_key]), env_id=str(record[env_key])
        )
        if input_key and record.get(input_key) is not None:
            task = replace(
                task,
                messages=(Message.text("user", str(record[input_key])),),
            )
        return task

    def _run_record(
        self,
        record: dict[str, Any],
        *,
        env_key: str,
        task_key: str,
        input_key: str | None,
    ) -> Trajectory:
        return self._runner().run(self._task(
            record, env_key=env_key, task_key=task_key, input_key=input_key
        ))

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str | None = None,
        output_key: str = "trajectory",
        env_key: str = "env_id",
        task_key: str = "task_id",
    ):
        if self.serving is None:
            raise ValueError("AgentMMExploreGenerator requires a serving instance")
        dataframe = storage.read(output_type="dataframe")
        for required in (env_key, task_key):
            if required not in dataframe.columns:
                raise KeyError(f"missing required input column: {required}")
        records = dataframe.to_dict(orient="records")
        results: list[Trajectory | None] = [None] * len(records)

        def failure(index: int, exc: Exception) -> Trajectory:
            task = self._task(
                records[index], env_key=env_key, task_key=task_key, input_key=input_key
            )
            timestamp = utc_now()
            return Trajectory(
                episode_id=f"{task.task_id}-failed-{index}",
                task_id=task.task_id,
                env_id=task.env_id,
                messages=task.messages,
                steps=(),
                final_answer=None,
                termination_reason="infrastructure_error",
                started_at=timestamp,
                completed_at=timestamp,
                metadata={"error": str(exc)},
            )

        if self.max_workers == 1:
            for index, record in enumerate(records):
                try:
                    results[index] = self._run_record(
                        record, env_key=env_key, task_key=task_key, input_key=input_key
                    )
                except Exception as exc:
                    self.logger.error(
                        f"[AgentMMExploreGenerator] episode {index} failed: {exc}"
                    )
                    results[index] = failure(index, exc)
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(
                        self._run_record,
                        record,
                        env_key=env_key,
                        task_key=task_key,
                        input_key=input_key,
                    ): index
                    for index, record in enumerate(records)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:
                        self.logger.error(
                            f"[AgentMMExploreGenerator] episode {index} failed: {exc}"
                        )
                        results[index] = failure(index, exc)

        trajectories = [item for item in results if item is not None]
        if self.trajectory_dir is not None:
            self.trajectory_dir.mkdir(parents=True, exist_ok=True)
            store = TrajectoryStore()
            outputs: list[Any] = [
                str(store.save(
                    trajectory,
                    self.trajectory_dir / f"{trajectory.episode_id}.jsonl",
                ))
                for trajectory in trajectories
            ]
        else:
            outputs = [trajectory.to_dict() for trajectory in trajectories]
        dataframe[output_key] = outputs
        storage.write(dataframe)
        self.logger.info(
            f"[AgentMMExploreGenerator] "
            f"{sum(item.success for item in trajectories)}/{len(trajectories)} completed"
        )
        return [output_key]


__all__ = ["AgentMMExploreGenerator"]
