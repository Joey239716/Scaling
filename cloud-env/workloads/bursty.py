"""Bursty: a 100 req/s baseline with short random bursts.

Bursts last 30-90 s, reach 250-500 req/s, and come 60-180 s apart. Tests
whether HPA over-reacts to brief spikes (pods arrive after the burst is over)
or flaps up and down.

The bursts are random but repeatable: the same seed gives the same bursts
every run, so different controllers face identical traffic. Change it with
the BURST_SEED environment variable (default 42). The schedule is printed at
startup.
"""

import os
import random

import common
from common import PATTERN_END, AppUser  # noqa: F401  (Locust finds AppUser here)

BASE = 100
SEED = int(os.environ.get("BURST_SEED", "42"))


def make_bursts(seed):
    """List of (start_s, end_s, req_per_s) bursts within the pattern."""
    rng = random.Random(seed)
    bursts = []
    t = 60  # one quiet minute first
    while True:
        duration = rng.randint(30, 90)
        if t + duration > PATTERN_END:
            break
        bursts.append((t, t + duration, rng.randint(250, 500)))
        t += duration + rng.randint(60, 180)
    return bursts


BURSTS = make_bursts(SEED)
print(f"bursty.py seed={SEED} bursts (start_s, end_s, req/s): {BURSTS}")


class Bursty(common.RateShape):
    spawn_rate = 500  # bursts start and stop within ~1 s

    def rate(self, t):
        for start, end, height in BURSTS:
            if start <= t < end:
                return height
        return BASE
