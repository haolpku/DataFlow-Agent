# DataFlow-Agent — Agentic Explore Operators

Agent-trajectory **data-synthesis** operators for
[DataFlow](https://github.com/OpenDCAI/DataFlow): drive an LLM agent through a
**pluggable sandbox** to generate, score, and filter multi-step exploration
trajectories — the `task → [thought, tool call, observation]* → answer` data
used to train/evaluate agentic models.

This repo is a **drop-in DataFlow extension module**. The files live under
`dataflow/operators/agentic_explore/` so they overlay directly onto a DataFlow
source tree; the operators register themselves via DataFlow's
`@OPERATOR_REGISTRY.register()`.

## The four operators (Generator → Evaluator → Filter loop)

| Operator | Category | What it does |
|---|---|---|
| `AgentExploreGenerator` | generate | Linear trajectory: one thought→action→observation chain per task. |
| `AgentExploreTreeGenerator` | generate | **Branching trajectory tree**: samples N candidate actions per node, dedups, expands (depth/breadth/node-bounded). Emits the tree **and** its root-to-leaf `paths` as linear trajectories, so the Filter/Evaluator consume them unchanged. |
| `TrajectoryQualityEvaluator` | eval | **LLM-as-judge** scoring on 4 rubric axes (goal_achievement / efficiency / coherence / tool_use, 1–5) + an `overall` ∈ [0,1] + rationale. |
| `TrajectoryFilter` | filter | **Deterministic, no-LLM** quality gate (success / step bounds / parse-error / hallucinated-tool / tool-error / repeated-action-loop / empty-answer). Drops failing rows. |

This is the differentiator vs. a sandbox that only *collects* trajectories:
here DataFlow *scores and refines* them.

## Pluggable sandbox, zero coupling

Operators depend only on two abstractions — `dataflow.core.LLMServingABC`
(picks the next action / judges quality) and `SandboxClientABC` (executes tool
calls). They never import a concrete sandbox. Backends are swappable subclasses:

| Backend | Module | Notes |
|---|---|---|
| `MockSandboxClient` | `sandbox/mock_client.py` | Offline, network-free. For tests/dev. |
| `AgentFlowSandboxClient` | `sandbox/agentflow_client.py` | Talks to an AgentFlow-protocol sandbox **over HTTP only** (plain `requests`) — imports nothing from any sandbox SDK. |
| *your own* | add a subclass | Implement `list_tools` + `execute` (+ optional session lifecycle). |

To target a different sandbox, write another `SandboxClientABC` subclass — the
operators are untouched.

## Scope: text / structured domains

This is a **text / structured-domain** explorer (web / rag / sql / doc / ds).
It is not designed for image/binary observations (GUI/VM `screenshot` returns
base64): the loop feeds observations back as text and `LLMServingABC` has no
image channel. A multimodal explorer is future work; the `SandboxClientABC`
transport layer itself is domain-agnostic.

## Install & test

```bash
# 1. overlay onto a DataFlow checkout (operators need DataFlow's core classes)
cp -r dataflow/operators/agentic_explore  <DataFlow>/dataflow/operators/
cp    test/test_agentic_explore.py        <DataFlow>/test/
cp -r examples/agentic_explore            <DataFlow>/examples/

# 2. run the offline test suite (21 tests, no network, no GPU, no API key)
cd <DataFlow> && pytest test/test_agentic_explore.py -v

# 3. offline demo pipeline (mock sandbox + scripted LLM)
PYTHONPATH=. python examples/agentic_explore/run_mock_pipeline.py
```

## Wiring a real run

```python
from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow.operators.agentic_explore.sandbox import AgentFlowSandboxClient
from dataflow.operators.agentic_explore.generate.agent_explore_generator import AgentExploreGenerator

storage = FileStorage(first_entry_file_name="queries.jsonl", cache_path="./cache")
llm = APILLMServing_request(api_url="https://.../v1/chat/completions", model_name="gpt-4o")
sandbox = AgentFlowSandboxClient(base_url="http://127.0.0.1:18890", domain="text2sql")

op = AgentExploreGenerator(llm_serving=llm, sandbox=sandbox, domain="text2sql",
                           max_steps=10, max_workers=8)
op.run(storage.step(), input_key="query", output_key="trajectory")
```

See `examples/agentic_explore/e2e_text2sql.py` for an end-to-end
Generator→Evaluator→Filter run against a real sandbox.

## Output schema

Each trajectory row:

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

- Multimodal explorer for GUI/VM (image observations).
- A `Refiner` that rewrites/repairs low-scoring trajectories (final stage of the
  Generator→Evaluator→Filter→Refiner loop).
- Preference-pair export (best vs. worst sibling paths from the tree) for
  DPO-style agent training data.
