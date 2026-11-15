"""Smoke test: 5 minutes, to check a setup works before a real run.

100 -> 300 req/s over 2 minutes, hold 3 minutes, stop. Should scale from
2 to ~10 pods. Not for results: it has no cooldown and no simulator twin. Ignores
RUN_LENGTH.
"""

from locust import LoadTestShape

from common import AppUser  # noqa: F401  (Locust finds AppUser here)


class Smoke(LoadTestShape):
    def tick(self):
        t = self.get_run_time()
        if t < 120:
            return round(100 + 200 * t / 120), 100
        if t < 300:
            return 300, 100
        return None  # end the test
