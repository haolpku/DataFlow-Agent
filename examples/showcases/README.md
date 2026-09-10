# DataFlow-MM-Agent showcases

These are GitHub-native walkthroughs of real multimodal trajectories. Each page
contains the natural task prompt, a fading GIF of the complete visual trajectory,
every tool action inside a folded section, the final answer, Judge results, and
ReplayVerify status. Every rendered observation is also preserved at its original
exported resolution inside the corresponding tool-call section. Compact JSON is
provided separately without system prompts, credentials, local workspace paths,
or embedded base64 images.

> **Refine comparisons are never shown as final-only results.** Every page marked
> as refined opens with an explicit notice and includes the complete original
> trajectory, its Judge/ReplayVerify diagnosis, and the complete refined
> trajectory. Refine creates a new rollout and does not overwrite the original.

For before/after comparisons, the original and refined rollouts have separate
full-trajectory animations. The PowerPoint case is an Original-only success:
its 70-step rollout scored 0.9643 against the task-owned rubric, passed the 0.75
gate directly, and did not enter Refine.

<table>
  <tr>
    <td align="center" width="42%">
      <a href="01_geometry_proof.md"><img src="assets/outputs/geometry_tangent_theorem.png" alt="Olympiad geometry construction"></a><br>
      <sub>Progressive Euclidean construction</sub>
    </td>
    <td align="center" width="58%">
      <a href="04_diagram.md"><img src="assets/outputs/lighthouse_incident_response.png" alt="Incident response diagram"></a><br>
      <sub>Two runbook pages synthesized into an editable flow</sub>
    </td>
  </tr>
</table>

## Walkthroughs

| # | Capability | Trajectory coverage | Evaluation |
| ---: | --- | --- | --- |
| 1 | [Image-grounded olympiad geometry](01_geometry_proof.md) | 12-step progressive construction and proof | ReplayVerify passed · Judge 1.00 |
| 2 | [Visual planning in a Pyxel game](02_pixel_game.md) | five gems and goal within a 40-move budget | ReplayVerify passed · Judge 0.85 |
| 3 | [Editable PowerPoint reconstruction](03_pptx.md) | original 70 actions; no Refine | task-owned rubric Judge 0.9643 ≥ 0.75 |
| 4 | [Document pages to an editable diagram](04_diagram.md) | last Refine pass: 38-action input → 45-action result | archived Judge 0.75 → 0.95 |
| 5 | [Why a deterministic verifier is necessary](05_why_deterministic_verifier.md) | original 8 actions → refined 4 actions, both replayed | Judge 0.30 → 1.00; verifier still failed |

The PPT and diagram cases are open-ended authoring tasks. Their
ReplayVerify status is `not_applicable`; this is intentional rather than a
missing implementation. Geometry and game tasks have meaningful exact state
contracts and therefore use deterministic replay verification.

The diagram example shows the final v23 Refine pass, including its actual input
trajectory (itself a previous refinement). Its historical, model-reported Judge
scores predate the current task-owned normalized-mean protocol and are not
directly comparable to the new PPTX score. Manual polishing is not included.

## Output artifacts

- [Editable PowerPoint deck](assets/outputs/northstar_board_update.pptx)
- [Editable Draw.io source](assets/outputs/lighthouse_incident_response.drawio)
- [Incident-flow PNG](assets/outputs/lighthouse_incident_response.png)
- [Final geometry construction](assets/outputs/geometry_tangent_theorem.png)

The reference deck and runbook pages are original repository fixtures; these
showcases do not redistribute a third-party presentation or document.

## Format

Markdown is the published representation because GitHub renders it directly.
The source pipeline still stores canonical JSONL trajectories with full image
content. `export_showcases.py` converts selected rows into:

```text
showcases/
├── 01_...md ... 05_...md     # human-readable walkthroughs
├── assets/                    # referenced PNGs and editable outputs
└── trajectories/              # compact, machine-readable JSON
```

The exporter writes every rendered observation once as a source PNG, embeds it
under the matching tool call, and builds a smaller 64-color GIF overview from the
same complete sequence. Compact trajectory JSON links every step to its exported
images through `observation_images`. Refined cases use a paired JSON document with
separate `original` and `refined` records.
