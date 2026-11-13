"""Spike: mirrors experiments/configs/spike.yaml.

100 req/s, then an instant jump to 500 at 7:30 (tick 30) for 5 minutes
(20 ticks), then back to 100. No warning: reactive HPA is overloaded until
new pods start. Cooldown and stop come from common.RateShape.
"""

import common
from common import TICK, AppUser  # noqa: F401  (Locust finds AppUser here)

BASE, PEAK = 100, 500
SPIKE_START = 30 * TICK  # 450 s
SPIKE_END = SPIKE_START + 20 * TICK  # 750 s


class Spike(common.RateShape):
    spawn_rate = 500  # reach the peak within ~1 s, as close to "instant" as Locust gets

    def rate(self, t):
        return PEAK if SPIKE_START <= t < SPIKE_END else BASE
