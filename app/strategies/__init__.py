"""Strategies: the Python part of the market evaluation.

A strategy looks at one `Evaluation` (app/evaluation.py: features, day context, H1/D1 state,
60-minute EMA, ... of a closed M5 bar) and returns the setups it sees as `Candidate`s, each with
entry, stop, target and the evidence it used. The same strategy runs in the backtester
(scripts/backtest.py) and live, where the LLM gets the candidates and makes the recommendation.

Rules for every strategy:
  - pure: decide only from the Evaluation (it holds closed bars only, so no lookahead)
  - deterministic: the same Evaluation always gives the same candidates
  - versioned: change `version` whenever a rule changes; it is stored with every signal

The first real strategy is chosen by Lorant. `demo` only exists to test the plumbing.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Candidate:
    setup: str  # e.g. "H2", "L2", "breakout_pullback" (protocol §4 setup types)
    direction: str  # long | short
    entry: float  # stop-order entry
    stop: float
    target: float | None  # None = runner without a fixed target (management.mode pyramid)
    with_trend: bool
    grade: str = "A"  # A | B: how clean the strategy rates it
    evidence: dict = field(default_factory=dict)  # what the rule saw (logged, shown to the LLM)

    def as_setup(self) -> dict:
        """The dict format of docs/protocol.md §4 (what validate.check_setup expects)."""
        return {
            "direction": self.direction, "type": self.setup, "with_trend": self.with_trend,
            "entry_type": "stop", "entry": self.entry, "stop": self.stop,
            "target": self.target, "grade": self.grade,
        }  # fmt: skip


class Strategy(Protocol):
    name: str
    version: str

    def candidates(self, ev) -> list[Candidate]:  # ev: app.evaluation.Evaluation
        ...


def get_strategy(name: str) -> Strategy:
    from app.strategies.demo import DemoStrategy

    registry: dict[str, type] = {"demo": DemoStrategy}
    if name not in registry:
        raise KeyError(f"unknown strategy {name!r}; known: {', '.join(sorted(registry))}")
    return registry[name]()
