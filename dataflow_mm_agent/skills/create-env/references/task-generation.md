# Task authoring and synthesis v2

## Ownership

Task generation is a dataset-build operation. Runtime receives a reviewed,
reusable `Task`; replay resolves the same `(env_id, task_id)`. Do not ask an LLM
to regenerate init data, goals, or verifier bindings during rollout or replay.

## Strict materialized shape

```json
{
  "schema_version": 2,
  "task_id": "task0001",
  "env_id": "my_env",
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "Solve this."}]}
  ],
  "scenario": {
    "init": {"private_world": {}}
  },
  "verification": {"kind": "my_trusted_binding"}
}
```

`scenario` may be absent or null. Put solver instructions only in `messages`.
Put private fresh-run data only in `scenario.init`; omit Scenario instead of
using an empty init. Runtime step budgets belong
to `RolloutConfig`; Judge rubrics and provenance belong to dataset/pipeline
records, not the Task contract.

Verification JSON is inert and independent of Scenario. Bind each allowed
`kind` through an explicit trusted builder passed to `JsonTaskStore`. Never
import a callable named by task data.

## LLM authoring loop

1. Supply solver-facing Env metadata, the real tool catalog, the Task v2
   schema, family brief, and reviewed examples.
2. Request strict JSON candidates with distinct ids and no Markdown.
3. Parse without semantic repair and reject unknown fields.
4. Validate message content, init domain constraints, ids, bounds, and private
   data separation.
5. Start a fresh Env and solve with a deterministic oracle/domain planner.
6. Replay the oracle trajectory and require the task-bound verifier to pass.
7. Corrupt every required outcome independently and require `failed`.
8. Tamper a control result and require `diverged` before verifier creation.
9. Review instruction clarity, diversity, reachability, leaks, and artifacts.
10. Materialize atomically without overwriting reviewed tasks by default.

For open-ended tasks, omit the verifier. Their ReplayVerify result should be
`not_applicable`; use a Judge as separate quality evidence.

## Prompt skeleton

```text
Author Tasks for Env {env_id}. Return a JSON array only.
Every item must follow Task schema_version 2.

Solver-facing Env metadata:
{environment_context}

Tool catalog:
{tools}

Task family:
{family_brief}

Requirements:
- put instructions in messages;
- add scenario only for private fresh-run transfer;
- use only approved inert verifier kinds;
- do not add budgets, Judge fields, provenance, code, credentials, or paths;
- do not expose hidden expected values in solver-visible messages.
```

Prompt compliance is not acceptance. Code owns every validation and oracle
gate.
