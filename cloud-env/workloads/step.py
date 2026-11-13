"""Step: load climbs a staircase, 100 -> 200 -> 300 -> 400 -> 500 req/s.

Each step holds for 4 minutes (16 ticks), long enough for HPA to settle
despite the ~1-2 minute metrics delay. Easy to read: on each step you can
see how long HPA takes to react and where the replica count ends up.
"""

import common
from common import TICK, AppUser  # noqa: F401  (Locust finds AppUser here)

STEPS = [100, 200, 300, 400, 500]
STEP_LENGTH = 16 * TICK  # 240 s; 5 steps fill the 20-minute pattern


class Step(common.RateShape):
    def rate(self, t):
        return STEPS[min(int(t // STEP_LENGTH), len(STEPS) - 1)]
