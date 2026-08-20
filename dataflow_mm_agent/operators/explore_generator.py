"""Official AgentExploreGenerator logic adapted to controlled MM environments."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage

from ..env.registry import get_environment_bundle, load_scenario, resolve_scenario
from ..contracts import Content
from ..contracts.environment import Scenario
from ..contracts.trajectory import Trajectory
from ..runtime_components import AgentRollout, RolloutConfig
from ..serving import ModelServing
from ..storage import TrajectoryStore


@OPERATOR_REGISTRY.register()
class AgentMMExploreGenerator(OperatorABC):
    """Generate one controlled multimodal exploration trajectory per row."""

    def __init__(
        self,
        serving: ModelServing | None = None,
        *,
        max_steps: int | None = None,
        max_workers: int = 8,
        system_prompt: str | None = None,
        include_tool_catalog: bool = True,
        max_observation_chars: int = 8000,
        validate_tool_names: bool = True,
        include_host_tools: bool = False,
        verify_during_rollout: bool = True,
        trajectory_dir: str | Path | None = None,
    ):
        if not isinstance(include_host_tools, bool):
            raise TypeError("include_host_tools must be a boolean")
        self.logger = get_logger()
        self.serving = serving
        self.max_steps = max_steps
        self.max_workers = max_workers
        self.system_prompt = system_prompt
        self.include_tool_catalog = include_tool_catalog
        self.max_observation_chars = max_observation_chars
        self.validate_tool_names = validate_tool_names
        self.include_host_tools = include_host_tools
        self.verify_during_rollout = verify_during_rollout
        self.trajectory_dir = Path(trajectory_dir) if trajectory_dir else None

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return "驱动 VLM 智能体在受控多模态 Env 中探索，并生成 message-index JSONL 轨迹。"
        return "Drives a VLM agent through controlled multimodal environments."

    def _run_scenario(
        self,
        scenario: Scenario,
        *,
        instruction_content: tuple[Content, ...] | None = None,
    ) -> Trajectory:
        if self.serving is None:
            raise ValueError("AgentMMExploreGenerator requires a serving instance")
        bundle = get_environment_bundle(scenario.env_id)
        defaults = RolloutConfig()
        config = RolloutConfig(
            max_steps=self.max_steps,
            system_prompt=self.system_prompt or defaults.system_prompt,
            include_host_tools=self.include_host_tools,
            include_tool_catalog=self.include_tool_catalog,
            validate_tool_names=self.validate_tool_names,
            max_observation_chars=self.max_observation_chars,
        )
        return AgentRollout(
            serving=self.serving,
            env_factory=bundle.env_factory,
            verifier=bundle.verifier if self.verify_during_rollout else None,
            config=config,
        ).run(scenario, instruction_content=instruction_content)

    def _run_record(
        self,
        record: dict[str, Any],
        *,
        env_key: str,
        task_key: str,
        input_key: str | None,
        scenario_key: str | None,
    ) -> Trajectory:
        if scenario_key is not None:
            raw_scenario = record.get(scenario_key)
            if not isinstance(raw_scenario, dict):
                raise TypeError(f"{scenario_key!r} must contain a Scenario object")
            scenario = resolve_scenario(Scenario.from_dict(raw_scenario))
        else:
            scenario = load_scenario(
                str(record[env_key]),
                str(record[task_key]),
            )
        if input_key and record.get(input_key) is not None:
            scenario = replace(scenario, instruction=str(record[input_key]))
        return self._run_scenario(scenario)

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str | None = None,
        output_key: str = "trajectory",
        env_key: str = "env_id",
        task_key: str = "task_id",
        scenario_key: str | None = None,
    ):
        if self.serving is None:
            raise ValueError("AgentMMExploreGenerator requires a serving instance")
        dataframe = storage.read(output_type="dataframe")
        required_keys = (
            (scenario_key,)
            if scenario_key is not None
            else (env_key, task_key)
        )
        for required in required_keys:
            if required not in dataframe.columns:
                raise KeyError(f"missing required input column: {required}")
        records = dataframe.to_dict(orient="records")
        results: list[Trajectory | None] = [None] * len(records)

        if self.max_workers == 1:
            results = [
                self._run_record(
                    record,
                    env_key=env_key,
                    task_key=task_key,
                    input_key=input_key,
                    scenario_key=scenario_key,
                )
                for record in records
            ]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(
                        self._run_record,
                        record,
                        env_key=env_key,
                        task_key=task_key,
                        input_key=input_key,
                        scenario_key=scenario_key,
                    ): index
                    for index, record in enumerate(records)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:  # isolate episode failures
                        self.logger.error(
                            f"[AgentMMExploreGenerator] episode {index} failed: {exc}"
                        )
                        if scenario_key is not None:
                            raw_scenario = records[index].get(scenario_key)
                            if not isinstance(raw_scenario, dict):
                                raise TypeError(
                                    f"{scenario_key!r} must contain a Scenario object"
                                )
                            scenario = Scenario.from_dict(raw_scenario)
                        else:
                            scenario = load_scenario(
                                str(records[index][env_key]),
                                str(records[index][task_key]),
                            )
                        if input_key and records[index].get(input_key) is not None:
                            scenario = replace(
                                scenario, instruction=str(records[index][input_key])
                            )
                        results[index] = Trajectory(
                            episode_id=f"{scenario.task_id}-failed-{index}",
                            scenario=scenario,
                            messages=(),
                            steps=(),
                            final_answer=None,
                            termination_reason="infrastructure_error",
                            started_at="",
                            completed_at="",
                            metadata={"error": str(exc)},
                        )

        trajectories = [item for item in results if item is not None]
        if self.trajectory_dir is not None:
            self.trajectory_dir.mkdir(parents=True, exist_ok=True)
            store = TrajectoryStore()
            outputs: list[Any] = []
            for trajectory in trajectories:
                destination = self.trajectory_dir / f"{trajectory.episode_id}.jsonl"
                outputs.append(str(store.save(trajectory, destination)))
        else:
            outputs = [trajectory.to_dict() for trajectory in trajectories]
        dataframe[output_key] = outputs
        storage.write(dataframe)
        successful = sum(item.success for item in trajectories)
        self.logger.info(
            f"[AgentMMExploreGenerator] {successful}/{len(trajectories)} "
            "episodes completed normally"
        )
        return [output_key]
