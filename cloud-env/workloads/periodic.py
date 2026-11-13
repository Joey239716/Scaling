"""Periodic: a smooth daily-style wave between 100 and 400 req/s.

Two full cycles of 10 minutes each: starts at 100, peaks at 400 at 5:00 and
15:00, back to 100 at 10:00 and 20:00. Shows whether HPA tracks a predictable
cycle or lags behind it (its 5-minute scale-down window is half a cycle).
"""

import math

import common
from common import AppUser  # noqa: F401  (Locust finds AppUser here)

LOW, HIGH = 100, 400
PERIOD = 600  # seconds per cycle


class Periodic(common.RateShape):
    def rate(self, t):
        middle, swing = (HIGH + LOW) / 2, (HIGH - LOW) / 2
        return middle - swing * math.cos(2 * math.pi * t / PERIOD)
