"""Ramp: mirrors experiments/configs/ramp.yaml.

100 req/s for 2:30 (ticks 0-10), a straight line up to 500 by 12:30
(tick 50), then hold until 20:00. Cooldown and stop come from
common.RateShape. A reactive HPA should cope well here: the load changes
slowly enough that the metrics delay barely hurts.
"""

import common
from common import TICK, AppUser  # noqa: F401  (Locust finds AppUser here)

START, PEAK = 100, 500
RAMP_START = 10 * TICK  # 150 s
RAMP_END = 50 * TICK  # 750 s


class Ramp(common.RateShape):
    def rate(self, t):
        if t < RAMP_START:
            return START
        if t < RAMP_END:
            return START + (PEAK - START) * (t - RAMP_START) / (RAMP_END - RAMP_START)
        return PEAK
