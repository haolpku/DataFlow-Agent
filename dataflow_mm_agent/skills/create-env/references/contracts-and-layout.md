# Agent-MM Env contracts and layout v2

## Recommended package

```text
my-agent-mm-env/
├── pyproject.toml
├── README.md
├── src/my_agent_mm_env/
│   ├── __init__.py          # register(); optional TASKS
│   ├── environment.py       # Env and registration metadata
│   ├── backend.py           # optional external-process/domain seam
│   ├── model.py             # optional canonical state
│   └── tasks/task0001.json  # optional strict Task v2
└── tests/
```

Use installed Agent-MM contracts; never copy them into the Env package.

## Mandatory and optional surfaces

```python
class MyEnv:
    def tools(self) -> Sequence[ToolSpec]: ...
    def call(self, tool_name: str, args: Mapping[str, Any]) -> ToolResult: ...

    # Optional capabilities:
    def start(self, init: Mapping[str, Any] | None, workspace: Path) -> ToolResult | None: ...
    def close(self) -> None: ...
```

An Env can be stateful without requiring a Scenario; `start` then receives
`None`. A stateless MCP can omit `start`. Snapshots, renderers, artifact
codecs, and `verify_task` are optional domain capabilities.

`EnvironmentSpec` contains only stable id, name, description, solver rules,
and modalities. `ToolSpec.operation_type` is `query`, `mutation`, or `unknown`.
Use `unknown` when an upstream declaration does not prove the distinction.

## Tasks and verification

```python
Task(
    task_id="task0001",
    env_id="my_env",
    messages=(Message.text("user", "..."),),
    scenario=Scenario(init={...}),
)
```

Task is required by the runner and reusable across rollouts. Scenario is
private and optional. Replay verifier factories are resolved independently by
`ReplayVerifierResolver`; they receive the live replay Env and immutable
Trajectory, and may return a graded `VerificationResult`.

## Tool and observation rules

- Use one authoritative catalog and unique stable names.
- Prefer closed, bounded JSON schemas.
- Validate semantic constraints and path confinement in the adapter.
- Return `ToolResult.failure` for expected errors.
- Keep failed multi-object mutations atomic.
- Return concise text receipts and images after relevant visual mutations.
- Do not expose shell/eval/unrestricted files or hidden truth-query tools.
- Do not implement `finish`; the runtime owns it.

Use a canonical model only when it materially improves replay, artifacts, or
rendering. External applications may stay authoritative behind an audited
session adapter.

## Registration

```python
from dataflow_mm_agent.env import register_env

def register() -> None:
    register_env(
        "my_env",
        MyEnv,
        name="My Env",
        description="Solver-facing purpose and limits.",
        modalities=("text", "image"),
    )
```

Registration never includes tasks or verifiers. Applications construct a
`JsonTaskStore` or another explicit `TaskResolver` separately.

```toml
[project.entry-points."dataflow_mm_agent.environments"]
my_env = "my_agent_mm_env:register"
```

Pin concrete MCP/browser/file dependencies in the Env distribution and test
the built wheel and sdist, including task/assets when present.
