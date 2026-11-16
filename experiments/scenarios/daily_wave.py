"""Daily wave: a smooth, repeating rise and fall, like a day's traffic sped up.

    users
     38 |      _               _
        |    /   \           /   \
     10 |___/     \_______/     \__________
        0        37.5%       75%             100%   of the duration

Two full cycles (low -> high -> low) in the first 75% of the run, then the low
level for the rest so scale-down shows. Tests whether a controller tracks a
predictable cycle or lags behind it (a predictive one should do best here).
"""

import math

import common
from common import AppUser

LOW, HIGH = 10, 38  # users
CYCLES = 2
WAVE_END = 0.75  # fraction of the run the waves take


class User(AppUser):
    """Each user requests /work about twice a second (see common.py)."""


class DailyWave(common.DurationShape):
    def users_at(self, progress):
        if progress >= WAVE_END:
            return LOW
        angle = 2 * math.pi * CYCLES * progress / WAVE_END
        return LOW + (HIGH - LOW) * (1 - math.cos(angle)) / 2
