"""Random bursts: a steady base with short bursts at random times and heights.

    users
     ~40 |      _        __           _
         |     | |      |  |    _    | |
      10 |_____| |______|  |___| |___| |________
         0                              75%     100%   of the duration

Bursts last 2.5-7.5% of the run (15-45 s in a 10-minute run), reach 20-42 users,
and come 5-15% of the run apart. Many end before a new pod is ready, so this
tests whether a controller over-reacts to brief load or flaps up and down.

The bursts are random but the same every run: they come from a fixed seed, so
every controller gets identical traffic. They're placed as fractions of the run,
so --duration stretches the same pattern.
"""

import random

import common
from common import AppUser

BASE = 10  # users between bursts
SEED = 42
BURSTS_START, BURSTS_END = 0.05, 0.75  # bursts only inside this part of the run


def make_bursts(seed: int) -> list[tuple[float, float, int]]:
    """List of (start, end, users), start and end as fractions of the run."""
    rng = random.Random(seed)
    bursts = []
    t = BURSTS_START
    while True:
        length = rng.uniform(0.025, 0.075)
        if t + length > BURSTS_END:
            return bursts
        bursts.append((round(t, 4), round(t + length, 4), rng.randint(20, 42)))
        t += length + rng.uniform(0.05, 0.15)


BURSTS = make_bursts(SEED)


class User(AppUser):
    """Each user requests /work about twice a second (see common.py)."""


class RandomBursts(common.DurationShape):
    spawn_rate = 100  # bursts start and stop within a second

    def users_at(self, progress):
        for start, end, users in BURSTS:
            if start <= progress < end:
                return users
        return BASE
