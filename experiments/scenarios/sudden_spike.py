"""Sudden spike: steady load, an instant jump to 4x for a quarter of the run, then back.

    users
     40 |          ________
        |         |        |
     10 |_________|        |_________________
        0        30%      55%              100%   of the duration

No warning before the jump: a reactive controller is overloaded until its new
pods are ready. The drop back at 55% leaves 45% of the run to watch scale-down.
40 users is about 80 req/s, roughly 9 pods at the HPA's 60% target.
"""

import common
from common import AppUser

BASE, PEAK = 10, 40  # users
SPIKE_START, SPIKE_END = 0.30, 0.55  # fractions of the run


class User(AppUser):
    """Each user requests /work about twice a second (see common.py)."""


class SuddenSpike(common.DurationShape):
    spawn_rate = 100  # all 30 extra users start within a second

    def users_at(self, progress):
        return PEAK if SPIKE_START <= progress < SPIKE_END else BASE
