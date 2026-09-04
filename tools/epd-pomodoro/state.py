"""Pomodoro timer state machine and JSON persistence.

The CLI keeps the mutable state (phase, remaining seconds, counters) in a
small JSON file next to the tool, so ``status`` / ``push`` / ``render`` can be
run from another terminal while ``start`` counts down in this one.
``updated_at`` is a wall-clock epoch; a state that was mid-run is rolled
forward by the elapsed wall time when loaded.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

PHASE_WORK = "work"
PHASE_SHORT_BREAK = "short_break"
PHASE_LONG_BREAK = "long_break"
PHASES = (PHASE_WORK, PHASE_SHORT_BREAK, PHASE_LONG_BREAK)

#: Display names as (中文, English).
PHASE_NAMES: dict[str, tuple[str, str]] = {
    PHASE_WORK: ("专注", "FOCUS"),
    PHASE_SHORT_BREAK: ("短休息", "SHORT BREAK"),
    PHASE_LONG_BREAK: ("长休息", "LONG BREAK"),
}

PHASE_DURATION_KEY = {
    PHASE_WORK: "work_minutes",
    PHASE_SHORT_BREAK: "short_minutes",
    PHASE_LONG_BREAK: "long_minutes",
}

DEFAULTS: dict[str, int] = {
    "work_minutes": 25,
    "short_minutes": 5,
    "long_minutes": 15,
    "rounds": 4,
}


def minutes_to_seconds(minutes) -> int:
    return max(1, int(round(float(minutes) * 60)))


def mmss(seconds: int | float) -> str:
    """Render a countdown as MM:SS, rounding up so 0.4 s left shows 00:01."""
    total = max(0, int(math.ceil(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


@dataclass
class PomodoroState:
    """Current position of one Pomodoro session.

    ``remaining`` is refreshed to the second while running. ``pomodoro_count``
    counts finished work sessions inside the current cycle (reset after a long
    break); ``cycle_total`` counts all work sessions finished today.
    """

    phase: str = PHASE_WORK
    phase_seconds: int = 25 * 60
    remaining: int = 25 * 60
    running: bool = False
    pomodoro_count: int = 0
    cycle_total: int = 0
    cycle_date: str = ""
    rounds: int = 4
    updated_at: float = 0.0

    @classmethod
    def from_cfg(cls, cfg: dict) -> PomodoroState:
        work = minutes_to_seconds(cfg.get("work_minutes", DEFAULTS["work_minutes"]))
        rounds = max(1, int(cfg.get("rounds", DEFAULTS["rounds"])))
        return cls(
            phase=PHASE_WORK,
            phase_seconds=work,
            remaining=work,
            rounds=rounds,
            cycle_date=date.today().isoformat(),
        )

    @classmethod
    def load(cls, path: Path) -> PomodoroState | None:
        """Load a saved state; None when missing or unreadable."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        if "phase" in fields and fields["phase"] not in PHASES:
            fields["phase"] = PHASE_WORK
        if "rounds" in fields:
            fields["rounds"] = max(1, int(fields["rounds"]))
        try:
            state = cls(**fields)
        except TypeError:
            return None
        state.phase_seconds = max(1, int(state.phase_seconds))
        state.remaining = max(0, min(int(state.remaining), state.phase_seconds))
        if state.running and state.updated_at:
            elapsed = max(0, int(time.time() - state.updated_at))
            state.remaining = max(0, state.remaining - elapsed)
            if state.remaining == 0:
                state.running = False
        return state

    def save(self, path: Path) -> None:
        self.updated_at = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)


def phase_seconds_for(phase: str, cfg: dict) -> int:
    key = PHASE_DURATION_KEY[phase]
    return minutes_to_seconds(cfg.get(key, DEFAULTS[key]))


def advance(state: PomodoroState, cfg: dict) -> None:
    """Move past the finished phase and start the next one.

    A finished work session increments the cycle counters (daily reset on date
    rollover) and picks a short or long break. A finished break returns to
    work; finishing the long break starts a brand-new cycle.
    """
    today = date.today().isoformat()
    if state.cycle_date != today:
        state.cycle_date = today
        state.cycle_total = 0
    if state.phase == PHASE_WORK:
        state.pomodoro_count += 1
        state.cycle_total += 1
        state.phase = (
            PHASE_LONG_BREAK
            if state.pomodoro_count % state.rounds == 0
            else PHASE_SHORT_BREAK
        )
    else:
        if state.phase == PHASE_LONG_BREAK:
            state.pomodoro_count = 0
        state.phase = PHASE_WORK
    state.phase_seconds = phase_seconds_for(state.phase, cfg)
    state.remaining = state.phase_seconds


def skip(state: PomodoroState, cfg: dict) -> None:
    """Move to the next phase without touching the counters (manual skip)."""
    if state.phase == PHASE_WORK:
        state.phase = PHASE_SHORT_BREAK
    else:
        if state.phase == PHASE_LONG_BREAK:
            state.pomodoro_count = 0
        state.phase = PHASE_WORK
    state.phase_seconds = phase_seconds_for(state.phase, cfg)
    state.remaining = state.phase_seconds
