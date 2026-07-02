"""
Sharded, resumable execution helper for large-scale trajectory synthesis.

Running a big seed file through the pipeline in one giant thread pool is fragile:
if it crashes at row 9000/10000 you lose everything and re-run from scratch. This
helper splits the input into **shards**, runs each shard independently into its
own cache directory, and **skips shards already completed** on a re-run -- so a
crash only costs the unfinished shards.

It is deliberately framework-light: you provide a ``run_shard`` callback that
executes whatever operator chain you want on one shard's FileStorage. The helper
handles splitting, per-shard cache isolation, completion markers, and skip logic.

Example
-------
    from dataflow_agent.runner import sharded_run
    from dataflow_agent import AgentExploreGenerator, MockSandboxClient

    def run_shard(storage, llm):
        AgentExploreGenerator(llm_serving=llm, sandbox=MockSandboxClient(),
                              domain="mock", max_workers=8).run(
            storage.step(), input_key="query", output_key="trajectory")

    sharded_run(
        rows=[{"query": q} for q in queries],   # or a jsonl path
        run_shard=run_shard,
        out_dir="./cache/run1",
        shard_size=500,
        run_kwargs={"llm": llm},
    )

Each shard's output lands in ``out_dir/shard_<i>/`` and, once finished, a
``_DONE`` marker file is written. Re-running ``sharded_run`` with the same
``out_dir`` skips every shard that has a ``_DONE`` marker.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Union

from dataflow import get_logger
from dataflow.utils.storage import FileStorage

logger = get_logger()

_DONE_MARKER = "_DONE"


def _load_rows(rows: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Accept either an in-memory list of dict rows or a path to a .jsonl file."""
    if isinstance(rows, list):
        return rows
    if isinstance(rows, str):
        out = []
        with open(rows, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
    raise TypeError(f"rows must be a list[dict] or a jsonl path, got {type(rows)}")


def _chunk(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def sharded_run(
    rows: Union[str, List[Dict[str, Any]]],
    run_shard: Callable[..., Any],
    out_dir: str,
    *,
    shard_size: int = 500,
    cache_type: str = "jsonl",
    input_filename: str = "input.jsonl",
    run_kwargs: Optional[Dict[str, Any]] = None,
    resume: bool = True,
) -> Dict[str, Any]:
    """Split ``rows`` into shards and run each through ``run_shard``, resumably.

    Args:
        rows: list of dict rows, or a path to a .jsonl file.
        run_shard: callback ``run_shard(storage, **run_kwargs)`` that runs the
            operator chain on one shard's :class:`FileStorage`. The storage is
            pre-seeded with the shard's rows as step 0.
        out_dir: root directory; each shard gets ``out_dir/shard_<i>/``.
        shard_size: rows per shard.
        cache_type: FileStorage cache type ("jsonl" / "json" / "parquet" / ...).
        input_filename: name of the per-shard seed file.
        run_kwargs: extra kwargs forwarded to ``run_shard`` (e.g. llm, sandbox).
        resume: skip shards that already have a ``_DONE`` marker (default True).

    Returns:
        A summary dict: {"total_shards", "ran", "skipped", "failed", "shard_dirs"}.
    """
    import pandas as pd

    run_kwargs = run_kwargs or {}
    all_rows = _load_rows(rows)
    shards = _chunk(all_rows, shard_size)
    os.makedirs(out_dir, exist_ok=True)

    ran, skipped, failed = 0, 0, []
    shard_dirs = []

    logger.info(
        f"[sharded_run] {len(all_rows)} rows -> {len(shards)} shards "
        f"(shard_size={shard_size}) into {out_dir}"
    )

    for i, shard in enumerate(shards):
        shard_dir = os.path.join(out_dir, f"shard_{i:04d}")
        shard_dirs.append(shard_dir)
        done_marker = os.path.join(shard_dir, _DONE_MARKER)

        if resume and os.path.exists(done_marker):
            skipped += 1
            logger.info(f"[sharded_run] shard {i} already done -> skip")
            continue

        os.makedirs(shard_dir, exist_ok=True)
        src = os.path.join(shard_dir, input_filename)
        pd.DataFrame(shard).to_json(src, orient="records", lines=True, force_ascii=False)
        storage = FileStorage(
            first_entry_file_name=src, cache_path=shard_dir, cache_type=cache_type,
        )

        try:
            run_shard(storage, **run_kwargs)
        except Exception as exc:  # noqa: BLE001 - isolate per-shard failure
            failed.append(i)
            logger.error(f"[sharded_run] shard {i} FAILED: {exc}")
            continue

        # mark done only on clean completion, so a re-run retries failed shards
        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(f"rows={len(shard)}\n")
        ran += 1
        logger.info(f"[sharded_run] shard {i} done ({len(shard)} rows)")

    summary = {
        "total_shards": len(shards),
        "ran": ran,
        "skipped": skipped,
        "failed": failed,
        "shard_dirs": shard_dirs,
    }
    logger.info(
        f"[sharded_run] complete: ran={ran} skipped={skipped} "
        f"failed={len(failed)}/{len(shards)}"
    )
    return summary
