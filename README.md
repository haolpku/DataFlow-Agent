<div align="center">

# 🐺 DataFlow-Agent

**Synthesize, score, and refine agent trajectories — high-quality training data for tool-using LLMs.**

Agentic-exploration operators for [DataFlow](https://github.com/OpenDCAI/DataFlow): drive an LLM agent through a **pluggable sandbox** to produce the
`task → [thought → tool call → observation]* → answer` data used to train and evaluate agentic models.

[![tests](https://img.shields.io/badge/tests-42%20passing-brightgreen)](test/test_agentic_explore.py)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)
[![built on](https://img.shields.io/badge/built%20on-open--dataflow-6c8cff)](https://github.com/OpenDCAI/DataFlow)
[![license](https://img.shields.io/badge/license-Apache--2.0-lightgrey)](#)

[Quickstart](#-quickstart) · [Operators](#-the-pipeline) · [Sandboxes](#-pluggable-sandboxes) · [Docs](#-documentation) · [Roadmap](#-roadmap)

</div>

---

## ✨ Why DataFlow-Agent

Most sandboxes only **collect** trajectories. DataFlow-Agent **collects, scores, selects, and repairs** them — turning raw agent rollouts into training-grade data through a full pipeline:

```
                 seed tasks
                     │
   ┌─────────────────▼─────────────────┐
   │  GENERATE   explore a sandbox      │   linear chain  ──►  AgentExploreGenerator
   │             (think → act → observe)│   branching tree ──► AgentExploreTreeGenerator
   └─────────────────┬─────────────────┘
                     │  trajectories
   ┌─────────────────▼─────────────────┐
   │  EVALUATE   LLM-as-judge (4 axes)  │  ──► TrajectoryQualityEvaluator
   └─────────────────┬─────────────────┘
                     │  + quality scores
   ┌─────────────────▼─────────────────┐
   │  SELECT/FILTER  keep the good ones │  ──► TrajectorySelector (top-N diverse)
   │                                    │      TrajectoryFilter    (rule-based gate)
   └─────────────────┬─────────────────┘
                     │  high-quality set
   ┌─────────────────▼─────────────────┐
   │  REFINE     repair the salvageable │  ──► TrajectoryRefiner
   └─────────────────┬─────────────────┘
                     ▼
            training-grade dataset
```

**Zero coupling.** Operators depend only on two abstractions — `LLMServingABC` (decides / judges) and `SandboxClientABC` (executes). They never import a concrete sandbox. Swap the backend, the operators don't change.

---

## 🚀 Quickstart

```bash
# 1. install (open-dataflow provides OperatorABC / LLMServingABC / registry / storage)
pip install open-dataflow
pip install -e .

# 2. run the offline test suite — 42 tests, no network / GPU / API key
pytest test/test_agentic_explore.py -v

# 3. run the offline demo (mock sandbox + scripted LLM)
python examples/agentic_explore/run_mock_pipeline.py
```

`import dataflow_agent` registers every operator into DataFlow's `OPERATOR_REGISTRY`, so they resolve by name like any built-in operator.

### Wiring a real run

```python
import dataflow_agent                                    # registers the operators
from dataflow.serving import APILLMServing_request       # from open-dataflow
from dataflow.utils.storage import FileStorage
from dataflow_agent import AgentExploreGenerator, AgentFlowSandboxClient

storage = FileStorage(first_entry_file_name="queries.jsonl", cache_path="./cache")
llm     = APILLMServing_request(api_url="https://.../v1/chat/completions", model_name="gpt-4o")
sandbox = AgentFlowSandboxClient(base_url="http://127.0.0.1:18890", domain="text2sql")

op = AgentExploreGenerator(llm_serving=llm, sandbox=sandbox, domain="text2sql",
                           max_steps=10, max_workers=8)
op.run(storage.step(), input_key="query", output_key="trajectory")
```

---

## 🧩 The pipeline

| Stage | Operator | What it does | LLM? |
|---|---|---|:--:|
| **Generate** | `AgentExploreGenerator` | Linear trajectory — one think→act→observe chain per task. | ✅ |
| **Generate** | `AgentExploreTreeGenerator` | Branching **trajectory tree** — samples N candidate actions per node, dedups, expands (depth/breadth/node-bounded, AgentFlow-style `depth_threshold`). Emits the tree **and** its root-to-leaf `paths` as linear trajectories. | ✅ |
| **Evaluate** | `TrajectoryQualityEvaluator` | **LLM-as-judge** on 4 axes (goal / efficiency / coherence / tool-use, 1–5) + `overall` ∈ [0,1] + rationale. | ✅ |
| **Select** | `TrajectorySelector` | **Top-N diverse selection** (ported from AgentFlow): score by depth(40)+info(30)+tool-diversity(30), then Jaccard de-dup. Deterministic. | ❌ |
| **Filter** | `TrajectoryFilter` | **Rule-based quality gate** (success / step bounds / parse-error / hallucinated-tool / tool-error / repeated-action loop / empty answer). Deterministic. | ❌ |
| **Refine** | `TrajectoryRefiner` | Re-explores **failed / low-scoring** trajectories primed with a diagnosis of what went wrong; good ones pass through untouched. | ✅ |

> **Select vs Filter** — Filter judges each trajectory good/bad and drops the bad. Selector picks the best *N distinct* from a pool (AgentFlow's "one seed → one tree → N gems").

### Output schema

Every trajectory row is a flat, judge-ready record:

```json
{
  "task": "...",
  "steps": [{"thought": "...", "action": {"tool": "...", "args": {...}}, "observation": ...}],
  "final_answer": "...",
  "num_steps": 3,
  "success": true
}
```

---

## 🔌 Pluggable sandboxes

A sandbox is any `SandboxClientABC` subclass. Adding one = implement `list_tools` + `execute`; the six operators stay untouched.

| Backend | Module | Use case |
|---|---|---|
| `CodingSandboxClient` | `sandbox/coding_client.py` | **Real coding agent** — isolated workspace with `read_file` / `write_file` / `run_python` / `run_tests` (pytest) / `run_shell`. Fix bugs, implement functions, run tests. |
| `AgentFlowSandboxClient` | `sandbox/agentflow_client.py` | Talks to an AgentFlow-protocol sandbox **over HTTP only** (plain `requests`) — imports nothing from any sandbox SDK. Covers all of AgentFlow's text domains (web / rag / text2sql / doc / ds). |
| `MockSandboxClient` | `sandbox/mock_client.py` | Offline, network-free. For tests / dev. |
| *your own* | add a subclass | Wrap any API / MCP server / tool as `ToolResult`. |

### Scope

A **text / structured-domain** explorer (web · rag · sql · doc · ds · coding · shell). Observations are fed back as text, so image/binary observations (GUI/VM `screenshot`) are **out of scope** — that's the multimodal explorer on the roadmap. The transport layer itself is domain-agnostic.

---

## 📚 Documentation

| Doc | Contents |
|---|---|
| [`docs/DESIGN_zh.md`](docs/DESIGN_zh.md) | 设计理念 — 为什么这么设计、五段流水线、可插拔沙箱 |
| [`docs/CAPABILITIES_zh.md`](docs/CAPABILITIES_zh.md) | 能力清单 — 支持哪些环境、能生成哪些数据、成熟度 |
| [`examples/agentic_explore/`](examples/agentic_explore/) | Runnable examples — mock pipeline, coding agent, filter demo, real-API e2e |

---

## 🗺️ Roadmap

- [x] Generator → Evaluator → Filter → **Refiner** loop (repair, not just drop)
- [x] **TrajectorySelector** — AgentFlow's top-N diverse selection algorithm
- [x] **CodingSandboxClient** — real workspace + pytest
- [ ] **Multimodal explorer** for GUI/VM (image observations)
- [ ] **Preference-pair export** (best vs. worst sibling paths → DPO data)

---

<div align="center">
<sub>Part of the <a href="https://github.com/OpenDCAI/DataFlow">OpenDCAI / DataFlow</a> ecosystem.</sub>
</div>
