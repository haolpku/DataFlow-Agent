# Agentic Explore Operators

Drive an LLM agent through a **sandbox** to synthesize multi-step exploration
trajectories — agentic training/eval data of the form
`task → [thought, tool call, observation]* → answer`.

## Scope: text / structured domains

`AgentExploreGenerator` is a **text / structured-domain** explorer. It works for
any sandbox domain whose observations are text or JSON-serializable structured
data:

| Domain | Example tools | Works? |
|---|---|---|
| web | `web-search`, `web-visit` | ✅ |
| rag | `rag-search` | ✅ |
| sql (text2sql) | `list_databases`, `get_schema`, `execute` | ✅ |
| doc | `doc-search`, `doc-read` | ✅ |
| ds (data science) | `read_csv`, `run_python`, `inspect_data` | ✅ |
| **vm / gui** | `screenshot` (base64), `click`, `type` | ❌ out of scope |

It is **not** designed for image/binary observations (a GUI/VM `screenshot`
returns base64). Two reasons, both in the agent loop rather than the sandbox
abstraction: (1) observations are fed back to the LLM as text, and (2)
`LLMServingABC.generate_from_input(List[str])` has no image channel. A dedicated
multimodal explorer is future work. The `SandboxClientABC` transport layer
itself is domain-agnostic — only the loop is text-bound.

## Operators

A full **Generator → Evaluator → Filter** loop for agent-trajectory data synthesis:

| Operator | Category | What it does |
|---|---|---|
| `AgentExploreGenerator` | generate | Linear trajectory: one thought→action→observation chain per task. |
| `AgentExploreTreeGenerator` | generate | **Branching trajectory tree**: samples N candidate actions per node, dedups, expands (depth/breadth/node-bounded). Emits the tree **and** its root-to-leaf `paths` as linear trajectories — so the Filter/Evaluator below consume them unchanged. |
| `TrajectoryQualityEvaluator` | eval | **LLM-as-judge** quality scoring on 4 rubric axes (goal_achievement / efficiency / coherence / tool_use, 1–5) + an `overall` ∈ [0,1] + rationale. Writes score columns. |
| `TrajectoryFilter` | filter | **Deterministic, no-LLM** quality gate: success / step bounds / parse-error / hallucinated-tool / tool-error / repeated-action-loop / empty-answer. Drops failing rows. |

Typical pipeline — cheap deterministic gate first, then the expensive judge:

```python
gen   = AgentExploreTreeGenerator(llm_serving=llm, sandbox=sandbox, domain="web")
filt  = TrajectoryFilter(require_success=True, max_repeated_actions=2)
judge = TrajectoryQualityEvaluator(llm_serving=llm)

gen.run(storage.step(),   input_key="query",      output_key="tree")
# (explode tree["paths"] into one-trajectory-per-row here, then:)
filt.run(storage.step(),  input_key="trajectory")                       # rule gate
judge.run(storage.step(), input_key="trajectory", output_key="traj_overall")  # LLM judge
# keep only high-quality: a second TrajectoryFilter on traj_overall, or a score filter
```

This is the differentiator vs. a sandbox that only *collects* trajectories:
DataFlow *scores and refines* them.

## Design: pluggable sandbox, zero coupling

The operators depend only on two abstractions:

- `dataflow.core.LLMServingABC` — picks the next action / judges quality.
- `SandboxClientABC` (this package) — executes tool calls.

It never imports a concrete sandbox. Backends are swappable subclasses:

| Backend | Module | Notes |
|---|---|---|
| `MockSandboxClient` | `sandbox/mock_client.py` | Offline, network-free. For tests/dev. |
| `AgentFlowSandboxClient` | `sandbox/agentflow_client.py` | Talks to an AgentFlow sandbox **over HTTP only** — imports nothing from AgentFlow. |
| *your own* | add a subclass | Implement `list_tools` + `execute` (+ optional session lifecycle). |

> The AgentFlow client reproduces only the wire protocol
> (`/api/v1/execute`, the `{code,message,data,meta}` envelope). DataFlow keeps
> **no code dependency** on AgentFlow. To migrate to a different sandbox, write
> another `SandboxClientABC` subclass — the operators are untouched.

## The contract a sandbox must satisfy

```python
class SandboxClientABC:
    stateful: bool                  # does it hold per-session state (VM/desktop)?
    def list_tools(domain) -> list[ToolSchema]
    def execute(action, params, *, worker_id=None, timeout=None) -> ToolResult
    # optional, no-ops by default:
    def create_session(domain, *, worker_id=None, config=None) -> str | None
    def destroy_session(domain, *, worker_id=None) -> None
    def health_check() -> bool
```

`ToolResult` normalizes every backend onto `{ok, observation, error, code, is_final, ...}`.

## Quick start (offline, no deps beyond pandas)

```bash
python examples/agentic_explore/run_mock_pipeline.py
```

## Wiring a real run

```python
from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow.operators.agentic_explore.sandbox import AgentFlowSandboxClient
from dataflow.operators.agentic_explore.generate.agent_explore_generator import AgentExploreGenerator

storage = FileStorage(first_entry_file_name="queries.jsonl", cache_path="./cache")
llm = APILLMServing_request(api_url="https://.../v1/chat/completions", model_name="gpt-4o")
sandbox = AgentFlowSandboxClient(base_url="http://127.0.0.1:18890", domain="web")
#   domain ∈ {web, rag, vm, sql, doc, ...}; set stateful=True for VM/GUI domains.

op = AgentExploreGenerator(llm_serving=llm, sandbox=sandbox, domain="web",
                           max_steps=15, max_workers=8)
op.run(storage.step(), input_key="query", output_key="trajectory")
```

Each output row's `trajectory` is:

```json
{
  "task": "...",
  "steps": [{"thought": "...", "action": {"tool": "...", "args": {...}}, "observation": ...}],
  "final_answer": "...",
  "num_steps": 3,
  "success": true
}
```

## Roadmap

- **Now**: linear trajectories (`AgentExploreGenerator`) + branching trajectory
  trees (`AgentExploreTreeGenerator`, multi-sample expansion + action dedup +
  depth/breadth/node bounds); LLM-as-judge scoring (`TrajectoryQualityEvaluator`)
  + deterministic rule gate (`TrajectoryFilter`); JSON tool-call protocol,
  thread-pool concurrency, session lifecycle for stateful domains, observation
  truncation, tool-name whitelist validation, parse-error recovery,
  max-steps/node termination.
- **Next**: multimodal explorer for GUI/VM (image observations); a `Refiner`
  that rewrites/repairs low-scoring trajectories (the final stage of the
  Generator→Evaluator→Filter→Refiner loop); preference-pair export (best vs.
  worst sibling paths from the tree) for DPO-style agent training data.

## Tests

```bash
pytest test/test_agentic_explore.py -v   # 21 tests, fully offline
```
