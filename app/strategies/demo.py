"""DEMO strategy — tests the plumbing only. NOT a trading idea and not a recommendation.

Long: aligned_bull, the evaluated bar is a usable buy signal bar (sig_long) and closed above
the EMA. Entry one tick above its high, stop one tick below its low, target 2R.
Short: the mirror image in aligned_bear.
"""

from app.strategies import Candidate


class DemoStrategy:
    name = "demo"
    version = "0"

    def candidates(self, ev) -> list[Candidate]:
        bar = ev.last
        tick = 10**-ev.digits
        out: list[Candidate] = []
        if ev.alignment == "aligned_bull" and bar["sig_long"] and bar["c"] > bar["ema"]:
            entry, stop = bar["h"] + tick, bar["l"] - tick
            out.append(Candidate("other", "long", entry, stop, entry + 2 * (entry - stop), True,
                                 evidence={"rule": "demo bull signal bar above EMA"}))  # fmt: skip
        if ev.alignment == "aligned_bear" and bar["sig_short"] and bar["c"] < bar["ema"]:
            entry, stop = bar["l"] - tick, bar["h"] + tick
            out.append(Candidate("other", "short", entry, stop, entry - 2 * (stop - entry), True,
                                 evidence={"rule": "demo bear signal bar below EMA"}))  # fmt: skip
        return out
