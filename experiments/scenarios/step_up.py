"""Step up: a staircase of load, then back to the base.

    users
     38 |                         ____
     31 |                    ____|    |
     24 |               ____|         |
     17 |          ____|              |
     10 |_________|                   |__________
        0        15%  30%  45%  60%  75%       100%   of the duration

Five equal steps, then the base again for the last quarter so scale-down shows.
Tests how long each controller takes to settle on each new level, and where
it settles (38 users is about 76 req/s, roughly 9 pods at the HPA's 60% target).
"""

import common
from common import AppUser

STEPS = [10, 17, 24, 31, 38]  # users on each step
STEPS_END = 0.75  # fraction of the run the staircase takes
BASE = STEPS[0]


class User(AppUser):
    """Each user requests /work about twice a second (see common.py)."""


class StepUp(common.DurationShape):
    spawn_rate = 50  # each step lands within a second or two

    def users_at(self, progress):
        if progress >= STEPS_END:
            return BASE
        step = int(progress / (STEPS_END / len(STEPS)))
        return STEPS[min(step, len(STEPS) - 1)]
