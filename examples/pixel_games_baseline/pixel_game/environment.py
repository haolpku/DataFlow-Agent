"""Small deterministic visual grid environment used by the example."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

from dataflow_mm_agent.contracts import (
    Env,
    EnvironmentSpec,
    ImageContent,
    RuleSpec,
    Scenario,
    TextContent,
    ToolResult,
    ToolSpec,
)
from dataflow_mm_agent.storage import StatePredicateVerifier


_POINT_SCHEMA: dict[str, Any] = {
    "type": "array",
    "prefixItems": [
        {"type": "integer", "minimum": 0},
        {"type": "integer", "minimum": 0},
    ],
    "items": False,
    "minItems": 2,
    "maxItems": 2,
}


ENV_SPEC = EnvironmentSpec(
    env_id="pixel_game",
    name="Pixel Grid Navigation",
    description=(
        "Navigate a visible grid, avoid blocked cells, collect gems, and end "
        "at the marked goal."
    ),
    rules=(
        RuleSpec(
            "bounded_grid",
            "The BOT cannot move outside the grid or into a blocked cell.",
        ),
        RuleSpec(
            "collect_on_entry",
            "Entering a cell containing a gem permanently collects that gem.",
        ),
        RuleSpec(
            "observe_is_read_only",
            "The observe tool returns the screen without changing state.",
        ),
        RuleSpec(
            "screen_after_play",
            "Every play result includes the updated game screen.",
        ),
    ),
    init_schema={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "columns",
            "rows",
            "start",
            "goal",
            "items",
            "obstacles",
            "mission",
        ],
        "properties": {
            "columns": {"type": "integer", "minimum": 2, "maximum": 10},
            "rows": {"type": "integer", "minimum": 2, "maximum": 10},
            "start": _POINT_SCHEMA,
            "goal": _POINT_SCHEMA,
            "items": {
                "type": "array",
                "items": _POINT_SCHEMA,
                "uniqueItems": True,
            },
            "obstacles": {
                "type": "array",
                "items": _POINT_SCHEMA,
                "uniqueItems": True,
            },
            "mission": {"type": "string", "minLength": 1},
        },
    },
    state_schema={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "player",
            "goal",
            "remaining_items",
            "initial_item_count",
            "collected",
            "mission",
            "moves",
            "closed",
        ],
        "properties": {
            "player": _POINT_SCHEMA,
            "goal": _POINT_SCHEMA,
            "remaining_items": {
                "type": "array",
                "items": _POINT_SCHEMA,
                "uniqueItems": True,
            },
            "initial_item_count": {"type": "integer", "minimum": 0},
            "collected": {"type": "integer", "minimum": 0},
            "mission": {"type": "string", "minLength": 1},
            "moves": {"type": "integer", "minimum": 0},
            "closed": {"type": "boolean"},
        },
    },
    modalities=("text", "image"),
    default_max_steps=8,
    tags=("example", "visual", "navigation"),
)


class PixelGameEnv(Env):
    """One fresh grid-game episode."""

    env_id = ENV_SPEC.env_id
    spec = ENV_SPEC

    def __init__(self) -> None:
        self._scenario: Scenario | None = None
        self._columns = 0
        self._rows = 0
        self._start = (0, 0)
        self._goal = (0, 0)
        self._player = (0, 0)
        self._items: set[tuple[int, int]] = set()
        self._initial_item_count = 0
        self._obstacles: set[tuple[int, int]] = set()
        self._mission = ""
        self._moves = 0
        self._closed = False

    def tools(self) -> Sequence[ToolSpec]:
        return (
            ToolSpec(
                name="observe",
                description="View the current game screen without changing state.",
                operation_type="query",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="play",
                description=(
                    "Move the BOT in one direction for one or more grid steps; "
                    "movement stops at the first blocked cell."
                ),
                operation_type="mutation",
                input_schema={
                    "type": "object",
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                        },
                        "steps": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8,
                            "default": 1,
                        },
                    },
                    "required": ["direction"],
                    "additionalProperties": False,
                },
            ),
        )

    def reset(self, scenario: Scenario, workspace: Path) -> ToolResult:
        del workspace
        if scenario.env_id != self.env_id:
            return ToolResult.failure(
                "wrong_environment", "scenario is not for pixel_game"
            )
        config = scenario.private_config.get("init_config")
        if not isinstance(config, Mapping):
            return ToolResult.failure(
                "invalid_task", "scenario has no materialized init_config"
            )
        try:
            self.spec.validate_init_config(config)
        except ValueError as exc:
            return ToolResult.failure("invalid_task", str(exc))

        self._scenario = scenario
        self._columns = int(config["columns"])
        self._rows = int(config["rows"])
        self._start = tuple(config["start"])
        self._goal = tuple(config["goal"])
        self._player = self._start
        self._items = {tuple(point) for point in config["items"]}
        self._initial_item_count = len(self._items)
        self._obstacles = {tuple(point) for point in config["obstacles"]}
        self._mission = str(config["mission"])
        self._moves = 0
        self._closed = False
        points = {self._start, self._goal, *self._items, *self._obstacles}
        if any(
            point[0] >= self._columns or point[1] >= self._rows
            for point in points
        ):
            return ToolResult.failure("invalid_task", "point outside the grid")
        if (
            self._start == self._goal
            or self._start in self._obstacles
            or self._goal in self._obstacles
            or self._items & self._obstacles
        ):
            return ToolResult.failure("invalid_task", "overlapping task state")
        return self._observation("Initial game screen")

    def call(self, tool_name: str, args: Mapping[str, Any]) -> ToolResult:
        if self._scenario is None or self._closed:
            return ToolResult.failure("not_ready", "environment is not active")
        if tool_name == "observe":
            if args:
                return ToolResult.failure(
                    "invalid_arguments", "observe accepts no arguments"
                )
            return self._observation("Current game screen")
        if tool_name != "play":
            return ToolResult.failure("unknown_tool", f"unknown tool: {tool_name}")
        if set(args).difference({"direction", "steps"}):
            return ToolResult.failure(
                "invalid_arguments", "unsupported play arguments"
            )
        direction = args.get("direction")
        steps = args.get("steps", 1)
        if direction not in {"up", "down", "left", "right"}:
            return ToolResult.failure("invalid_arguments", "invalid direction")
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 8:
            return ToolResult.failure(
                "invalid_arguments", "steps must be an integer from 1 to 8"
            )
        delta = {
            "up": (0, -1),
            "down": (0, 1),
            "left": (-1, 0),
            "right": (1, 0),
        }[str(direction)]
        moved = 0
        for _ in range(steps):
            target = (self._player[0] + delta[0], self._player[1] + delta[1])
            if (
                target[0] < 0
                or target[0] >= self._columns
                or target[1] < 0
                or target[1] >= self._rows
                or target in self._obstacles
            ):
                break
            self._player = target
            self._items.discard(target)
            self._moves += 1
            moved += 1
        return self._observation(f"Moved {moved} step(s) {direction}")

    def _observation(self, status: str) -> ToolResult:
        collected = self._initial_item_count - len(self._items)
        return ToolResult.success((
            TextContent(
                f"{status}. Collected {collected}/{self._initial_item_count}."
            ),
            ImageContent.from_bytes(self._render(), "image/png"),
        ))

    def _render(self) -> bytes:
        width, height = 720, 500
        image = Image.new("RGB", (width, height), "#101827")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((18, 18, width - 18, 78), fill="#1f2a44")
        draw.text((34, 31), f"MISSION: {self._mission}", fill="white", font=font)
        draw.text(
            (34, 52),
            f"COLLECTED {self._initial_item_count - len(self._items)}/"
            f"{self._initial_item_count}   MOVES {self._moves}",
            fill="#facc15",
            font=font,
        )
        left, top = 65, 105
        cell = min(70, (width - 130) // self._columns, (height - 170) // self._rows)
        for y in range(self._rows):
            for x in range(self._columns):
                x0, y0 = left + x * cell, top + y * cell
                draw.rectangle(
                    (x0, y0, x0 + cell, y0 + cell),
                    fill="#172033",
                    outline="#526078",
                    width=2,
                )
                point = (x, y)
                if point in self._obstacles:
                    draw.rectangle(
                        (x0 + 8, y0 + 8, x0 + cell - 8, y0 + cell - 8),
                        fill="#4b5563",
                    )
                if point in self._items:
                    cx, cy = x0 + cell // 2, y0 + cell // 2
                    draw.polygon(
                        [(cx, cy - 15), (cx + 15, cy), (cx, cy + 15), (cx - 15, cy)],
                        fill="#facc15",
                    )
                if point == self._goal:
                    draw.rectangle(
                        (x0 + 4, y0 + 4, x0 + cell - 4, y0 + cell - 4),
                        outline="#34d399",
                        width=5,
                    )
                if point == self._player:
                    draw.ellipse(
                        (x0 + 13, y0 + 13, x0 + cell - 13, y0 + cell - 13),
                        fill="#60a5fa",
                        outline="white",
                        width=3,
                    )
                    draw.text(
                        (x0 + cell // 2 - 10, y0 + cell // 2 - 4),
                        "BOT",
                        fill="#08111f",
                        font=font,
                    )
        start_x = left + self._start[0] * cell
        start_y = top + self._start[1] * cell
        goal_x = left + self._goal[0] * cell
        goal_y = top + self._goal[1] * cell
        draw.text(
            (start_x + 4, start_y + cell - 15),
            "START",
            fill="#93c5fd",
            font=font,
        )
        draw.text(
            (goal_x + 8, goal_y + cell - 15),
            "GOAL",
            fill="#6ee7b7",
            font=font,
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False)
        return buffer.getvalue()

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "player": list(self._player),
            "goal": list(self._goal),
            "remaining_items": [list(point) for point in sorted(self._items)],
            "initial_item_count": self._initial_item_count,
            "collected": self._initial_item_count - len(self._items),
            "mission": self._mission,
            "moves": self._moves,
            "closed": self._closed,
        }

    def close(self) -> None:
        self._closed = True


class PixelGameVerifier(StatePredicateVerifier):
    """Evaluate the task's state predicates against the replayed final snapshot."""

    def __init__(self) -> None:
        super().__init__(env_id=ENV_SPEC.env_id)
