#!/usr/bin/env python3
"""Run the self-contained PixelGames Agent-MM baseline."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from dataflow.utils.storage import DummyStorage
from dataflow_mm_agent.operators import (
    AgentMMExploreGenerator,
    AgentMMTrajectoryFilter,
    AgentMMTrajectoryQualityEvaluator,
    AgentMMTrajectoryRefiner,
    AgentMMTrajectorySelector,
    AgentMMTrajectoryVerifier,
)
from dataflow_mm_agent.operators.utils.trajectory import (
    as_trajectory_dict,
    normal_success,
)
from dataflow_mm_agent.serving import ModelServing, create_model_serving
from pixel_game import register as register_pixel_game


EXAMPLE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BaselineConfig:
    max_steps: int | None = None
    max_workers: int = 1
    judge_threshold: float = 0.6
    min_steps: int = 1
    max_selected: int = 4
    include_host_tools: bool = False


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        if os.environ.get("OPENAI_MODEL") and os.environ.get("OPENAI_BASE_URL"):
            return
        raise FileNotFoundError(
            f"missing {path}; copy .env.example to .env.local and fill the API values"
        )
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            raise ValueError(f"invalid .env.local entry at line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def run_operator(
    operator: Any,
    dataframe: pd.DataFrame,
    **kwargs: Any,
) -> pd.DataFrame:
    storage = DummyStorage()
    storage.set_data(dataframe.copy())
    operator.run(storage, **kwargs)
    return storage.read(output_type="dataframe")


def write_stage(dataframe: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_json(path, orient="records", lines=True, force_ascii=False)


def is_qualified(row: pd.Series, threshold: float) -> bool:
    trajectory = as_trajectory_dict(row.get("trajectory"))
    if trajectory is None or not normal_success(trajectory):
        return False
    verification = trajectory.get("verification")
    if not isinstance(verification, dict) or not verification.get("passed", False):
        return False
    try:
        return float(row.get("traj_overall")) >= threshold
    except (TypeError, ValueError):
        return False


def prepare_repair_inputs(
    dataframe: pd.DataFrame,
    threshold: float,
) -> pd.DataFrame:
    prepared = dataframe.copy()
    prepared["traj_overall_before_refine"] = prepared.get("traj_overall")
    trigger_score = threshold - max(abs(threshold), 1.0) * 1e-6
    for index, row in prepared.iterrows():
        trajectory = as_trajectory_dict(row.get("trajectory"))
        verification = (trajectory or {}).get("verification")
        if not isinstance(verification, dict) or not verification.get("passed", False):
            prepared.at[index, "traj_overall"] = trigger_score
    return prepared


def replay_verify(dataframe: pd.DataFrame, max_workers: int) -> pd.DataFrame:
    return run_operator(
        AgentMMTrajectoryVerifier(
            max_workers=max_workers,
            include_host_tools=False,
        ),
        dataframe,
    )


def run_baseline(
    inputs: pd.DataFrame,
    output_dir: Path,
    *,
    serving: ModelServing,
    config: BaselineConfig = BaselineConfig(),
) -> pd.DataFrame:
    """Generate, replay-verify, judge, repair, filter, and select trajectories."""
    output_dir.mkdir(parents=True, exist_ok=True)

    generated = run_operator(
        AgentMMExploreGenerator(
            serving=serving,
            max_steps=config.max_steps,
            max_workers=config.max_workers,
            include_host_tools=config.include_host_tools,
            verify_during_rollout=False,
        ),
        inputs,
    )
    write_stage(generated, output_dir / "01_generated.jsonl")

    verified = replay_verify(generated, config.max_workers)
    write_stage(verified, output_dir / "02_verified.jsonl")

    judged = run_operator(
        AgentMMTrajectoryQualityEvaluator(
            llm_serving=serving,
            max_workers=config.max_workers,
        ),
        verified,
    )
    write_stage(judged, output_dir / "03_judged.jsonl")

    qualified_mask = judged.apply(
        lambda row: is_qualified(row, config.judge_threshold), axis=1
    )
    qualified = judged[qualified_mask].reset_index(drop=True)
    repair_candidates = judged[~qualified_mask].reset_index(drop=True)
    write_stage(qualified, output_dir / "04_qualified_branch.jsonl")
    write_stage(repair_candidates, output_dir / "04_repair_branch.jsonl")

    filtered_good = run_operator(
        AgentMMTrajectoryFilter(min_steps=config.min_steps),
        qualified,
    )
    write_stage(filtered_good, output_dir / "05_filtered_qualified.jsonl")

    if repair_candidates.empty:
        refined = repair_candidates.copy()
        reverified = repair_candidates.copy()
        rejudged = repair_candidates.copy()
        filtered_repaired = repair_candidates.copy()
    else:
        refined = run_operator(
            AgentMMTrajectoryRefiner(
                llm_serving=serving,
                max_steps=config.max_steps,
                max_workers=config.max_workers,
                score_threshold=config.judge_threshold,
                include_host_tools=config.include_host_tools,
            ),
            prepare_repair_inputs(repair_candidates, config.judge_threshold),
        )
        reverified = replay_verify(refined, config.max_workers)
        rejudged = run_operator(
            AgentMMTrajectoryQualityEvaluator(
                llm_serving=serving,
                max_workers=config.max_workers,
            ),
            reverified,
        )
        requalified = rejudged[
            rejudged.apply(
                lambda row: is_qualified(row, config.judge_threshold), axis=1
            )
        ].reset_index(drop=True)
        filtered_repaired = run_operator(
            AgentMMTrajectoryFilter(min_steps=config.min_steps),
            requalified,
        )
    write_stage(refined, output_dir / "06_refined.jsonl")
    write_stage(reverified, output_dir / "07_reverified.jsonl")
    write_stage(rejudged, output_dir / "08_rejudged.jsonl")
    write_stage(filtered_repaired, output_dir / "09_filtered_repaired.jsonl")

    nonempty = [
        frame for frame in (filtered_good, filtered_repaired) if not frame.empty
    ]
    merged = (
        pd.concat(nonempty, ignore_index=True)
        if nonempty
        else judged.iloc[0:0].copy()
    )
    selected = run_operator(
        AgentMMTrajectorySelector(
            max_selected=config.max_selected,
            min_depth=1,
            path_similarity_threshold=0.7,
            mode="rows",
        ),
        merged,
        input_key="trajectory",
    )
    write_stage(selected, output_dir / "10_selected.jsonl")
    return selected


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--input", type=Path, default=EXAMPLE_DIR / "input.jsonl")
    value.add_argument("--output-dir", type=Path, default=EXAMPLE_DIR / "runs")
    value.add_argument("--max-steps", type=int)
    value.add_argument("--max-workers", type=int, default=1)
    value.add_argument("--max-tokens", type=int, default=2048)
    value.add_argument("--judge-threshold", type=float, default=0.6)
    value.add_argument("--max-selected", type=int, default=4)
    value.add_argument("--timeout", type=float, default=300)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    load_env_file(EXAMPLE_DIR / ".env.local")
    register_pixel_game()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not model or not base_url:
        raise ValueError("OPENAI_MODEL and OPENAI_BASE_URL are required")
    serving = create_model_serving(
        backend="openai",
        model=model,
        base_url=base_url,
        api_key=os.environ.get("OPENAI_API_KEY") or "EMPTY",
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        max_workers=args.max_workers,
    )
    inputs = pd.read_json(args.input, lines=True)
    selected = run_baseline(
        inputs,
        args.output_dir,
        serving=serving,
        config=BaselineConfig(
            max_steps=args.max_steps,
            max_workers=args.max_workers,
            judge_threshold=args.judge_threshold,
            max_selected=args.max_selected,
        ),
    )
    print(f"selected {len(selected)}/{len(inputs)} trajectories")
    print((args.output_dir / "10_selected.jsonl").resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
